import sys
import gc
import importlib.util
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from receipt_app.config import AppConfig
from receipt_app.controller import Controller
from receipt_app.printer import DryRunPrinter
from receipt_app.rendering import render_receipt
from receipt_app.storage import Storage


class FakeLiveCamera:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.sequence = 0
        self.error = ""

    def start(self):
        self.started = True

    def stop(self, timeout=1.0):
        self.stopped = True
        return True

    def preview(self):
        self.sequence += 1
        if self.error or self.stopped:
            return None, self.error or "停止済み", self.sequence
        return Image.new("RGB", (640, 480), "blue"), "カメラ映像を表示中です。", self.sequence

    def snapshot(self):
        if self.error or self.stopped:
            raise RuntimeError(self.error or "停止済み")
        return Image.new("RGB", (640, 480), "blue")


@unittest.skipUnless(sys.platform == "win32" and importlib.util.find_spec("tkinter"),
                     "Windows with Tcl/Tk is required for GUI tests")
class GuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from receipt_app.gui import ReceiptWindow
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.cfg = AppConfig(Path(self.temp.name))
        self.storage = Storage(Path(self.temp.name) / "test.sqlite3")
        self.capture = Mock(side_effect=AssertionError("camera must not be accessed"))
        self.controller = Controller(self.cfg, self.storage, self.capture, render_receipt, DryRunPrinter())
        self.camera = FakeLiveCamera()
        self.window = ReceiptWindow(self.root, self.cfg, self.controller, self.camera)

    def tearDown(self):
        self.window.executor.shutdown(wait=True)
        self.camera.stop()
        self.controller.discard()
        self.window._cancel_poll()
        self.root.destroy()
        self.window = None
        self.root = None
        gc.collect()
        self.temp.cleanup()

    def settle(self):
        deadline = time.monotonic() + 5
        while self.window.future is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertIsNone(self.window.future)

    def test_gui_can_receive_no_photo_and_show_memory_preview(self):
        self.assertTrue(self.camera.started)
        self.assertIsNotNone(self.window.camera_ref)
        self.window.accept(False)
        self.settle()
        self.assertEqual(self.controller.snapshot().state, "completed")
        self.assertEqual(self.storage.latest().number, 1)
        self.assertIsNotNone(self.window.preview_ref)
        self.capture.assert_not_called()
        self.window.discard()
        self.assertIsNone(self.window.preview_ref)
        self.assertIsNotNone(self.window.camera_ref)

    def test_camera_preview_updates_during_printing(self):
        import threading
        from receipt_app.printer import PrintResult
        entered, release = threading.Event(), threading.Event()

        class BlockingPrinter:
            def print_image(self, _):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test print wait expired")
                return PrintResult(True, "test complete")

        self.controller.printer = BlockingPrinter()
        self.window.accept(False)
        try:
            self.assertTrue(entered.wait(2))
            old = self.window.camera_ref
            deadline = time.monotonic() + 2
            while self.window.camera_ref is old and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertEqual(self.controller.snapshot().state, "printing")
            self.assertIsNot(self.window.camera_ref, old)
            self.assertIsNotNone(self.window.camera_ref)
        finally:
            release.set()
            self.settle()

    def test_camera_error_clears_live_frame_and_no_photo_still_prints(self):
        self.camera.error = "カメラ接続が切れました"
        self.window._update_camera_preview()
        self.assertIsNone(self.window.camera_ref)
        self.assertIn("接続が切れ", self.window.camera_status_var.get())
        self.window.accept(False)
        self.settle()
        self.assertEqual(self.controller.snapshot().state, "completed")
        self.capture.assert_not_called()

    def test_photo_receipt_uses_live_stream_snapshot(self):
        self.controller.capture = Mock(side_effect=lambda _: self.camera.snapshot())
        self.window.accept(True)
        self.settle()
        self.assertEqual(self.controller.snapshot().state, "completed")
        self.controller.capture.assert_called_once()
        self.assertTrue(self.storage.latest().with_photo)

    def test_camera_preview_continues_after_receipt_error(self):
        self.window.accept(True)
        self.settle()
        self.assertEqual(self.controller.snapshot().state, "error")
        old = self.window.camera_ref
        self.window._update_camera_preview()
        self.assertIsNotNone(self.window.camera_ref)
        self.assertIsNot(self.window.camera_ref, old)

    def test_camera_shutdown_has_a_deadline_without_blocking_the_event_loop(self):
        self.camera.stop = Mock(return_value=False)
        finished = Mock(side_effect=self.window._cancel_poll)
        self.window._finish_close = finished
        started = time.monotonic()
        self.window.close()
        self.assertTrue(self.window._closing)
        self.assertFalse(finished.called)
        with patch("receipt_app.gui.time.monotonic", return_value=started + 2.1), \
                patch("receipt_app.gui.sys.stderr", new_callable=io.StringIO) as warnings:
            deadline = time.perf_counter() + 2
            while not finished.called and time.perf_counter() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertTrue(finished.called)
            self.assertIn("解放を確認できない", warnings.getvalue())
        self.camera.stop.assert_called_with(timeout=0)

    def test_artificial_photo_sample_does_not_allocate_or_capture(self):
        self.window.sample(True)
        self.assertIsNone(self.storage.latest())
        self.assertTrue(self.window.is_sample)
        self.assertIsNotNone(self.window.preview_ref)
        self.capture.assert_not_called()

    def test_gui_does_not_queue_a_second_worker(self):
        from concurrent.futures import Future
        pending = Future()
        self.window.future = pending
        self.window.accept(False)
        self.assertIs(self.window.future, pending)
        self.assertIsNone(self.storage.latest())
        self.window.future = None

    def test_held_key_requires_release_before_reuse(self):
        from receipt_app.gui import KeyGate
        gate = KeyGate()
        self.assertTrue(gate.press("F1"))
        self.assertFalse(gate.press("F1"))
        self.assertTrue(gate.press("F2"))
        gate.release("F1")
        self.assertTrue(gate.press("F1"))


@unittest.skipUnless(sys.platform == "win32", "Windows file lock")
class InstanceTests(unittest.TestCase):
    def test_second_instance_is_rejected_and_lock_can_be_reused(self):
        from receipt_app.instance import InstanceLock
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.lock"
            with InstanceLock(path):
                with self.assertRaisesRegex(RuntimeError, "起動済み"):
                    InstanceLock(path)
            with InstanceLock(path):
                pass


if __name__ == "__main__":
    unittest.main()
