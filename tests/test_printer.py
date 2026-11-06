from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from receipt_app.config import AppConfig, PrinterConfig
from receipt_app.printer import (
    PINNED_COMMIT, DryRunPrinter, PrintFailure, TiMiniPrinter,
    _bw_pixels, _load_timini, _validate_paper, verify_source,
)


def paper(**changes):
    values = dict(render_width_px=384, render_height_px=None, raster_height_px=None,
                  rotation_degrees=0, render_height_scale=1.0,
                  max_height_px=None, top_padding_px=0)
    return SimpleNamespace(**(values | changes))


class FakePrinter:
    def __init__(self, *, fail_send=False, fail_close=False, slow=False):
        self.fail_send, self.fail_close, self.slow = fail_send, fail_close, slow
        self.jobs = []
        self.closed = False

    async def disconnect(self):
        self.closed = True
        if self.fail_close:
            raise OSError("disconnect failed")

    def printer_device(self):
        return "prepared-device"

    def raster_job(self, raster_set, **kwargs):
        return SimpleNamespace(raster=raster_set, settings=kwargs["settings"])

    async def send_job(self, job, **_):
        self.jobs.append(job)
        if self.slow:
            await asyncio.sleep(30)
        if self.fail_send:
            raise OSError("link lost")


class FakeCleanupError(RuntimeError):
    @classmethod
    def is_cause_of(cls, error):
        seen = set()
        while error is not None and id(error) not in seen:
            if isinstance(error, cls):
                return True
            seen.add(id(error))
            error = error.__cause__
        return False


class PrinterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig(Path.cwd(), test_mode=False,
                             printer=PrinterConfig(bluetooth_target="AA:BB:CC:DD:EE:FF"))
        self.image = Image.new("1", (384, 20), 1)
        self.adapters = []

    def tearDown(self):
        for adapter in self.adapters:
            try:
                adapter.close()
            except PrintFailure:
                pass
        self.image.close()

    def adapter(self, cfg=None):
        adapter = TiMiniPrinter(cfg or self.cfg)
        self.adapters.append(adapter)
        return adapter

    def fake_api(self, printer, selected_paper=None, connect_error=None):
        self.targets = []
        self.models = []
        test = self

        class Catalog:
            @classmethod
            def load(cls):
                return cls()

            def device_from_key(self, model, *, transport_target):
                test.models.append((model, transport_target))
                return "forced-device"

        class Discovery:
            def __init__(self, _):
                pass

            async def resolve_device(self, target):
                test.targets.append(target)
                return "discovered-device"

            async def resolve_transport_target(self, target):
                test.targets.append(target)
                return SimpleNamespace(transport_target="selected-target")

        async def connect(*_, **__):
            if connect_error:
                raise connect_error
            return printer

        return SimpleNamespace(
            PrinterCatalog=Catalog, BluetoothDiscovery=Discovery,
            BleakBluetoothConnector=lambda: object(), connect_printer=connect,
            ConnectionCleanupError=FakeCleanupError,
            PrintSettings=lambda **kwargs: SimpleNamespace(**kwargs),
            resolve_paper=lambda *_: selected_paper or paper(),
            RasterBuffer=lambda **kwargs: SimpleNamespace(**kwargs),
            RasterSet=SimpleNamespace(from_single=lambda value: value),
            PixelFormat=SimpleNamespace(BW1="bw1"),
        )

    def print_fake(self, printer, *, cfg=None, selected_paper=None, connect_error=None):
        api = self.fake_api(printer, selected_paper, connect_error)
        with patch("receipt_app.printer._load_timini", return_value=api):
            return self.adapter(cfg).print_image(self.image)

    def test_dry_run_neither_writes_nor_loads_backend(self):
        with patch("receipt_app.printer._load_timini", side_effect=AssertionError("backend called")):
            result = DryRunPrinter().print_image(self.image)
        self.assertTrue(result.test_mode)

    def test_real_adapter_also_honors_test_mode(self):
        with patch("receipt_app.printer._load_timini", side_effect=AssertionError("backend called")):
            result = TiMiniPrinter(replace(self.cfg, test_mode=True)).print_image(self.image)
        self.assertTrue(result.test_mode)

    def test_bw_mapping_black_one_white_zero(self):
        image = Image.new("1", (2, 1), 1)
        image.putpixel((0, 0), 0)
        self.assertEqual(_bw_pixels(image), b"\x01\x00")

    def test_requires_binary_image(self):
        with self.assertRaises(PrintFailure):
            _bw_pixels(Image.new("L", (10, 10)))

    def test_requires_explicit_bluetooth_target(self):
        cfg = replace(self.cfg, printer=PrinterConfig())
        with patch("receipt_app.printer._load_timini", side_effect=AssertionError("backend called")):
            with self.assertRaises(PrintFailure):
                TiMiniPrinter(cfg).print_image(self.image)

    def test_sends_once_and_keeps_connection_until_explicit_close(self):
        printer = FakePrinter()
        result = self.print_fake(printer)
        self.assertFalse(result.test_mode)
        self.assertEqual(len(printer.jobs), 1)
        self.assertFalse(printer.closed)
        self.assertEqual(self.targets, [self.cfg.printer.bluetooth_target])
        self.assertEqual(len(printer.jobs[0].raster.pixels), 384 * 20)
        self.assertFalse(printer.jobs[0].settings.trim_side_margins)
        adapter = self.adapters[-1]
        self.assertTrue(adapter._thread.is_alive())
        adapter.close()
        self.assertTrue(printer.closed)
        self.assertFalse(adapter._thread.is_alive())

    def test_forced_model_uses_selected_target(self):
        cfg = replace(self.cfg, printer=replace(self.cfg.printer, model_key="chosen-model"))
        self.print_fake(FakePrinter(), cfg=cfg)
        self.assertEqual(self.models, [("chosen-model", "selected-target")])

    def test_width_mismatch_stops_before_send(self):
        printer = FakePrinter()
        with self.assertRaises(PrintFailure) as caught:
            self.print_fake(printer, selected_paper=paper(render_width_px=576))
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(printer.jobs, [])
        self.assertTrue(printer.closed)

    def test_fixed_length_rotated_or_scaled_paper_rejected(self):
        for changes in (dict(render_height_px=500), dict(raster_height_px=500),
                        dict(rotation_degrees=90), dict(render_height_scale=1.5)):
            with self.subTest(changes=changes), self.assertRaises(PrintFailure):
                _validate_paper(paper(**changes), self.image)

    def test_height_limit_rejected(self):
        with self.assertRaises(PrintFailure):
            _validate_paper(paper(max_height_px=19), self.image)

    def test_connection_failure_does_not_claim_receipt_was_sent(self):
        with self.assertRaises(PrintFailure) as caught:
            self.print_fake(FakePrinter(), connect_error=OSError("not connected"))
        self.assertFalse(caught.exception.uncertain)

    def test_send_failure_is_uncertain_without_automatic_retry(self):
        printer = FakePrinter(fail_send=True)
        with self.assertRaises(PrintFailure) as caught:
            self.print_fake(printer)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(printer.jobs), 1)
        self.assertTrue(printer.closed)

    def test_cleanup_failure_after_send_is_uncertain(self):
        self.print_fake(FakePrinter(fail_close=True))
        with self.assertRaises(PrintFailure) as caught:
            self.adapters[-1].close()
        self.assertTrue(caught.exception.uncertain)

    def test_cleanup_failure_blocks_later_connection_and_preserves_uncertainty(self):
        for send_failed in (False, True):
            with self.subTest(send_failed=send_failed):
                printer = FakePrinter(fail_send=send_failed, fail_close=True)
                api = self.fake_api(printer)
                adapter = self.adapter()
                with patch("receipt_app.printer._load_timini", return_value=api) as load:
                    with self.assertRaises(PrintFailure) as first:
                        if send_failed:
                            adapter.print_image(self.image)
                        else:
                            adapter.print_image(self.image)
                            adapter.close()
                    with self.assertRaises(PrintFailure) as second:
                        adapter.print_image(self.image)
                self.assertTrue(first.exception.uncertain)
                self.assertTrue(second.exception.uncertain)
                self.assertIn("再起動", str(second.exception))
                self.assertEqual(load.call_count, 1)
                self.assertEqual(len(printer.jobs), 1)

    def test_cleanup_failure_before_send_blocks_reconnect_without_duplicate_claim(self):
        printer = FakePrinter(fail_close=True)
        api = self.fake_api(printer, selected_paper=paper(render_width_px=576))
        adapter = self.adapter()
        with patch("receipt_app.printer._load_timini", return_value=api) as load:
            for _ in range(2):
                with self.assertRaises(PrintFailure) as caught:
                    adapter.print_image(self.image)
                self.assertFalse(caught.exception.uncertain)
                self.assertIn("再起動", str(caught.exception))
        self.assertEqual(load.call_count, 1)
        self.assertEqual(printer.jobs, [])

    def test_preparation_cleanup_marker_blocks_reconnect(self):
        error = RuntimeError("preparation failed")
        error.__cause__ = FakeCleanupError("cleanup failed")
        adapter = self.adapter()
        api = self.fake_api(FakePrinter(), connect_error=error)
        with patch("receipt_app.printer._load_timini", return_value=api) as load:
            for _ in range(2):
                with self.assertRaises(PrintFailure) as caught:
                    adapter.print_image(self.image)
                self.assertFalse(caught.exception.uncertain)
                self.assertIn("再起動", str(caught.exception))
        self.assertEqual(load.call_count, 1)

    def test_ordinary_chained_connection_error_allows_retry(self):
        error = RuntimeError("connection failed")
        error.__cause__ = OSError("ordinary cause")
        adapter = self.adapter()
        printer = FakePrinter()
        with patch("receipt_app.printer._load_timini", return_value=self.fake_api(printer, connect_error=error)):
            with self.assertRaises(PrintFailure):
                adapter.print_image(self.image)
        with patch("receipt_app.printer._load_timini", return_value=self.fake_api(printer)):
            self.assertFalse(adapter.print_image(self.image).test_mode)
        self.assertEqual(len(printer.jobs), 1)

    def test_timeout_after_send_is_uncertain_and_disconnects(self):
        printer = FakePrinter(slow=True)
        cfg = replace(self.cfg, printer=replace(self.cfg.printer, total_timeout_s=0.01))
        with self.assertRaises(PrintFailure) as caught:
            self.print_fake(printer, cfg=cfg)
        self.assertTrue(caught.exception.uncertain)
        self.assertTrue(printer.closed)

    def test_consecutive_jobs_reuse_one_prepared_connection(self):
        printer = FakePrinter()
        adapter = self.adapter()
        with patch("receipt_app.printer._load_timini", return_value=self.fake_api(printer)):
            adapter.print_image(self.image)
            adapter.print_image(self.image)
        self.assertEqual(self.targets, [self.cfg.printer.bluetooth_target])
        self.assertEqual(len(printer.jobs), 2)
        self.assertFalse(printer.closed)
        adapter.close()
        self.assertTrue(printer.closed)
        self.assertFalse(adapter._thread.is_alive())

    def test_receiver_loop_stays_alive_after_send_returns(self):
        notification = threading.Event()

        class NotificationPrinter(FakePrinter):
            async def send_job(self, job, **kwargs):
                await super().send_job(job, **kwargs)

                async def receive():
                    await asyncio.sleep(0.02)
                    notification.set()

                asyncio.create_task(receive())

        printer = NotificationPrinter()
        self.print_fake(printer)
        self.assertTrue(notification.wait(timeout=2))
        self.assertFalse(printer.closed)

    def test_close_timeout_returns_and_cleanup_stops_loop_when_io_settles(self):
        release = threading.Event()

        class SlowClosePrinter(FakePrinter):
            async def disconnect(self):
                while not release.is_set():
                    await asyncio.sleep(0.005)
                self.closed = True

        cfg = replace(self.cfg, printer=replace(self.cfg.printer, operation_timeout_s=0.05))
        printer = SlowClosePrinter()
        self.print_fake(printer, cfg=cfg)
        adapter = self.adapters[-1]
        started = time.monotonic()
        try:
            with self.assertRaises(PrintFailure) as caught:
                adapter.close()
            self.assertTrue(caught.exception.uncertain)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
            adapter._thread.join(timeout=2)
        self.assertTrue(printer.closed)
        self.assertFalse(adapter._thread.is_alive())

    def test_close_without_printing_does_not_start_loop(self):
        adapter = self.adapter()
        adapter.close()
        self.assertIsNone(adapter._thread)
        with self.assertRaises(PrintFailure):
            adapter.print_image(self.image)

    def test_event_loop_start_failure_is_reported_and_thread_finishes(self):
        adapter = self.adapter()
        with patch("receipt_app.printer._load_timini", return_value=self.fake_api(FakePrinter())), \
                patch("receipt_app.printer.asyncio.new_event_loop", side_effect=OSError("loop unavailable")):
            with self.assertRaises(PrintFailure) as caught:
                adapter.print_image(self.image)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        adapter._thread.join(timeout=1)
        self.assertFalse(adapter._thread.is_alive())
        self.assertEqual(self.targets, [])

    def test_source_marker_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "timiniprint").mkdir()
            (root / "timiniprint" / "__init__.py").touch()
            marker = root / "PINNED_COMMIT.txt"
            marker.write_text("wrong", encoding="utf-8")
            with self.assertRaises(PrintFailure):
                verify_source(root)
            marker.write_text(PINNED_COMMIT, encoding="utf-8")
            verify_source(root)


class UpstreamOfflineTests(unittest.TestCase):
    def test_actual_upstream_builds_a_variable_height_job_without_hardware(self):
        source = Path(os.environ.get("TIMINI_SOURCE_DIR", "vendor/TiMini-Print")).resolve()
        if not (source / "timiniprint" / "__init__.py").is_file():
            self.skipTest("TiMini Print 未導入。setup.ps1 後、または TIMINI_SOURCE_DIR 指定で実行")
        try:
            api = _load_timini(source)
        except PrintFailure as exc:
            if isinstance(exc.__cause__, ImportError):
                self.skipTest(f"プリンター依存が未導入です。requirements-printer.txt を導入してください: {exc}")
            raise
        from timiniprint.printing.connected import ConnectedPrinter
        from timiniprint.printing.runtime.base import PreparedPrinter

        device = api.PrinterCatalog.load().device_from_profile("x6h")
        printer = ConnectedPrinter(object(), PreparedPrinter(device=device))
        settings = api.PrintSettings(image_mode="threshold", trim_side_margins=False,
                                     trim_top_bottom_margins=False)
        selected_paper = api.resolve_paper(device, settings)
        jobs = []
        for height in (17, 257):
            image = Image.new("1", (selected_paper.render_width_px, height), 1)
            image.putpixel((0, 0), 0)
            _validate_paper(selected_paper, image)
            raster = api.RasterBuffer(pixels=_bw_pixels(image), width=image.width,
                                      pixel_format=api.PixelFormat.BW1)
            raster.validate()
            job = printer.raster_job(api.RasterSet.from_single(raster), is_text=False, settings=settings)
            self.assertTrue(job.payload)
            self.assertTrue(job.wait_for_completion)
            jobs.append(job)
        self.assertNotEqual(jobs[0].payload, jobs[1].payload)


if __name__ == "__main__":
    unittest.main()
