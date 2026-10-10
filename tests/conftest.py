import pytest


@pytest.fixture(autouse=True)
def _no_host_app(monkeypatch):
    """Tests must not depend on which app is in front on the machine running them (Cursor is refused there)."""
    from cu_mcp import server
    monkeypatch.setattr(server, "HOST_APPS", set())
