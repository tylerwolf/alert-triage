"""Tests for investigation tool execution (tools.py)."""

import json

import httpx
import pytest
import respx

from alert_triage.config import Settings
from alert_triage.tools import execute_tool


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


def _loki_result(streams):
    return {"data": {"result": streams}}


class TestQueryLoki:
    @respx.mock
    async def test_formats_log_lines(self, http, settings):
        respx.get("http://loki:3100/loki/api/v1/query_range").respond(
            json=_loki_result(
                [
                    {
                        "stream": {"container": "sonarr", "job": "docker"},
                        "values": [["1", "line one"], ["2", "line two"]],
                    }
                ]
            )
        )
        result = await execute_tool(
            "query_loki", {"query": '{container="sonarr"}'}, http, settings
        )
        assert result == "[container=sonarr] line one\n[container=sonarr] line two"

    @respx.mock
    @pytest.mark.parametrize(
        ("lookback", "expected_seconds"),
        [
            ("1h", 3600),
            ("30m", 1800),
            ("2d", 2 * 86400),
            ("1x", 3600),  # unknown unit falls back to hours
        ],
    )
    async def test_lookback_sets_query_range(
        self, http, settings, lookback, expected_seconds
    ):
        route = respx.get("http://loki:3100/loki/api/v1/query_range").respond(
            json=_loki_result([])
        )
        await execute_tool(
            "query_loki", {"query": "{}", "lookback": lookback}, http, settings
        )
        params = route.calls.last.request.url.params
        span_ns = int(params["end"]) - int(params["start"])
        assert span_ns == expected_seconds * int(1e9)

    async def test_malformed_lookback_returns_error_string(self, http, settings):
        result = await execute_tool(
            "query_loki", {"query": "{}", "lookback": "abc"}, http, settings
        )
        assert result.startswith("Error executing query_loki:")

    @respx.mock
    async def test_empty_result(self, http, settings):
        respx.get("http://loki:3100/loki/api/v1/query_range").respond(
            json=_loki_result([])
        )
        result = await execute_tool("query_loki", {"query": "{}"}, http, settings)
        assert result == "No log lines found for this query."

    @respx.mock
    async def test_limit_caps_returned_lines(self, http, settings):
        respx.get("http://loki:3100/loki/api/v1/query_range").respond(
            json=_loki_result(
                [{"stream": {"container": "a"}, "values": [["1", "l1"], ["2", "l2"]]}]
            )
        )
        result = await execute_tool(
            "query_loki", {"query": "{}", "limit": 1}, http, settings
        )
        assert result == "[container=a] l1"


class TestQueryPrometheus:
    @respx.mock
    async def test_formats_metric_values(self, http, settings):
        respx.get("http://prometheus:9090/api/v1/query").respond(
            json={
                "data": {
                    "result": [{"metric": {"__name__": "up"}, "value": [1234, "1"]}]
                }
            }
        )
        result = await execute_tool("query_prometheus", {"query": "up"}, http, settings)
        assert result == '{"__name__":"up"} => 1'

    @respx.mock
    async def test_empty_result(self, http, settings):
        respx.get("http://prometheus:9090/api/v1/query").respond(
            json={"data": {"result": []}}
        )
        result = await execute_tool("query_prometheus", {"query": "up"}, http, settings)
        assert result == "No results for this query."


class TestGetDockerContainers:
    @respx.mock
    async def test_lists_containers(self, http, settings):
        respx.get("http://docker/containers/json").respond(
            json=[
                {
                    "Names": ["/sonarr"],
                    "State": "running",
                    "Status": "Up 3 days",
                    "Image": "img:1.0",
                }
            ]
        )
        result = await execute_tool("get_docker_containers", {}, http, settings)
        assert result == "sonarr: state=running, status=Up 3 days, image=img:1.0"

    @respx.mock
    async def test_name_filter_serialized(self, http, settings):
        route = respx.get("http://docker/containers/json").respond(json=[])
        result = await execute_tool(
            "get_docker_containers", {"name_filter": "son"}, http, settings
        )
        params = route.calls.last.request.url.params
        assert json.loads(params["filters"]) == {"name": ["son"]}
        assert result == "No containers found matching the filter."


class TestGetAlertDetails:
    @respx.mock
    async def test_formats_alerts(self, http, settings):
        respx.get("http://alertmanager:9093/api/v2/alerts").respond(
            json=[
                {
                    "labels": {"alertname": "DNSDown"},
                    "annotations": {"summary": "DNS is down"},
                    "status": {"state": "active"},
                    "startsAt": "2026-08-19T00:00:00Z",
                }
            ]
        )
        result = await execute_tool("get_alert_details", {}, http, settings)
        assert result == "[active] DNSDown — DNS is down (since 2026-08-19T00:00:00Z)"

    @respx.mock
    async def test_no_alerts(self, http, settings):
        respx.get("http://alertmanager:9093/api/v2/alerts").respond(json=[])
        result = await execute_tool("get_alert_details", {}, http, settings)
        assert result == "No active alerts in Alertmanager."


class TestErrorHandling:
    async def test_unknown_tool(self, http, settings):
        result = await execute_tool("bogus", {}, http, settings)
        assert result == "Unknown tool: bogus"

    @respx.mock
    async def test_http_error_returns_error_string(self, http, settings):
        respx.get("http://prometheus:9090/api/v1/query").respond(status_code=500)
        result = await execute_tool("query_prometheus", {"query": "up"}, http, settings)
        assert result.startswith("Error executing query_prometheus:")
