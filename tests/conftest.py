"""Test fixtures.

The env vars below must be set before `alert_triage.app` is imported: the app
module instantiates Settings() and IncidentStore() at import time. Pytest
imports this conftest before collecting any test module, which guarantees the
ordering.
"""

import os
import tempfile

_session_dir = tempfile.mkdtemp(prefix="alert-triage-tests-")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")
os.environ["ALERT_TRIAGE_INCIDENTS_DIR"] = _session_dir
os.environ["ALERT_TRIAGE_STARTUP_RECONCILE"] = "false"
os.environ["ALERT_TRIAGE_COALESCE_WINDOW"] = "0"
os.environ.pop("DISCORD_WEBHOOK_URL", None)  # force NullNotifier at import

import httpx
import pytest

import alert_triage.app as app_module
from alert_triage.incidents import IncidentStore

from .helpers import RecordingNotifier


@pytest.fixture
def store(tmp_path) -> IncidentStore:
    return IncidentStore(tmp_path / "incidents")


@pytest.fixture
def app_state(tmp_path, monkeypatch):
    """Fresh per-test app globals: incident store, notifier, coalesce buffer."""
    fresh_store = IncidentStore(tmp_path / "incidents")
    notifier = RecordingNotifier()
    monkeypatch.setattr(app_module, "incident_store", fresh_store)
    monkeypatch.setattr(app_module, "notifier", notifier)
    monkeypatch.setattr(app_module, "_coalesce", None)
    yield {"store": fresh_store, "notifier": notifier}
    buf = app_module._coalesce
    if buf and (task := buf.get("task")):
        task.cancel()
    app_module._coalesce = None


@pytest.fixture
async def client(app_state):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
