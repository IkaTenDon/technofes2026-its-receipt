from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from .config import AppConfig

PINNED_COMMIT = "4aaf9459f1d7854bdca5bafffd86756704b76376"


@dataclass(frozen=True)
class PrintResult:
    test_mode: bool
    detail: str


class PrintFailure(Exception):
    def __init__(self, message: str, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


class DryRunPrinter:
    def print_image(self, image: Image.Image) -> PrintResult:
        return PrintResult(True, f"テスト完了: {image.width}×{image.height} px。実際の印刷・画像保存は行っていません。")

    def close(self) -> None:
        pass


def verify_source(source_dir: Path) -> None:
    source_dir = source_dir.resolve()
    if not (source_dir / "timiniprint" / "__init__.py").is_file():
        raise PrintFailure("TiMini Print が未導入です。scripts/setup.ps1 を実行してください。")
    marker = source_dir / "PINNED_COMMIT.txt"
    revision = ""
    if (source_dir / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                revision = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not revision and marker.is_file():
        revision = marker.read_text(encoding="utf-8-sig").strip()
    if revision != PINNED_COMMIT:
        raise PrintFailure(f"TiMini Print の版が一致しません。必要な commit: {PINNED_COMMIT}")


def _load_timini(source_dir: Path) -> SimpleNamespace:
    source_dir = source_dir.resolve()
    verify_source(source_dir)
    existing = sys.modules.get("timiniprint")
    if existing is not None and Path(existing.__file__).resolve().parent != source_dir / "timiniprint":
        raise PrintFailure("別の場所の TiMini Print が読み込まれています。設定を確認してアプリを再起動してください。")
    if str(source_dir) not in sys.path:
        sys.path.insert(0, str(source_dir))
    try:
        devices = importlib.import_module("timiniprint.devices")
        bluetooth = importlib.import_module("timiniprint.transport.bluetooth")
        connected = importlib.import_module("timiniprint.printing.connected")
        transport = importlib.import_module("timiniprint.transport.base")
        settings = importlib.import_module("timiniprint.printing.settings")
        paper = importlib.import_module("timiniprint.printing.paper")
        raster = importlib.import_module("timiniprint.raster")
    except ImportError as exc:
        raise PrintFailure(f"TiMini Print の依存関係を読み込めません: {exc}") from exc
    return SimpleNamespace(
        PrinterCatalog=devices.PrinterCatalog,
        BluetoothDiscovery=bluetooth.BluetoothDiscovery,
        BleakBluetoothConnector=bluetooth.BleakBluetoothConnector,
        connect_printer=connected.connect_printer,
        ConnectionCleanupError=transport.ConnectionCleanupError,
        PrintSettings=settings.PrintSettings,
        resolve_paper=paper.resolve_paper,
        RasterBuffer=raster.RasterBuffer,
        RasterSet=raster.RasterSet,
        PixelFormat=raster.PixelFormat,
    )


def _validate_paper(paper, image: Image.Image) -> None:
    if image.width != paper.render_width_px:
        raise PrintFailure(
            f"画像幅 {image.width} px と機種の印字幅 {paper.render_width_px} px が異なります。"
            "receipt.width_px と printer.paper_preset を確認してください。自動リサイズはしません。"
        )
    if paper.render_height_px is not None or paper.raster_height_px is not None:
        raise PrintFailure("固定長用紙の設定です。可変長のロール紙プリセットを選択してください。")
    if paper.rotation_degrees != 0 or paper.render_height_scale != 1.0:
        raise PrintFailure("回転・縦方向の倍率を必要とする用紙には対応していません。")
    if paper.max_height_px is not None and image.height + paper.top_padding_px > paper.max_height_px:
        raise PrintFailure(f"レシートの高さが機種の制限 {paper.max_height_px} px を超えています。")


def _bw_pixels(image: Image.Image) -> bytes:
    if image.mode != "1":
        raise PrintFailure("印刷には描画済みの二値画像（mode 1）が必要です。")
    return image.convert("L").tobytes().translate(bytes(1 if value < 128 else 0 for value in range(256)))


class TiMiniPrinter:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._unsafe_connection = False
        self._unsafe_uncertain = False
        self._request_lock = threading.Lock()
        self._loop_ready = threading.Event()
        self._stop_before_start = threading.Event()
        self._loop = None
        self._loop_error = None
        self._thread = None
        self._session = None
        self._ever_sent = False
        self._closed = False

    def _run_loop(self):
        try:
            loop = asyncio.new_event_loop()
        except Exception as exc:
            self._loop_error = exc
            self._loop_ready.set()
            return
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._loop_ready.set()
        try:
            if not self._stop_before_start.is_set():
                loop.run_forever()
        finally:
            pending = list(asyncio.all_tasks(loop))
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor(timeout=self.cfg.printer.operation_timeout_s))
            loop.close()
            asyncio.set_event_loop(None)

    def _start_loop(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run_loop, name="receipt-printer", daemon=True)
            self._thread.start()
        if not self._loop_ready.wait(timeout=5) or self._loop is None or self._loop.is_closed():
            raise PrintFailure("印刷用のイベントループを起動できませんでした。アプリを再起動してください。") from self._loop_error

    @staticmethod
    def _restart_message() -> str:
        return "接続を安全に解放できなかったため再接続を停止しました。プリンターの状態を確認し、受付アプリを再起動してください。"

    def print_image(self, image: Image.Image) -> PrintResult:
        with self._request_lock:
            if self._unsafe_connection:
                raise PrintFailure(self._restart_message(), uncertain=self._unsafe_uncertain)
            if self._closed:
                raise PrintFailure("プリンター処理は終了済みです。アプリを再起動してください。")
            if self.cfg.test_mode:
                return DryRunPrinter().print_image(image)
            if not self.cfg.printer.bluetooth_target.strip():
                raise PrintFailure("printer.bluetooth_target に使用するプリンターの名前またはアドレスを設定してください。")
            if image.width != self.cfg.receipt.width_px:
                raise PrintFailure("画像幅と receipt.width_px が異なります。")
            pixels = _bw_pixels(image)
            try:
                api = _load_timini(self.cfg.resolve(self.cfg.printer.source_dir))
                self._start_loop()
                return asyncio.run_coroutine_threadsafe(self._print(api, image, pixels), self._loop).result()
            except PrintFailure:
                raise
            except Exception as exc:
                raise PrintFailure(f"プリンターを準備できません: {exc}") from exc

    async def _disconnect_session(self, *, uncertain: bool):
        if self._session is None:
            return
        try:
            await self._session.disconnect()
        except (Exception, asyncio.CancelledError) as exc:
            self._unsafe_connection = True
            self._unsafe_uncertain = self._unsafe_uncertain or uncertain
            raise PrintFailure(f"接続の解放に失敗しました: {exc}\n{self._restart_message()}",
                               uncertain=self._unsafe_uncertain) from exc
        self._session = None

    def close(self) -> None:
        """Release the persistent connection after the operator checks the paper.

        A stuck OS driver may outlive the caller's budget. In that case cleanup
        remains on the daemon loop and stops it once I/O has settled; the error
        is reported instead of closing an event loop still used by worker I/O.
        """
        budget = self.cfg.printer.operation_timeout_s
        deadline = time.monotonic() + budget
        if not self._request_lock.acquire(timeout=budget):
            raise PrintFailure("印刷処理が終わらないため接続を解放できません。用紙とプリンターを確認してください。",
                               uncertain=self._ever_sent)
        try:
            if self._closed:
                return
            self._closed = True
            if self._thread is None:
                return
            if self._loop is None:
                self._stop_before_start.set()
                if not self._thread.is_alive():
                    return
                raise PrintFailure("印刷用イベントループの起動・終了が確認できません。アプリを再起動してください。")
            if self._loop.is_closed():
                return
            future = asyncio.run_coroutine_threadsafe(
                self._disconnect_session(uncertain=self._ever_sent), self._loop,
            )
            future.add_done_callback(lambda _: self._loop.call_soon_threadsafe(self._loop.stop))
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeoutError as exc:
                self._unsafe_connection = True
                self._unsafe_uncertain = self._unsafe_uncertain or self._ever_sent
                raise PrintFailure("接続の終了待ちがタイムアウトしました。印刷・解放の完了は確認できません。",
                                   uncertain=self._unsafe_uncertain) from exc
            finally:
                self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if self._thread.is_alive():
                raise PrintFailure("印刷用の終了処理が完了していません。プリンターの状態を確認してください。",
                                   uncertain=self._ever_sent)
        finally:
            self._request_lock.release()

    async def _print(self, api, image: Image.Image, pixels: bytes) -> PrintResult:
        sending = False

        async def operation():
            nonlocal sending
            cfg = self.cfg.printer
            if self._session is None:
                catalog = api.PrinterCatalog.load()
                discovery = api.BluetoothDiscovery(catalog)
                if cfg.model_key:
                    target = await discovery.resolve_transport_target(cfg.bluetooth_target)
                    device = catalog.device_from_key(cfg.model_key, transport_target=target.transport_target)
                else:
                    device = await discovery.resolve_device(cfg.bluetooth_target)
                self._session = await api.connect_printer(
                    device, api.BleakBluetoothConnector(), timeout=cfg.operation_timeout_s
                )
            printer = self._session
            settings = api.PrintSettings(
                image_mode="threshold", paper_preset_key=cfg.paper_preset or None,
                blackening=cfg.blackening, feed_padding=cfg.feed_padding,
                trim_side_margins=False, trim_top_bottom_margins=False,
            )
            paper = api.resolve_paper(printer.printer_device(), settings)
            _validate_paper(paper, image)
            raster = api.RasterBuffer(pixels=pixels, width=image.width, pixel_format=api.PixelFormat.BW1)
            job = printer.raster_job(api.RasterSet.from_single(raster), is_text=False, settings=settings)
            sending = True
            self._ever_sent = True
            await printer.send_job(job, timeout=cfg.operation_timeout_s)
            return PrintResult(False, "印刷データを送信しました。接続を保持しています。用紙の印刷完了を確認してください。")

        try:
            return await asyncio.wait_for(operation(), timeout=self.cfg.printer.total_timeout_s)
        except Exception as exc:
            if self._session is not None:
                await self._disconnect_session(uncertain=sending)
            if api.ConnectionCleanupError.is_cause_of(exc):
                self._unsafe_connection = True
                self._unsafe_uncertain = self._unsafe_uncertain or sending
                raise PrintFailure(f"接続の初期化・解放に失敗しました: {exc}\n{self._restart_message()}",
                                   uncertain=self._unsafe_uncertain) from exc
            if isinstance(exc, PrintFailure):
                if sending and not exc.uncertain:
                    raise PrintFailure(str(exc), uncertain=True) from exc
                raise
            if isinstance(exc, TimeoutError):
                raise PrintFailure("プリンター処理がタイムアウトしました。接続・用紙を確認してください。", uncertain=sending) from exc
            raise PrintFailure(f"プリンター処理に失敗しました: {exc}", uncertain=sending) from exc
