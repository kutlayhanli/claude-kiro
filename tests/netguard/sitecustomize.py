"""Socket guard for `ck` child processes under the `no_network` fixture.

Python imports this module at start-up when tests/netguard is first on PYTHONPATH. Every connect over an
AF_INET/AF_INET6 socket appends one line to $CK_TEST_NET_LOG and raises; Unix sockets stay allowed.
"""

import os
import socket

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _record(kind, address):
    path = os.environ.get("CK_TEST_NET_LOG")
    if path:
        try:
            with open(path, "a") as fh:
                fh.write(f"{kind} {address!r}\n")
        except OSError:
            pass


def _guarded(real, kind):
    def wrapper(self, address, *a, **k):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _record(kind, address)
            raise OSError("network blocked by tests/netguard")
        return real(self, address, *a, **k)

    return wrapper


def _create_connection(address, *a, **k):
    _record("create_connection", address)
    raise OSError("network blocked by tests/netguard")


socket.socket.connect = _guarded(_real_connect, "connect")
socket.socket.connect_ex = _guarded(_real_connect_ex, "connect_ex")
socket.create_connection = _create_connection
