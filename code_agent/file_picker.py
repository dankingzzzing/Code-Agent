"""Native desktop dialogs isolated from the HTTP server's worker threads."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .errors import AgentError


def pick_local_path(kind: str, initial_dir: Path) -> str | None:
    if kind not in {"file", "directory"}:
        raise AgentError("请选择文件或文件夹。")
    command = [sys.executable, "-m", "code_agent.file_picker", "--kind", kind,
               "--initial-dir", str(initial_dir)]
    try:
        process = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=180, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        value = json.loads(process.stdout)
        if process.returncode or not value.get("ok"):
            raise AgentError(value.get("error", "无法打开文件选择器，可直接粘贴本地路径。"))
        path = value.get("path")
        if path is not None and not isinstance(path, str):
            raise ValueError("invalid path")
        return path or None
    except subprocess.TimeoutExpired as exc:
        raise AgentError("文件选择已超时，请重新选择。") from exc
    except (OSError, ValueError) as exc:
        raise AgentError("无法打开本机文件选择器，可直接粘贴文件或文件夹的绝对路径。") from exc


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("file", "directory"), required=True)
    parser.add_argument("--initial-dir", type=Path, required=True)
    args = parser.parse_args()
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        options = {"parent": root, "initialdir": str(args.initial_dir)}
        if args.kind == "directory":
            path = filedialog.askdirectory(title="选择需要审查的代码文件夹", mustexist=True, **options)
        else:
            path = filedialog.askopenfilename(title="选择需要审查的代码文件", filetypes=[
                ("代码和文本", "*.py *.js *.ts *.tsx *.jsx *.java *.go *.rs *.c *.cpp *.md *.txt"),
                ("所有文件", "*.*")], **options)
        print(json.dumps({"ok": True, "path": path or None}, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "error": "本机没有可用的桌面文件选择器，请直接粘贴本地路径。"}, ensure_ascii=False))
        return 1
    finally:
        if root is not None:
            root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
