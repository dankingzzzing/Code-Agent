"""Local remembered settings. Windows keys use the current user's DPAPI protection."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .config import Config
from .errors import ConfigError


def _windows_crypt(data: bytes, decrypt: bool = False) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buffer), Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    operation = library.CryptUnprotectData if decrypt else library.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ConfigError("无法用当前 Windows 用户保护或解密已保存的密钥，请重新填写。")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(output.data)


class SettingsStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / ".codeagent" / "web-settings.json"

    def _check(self):
        try:
            self.path.resolve().relative_to(self.root)
        except (ValueError, OSError, RuntimeError) as exc:
            raise ConfigError("本机设置目录超出了项目范围。") from exc

    def save(self, config: Config, demo: bool) -> None:
        self._check()
        public = asdict(config)
        key = public.pop("api_key")
        encrypted = _windows_crypt(key.encode()) if os.name == "nt" and key else key.encode()
        document = {"version": 1, "config": public, "demo": demo,
                    "key_protection": "windows-dpapi" if os.name == "nt" else "private-file",
                    "key_data": base64.b64encode(encrypted).decode("ascii")}
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                json.dump(document, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        except OSError as exc:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise ConfigError("无法保存本机模型配置，请检查项目目录权限。") from exc

    def load(self) -> tuple[Config, bool] | None:
        self._check()
        if not self.path.exists():
            return None
        try:
            if self.path.stat().st_size > 65536:
                raise ValueError("oversized settings")
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document["version"] != 1 or not isinstance(document["demo"], bool):
                raise ValueError("invalid settings")
            raw = base64.b64decode(document["key_data"], validate=True)
            if document["key_protection"] == "windows-dpapi":
                if os.name != "nt":
                    raise ValueError("different operating system")
                raw = _windows_crypt(raw, decrypt=True) if raw else b""
            elif document["key_protection"] != "private-file":
                raise ValueError("unknown protection")
            values = document["config"]
            allowed = set(Config.__dataclass_fields__) - {"api_key"}
            if not isinstance(values, dict) or set(values) != allowed:
                raise ValueError("invalid config fields")
            config = Config(api_key=raw.decode("utf-8"), **values)
            config = config.with_web_settings({})
            if not document["demo"]:
                config.require_llm()
            return config, document["demo"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError) as exc:
            raise ConfigError("已保存的本机配置损坏或无法解密，请重新配置模型。") from exc

    def clear(self) -> None:
        self._check()
        self.path.unlink(missing_ok=True)
