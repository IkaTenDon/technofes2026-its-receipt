from __future__ import annotations

import unicodedata
from datetime import datetime
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps
from qrcode.exceptions import DataOverflowError

from .config import ReceiptConfig


MAX_RECEIPT_HEIGHT = 30000
MAX_RECEIPT_PIXELS = 32_000_000


def format_receipt_datetime(created_at: str) -> str:
    value = datetime.fromisoformat(created_at)
    weekday = "月火水木金土日"[value.weekday()]
    return f"{value:%Y/%m/%d} ({weekday}) {value:%H:%M:%S}.{value.microsecond // 1000:03d}"


def _font(cfg: ReceiptConfig, size: int) -> ImageFont.FreeTypeFont:
    if not Path(cfg.font_path).is_file():
        raise ValueError(f"日本語フォントが見つかりません。receipt.font_path を確認してください: {cfg.font_path}")
    try:
        return ImageFont.truetype(cfg.font_path, size=size, index=cfg.font_index)
    except OSError as exc:
        raise ValueError("日本語フォントを開けません。font_path / font_index を確認してください") from exc


def _characters(text: str):
    current = ""
    for char in text:
        if current and (unicodedata.combining(char) or char in ("\ufe0e", "\ufe0f")):
            current += char
        else:
            if current:
                yield current
            current = char
    if current:
        yield current


def wrap_text(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Wrap by measured glyph width, including strings without spaces."""
    if width <= 0:
        raise ValueError("文章を配置する幅がありません")
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = ""
        for char in _characters(paragraph.expandtabs(4)):
            if font.getlength(char) > width:
                raise ValueError("1文字が配置幅を超えています。文字サイズまたは余白を変更してください")
            if line and font.getlength(line + char) > width:
                lines.append(line)
                line = char
            else:
                line += char
        lines.append(line)
    return lines


def _check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("レシート画像の寸法は正数にしてください")
    if height > MAX_RECEIPT_HEIGHT or width * height > MAX_RECEIPT_PIXELS:
        raise ValueError("レシートが長すぎます。文面・文字サイズ・余白を見直してください")


def _text_block(cfg: ReceiptConfig, text: str, size: int, *, centered: bool = False) -> Image.Image:
    width = cfg.width_px - 2 * cfg.margin_px
    font = _font(cfg, size)
    lines = wrap_text(text, font, width)
    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    height = len(lines) * line_height + max(0, len(lines) - 1) * cfg.line_spacing_px
    _check_dimensions(width, height)
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        x = (width - font.getlength(line)) / 2 if centered else 0
        draw.text((x, index * (line_height + cfg.line_spacing_px)), line, font=font,
                  fill=0, anchor="lt")
    return image


def make_qr(cfg: ReceiptConfig) -> Image.Image:
    """Render square integer modules with a four-module white quiet zone."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4, box_size=1)
    try:
        qr.add_data(cfg.qr_data)
        qr.make(fit=True)
    except DataOverflowError as exc:
        raise ValueError("QRコードの内容が長すぎます。qr_data を短くしてください") from exc
    matrix = qr.get_matrix()
    side = len(matrix)
    max_width = min(cfg.qr_max_px, cfg.width_px - 2 * cfg.margin_px)
    module = max_width // side
    if module < cfg.qr_min_module_px:
        raise ValueError(
            f"QRコードが配置幅に収まりません。{side}モジュール × 最低{cfg.qr_min_module_px}px が必要です。"
            "qr_data を短くするか、印字幅・余白・qr_max_px を確認してください"
        )
    image = Image.new("1", (side * module, side * module), 1)
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(matrix):
        for x, black in enumerate(row):
            if black:
                draw.rectangle((x * module, y * module, (x + 1) * module - 1,
                                (y + 1) * module - 1), fill=0)
    return image


def prepare_photo(cfg: ReceiptConfig, photo: Image.Image) -> Image.Image:
    """Only the photograph receives contrast/gamma and error-diffusion dithering."""
    size = (cfg.width_px - 2 * cfg.margin_px, cfg.photo_height_px)
    gray = ImageOps.fit(ImageOps.exif_transpose(photo).convert("L"), size,
                        method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    gray = ImageOps.autocontrast(gray)
    gray = ImageEnhance.Contrast(gray).enhance(cfg.photo_contrast)
    gamma = [round(255 * (level / 255) ** (1 / cfg.photo_gamma)) for level in range(256)]
    gray = gray.point(gamma)
    return gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)


def _stack(cfg: ReceiptConfig, blocks: list[Image.Image]) -> Image.Image:
    height = 2 * cfg.margin_px + sum(block.height for block in blocks)
    height += max(0, len(blocks) - 1) * cfg.gap_px
    _check_dimensions(cfg.width_px, height)
    receipt = Image.new("L", (cfg.width_px, height), 255)
    y = cfg.margin_px
    for block in blocks:
        if block.width > cfg.width_px - 2 * cfg.margin_px:
            raise ValueError("レシートの要素が配置幅を超えています")
        receipt.paste(block, ((cfg.width_px - block.width) // 2, y))
        y += block.height + cfg.gap_px
    return receipt.convert("1", dither=Image.Dither.NONE)


def render_receipt(cfg: ReceiptConfig, number: int, created_at: str,
                   photo: Image.Image | None = None) -> Image.Image:
    if type(number) is not int or number < 1:
        raise ValueError("来場番号には1以上の整数が必要です")
    blocks = []
    if cfg.title:
        blocks.append(_text_block(cfg, cfg.title, cfg.title_size, centered=True))
    blocks.append(_text_block(cfg, f"来場番号 {number:06d}", cfg.number_size, centered=True))
    blocks.append(_text_block(cfg, f"日時 {format_receipt_datetime(created_at)}", cfg.body_size, centered=True))
    if cfg.body:
        blocks.append(_text_block(cfg, cfg.body, cfg.body_size))
    if photo is not None:
        blocks.append(prepare_photo(cfg, photo))
    blocks.append(make_qr(cfg))
    if cfg.footer:
        blocks.append(_text_block(cfg, cfg.footer, cfg.body_size))
    return _stack(cfg, blocks)


def make_width_pattern(cfg: ReceiptConfig) -> Image.Image:
    """Width/edge ruler and dither patches; not a calibration claim about hardware."""
    width = cfg.width_px
    label = _text_block(cfg, f"印字幅テスト: {width} dot\n左右の端線と8 dot刻みを確認", cfg.body_size)
    qr = make_qr(cfg)
    height = label.height + qr.height + 190 + 2 * cfg.margin_px
    _check_dimensions(width, height)
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    image.paste(label, (cfg.margin_px, cfg.margin_px))
    y = cfg.margin_px + label.height + 12
    draw.line((0, y, width - 1, y), fill=0, width=1)
    for x in range(0, width, 8):
        draw.line((x, y, x, y + (24 if x % 32 == 0 else 12)), fill=0)
    for edge in (0, width - 1):
        draw.line((edge, y - 8, edge, y + 38), fill=0, width=1)
    font = _font(cfg, min(cfg.body_size, 16))
    for x in range(0, width, 64):
        draw.text((x, y + 28), str(x), font=font, fill=0, anchor="lt")
    draw.text((width - font.getlength(str(width - 1)), y + 50), str(width - 1), font=font,
              fill=0, anchor="lt")
    content_width = width - 2 * cfg.margin_px
    gradient = Image.new("L", (content_width, 40))
    gradient.putdata([round(255 * x / max(1, content_width - 1))
                      for _ in range(40) for x in range(content_width)])
    image.paste(gradient.convert("1", dither=Image.Dither.FLOYDSTEINBERG),
                (cfg.margin_px, y + 84))
    image.paste(qr, ((width - qr.width) // 2, y + 144))
    return image.convert("1", dither=Image.Dither.NONE)
