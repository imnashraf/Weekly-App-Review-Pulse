import json

import pytest

from pulse.__main__ import main
from pulse.orchestrator import STAGES, StageError, is_done, run_all
from pulse.store import RunStore
from tests.test_insights import make_settings

MARKERS = {"ingest": "01_ingest_meta.json", "clean": "02_clean_meta.json",
           "themes": "04_label_stats.json", "rank": "05_analysis.json"}


@pytest.fixture
def env(tmp_path):
    s = make_settings(tmp_path)
    return s, RunStore(s.runs_dir, "2026-W41")


def fake_runners(store, calls, fail=None):
    """Runners that write each stage's marker file the way the real stages do."""
    def make(stage):
        def run():
            calls.append(stage)
            if stage == fail:
                raise RuntimeError(f"{stage} broke")
            if stage in MARKERS:
                store.write_json(MARKERS[stage], {})
            elif stage == "compose":
                store.write_text("pulse.md", "pulse")
                store.write_json("run_report.json", {"status": "passed"})
            elif stage == "publish":
                store.write_json("run_report.json", {"status": "passed", "publish": {
                    "doc": {"status": "done"}, "mail": {"status": "done"}}})
        return run
    return {s: make(s) for s in STAGES}


def test_fresh_run_executes_every_stage_in_order(env):
    s, store = env
    calls = []
    results = run_all(s, store, runners=fake_runners(store, calls))
    assert calls == list(STAGES) and all(r.status == "ran" for r in results)
    assert all(is_done(stage, store) for stage in STAGES)


def test_rerun_skips_finished_stages(env):
    s, store = env
    run_all(s, store, runners=fake_runners(store, []))
    calls = []
    results = run_all(s, store, runners=fake_runners(store, calls))
    assert calls == [] and all(r.status == "skipped" for r in results)


def test_failure_stops_there_and_resume_continues_from_it(env):
    s, store = env
    calls = []
    with pytest.raises(StageError, match="themes broke") as e:
        run_all(s, store, runners=fake_runners(store, calls, fail="themes"))
    assert e.value.stage == "themes" and calls == ["ingest", "clean", "themes"]
    assert is_done("clean", store) and not is_done("themes", store)
    resumed = []
    run_all(s, store, runners=fake_runners(store, resumed))
    assert resumed == ["themes", "rank", "compose", "publish"]      # ingest and clean not repeated


def test_start_from_reruns_that_stage_and_everything_after(env):
    s, store = env
    run_all(s, store, runners=fake_runners(store, []))
    calls = []
    run_all(s, store, start_from="rank", runners=fake_runners(store, calls))
    assert calls == ["rank", "compose", "publish"]


def test_unknown_start_stage_is_rejected(env):
    s, store = env
    with pytest.raises(ValueError, match="unknown stage"):
        run_all(s, store, start_from="deploy", runners=fake_runners(store, []))


def test_dry_run_always_runs_publish_but_skips_finished_work(env):
    s, store = env
    run_all(s, store, runners=fake_runners(store, []))
    calls = []
    run_all(s, store, dry_run=True, runners=fake_runners(store, calls))
    assert calls == ["publish"]


def test_publish_incomplete_when_mail_not_done(env):
    s, store = env
    store.write_json("run_report.json", {"status": "passed", "publish": {
        "doc": {"status": "done"}, "mail": {"status": "failed"}}})
    assert not is_done("publish", store)


def test_failed_compose_gate_is_not_done(env):
    s, store = env
    store.write_text("pulse.md", "x")
    store.write_json("run_report.json", {"status": "failed"})
    assert not is_done("compose", store)


def test_cli_run_reports_failure_with_resume_command(tmp_path, monkeypatch, capsys):
    import pulse.orchestrator as orch
    for v, val in (("LLM_API_KEY", "k"), ("PULSE_RECIPIENT", "a@b.co"),
                   ("MCP_ACCESS_TOKEN", "t"), ("PULSE_DOC_ID", "d")):
        monkeypatch.setenv(v, val)
    monkeypatch.setattr("pulse.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr("pulse.store.RunStore.__init__",
                        lambda self, runs_dir, run_id=None: (
                            setattr(self, "run_id", run_id or "2026-W41"),
                            setattr(self, "dir", tmp_path)) and None)

    def boom(*a, **k):
        raise StageError("ingest", "network down")
    monkeypatch.setattr(orch, "run_all", boom)
    assert main(["run", "--run", "2026-W41"]) == 1
    err = capsys.readouterr().err
    assert "stage 'ingest' failed: network down" in err
    assert "python -m pulse run --run 2026-W41" in err


def test_cli_run_requires_all_publish_settings(monkeypatch, capsys):
    for v in ("LLM_API_KEY", "PULSE_RECIPIENT", "MCP_ACCESS_TOKEN", "PULSE_DOC_ID"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr("pulse.config.load_dotenv", lambda *a, **k: None)
    assert main(["run"]) == 1
    err = capsys.readouterr().err
    assert all(v in err for v in ("LLM_API_KEY", "MCP_ACCESS_TOKEN", "PULSE_DOC_ID"))
