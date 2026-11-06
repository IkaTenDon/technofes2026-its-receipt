from __future__ import annotations

from PIL import Image

from .config import CameraConfig


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
