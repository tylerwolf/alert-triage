"""Incident storage and retrieval."""

import json
from datetime import datetime, timezone
from pathlib import Path


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
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        incident_file = self.dir / f"{ts}_{incident_id}_{alertname}.json"
        incident_data = {
            "incident_id": incident_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
        matches = list(self.dir.glob(f"*_{incident_id}_*.json"))
        if not matches:
            return None
        return json.loads(matches[0].read_text())

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
                    "alerts": sorted(set(alertnames)),
                    "duration_s": data.get("duration_s"),
                    "tokens": data.get("tokens"),
                }
            )
        return incidents
