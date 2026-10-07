"""Create a self-contained homework ZIP, excluding secrets and build caches."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", ".venv", ".qa", ".codeagent", "__pycache__", ".pytest_cache", "artifacts", "build", "dist", "node_modules"}


def included(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return not any(part in EXCLUDED or part.endswith(".egg-info") for part in relative.parts) and not (
        path.name.startswith(".env") and path.name != ".env.example") and path.suffix not in {".pyc", ".pyo", ".key", ".pem"}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="生成学号姓名作业提交包")
    parser.add_argument("--student-id", default="2412190104")
    parser.add_argument("--name", default="唐佳杰")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9A-Za-z_-]{1,40}", args.student_id) or not re.fullmatch(r"[\w\u4e00-\u9fff-]{1,40}", args.name):
        parser.error("学号与姓名不得包含路径分隔符或特殊字符。")
    folder = f"{args.student_id}-{args.name}"
    destination = ROOT / "artifacts" / f"{folder}.zip"
    destination.parent.mkdir(exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file() and not path.is_symlink() and included(path):
                archive.write(path, f"{folder}/{path.relative_to(ROOT).as_posix()}")
    if destination.stat().st_size >= 200 * 1024 * 1024:
        raise SystemExit("提交包超过 200 MB，请缩小演示素材。")
    with ZipFile(destination) as archive:
        if archive.testzip():
            raise SystemExit("ZIP 完整性检查失败。")
        count = len(archive.namelist())
    print(f"已生成 {destination}（{count} 个文件，{destination.stat().st_size / 1024:.1f} KiB）")


if __name__ == "__main__":
    main()
