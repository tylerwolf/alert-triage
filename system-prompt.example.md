## Environment

Describe your infrastructure here so the AI diagnostician has context about your setup.

Services running (Docker):
- Traefik (reverse proxy, port 443/80)
- Your services here...
- Prometheus (port 9090), Grafana (port 3000), Alertmanager (port 9093)
- Loki (port 3100), Promtail, Node Exporter, cAdvisor, Blackbox Exporter

Network details:
- Server IP: 192.168.x.x
- NAS mounts: /mnt/nas/data (nas-hostname, 192.168.x.x)

## Alert Types

Describe the alerts configured in your Prometheus rules:

- ServiceDown: Blackbox probe failing. Check if the container is running, then check its logs.
- HighCPU: CPU above 85% for 5+ min. Identify top consumers via container metrics.
- HighMemory: Memory above 90% for 5+ min. Identify top consumers.
- Add your custom alerts here...
