"""Configuration via environment variables with sensible defaults."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "ALERT_TRIAGE_"}

    # Required (no prefix — standard names)
    anthropic_api_key: str = Field(alias="ANTHROPIC_API_KEY")
    discord_webhook_url: str | None = Field(default=None, alias="DISCORD_WEBHOOK_URL")

    # Model & API
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024
    max_iterations: int = 10

    # Service URLs
    loki_url: str = "http://loki:3100"
    prometheus_url: str = "http://prometheus:9090"
    alertmanager_url: str = "http://alertmanager:9093"
    docker_socket: str = "/var/run/docker.sock"

    # Storage
    incidents_dir: Path = Path("/data/incidents")

    # System prompt
    system_prompt_file: Path | None = None

    # Timing
    dedup_window: int = 1800  # seconds
    coalesce_window: int = 45  # seconds
    max_concurrent: int = 3

    # Startup reconciliation
    startup_reconcile: bool = True
    reconcile_delay: int = 30  # seconds

    # Server
    port: int = 8099


BASE_SYSTEM_PROMPT = """\
You are an AI diagnostician for a Docker Compose infrastructure stack. Your job is to \
investigate firing Prometheus alerts by querying available data sources, then produce a \
concise diagnosis with suggested remediation.

## Investigation Approach
1. Understand the alert: what fired, severity, duration
2. Gather evidence: query relevant metrics, logs, and container status
3. Correlate: look for related issues across services
4. Diagnose: identify root cause or most likely explanation
5. Suggest: provide actionable remediation steps

## Guidelines
- Make 2-5 tool calls per investigation (more if needed for complex issues)
- For service-down alerts: always check container status + recent logs for that service
- For resource alerts (CPU/memory): query container-level metrics to find top consumers
- For mount/storage alerts: check if mount metrics exist, look for filesystem errors in logs
- Use Loki for container logs (LogQL), Prometheus for metrics (PromQL)

## Output Format
Provide your analysis as:
- **Confidence**: high / medium / low
- **Diagnosis**: 3-5 bullet points summarizing what you found
- **Suggested Actions**: concrete remediation steps the operator should take
"""


def build_system_prompt(settings: Settings) -> str:
    """Build the full system prompt from base + optional user environment file."""
    prompt = BASE_SYSTEM_PROMPT
    if settings.system_prompt_file and settings.system_prompt_file.exists():
        user_prompt = settings.system_prompt_file.read_text().strip()
        prompt = prompt + "\n\n" + user_prompt
    return prompt
