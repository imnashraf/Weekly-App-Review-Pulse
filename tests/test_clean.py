from collections import Counter

import pytest

from pulse.clean import CLEAN_FILE, META_FILE, RAW_FILE, clean_reviews, run_clean
from pulse.clean.dedupe import text_key
from pulse.clean.normalize import has_emoji, is_hinglish, is_latin_script, iso_week, word_count
from pulse.clean.pii import has_pii, scrub
from pulse.config import load_config
from pulse.store import RunStore

PII_CASES = [
    ("mail me at john.doe+x@gmail.com please", "mail me at [email] please"),
    ("call 9876543210 now", "call [phone] now"),
    ("call +91 98765 43210 now", "call [phone] now"),
    ("call +91-9876543210 now", "call [phone] now"),
    ("call 919876543210 now", "call [phone] now"),
    ("my pan ABCDE1234F is blocked", "my pan [id] is blocked"),
    ("aadhaar 1234 5678 9012 linked", "aadhaar [id] linked"),
    ("aadhaar 123456789012 linked", "aadhaar [id] linked"),
    ("account 123456789 failed", "account [number] failed"),
    ("pay to rahul@okhdfcbank failed", "pay to [upi] failed"),
    ("see https://groww.in/help?x=1 or www.groww.in", "see [link] or [link]"),
    ("thanks @groww_support for nothing", "thanks [name] for nothing"),
]

# Things that look numeric but are not PII.
NOT_PII = [
    "rated 5 stars after version 18.20.2 update",
    "lost 25000 rupees in 3 days",
    "order 12345678 pending",           # 8 digits stays
    "app crashes at 10:30 every day, 100% sure",
    "good app, 1.5 lakh profit",
    "not working since @10/09/2026 at all",   # @ before digits is not a mention
]


@pytest.mark.parametrize("raw,expected", PII_CASES)
def test_scrub_replaces_pii(raw, expected):
    assert scrub(raw) == expected


@pytest.mark.parametrize("text", NOT_PII)
def test_scrub_leaves_non_pii(text):
    assert scrub(text) == text
    assert not has_pii(text)


@pytest.mark.parametrize("raw,_", PII_CASES)
def test_scrub_is_idempotent(raw, _):
    once = scrub(raw)
    assert scrub(once) == once


def test_scrub_counts_redactions():
    counts = Counter()
    scrub("a@b.com and c@d.com call 9876543210", counts)
    assert counts == {"email": 2, "phone": 1}


def test_word_count_ignores_placeholders_emoji_punctuation():
    assert word_count("this app is best ❤❤❤") == 4
    assert word_count("write to [email] about [phone] now") == 4
    assert word_count("") == 0


def test_script_detection():
    assert is_latin_script("mara number ko unban kor do")           # Hinglish
    assert is_latin_script("great app, कमाल")                        # mostly Latin
    assert not is_latin_script("बहुत अच्छा ऐप है धन्यवाद")           # Devanagari
    assert not is_latin_script("❤❤❤")


def test_emoji_detection():
    assert has_emoji("great app ❤") and has_emoji("so good 👍 really") and has_emoji("🥵")
    assert not has_emoji("great app, 100% worth it! ₹500 profit - 5★ rating?")


def test_hinglish_detection():
    assert is_hinglish("app me login nahi ho raha hai")
    assert is_hinglish("bahut hidden charges lagate hai")
    # single marker or English words that double as Hindi spellings stay English
    assert not is_hinglish("the UPI tab has been hidden inside profile, please bring back")
    assert not is_hinglish("downloaded the aap in June and the kyc mismatch is annoying")
    assert not is_hinglish("customer support is worst, withdrawals take many days nahi")


def test_text_key_ignores_case_punctuation_emoji():
    assert text_key("Great app!! 👍") == text_key("great   app")
    assert text_key("great app") != text_key("great apps")


def test_iso_week():
    assert iso_week("2026-10-08") == "2026-W41"
    assert iso_week("2026-01-01") == "2026-W01"


def raw(i, text, rating=3, day="2026-10-08"):
    return {"id": f"id{i}", "source": "google_play", "rating": rating,
            "text": text, "date": day, "app_version": "1.0"}


LONG = "withdrawal takes too many days and support never replies to me"   # 11 words


def test_more_than_eight_words_filter_boundary():
    eight = "one two three four five six seven eight"
    nine = eight + " nine"
    kept, stats = clean_reviews([raw(1, eight), raw(2, nine)], min_words=9)
    assert [r["id"] for r in kept] == ["id2"]
    assert stats["dropped"]["too_short"] == 1


def test_clean_reviews_schema_and_drop_counts():
    rows = [
        raw(1, LONG, rating=1),
        raw(2, "good"),                                                  # too short
        raw(3, "❤ 👍 🙏"),                                               # empty (no words)
        raw(4, "यह ऐप बहुत अच्छा है और मुझे पसंद आया है बहुत ज्यादा"),   # other script
        raw(5, LONG.upper() + "!!"),                                     # duplicate of 1
        raw(7, LONG + " 👍"),                                            # emoji
        raw(8, "app me login nahi ho raha hai since the latest update came"),  # Hinglish
        raw(6, "email me at a@b.com about the stuck withdrawal of my money today"),
    ]
    kept, stats = clean_reviews(rows, min_words=9)
    assert [r["id"] for r in kept] == ["id1", "id6"]
    assert kept[0] == {
        "id": "id1", "source": "google_play", "rating": 1, "title": None, "text": LONG,
        "date": "2026-10-08", "app_version": "1.0", "week": "2026-W41",
    }
    assert "a@b.com" not in kept[1]["text"] and "[email]" in kept[1]["text"]
    assert stats["input_count"] == 8 and stats["output_count"] == 2
    assert stats["dropped"] == {"empty": 1, "too_short": 1, "emoji": 1,
                               "non_english_script": 1, "hinglish": 1, "duplicate": 1}
    assert stats["pii_redactions"] == {"email": 1}


def test_placeholders_do_not_count_towards_min_words():
    kept, stats = clean_reviews([raw(1, "a@b.com c@d.com 9876543210 call me now please")], 9)
    assert kept == [] and stats["dropped"]["too_short"] == 1


def test_run_clean_writes_files_and_output_is_pii_free(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("app: {google_play_id: x}\nllm: {model: m}\n"
                   "mcp: {docs: {command: d}, gmail: {command: g}}\n")
    settings = load_config(cfg)
    store = RunStore(tmp_path / "runs", "2026-W41")
    store.write_jsonl(RAW_FILE, [
        raw(1, "please call 9876543210 or mail x@y.com about my stuck withdrawal money"),
        raw(2, LONG),
    ])
    stats = run_clean(settings, store)
    assert stats["output_count"] == 2
    assert store.read_json(META_FILE) == stats
    for r in store.read_jsonl(CLEAN_FILE):
        assert not has_pii(r["text"])


def test_run_clean_requires_raw_file(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("app: {google_play_id: x}\nllm: {model: m}\n"
                   "mcp: {docs: {command: d}, gmail: {command: g}}\n")
    with pytest.raises(RuntimeError, match="ingest"):
        run_clean(load_config(cfg), RunStore(tmp_path / "runs", "2026-W41"))
