from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from receipt_app.config import AppConfig
from receipt_app.controller import Controller
from receipt_app.storage import Storage


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds=2.0):
        self.value += seconds


class UncertainFailure(RuntimeError):
    uncertain = True


class CertainFailure(RuntimeError):
    uncertain = False


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = AppConfig(base_dir=Path(self.temp.name))
        self.store = Storage(self.cfg.resolve(self.cfg.database_path))
        self.clock = FakeClock()
        self.capture = Mock(side_effect=lambda _: Image.new("RGB", (40, 30), "gray"))
        self.render = Mock(side_effect=lambda *_: Image.new("1", (384, 200), 1))
        self.printer = Mock()
        self.printer.print_image.return_value = SimpleNamespace(test_mode=True, detail="test complete")
        self.controller = self.make_controller()

    def make_controller(self):
        return Controller(self.cfg, self.store, self.capture, self.render, self.printer,
                          clock=self.clock, now=lambda: "2026-11-07T12:34:56+09:00")

    def test_no_photo_never_accesses_camera_and_passes_none(self):
        self.assertTrue(self.controller.accept(False))
        self.capture.assert_not_called()
        args = self.render.call_args.args
        self.assertEqual(args[1:3], (1, "2026-11-07T12:34:56+09:00"))
        self.assertIsNone(args[3])
        self.assertEqual(self.controller.snapshot().state, "completed")
        self.assertEqual(self.store.get(1).status, "test_completed")

    def test_failure_retries_same_number_datetime_and_image_without_recapture(self):
        self.printer.print_image.side_effect = [UncertainFailure("wire disconnected"),
                                                SimpleNamespace(test_mode=False, detail="sent")]
        self.assertTrue(self.controller.accept(True))
        first_image = self.printer.print_image.call_args.args[0]
        first_ticket = self.store.get(1)
        self.assertEqual(self.controller.snapshot().state, "error")
        self.assertTrue(self.controller.snapshot().can_retry)
        self.assertIn("重複", self.controller.snapshot().message)
        self.assertFalse(self.controller.accept(False))
        self.clock.advance()
        self.assertTrue(self.controller.retry())
        self.assertIs(self.printer.print_image.call_args.args[0], first_image)
        self.capture.assert_called_once()
        self.render.assert_called_once()
        self.assertEqual(self.store.latest().number, 1)
        self.assertEqual(self.store.get(1).created_at, first_ticket.created_at)
        self.assertEqual(self.store.get(1).attempts, 2)
        self.assertEqual(self.store.get(1).status, "completed")

    def test_capture_failure_can_recapture_on_same_number(self):
        self.capture.side_effect = [RuntimeError("camera unavailable"), Image.new("RGB", (20, 20))]
        self.controller.accept(True)
        self.assertEqual(self.controller.snapshot().number, 1)
        self.assertTrue(self.controller.snapshot().can_retry)
        self.assertFalse(self.controller.snapshot().has_image)
        self.assertIn("撮り直", self.controller.snapshot().message)
        self.printer.print_image.assert_not_called()
        self.clock.advance()
        self.assertTrue(self.controller.retry())
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(self.store.latest().number, 1)
        self.assertEqual(self.store.get(1).attempts, 1)

    def test_busy_input_is_discarded_instead_of_queued(self):
        started, finish = threading.Event(), threading.Event()
        result = []

        def blocked_print(_image):
            started.set()
            if not finish.wait(5):
                raise RuntimeError("test worker timed out")
            return SimpleNamespace(test_mode=True, detail="done")

        self.printer.print_image.side_effect = blocked_print
        worker = threading.Thread(target=lambda: result.append(self.controller.accept(False)))
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            self.assertTrue(self.controller.snapshot().busy)
            self.assertFalse(self.controller.accept(True))
            self.assertFalse(self.controller.retry())
            self.assertFalse(self.controller.discard())
            self.assertIsNone(self.controller.preview_image())
        finally:
            finish.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(self.store.latest().number, 1)
        self.capture.assert_not_called()

    def test_debounce_starts_at_completion(self):
        self.controller.accept(False)
        self.assertFalse(self.controller.accept(True))
        self.assertFalse(self.controller.retry())
        self.clock.advance(self.cfg.debounce_ms / 1000 - 0.01)
        self.assertFalse(self.controller.accept(False))
        self.clock.advance(0.02)
        self.assertTrue(self.controller.accept(False))
        self.assertEqual(self.store.latest().number, 2)

    def test_failed_image_expires_and_blocks_new_ticket_until_discard(self):
        self.printer.print_image.side_effect = CertainFailure("printer absent")
        self.controller.accept(True)
        self.clock.advance(self.cfg.image_retention_seconds)
        self.controller.expire()
        state = self.controller.snapshot()
        self.assertFalse(state.has_image)
        self.assertFalse(state.can_retry)
        self.assertEqual(state.state, "error")
        self.assertIn("保持期限", state.message)
        self.assertFalse(self.controller.retry())
        self.assertFalse(self.controller.accept(False))
        self.assertTrue(self.controller.discard())
        self.assertEqual(self.store.get(1).status, "discarded")
        self.printer.print_image.side_effect = None
        self.assertTrue(self.controller.accept(False))
        self.assertEqual(self.store.latest().number, 2)

    def test_completed_image_expires_and_next_ticket_is_allowed(self):
        self.controller.accept(False)
        preview = self.controller.preview_image()
        self.assertIsNotNone(preview)
        preview.close()
        self.clock.advance(self.cfg.image_retention_seconds)
        self.controller.expire()
        self.assertEqual(self.controller.snapshot().state, "expired")
        self.assertFalse(self.controller.snapshot().has_image)
        self.assertIsNone(self.controller.preview_image())
        self.assertTrue(self.controller.accept(False))
        self.assertEqual(self.store.latest().number, 2)

    def test_failed_camera_retry_also_has_retention_deadline(self):
        self.capture.side_effect = RuntimeError("camera unavailable")
        self.controller.accept(True)
        self.clock.advance(self.cfg.image_retention_seconds)
        self.controller.expire()
        self.assertFalse(self.controller.snapshot().can_retry)
        self.assertFalse(self.controller.retry())

    def test_database_failure_before_send_prevents_printer_call(self):
        original_update = self.store.update

        def update(number, status, *args, **kwargs):
            if status == "printing":
                raise sqlite3.OperationalError("disk full")
            return original_update(number, status, *args, **kwargs)

        with patch.object(self.store, "update", side_effect=update):
            self.controller.accept(False)
        self.printer.print_image.assert_not_called()
        self.assertEqual(self.store.get(1).attempts, 0)
        self.assertEqual(self.controller.snapshot().state, "error")
        self.assertNotIn("重複", self.controller.snapshot().message)

    def test_database_failure_after_send_reports_uncertain_result(self):
        original_update = self.store.update

        def update(number, status, *args, **kwargs):
            if status == "test_completed":
                raise sqlite3.OperationalError("disk full")
            return original_update(number, status, *args, **kwargs)

        with patch.object(self.store, "update", side_effect=update):
            self.controller.accept(False)
        self.printer.print_image.assert_called_once()
        self.assertEqual(self.controller.snapshot().state, "error")
        self.assertIn("重複", self.controller.snapshot().message)
        self.assertEqual(self.store.get(1).status, "error")

    def test_restarted_unresolved_ticket_has_no_image_and_no_retry(self):
        ticket = self.store.allocate(True, "original timestamp")
        self.store.update(ticket.number, "printing", increment_attempt=True)
        restarted = self.make_controller()
        state = restarted.snapshot()
        self.assertEqual((state.state, state.number, state.with_photo), ("interrupted", 1, True))
        self.assertFalse(state.has_image)
        self.assertFalse(state.can_retry)
        self.assertFalse(restarted.retry())
        self.assertFalse(restarted.accept(False))
        self.assertEqual(self.store.get(1).status, "interrupted")
        self.assertTrue(restarted.discard())
        self.assertEqual(self.store.get(1).status, "discarded")

    def test_failed_allocation_does_not_print_or_claim_retry_is_possible(self):
        with patch.object(self.store, "allocate", side_effect=sqlite3.OperationalError("readonly")):
            self.assertTrue(self.controller.accept(False))
        self.assertEqual(self.controller.snapshot().state, "error")
        self.assertIsNone(self.controller.snapshot().number)
        self.assertFalse(self.controller.snapshot().can_retry)
        self.capture.assert_not_called()
        self.render.assert_not_called()
        self.printer.print_image.assert_not_called()

    def test_render_failure_releases_captured_photo(self):
        photo = Image.new("RGB", (20, 20))
        self.capture.side_effect = None
        self.capture.return_value = photo
        self.render.side_effect = ValueError("QR too large")
        with patch.object(photo, "close", wraps=photo.close) as close:
            self.controller.accept(True)
            close.assert_called_once()
        self.printer.print_image.assert_not_called()
        self.assertTrue(self.controller.snapshot().can_retry)

    def test_pre_send_failure_has_no_duplicate_warning(self):
        self.printer.print_image.side_effect = CertainFailure("bad configured width")
        self.controller.accept(False)
        self.assertEqual(self.controller.snapshot().state, "error")
        self.assertNotIn("重複", self.controller.snapshot().message)

    def test_plain_send_exception_is_conservatively_uncertain(self):
        self.printer.print_image.side_effect = RuntimeError("unknown failure")
        self.controller.accept(False)
        self.assertIn("重複", self.controller.snapshot().message)


if __name__ == "__main__":
    unittest.main()
