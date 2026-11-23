"""Pronunciation fixes, applied to a break's text just before it's spoken."""

from __future__ import annotations

import re

from config import Pronunciation


class Pronouncer:
    def __init__(self, entries: dict[str, Pronunciation]):
        self.entries = {name.casefold(): fix for name, fix in entries.items()}
        names = sorted(entries, key=len, reverse=True)  # "Sigur Rós" before "Sigur"
        self.pattern = (
            re.compile(r"(?<!\w)(" + "|".join(re.escape(n) for n in names) + r")(?!\w)", re.IGNORECASE)
            if names
            else None
        )

    def apply(self, text: str, style: str) -> str:
        """style: "kokoro" (phoneme markup allowed) or "text" (spellings only)."""
        if not self.pattern:
            return text

        def fix(m: re.Match) -> str:
            word = m.group(0)
            p = self.entries[word.casefold()]
            if style == "kokoro" and p.phonemes:
                return f"[{word}](/{p.phonemes}/)"
            return p.say or word

        return self.pattern.sub(fix, text)
