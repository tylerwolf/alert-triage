"""Tool definitions and execution for alert investigation."""

import json
import time

import httpx

from .config import Settings

TOOLS = [
    {
        "name": "query_loki",
        "description": (
            "Query container logs via Loki's LogQL API. "
            'Use LogQL syntax, e.g. {container="sonarr"} for all sonarr logs, '
            'or {container="sonarr"} |= "error" to filter. '
            "Returns up to `limit` log lines from the last `lookback` period."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": 'LogQL query, e.g. {container="sonarr"} |= "error"',
                },
                "limit": {
                    "type": "integer",
                    "description": "Max log lines to return (default 50)",
                    "default": 50,
                },
                "lookback": {
                    "type": "string",
                    "description": "Time range to search, e.g. '1h', '30m', '6h' (default '1h')",
                    "default": "1h",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "query_prometheus",
        "description": (
            "Query Prometheus metrics via PromQL. Supports instant queries. "
            "Use for checking current metric values, e.g. "
            'up{job="blackbox"}, container_memory_usage_bytes, node_cpu_seconds_total.'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": 'PromQL query, e.g. up{job="blackbox"}',
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_docker_containers",
        "description": (
            "List Docker containers with their status, health, and resource usage. "
            "Can filter by container name. Returns container ID, name, state, status, "
            "and image for each container."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_filter": {
                    "type": "string",
                    "description": "Filter containers by name (substring match). Leave empty for all containers.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_alert_details",
        "description": (
            "Get current active alerts from Alertmanager. "
            "Returns all firing and pending alerts with their labels, annotations, "
            "and timing information."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


async def execute_tool(
    name: str, input_data: dict, http: httpx.AsyncClient, settings: Settings
) -> str:
    """Execute a tool call and return the result as a string."""
    try:
        if name == "query_loki":
            query = input_data["query"]
            limit = input_data.get("limit", 50)
            lookback = input_data.get("lookback", "1h")
            unit = lookback[-1]
            value = int(lookback[:-1])
            seconds = value * {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 3600)
            now_ns = int(time.time() * 1e9)
            start_ns = now_ns - (seconds * int(1e9))
            resp = await http.get(
                f"{settings.loki_url}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": str(start_ns),
                    "end": str(now_ns),
                    "limit": str(limit),
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("data", {}).get("result", [])
            lines = []
            for stream in results:
                labels = stream.get("stream", {})
                label_str = ", ".join(
                    f"{k}={v}" for k, v in labels.items() if k == "container"
                )
                for _ts, line in stream.get("values", []):
                    lines.append(f"[{label_str}] {line}")
            if not lines:
                return "No log lines found for this query."
            return "\n".join(lines[:limit])

        elif name == "query_prometheus":
            query = input_data["query"]
            resp = await http.get(
                f"{settings.prometheus_url}/api/v1/query",
                params={"query": query},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("data", {}).get("result", [])
            if not results:
                return "No results for this query."
            lines = []
            for r in results:
                metric = r.get("metric", {})
                val = r.get("value", [None, None])
                metric_str = json.dumps(metric, separators=(",", ":"))
                lines.append(f"{metric_str} => {val[1]}")
            return "\n".join(lines)

        elif name == "get_docker_containers":
            name_filter = input_data.get("name_filter", "")
            params = {}
            if name_filter:
                params["filters"] = json.dumps({"name": [name_filter]})
            transport = httpx.AsyncHTTPTransport(uds=settings.docker_socket)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://docker"
            ) as docker:
                resp = await docker.get("/containers/json", params=params, timeout=10.0)
                resp.raise_for_status()
                containers = resp.json()
            if not containers:
                return "No containers found matching the filter."
            lines = []
            for c in containers:
                names = ", ".join(n.lstrip("/") for n in c.get("Names", []))
                state = c.get("State", "unknown")
                status = c.get("Status", "unknown")
                image = c.get("Image", "unknown")
                lines.append(f"{names}: state={state}, status={status}, image={image}")
            return "\n".join(lines)

        elif name == "get_alert_details":
            resp = await http.get(
                f"{settings.alertmanager_url}/api/v2/alerts", timeout=10.0
            )
            resp.raise_for_status()
            alerts = resp.json()
            if not alerts:
                return "No active alerts in Alertmanager."
            lines = []
            for a in alerts:
                labels = a.get("labels", {})
                annotations = a.get("annotations", {})
                status = a.get("status", {}).get("state", "unknown")
                starts_at = a.get("startsAt", "unknown")
                lines.append(
                    f"[{status}] {labels.get('alertname', '?')} — "
                    f"{annotations.get('summary', 'no summary')} "
                    f"(since {starts_at})"
                )
            return "\n".join(lines)

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Error executing {name}: {e}"
