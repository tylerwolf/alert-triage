"""FastAPI application — webhook routes, coalescing, dedup, startup reconciliation."""

import asyncio
import logging
import time
import uuid

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings, build_system_prompt
from .incidents import IncidentStore
from .investigation import investigate
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

# Deduplication: frozenset of (alertname, instance) -> last investigation timestamp
_recent: dict[frozenset, float] = {}

# Concurrency cap
_semaphore = asyncio.Semaphore(settings.max_concurrent)

# Coalescing: buffer all alerts arriving in a window into one investigation
_coalesce: dict | None = None
_coalesce_lock = asyncio.Lock()


def _alert_fingerprint(alerts: list[dict]) -> frozenset:
    return frozenset(
        (
            a.get("labels", {}).get("alertname", ""),
            a.get("labels", {}).get("instance", ""),
        )
        for a in alerts
    )


async def _flush_coalesce() -> None:
    global _coalesce
    await asyncio.sleep(settings.coalesce_window)

    async with _coalesce_lock:
        if _coalesce is None:
            return
        alerts = _coalesce["alerts"]
        _coalesce = None

    fp = _alert_fingerprint(alerts)
    _recent[fp] = time.time()

    now = time.time()
    expired = [k for k, v in _recent.items() if now - v > settings.dedup_window]
    for k in expired:
        del _recent[k]

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

        active = [
            a
            for a in alerts
            if a.get("status", {}).get("state") == "active"
            and not a.get("status", {}).get("silencedBy")
        ]

        if not active:
            log.info("Startup reconciliation: no active non-silenced alerts")
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

    if payload.get("status") != "firing":
        return JSONResponse({"status": "ignored", "reason": "not firing"})

    incoming_alerts = payload.get("alerts", [])
    if not incoming_alerts:
        return JSONResponse({"status": "ignored", "reason": "no alerts"})

    fp = _alert_fingerprint(incoming_alerts)
    now = time.time()
    if fp in _recent and (now - _recent[fp]) < settings.dedup_window:
        log.info("Dedup: skipping (investigated %.0fs ago)", now - _recent[fp])
        return JSONResponse({"status": "deduped"})

    async with _coalesce_lock:
        if _coalesce is None:
            _coalesce = {
                "alerts": list(incoming_alerts),
                "seen": {
                    (
                        a.get("labels", {}).get("alertname", ""),
                        a.get("labels", {}).get("instance", ""),
                    )
                    for a in incoming_alerts
                },
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
                key = (
                    a.get("labels", {}).get("alertname", ""),
                    a.get("labels", {}).get("instance", ""),
                )
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
