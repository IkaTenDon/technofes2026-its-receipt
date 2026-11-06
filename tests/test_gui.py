import sys
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from receipt_app.config import AppConfig
from receipt_app.controller import Controller
from receipt_app.printer import DryRunPrinter
from receipt_app.rendering import render_receipt
from receipt_app.storage import Storage


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
        self.window = ReceiptWindow(self.root, self.cfg, self.controller)

    def tearDown(self):
        self.window.executor.shutdown(wait=True)
        self.controller.discard()
        self.root.destroy()
        self.temp.cleanup()

    def settle(self):
        deadline = time.monotonic() + 5
        while self.window.future is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertIsNone(self.window.future)

    def test_gui_can_receive_no_photo_and_show_memory_preview(self):
        self.window.accept(False)
        self.settle()
        self.assertEqual(self.controller.snapshot().state, "completed")
        self.assertEqual(self.storage.latest().number, 1)
        self.assertIsNotNone(self.window.preview_ref)
        self.capture.assert_not_called()
        self.window.discard()
        self.assertIsNone(self.window.preview_ref)

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
