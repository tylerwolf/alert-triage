"""Tests for incident storage and lifecycle (incidents.py)."""

import json
from datetime import UTC, datetime, timedelta

import alert_triage.incidents as incidents_module
from alert_triage.incidents import alert_fingerprint

from .helpers import make_alert, make_payload


def _save(store, incident_id="abc12345", alerts=None, diagnosis="diag"):
    payload = make_payload(alerts=alerts or [make_alert()])
    return store.save(
        incident_id=incident_id,
        alert_payload=payload,
        tool_transcript=[],
        diagnosis=diagnosis,
        model="test-model",
        tokens={"input": 1, "output": 2},
        duration_s=1.5,
    )


class TestAlertFingerprint:
    def test_prefers_alertmanager_fingerprint(self):
        alert = make_alert(fingerprint="am-fp-1")
        assert alert_fingerprint(alert) == "am-fp-1"

    def test_falls_back_to_label_hash(self):
        alert = make_alert(fingerprint=None)
        fp = alert_fingerprint(alert)
        assert len(fp) == 16
        assert fp != alert_fingerprint({"labels": {"other": "labels"}})

    def test_label_order_independent(self):
        a = {"labels": {"a": "1", "b": "2"}}
        b = {"labels": {"b": "2", "a": "1"}}
        assert alert_fingerprint(a) == alert_fingerprint(b)

    def test_empty_alert_is_stable(self):
        assert alert_fingerprint({}) == alert_fingerprint({})


class TestSaveAndGet:
    def test_save_creates_named_file_and_roundtrips(self, store):
        path = _save(store, "abc12345", [make_alert("DNSDown")])
        assert path.name.endswith("_abc12345_DNSDown.json")
        data = store.get("abc12345")
        assert data["incident_id"] == "abc12345"
        assert data["status"] == "open"
        assert data["diagnosis"] == "diag"
        assert data["alert_fingerprints"]["fp-test"]["status"] == "firing"

    def test_save_joins_sorted_alertnames(self, store):
        alerts = [
            make_alert("ServiceDown", fingerprint="fp2"),
            make_alert("DNSDown", fingerprint="fp1"),
        ]
        path = _save(store, "abc12345", alerts)
        assert "_abc12345_DNSDown+ServiceDown.json" in path.name

    def test_get_unknown_id_returns_none(self, store):
        assert store.get("nope") is None


class TestFindByFingerprints:
    def test_matches_open_incident_on_intersection(self, store):
        _save(store, "abc12345")
        found = store.find_by_fingerprints({"fp-test", "other"}, 86400, 1800)
        assert found is not None
        assert found["incident_id"] == "abc12345"

    def test_no_intersection_returns_none(self, store):
        _save(store, "abc12345")
        assert store.find_by_fingerprints({"unrelated"}, 86400, 1800) is None

    def test_idle_open_incident_auto_resolved(self, store, monkeypatch):
        _save(store, "abc12345")
        later = datetime.now(UTC) + timedelta(seconds=200)
        monkeypatch.setattr(incidents_module, "_now", lambda: later)
        assert store.find_by_fingerprints({"fp-test"}, 100, 10) is None
        data = store.get("abc12345")
        assert data["status"] == "resolved"
        assert data["auto_resolved"] is True
        assert data["updates"][-1]["type"] == "auto_resolved"

    def test_resolved_within_reopen_window_returned(self, store):
        _save(store, "abc12345")
        store.mark_alerts_resolved("abc12345", {"fp-test"})
        found = store.find_by_fingerprints({"fp-test"}, 86400, 1800)
        assert found is not None
        assert found["status"] == "resolved"

    def test_resolved_outside_reopen_window_skipped(self, store, monkeypatch):
        _save(store, "abc12345")
        store.mark_alerts_resolved("abc12345", {"fp-test"})
        later = datetime.now(UTC) + timedelta(seconds=3600)
        monkeypatch.setattr(incidents_module, "_now", lambda: later)
        assert store.find_by_fingerprints({"fp-test"}, 86400, 1800) is None

    def test_auto_resolved_incident_not_reopen_candidate(self, store, monkeypatch):
        _save(store, "abc12345")
        later = datetime.now(UTC) + timedelta(seconds=200)
        monkeypatch.setattr(incidents_module, "_now", lambda: later)
        # first scan auto-resolves it; second scan must not offer it for reopen
        store.find_by_fingerprints({"fp-test"}, 100, 86400)
        assert store.find_by_fingerprints({"fp-test"}, 100, 86400) is None

    def test_pre_020_file_without_status_skipped(self, store):
        legacy = store.dir / "2024-01-01_00-00-00_old12345_Legacy.json"
        legacy.write_text(json.dumps({"incident_id": "old12345"}))
        assert store.find_by_fingerprints({"fp-test"}, 86400, 1800) is None


class TestUpdatesAndLifecycle:
    def test_append_update_bumps_timestamps(self, store):
        _save(store, "abc12345")
        store.append_update("abc12345", {"type": "repeat", "message": "m"})
        data = store.get("abc12345")
        assert data["updates"][-1]["type"] == "repeat"
        assert data["updates"][-1]["timestamp"]  # defaulted
        assert data["last_update_at"] >= data["timestamp"]

    def test_append_update_notified_bumps_last_notified(self, store):
        _save(store, "abc12345")
        before = store.get("abc12345")["last_notified_at"]
        store.append_update("abc12345", {"type": "repeat"}, notified=True)
        assert store.get("abc12345")["last_notified_at"] >= before

    def test_append_update_missing_id_is_noop(self, store):
        store.append_update("nope", {"type": "repeat"})

    def test_lifecycle_methods_missing_id_are_noops(self, store):
        assert store.mark_alerts_resolved("nope", {"fp"}) is None
        store.add_alerts("nope", [make_alert()])
        store.reopen("nope", [make_alert()])

    def test_unparseable_timestamp_treated_as_fresh(self, store):
        _save(store, "abc12345")
        _path = store._path_for("abc12345")
        data = json.loads(_path.read_text())
        data["last_update_at"] = "not-a-timestamp"
        data["timestamp"] = None
        _path.write_text(json.dumps(data))
        # both timestamps unparseable -> TTL check skipped, incident matches
        found = store.find_by_fingerprints({"fp-test"}, 100, 10)
        assert found is not None

    def test_mark_alerts_resolved_partial_keeps_open(self, store):
        alerts = [make_alert(fingerprint="fp1"), make_alert(fingerprint="fp2")]
        _save(store, "abc12345", alerts)
        updated = store.mark_alerts_resolved("abc12345", {"fp1"})
        assert updated["status"] == "open"
        assert updated["alert_fingerprints"]["fp1"]["status"] == "resolved"
        assert updated["alert_fingerprints"]["fp2"]["status"] == "firing"

    def test_mark_alerts_resolved_all_closes(self, store):
        alerts = [make_alert(fingerprint="fp1"), make_alert(fingerprint="fp2")]
        _save(store, "abc12345", alerts)
        updated = store.mark_alerts_resolved("abc12345", {"fp1", "fp2"})
        assert updated["status"] == "resolved"
        assert updated["resolved_at"] is not None

    def test_resolve_closes_incident_and_all_alerts(self, store):
        alerts = [make_alert(fingerprint="fp1"), make_alert(fingerprint="fp2")]
        _save(store, "abc12345", alerts)
        updated = store.resolve("abc12345")
        assert updated["status"] == "resolved"
        assert updated["resolved_at"] is not None
        assert updated["manually_resolved"] is True
        assert all(
            v["status"] == "resolved" for v in updated["alert_fingerprints"].values()
        )
        assert updated["updates"][-1]["type"] == "manual_resolved"

    def test_resolve_missing_id_returns_none(self, store):
        assert store.resolve("nope") is None

    def test_resolve_already_resolved_is_noop(self, store):
        _save(store, "abc12345")
        store.mark_alerts_resolved("abc12345", {"fp-test"})
        updated = store.resolve("abc12345")
        assert updated["status"] == "resolved"
        assert "manually_resolved" not in updated
        assert not any(
            u["type"] == "manual_resolved" for u in updated.get("updates", [])
        )

    def test_manually_resolved_incident_not_reopen_candidate(self, store):
        _save(store, "abc12345")
        store.resolve("abc12345")
        assert store.find_by_fingerprints({"fp-test"}, 86400, 1800) is None

    def test_add_alerts_merges_new_fingerprints(self, store):
        _save(store, "abc12345")
        store.add_alerts("abc12345", [make_alert("Extra", fingerprint="fp-new")])
        fps = store.get("abc12345")["alert_fingerprints"]
        assert fps["fp-new"] == {
            "alertname": "Extra",
            "instance": "host:9100",
            "status": "firing",
        }
        assert "fp-test" in fps

    def test_reopen_restores_open_state(self, store):
        _save(store, "abc12345")
        store.mark_alerts_resolved("abc12345", {"fp-test"})
        store.reopen("abc12345", [make_alert()])
        data = store.get("abc12345")
        assert data["status"] == "open"
        assert data["resolved_at"] is None
        assert data["alert_fingerprints"]["fp-test"]["status"] == "firing"
        assert data["updates"][-1]["type"] == "reopened"


class TestListRecent:
    def test_limit_and_order(self, store, monkeypatch):
        base = datetime.now(UTC)
        for i, iid in enumerate(["aaa11111", "bbb22222", "ccc33333"]):
            monkeypatch.setattr(
                incidents_module, "_now", lambda i=i: base + timedelta(seconds=i)
            )
            _save(store, iid, [make_alert(f"Alert{i}", fingerprint=f"fp{i}")])
        recent = store.list_recent(limit=2)
        assert [r["incident_id"] for r in recent] == ["ccc33333", "bbb22222"]
        assert recent[0]["alerts"] == ["Alert2"]
        assert recent[0]["status"] == "open"
        assert recent[0]["updates"] == 0
