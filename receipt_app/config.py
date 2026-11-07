from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class ReceiptConfig:
    width_px: int = 384
    margin_px: int = 16
    gap_px: int = 12
    head: str = "ご来場ありがとうございます"
    number: str = "あなたは{number}人目の来場者です"
    time: str = "日時 {time}"
    menu: str = "【変更してください】展示のご案内\n展示内容をここに記入してください。"
    qr: str = "公式SNSのご案内"
    qr_data: str = "https://example.com/"
    font_path: str = "C:/Windows/Fonts/meiryo.ttc"
    font_index: int = 0
    head_size: int = 26
    body_size: int = 20
    number_size: int = 22
    line_spacing_px: int = 5
    qr_max_px: int = 280
    qr_min_module_px: int = 4
    photo_height_px: int = 260
    photo_contrast: float = 1.25
    photo_gamma: float = 1.0


@dataclass(frozen=True)
class CameraConfig:
    device_index: int = 0
    width: int = 1280
    height: int = 720
    warmup_frames: int = 5
    backend: str = "dshow"


@dataclass(frozen=True)
class PrinterConfig:
    source_dir: str = "vendor/TiMini-Print"
    bluetooth_target: str = ""
    model_key: str = ""
    paper_preset: str = ""
    blackening: int = 3
    feed_padding: int = 12
    operation_timeout_s: float = 10.0
    total_timeout_s: float = 180.0


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    test_mode: bool = True
    database_path: str = "data/receipts.sqlite3"
    photo_key: str = "F1"
    no_photo_key: str = "F2"
    debounce_ms: int = 1000
    image_retention_seconds: int = 120
    receipt: ReceiptConfig = field(default_factory=ReceiptConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    printer: PrinterConfig = field(default_factory=PrinterConfig)

    def resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.base_dir / path


def _section(cls, data: dict):
    try:
        return cls(**data)
    except TypeError as exc:
        raise ValueError(f"{cls.__name__}: 設定名を確認してください: {exc}") from exc


def load_config(path: str | Path) -> AppConfig:
    path = Path(path).resolve()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    unknown = data.keys() - {"app", "receipt", "camera", "printer"}
    if unknown:
        raise ValueError(f"不明な設定セクション: {', '.join(unknown)}")
    receipt = data.get("receipt", {})
    if isinstance(receipt, dict):
        for old, new in (("title", "head"), ("body", "menu"), ("footer", "qr"),
                         ("title_size", "head_size")):
            if old in receipt:
                receipt.setdefault(new, receipt.pop(old))
    try:
        cfg = AppConfig(
            base_dir=path.parent,
            receipt=_section(ReceiptConfig, receipt),
            camera=_section(CameraConfig, data.get("camera", {})),
            printer=_section(PrinterConfig, data.get("printer", {})),
            **data.get("app", {}),
        )
    except TypeError as exc:
        raise ValueError(f"app 設定を確認してください: {exc}") from exc
    validate_config(cfg)
    return replace(cfg, receipt=replace(cfg.receipt, font_path=str(cfg.resolve(cfg.receipt.font_path))))


def validate_config(cfg: AppConfig) -> None:
    r, p, c = cfg.receipt, cfg.printer, cfg.camera
    integer_fields = {
        "width_px": (r.width_px, 128, 2048),
        "margin_px": (r.margin_px, 0, 512),
        "gap_px": (r.gap_px, 0, 512),
        "head_size": (r.head_size, 8, 200),
        "body_size": (r.body_size, 8, 200),
        "number_size": (r.number_size, 8, 200),
        "line_spacing_px": (r.line_spacing_px, 0, 100),
        "qr_max_px": (r.qr_max_px, 64, 2048),
        "qr_min_module_px": (r.qr_min_module_px, 2, 20),
        "photo_height_px": (r.photo_height_px, 32, 2048),
        "font_index": (r.font_index, 0, 100),
        "debounce_ms": (cfg.debounce_ms, 100, 10000),
        "image_retention_seconds": (cfg.image_retention_seconds, 10, 3600),
        "device_index": (c.device_index, 0, 100),
        "camera.width": (c.width, 1, 8192),
        "camera.height": (c.height, 1, 8192),
        "warmup_frames": (c.warmup_frames, 0, 60),
        "blackening": (p.blackening, 1, 5),
        "feed_padding": (p.feed_padding, 0, 1000),
    }
    for name, (value, low, high) in integer_fields.items():
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} は {low}〜{high} の整数にしてください")
    if r.width_px % 8:
        raise ValueError("width_px は8の倍数にしてください。自動リサイズは行いません")
    if r.width_px - 2 * r.margin_px < 96:
        raise ValueError("余白が大きすぎます")
    if not isinstance(c.backend, str) or c.backend not in {"dshow", "msmf", "any"}:
        raise ValueError("camera.backend は dshow / msmf / any を指定してください")
    for name, value in (("photo_contrast", r.photo_contrast), ("photo_gamma", r.photo_gamma),
                        ("operation_timeout_s", p.operation_timeout_s),
                        ("total_timeout_s", p.total_timeout_s)):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} は正の有限数にしてください")
    if type(cfg.test_mode) is not bool:
        raise ValueError("test_mode は true / false を指定してください")
    if cfg.photo_key == cfg.no_photo_key or any(
        not isinstance(key, str) or key not in {f"F{i}" for i in range(1, 13)}
        for key in (cfg.photo_key, cfg.no_photo_key)
    ):
        raise ValueError("受付キーには異なる F1〜F12 を指定してください")
    for value in (r.head, r.number, r.time, r.menu, r.qr, r.qr_data, r.font_path,
                  p.source_dir, p.bluetooth_target, p.model_key, p.paper_preset, cfg.database_path):
        if not isinstance(value, str):
            raise ValueError("文言・パス・接続先は文字列で指定してください")
    if not r.qr_data or len(r.qr_data) > 2000:
        raise ValueError("qr_data は1〜2000文字にしてください")
    if sum(map(len, (r.head, r.number, r.time, r.menu, r.qr))) > 10000:
        raise ValueError("レシート文面は合計10000文字以内にしてください")
    for name, template, placeholder in (("number", r.number, "{number}"), ("time", r.time, "{time}")):
        if template.count(placeholder) != 1:
            raise ValueError(f"receipt.{name} には {placeholder} を1回だけ入れてください")
