"""Faster asyncio event loop where supported (uvloop on Linux/macOS)."""

from __future__ import annotations


def install_fast_asyncio() -> str:
    """
    Prefer libuv-backed loop (lower overhead than stdlib selector loop).

    On Windows, uvloop is unavailable — returns ``stdlib``.
    """
    import asyncio
    import sys

    if sys.platform == "win32":
        return "stdlib"
    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        return "uvloop"
    except ImportError:
        return "stdlib"
