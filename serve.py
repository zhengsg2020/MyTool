#!/usr/bin/env python3
"""Single entry: run server or clean build artifacts."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def venv_python() -> Path:
    if sys.platform == "win32":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def ensure_venv() -> Path:
    exe = venv_python()
    if exe.is_file():
        return exe
    subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    if not exe.is_file():
        raise SystemExit("Failed to create .venv")
    return exe


def pip_cmd(py: Path, *args: str) -> list[str]:
    return [str(py), "-m", "pip", *args]


def pip_install_requirements(py: Path) -> None:
    req = ROOT / "backend" / "requirements.txt"
    extra = os.environ.get("MYTOOL_PIP_INSTALL_ARGS", "").strip()
    if extra:
        subprocess.run(
            pip_cmd(py, "install", "-q", "-r", str(req), *extra.split()),
            check=True,
        )
    else:
        subprocess.run(
            pip_cmd(
                py,
                "install",
                "-q",
                "-r",
                str(req),
                "--index-url",
                "https://pypi.org/simple",
                "--trusted-host",
                "pypi.org",
                "--trusted-host",
                "files.pythonhosted.org",
            ),
            check=True,
        )


def npm_build() -> None:
    fe = ROOT / "frontend"
    subprocess.run(["npm", "install"], cwd=fe, check=True)
    subprocess.run(["npm", "run", "build"], cwd=fe, check=True)


def cmd_run(args: argparse.Namespace) -> int:
    py = ensure_venv()
    pip_install_requirements(py)
    if not args.skip_frontend:
        npm_build()
    env = os.environ.copy()
    env["CONFIG_PATH"] = str(ROOT / "config.json")
    env["FRONTEND_DIST"] = str(ROOT / "frontend" / "dist")
    if args.port is not None:
        env["PORT"] = str(args.port)
    else:
        env.pop("PORT", None)
    return subprocess.run(
        [str(py), str(ROOT / "backend" / "run_server.py")],
        cwd=ROOT / "backend",
        env=env,
    ).returncode


def cmd_clean(args: argparse.Namespace) -> int:
    for p in (ROOT / "build", ROOT / "dist", ROOT / "frontend" / "dist"):
        if p.is_dir():
            shutil.rmtree(p)
            print("removed", p)
    if args.all and (ROOT / ".venv").is_dir():
        shutil.rmtree(ROOT / ".venv")
        print("removed", ROOT / ".venv")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Docker build web tool")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="venv + deps + optional FE build + start API")
    pr.add_argument("--skip-frontend", action="store_true", help="skip npm install/build")
    pr.add_argument("--port", type=int, default=None, help="override config server.port (env PORT)")

    pc = sub.add_parser("clean", help="remove build/, dist/, frontend/dist/")
    pc.add_argument("--all", action="store_true", help="also remove .venv")

    args = p.parse_args()
    if args.command == "run":
        return cmd_run(args)
    if args.command == "clean":
        return cmd_clean(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
