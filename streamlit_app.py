"""Streamlit front end for the Groww weekly review pulse.

Everyone can browse pulses. Running the pipeline or publishing (which sends a real email)
needs the ADMIN_PASSWORD secret.
"""
from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
SECRET_KEYS = ("LLM_API_KEY", "PULSE_RECIPIENT", "MCP_ACCESS_TOKEN", "PULSE_DOC_ID")


def load_secrets() -> None:
    """Streamlit Cloud keeps secrets in st.secrets; the pipeline reads environment variables."""
    try:
        for key in SECRET_KEYS:
            if key in st.secrets and not os.environ.get(key):
                os.environ[key] = str(st.secrets[key])
    except Exception:       # no secrets configured (local run with a .env)
        pass


def admin_password() -> str | None:
    try:
        return str(st.secrets["ADMIN_PASSWORD"])
    except Exception:
        return os.environ.get("ADMIN_PASSWORD")


def run_dirs() -> dict[str, Path]:
    """Run id -> folder holding pulse.md. Live runs win over the bundled sample."""
    found: dict[str, Path] = {}
    for base in (ROOT / "sample_runs", ROOT / "data" / "runs"):
        if base.is_dir():
            for d in base.iterdir():
                if (d / "pulse.md").is_file() and (d / "run_report.json").is_file():
                    found[d.name] = d
    return dict(sorted(found.items(), reverse=True))


def show_run(folder: Path) -> None:
    report = json.loads((folder / "run_report.json").read_text())
    st.markdown((folder / "pulse.md").read_text())

    st.divider()
    c = report["counts"]
    cols = st.columns(4)
    cols[0].metric("Fetched", f"{c['fetched']:,}" if c.get("fetched") else "n/a")
    cols[1].metric("After cleaning", f"{c['cleaned']:,}" if c.get("cleaned") else "n/a")
    cols[2].metric("Words in pulse", report["pulse"]["words"] or "n/a")
    cols[3].metric("LLM calls", report["llm"]["calls"])

    st.subheader("All themes")
    st.dataframe(
        [{"Rank": t["rank"], "Theme": t["name"], "Reviews": t["volume"],
          "Share": f"{t['share']:.0%}", "Negative": f"{t['neg_share']:.0%}",
          "Trend": f"{t['trend']:+.1%}", "In pulse": t["theme_id"] in report["themes"]["in_pulse"]}
         for t in report["themes"]["ranking"]],
        hide_index=True, width="stretch")

    st.subheader("Validation gates")
    for g in report["gates"]:
        st.write(f"{'✅' if g['passed'] else '❌'} **{g['name']}** {g.get('detail', '')}")
    for w in report.get("warnings", []):
        st.warning(w)
    if report.get("doc_url"):
        st.link_button("Open the Google Doc", report["doc_url"])


def pipeline(args: list[str], run_id: str) -> None:
    """Start `python -m pulse ...` in the background, logging to the run folder."""
    log_dir = ROOT / "data" / "runs" / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "ui_job.log").open("w")
    st.session_state["job"] = subprocess.Popen(
        [sys.executable, "-m", "pulse", *args, "--run", run_id],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy())
    st.session_state["job_log"] = log_dir / "ui_job.log"
    st.session_state["job_name"] = " ".join(args)


def admin_panel() -> None:
    st.subheader("Run the pipeline")
    password = admin_password()
    if not password:
        st.info("Admin actions are disabled: set the ADMIN_PASSWORD secret to enable them.")
        return
    entered = st.text_input("Admin password", type="password")
    if not entered or not hmac.compare_digest(entered, password):
        if entered:
            st.error("Wrong password.")
        return

    from pulse.store import current_run_id
    run_id = st.text_input("Run id", current_run_id(), help="ISO week, e.g. 2026-W41")
    missing = [k for k in SECRET_KEYS if not os.environ.get(k)]
    if missing:
        st.warning(f"Missing settings: {', '.join(missing)}")

    job = st.session_state.get("job")
    busy = job is not None and job.poll() is None
    a, b, c, d = st.columns(4)
    if a.button("Ingest", disabled=busy):
        pipeline(["ingest"], run_id); st.rerun()
    if b.button("Analyze (about 15 min)", disabled=busy):
        pipeline(["analyze"], run_id); st.rerun()
    if c.button("Dry-run publish", disabled=busy):
        pipeline(["publish", "--dry-run"], run_id); st.rerun()
    confirm = d.checkbox("I want to send the email")
    if d.button("Publish", disabled=busy or not confirm, type="primary"):
        pipeline(["publish"], run_id); st.rerun()

    if job is not None:
        state = "running" if busy else f"finished (exit code {job.returncode})"
        st.write(f"Job `{st.session_state['job_name']}`: **{state}**")
        lines = st.session_state["job_log"].read_text().splitlines()
        shown = [l for l in lines if '"level": "INFO"' not in l][-15:]
        st.code("\n".join(shown) or "(no output yet)")
        st.button("Refresh")


def main() -> None:
    st.set_page_config(page_title="Groww Review Pulse", page_icon="📊", layout="centered")
    load_secrets()
    st.title("Groww Review Pulse")
    runs = run_dirs()
    tab_view, tab_admin = st.tabs(["Latest pulse", "Run & publish"])
    with tab_view:
        if not runs:
            st.info("No pulses yet. Run the pipeline from the next tab.")
        else:
            run_id = st.selectbox("Week", list(runs))
            show_run(runs[run_id])
    with tab_admin:
        admin_panel()


main()
