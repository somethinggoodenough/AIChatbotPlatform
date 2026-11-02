"""Run every offline suite. Network connections are restricted to loopback."""

import ipaddress
from pathlib import Path
import socket
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def local_only(original):
    def connect(sock, address):
        if isinstance(address, tuple):
            host = address[0]
            if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
                raise RuntimeError("Offline acceptance forbids non-loopback connections")
        return original(sock, address)
    return connect


def main() -> int:
    socket.socket.connect = local_only(socket.socket.connect)
    socket.socket.connect_ex = local_only(socket.socket.connect_ex)
    suite = unittest.TestSuite()
    # Independent discovery keeps existing non-package test directories working.
    for directory in sorted({p.parent for p in (ROOT / "tests").rglob("test_*.py")}):
        suite.addTests(unittest.TestLoader().discover(str(directory), pattern="test_*.py"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
