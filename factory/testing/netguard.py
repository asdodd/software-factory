"""Network guard for simulation profiles and every test run: any socket to a
non-local address fails loudly, and the attempt is remembered so the test
session fails even if the caller swallowed the error."""

from __future__ import annotations

import ipaddress
import socket

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_create_connection = socket.create_connection
violations: list[str] = []


class NetworkBlocked(ConnectionRefusedError):
    pass


def _is_local(address: object) -> bool:
    if isinstance(address, (str, bytes)):
        return True  # AF_UNIX path
    host = address[0] if isinstance(address, tuple) and address else address
    if host in ("localhost", "", None):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _check(address: object) -> None:
    if not _is_local(address):
        violations.append(repr(address))
        raise NetworkBlocked(f"network guard: connection to non-local address {address!r} blocked")


def _connect(self, address):
    _check(address)
    return _original_connect(self, address)


def _connect_ex(self, address):
    _check(address)
    return _original_connect_ex(self, address)


def _create_connection(address, *args, **kwargs):
    _check(address)
    return _original_create_connection(address, *args, **kwargs)


def install() -> None:
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.create_connection = _create_connection


def uninstall() -> None:
    socket.socket.connect = _original_connect
    socket.socket.connect_ex = _original_connect_ex
    socket.create_connection = _original_create_connection
