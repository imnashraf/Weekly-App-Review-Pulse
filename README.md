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
Each stage writes its output to `data/runs/<YYYY-Www>/` (git-ignored).

| Stage | Output file |
|---|---|
| ingest | `01_raw_reviews.jsonl`, `01_ingest_meta.json` |
| clean | `02_clean_reviews.jsonl`, `02_clean_meta.json` |
| themes | `03_themes.json`, `04_labeled_reviews.jsonl`, `04_label_stats.json` |
| rank | `05_analysis.json` (top 3 themes, quotes, actions) |
| compose | `pulse.md`, `run_report.json` (gate results, counts, token usage) |
| publish | doc append + email; state saved in `run_report.json` |

## Setup
Requires Python 3.11 or newer.

    git clone https://github.com/imnashraf/Weekly-App-Review-Pulse.git
    cd Weekly-App-Review-Pulse
    python -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev]"
    cp .env.example .env        # then fill it in
    pytest                      # 180+ tests, no network needed

### Environment variables (`.env`)
| Variable | What it is |
|---|---|
| `LLM_API_KEY` | API key for the provider set in `config.yaml` (`llm.provider`: gemini, anthropic or openai) |
| `PULSE_RECIPIENT` | The one address the pulse is emailed to (plain `name@example.com`) |
| `MCP_ACCESS_TOKEN` | Bearer token for the MCP server |
| `PULSE_DOC_ID` | URL or id of an existing Google Doc; each week's pulse is appended to it |

Never commit `.env`. Model, review window, limits and the MCP server URL are in `config.yaml`
(set `llm.model` to a model your key can use).

### MCP server
Docs and Gmail go through one MCP server over Streamable HTTP
([imnashraf/MCP-Server](https://github.com/imnashraf/MCP-Server)). The pipeline calls only three of its tools,
mapped under `mcp.tools` in `config.yaml`:

| Key | Tool | Used for |
|---|---|---|
| `status` | `google_workspace_status` | startup check: tools present, Google authorized |
| `docs_append` | `google_docs_append_text` | append the weekly section to the doc |
| `gmail_send` | `gmail_send_email` | send the pulse to `PULSE_RECIPIENT` |

The server has no draft tool, so the pulse is emailed rather than drafted. It never retries a write, so
re-running `publish` is safe: each step uses an idempotency key and completed steps are skipped. Set
`GMAIL_ALLOWED_RECIPIENTS` on the server to your `PULSE_RECIPIENT`.

## Commands
    python -m pulse run                 # ingest → clean → themes → rank → compose → publish
    python -m pulse run --dry-run       # everything except the doc append and the email
    python -m pulse run --from compose  # re-run one stage and all later ones
    python -m pulse run --run 2026-W41  # a specific ISO week (default: the current one)

Single stages: `ingest`, `clean`, `themes`, `rank`, `compose`, `analyze` (clean → compose) and
`publish [--dry-run] [--retry-unknown]`. Finished stages are skipped, so a failed run resumes where it
stopped and never publishes twice. If a publish step ends with an unknown outcome (for example a timeout),
check the doc or your Sent folder first, then re-run with `--retry-unknown`.

## Weekly schedule
`.github/workflows/weekly-pulse.yml` runs every Monday at 03:30 UTC (09:00 IST). Add the four variables
above as repository secrets (Settings → Secrets and variables → Actions). Manual runs from the Actions tab
default to a dry run.

## Streamlit app
`streamlit_app.py` shows the latest pulse for everyone. The Run & publish tab needs an `ADMIN_PASSWORD`
secret. To deploy: create the app on share.streamlit.io from this repo, main file `streamlit_app.py`, and
paste the secrets (the four variables plus `ADMIN_PASSWORD`). Its disk is not persistent, so use the
scheduled workflow for real weekly runs.

## Adding another source (e.g. the App Store)
Sources are adapters (`pulse/ingest/base.py`). To add one:
1. Create `pulse/ingest/app_store.py` with a class that has a `source` name and
   `fetch(window_start, max_reviews)` yielding `RawReview` dicts (`id`, `source`, `rating`, `text`,
   `date`, `app_version`), newest first. Drop reviewer names and hash the review id inside the adapter.
2. Make `run_ingest` (`pulse/ingest/__init__.py`) use it, or merge several adapters' output into
   `01_raw_reviews.jsonl`. Everything after ingest only reads that file.
3. Add tests with recorded fixtures, as in `tests/test_ingest.py`.

## Privacy and operations
See [docs/operations.md](docs/operations.md) for the privacy checklist, token and cost numbers, and rate
limits. Design and requirements: [docs/architecture.md](docs/architecture.md),
[docs/implementation-plan.md](docs/implementation-plan.md).

## Status
All phases (0–8) are implemented.
