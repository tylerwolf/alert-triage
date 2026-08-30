"""Tests for startup reconciliation (app.py) — filter seam and full startup path."""

import asyncio
from unittest.mock import AsyncMock

import pytest
import respx

import alert_triage.app as app_module

from .helpers import make_alert


def _am_alert(alertname: str, state: str = "active", silenced_by: list | None = None):
    alert = make_alert(alertname, fingerprint=None)
    alert["status"] = {"state": state, "silencedBy": silenced_by or []}
    return alert


async def _drain_tasks():
    for _ in range(10):
        await asyncio.sleep(0)


class TestReconcilableAlerts:
    def test_filters_state_silence_and_exclusion(self):
        active = _am_alert("ServiceDown")
        suppressed = _am_alert("HighCPU", state="suppressed")
        silenced = _am_alert("DiskFull", silenced_by=["silence-id"])
        watchdog = _am_alert("Watchdog")
        result = app_module._reconcilable_alerts(
            [active, suppressed, silenced, watchdog]
        )
        assert result == [active]


class TestStartupReconcile:
    @pytest.fixture
    def reconcile_env(self, app_state, monkeypatch):
        monkeypatch.setattr(app_module.settings, "startup_reconcile", True)
        monkeypatch.setattr(app_module.settings, "reconcile_delay", 0)
        mock = AsyncMock()
        monkeypatch.setattr(app_module, "investigate", mock)
        return mock

    @respx.mock
    async def test_skips_excluded_alert(self, reconcile_env):
        respx.get("http://alertmanager:9093/api/v2/alerts").respond(
            json=[_am_alert("Watchdog")]
        )
        await app_module.startup_reconcile()
        await _drain_tasks()
        reconcile_env.assert_not_awaited()

    @respx.mock
    async def test_investigates_real_alert(self, reconcile_env):
        respx.get("http://alertmanager:9093/api/v2/alerts").respond(
            json=[_am_alert("Watchdog"), _am_alert("ServiceDown")]
        )
        await app_module.startup_reconcile()
        await _drain_tasks()
        reconcile_env.assert_awaited_once()
        alerts = reconcile_env.await_args.args[0]["alerts"]
        assert [a["labels"]["alertname"] for a in alerts] == ["ServiceDown"]
