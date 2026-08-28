"""Notification backends for sending investigation results."""

import logging
from datetime import UTC, datetime
from typing import Protocol

import httpx

log = logging.getLogger("alert-triage")

COLOR_INVESTIGATION = 0x9B59B6  # purple
COLOR_UPDATE = 0x95A5A6  # muted grey
COLOR_RESOLVED = 0x2ECC71  # green
COLOR_FAILURE = 0xE74C3C  # red


class Notifier(Protocol):
    async def send(self, alertname: str, analysis: str, incident_id: str) -> None: ...

    async def send_update(
        self, alertname: str, message: str, incident_id: str
    ) -> None: ...

    async def send_resolved(
        self, alertname: str, incident_id: str, duration_str: str
    ) -> None: ...

    async def send_failure(self, alertname: str, message: str) -> None: ...


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    async def _post(
        self, title: str, description: str, color: int, footer: str
    ) -> None:
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = {
            "title": title,
            "description": description,
            "color": color,
            "footer": {"text": footer},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        async with httpx.AsyncClient() as http:
            try:
                resp = await http.post(
                    self.webhook_url, json={"embeds": [embed]}, timeout=10.0
                )
                resp.raise_for_status()
                log.info("Discord message posted: %s", title)
            except Exception as e:
                log.error("Failed to post to Discord: %s", e)

    async def send(self, alertname: str, analysis: str, incident_id: str) -> None:
        await self._post(
            title=f"Alert Triage: {alertname}",
            description=analysis,
            color=COLOR_INVESTIGATION,
            footer=f"Incident: {incident_id}",
        )

    async def send_update(self, alertname: str, message: str, incident_id: str) -> None:
        await self._post(
            title=f"Incident Update: {alertname}",
            description=message,
            color=COLOR_UPDATE,
            footer=f"Incident: {incident_id} (ongoing)",
        )

    async def send_resolved(
        self, alertname: str, incident_id: str, duration_str: str
    ) -> None:
        await self._post(
            title=f"Incident Resolved: {alertname}",
            description=f"All alerts have cleared. Total duration: {duration_str}.",
            color=COLOR_RESOLVED,
            footer=f"Incident: {incident_id}",
        )

    async def send_failure(self, alertname: str, message: str) -> None:
        await self._post(
            title=f"Triage Failed: {alertname}",
            description=message,
            color=COLOR_FAILURE,
            footer="No incident recorded",
        )


class NullNotifier:
    async def send(self, alertname: str, analysis: str, incident_id: str) -> None:
        log.info("Notification suppressed (no notifier configured) for %s", alertname)

    async def send_update(self, alertname: str, message: str, incident_id: str) -> None:
        log.info("Update notification suppressed for %s", alertname)

    async def send_resolved(
        self, alertname: str, incident_id: str, duration_str: str
    ) -> None:
        log.info("Resolved notification suppressed for %s", alertname)

    async def send_failure(self, alertname: str, message: str) -> None:
        log.info("Failure notification suppressed for %s", alertname)
