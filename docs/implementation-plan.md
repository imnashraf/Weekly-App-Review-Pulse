# Implementation Plan: Groww Weekly App Review Pulse

> Requirements: [problemStatement.md](./problemStatement.md) · Design: [architecture.md](./architecture.md)
> Scope: Google Play reviews only (Apple App Store deferred).

Each phase ends with something runnable and a clear exit check, so progress can be verified before moving on. Requirement IDs (R1–R10) refer to the traceability table in the architecture doc.

---

## Phase Overview

| Phase | Goal | Output | Reqs |
|---|---|---|---|
| 0 | Project setup | Skeleton, config, CLI stub, test harness | – |
| 1 | Ingestion | Raw Play reviews for the window | R1, R2 |
| 2 | Cleaning & PII | Scrubbed, de-duplicated reviews | R8 |
| 3 | LLM client & theming | ≤5 themes, every review labeled | R3 |
| 4 | Ranking, quotes, actions | Top 3 themes, 3 verbatim quotes, 3 actions | R4–R6 |
| 5 | Composition & gates | `pulse.md` + `run_report.json`, validated | R7, R8 |
| 6 | MCP integration | Pulse appended to a Google Doc + email sent to self via our MCP server | R9, R10 |
| 7 | Orchestration & scheduling | One-command and weekly runs, resume | – |
| 8 | Hardening & docs | E2E tests, error handling, README | – |

---

## Phase 0 — Project Setup

**Tasks**
- Create `pyproject.toml` (Python 3.11+), with dependencies: `google-play-scraper`, `pydantic`, `pyyaml`, `python-dotenv`, `jinja2`, `mcp`, `pytest`, plus the chosen LLM SDK.
- Create the layout from architecture §7: `pulse/` packages, `tests/`, `data/runs/` (git-ignored), `config.yaml`, `.env.example`.
- Implement `config.py` (loads `.env` + `config.yaml` into a pydantic settings model).
- Implement `__main__.py` CLI stub with subcommands `run | ingest | clean | themes | analyze | publish`, plus `--dry-run` and `--run <id>`.
- Implement run-store helper: creates `data/runs/<YYYY-WW>/`, reads/writes stage artifacts (JSONL/JSON).
- Set up structured JSON logging.

**Exit check:** `python -m pulse --help` works; `pytest` runs (even with a placeholder test); config loads and fails clearly when a required value is missing.

---

## Phase 1 — Ingestion (Google Play)

**Tasks**
- `ingest/google_play.py`: page through `reviews()` sorted `NEWEST` for `com.nextbillion.groww` (`lang=en`, `country=in`) using continuation tokens; stop when `at < window_start`.
- Window logic: `window_start = run_date − weeks` (default 12, min 8).
- Polite delay between pages, exponential backoff (max 5 tries) on errors, and `MAX_REVIEWS_PER_SOURCE` cap.
- Drop author name, user image and review IDs of reviewers inside the adapter; keep only a hashed review ID, rating, text, date, app version.
- Define a `SourceAdapter` interface so App Store can be added later.
- Write `01_raw_reviews.jsonl`.

**Tests:** recorded fixtures; window cut-off; paging stop; cap; assert no author fields in output.

**Exit check:** `python -m pulse ingest` produces a JSONL file with reviews inside the window only.

---

## Phase 2 — Normalize, PII Scrub, De-duplicate

**Tasks**
- `clean/normalize.py`: map to the normalized schema (id, source, rating, title=null, text, date, app_version, ISO week).
- `clean/pii.py`: regex scrubber for email, phone (+91/10-digit), PAN, Aadhaar-like, long digit runs, UPI handles, URLs, @mentions (architecture §5.2).
- `clean/dedupe.py`: normalized-text hash de-duplication.
- Filters: keep only reviews with **more than 8 words** (`limits.min_review_words: 9`; shorter and empty ones are counted, not kept), drop reviews containing emoji, and keep English only: other scripts (e.g. Devanagari) and romanized Hindi/Hinglish are counted and excluded.
- Write `02_clean_reviews.jsonl` and drop counts.

**Tests:** table-driven PII cases (including false positives such as ratings or version numbers); dedupe; filter counts.

**Exit check:** no scrubber pattern matches any text in the cleaned file; drop counts are reported.

---

## Phase 3 — LLM Client & Theme Discovery

**What the cleaned data looks like** (`02_clean_reviews.jsonl`, run 2026-W41; this shapes the design below)

| Fact | Value | Consequence |
|---|---|---|
| Size | 1,588 reviews, 13 ISO weeks (W29–W41), ~245k chars ≈ **61k tokens** in total | Small enough that cost is trivial and the whole set could even fit in one prompt. A ~300 sample for discovery is still plenty. |
| Length | median 19 words, p90 61, max 101 | Short texts, no truncation needed. 50 per batch ≈ 2–3k tokens. |
| Ratings | 1★ 664 (42%) · 2★ 96 · 3★ 140 · 4★ 150 · 5★ 538 (34%) | Heavily polarized. A proportional sample would be ~76% 1★/5★ and starve 2–4★, so sample by rating quota, not proportionally. |
| Weekly volume | 37 → 206 per week; W29 and W41 are **partial weeks** | Sampling must spread across weeks, but a week can have as few as 1 review per rating, so strata are filled best-effort. |
| Language | English only (Hinglish, emoji and other scripts already removed) | Prompts need no Hinglish handling. |
| Topics (rough keyword scan) | performance/orders ~22% · UI/feature requests ~21% · support ~19% · F&O/options ~17% · charges ~13% · MF/SIP ~13% · payments ~5% · withdrawals ~4% · statements/tax ~4% · KYC ~3% · IPO ~2% | The architecture seed list is too fine-grained: KYC, withdrawals, payments and statements are each only 3–5%. With a hard cap of 5 themes they must be merged. F&O/trading, MF/SIP and feature requests are missing from the seed list. |
| Sentiment | ~23% of reviews are plain praise (only ~9% of those are 1–2★); problem topics run 60–78% negative | Praise should land in `other`, not use up one of the 5 theme slots. |

**Tasks**
- `llm/client.py`: provider-agnostic `LLMClient` with JSON-mode calls, schema validation (pydantic), retry that feeds the validation error back (max 2), temperature 0 for both passes, token and cost logging per call and per run. Provider and model stay config-driven (open item 2). Gemini, OpenAI and Anthropic adapters call the provider's HTTPS API directly, so no vendor SDK is needed; `llm.model` and `LLM_API_KEY` must be set.
- `llm/prompts/`: versioned prompt files (`discovery_v1`, `classification_v1`); the prompt version is recorded in `03_themes.json` and the run report.
- Config: add `analysis.sample_size: 300`, `analysis.batch_size: 50`, `analysis.max_other_share: 0.40`.
- `analyze/sample.py` (deterministic, seeded): builds the discovery sample.
  - Rating quotas instead of proportional: about 90 × 1★, 60 × 2★, 50 × 3★, 50 × 4★, 50 × 5★. A rating with fewer reviews than its quota gives all of them, and the shortfall goes to the largest ratings.
  - Within a rating, round-robin across weeks so no single week dominates.
- `analyze/themes.py`:
  - **Pass A, discovery:** one call on the sample, returning **1–5** themes, each `{id: T1.., name, description, include_cues}`.
    - The seed list is rewritten for this data and is guidance only: *app performance & order execution · customer support · charges & brokerage · F&O / trading tools (charts, margin, options) · mutual funds & SIP · money in/out (KYC, deposits, withdrawals, payments) · statements & tax reports · feature requests / UX.* The model may merge or replace them to stay within 5.
    - The prompt says problem areas come first and plain praise is not a theme. Praise and one-off comments go to `other`.
    - Optional: if the previous week's `03_themes.json` exists, pass it as "prior themes" so names stay stable week to week. The model may keep, rename or drop them.
  - **Pass B, classification:** batches of 50 (≈32 calls for 1,588 reviews).
    - Each review is sent as a short batch index (`1..50`) with its rating and text, not the 16-char hash id. This saves tokens and avoids id copy errors.
    - Each review gets exactly one `theme_id` (its main complaint or request when it mentions several topics) or `other`, plus `sentiment` (`neg | neu | pos`).
    - Code check per batch: every index appears exactly once, and `theme_id` is a known theme or `other`. Missing or invalid items are re-asked once in a smaller batch. Anything still unresolved becomes `other` and is counted in the stats.
- Outputs:
  - `03_themes.json`: themes, prompt version, sample size and sampled review ids.
  - `04_labeled_reviews.jsonl`: every cleaned review plus `theme_id` and `sentiment`.
  - `04_label_stats.json`: reviews per theme, `other` share, label-fill failures, LLM calls and tokens, and **rating vs sentiment disagreements** (e.g. 1★ labelled `pos`) as a quality signal.
- CLI: new subcommand `python -m pulse themes [--run <id>]`, like `clean`. It needs `LLM_API_KEY`. `analyze` (Phase 5) later chains clean → themes → … → compose.
- Warnings, not failures: `other` share above `max_other_share` (a sign the themes miss a big topic), or any theme with fewer than ~3% of reviews (a sign it should merge with another).

**Budget per run** (estimate for ~1.6k reviews): Pass A ≈ 12k tokens in, Pass B ≈ 32 calls × ~3k in ≈ 95k in, and about 20k out. That is about **100–130k tokens in total**, so any mid-tier model fits a small per-run budget.

**Tests**
- Sampler: quotas per rating, week spread, deterministic with a fixed seed, correct when a rating is short of its quota.
- Fixture: ~60 real cleaned reviews in `tests/fixtures/` (already PII-free), with recorded LLM responses for both passes.
- Schema rejects more than 5 themes, an empty list, duplicate theme ids and unknown `theme_id`s.
- Invalid JSON triggers the retry path and succeeds on the second try; a third failure raises a clear error.
- Classification: missing and duplicate indices are re-asked; unresolved ones become `other` and are counted.
- Stats: `other` share and disagreement counts are computed correctly; the warning fires above the threshold.

**Exit check:** `python -m pulse themes` on the W41 data gives every one of the 1,588 cleaned reviews a label, 5 or fewer themes, an `other` share at or below 40% (or an explicit warning), and a spot check of ~20 random labelled reviews looks right to a human.

---

## Phase 4 — Ranking, Quotes, Actions

**Tasks**
- `analyze/rank.py` (deterministic): `volume`, `neg_share`, `trend`; `score = volume × (0.5 + neg_share)` plus a small rising-trend bonus; pick top 3.
- `analyze/quotes.py`: send ~30 candidate reviews per top theme; LLM returns `{review_id, quote}`; the verbatim validator checks the normalized quote is a substring of that review (retry twice, then fall back to the best candidate's first sentence); cap ~25 words; no attribution.
- `analyze/actions.py`: exactly 3 actions with `owner_hint` (Product / Eng / Support) and `theme_id`, each tied to a top-3 theme.
- Write `05_analysis.json`.

**Tests:** ranking snapshot on fixtures; verbatim check (exact, whitespace-normalized, reworded rejected); action-theme linkage.

**Exit check:** analysis file holds 3 themes, 3 quotes that exist verbatim in the source, 3 actions.

---

## Phase 5 — Composition & Validation Gates

**Tasks**
- `compose/template.md.j2` and `composer.py`: fixed template; the LLM only fills short slots (theme one-liners).
- `validate/gates.py`: schema, theme cap (≤5, exactly 3 in the pulse), verbatim, word count ≤250 (LLM shorten, then deterministic truncation), final PII re-scan, counts (3 quotes, 3 actions).
- Low-sample warning when fewer than 50 reviews.
- Write `pulse.md` and `run_report.json` (window, counts per stage, dropped counts, all themes with stats, gate results, prompt version, token and cost).

**Tests:** each gate has a pass and a fail case; PII failure blocks output; snapshot of the composed pulse.

**Exit check:** `python -m pulse analyze` yields a valid `pulse.md` ≤250 words with all gates green.

---

## Phase 6 — MCP Integration (Docs + Gmail)

**The MCP server** (ours: [imnashraf/MCP-Server](https://github.com/imnashraf/MCP-Server), deployed at `https://mcp-server-production-d658.up.railway.app`). One server covers both Docs and Gmail, for a single authorized Google account. It exposes three tools:

| Tool | Writes? | Required args | Useful optional args |
|---|---|---|---|
| `google_workspace_status` | No | – | – (diagnostics: capabilities and authorization state, no secrets) |
| `google_docs_append_text` | Yes | `document` (docs.google.com/document/d/<id> URL or raw ID), `text` (≤ 100,000 chars) | `prepend_newline`, `append_newline` (both default true), `idempotency_key` (8–128 chars) |
| `gmail_send_email` | Yes, **sends real mail** | `to`, `subject` (≤ 998 chars), `body_text` | `cc`, `bcc`, `body_html` (only if the server sets `GMAIL_ENABLE_HTML_BODY=true`), `reply_to`, `confirm_send`, `idempotency_key` |

How it behaves, and what that means for the pipeline:
- **Transport:** Streamable HTTP at `<host>/mcp`, stateless JSON, with `Authorization: Bearer <MCP_ACCESS_TOKEN>` on every request (`GET /healthz` needs no auth). So the client uses an HTTP transport, not stdio. The bearer token is the only secret the pipeline holds, and it never touches Google credentials.
- **No draft tool.** The server can only send. The earlier "draft only" design is replaced by sending one email to `PULSE_RECIPIENT` (our own address or alias). Safety shifts to the server's recipient allowlist, `confirm_send`, idempotency keys and `--dry-run` (see tasks).
- **No create, find or clear for docs.** The doc must already exist, and the tool only **appends** (never replaces). So we use one running doc with a new section appended each week, and the pipeline needs the doc URL or ID as config. This settles open item 4.
- **Results are JSON** (also in `structuredContent`) with `success`, `operation`, `request_id`, and `message_id` / `thread_id` for mail. Failures set `isError` and return `error.code` plus `error.outcome` (`not_performed` | `performed` | `unknown`).
- **Writes are never retried by the server.** A timeout or 5xx gives `OUTCOME_UNKNOWN`; the client must not blindly retry. The same `idempotency_key` with the same input returns the earlier result without a second write; the same key with different input is a `VALIDATION_ERROR`; a key whose earlier attempt ended unknown is blocked until a new key is used. The store is a file on a single node, so the server must stay at one replica.
- Plain text only by default: `body_text` is always required. HTML is sent only if the server allows it.

**Tasks**
- Config and env: replace the stdio `mcp.docs/gmail.command` entries in `config.yaml` with one `mcp` block: `url` (the Railway `/mcp` endpoint), `tools: { status: google_workspace_status, docs_append: google_docs_append_text, gmail_send: gmail_send_email }`, and `docs.document` (the target doc URL or ID, from `PULSE_DOC_ID`). Add `MCP_ACCESS_TOKEN` to `.env.example` and to the env vars `publish` requires, next to `PULSE_RECIPIENT`.
- `integrations/mcp_client.py`: a Streamable HTTP session using the `mcp` SDK, with the bearer header. At startup run `list_tools()` and fail fast if a mapped tool is missing or `docs_append`/`gmail_send` is disabled on the server, then call `google_workspace_status` and fail with a clear message if it reports the account is not authorized. A `call(tool, args)` helper parses the JSON result and raises a typed error carrying `code` and `outcome`.
- `docs_publisher.py`: render the pulse as plain text or Markdown with a dated heading (`Groww Review Pulse — {YYYY}-W{WW}`) and append it to the configured doc, with `idempotency_key = "pulse-doc-{run_id}"`. Return the doc URL, and a link to the new section if the server can give one.
- `mail_drafter.py` becomes `mail_sender.py`: send one email to `PULSE_RECIPIENT` (no cc/bcc) with subject `Groww Review Pulse — {YYYY}-W{WW}`, the pulse as `body_text` plus the doc link, and `idempotency_key = "pulse-mail-{run_id}"`. Pass `confirm_send=true` only when the server requires it. Use `body_html` only if the server has HTML enabled (detect from the `google_workspace_status` capabilities, otherwise stay plain text). Return `message_id` and `thread_id`.
- Safety for a send-capable tool:
  - `publish` refuses to run unless every gate in `run_report.json` passed, and re-runs the PII gate on the exact text it is about to send.
  - The recipient comes only from `PULSE_RECIPIENT`; the pipeline never builds recipients from review text. Recommend setting `GMAIL_ALLOWED_RECIPIENTS` on the server to that address.
  - `--dry-run` renders the doc text and email to the run folder and makes no MCP calls except the read-only `google_workspace_status`.
- Failure handling: `success: false` with `outcome: not_performed` → stop with the error `code` and its hint (for example `AUTH_EXPIRED_OR_REVOKED` → re-run `npm run auth` on the server). `outcome: unknown` → do **not** retry; record it in `run_report.json`, tell the user to check the doc or the Sent folder, and offer a new key only after they confirm. Docs done but email failed → record the doc result, and re-running `publish` skips the doc step and retries only the email.
- Record `publish` state in `run_report.json`: `doc_url`, doc and mail `request_id`s, `message_id`, `thread_id` and each step's outcome, so a re-run knows what already happened.

**Tests:** a fake Streamable HTTP MCP server (the `mcp` SDK in-process) that asserts tool names, argument payloads, the bearer header and idempotency keys. Cases: a missing mapped tool fails fast; unauthorized `status` fails fast; docs success then mail failure → re-run sends only the mail; `OUTCOME_UNKNOWN` is not retried; a re-run with the same keys returns the recorded results without a second write; a failed gate blocks `publish`; `--dry-run` makes no write calls.

**Exit check:** `python -m pulse publish --run <id>` appends the pulse to the Google Doc and delivers one email to `PULSE_RECIPIENT`, both visible in the account. Running it again does not duplicate either. Start with a throwaway test doc and the recipient set to our own address.

---

## Phase 7 — Orchestration & Scheduling

**Tasks**
- `orchestrator.py`: run stages in order, persist each stage's output, support resume from any stage and `--run <id>`.
- Failure behavior: MCP down → save `pulse.md`, exit non-zero, print the resume command; Docs OK but Gmail failed → `publish` skips the doc step and retries only the email.
- Scheduling: weekly cron / GitHub Actions / launchd running `python -m pulse run`; secrets via environment.

**Exit check:** `python -m pulse run` completes end to end; killing a run midway and resuming works; scheduled trigger fires once.

---

## Phase 8 — Hardening & Documentation

**Tasks**
- End-to-end test: `python -m pulse run --dry-run` on fixtures.
- Review against the privacy checklist: no author fields on disk, PII gate on the final output, email sent only to `PULSE_RECIPIENT` through the MCP server.
- Cost and rate-limit review (LLM calls per run, Play paging).
- README: setup, MCP server config, env vars, commands, how to add an App Store adapter later.

**Exit check:** all tests pass; a fresh clone can follow the README to a successful run.

---

## Definition of Done

- Weekly run yields a one-page pulse of ≤250 words: top 3 themes (from ≤5), 3 verbatim anonymous quotes, 3 action ideas.
- Pulse is appended to the Google Doc and emailed to the recipient (self or alias), both through our MCP server.
- No PII in any artifact; all validation gates pass or the run fails without publishing.

## Open Items to Settle Early

1. ~~Which Docs and Gmail MCP servers~~ — settled: our Railway-hosted server (see Phase 6). Still needed: `MCP_ACCESS_TOKEN` and the target Google Doc.
2. LLM provider, model and per-run budget (blocks Phase 3).
3. Recipient address or alias.
4. ~~New doc per week, or one running doc~~ — settled: one running doc with a section appended each week, because the server only appends to an existing doc.
5. Confirm the mail change with whoever owns the requirements: the server cannot create drafts, so the email is sent rather than drafted.
