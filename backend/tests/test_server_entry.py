"""The packaged backend's entry point: loopback-only host and exit-with-parent."""
from __future__ import annotations

import argparse
import os
import threading

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pymupdf")

import server_entry  # noqa: E402


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host):
    assert server_entry.loopback_host(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com", ""])
def test_network_hosts_are_refused(host):
    with pytest.raises(argparse.ArgumentTypeError):
        server_entry.loopback_host(host)


def test_backend_stops_when_the_app_closes_its_stdin():
    read_end, write_end = os.pipe()
    closed = threading.Event()
    with os.fdopen(read_end, "rb", buffering=0) as stream:
        thread = server_entry.exit_when_closed(stream, closed.set)
        os.write(write_end, b"still here")
        assert not closed.wait(0.3)
        os.close(write_end)  # what the OS does when the app dies
        assert closed.wait(5)
        thread.join(5)
