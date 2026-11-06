import os
from pathlib import Path


class InstanceLock:
    """OS lock is released on process exit; a remaining file is harmless."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")
        try:
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
        except (OSError, ImportError) as exc:
            self.handle.close()
            raise RuntimeError("同じデータベースを使う受付が起動済みか、ロックファイルにアクセスできません。") from exc

    def close(self):
        if not self.handle.closed:
            import msvcrt
            self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
