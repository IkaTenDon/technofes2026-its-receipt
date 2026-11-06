from __future__ import annotations

import threading
import time

from PIL import Image

from .config import CameraConfig


class LiveCamera:
    """One worker owns the device; callers receive independent RAM-only images."""

    def __init__(self, cfg: CameraConfig, *, clock=time.monotonic, max_frame_age_s=1.0):
        self.cfg = cfg
        self.clock = clock
        self.max_frame_age_s = max_frame_age_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._frame = None
        self._captured_at = None
        self._message = "カメラを準備しています。"
        self._sequence = 0

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera-preview", daemon=True)
        self._thread.start()

    def _clear(self, message):
        with self._lock:
            if self._frame is not None:
                self._frame.close()
            self._frame = None
            self._captured_at = None
            self._message = message
            self._sequence += 1

    def _publish(self, image):
        with self._lock:
            if self._stop.is_set():
                image.close()
                return
            previous = self._frame
            self._frame = image
            self._captured_at = self.clock()
            self._message = "カメラ映像を表示中です。写真ありの受付ではこの映像を使用します。"
            self._sequence += 1
            if previous is not None:
                previous.close()

    def _current(self):
        if self._stop.is_set():
            return None, self._message
        if self._frame is None:
            return None, self._message
        if self._captured_at is None or self.clock() - self._captured_at > self.max_frame_age_s:
            self._frame.close()
            self._frame = None
            self._captured_at = None
            self._message = "カメラ映像が更新されていません。接続を確認してください。"
            self._sequence += 1
            return None, self._message
        return self._frame, self._message

    def preview(self):
        with self._lock:
            frame, message = self._current()
            return frame.copy() if frame is not None else None, message, self._sequence

    def snapshot(self):
        with self._lock:
            frame, message = self._current()
            if frame is None:
                raise RuntimeError(message)
            return frame.copy()

    def stop(self, timeout=1.0):
        self._stop.set()
        self._clear("カメラを停止しています。")
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                return False
        return True

    def _run(self):
        try:
            import cv2
        except Exception as exc:
            self._clear(f"カメラを初期化できません: {exc}")
            return

        backends = {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF, "any": cv2.CAP_ANY}
        while not self._stop.is_set():
            camera = None
            try:
                camera = cv2.VideoCapture(self.cfg.device_index, backends[self.cfg.backend])
                if not camera.isOpened():
                    raise RuntimeError("カメラを開けません。接続・カメラ番号・Windowsのカメラ権限を確認してください。")
                camera.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
                warmup = self.cfg.warmup_frames
                while not self._stop.is_set():
                    ok, frame = camera.read()
                    if not ok or frame is None:
                        raise RuntimeError("カメラ映像を取得できません。接続を確認しています。")
                    if warmup:
                        warmup -= 1
                        del frame
                        continue
                    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).copy()
                    del frame
                    self._publish(image)
                    self._stop.wait(0.05)
            except Exception as exc:
                self._clear(f"カメラエラー: {exc}")
            finally:
                if camera is not None:
                    try:
                        camera.release()
                    except Exception as exc:
                        self._clear(f"カメラの解放に失敗しました。アプリを再起動してください: {exc}")
                        return
            if not self._stop.is_set():
                self._stop.wait(1.0)
        self._clear("カメラを停止しました。")


def capture_photo(cfg: CameraConfig) -> Image.Image:
    """Open one camera for this capture and return an independent RGB image.

    OpenCV and some camera drivers can block inside open/read. This synchronous
    operation has no guaranteed hard timeout; run it outside the GUI thread.
    """
    import cv2

    backends = {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF, "any": cv2.CAP_ANY}
    if cfg.backend not in backends:
        raise ValueError("カメラの backend は dshow / msmf / any を指定してください")
    if cfg.device_index < 0 or cfg.warmup_frames < 0:
        raise ValueError("カメラ番号と warmup_frames は0以上にしてください")

    camera = cv2.VideoCapture(cfg.device_index, backends[cfg.backend])
    try:
        if not camera.isOpened():
            raise RuntimeError("カメラを開けません。接続、カメラ番号、Windowsのカメラ権限を確認してください")
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        for _ in range(cfg.warmup_frames):
            ok, frame = camera.read()
            if not ok or frame is None:
                raise RuntimeError("カメラの準備中に画像を取得できませんでした")
        ok, frame = camera.read()
        if not ok or frame is None:
            raise RuntimeError("カメラ画像を取得できませんでした")
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).copy()
    finally:
        camera.release()
