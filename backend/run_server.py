"""启动入口：由 serve.py / 部署脚本调用。"""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _config_path() -> Path:
    p = os.environ.get("CONFIG_PATH")
    if p:
        return Path(p)
    return _runtime_root() / "config.json"


def load_listen() -> tuple[str, int]:
    host = "0.0.0.0"
    port = 8000
    path = _config_path()
    if path.is_file():
        try:
            with path.open(encoding="utf-8") as f:
                cfg = json.load(f)
            srv = cfg.get("server") or {}
            h = srv.get("host")
            if isinstance(h, str) and h.strip():
                host = h.strip()
            p = srv.get("port")
            if isinstance(p, int) and 1 <= p <= 65535:
                port = p
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    if os.environ.get("HOST", "").strip():
        host = os.environ["HOST"].strip()
    if os.environ.get("PORT", "").strip():
        port = int(os.environ["PORT"])
    return host, port


def main() -> None:
    import uvicorn

    host, port = load_listen()
    reload = os.environ.get("RELOAD", "").strip() in ("1", "true", "yes") and not getattr(
        sys, "frozen", False
    )

    if getattr(sys, "frozen", False):
        os.chdir(str(Path(sys.executable).resolve().parent))

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=reload,
        factory=False,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
