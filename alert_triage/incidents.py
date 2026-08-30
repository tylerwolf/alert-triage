"""Incident storage, retrieval, and lifecycle management."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path


def alert_fingerprint(alert: dict) -> str:
    """Fingerprint for a single alert.

    Prefers Alertmanager's own per-alert fingerprint (stable across repeats
    and firing/resolved transitions); falls back to a hash of sorted labels.
    """
    fp = alert.get("fingerprint")
    if fp:
        return fp
    labels = alert.get("labels", {})
    canonical = json.dumps(sorted(labels.items()), separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class IncidentStore:
    def __init__(self, incidents_dir: Path) -> None:
        self.dir = incidents_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        incident_id: str,
        alert_payload: dict,
        tool_transcript: list[dict],
        diagnosis: str,
        model: str,
        tokens: dict,
        duration_s: float,
    ) -> Path:
        alerts = alert_payload.get("alerts", [])
        alertnames = sorted(
            {a.get("labels", {}).get("alertname", "unknown") for a in alerts}
        )
        alertname = "+".join(alertnames) if alertnames else "unknown"
        now = _now()
        ts = now.strftime("%Y-%m-%d_%H-%M-%S")
        incident_file = self.dir / f"{ts}_{incident_id}_{alertname}.json"
        fingerprints = {
            alert_fingerprint(a): {
                "alertname": a.get("labels", {}).get("alertname", "unknown"),
                "instance": a.get("labels", {}).get("instance", ""),
                "status": "firing",
            }
            for a in alerts
        }
        incident_data = {
            "incident_id": incident_id,
            "timestamp": now.isoformat(),
            "status": "open",
            "alert_fingerprints": fingerprints,
            "updates": [],
            "last_update_at": now.isoformat(),
            "last_notified_at": now.isoformat(),
            "resolved_at": None,
            "alert_payload": alert_payload,
            "tool_transcript": tool_transcript,
            "diagnosis": diagnosis,
            "model": model,
            "tokens": tokens,
            "duration_s": round(duration_s, 2),
        }
        incident_file.write_text(json.dumps(incident_data, indent=2, default=str))
        return incident_file

    def get(self, incident_id: str) -> dict | None:
        path = self._path_for(incident_id)
        if path is None:
            return None
        return json.loads(path.read_text())

    def _path_for(self, incident_id: str) -> Path | None:
        matches = list(self.dir.glob(f"*_{incident_id}_*.json"))
        return matches[0] if matches else None

    def _write(self, incident_id: str, data: dict) -> None:
        path = self._path_for(incident_id)
        if path is not None:
            path.write_text(json.dumps(data, indent=2, default=str))

    def find_by_fingerprints(
        self,
        fingerprints: set[str],
        incident_ttl: int,
        reopen_window: int,
    ) -> dict | None:
        """Find the most recent incident whose alert set intersects `fingerprints`.

        Returns an open incident, or a resolved incident that closed within
        `reopen_window` seconds (a candidate for reopening). Open incidents idle
        past `incident_ttl` are lazily marked resolved during the scan.
        """
        now = _now()
        for f in sorted(self.dir.glob("*.json"), reverse=True):
            data = json.loads(f.read_text())
            status = data.get("status")
            if status not in ("open", "resolved"):
                continue  # pre-0.2.0 incident file without lifecycle fields

            if status == "open":
                last_update = _parse_ts(data.get("last_update_at")) or _parse_ts(
                    data.get("timestamp")
                )
                if (
                    last_update is not None
                    and (now - last_update).total_seconds() > incident_ttl
                ):
                    data["status"] = "resolved"
                    data["resolved_at"] = now.isoformat()
                    data["auto_resolved"] = True
                    data["updates"].append(
                        {
                            "timestamp": now.isoformat(),
                            "type": "auto_resolved",
                            "message": f"Auto-resolved after {incident_ttl}s of inactivity",
                        }
                    )
                    f.write_text(json.dumps(data, indent=2, default=str))
                    continue
                if fingerprints & set(data.get("alert_fingerprints", {})):
                    return data
            else:  # resolved — only a match if within the reopen window
                if data.get("auto_resolved") or data.get("manually_resolved"):
                    continue  # TTL-expired/human-closed incidents don't reopen
                resolved_at = _parse_ts(data.get("resolved_at"))
                if (
                    resolved_at is not None
                    and (now - resolved_at).total_seconds() <= reopen_window
                    and fingerprints & set(data.get("alert_fingerprints", {}))
                ):
                    return data
        return None

    def append_update(
        self,
        incident_id: str,
        entry: dict,
        notified: bool = False,
    ) -> None:
        data = self.get(incident_id)
        if data is None:
            return
        now = _now().isoformat()
        entry.setdefault("timestamp", now)
        data.setdefault("updates", []).append(entry)
        data["last_update_at"] = now
        if notified:
            data["last_notified_at"] = now
        self._write(incident_id, data)

    def mark_alerts_resolved(
        self, incident_id: str, fingerprints: set[str]
    ) -> dict | None:
        """Mark alerts resolved; close the incident when none remain firing.

        Returns the updated incident data.
        """
        data = self.get(incident_id)
        if data is None:
            return None
        now = _now().isoformat()
        fps = data.setdefault("alert_fingerprints", {})
        for fp in fingerprints:
            if fp in fps:
                fps[fp]["status"] = "resolved"
        if all(v.get("status") == "resolved" for v in fps.values()):
            data["status"] = "resolved"
            data["resolved_at"] = now
        data["last_update_at"] = now
        self._write(incident_id, data)
        return data

    def resolve(self, incident_id: str) -> dict | None:
        """Manually resolve an incident regardless of remaining firing alerts.

        Returns the updated incident data, or None if the incident doesn't exist.
        Already-resolved incidents are returned unchanged.
        """
        data = self.get(incident_id)
        if data is None:
            return None
        if data.get("status") == "resolved":
            return data
        now = _now().isoformat()
        for v in data.setdefault("alert_fingerprints", {}).values():
            v["status"] = "resolved"
        data["status"] = "resolved"
        data["resolved_at"] = now
        data["manually_resolved"] = True
        data.setdefault("updates", []).append(
            {
                "timestamp": now,
                "type": "manual_resolved",
                "message": "Manually resolved via API",
            }
        )
        data["last_update_at"] = now
        self._write(incident_id, data)
        return data

    def add_alerts(self, incident_id: str, alerts: list[dict]) -> None:
        """Merge newly-firing alerts into an open incident's fingerprint set."""
        data = self.get(incident_id)
        if data is None:
            return
        fps = data.setdefault("alert_fingerprints", {})
        for a in alerts:
            fp = alert_fingerprint(a)
            fps[fp] = {
                "alertname": a.get("labels", {}).get("alertname", "unknown"),
                "instance": a.get("labels", {}).get("instance", ""),
                "status": "firing",
            }
        data["last_update_at"] = _now().isoformat()
        self._write(incident_id, data)

    def reopen(self, incident_id: str, alerts: list[dict]) -> None:
        """Reopen a recently-resolved incident (flap) and re-fire matching alerts."""
        data = self.get(incident_id)
        if data is None:
            return
        now = _now().isoformat()
        data["status"] = "open"
        data["resolved_at"] = None
        fps = data.setdefault("alert_fingerprints", {})
        for a in alerts:
            fp = alert_fingerprint(a)
            if fp in fps:
                fps[fp]["status"] = "firing"
        data.setdefault("updates", []).append(
            {
                "timestamp": now,
                "type": "reopened",
                "message": "Alert re-fired shortly after resolution (flap)",
            }
        )
        data["last_update_at"] = now
        self._write(incident_id, data)

    def list_recent(self, limit: int = 20) -> list[dict]:
        files = sorted(self.dir.glob("*.json"), reverse=True)[:limit]
        incidents = []
        for f in files:
            data = json.loads(f.read_text())
            alertnames = [
                a.get("labels", {}).get("alertname", "unknown")
                for a in data.get("alert_payload", {}).get("alerts", [])
            ]
            incidents.append(
                {
                    "incident_id": data.get("incident_id"),
                    "timestamp": data.get("timestamp"),
                    "status": data.get("status", "unknown"),
                    "resolved_at": data.get("resolved_at"),
                    "alerts": sorted(set(alertnames)),
                    "updates": len(data.get("updates", [])),
                    "duration_s": data.get("duration_s"),
                    "tokens": data.get("tokens"),
                }
            )
        return incidents
