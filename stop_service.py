#!/usr/bin/env python3
"""Stop running service by configured TCP port."""
from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
PID_FILE = ROOT / "service.pid"


def read_port() -> int:
    port_env = os.environ.get("PORT", "").strip()
    if port_env:
        return int(port_env)
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return 8000
    server = cfg.get("server") if isinstance(cfg, dict) else {}
    port = server.get("port") if isinstance(server, dict) else 8000
    if isinstance(port, int) and 1 <= port <= 65535:
        return port
    return 8000


def windows_pids_by_port(port: int) -> set[int]:
    pids: set[int] = set()
    proc = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    needle = f":{port}"
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or needle not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            pids.add(int(parts[-1]))
    return pids


def unix_pids_by_port(port: int) -> set[int]:
    # 1) lsof (best)
    if shutil.which("lsof"):
        proc = subprocess.run(
            ["lsof", "-t", f"-i:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        pids: set[int] = set()
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
        return pids

    # 2) ss (fallback, common on newer Linux)
    if shutil.which("ss"):
        proc = subprocess.run(
            ["ss", "-ltnp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        pids: set[int] = set()
        needle = f":{port} "
        for line in proc.stdout.splitlines():
            if needle not in line:
                continue
            # Example: users:(("python3",pid=123,fd=3))
            # Keep parsing as best-effort to avoid dependencies.
            for token in line.split("pid=")[1:]:
                pid_part = token.split(",")[0].strip()
                if pid_part.isdigit():
                    pids.add(int(pid_part))
        return pids

    # 3) netstat (very rough)
    if shutil.which("netstat"):
        proc = subprocess.run(
            ["netstat", "-ltnp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        pids: set[int] = set()
        needle = f":{port} "
        for line in proc.stdout.splitlines():
            if needle not in line:
                continue
            # Example: ... 0.0.0.0:8000 ... users:(("python3",pid=123,fd=3))
            for token in line.split("pid=")[1:]:
                pid_part = token.split(",")[0].strip()
                if pid_part.isdigit():
                    pids.add(int(pid_part))
        return pids

    return set()


def kill_pids(pids: set[int]) -> int:
    if not pids:
        return 0
    for pid in sorted(pids):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False)
            else:
                os.kill(pid, signal.SIGTERM)
            print(f"Stopped PID {pid}")
        except Exception as exc:
            print(f"Failed to stop PID {pid}: {exc}")
            return 1
    return 0


def try_kill_pid_file() -> bool:
    """
    If service.pid exists, prefer killing that PID.
    This makes `--bg` runs stoppable even if lsof/ss parsing differs.
    """
    if not PID_FILE.is_file():
        return False
    try:
        raw = PID_FILE.read_text(encoding="utf-8", errors="ignore").strip()
        if not raw:
            return False
        pid = int(raw.split()[0])
    except Exception:
        return False

    # kill
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False)
        else:
            # Check if process exists
            os.kill(pid, 0)
            os.kill(pid, signal.SIGTERM)
    except Exception:
        # If it's already dead, we still clean pid file.
        pass
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    print(f"Stopped by pid file: {pid}")
    return True


def main() -> int:
    port = read_port()
    print(f"Target port: {port}")

    # Prefer stopping the PID we started in --bg mode.
    if try_kill_pid_file():
        return 0

    if os.name == "nt":
        pids = windows_pids_by_port(port)
    else:
        pids = unix_pids_by_port(port)
    if not pids:
        print("No running service process found.")
        return 0
    return kill_pids(pids)


if __name__ == "__main__":
    raise SystemExit(main())
