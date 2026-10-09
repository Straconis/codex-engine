from __future__ import annotations

import argparse
import ipaddress
import sys
import threading
from typing import BinaryIO, Callable

import uvicorn

from codex_engine.app import app, stop_and_exit


def loopback_host(host: str) -> str:
    """Accept only addresses on this computer: the API has no login, so it must never be
    reachable from the network."""
    if host == "localhost":
        return host
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"{host} is not a loopback address; use 127.0.0.1 or localhost.")


def exit_when_closed(stream: BinaryIO, on_close: Callable[[], None]) -> threading.Thread:
    """Call on_close once the other end of `stream` is closed.

    The desktop app holds the write end of the backend's stdin. If the app crashes or is
    killed, the OS closes it, so the backend (and its Ollama) stops instead of lingering
    on the same database.
    """

    def watch() -> None:
        try:
            while stream.read(4096):
                pass
        except (OSError, ValueError):
            pass
        on_close()

    thread = threading.Thread(target=watch, name="parent-watch", daemon=True)
    thread.start()
    return thread


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex Engine backend sidecar")
    parser.add_argument("--host", type=loopback_host, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--exit-with-stdin", action="store_true", help="stop when stdin is closed (used by the desktop app)")
    args = parser.parse_args()
    if args.exit_with_stdin:
        exit_when_closed(sys.stdin.buffer, stop_and_exit)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
