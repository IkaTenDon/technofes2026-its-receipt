from dataclasses import replace
from pathlib import Path
import tempfile
import tomllib
import unittest

from receipt_app.config import AppConfig, load_config, validate_config
from receipt_app.rendering import make_qr


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "config.toml"

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return load_config(self.path)

    def test_shipped_config_defaults_to_test_mode_and_explicit_placeholders(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / "config.toml")
        self.assertTrue(cfg.test_mode)
        self.assertEqual((cfg.photo_key, cfg.no_photo_key), ("F1", "F2"))
        self.assertEqual(cfg.receipt.width_px, 384)
        self.assertIn("変更してください", cfg.receipt.menu)
        self.assertEqual(cfg.receipt.qr_data, "https://example.com/")
        self.assertEqual(cfg.printer.bluetooth_target, "")

    def test_partial_config_uses_defaults_and_keeps_japanese_text(self):
        cfg = self.load('[receipt]\nmenu = """\n日本語のご案内\n展示の説明"""\n')
        self.assertEqual(cfg.receipt.menu, "日本語のご案内\n展示の説明")
        self.assertEqual(cfg.base_dir, self.base.resolve())
        self.assertTrue(cfg.test_mode)
        self.assertEqual(cfg.resolve(cfg.database_path), self.base / "data" / "receipts.sqlite3")

    def test_old_receipt_keys_are_loaded_and_explicit_new_values_win(self):
        cfg = self.load(
            '[receipt]\ntitle = "旧見出し"\nhead = "新見出し"\nbody = "展示の説明"\n'
            'footer = "SNSへの案内"\ntitle_size = 28\n'
        )
        self.assertEqual(cfg.receipt.head, "新見出し")
        self.assertEqual(cfg.receipt.menu, "展示の説明")
        self.assertEqual(cfg.receipt.qr, "SNSへの案内")
        self.assertEqual(cfg.receipt.head_size, 28)
        self.assertEqual(self.load('[receipt]\ntitle = "旧見出し"\n').receipt.head, "旧見出し")

    def test_number_and_time_templates_require_their_placeholder_once(self):
        cfg = self.load('[receipt]\nnumber = "受付 {number} 番"\ntime = "{time} 発行"\n')
        self.assertEqual(cfg.receipt.number, "受付 {number} 番")
        self.assertEqual(cfg.receipt.time, "{time} 発行")
        for key in ("number", "time"):
            for value in ("固定文", f"{{{key}}} {{{key}}}"):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, f"receipt.{key}"):
                    self.load(f'[receipt]\n{key} = "{value}"\n')

    def test_unknown_names_are_rejected_instead_of_ignored(self):
        for text in ('[other]\nvalue=1\n', '[receipt]\nwidht_px=384\n',
                     '[app]\ntest_mod=true\n', '[camera]\nunknown=1\n',
                     '[printer]\nunknown=1\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)

    def test_types_are_validated_before_membership_or_numeric_processing(self):
        for text in ('[app]\ntest_mode="false"\n', '[receipt]\nwidth_px=true\n',
                     '[camera]\nbackend=["dshow"]\n', '[app]\nphoto_key=["F1"]\n',
                     '[receipt]\nqr_data=123\n', '[receipt]\nphoto_gamma=true\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)

    def test_width_must_be_safe_range_and_multiple_of_eight(self):
        for width in (0, 120, 385, 2056):
            with self.subTest(width=width), self.assertRaises(ValueError):
                self.load(f"[receipt]\nwidth_px={width}\n")
        cfg = self.load("[receipt]\nwidth_px=448\n")
        self.assertEqual(cfg.receipt.width_px, 448)

    def test_oversized_margins_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "余白"):
            self.load("[receipt]\nwidth_px=384\nmargin_px=150\n")

    def test_timeouts_and_photo_parameters_must_be_positive_finite_numbers(self):
        for section, name, value in (("printer", "operation_timeout_s", "0"),
                                     ("printer", "total_timeout_s", "nan"),
                                     ("receipt", "photo_gamma", "inf"),
                                     ("receipt", "photo_contrast", "-1.0")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.load(f"[{section}]\n{name}={value}\n")

    def test_reception_keys_must_be_distinct_supported_function_keys(self):
        for text in ('[app]\nphoto_key="F2"\n', '[app]\nphoto_key="F13"\n',
                     '[app]\nno_photo_key="f2"\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)
        cfg = self.load('[app]\nphoto_key="F3"\nno_photo_key="F4"\n')
        self.assertEqual((cfg.photo_key, cfg.no_photo_key), ("F3", "F4"))

    def test_qr_length_limit_and_layout_failure_are_explicit(self):
        for text in ("", "a" * 2001):
            with self.subTest(length=len(text)), self.assertRaisesRegex(ValueError, "qr_data"):
                self.load('[receipt]\nqr_data="' + text + '"\n')
        cfg = self.load('[receipt]\nqr_data="' + "a" * 1000 + '"\n')
        with self.assertRaisesRegex(ValueError, "QRコードが配置幅"):
            make_qr(cfg.receipt)

    def test_relative_font_path_is_resolved_against_config_directory(self):
        cfg = self.load('[receipt]\nfont_path="fonts/custom.ttc"\n')
        self.assertEqual(Path(cfg.receipt.font_path), self.base / "fonts" / "custom.ttc")

    def test_long_text_is_rejected_before_allocating_render_canvas(self):
        with self.assertRaisesRegex(ValueError, "10000文字"):
            self.load('[receipt]\nmenu="' + "あ" * 10001 + '"\n')

    def test_malformed_toml_is_not_silently_defaulted(self):
        with self.assertRaises(tomllib.TOMLDecodeError):
            self.load('[app\ntest_mode=true\n')

    def test_direct_config_validation_does_not_accept_boolean_as_integer(self):
        cfg = AppConfig(base_dir=self.base)
        with self.assertRaisesRegex(ValueError, "debounce_ms"):
            validate_config(replace(cfg, debounce_ms=True))


if __name__ == "__main__":
    unittest.main()
