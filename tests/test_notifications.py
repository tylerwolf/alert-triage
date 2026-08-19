"""Tests for Discord notifications (notifications.py)."""

import json

import httpx
import respx

from alert_triage.notifications import (
    COLOR_INVESTIGATION,
    COLOR_RESOLVED,
    COLOR_UPDATE,
    DiscordNotifier,
    NullNotifier,
)

WEBHOOK = "https://discord.test/webhook"


def _sent_embed(route) -> dict:
    body = json.loads(route.calls.last.request.content)
    return body["embeds"][0]


class TestDiscordNotifier:
    @respx.mock
    async def test_send_posts_investigation_embed(self):
        route = respx.post(WEBHOOK).respond(status_code=204)
        await DiscordNotifier(WEBHOOK).send("DNSDown", "analysis text", "abc12345")
        embed = _sent_embed(route)
        assert embed["title"] == "Alert Triage: DNSDown"
        assert embed["description"] == "analysis text"
        assert embed["color"] == COLOR_INVESTIGATION
        assert embed["footer"]["text"] == "Incident: abc12345"

    @respx.mock
    async def test_send_update_and_resolved_embeds(self):
        route = respx.post(WEBHOOK).respond(status_code=204)
        notifier = DiscordNotifier(WEBHOOK)
        await notifier.send_update("A", "msg", "abc12345")
        embed = _sent_embed(route)
        assert embed["title"] == "Incident Update: A"
        assert embed["color"] == COLOR_UPDATE
        assert embed["footer"]["text"] == "Incident: abc12345 (ongoing)"

        await notifier.send_resolved("A", "abc12345", "2h 1m")
        embed = _sent_embed(route)
        assert embed["title"] == "Incident Resolved: A"
        assert embed["color"] == COLOR_RESOLVED
        assert "Total duration: 2h 1m." in embed["description"]

    @respx.mock
    async def test_long_description_truncated(self):
        route = respx.post(WEBHOOK).respond(status_code=204)
        await DiscordNotifier(WEBHOOK).send("A", "x" * 5000, "abc12345")
        description = _sent_embed(route)["description"]
        assert len(description) == 4000
        assert description.endswith("...")

    @respx.mock
    async def test_http_error_swallowed(self):
        respx.post(WEBHOOK).respond(status_code=500)
        await DiscordNotifier(WEBHOOK).send("A", "text", "abc12345")

    @respx.mock
    async def test_connection_error_swallowed(self):
        respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError("boom"))
        await DiscordNotifier(WEBHOOK).send("A", "text", "abc12345")


class TestNullNotifier:
    async def test_all_methods_are_noops(self):
        notifier = NullNotifier()
        await notifier.send("A", "text", "abc12345")
        await notifier.send_update("A", "msg", "abc12345")
        await notifier.send_resolved("A", "abc12345", "1m")
