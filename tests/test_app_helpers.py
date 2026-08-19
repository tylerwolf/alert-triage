"""Tests for pure helper functions in app.py."""

from datetime import UTC, datetime, timedelta

import pytest

from alert_triage.app import _alertname, _format_duration, _incident_age

from .helpers import make_alert


class TestAlertname:
    def test_dedupes_and_sorts(self):
        alerts = [
            make_alert("ServiceDown"),
            make_alert("DNSDown"),
            make_alert("DNSDown"),
        ]
        assert _alertname(alerts) == "DNSDown+ServiceDown"

    def test_empty_list_is_unknown(self):
        assert _alertname([]) == "unknown"

    def test_missing_labels_is_unknown(self):
        assert _alertname([{"labels": {}}]) == "unknown"
        assert _alertname([{}]) == "unknown"


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (59, "0m"),
            (60, "1m"),
            (3599, "59m"),
            (3600, "1h 0m"),
            (7260, "2h 1m"),
        ],
    )
    def test_formats(self, seconds, expected):
        assert _format_duration(seconds) == expected


class TestIncidentAge:
    def test_age_from_timestamp(self):
        started = datetime.now(UTC) - timedelta(seconds=120)
        age = _incident_age({"timestamp": started.isoformat()})
        assert 119 <= age <= 125
