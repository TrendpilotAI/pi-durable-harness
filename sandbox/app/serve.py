"""Dual-stack entrypoint for the sandbox service.

Railway's private network is IPv6-only in environments created before
2025-10-16 (production) and dual-stack in newer ones (staging), while Railway's
edge and health checks reach the container over IPv4. ``uvicorn --host ::``
is not enough: asyncio sets ``IPV6_V6ONLY`` on every AF_INET6 listener, so that
bind would serve the private network and fail the health check. Bind one
AF_INET6 socket with ``IPV6_V6ONLY=0`` ourselves and hand it to uvicorn, so a
single listener accepts both families.
"""

from __future__ import annotations

import logging
import os
import socket

import uvicorn

from app.config import settings


def bind_dual_stack(port: int, *, host: str = "::", backlog: int = 2048) -> socket.socket:
    """Return a listening AF_INET6 socket that also accepts IPv4 (v4-mapped)."""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind((host, port))
    sock.listen(backlog)
    return sock


def main() -> None:
    port = int(os.environ.get("PORT", settings.port))
    sock = bind_dual_stack(port)
    # Config() installs uvicorn's logging; log after it so the line is emitted.
    # uvicorn skips its own "running on" line when handed sockets.
    config = uvicorn.Config("app.main:app", log_level="info")
    logging.getLogger("uvicorn.error").info("Listening on [::]:%d (dual-stack)", port)
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
