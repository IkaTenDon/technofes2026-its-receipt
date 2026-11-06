from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PIL import Image

from .camera import LiveCamera
from .config import load_config
from .controller import Controller
from .instance import InstanceLock
from .printer import DryRunPrinter, TiMiniPrinter
from .rendering import make_width_pattern, render_receipt
from .storage import Storage


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Windows用イベント受付レシート")
    parser.add_argument("--config", default="config.toml", help="設定ファイル（既定: config.toml）")
    parser.add_argument("--test", action="store_true", help="設定にかかわらず実印刷を無効化")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Tkinter受付画面を起動")
    preview = sub.add_parser("preview", help="人工的な見本PNGを作成（撮影・採番・送信なし）")
    preview.add_argument("--photo-sample", action="store_true", help="人工写真を追加")
    preview.add_argument("--output", type=Path, required=True)
    pattern = sub.add_parser("width-pattern", help="印字幅確認用PNGを作成")
    pattern.add_argument("--output", type=Path, required=True)
    pattern.add_argument("--print", dest="print_pattern", action="store_true", help="設定したモードで印刷も実行")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.test:
            cfg = replace(cfg, test_mode=True)
        command = args.command or "run"
        if command == "preview":
            photo = Image.linear_gradient("L").convert("RGB") if args.photo_sample else None
            try:
                image = render_receipt(cfg.receipt, 1, datetime.now().astimezone().isoformat(timespec="seconds"), photo)
            finally:
                if photo:
                    photo.close()
            try:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                image.save(args.output, format="PNG")
                print(f"見本PNG: {args.output.resolve()} ({image.width}x{image.height})。撮影・採番・印刷は行っていません。")
            finally:
                image.close()
            return 0
        if command == "width-pattern":
            image = make_width_pattern(cfg.receipt)
            try:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                image.save(args.output, format="PNG")
                print(f"印字幅テストPNG: {args.output.resolve()}")
                if args.print_pattern:
                    printer = DryRunPrinter() if cfg.test_mode else TiMiniPrinter(cfg)
                    with InstanceLock(cfg.resolve(cfg.database_path).with_suffix(".lock")):
                        try:
                            result = printer.print_image(image)
                            print(result.detail)
                            if not result.test_mode:
                                input("用紙が最後まで印刷されたことを確認してから Enter を押してください: ")
                        finally:
                            printer.close()
            finally:
                image.close()
            return 0
        if sys.platform != "win32":
            raise RuntimeError("受付GUIはWindows用です。見本画像の生成は他のOSでも利用できます。")
        import tkinter as tk
        from .gui import ReceiptWindow

        # Catch invalid font / QR layout before allocating any visitor number.
        check = render_receipt(cfg.receipt, 1, datetime.now().astimezone().isoformat(timespec="seconds"))
        check.close()
        database = cfg.resolve(cfg.database_path)
        with InstanceLock(database.with_suffix(".lock")):
            store = Storage(database)
            printer = DryRunPrinter() if cfg.test_mode else TiMiniPrinter(cfg)
            camera = LiveCamera(cfg.camera)
            try:
                controller = Controller(cfg, store, lambda _: camera.snapshot(), render_receipt, printer)
                root = tk.Tk()
                ReceiptWindow(root, cfg, controller, camera)
                root.mainloop()
            finally:
                try:
                    camera.stop()
                finally:
                    printer.close()
        return 0
    except Exception as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
