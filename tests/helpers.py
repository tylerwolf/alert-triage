"""Shared test doubles and payload factories."""

from types import SimpleNamespace


class RecordingNotifier:
    """Notifier that records every call instead of posting anywhere."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send(self, alertname: str, analysis: str, incident_id: str) -> None:
        self.calls.append(("send", alertname, analysis, incident_id))

    async def send_update(self, alertname: str, message: str, incident_id: str) -> None:
        self.calls.append(("send_update", alertname, message, incident_id))

    async def send_resolved(
        self, alertname: str, incident_id: str, duration_str: str
    ) -> None:
        self.calls.append(("send_resolved", alertname, incident_id, duration_str))

    async def send_failure(self, alertname: str, message: str) -> None:
        self.calls.append(("send_failure", alertname, message))

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


def make_alert(
    alertname: str = "TestAlert",
    fingerprint: str | None = "fp-test",
    instance: str = "host:9100",
    severity: str = "warning",
    **labels: str,
) -> dict:
    alert = {
        "labels": {
            "alertname": alertname,
            "instance": instance,
            "severity": severity,
            **labels,
        },
        "annotations": {
            "summary": f"{alertname} summary",
            "description": f"{alertname} description",
        },
    }
    if fingerprint is not None:
        alert["fingerprint"] = fingerprint
    return alert


def make_payload(status: str = "firing", alerts: list[dict] | None = None) -> dict:
    if alerts is None:
        alerts = [make_alert()]
    return {"status": status, "alerts": alerts}


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(name: str, input_data: dict, block_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=input_data, id=block_id)


def fake_response(
    content: list,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_write_tokens,
            cache_read_input_tokens=cache_read_tokens,
        ),
    )


class FakeAnthropic:
    """Stands in for anthropic.Anthropic; pops scripted responses per call.

    Repeats the last scripted response once the list is exhausted, so
    max_iterations tests can script a single always-tool-using response.
    """

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs) -> SimpleNamespace:
        self.calls.append(kwargs)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]
