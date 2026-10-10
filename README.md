# Groww Weekly App Review Pulse

Turns a week of public Google Play reviews for the Groww app into a one-page
summary a team can scan in minutes: what users care about, what they actually
said, and what to do next.

**Live app:** https://weekly-app-review-pulse-zvhr3mv7zz7kk87mavs9fh.streamlit.app/

## What it produces
- Top 3 themes (chosen from at most 5), with each theme's share of reviews
- 3 verbatim, anonymous user quotes
- 3 action ideas, each tagged Product / Eng / Support
- A pulse of 250 words or fewer, appended to a Google Doc and emailed to you

## How it works
Ingest → clean and remove PII → discover themes (LLM) → rank → pick quotes and
actions → compose → validation gates → publish via MCP.

Every quote is checked to be an exact excerpt of a real review, and the final
text is re-scanned for personal data. If any gate fails, nothing is published.

## Quick start
    pip install -e ".[dev]"
    cp .env.example .env        # fill in the keys
    python -m pulse analyze     # clean, theme, rank, compose
    python -m pulse publish --dry-run
    python -m pulse publish

## Configuration
`LLM_API_KEY`, `PULSE_RECIPIENT`, `MCP_ACCESS_TOKEN` and `PULSE_DOC_ID` go in `.env`.
Model, window and limits are in `config.yaml`.

## Status
Phases 0–6 are done. Orchestration and scheduling are next. See
[docs/implementation-plan.md](docs/implementation-plan.md).
