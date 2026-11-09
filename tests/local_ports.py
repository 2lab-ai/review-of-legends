"""A loopback port that refuses connections, for tests of what a client does
when nothing answers (tests/test_release.py, tests/test_dashboard_deploy.py).
"""

from __future__ import annotations

import socket
import unittest


def refused_port(test: unittest.TestCase) -> int:
    """A loopback port that refuses connections until ``test`` ends.

    A socket connected from it holds the port, so nothing else gets it from
    the kernel; with no listener there, a connection to it is refused at
    once. (A socket that is only bound does not do: macOS drops the
    connection attempt instead, and curl waits out its timeout.)
    """
    server = socket.create_server(("127.0.0.1", 0), backlog=1)
    test.addCleanup(server.close)
    holder = socket.create_connection(server.getsockname())
    test.addCleanup(holder.close)
    peer, _ = server.accept()
    test.addCleanup(peer.close)
    return holder.getsockname()[1]
