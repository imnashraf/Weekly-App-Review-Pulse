"""Regex PII scrubber (architecture §5.2). Idempotent: scrubbing scrubbed text changes nothing."""
from __future__ import annotations

import re
from collections import Counter

# (label, pattern, replacement). Order matters: earlier patterns consume text first.
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    ("link", re.compile(r"(?:https?://|www\.)\S+", re.I), "[link]"),
    # name@bank, no dot in the domain part (emails were already replaced above)
    ("upi", re.compile(r"(?<![\w@])[\w.\-]{2,}@[A-Za-z]{2,}\b"), "[upi]"),
    ("name", re.compile(r"(?<![\w@])@[A-Za-z_]\w*"), "[name]"),
    ("id", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[id]"),                  # PAN
    ("id", re.compile(r"(?<!\d)\d{4}[ -]\d{4}[ -]\d{4}(?!\d)"), "[id]"),      # Aadhaar, grouped
    ("phone", re.compile(r"(?<![\d\w])(?:(?:\+|00)?91[ -]?)?0?[6-9]\d{4}[ -]?\d{5}(?!\d)"), "[phone]"),
    ("id", re.compile(r"(?<!\d)\d{12}(?!\d)"), "[id]"),                        # Aadhaar, plain
    ("number", re.compile(r"(?<!\d)\d{9,}(?!\d)"), "[number]"),                # accounts, long runs
]

PLACEHOLDERS = frozenset(repl for _, _, repl in _RULES)
PLACEHOLDER_RE = re.compile(r"\[(?:email|link|upi|name|id|phone|number)\]")


def scrub(text: str, counts: Counter | None = None) -> str:
    """Replace PII with placeholders. If `counts` is given, tally redactions per label."""
    for label, pattern, repl in _RULES:
        text, n = pattern.subn(repl, text)
        if n and counts is not None:
            counts[label] += n
    return text


def has_pii(text: str) -> bool:
    return scrub(text) != text
