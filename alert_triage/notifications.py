"""Notification backends for sending investigation results."""

import logging
from datetime import datetime, timezone
from typing import Protocol

import httpx

log = logging.getLogger("alert-triage")


class Notifier(Protocol):
    async def send(self, alertname: str, analysis: str, incident_id: str) -> None: ...


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    async def send(self, alertname: str, analysis: str, incident_id: str) -> None:
        if len(analysis) > 4000:
            analysis = analysis[:3997] + "..."

        embed = {
            "title": f"Alert Triage: {alertname}",
            "description": analysis,
            "color": 0x9B59B6,
            "footer": {"text": f"Incident: {incident_id}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        async with httpx.AsyncClient() as http:
            try:
                resp = await http.post(
                    self.webhook_url, json={"embeds": [embed]}, timeout=10.0
                )
                resp.raise_for_status()
                log.info("Discord message posted for %s", alertname)
            except Exception as e:
                log.error("Failed to post to Discord: %s", e)


class NullNotifier:
    async def send(self, alertname: str, analysis: str, incident_id: str) -> None:
        log.info("Notification suppressed (no notifier configured) for %s", alertname)
