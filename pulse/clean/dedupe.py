"""De-duplication by normalized-text hash (spam and copy-paste reviews)."""
from __future__ import annotations

import hashlib
import re

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def text_key(text: str) -> str:
    """Hash of the lowercased text with punctuation, emoji and whitespace removed."""
    normalized = _NON_WORD.sub("", text.casefold())
    return hashlib.sha256(normalized.encode()).hexdigest()
