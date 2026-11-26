import socket

import pytest

from factory.testing import netguard


def test_non_local_connection_is_blocked_and_recorded():
    before = len(netguard.violations)
    with pytest.raises(netguard.NetworkBlocked):
        socket.create_connection(("203.0.113.10", 443), timeout=1)
    assert len(netguard.violations) == before + 1
    del netguard.violations[before:]


def test_loopback_is_allowed():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=1)
    client.close()
    server.close()
