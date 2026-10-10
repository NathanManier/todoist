import socket

import pytest
import requests


@pytest.fixture(autouse=True)
def block_live_network(monkeypatch):
    """This suite must never reach Canvas/Todoist or use real credentials."""
    def blocked(*args, **kwargs):
        raise AssertionError('Live network access is forbidden in tests')

    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
