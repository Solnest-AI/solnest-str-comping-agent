"""pytest bootstrap — put the project root on sys.path for `import agent` etc.

Shared fixture-loading helpers live in `tests/_fixtures.py`, not here, so the
test modules import them once by name instead of pytest importing conftest as a
plugin and the tests importing a second copy of it as a module.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_TESTS = Path(__file__).parent
for p in (str(_ROOT), str(_TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)


import socket  # noqa: E402

import pytest  # noqa: E402

_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", ""}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _local(address) -> bool:
    """A unix socket path, or this computer: asyncio and local test servers use
    127.0.0.1 (Windows' event loop does even for its own wake-up pipe)."""
    if not isinstance(address, tuple):
        return True
    host = str(address[0]).lower()
    return host in _LOCAL_NAMES or host.startswith("127.") or host == "::ffff:127.0.0.1"


def _refuse(address):
    raise OSError(f"tests must not reach the network ({address!r}); fake the vendor instead")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No test reaches the internet, with or without keys on the machine.

    The end of every run checks GitHub once a day for a newer release
    (scripts/update.py); COMPING_NO_UPDATE_CHECK=1 turns that off, and tests of
    the check delete it. Blocked at the socket as well, not just urlopen:
    patching urlopen misses build_opener() and httpx (2026-10-10, a revenue
    manager "offline" test that was calling the real vendor)."""
    monkeypatch.setenv("COMPING_NO_UPDATE_CHECK", "1")

    def connect(self, address):
        return _real_connect(self, address) if _local(address) else _refuse(address)

    def connect_ex(self, address):
        return _real_connect_ex(self, address) if _local(address) else _refuse(address)

    def getaddrinfo(host, *args, **kwargs):
        if not _local((host,)):
            raise socket.gaierror(f"tests must not reach the network ({host!r}); fake the vendor instead")
        return _real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def _private_setup_stamp(tmp_path, monkeypatch):
    """A test that builds a 401/402/403 AirROIError must never delete the real
    .cache/setup_ok.json (kit.key_failure forgets it by design)."""
    import kit
    monkeypatch.setattr(kit, "SETUP_STAMP", tmp_path / "setup_ok.json")
    # Nor read or pin this machine's real kit choice.
    monkeypatch.setattr(kit, "KIT_CHOICE", tmp_path / "kit_path.txt")
