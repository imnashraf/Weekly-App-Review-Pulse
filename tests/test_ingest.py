import json
from datetime import date, datetime

import pytest

from pulse.config import load_config
from pulse.ingest import META_FILE, RAW_FILE, run_ingest, window_start
from pulse.ingest.google_play import GooglePlayAdapter, to_raw_review
from pulse.store import RunStore


def item(i, day, text="great app but slow withdrawals", score=4):
    return {
        "reviewId": f"gp-{i}", "userName": "Real Name", "userImage": "http://img/x.png",
        "content": text, "score": score, "thumbsUpCount": 3,
        "reviewCreatedVersion": "1.0", "appVersion": "9.9.9",
        "at": datetime.fromisoformat(f"{day}T10:00:00"),
        "replyContent": "thanks", "repliedAt": None,
    }


def paged(pages):
    """fetch_page stub serving `pages` in order, tokens are page indexes."""
    calls = []

    def fetch(token):
        calls.append(token)
        idx = 0 if token is None else token
        nxt = idx + 1 if idx + 1 < len(pages) else None
        return pages[idx], nxt

    fetch.calls = calls
    return fetch


def adapter(fetch):
    return GooglePlayAdapter("app", fetch_page=fetch, sleep=lambda s: None)


START = date(2026, 7, 1)


def test_no_identity_fields_kept():
    r = to_raw_review(item(1, "2026-09-01"))
    assert set(r) == {"id", "source", "rating", "text", "date", "app_version"}
    blob = json.dumps(r)
    assert "Real Name" not in blob and "gp-1" not in blob and "img" not in blob
    assert len(r["id"]) == 16 and r["date"] == "2026-09-01" and r["app_version"] == "9.9.9"


def test_empty_review_skipped():
    assert to_raw_review(item(1, "2026-09-01", text="   ")) is None


def test_window_cutoff_and_paging_stop():
    pages = [
        [item(1, "2026-09-30"), item(2, "2026-08-01")],
        [item(3, "2026-07-02"), item(4, "2026-06-30")],  # crosses window start
        [item(5, "2026-06-01")],                          # must not be fetched
    ]
    f = paged(pages)
    got = list(adapter(f).fetch(START, 1000))
    assert [r["date"] for r in got] == ["2026-09-30", "2026-08-01", "2026-07-02"]
    assert f.calls == [None, 1]


def test_stops_when_no_more_pages():
    f = paged([[item(1, "2026-09-30")]])
    assert len(list(adapter(f).fetch(START, 1000))) == 1
    assert f.calls == [None]


def test_cap_respected():
    f = paged([[item(i, "2026-09-30") for i in range(5)], [item(9, "2026-09-29")]])
    assert len(list(adapter(f).fetch(START, 3))) == 3
    assert f.calls == [None]


def test_retries_with_backoff_then_succeeds():
    state = {"n": 0}
    sleeps = []

    def flaky(token):
        state["n"] += 1
        if state["n"] < 3:
            raise ConnectionError("429")
        return [item(1, "2026-09-30")], None

    a = GooglePlayAdapter("app", fetch_page=flaky, sleep=sleeps.append)
    assert len(list(a.fetch(START, 10))) == 1
    assert sleeps == [2, 4]


def test_gives_up_after_five_attempts():
    def bad(token):
        raise ConnectionError("down")

    with pytest.raises(RuntimeError, match="5 attempts"):
        list(GooglePlayAdapter("app", fetch_page=bad, sleep=lambda s: None).fetch(START, 10))


def test_window_start_math():
    assert window_start(date(2026, 10, 9), 12) == date(2026, 7, 17)


def test_run_ingest_writes_files(tmp_path):
    settings = load_config()
    store = RunStore(tmp_path, "2026-W41")
    f = paged([[item(1, "2026-10-01"), item(2, "2026-09-01")]])
    meta = run_ingest(settings, store, adapter(f), run_date=date(2026, 10, 9))
    rows = list(store.read_jsonl(RAW_FILE))
    assert len(rows) == 2 and meta["review_count"] == 2
    assert store.read_json(META_FILE)["window_start"] == "2026-07-17"
    assert all(r["date"] >= "2026-07-17" for r in rows)
