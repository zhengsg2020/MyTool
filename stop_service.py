#!/usr/bin/env python3
"""Stop running service by configured TCP port."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))


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


def main() -> int:
    port = read_port()
    print(f"Target port: {port}")
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
