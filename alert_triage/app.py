"""FastAPI application — webhook routes, correlation, coalescing, startup reconciliation."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings, build_system_prompt
from .filtering import split_excluded
from .incidents import IncidentStore, alert_fingerprint
from .investigation import check_status_change, investigate
from .notifications import DiscordNotifier, NullNotifier

app = FastAPI(title="Alert Triage")
log = logging.getLogger("alert-triage")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

settings = Settings()
system_prompt = build_system_prompt(settings)
incident_store = IncidentStore(settings.incidents_dir)

if settings.discord_webhook_url:
    notifier = DiscordNotifier(settings.discord_webhook_url)
else:
    notifier = NullNotifier()

# Concurrency cap
_semaphore = asyncio.Semaphore(settings.max_concurrent)

# Coalescing: buffer all alerts arriving in a window into one investigation
_coalesce: dict | None = None
_coalesce_lock = asyncio.Lock()


def _alertname(alerts: list[dict]) -> str:
    names = sorted({a.get("labels", {}).get("alertname", "unknown") for a in alerts})
    return "+".join(names) if names else "unknown"


def _reconcilable_alerts(alerts: list[dict]) -> list[dict]:
    active = [
        a
        for a in alerts
        if a.get("status", {}).get("state") == "active"
        and not a.get("status", {}).get("silencedBy")
    ]
    kept, dropped = split_excluded(active, settings.excluded_alert_names)
    if dropped:
        log.info(
            "Startup reconciliation: excluding %s",
            ", ".join(sorted({_alertname([a]) for a in dropped})),
        )
    return kept


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours, rem = divmod(seconds, 3600)
    return f"{hours}h {rem // 60}m"


def _incident_age(incident: dict) -> float:
    started = datetime.fromisoformat(incident["timestamp"])
    return (datetime.now(UTC) - started).total_seconds()


async def _flush_coalesce() -> None:
    global _coalesce
    await asyncio.sleep(settings.coalesce_window)

    async with _coalesce_lock:
        if _coalesce is None:
            return
        alerts = _coalesce["alerts"]
        _coalesce = None

    incident_id = str(uuid.uuid4())[:8]
    alert_desc = ", ".join(
        sorted({a.get("labels", {}).get("alertname", "?") for a in alerts})
    )
    log.info(
        "Flushing coalesced buffer: %d alerts [%s] -> investigation %s",
        len(alerts),
        alert_desc,
        incident_id,
    )

    async with _semaphore:
        await investigate(
            {"alerts": alerts},
            incident_id,
            settings,
            system_prompt,
            notifier,
            incident_store,
        )


async def _handle_repeat(incident: dict, alerts: list[dict]) -> str:
    """Handle firing alerts that correlate to an existing open incident.

    Returns a short status string for the webhook response.
    """
    incident_id = incident["incident_id"]
    known_fps = set(incident.get("alert_fingerprints", {}))
    new_alerts = [a for a in alerts if alert_fingerprint(a) not in known_fps]

    if new_alerts:
        # Alert set changed — focused delta investigation with prior context
        incident_store.add_alerts(incident_id, new_alerts)
        log.info(
            "Incident %s: %d new alert(s) joined, running delta investigation",
            incident_id,
            len(new_alerts),
        )

        async def _delta() -> None:
            async with _semaphore:
                await investigate(
                    {"alerts": alerts},
                    incident_id,
                    settings,
                    system_prompt,
                    notifier,
                    incident_store,
                    context=incident.get("diagnosis", ""),
                    append_to=incident_id,
                )

        asyncio.create_task(_delta())
        return "delta_investigation"

    # Same alert set — lightweight check, no Claude call
    delta = await check_status_change(incident, settings)
    age = _format_duration(_incident_age(incident))

    last_notified = incident.get("last_notified_at") or incident.get("timestamp")
    since_notified = (
        datetime.now(UTC) - datetime.fromisoformat(last_notified)
    ).total_seconds()
    should_notify = since_notified >= settings.update_min_interval

    if delta:
        message = f"Ongoing for {age}. Situation changed:\n{delta}"
    else:
        message = f"Still firing — no change since initial diagnosis ({age} ago)."

    incident_store.append_update(
        incident_id,
        {"type": "repeat", "message": message},
        notified=should_notify,
    )
    if should_notify:
        await notifier.send_update(_alertname(alerts), message, incident_id)
        log.info("Incident %s: posted status update (%s)", incident_id, age)
    else:
        log.info(
            "Incident %s: repeat received, update throttled (%ds since last notify)",
            incident_id,
            since_notified,
        )
    return "correlated"


async def _handle_resolved(alerts: list[dict]) -> str:
    fps = {alert_fingerprint(a) for a in alerts}
    incident = incident_store.find_by_fingerprints(
        fps, settings.incident_ttl, settings.reopen_window
    )
    if incident is None or incident.get("status") != "open":
        log.info("Resolved alerts with no matching open incident, ignoring")
        return "no_match"

    incident_id = incident["incident_id"]
    updated = incident_store.mark_alerts_resolved(incident_id, fps)
    if updated is not None and updated.get("status") == "resolved":
        duration = _format_duration(_incident_age(updated))
        log.info("Incident %s fully resolved after %s", incident_id, duration)
        await notifier.send_resolved(_alertname(alerts), incident_id, duration)
        return "resolved"
    log.info(
        "Incident %s: %d alert(s) resolved, incident still open", incident_id, len(fps)
    )
    return "partially_resolved"


@app.on_event("startup")
async def startup_reconcile() -> None:
    if not settings.startup_reconcile:
        return

    async def _reconcile() -> None:
        await asyncio.sleep(settings.reconcile_delay)
        log.info("Startup reconciliation: checking Alertmanager for firing alerts")
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    f"{settings.alertmanager_url}/api/v2/alerts",
                    timeout=10.0,
                )
                resp.raise_for_status()
                alerts = resp.json()
        except Exception as e:
            log.warning("Startup reconciliation failed to reach Alertmanager: %s", e)
            return

        active = _reconcilable_alerts(alerts)

        if not active:
            log.info("Startup reconciliation: no reconcilable alerts")
            return

        # Correlate against existing open incidents so a restart mid-outage
        # doesn't spawn a duplicate investigation.
        fps = {alert_fingerprint(a) for a in active}
        incident = incident_store.find_by_fingerprints(
            fps, settings.incident_ttl, settings.reopen_window
        )
        if incident is not None and incident.get("status") == "open":
            log.info(
                "Startup reconciliation: alerts correlate to open incident %s",
                incident["incident_id"],
            )
            await _handle_repeat(incident, active)
            return

        alertnames = sorted({a.get("labels", {}).get("alertname", "?") for a in active})
        log.info(
            "Startup reconciliation: found %d active alert(s) [%s], investigating",
            len(active),
            ", ".join(alertnames),
        )
        incident_id = str(uuid.uuid4())[:8]
        async with _semaphore:
            await investigate(
                {"alerts": active},
                incident_id,
                settings,
                system_prompt,
                notifier,
                incident_store,
            )

    asyncio.create_task(_reconcile())


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    global _coalesce
    payload = await request.json()

    incoming_alerts = payload.get("alerts", [])
    if not incoming_alerts:
        return JSONResponse({"status": "ignored", "reason": "no alerts"})

    incoming_alerts, dropped = split_excluded(
        incoming_alerts, settings.excluded_alert_names
    )
    if dropped:
        log.info("Webhook: excluding %s", _alertname(dropped))
    if not incoming_alerts:
        return JSONResponse({"status": "ignored", "reason": "all alerts excluded"})

    status = payload.get("status")
    if status == "resolved":
        result = await _handle_resolved(incoming_alerts)
        return JSONResponse({"status": result})
    if status != "firing":
        return JSONResponse({"status": "ignored", "reason": f"status {status}"})

    fps = {alert_fingerprint(a) for a in incoming_alerts}
    incident = incident_store.find_by_fingerprints(
        fps, settings.incident_ttl, settings.reopen_window
    )

    if incident is not None:
        if incident.get("status") == "resolved":
            # Re-fired shortly after resolution — reopen instead of new incident
            incident_id = incident["incident_id"]
            incident_store.reopen(incident_id, incoming_alerts)
            age = _format_duration(_incident_age(incident))
            log.info("Incident %s reopened (flap)", incident_id)
            await notifier.send_update(
                _alertname(incoming_alerts),
                f"Alert re-fired shortly after resolution (flapping?). "
                f"Reopening incident (originally started {age} ago).",
                incident_id,
            )
            return JSONResponse({"status": "reopened"})
        result = await _handle_repeat(incident, incoming_alerts)
        return JSONResponse({"status": result})

    async with _coalesce_lock:
        if _coalesce is None:
            _coalesce = {
                "alerts": list(incoming_alerts),
                "seen": {alert_fingerprint(a) for a in incoming_alerts},
            }
            _coalesce["task"] = asyncio.create_task(_flush_coalesce())
            log.info(
                "Coalesce: buffered %d alert(s), window open for %ds",
                len(incoming_alerts),
                settings.coalesce_window,
            )
            return JSONResponse({"status": "buffered"})
        else:
            added = 0
            for a in incoming_alerts:
                key = alert_fingerprint(a)
                if key not in _coalesce["seen"]:
                    _coalesce["alerts"].append(a)
                    _coalesce["seen"].add(key)
                    added += 1
            log.info(
                "Coalesce: merged %d new alert(s) into buffer (total: %d)",
                added,
                len(_coalesce["alerts"]),
            )
            return JSONResponse({"status": "coalesced"})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str) -> JSONResponse:
    data = incident_store.get(incident_id)
    if data is None:
        return JSONResponse({"error": "Incident not found"}, status_code=404)
    return JSONResponse(data)


@app.get("/incidents")
async def list_incidents(
    limit: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    return JSONResponse(incident_store.list_recent(limit))
