"""Claude-powered alert investigation loop."""

import logging
import time

import anthropic
import httpx

from .config import Settings
from .incidents import IncidentStore
from .notifications import Notifier
from .tools import TOOLS, execute_tool

log = logging.getLogger("alert-triage")


async def check_status_change(incident: dict, settings: Settings) -> str | None:
    """Lightweight delta check for an ongoing incident (no Claude call).

    Queries Prometheus for currently-firing alerts and compares against the
    incident's alert set. Returns None if nothing changed, otherwise a short
    human-readable delta summary.
    """
    incident_alerts = {
        (v.get("alertname", ""), v.get("instance", ""))
        for v in incident.get("alert_fingerprints", {}).values()
        if v.get("status") == "firing"
    }

    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"{settings.prometheus_url}/api/v1/query",
                params={"query": 'ALERTS{alertstate="firing"}'},
                timeout=15.0,
            )
            resp.raise_for_status()
            results = resp.json().get("data", {}).get("result", [])
    except Exception as e:
        log.warning("Delta check failed to reach Prometheus: %s", e)
        return None

    currently_firing = {
        (
            r.get("metric", {}).get("alertname", ""),
            r.get("metric", {}).get("instance", ""),
        )
        for r in results
    }

    new_alerts = currently_firing - incident_alerts
    cleared_alerts = incident_alerts - currently_firing
    if not new_alerts and not cleared_alerts:
        return None

    parts = []
    if new_alerts:
        parts.append(
            "New alerts firing: "
            + ", ".join(sorted(f"{a} ({i})" if i else a for a, i in new_alerts))
        )
    if cleared_alerts:
        parts.append(
            "Alerts cleared: "
            + ", ".join(sorted(f"{a} ({i})" if i else a for a, i in cleared_alerts))
        )
    return "\n".join(parts)


async def investigate(
    alert_payload: dict,
    incident_id: str,
    settings: Settings,
    system_prompt: str,
    notifier: Notifier,
    incident_store: IncidentStore,
    context: str | None = None,
    append_to: str | None = None,
) -> None:
    """Run an AI-powered investigation for a set of firing alerts.

    When `append_to` is set, this is a focused delta investigation on an
    existing incident: `context` carries the prior diagnosis, and results are
    appended to that incident instead of creating a new one.
    """
    start_time = time.monotonic()
    tool_transcript: list[dict] = []
    alerts = alert_payload.get("alerts", [])
    alert_summary = []
    for a in alerts:
        labels = a.get("labels", {})
        annotations = a.get("annotations", {})
        alert_summary.append(
            f"- **{labels.get('alertname', 'unknown')}** ({labels.get('severity', '?')}): "
            f"{annotations.get('summary', 'no summary')} — "
            f"{annotations.get('description', 'no description')}"
        )

    if context:
        user_message = (
            "This is a follow-up on an ongoing incident. Prior diagnosis:\n\n"
            + context
            + "\n\nThe situation has changed. The following alerts are now firing:\n\n"
            + "\n".join(alert_summary)
            + "\n\nInvestigate what changed since the prior diagnosis. Focus on the "
            "delta — do not re-diagnose what is already covered above."
        )
    else:
        user_message = (
            "The following alerts are firing:\n\n"
            + "\n".join(alert_summary)
            + "\n\nInvestigate these alerts. Query relevant data sources to determine "
            "the root cause and suggest remediation steps."
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    messages = [{"role": "user", "content": user_message}]
    total_input_tokens = 0
    total_output_tokens = 0
    final_text = ""

    async with httpx.AsyncClient() as http:
        for _iteration in range(settings.max_iterations):
            response = client.messages.create(
                model=settings.model,
                max_tokens=settings.max_tokens,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens

            text_parts = []
            tool_uses = []
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append(block)

            if text_parts:
                final_text = "\n".join(text_parts)

            if response.stop_reason == "end_turn" or not tool_uses:
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tool_use in tool_uses:
                tool_start = time.monotonic()
                result = await execute_tool(
                    tool_use.name, tool_use.input, http, settings
                )
                tool_duration = time.monotonic() - tool_start
                if len(result) > 4000:
                    result = result[:4000] + "\n... (truncated)"
                tool_transcript.append(
                    {
                        "tool": tool_use.name,
                        "input": tool_use.input,
                        "output": result,
                        "duration_s": round(tool_duration, 2),
                    }
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

    total_duration = time.monotonic() - start_time

    alertnames = sorted(
        {a.get("labels", {}).get("alertname", "unknown") for a in alerts}
    )
    alertname = "+".join(alertnames) if alertnames else "unknown"

    if append_to:
        incident_store.append_update(
            append_to,
            {
                "type": "delta_investigation",
                "alert_payload": alert_payload,
                "tool_transcript": tool_transcript,
                "diagnosis": final_text,
                "tokens": {
                    "input": total_input_tokens,
                    "output": total_output_tokens,
                },
                "duration_s": round(total_duration, 2),
            },
            notified=True,
        )
        log.info("Delta investigation appended to incident %s", append_to)
        await notifier.send_update(alertname, final_text, append_to)
    else:
        incident_file = incident_store.save(
            incident_id=incident_id,
            alert_payload=alert_payload,
            tool_transcript=tool_transcript,
            diagnosis=final_text,
            model=settings.model,
            tokens={"input": total_input_tokens, "output": total_output_tokens},
            duration_s=total_duration,
        )
        log.info("Incident saved to %s", incident_file)
        await notifier.send(alertname, final_text, incident_id)
