"""Tests for Settings and system prompt building (config.py)."""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

import alert_triage
from alert_triage.config import BASE_SYSTEM_PROMPT, Settings, build_system_prompt


def test_reads_prefixed_env_vars(monkeypatch):
    monkeypatch.setenv("ALERT_TRIAGE_MODEL", "test-model-x")
    monkeypatch.setenv("ALERT_TRIAGE_COALESCE_WINDOW", "7")
    settings = Settings()
    assert settings.model == "test-model-x"
    assert settings.coalesce_window == 7


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(ValidationError):
        Settings()


def test_build_system_prompt_without_file():
    assert build_system_prompt(Settings()) == BASE_SYSTEM_PROMPT


def test_build_system_prompt_appends_file(tmp_path):
    prompt_file = tmp_path / "env.md"
    prompt_file.write_text("## My Environment\n")
    settings = Settings(system_prompt_file=prompt_file)
    result = build_system_prompt(settings)
    assert result == BASE_SYSTEM_PROMPT + "\n\n## My Environment"


def test_build_system_prompt_missing_file_uses_base(tmp_path):
    settings = Settings(system_prompt_file=tmp_path / "nope.md")
    assert build_system_prompt(settings) == BASE_SYSTEM_PROMPT


def test_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert alert_triage.__version__ == version
