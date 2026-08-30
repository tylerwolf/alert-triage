"""Tests for alert exclusion partitioning (filtering.py)."""

from alert_triage.filtering import split_excluded

from .helpers import make_alert


def test_split_excluded_partitions():
    watchdog = make_alert("Watchdog", fingerprint="fp-wd")
    real = make_alert("ServiceDown", fingerprint="fp-sd")
    kept, dropped = split_excluded([watchdog, real], frozenset({"Watchdog"}))
    assert kept == [real]
    assert dropped == [watchdog]


def test_split_excluded_exact_case_sensitive():
    alert = make_alert("watchdog")
    kept, dropped = split_excluded([alert], frozenset({"Watchdog"}))
    assert kept == [alert]
    assert dropped == []


def test_split_excluded_missing_labels_kept():
    kept, dropped = split_excluded([{}], frozenset({"Watchdog"}))
    assert kept == [{}]
    assert dropped == []


def test_split_excluded_empty_exclusions_keeps_all():
    alerts = [make_alert("Watchdog")]
    kept, dropped = split_excluded(alerts, frozenset())
    assert kept == alerts
    assert dropped == []
