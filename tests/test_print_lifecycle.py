import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from receipt_app.__main__ import main
from receipt_app.config import AppConfig
from receipt_app.printer import PrintFailure, PrintResult


@unittest.skipUnless(sys.platform == "win32", "Windows printer lifecycle")
class PrintLifecycleTests(unittest.TestCase):
    def test_width_pattern_disconnects_only_after_paper_confirmation(self):
        events = []

        class Printer:
            def print_image(self, image):
                events.append("send")
                return PrintResult(False, "sent")

            def close(self):
                events.append("disconnect")

        def paper_confirm(_):
            self.assertEqual(events, ["send"])
            events.append("paper-confirmed")
            return ""

        with tempfile.TemporaryDirectory() as folder:
            cfg = AppConfig(Path(folder), test_mode=False)
            with patch("receipt_app.__main__.load_config", return_value=cfg), \
                    patch("receipt_app.__main__.TiMiniPrinter", return_value=Printer()), \
                    patch("builtins.input", side_effect=paper_confirm), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = main(["width-pattern", "--output", str(Path(folder) / "pattern.png"), "--print"])
        self.assertEqual(result, 0)
        self.assertEqual(events, ["send", "paper-confirmed", "disconnect"])

    def test_shutdown_failure_is_visible_and_returns_nonzero(self):
        class Printer:
            def print_image(self, image):
                return PrintResult(False, "sent")

            def close(self):
                raise PrintFailure("connection cleanup failed", uncertain=True)

        error = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            cfg = AppConfig(Path(folder), test_mode=False)
            with patch("receipt_app.__main__.load_config", return_value=cfg), \
                    patch("receipt_app.__main__.TiMiniPrinter", return_value=Printer()), \
                    patch("builtins.input", return_value=""), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error):
                result = main(["width-pattern", "--output", str(Path(folder) / "pattern.png"), "--print"])
        self.assertEqual(result, 1)
        self.assertIn("connection cleanup failed", error.getvalue())


if __name__ == "__main__":
    unittest.main()
