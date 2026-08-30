"""Tests for the /webhook and incident API routes (app.py)."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import alert_triage.app as app_module

from .helpers import make_alert, make_payload


@pytest.fixture
def investigate_mock(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(app_module, "investigate", mock)
    return mock


@pytest.fixture
def status_change_mock(monkeypatch):
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(app_module, "check_status_change", mock)
    return mock


def _seed_incident(store, incident_id="abc12345", alerts=None, diagnosis="prior"):
    store.save(
        incident_id=incident_id,
        alert_payload=make_payload(alerts=alerts or [make_alert()]),
        tool_transcript=[],
        diagnosis=diagnosis,
        model="test-model",
        tokens={"input": 1, "output": 2},
        duration_s=1.0,
    )


def _backdate(store, incident_id, **fields):
    data = store.get(incident_id)
    data.update(fields)
    store._write(incident_id, data)


async def _drain_tasks():
    for _ in range(5):
        await asyncio.sleep(0)


class TestIgnored:
    async def test_no_alerts(self, client):
        resp = await client.post("/webhook", json={"status": "firing", "alerts": []})
        assert resp.json() == {"status": "ignored", "reason": "no alerts"}

    async def test_unknown_status(self, client):
        resp = await client.post("/webhook", json=make_payload(status="pending"))
        assert resp.json() == {"status": "ignored", "reason": "status pending"}


class TestCoalescing:
    async def test_first_firing_buffers_then_flushes(
        self, client, app_state, investigate_mock
    ):
        resp = await client.post("/webhook", json=make_payload())
        assert resp.json() == {"status": "buffered"}
        task = app_module._coalesce["task"]
        await task  # window is 0 in tests: flush immediately
        investigate_mock.assert_awaited_once()
        args = investigate_mock.await_args.args
        assert args[0] == {"alerts": [make_alert()]}
        assert len(args[1]) == 8  # generated incident id

    async def test_second_payload_coalesces_and_dedupes(
        self, client, app_state, investigate_mock, monkeypatch
    ):
        monkeypatch.setattr(app_module.settings, "coalesce_window", 0.05)
        await client.post("/webhook", json=make_payload())
        resp = await client.post(
            "/webhook",
            json=make_payload(
                alerts=[
                    make_alert(),  # duplicate fingerprint — not re-added
                    make_alert("ServiceDown", fingerprint="fp-2"),
                ]
            ),
        )
        assert resp.json() == {"status": "coalesced"}
        await app_module._coalesce["task"]
        merged = investigate_mock.await_args.args[0]["alerts"]
        assert len(merged) == 2
        assert {a["fingerprint"] for a in merged} == {"fp-test", "fp-2"}


class TestCorrelation:
    async def test_repeat_throttled_update(self, client, app_state, status_change_mock):
        store, notifier = app_state["store"], app_state["notifier"]
        _seed_incident(store)  # last_notified_at = now -> throttled
        resp = await client.post("/webhook", json=make_payload())
        assert resp.json() == {"status": "correlated"}
        assert notifier.calls == []
        update = store.get("abc12345")["updates"][-1]
        assert update["type"] == "repeat"
        assert "no change" in update["message"]

    async def test_repeat_notifies_after_min_interval(
        self, client, app_state, status_change_mock
    ):
        store, notifier = app_state["store"], app_state["notifier"]
        _seed_incident(store)
        stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _backdate(store, "abc12345", last_notified_at=stale)
        resp = await client.post("/webhook", json=make_payload())
        assert resp.json() == {"status": "correlated"}
        assert notifier.kinds() == ["send_update"]

    async def test_repeat_with_delta_reports_change(
        self, client, app_state, status_change_mock
    ):
        status_change_mock.return_value = "New alerts firing: Other"
        store = app_state["store"]
        _seed_incident(store)
        resp = await client.post("/webhook", json=make_payload())
        assert resp.json() == {"status": "correlated"}
        message = store.get("abc12345")["updates"][-1]["message"]
        assert "Situation changed" in message
        assert "New alerts firing: Other" in message

    async def test_new_alert_joins_incident_delta_investigation(
        self, client, app_state, investigate_mock
    ):
        store = app_state["store"]
        _seed_incident(store, diagnosis="prior diagnosis")
        alerts = [make_alert(), make_alert("ServiceDown", fingerprint="fp-new")]
        resp = await client.post("/webhook", json=make_payload(alerts=alerts))
        assert resp.json() == {"status": "delta_investigation"}
        assert "fp-new" in store.get("abc12345")["alert_fingerprints"]
        await _drain_tasks()
        investigate_mock.assert_awaited_once()
        kwargs = investigate_mock.await_args.kwargs
        assert kwargs["append_to"] == "abc12345"
        assert kwargs["context"] == "prior diagnosis"
        assert investigate_mock.await_args.args[0] == {"alerts": alerts}


class TestReopenAndResolve:
    async def test_refire_reopens_resolved_incident(self, client, app_state):
        store, notifier = app_state["store"], app_state["notifier"]
        _seed_incident(store)
        store.mark_alerts_resolved("abc12345", {"fp-test"})
        resp = await client.post("/webhook", json=make_payload())
        assert resp.json() == {"status": "reopened"}
        assert store.get("abc12345")["status"] == "open"
        assert notifier.kinds() == ["send_update"]
        assert "Reopening incident" in notifier.calls[0][2]

    async def test_resolved_without_match(self, client, app_state):
        resp = await client.post("/webhook", json=make_payload(status="resolved"))
        assert resp.json() == {"status": "no_match"}

    async def test_partially_resolved(self, client, app_state):
        store = app_state["store"]
        alerts = [make_alert(fingerprint="fp1"), make_alert(fingerprint="fp2")]
        _seed_incident(store, alerts=alerts)
        resp = await client.post(
            "/webhook",
            json=make_payload(status="resolved", alerts=[alerts[0]]),
        )
        assert resp.json() == {"status": "partially_resolved"}
        assert store.get("abc12345")["status"] == "open"
        assert app_state["notifier"].calls == []

    async def test_fully_resolved_notifies_duration(self, client, app_state):
        store, notifier = app_state["store"], app_state["notifier"]
        _seed_incident(store)
        resp = await client.post("/webhook", json=make_payload(status="resolved"))
        assert resp.json() == {"status": "resolved"}
        assert store.get("abc12345")["status"] == "resolved"
        assert notifier.kinds() == ["send_resolved"]


class TestExclusion:
    async def test_excluded_only_payload_ignored(
        self, client, app_state, investigate_mock
    ):
        resp = await client.post(
            "/webhook",
            json=make_payload(alerts=[make_alert("Watchdog", fingerprint="fp-wd")]),
        )
        assert resp.json() == {"status": "ignored", "reason": "all alerts excluded"}
        assert app_module._coalesce is None
        investigate_mock.assert_not_awaited()

    async def test_mixed_payload_drops_excluded(
        self, client, app_state, investigate_mock
    ):
        resp = await client.post(
            "/webhook",
            json=make_payload(
                alerts=[
                    make_alert("Watchdog", fingerprint="fp-wd"),
                    make_alert("ServiceDown", fingerprint="fp-sd"),
                ]
            ),
        )
        assert resp.json() == {"status": "buffered"}
        await app_module._coalesce["task"]
        alerts = investigate_mock.await_args.args[0]["alerts"]
        assert [a["labels"]["alertname"] for a in alerts] == ["ServiceDown"]


class TestIncidentApi:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_get_incident_404(self, client):
        resp = await client.get("/incidents/nope")
        assert resp.status_code == 404

    async def test_get_incident_found(self, client, app_state):
        _seed_incident(app_state["store"])
        resp = await client.get("/incidents/abc12345")
        assert resp.status_code == 200
        assert resp.json()["incident_id"] == "abc12345"

    async def test_resolve_incident(self, client, app_state):
        _seed_incident(app_state["store"])
        resp = await client.post("/incidents/abc12345/resolve")
        assert resp.json() == {"status": "resolved", "incident_id": "abc12345"}
        assert app_state["store"].get("abc12345")["status"] == "resolved"

    async def test_resolve_incident_404(self, client, app_state):
        resp = await client.post("/incidents/nope/resolve")
        assert resp.status_code == 404

    async def test_resolve_incident_already_resolved(self, client, app_state):
        _seed_incident(app_state["store"])
        await client.post("/incidents/abc12345/resolve")
        resp = await client.post("/incidents/abc12345/resolve")
        assert resp.json() == {
            "status": "already_resolved",
            "incident_id": "abc12345",
        }

    async def test_list_incidents_limit_bounds(self, client, app_state):
        _seed_incident(app_state["store"])
        assert (await client.get("/incidents")).json()[0]["incident_id"] == "abc12345"
        assert (await client.get("/incidents", params={"limit": 0})).status_code == 422
        assert (
            await client.get("/incidents", params={"limit": 101})
        ).status_code == 422
