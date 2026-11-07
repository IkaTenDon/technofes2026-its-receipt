from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from .rendering import render_receipt


class KeyGate:
    def __init__(self):
        self.held = set()

    def press(self, key: str) -> bool:
        if key in self.held:
            return False
        self.held.add(key)
        return True

    def release(self, key: str):
        self.held.discard(key)


STATE_LABELS = {
    "waiting": "キー入力待機中", "capturing": "撮影中", "rendering": "画像作成中",
    "printing": "印刷処理中", "completed": "印刷処理完了", "error": "エラー",
    "expired": "画像破棄済み",
}


class ReceiptWindow:
    def __init__(self, root: tk.Tk, cfg, controller, camera):
        self.root, self.cfg, self.controller = root, cfg, controller
        self.camera = camera
        self._closing = False
        self._close_started_at = None
        self._after_id = None
        self.camera_ref = None
        self.camera_token = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="receipt")
        self.future = None
        self.gate = KeyGate()
        self.get_key_state = ctypes.windll.user32.GetAsyncKeyState
        self.get_key_state.argtypes = (ctypes.c_int,)
        self.get_key_state.restype = ctypes.c_short
        self.last_view = None
        self.preview_ref = None
        self.is_sample = False
        self.preview_token = None
        self.buttons = []
        root.title("イベント受付レシート")
        root.geometry("920x760")
        root.minsize(800, 620)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        controls = ttk.Frame(root, padding=20)
        controls.grid(row=0, column=0, sticky="nsew")
        controls.columnconfigure(0, weight=1)
        mode = "テストモード：実印刷しません" if cfg.test_mode else "本番モード：Bluetoothへ送信します"
        ttk.Label(controls, text=mode, font=("Meiryo", 12, "bold")).grid(sticky="w", pady=(0, 18))
        self.state_var, self.number_var, self.option_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.message_var = tk.StringVar()
        ttk.Label(controls, textvariable=self.state_var, font=("Meiryo", 22, "bold")).grid(sticky="w")
        ttk.Label(controls, textvariable=self.number_var, font=("Meiryo", 16)).grid(sticky="w", pady=10)
        ttk.Label(controls, textvariable=self.option_var).grid(sticky="w")
        ttk.Label(controls, textvariable=self.message_var, wraplength=390,
                  justify="left").grid(sticky="w", pady=16)
        self.photo_button = self._button(controls, f"{cfg.photo_key} 写真あり", lambda: self.accept(True))
        self.no_photo_button = self._button(controls, f"{cfg.no_photo_key} 写真なし", lambda: self.accept(False))
        self.retry_button = self._button(controls, "同じ番号で再試行・再印刷", self.retry)
        self.discard_button = self._button(controls, "画像を破棄して受付を閉じる", self.discard)
        self.sample_button = self._button(controls, "見本を表示（写真なし・採番なし）", lambda: self.sample(False))
        self.sample_photo_button = self._button(controls, "見本を表示（人工写真・採番なし）", lambda: self.sample(True))
        ttk.Label(controls, text="この画面にフォーカスがあるときに受付キーが有効です。\n"
                  "処理中・未解決エラー中の新規受付入力は破棄します。\n"
                  f"再印刷用画像は処理終了から{cfg.image_retention_seconds}秒で破棄します。",
                  wraplength=390, justify="left").grid(sticky="w", pady=16)
        preview = ttk.Frame(root, padding=10)
        preview.grid(row=0, column=1, sticky="nsew")
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(4, weight=1)
        ttk.Label(preview, text="カメラプレビュー").grid(row=0, sticky="w")
        self.camera_canvas = tk.Canvas(preview, background="#222222", highlightthickness=0,
                                       width=400, height=225)
        self.camera_canvas.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.camera_status_var = tk.StringVar(value="カメラを準備しています。")
        ttk.Label(preview, textvariable=self.camera_status_var, wraplength=400,
                  justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 12))
        self.preview_var = tk.StringVar(value="レシートプレビュー")
        ttk.Label(preview, textvariable=self.preview_var).grid(row=3, sticky="w")
        self.canvas = tk.Canvas(preview, background="#dddddd", highlightthickness=0, width=400)
        self.canvas.grid(row=4, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(preview, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=4, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        for key in (cfg.photo_key, cfg.no_photo_key):
            root.bind_all(f"<KeyPress-{key}>", self.key_press)
        self.camera.start()
        self._poll()

    def _button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command, takefocus=False)
        button.grid(sticky="ew", pady=5, ipady=5)
        self.buttons.append(button)
        return button

    def key_press(self, event):
        if self.gate.press(event.keysym):
            self.accept(event.keysym == self.cfg.photo_key)
        return "break"

    def _start(self, operation):
        if self._closing or self.future is not None or self.controller.snapshot().busy:
            return
        self._clear_preview()
        self.future = self.executor.submit(operation)

    def accept(self, with_photo):
        if self.controller.snapshot().state != "error":
            controller = self.controller
            self._start(lambda: controller.accept(with_photo))

    def retry(self):
        if not self.controller.snapshot().can_retry:
            return
        if not self.cfg.test_mode and not messagebox.askyesno(
            "同じ番号の再印刷", "印刷済みの場合は重複します。写真が失われた撮影失敗時は撮り直します。\n同じ番号で再試行しますか？",
            parent=self.root,
        ):
            return
        self._start(self.controller.retry)

    def discard(self):
        if self.controller.discard():
            self._clear_preview()

    def _clear_preview(self):
        self.canvas.delete("all")
        self.canvas.configure(scrollregion=(0, 0, 1, 1))
        self.preview_ref = None
        self.preview_token = None
        self.is_sample = False

    def _show_image(self, image):
        self._clear_preview()
        try:
            display = image.copy()
            if display.width > 400:
                display = display.resize((400, round(display.height * 400 / display.width)), Image.Resampling.NEAREST)
            self.preview_ref = ImageTk.PhotoImage(display)
            self.canvas.create_image(8, 8, image=self.preview_ref, anchor="nw")
            self.canvas.configure(scrollregion=(0, 0, display.width + 16, display.height + 16))
            self.canvas.yview_moveto(0)
            display.close()
        finally:
            image.close()

    def sample(self, with_photo):
        if self._closing or self.future is not None:
            return
        photo = Image.linear_gradient("L").convert("RGB") if with_photo else None
        try:
            image = render_receipt(self.cfg.receipt, 1, datetime.now().astimezone().isoformat(timespec="milliseconds"), photo)
            self._show_image(image)
            self.is_sample = True
        except Exception as exc:
            messagebox.showerror("見本を描画できません", str(exc), parent=self.root)
        finally:
            if photo:
                photo.close()

    def _update_camera_preview(self):
        image, message, sequence = self.camera.preview()
        self.camera_status_var.set(message)
        token = sequence, message
        try:
            if token == self.camera_token:
                return
            self.camera_canvas.delete("all")
            self.camera_ref = None
            if image is not None:
                image.thumbnail((400, 225), Image.Resampling.LANCZOS)
                self.camera_ref = ImageTk.PhotoImage(image)
                self.camera_canvas.create_image(200, 112, image=self.camera_ref, anchor="center")
            self.camera_token = token
        finally:
            if image is not None:
                image.close()

    def _poll(self):
        self._after_id = None
        self._update_camera_preview()
        if self._closing:
            if self.camera.stop(timeout=0):
                self._finish_close()
            elif time.monotonic() - self._close_started_at >= 2.0:
                print("警告: カメラドライバーの解放を確認できないため、停止要求を維持して終了します。", file=sys.stderr)
                self._finish_close()
            else:
                self.message_var.set("カメラの終了を待っています。ドライバーが停止した場合は自動解放できないことがあります。")
                self._after_id = self.root.after(50, self._poll)
            return
        for key in (self.cfg.photo_key, self.cfg.no_photo_key):
            if not (self.get_key_state(0x70 + int(key[1:]) - 1) & 0x8000):
                self.gate.release(key)
        if self.future is not None and self.future.done():
            try:
                self.future.result()
            except Exception as exc:
                messagebox.showerror("受付処理エラー", str(exc), parent=self.root)
            self.future = None
        self.controller.expire()
        view = self.controller.snapshot()
        self.state_var.set(STATE_LABELS.get(view.state, view.state))
        self.number_var.set(f"来場番号 {view.number:06d}" if view.number is not None else "来場番号 未発行")
        self.option_var.set("写真あり" if view.with_photo else "写真なし" if view.with_photo is not None else "")
        self.message_var.set(view.message)
        busy = view.busy or self.future is not None
        for button in self.buttons:
            button.configure(state="disabled" if busy else "normal")
        if view.state == "error":
            self.photo_button.configure(state="disabled")
            self.no_photo_button.configure(state="disabled")
        self.retry_button.configure(state="normal" if view.can_retry and not busy else "disabled")
        token = (view.number, view.state, view.message)
        if view.has_image and not busy and not self.is_sample and self.preview_token != token:
            image = self.controller.preview_image()
            if image is not None:
                self._show_image(image)
                self.preview_token = token
        elif (busy or not view.has_image) and self.preview_ref is not None and not self.is_sample:
            self._clear_preview()
        self.last_view = view
        self._after_id = self.root.after(50, self._poll)

    def _cancel_poll(self):
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None

    def close(self):
        if self._closing:
            return
        if self.future is not None or self.controller.snapshot().busy:
            messagebox.showinfo("処理中です", "処理終了後に閉じてください。強制終了後は送信結果が不明になる場合があります。", parent=self.root)
            return
        self._closing = True
        self._close_started_at = time.monotonic()
        for button in self.buttons:
            button.configure(state="disabled")
        if self.camera.stop(timeout=0):
            self._finish_close()

    def _finish_close(self):
        self._cancel_poll()
        if not self.controller.shutdown():
            return
        self._clear_preview()
        self.camera_canvas.delete("all")
        self.camera_ref = None
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()
