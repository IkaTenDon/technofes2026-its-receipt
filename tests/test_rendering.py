from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import Mock, call, patch

import cv2
import numpy as np
from PIL import Image, ImageFont

from receipt_app import rendering
from receipt_app.config import ReceiptConfig
from receipt_app.rendering import (MAX_RECEIPT_HEIGHT, format_receipt_datetime, make_qr, make_width_pattern,
                                   prepare_photo, render_receipt, wrap_text)


FONT_PATH = Path("C:/Windows/Fonts/meiryo.ttc")


@unittest.skipUnless(FONT_PATH.is_file(), "Windows日本語フォントが必要です")
class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ReceiptConfig()

    def test_optional_photo_changes_height_only_by_photo_and_gap(self):
        without = render_receipt(self.cfg, 1, "2026-11-07T12:34:56.123+09:00")
        with_photo = render_receipt(self.cfg, 1, "2026-11-07T12:34:56.123+09:00",
                                    Image.new("RGB", (640, 480), "gray"))
        self.assertEqual(without.mode, "1")
        self.assertEqual(without.width, self.cfg.width_px)
        self.assertEqual(with_photo.width, self.cfg.width_px)
        self.assertEqual(with_photo.height - without.height,
                         self.cfg.photo_height_px + self.cfg.gap_px)
        self.assertNotEqual(without.height, 1000)

    def test_text_wraps_without_spaces_and_preserves_paragraphs(self):
        font = ImageFont.truetype(str(FONT_PATH), size=20)
        text = "日本語の文章" * 12 + "\n\n" + "verylongunbrokenword" * 12
        lines = wrap_text(text, font, 180)
        self.assertIn("", lines)
        self.assertTrue(all(font.getlength(line) <= 180 for line in lines))
        self.assertEqual("".join(lines), text.replace("\n", ""))

    def test_receipt_datetime_shows_japanese_weekday_and_milliseconds(self):
        self.assertEqual(format_receipt_datetime("2026-11-07T12:34:56.123456+09:00"),
                         "2026/11/07 (土) 12:34:56.123")
        self.assertEqual(format_receipt_datetime("2026-11-09T00:00:00+09:00"),
                         "2026/11/09 (月) 00:00:00.000")

    def test_custom_sections_render_in_order_with_number_and_time_filled_in(self):
        cfg = replace(self.cfg, head="ようこそ", number="受付 {number} 番", time="受付時刻\n{time}",
                      menu="展示A\n展示B", qr="公式SNS")
        order = Mock()
        with Image.new("RGB", (640, 480), "gray") as photo, \
                patch.object(rendering, "_text_block", wraps=rendering._text_block) as text, \
                patch.object(rendering, "prepare_photo", wraps=prepare_photo) as picture, \
                patch.object(rendering, "make_qr", wraps=make_qr) as qr:
            order.attach_mock(text, "text")
            order.attach_mock(picture, "photo")
            order.attach_mock(qr, "qr")
            render_receipt(cfg, 42, "2026-11-07T12:34:56.123+09:00", photo).close()
        self.assertEqual(order.mock_calls, [
            call.text(cfg, "ようこそ", cfg.head_size, centered=True),
            call.text(cfg, "受付 0042 番", cfg.number_size, centered=True),
            call.text(cfg, "受付時刻\n2026/11/07 (土) 12:34:56.123", cfg.body_size, centered=True),
            call.photo(cfg, photo),
            call.text(cfg, "展示A\n展示B", cfg.body_size),
            call.text(cfg, "公式SNS", cfg.body_size, centered=True),
            call.qr(cfg),
        ])

    def test_long_menu_increases_receipt_height(self):
        short = render_receipt(self.cfg, 1, "2026-11-07 12:00")
        long = render_receipt(replace(self.cfg, menu="ご来場ありがとうございます。" * 100),
                              1, "2026-11-07 12:00")
        self.assertGreater(long.height, short.height)

    def test_qr_is_decodable_and_quiet_zone_is_white(self):
        qr = make_qr(self.cfg)
        data, points, _ = cv2.QRCodeDetector().detectAndDecode(np.asarray(qr.convert("L")))
        self.assertEqual(data, self.cfg.qr_data)
        self.assertIsNotNone(points)
        dark = np.asarray(qr.convert("L")) == 0
        min_y, min_x = np.argwhere(dark).min(axis=0)
        max_y, max_x = np.argwhere(dark).max(axis=0)
        self.assertEqual(min_x, min_y)
        self.assertGreaterEqual(min_x, 4 * self.cfg.qr_min_module_px)
        self.assertEqual(qr.width - 1 - max_x, min_x)
        self.assertEqual(qr.height - 1 - max_y, min_y)

    def test_qr_too_small_fails_instead_of_rescaling(self):
        with self.assertRaisesRegex(ValueError, "QRコードが配置幅"):
            make_qr(replace(self.cfg, qr_max_px=64))

    def test_photo_is_binary_and_fixed_crop_size(self):
        photo = Image.linear_gradient("L").resize((640, 480)).convert("RGB")
        prepared = prepare_photo(self.cfg, photo)
        self.assertEqual(prepared.mode, "1")
        self.assertEqual(prepared.size, (self.cfg.width_px - 2 * self.cfg.margin_px,
                                        self.cfg.photo_height_px))
        self.assertEqual(set(prepared.convert("L").tobytes()), {0, 255})
        self.assertEqual(photo.mode, "RGB")

    def test_bad_font_is_explicit_error(self):
        with self.assertRaisesRegex(ValueError, "フォントが見つかりません"):
            render_receipt(replace(self.cfg, font_path="missing-font.ttf"), 1, "2026-11-07T12:00:00")

    def test_invalid_number_fails(self):
        with self.assertRaises(ValueError):
            render_receipt(self.cfg, 0, "now")

    def test_excessive_length_fails_before_final_canvas(self):
        with self.assertRaisesRegex(ValueError, "長すぎます"):
            render_receipt(replace(self.cfg, menu="あ\n" * MAX_RECEIPT_HEIGHT), 1, "2026-11-07T12:00:00")

    def test_width_pattern_has_black_pixels_at_both_edges(self):
        pattern = make_width_pattern(self.cfg)
        self.assertEqual(pattern.mode, "1")
        self.assertEqual(pattern.width, self.cfg.width_px)
        self.assertEqual(pattern.crop((0, 0, 1, pattern.height)).convert("L").getextrema()[0], 0)
        self.assertEqual(pattern.crop((pattern.width - 1, 0, pattern.width, pattern.height))
                         .convert("L").getextrema()[0], 0)


if __name__ == "__main__":
    unittest.main()
