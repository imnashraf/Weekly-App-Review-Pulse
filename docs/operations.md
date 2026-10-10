# Operations review: privacy, cost and rate limits

Findings from the phase 8 review. Numbers come from the real 2026-W41 run
(`data/runs/2026-W41/run_report.json`), 1,588 reviews after cleaning.

## Privacy checklist

| Check | Result | Where it is enforced / tested |
|---|---|---|
| Reviewer name, avatar, original review id and developer replies never reach disk | Pass | Dropped inside the Google Play adapter. `tests/test_e2e.py::test_no_reviewer_identity_or_pii_in_any_stored_artifact` scans every file of a run. |
| Review ids stored are one-way hashes | Pass | `ingest/google_play.py` (`review_id`). |
| PII scrubbed before any LLM sees review text | Pass | `clean/pii.py` runs in the clean stage; themes, quotes and compose read only `02_` onwards. |
| Final pulse re-scanned for PII, and a failure blocks output | Pass | PII gate in `validate/gates.py`; `publish` re-checks the exact doc text, subject and body. |
| Quotes are verbatim, anonymous excerpts | Pass | Verbatim gate; quotes carry no attribution. |
| Email goes only to `PULSE_RECIPIENT`, with no cc/bcc | Pass | `tests/test_e2e.py::test_email_goes_only_to_the_configured_recipient`. The pipeline calls only `status`, `docs_append` and `gmail_send`. |
| Nothing is published unless all gates passed | Pass | `integrations/publish.py` (`_check_ready`). |
| Secrets stay out of git | Pass | `.env` is git-ignored; secrets live in Streamlit and GitHub Actions secrets. |

Accepted risks and notes:
- **`01_raw_reviews.jsonl` keeps review text as the reviewer wrote it.** A reviewer who typed their own
  phone number or email into a public review will have it in this file. It is public text, git-ignored and
  never sent to the LLM or published. It is stored in the Actions cache (private to the repo) and on the
  Streamlit server's disk, but not uploaded as a workflow artifact (only `pulse.md` and `run_report.json` are).
- The scrubber is regex based. It can miss free-form personal details such as a name written in a sentence.
  The LLM is told not to name people, but there is no name detector.
- The MCP bearer token lets a holder send email as the authorized Gmail account to any address the server
  allows. Set `GMAIL_ALLOWED_RECIPIENTS` on the server to `PULSE_RECIPIENT`, and rotate the token if it leaks.
- The Streamlit app's Run & publish tab is protected only by `ADMIN_PASSWORD`.

## Cost review (one weekly run)

| Stage | LLM calls | Tokens in | Tokens out |
|---|---|---|---|
| Theme discovery + labeling | 35 | 100,153 | 145,828 |
| Quotes + actions | 4 | 5,754 | 3,629 |
| Compose (one-liners) | 1 | 360 | 865 |
| **Total** | **40** | **106,267** | **150,322** |

- Input tokens match the Phase 3 estimate (about 100k). **Output tokens are about 7x the estimate** (150k
  against about 20k). The labeling replies are small, so most of this is almost certainly the model's hidden
  reasoning, which is billed as output. Turning the reasoning level down for the labeling calls is the single
  biggest saving; test it on a small run first, because the setting name differs between models.
- `cost_usd` shows `null` because no prices are configured. Set `llm.price_per_mtok_in` and
  `llm.price_per_mtok_out` in `config.yaml` (USD per million tokens, from the provider's price page) and the
  report and logs will include the cost.
- Two classification replies were malformed JSON and retried (2 extra calls). The client retries at most
  twice per call, so the worst case is about 3x the calls for a bad run; the run fails clearly if a call
  keeps failing.

## Rate limits and run time

- **Google Play:** about 200 reviews per page, 65 pages for 12 weeks (about 13k reviews), with a 1.5 s delay
  between pages (about 2 minutes) and up to 5 attempts with exponential backoff on errors. A hard cap of
  `limits.max_reviews_per_source` (20,000) stops runaway paging.
- **LLM:** 40 calls, made one after another. The labeling step is about 32 batches and takes most of the
  run, roughly 14 minutes in total with `gemini-3.8-flash`. HTTP errors 408, 429 and 5xx are retried up to 4
  times with backoff. Running batches in parallel would cut the time but raise the chance of hitting rate
  limits on a free key.
- **MCP server:** 3 to 4 calls per publish. Writes are never auto-retried, and idempotency keys stop
  duplicates.
- **GitHub Actions:** the workflow has a 60-minute timeout, comfortably above the run time.
