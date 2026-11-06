import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from receipt_app.camera import capture_photo
from receipt_app.config import CameraConfig


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.array([[[1, 2, 3], [4, 5, 6]]], dtype=np.uint8)
        self.camera = Mock()
        self.camera.isOpened.return_value = True
        self.camera.read.return_value = (True, self.frame)
        self.cv2 = SimpleNamespace(
            CAP_DSHOW=700,
            CAP_MSMF=1400,
            CAP_ANY=0,
            CAP_PROP_FRAME_WIDTH=3,
            CAP_PROP_FRAME_HEIGHT=4,
            COLOR_BGR2RGB=4,
            VideoCapture=Mock(return_value=self.camera),
            cvtColor=Mock(side_effect=lambda frame, _: frame[:, :, ::-1]),
        )
        self.modules = patch.dict(sys.modules, {"cv2": self.cv2})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_warmup_then_rgb_capture_and_release(self):
        photo = capture_photo(CameraConfig(warmup_frames=3))
        self.cv2.VideoCapture.assert_called_once_with(0, 700)
        self.assertEqual(self.camera.read.call_count, 4)
        self.camera.set.assert_any_call(3, 1280)
        self.camera.set.assert_any_call(4, 720)
        self.assertEqual(photo.mode, "RGB")
        self.assertEqual(photo.size, (2, 1))
        self.assertEqual(photo.getpixel((0, 0)), (3, 2, 1))
        self.frame[:] = 255
        self.assertEqual(photo.getpixel((0, 0)), (3, 2, 1))
        self.camera.release.assert_called_once()

    def test_each_backend_is_explicitly_selected(self):
        for backend, expected in (("dshow", 700), ("msmf", 1400), ("any", 0)):
            with self.subTest(backend=backend):
                self.cv2.VideoCapture.reset_mock()
                capture_photo(CameraConfig(device_index=2, backend=backend, warmup_frames=0))
                self.cv2.VideoCapture.assert_called_once_with(2, expected)

    def test_open_failure_releases_device(self):
        self.camera.isOpened.return_value = False
        with self.assertRaisesRegex(RuntimeError, "カメラを開けません"):
            capture_photo(CameraConfig())
        self.camera.read.assert_not_called()
        self.camera.release.assert_called_once()

    def test_bad_warmup_frame_releases_device(self):
        self.camera.read.return_value = (False, None)
        with self.assertRaisesRegex(RuntimeError, "準備中"):
            capture_photo(CameraConfig(warmup_frames=1))
        self.cv2.cvtColor.assert_not_called()
        self.camera.release.assert_called_once()

    def test_missing_capture_frame_releases_device(self):
        self.camera.read.return_value = (True, None)
        with self.assertRaisesRegex(RuntimeError, "画像を取得"):
            capture_photo(CameraConfig(warmup_frames=0))
        self.camera.release.assert_called_once()

    def test_driver_read_exception_releases_device(self):
        self.camera.read.side_effect = OSError("driver failure")
        with self.assertRaisesRegex(OSError, "driver failure"):
            capture_photo(CameraConfig(warmup_frames=0))
        self.camera.release.assert_called_once()

    def test_conversion_exception_releases_device(self):
        self.cv2.cvtColor.side_effect = ValueError("bad image")
        with self.assertRaisesRegex(ValueError, "bad image"):
            capture_photo(CameraConfig(warmup_frames=0))
        self.camera.release.assert_called_once()

    def test_invalid_backend_does_not_open_camera(self):
        with self.assertRaises(ValueError):
            capture_photo(replace(CameraConfig(), backend="invalid"))
        self.cv2.VideoCapture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
