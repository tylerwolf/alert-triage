"""Tests for the Claude investigation loop and delta checks (investigation.py)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import alert_triage.investigation as investigation_module
from alert_triage.config import Settings
from alert_triage.investigation import check_status_change, investigate

from .helpers import (
    FakeAnthropic,
    RecordingNotifier,
    fake_response,
    make_payload,
    text_block,
    tool_use_block,
)


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def notifier():
    return RecordingNotifier()


def _patch_anthropic(monkeypatch, responses):
    fake = FakeAnthropic(responses)
    monkeypatch.setattr(
        investigation_module.anthropic, "Anthropic", lambda api_key: fake
    )
    return fake


class TestInvestigate:
    async def test_single_text_response_saves_and_notifies(
        self, monkeypatch, settings, notifier, store
    ):
        fake = _patch_anthropic(
            monkeypatch, [fake_response([text_block("the diagnosis")])]
        )
        await investigate(make_payload(), "abc12345", settings, "sys", notifier, store)
        assert len(fake.calls) == 1
        assert fake.calls[0]["system"] == [
            {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
        ]
        data = store.get("abc12345")
        assert data["diagnosis"] == "the diagnosis"
        assert data["tokens"] == {
            "input": 100,
            "output": 50,
            "cache_write": 0,
            "cache_read": 0,
        }
        assert data["tool_transcript"] == []
        assert notifier.calls == [("send", "TestAlert", "the diagnosis", "abc12345")]

    async def test_tool_use_loop_wires_tool_results(
        self, monkeypatch, settings, notifier, store
    ):
        fake = _patch_anthropic(
            monkeypatch,
            [
                fake_response(
                    [
                        text_block("checking logs"),
                        tool_use_block("query_loki", {"query": "{}"}, "tu_1"),
                    ],
                    stop_reason="tool_use",
                ),
                fake_response([text_block("final diagnosis")]),
            ],
        )
        execute_mock = AsyncMock(return_value="LOG LINES")
        monkeypatch.setattr(investigation_module, "execute_tool", execute_mock)

        await investigate(make_payload(), "abc12345", settings, "sys", notifier, store)
        assert len(fake.calls) == 2
        messages = fake.calls[1]["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        tool_result = messages[2]["content"][0]
        assert tool_result["type"] == "tool_result"
        assert tool_result["tool_use_id"] == "tu_1"
        assert tool_result["content"] == "LOG LINES"
        data = store.get("abc12345")
        assert data["diagnosis"] == "final diagnosis"
        transcript = data["tool_transcript"]
        assert len(transcript) == 1
        assert transcript[0]["tool"] == "query_loki"
        assert transcript[0]["output"] == "LOG LINES"

    async def test_long_tool_output_truncated(
        self, monkeypatch, settings, notifier, store
    ):
        _patch_anthropic(
            monkeypatch,
            [
                fake_response(
                    [tool_use_block("query_loki", {"query": "{}"}, "tu_1")],
                    stop_reason="tool_use",
                ),
                fake_response([text_block("done")]),
            ],
        )
        execute_mock = AsyncMock(return_value="x" * 5000)
        monkeypatch.setattr(investigation_module, "execute_tool", execute_mock)

        await investigate(make_payload(), "abc12345", settings, "sys", notifier, store)
        output = store.get("abc12345")["tool_transcript"][0]["output"]
        assert output.endswith("\n... (truncated)")
        assert len(output) == 4000 + len("\n... (truncated)")

    async def test_max_iterations_caps_loop(self, monkeypatch, notifier, store):
        settings = Settings(max_iterations=2)
        # a response that always asks for another tool call
        fake = _patch_anthropic(
            monkeypatch,
            [
                fake_response(
                    [
                        text_block("still looking"),
                        tool_use_block("query_loki", {"query": "{}"}, "tu_x"),
                    ],
                    stop_reason="tool_use",
                )
            ],
        )
        monkeypatch.setattr(
            investigation_module, "execute_tool", AsyncMock(return_value="out")
        )
        await investigate(make_payload(), "abc12345", settings, "sys", notifier, store)
        assert len(fake.calls) == 2
        assert store.get("abc12345")["diagnosis"] == "still looking"

    async def test_tokens_summed_across_iterations(
        self, monkeypatch, settings, notifier, store
    ):
        _patch_anthropic(
            monkeypatch,
            [
                fake_response(
                    [tool_use_block("query_loki", {"query": "{}"}, "tu_1")],
                    stop_reason="tool_use",
                    input_tokens=100,
                    output_tokens=10,
                    cache_write_tokens=4000,
                ),
                fake_response(
                    [text_block("done")],
                    input_tokens=200,
                    output_tokens=20,
                    cache_write_tokens=500,
                    cache_read_tokens=4000,
                ),
            ],
        )
        monkeypatch.setattr(
            investigation_module, "execute_tool", AsyncMock(return_value="out")
        )
        await investigate(make_payload(), "abc12345", settings, "sys", notifier, store)
        assert store.get("abc12345")["tokens"] == {
            "input": 300,
            "output": 30,
            "cache_write": 4500,
            "cache_read": 4000,
        }

    async def test_cache_marker_moves_to_latest_tool_results(
        self, monkeypatch, settings, notifier, store
    ):
        fake = _patch_anthropic(
            monkeypatch,
            [
                fake_response(
                    [tool_use_block("query_loki", {"query": "{}"}, "tu_1")],
                    stop_reason="tool_use",
                ),
                fake_response(
                    [tool_use_block("query_loki", {"query": "{}"}, "tu_2")],
                    stop_reason="tool_use",
                ),
                fake_response([text_block("done")]),
            ],
        )
        monkeypatch.setattr(
            investigation_module, "execute_tool", AsyncMock(return_value="out")
        )
        await investigate(make_payload(), "abc12345", settings, "sys", notifier, store)
        assert len(fake.calls) == 3
        messages = fake.calls[2]["messages"]
        first_results = messages[2]["content"]
        second_results = messages[4]["content"]
        assert "cache_control" not in first_results[-1]
        assert second_results[-1]["cache_control"] == {"type": "ephemeral"}

    async def test_api_failure_notifies_and_records_nothing(
        self, monkeypatch, settings, notifier, store
    ):
        class RaisingAnthropic:
            def __init__(self) -> None:
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(
            investigation_module.anthropic,
            "Anthropic",
            lambda api_key: RaisingAnthropic(),
        )
        await investigate(make_payload(), "abc12345", settings, "sys", notifier, store)
        assert store.get("abc12345") is None
        assert notifier.kinds() == ["send_failure"]
        _kind, alertname, message = notifier.calls[0]
        assert alertname == "TestAlert"
        assert "RuntimeError" in message
        assert "credit balance" not in message

    async def test_credit_exhaustion_failure_includes_reload_hint(
        self, monkeypatch, settings, notifier, store
    ):
        class RaisingAnthropic:
            def __init__(self) -> None:
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                raise RuntimeError(
                    "Your credit balance is too low to access the Anthropic API."
                )

        monkeypatch.setattr(
            investigation_module.anthropic,
            "Anthropic",
            lambda api_key: RaisingAnthropic(),
        )
        await investigate(make_payload(), "abc12345", settings, "sys", notifier, store)
        assert notifier.kinds() == ["send_failure"]
        assert "credit balance appears to be exhausted" in notifier.calls[0][2]

    async def test_delta_investigation_appends_to_incident(
        self, monkeypatch, settings, notifier, store
    ):
        store.save(
            incident_id="abc12345",
            alert_payload=make_payload(),
            tool_transcript=[],
            diagnosis="prior",
            model="m",
            tokens={"input": 1, "output": 1},
            duration_s=1.0,
        )
        fake = _patch_anthropic(
            monkeypatch, [fake_response([text_block("delta diagnosis")])]
        )
        await investigate(
            make_payload(),
            "unused-id",
            settings,
            "sys",
            notifier,
            store,
            context="prior diagnosis text",
            append_to="abc12345",
        )
        user_message = fake.calls[0]["messages"][0]["content"]
        assert "follow-up" in user_message
        assert "prior diagnosis text" in user_message
        data = store.get("abc12345")
        assert data["diagnosis"] == "prior"  # original untouched
        update = data["updates"][-1]
        assert update["type"] == "delta_investigation"
        assert update["diagnosis"] == "delta diagnosis"
        assert notifier.kinds() == ["send_update"]


class TestCheckStatusChange:
    def _incident(self):
        return {
            "alert_fingerprints": {
                "fp1": {
                    "alertname": "DNSDown",
                    "instance": "host:53",
                    "status": "firing",
                },
                "fp2": {
                    "alertname": "Resolved",
                    "instance": "",
                    "status": "resolved",
                },
            }
        }

    def _prom_result(self, series):
        return {"data": {"result": series}}

    @respx.mock
    async def test_unchanged_returns_none(self, settings):
        respx.get("http://prometheus:9090/api/v1/query").respond(
            json=self._prom_result(
                [{"metric": {"alertname": "DNSDown", "instance": "host:53"}}]
            )
        )
        assert await check_status_change(self._incident(), settings) is None

    @respx.mock
    async def test_new_and_cleared_alerts_reported(self, settings):
        respx.get("http://prometheus:9090/api/v1/query").respond(
            json=self._prom_result(
                [{"metric": {"alertname": "HighCPU", "instance": "host:9100"}}]
            )
        )
        delta = await check_status_change(self._incident(), settings)
        assert "New alerts firing: HighCPU (host:9100)" in delta
        assert "Alerts cleared: DNSDown (host:53)" in delta

    @respx.mock
    async def test_prometheus_unreachable_returns_none(self, settings):
        respx.get("http://prometheus:9090/api/v1/query").mock(
            side_effect=httpx.ConnectError("boom")
        )
        assert await check_status_change(self._incident(), settings) is None
