"""Alert exclusion filtering shared by the webhook, reconciliation, and delta paths."""


def split_excluded(
    alerts: list[dict], excluded: frozenset[str]
) -> tuple[list[dict], list[dict]]:
    """Partition alerts into (kept, dropped) by exact alertname match."""
    kept: list[dict] = []
    dropped: list[dict] = []
    for alert in alerts:
        alertname = alert.get("labels", {}).get("alertname")
        if alertname in excluded:
            dropped.append(alert)
        else:
            kept.append(alert)
    return kept, dropped
