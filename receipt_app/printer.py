from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
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

    @staticmethod
    def _restart_message() -> str:
        return "接続を安全に解放できなかったため再接続を停止しました。プリンターの状態を確認し、受付アプリを再起動してください。"

    def print_image(self, image: Image.Image) -> PrintResult:
        if self.cfg.test_mode:
            return DryRunPrinter().print_image(image)
        if self._unsafe_connection:
            raise PrintFailure(self._restart_message(), uncertain=self._unsafe_uncertain)
        if not self.cfg.printer.bluetooth_target.strip():
            raise PrintFailure("printer.bluetooth_target に使用するプリンターの名前またはアドレスを設定してください。")
        if image.width != self.cfg.receipt.width_px:
            raise PrintFailure("画像幅と receipt.width_px が異なります。")
        pixels = _bw_pixels(image)
        try:
            api = _load_timini(self.cfg.resolve(self.cfg.printer.source_dir))
            return asyncio.run(self._print(api, image, pixels))
        except PrintFailure:
            raise
        except Exception as exc:
            raise PrintFailure(f"プリンターを準備できません: {exc}") from exc

    async def _print(self, api, image: Image.Image, pixels: bytes) -> PrintResult:
        sending = False

        async def operation():
            nonlocal sending
            cfg = self.cfg.printer
            catalog = api.PrinterCatalog.load()
            discovery = api.BluetoothDiscovery(catalog)
            if cfg.model_key:
                target = await discovery.resolve_transport_target(cfg.bluetooth_target)
                device = catalog.device_from_key(cfg.model_key, transport_target=target.transport_target)
            else:
                device = await discovery.resolve_device(cfg.bluetooth_target)
            printer = await api.connect_printer(
                device, api.BleakBluetoothConnector(), timeout=cfg.operation_timeout_s
            )
            try:
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
                await printer.send_job(job, timeout=cfg.operation_timeout_s)
            finally:
                try:
                    await printer.disconnect()
                except (Exception, asyncio.CancelledError) as exc:
                    self._unsafe_connection = True
                    self._unsafe_uncertain = self._unsafe_uncertain or sending
                    raise PrintFailure(f"接続の解放に失敗しました: {exc}\n{self._restart_message()}",
                                       uncertain=self._unsafe_uncertain) from exc
            return PrintResult(False, "印刷処理が完了しました。機種によって排紙完了は確認できません。用紙を確認してください。")

        try:
            return await asyncio.wait_for(operation(), timeout=self.cfg.printer.total_timeout_s)
        except Exception as exc:
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
