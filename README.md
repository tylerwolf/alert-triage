# alert-triage

AI-powered alert diagnosis for Docker Compose stacks. Receives Alertmanager webhooks, investigates using Claude tool-calling (Loki logs, Prometheus metrics, Docker status), and posts diagnoses to Discord.

## Quick Start

1. Create an env file with your API keys:

```bash
# alert-triage.env
ANTHROPIC_API_KEY=sk-ant-...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

2. Add to your monitoring stack's `docker-compose.yml`:

```yaml
services:
  alert-triage:
    image: ghcr.io/tylerwolf/alert-triage:0.1.0
    container_name: alert-triage
    restart: unless-stopped
    env_file: alert-triage.env
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - alert_triage_data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8099/health')"]
      interval: 30s
      timeout: 5s
      retries: 3

volumes:
  alert_triage_data:
```

3. Point Alertmanager at it:

```yaml
# alertmanager.yml
receivers:
  - name: alert-triage
    webhook_configs:
      - url: http://alert-triage:8099/webhook
        send_resolved: false
```

That's it. Alerts fire, investigations run, diagnoses land in Discord.

## System Prompt

Out of the box, alert-triage uses a generic investigation prompt. For better results, describe your environment in a markdown file and mount it:

```yaml
environment:
  - ALERT_TRIAGE_SYSTEM_PROMPT_FILE=/config/system-prompt.md
volumes:
  - ./my-system-prompt.md:/config/system-prompt.md:ro
```

The file should describe your services, network layout, and alert types. See [`system-prompt.example.md`](system-prompt.example.md) for a template.

The generic base prompt (investigation methodology, output format) is always included — your file is appended to it.

## Configuration

All configuration is via environment variables. Variables prefixed with `ALERT_TRIAGE_` are specific to this project; `ANTHROPIC_API_KEY` and `DISCORD_WEBHOOK_URL` use their standard names.

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required)* | Anthropic API key |
| `DISCORD_WEBHOOK_URL` | `None` | Discord webhook URL (omit to disable notifications) |
| `ALERT_TRIAGE_MODEL` | `claude-sonnet-4-6` | Claude model to use |
| `ALERT_TRIAGE_LOKI_URL` | `http://loki:3100` | Loki base URL |
| `ALERT_TRIAGE_PROMETHEUS_URL` | `http://prometheus:9090` | Prometheus base URL |
| `ALERT_TRIAGE_ALERTMANAGER_URL` | `http://alertmanager:9093` | Alertmanager base URL |
| `ALERT_TRIAGE_DOCKER_SOCKET` | `/var/run/docker.sock` | Docker socket path |
| `ALERT_TRIAGE_INCIDENTS_DIR` | `/data/incidents` | Directory for incident JSON files |
| `ALERT_TRIAGE_SYSTEM_PROMPT_FILE` | `None` | Path to environment description markdown |
| `ALERT_TRIAGE_DEDUP_WINDOW` | `1800` | Seconds to suppress duplicate alert sets |
| `ALERT_TRIAGE_COALESCE_WINDOW` | `45` | Seconds to buffer alerts before investigating |
| `ALERT_TRIAGE_MAX_CONCURRENT` | `3` | Max concurrent investigations |
| `ALERT_TRIAGE_MAX_ITERATIONS` | `10` | Max tool-calling loop iterations per investigation |
| `ALERT_TRIAGE_MAX_TOKENS` | `1024` | Max response tokens per Claude call |
| `ALERT_TRIAGE_STARTUP_RECONCILE` | `true` | Check for firing alerts on startup |
| `ALERT_TRIAGE_RECONCILE_DELAY` | `30` | Seconds to wait before startup reconciliation |
| `ALERT_TRIAGE_PORT` | `8099` | Server port |

## How It Works

1. **Alertmanager** sends a webhook when alerts fire
2. **Coalescing** buffers alerts for 45 seconds to group related alerts into one investigation
3. **Dedup** skips alert sets already investigated in the last 30 minutes
4. **Claude** receives the alert details and uses four tools to investigate:
   - `query_loki` — search container logs via LogQL
   - `query_prometheus` — query metrics via PromQL
   - `get_docker_containers` — list container status via Docker API
   - `get_alert_details` — get current alerts from Alertmanager
5. **Diagnosis** is posted to Discord and saved as a JSON incident file
6. **Startup reconciliation** checks Alertmanager for alerts that fired while the service was down

## API

| Endpoint | Method | Description |
|---|---|---|
| `/webhook` | POST | Alertmanager webhook receiver |
| `/health` | GET | Health check |
| `/incidents` | GET | List recent incidents (query: `?limit=20`) |
| `/incidents/{id}` | GET | Get incident by ID |

## License

MIT
