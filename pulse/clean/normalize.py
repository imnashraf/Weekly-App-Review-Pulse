"""Map raw reviews to the normalized schema (architecture §5.2)."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from .pii import PLACEHOLDER_RE

_WORD = re.compile(r"\w+", re.UNICODE)
# U+2605/2606 (star symbols, as in "5★") are plain text, not emoji.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u2604\u2607-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
_LATIN_WORD = re.compile(r"[a-z]+")

# Unambiguous romanized-Hindi function words. Words that are also English or common typos
# ("tab", "aap", "mat", "par", "to", "bas", "kab", "jab") are left out on purpose.
HINGLISH_MARKERS = frozenset("""
hai hain nahi nahin nhi kya ke ki se mein bhi aur toh kiya kiye raha rahe rahi hota hoti hoga hogi
bahut bohot accha acha achha kyu kyun kyon koi paisa paise wala wali hum tum abhi kuch lekin magar
yeh liye karna karo kaam kaise kitna agar gaya gaye bekar bakwas apna apni
""".split())
HINGLISH_MIN_MARKERS = 2
_WS = re.compile(r"\s+")


def iso_week(day: str) -> str:
    year, week, _ = date.fromisoformat(day).isocalendar()
    return f"{year}-W{week:02d}"


def normalize(raw: dict, text: str) -> dict:
    """Normalized review. `text` is the already PII-scrubbed body."""
    return {
        "id": raw["id"],
        "source": raw["source"],
        "rating": raw["rating"],
        "title": None,
        "text": _WS.sub(" ", text).strip(),
        "date": raw["date"],
        "app_version": raw.get("app_version"),
        "week": iso_week(raw["date"]),
    }


def word_count(text: str) -> int:
    """Words in `text`, ignoring PII placeholders, emoji and punctuation."""
    return len(_WORD.findall(PLACEHOLDER_RE.sub(" ", text)))


def is_latin_script(text: str) -> bool:
    """True if most letters are Latin (English and romanized Hinglish); False for Devanagari etc."""
    latin = other = 0
    for ch in text:
        if ch.isalpha():
            if unicodedata.name(ch, "").startswith("LATIN"):
                latin += 1
            else:
                other += 1
    return latin >= other and latin > 0


def has_emoji(text: str) -> bool:
    return _EMOJI.search(text) is not None


def is_hinglish(text: str) -> bool:
    """Romanized Hindi: Latin script but at least HINGLISH_MIN_MARKERS Hindi function words."""
    hits = sum(w in HINGLISH_MARKERS for w in _LATIN_WORD.findall(text.lower()))
    return hits >= HINGLISH_MIN_MARKERS
