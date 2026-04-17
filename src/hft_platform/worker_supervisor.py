"""Spawn/stop lite HFT worker subprocesses on the same host as the API (dev / single-node)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "src" / "run_lite_worker.py"
_lock = threading.Lock()
_processes: dict[str, subprocess.Popen] = {}


def _api_base_reachable_from_host(api_base: str) -> str:
    """If the server bound to 0.0.0.0, child must call 127.0.0.1, not 0.0.0.0."""
    p = urlparse(api_base)
    host = p.hostname or "127.0.0.1"
    if host in ("0.0.0.0", "[::]", "::"):
        port = p.port or (443 if p.scheme == "https" else 80)
        return urlunparse((p.scheme, f"127.0.0.1:{port}", p.path or "", "", "", "")).rstrip("/")
    return api_base.rstrip("/")


def spawn_lite_worker(*, api_base: str, session_id: str, worker_token: str) -> dict[str, Any]:
    """Start `run_lite_worker.py` for this session if not already running. Returns spawn metadata."""
    if not _SCRIPT.is_file():
        raise FileNotFoundError(f"Worker script missing: {_SCRIPT}")

    base = _api_base_reachable_from_host(api_base)
    with _lock:
        existing = _processes.get(session_id)
        if existing is not None and existing.poll() is None:
            return {"spawned": False, "pid": existing.pid, "detail": "worker already running for this session"}

        if existing is not None:
            del _processes[session_id]

        uv = shutil.which("uv")
        if uv:
            cmd = [
                uv,
                "run",
                "python",
                str(_SCRIPT),
                "--api-base",
                base,
                "--session-id",
                session_id,
                "--worker-token",
                worker_token,
            ]
        else:
            cmd = [
                sys.executable,
                str(_SCRIPT),
                "--api-base",
                base,
                "--session-id",
                session_id,
                "--worker-token",
                worker_token,
            ]

        env = os.environ.copy()
        env["HFT_API_BASE"] = base
        env["HFT_SESSION_ID"] = session_id
        env["HFT_WORKER_TOKEN"] = worker_token

        # Never use PIPE for stderr without a reader: the buffer fills (~64KiB) and the
        # worker blocks on write, which freezes telemetry and the whole asyncio loop.
        kwargs: dict[str, Any] = {
            "cwd": str(_REPO_ROOT),
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform != "win32":
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as e:
            logger.exception("Failed to spawn lite worker for session %s", session_id)
            raise RuntimeError(f"spawn failed: {e}") from e

        _processes[session_id] = proc
        logger.info("Spawned lite worker for session %s pid=%s cmd=%s", session_id, proc.pid, cmd[0])
        return {"spawned": True, "pid": proc.pid, "detail": None}


def stop_lite_worker(session_id: str) -> dict[str, Any]:
    """Terminate supervised worker for this session, if any."""
    with _lock:
        proc = _processes.pop(session_id, None)
    if proc is None:
        return {"stopped": False, "detail": "no supervised worker for this session"}
    if proc.poll() is not None:
        return {"stopped": False, "detail": "worker already exited", "exit_code": proc.returncode}
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    logger.info("Stopped lite worker for session %s", session_id)
    return {"stopped": True, "detail": None, "exit_code": proc.returncode}
