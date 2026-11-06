import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from receipt_app.camera import LiveCamera
from receipt_app.config import CameraConfig


class LiveCameraTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((2, 3, 3), dtype=np.uint8)
        self.frame[:, :, :] = (1, 2, 3)
        self.device = Mock()
        self.device.isOpened.return_value = True
        self.device.read.side_effect = lambda: (True, self.frame.copy())
        self.cv2 = SimpleNamespace(
            CAP_DSHOW=700, CAP_MSMF=1400, CAP_ANY=0,
            CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, COLOR_BGR2RGB=4,
            VideoCapture=Mock(return_value=self.device),
            cvtColor=lambda frame, _: frame[:, :, ::-1],
        )
        modules = patch.dict(sys.modules, {"cv2": self.cv2})
        modules.start()
        self.addCleanup(modules.stop)

    def camera(self, **kwargs):
        stream = LiveCamera(CameraConfig(warmup_frames=2), **kwargs)
        self.addCleanup(stream.stop)
        return stream

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(condition())

    @staticmethod
    def ready(stream):
        image, _, _ = stream.preview()
        if image is None:
            return False
        image.close()
        return True

    def test_one_worker_owns_device_and_snapshots_are_independent(self):
        owners = []
        self.cv2.VideoCapture.side_effect = lambda *_: owners.append(threading.get_ident()) or self.device
        self.device.release.side_effect = lambda: owners.append(threading.get_ident())
        stream = self.camera()
        stream.start()
        stream.start()
        self.wait_for(lambda: self.ready(stream))
        first = stream.snapshot()
        first.putpixel((0, 0), (255, 255, 255))
        second = stream.snapshot()
        self.assertEqual(second.getpixel((0, 0)), (3, 2, 1))
        first.close()
        second.close()
        self.assertEqual(self.cv2.VideoCapture.call_count, 1)
        self.device.release.assert_not_called()
        self.assertTrue(stream.stop())
        self.device.release.assert_called_once()
        self.assertEqual(owners[0], owners[1])
        self.assertNotEqual(owners[0], threading.get_ident())
        with self.assertRaises(RuntimeError):
            stream.snapshot()

    def test_stale_frame_is_neither_displayed_nor_captured(self):
        clock = [5.0]
        stream = self.camera(clock=lambda: clock[0])
        stream._publish(Image.new("RGB", (3, 2), "red"))
        self.assertTrue(self.ready(stream))
        clock[0] = 6.1
        image, message, _ = stream.preview()
        self.assertIsNone(image)
        self.assertIn("更新されていません", message)
        with self.assertRaisesRegex(RuntimeError, "更新されていません"):
            stream.snapshot()

    def test_read_failure_removes_last_frame(self):
        allow_failure = threading.Event()
        reads = [0]

        def read():
            reads[0] += 1
            if reads[0] > 3:
                allow_failure.wait(2)
                return False, None
            return True, self.frame.copy()

        self.device.read.side_effect = read
        stream = self.camera()
        self.addCleanup(allow_failure.set)
        stream.start()
        self.wait_for(lambda: self.ready(stream))
        allow_failure.set()
        self.wait_for(lambda: "カメラエラー" in stream.preview()[1])
        self.assertIsNone(stream.preview()[0])
        with self.assertRaises(RuntimeError):
            stream.snapshot()
        stream.stop()
        self.device.release.assert_called_once()

    def test_open_failure_is_reported_without_a_stale_frame(self):
        self.device.isOpened.return_value = False
        stream = self.camera()
        stream.start()
        self.wait_for(lambda: "開けません" in stream.preview()[1])
        self.assertIsNone(stream.preview()[0])
        self.assertTrue(stream.stop())
        self.device.release.assert_called_once()

    def test_blocked_read_has_bounded_stop_and_releases_on_owner_when_unblocked(self):
        entered, unblock = threading.Event(), threading.Event()

        def read():
            entered.set()
            unblock.wait(3)
            return True, self.frame.copy()

        self.device.read.side_effect = read
        stream = self.camera()
        self.addCleanup(unblock.set)
        stream.start()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        self.assertFalse(stream.stop(timeout=0.01))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsNone(stream.preview()[0])
        self.device.release.assert_not_called()
        unblock.set()
        self.assertTrue(stream.stop())
        self.device.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
