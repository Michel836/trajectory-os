"""MVP — intake helpers: what a pasted message contains, deterministically.

The intake loop must do more than store text: a message usually carries
**questions** (things only the correspondent can answer), and those are what
make a dossier resumable. Extracting them is a deterministic, explainable
operation — no model (the guide's P05/P07).

This module is deliberately tiny and pure: no I/O, no clock, no portfolio.
"""

from __future__ import annotations

import re

#: A question must carry enough words to be meaningful.
MIN_QUESTION_CHARS = 15
#: Never create more questions than this from one message.
MAX_QUESTIONS = 5

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.?!])\s+")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def extract_questions(text: str, *, maximum: int = MAX_QUESTIONS) -> list[str]:
    """Return the interrogative sentences of a message, cleaned and deduped.

    A sentence is kept when it ends with ``?`` and carries at least
    :data:`MIN_QUESTION_CHARS` characters, so a stray "OK?" or a quoted echo is
    not turned into a durable question. Order is the order of the message.
    """
    if not text:
        return []
    bound = max(1, min(int(maximum), MAX_QUESTIONS))
    questions: list[str] = []
    seen: set[str] = set()
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        candidate = _clean(sentence)
        if not candidate.endswith("?"):
            continue
        # Drop a leading connective that belongs to the previous sentence.
        candidate = re.sub(r"^(?:et|and|ou|or|mais|but)\s+", "", candidate,
                           flags=re.IGNORECASE)
        if len(candidate) < MIN_QUESTION_CHARS:
            continue
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        questions.append(candidate)
        if len(questions) >= bound:
            break
    return questions


def question_titles(questions: list[str], *, maximum_chars: int = 120
                    ) -> list[str]:
    """Short, human-readable forms of the questions (for a list)."""
    return [question if len(question) <= maximum_chars
            else question[:maximum_chars - 1].rstrip() + "…"
            for question in questions]


__all__ = [
    "MAX_QUESTIONS",
    "MIN_QUESTION_CHARS",
    "extract_questions",
    "question_titles",
]
