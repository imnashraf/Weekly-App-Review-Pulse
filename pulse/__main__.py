"""CLI: `python -m pulse run | ingest | analyze | publish`."""
from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_config
from .logging_setup import setup_logging
from .store import RunStore

log = logging.getLogger("pulse")

# command -> (env vars it needs, phase that implements it)
COMMANDS: dict[str, tuple[tuple[str, ...], int, str]] = {
    "run": (("LLM_API_KEY", "PULSE_RECIPIENT"), 7, "Run the whole pipeline end to end"),
    "ingest": ((), 1, "Fetch and store raw Google Play reviews"),
    "clean": ((), 2, "Normalize, scrub PII, filter and de-duplicate raw reviews"),
    "themes": (("LLM_API_KEY",), 3, "Discover themes and label every cleaned review"),
    "rank": (("LLM_API_KEY",), 4, "Rank themes, pick top-3 quotes and actions"),
    "compose": (("LLM_API_KEY",), 5, "Compose pulse.md and run_report.json from the analysis"),
    "analyze": (("LLM_API_KEY",), 5, "Clean, theme, rank and compose the pulse"),
    "publish": (("PULSE_RECIPIENT", "MCP_ACCESS_TOKEN", "PULSE_DOC_ID"), 6, "Publish a composed pulse to Docs and Gmail via MCP"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse", description="Groww weekly app review pulse"
    )
    parser.add_argument("--config", help="path to config.yaml (default: project config.yaml)")
    parser.add_argument("--log-level", default="INFO", help="logging level (default: INFO)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{run,ingest,clean,themes,rank,compose,analyze,publish}")
    for name, (_, _, help_text) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("--run", dest="run_id", metavar="RUN_ID",
                       help="run id such as 2026-W41 (default: current ISO week)")
        p.add_argument("--dry-run", action="store_true",
                       help="render the pulse without calling MCP servers")
        if name == "publish":
            p.add_argument("--retry-unknown", action="store_true",
                           help="retry a step whose earlier outcome was unknown (check Google first)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    require_env, phase, _ = COMMANDS[args.command]
    try:
        settings = load_config(args.config, require_env=() if args.dry_run else require_env)
        store = RunStore(settings.runs_dir, args.run_id)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.command == "ingest":
        from .ingest import run_ingest

        try:
            meta = run_ingest(settings, store)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Fetched {meta['review_count']} reviews "
              f"({meta['oldest_review']} to {meta['newest_review']}) -> "
              f"{store.path('01_raw_reviews.jsonl')}")
        return 0

    if args.command == "clean":
        from .clean import CLEAN_FILE, run_clean

        try:
            stats = run_clean(settings, store)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Kept {stats['output_count']} of {stats['input_count']} reviews "
              f"(dropped {stats['dropped']}) -> {store.path(CLEAN_FILE)}")
        return 0

    if args.command == "themes":
        from .analyze.themes import LABELED_FILE, run_themes
        from .llm import LLMError

        try:
            stats = run_themes(settings, store)
        except (RuntimeError, LLMError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        shares = ", ".join(f"{t['name']} {t['share']:.0%}" for t in stats["themes"].values())
        print(f"Labeled {stats['total']} reviews: {shares}, other {stats['other']['share']:.0%} "
              f"-> {store.path(LABELED_FILE)}")
        for warning in stats["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        return 0

    if args.command == "rank":
        from .analyze.insights import ANALYSIS_FILE, run_insights
        from .llm import LLMError

        try:
            analysis = run_insights(settings, store)
        except (RuntimeError, LLMError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        top = ", ".join(f"{t['name']} ({t['volume']})" for t in analysis["top_themes"])
        print(f"Top themes: {top}; {len(analysis['quotes'])} quotes, "
              f"{len(analysis['actions'])} actions -> {store.path(ANALYSIS_FILE)}")
        for warning in analysis["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        return 0

    if args.command == "publish":
        from .integrations.publish import PublishError, run_publish

        try:
            state = run_publish(settings, store, dry_run=args.dry_run,
                                retry_unknown=args.retry_unknown)
        except PublishError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(f"resume with: python -m pulse publish --run {store.run_id}", file=sys.stderr)
            return 1
        if args.dry_run:
            print(f"Dry run: wrote {state['doc_text']} and {state['email']} (MCP: {state['mcp_status']})")
        else:
            print(f"Published run {store.run_id}: doc {state['doc']['status']}, "
                  f"email {state['mail']['status']} -> {store.path('run_report.json')}")
        return 0

    if args.command in ("compose", "analyze"):
        from .compose.composer import PULSE_FILE, REPORT_FILE, run_compose
        from .llm import LLMError
        from .validate.gates import GateError

        try:
            if args.command == "analyze":
                from .analyze.insights import run_insights
                from .analyze.themes import run_themes
                from .clean import run_clean

                run_clean(settings, store)
                run_themes(settings, store)
                run_insights(settings, store)
            report = run_compose(settings, store)
        except GateError as exc:
            print(f"error: validation failed, nothing published: {exc}", file=sys.stderr)
            print(f"details: {store.path(REPORT_FILE)}", file=sys.stderr)
            return 1
        except (RuntimeError, LLMError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Pulse of {report['pulse']['words']} words, all gates passed -> {store.path(PULSE_FILE)}")
        for warning in report["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        return 0

    log.info("command not implemented yet", extra={
        "command": args.command, "run_id": store.run_id,
        "dry_run": args.dry_run, "implemented_in_phase": phase,
    })
    return 2


if __name__ == "__main__":
    sys.exit(main())
