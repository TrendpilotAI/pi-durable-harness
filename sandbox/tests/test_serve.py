"""The service listener must accept IPv4 and IPv6 on one port (Railway legacy
environments route private traffic over IPv6 only; health checks arrive over
IPv4)."""

from __future__ import annotations

import socket

import pytest

from app.serve import bind_dual_stack


def _connect(family: int, address: str, port: int) -> None:
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect((address, port))


def test_listener_is_not_v6only() -> None:
    sock = bind_dual_stack(0)
    try:
        assert sock.family == socket.AF_INET6
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
    finally:
        sock.close()


def test_listener_accepts_ipv4_and_ipv6() -> None:
    sock = bind_dual_stack(0)
    port = sock.getsockname()[1]
    try:
        _connect(socket.AF_INET, "127.0.0.1", port)
        try:
            _connect(socket.AF_INET6, "::1", port)
        except OSError as exc:  # host without a loopback IPv6 address
            pytest.skip(f"IPv6 loopback unavailable: {exc}")
    finally:
        sock.close()
