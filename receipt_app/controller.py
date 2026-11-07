from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime

from .config import AppConfig
from .storage import Storage, Ticket


@dataclass(frozen=True)
class ViewState:
    state: str
    number: int | None
    with_photo: bool | None
    message: str
    busy: bool
    can_retry: bool
    has_image: bool


class Controller:
    def __init__(self, cfg: AppConfig, store: Storage, capture, render, printer,
                 clock=time.monotonic, now=None):
        self.cfg, self.store = cfg, store
        self.capture, self.render, self.printer = capture, render, printer
        self.clock = clock
        self.now = now or (lambda: datetime.now().astimezone().isoformat(timespec="milliseconds"))
        self._lock = threading.RLock()
        self._image = None
        self._deadline = None
        self._busy = False
        self._last_finished = float("-inf")
        self._live_preparation_retry = False
        self._outcome_uncertain = False
        self._ticket: Ticket | None = None
        self._state, self._message = "waiting", ""
        store.recover_interrupted()
        latest = store.latest()
        if latest and latest.status in {"error", "interrupted"}:
            self._ticket = latest
            self._outcome_uncertain = latest.outcome_uncertain
            self._state = "interrupted"
            self._message = (f"前回の受付 {latest.number} は未解決です。{latest.error}\n"
                             "再起動により画像を失ったため再印刷できません。確認して受付を閉じてください。")

    def snapshot(self) -> ViewState:
        with self._lock:
            return ViewState(self._state, self._ticket.number if self._ticket else None,
                             self._ticket.with_photo if self._ticket else None,
                             self._message, self._busy,
                             not self._busy and (self._image is not None or self._live_preparation_retry),
                             self._image is not None)

    def _clear_image(self):
        if self._image is not None:
            self._image.close()
            self._image = None
        self._deadline = None

    def _set(self, state: str, message: str):
        with self._lock:
            self._state, self._message = state, message

    def _begin(self) -> bool:
        if self._busy or self.clock() - self._last_finished < self.cfg.debounce_ms / 1000:
            return False
        self._busy = True
        self._deadline = None
        return True

    def accept(self, with_photo: bool) -> bool:
        with self._lock:
            if self._state in {"error", "interrupted"} or not self._begin():
                return False
            self._clear_image()
            self._ticket = None
            self._outcome_uncertain = False
            self._live_preparation_retry = True
        try:
            self._ticket = self.store.allocate(with_photo, self.now())
            self._process()
        except Exception as exc:
            self._record_error(f"受付を保存できませんでした: {exc}")
        finally:
            self._finish()
        return True

    def retry(self) -> bool:
        self.expire()
        with self._lock:
            if not self._ticket or not (self._image is not None or self._live_preparation_retry):
                return False
            if not self._begin():
                return False
        try:
            self._process()
        finally:
            self._finish()
        return True

    def _process(self):
        ticket = self._ticket
        send_started = False
        photo = None
        try:
            if self._image is None:
                if ticket.with_photo:
                    self._set("capturing", "撮影中です。撮影に失敗した場合は同番号で撮り直せます。")
                    self.store.update(ticket.number, "capturing")
                    photo = self.capture(self.cfg.camera)
                self._set("rendering", "レシート画像を作成しています。")
                self.store.update(ticket.number, "rendering")
                image = self.render(self.cfg.receipt, ticket.number, ticket.created_at, photo)
                with self._lock:
                    self._image = image
                    self._live_preparation_retry = False
            if photo is not None:
                photo.close()
                photo = None
            self._set("printing", "テスト処理中です。" if self.cfg.test_mode else "プリンターへ送信中です。")
            self.store.update(ticket.number, "printing", increment_attempt=True)
            send_started = True
            result = self.printer.print_image(self._image)
            status = "test_completed" if result.test_mode else "completed"
            self.store.update(ticket.number, status, outcome_uncertain=self._outcome_uncertain)
            message = result.detail
            if self._outcome_uncertain:
                message += "\n以前の印刷結果は不明です。同じ番号の紙が重複している可能性があります。"
            self._set("completed", message)
        except Exception as exc:
            # A transport adapter distinguishes pre-send errors from uncertain sends.
            uncertain = getattr(exc, "uncertain", send_started)
            self._outcome_uncertain = self._outcome_uncertain or uncertain
            message = str(exc) or type(exc).__name__
            if self._outcome_uncertain:
                message += "\n受信・印刷結果は不明です。同番号の再印刷でも重複する可能性があります。"
            elif self._image is None:
                message += "\n再試行は同じ番号です。写真ありの場合は撮り直します。"
            self._record_error(message)
        finally:
            if photo is not None:
                photo.close()

    def _record_error(self, message: str):
        if self._ticket:
            try:
                self.store.update(self._ticket.number, "error", message,
                                  outcome_uncertain=self._outcome_uncertain)
            except Exception as exc:
                message += f"\n状態をSQLiteへ保存できませんでした: {exc}"
        else:
            self._live_preparation_retry = False
        self._set("error", message)

    def _finish(self):
        with self._lock:
            self._busy = False
            self._last_finished = self.clock()
            if self._image is not None or self._live_preparation_retry:
                self._deadline = self.clock() + self.cfg.image_retention_seconds

    def expire(self):
        with self._lock:
            if self._busy or self._deadline is None or self.clock() < self._deadline:
                return
            self._clear_image()
            self._live_preparation_retry = False
            if self._state == "completed":
                self._state = "expired"
                self._message = "画像の保持期限が過ぎました。次の受付ができます。"
            else:
                self._message += "\n保持期限が過ぎました。画像は破棄済みで再印刷できません。"

    def discard(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            if self._ticket and self._state in {"error", "interrupted"}:
                try:
                    self.store.update(self._ticket.number, "discarded")
                except Exception as exc:
                    self._message = f"受付を閉じる状態を保存できませんでした: {exc}"
                    return False
            self._clear_image()
            self._live_preparation_retry = False
            self._ticket = None
            self._outcome_uncertain = False
            self._state, self._message = "waiting", ""
            return True

    def shutdown(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._clear_image()
            self._live_preparation_retry = False
            return True

    def preview_image(self):
        self.expire()
        with self._lock:
            return self._image.copy() if self._image is not None and not self._busy else None
