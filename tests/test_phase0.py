import json
import logging
from datetime import date

import pytest

from pulse.__main__ import main
from pulse.config import ConfigError, load_config
from pulse.logging_setup import JsonFormatter
from pulse.store import RunStore, current_run_id

MINIMAL = """
app: {google_play_id: com.example.app}
llm: {model: test-model}
mcp:
  docs: {command: docs-server}
  gmail: {command: gmail-server}
delivery: {recipient: "${PULSE_RECIPIENT}"}
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for var in ("LLM_API_KEY", "PULSE_RECIPIENT"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL)
    return path


def test_config_loads_with_defaults(cfg):
    s = load_config(cfg)
    assert s.app.google_play_id == "com.example.app"
    assert s.window.weeks == 12 and s.limits.max_themes == 5
    assert s.delivery.recipient is None


def test_env_substitution_and_key(cfg, monkeypatch):
    monkeypatch.setenv("PULSE_RECIPIENT", "me@example.com")
    monkeypatch.setenv("LLM_API_KEY", "k")
    s = load_config(cfg)
    assert s.delivery.recipient == "me@example.com" and s.llm_api_key == "k"


def test_missing_required_env_fails_clearly(cfg):
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        load_config(cfg, require_env=("LLM_API_KEY",))


def test_invalid_config_fails_clearly(cfg):
    cfg.write_text(MINIMAL.replace("llm: {model: test-model}", "llm: {}"))
    with pytest.raises(ConfigError, match="llm.model"):
        load_config(cfg)


def test_window_bounds_enforced(cfg):
    cfg.write_text(MINIMAL + "window: {weeks: 3}\n")
    with pytest.raises(ConfigError, match="window.weeks"):
        load_config(cfg)


def test_project_config_loads():
    assert load_config().app.google_play_id == "com.nextbillion.groww"


def test_run_id_is_iso_week():
    assert current_run_id(date(2026, 10, 9)) == "2026-W41"


def test_run_store_roundtrip(tmp_path):
    store = RunStore(tmp_path, "2026-W41")
    assert store.dir.is_dir()
    store.write_json("a.json", {"x": 1})
    store.write_jsonl("b.jsonl", [{"i": 1}, {"i": 2}])
    store.write_text("pulse.md", "hi")
    assert store.read_json("a.json") == {"x": 1}
    assert list(store.read_jsonl("b.jsonl")) == [{"i": 1}, {"i": 2}]
    assert store.read_text("pulse.md") == "hi" and store.exists("pulse.md")


def test_run_store_rejects_bad_id(tmp_path):
    with pytest.raises(ValueError):
        RunStore(tmp_path, "../etc")


def test_json_log_format():
    rec = logging.LogRecord("pulse", logging.INFO, "f", 1, "hello", None, None)
    rec.stage = "ingest"
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "hello" and out["stage"] == "ingest" and out["level"] == "INFO"


def test_cli_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert "ingest" in capsys.readouterr().out


def test_cli_missing_env_errors(cfg, capsys):
    assert main(["--config", str(cfg), "analyze"]) == 1
    assert "LLM_API_KEY" in capsys.readouterr().err
