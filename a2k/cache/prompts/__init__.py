"""The admission prompts, as written by the business/domain owners: the
classifier, the judge core and one judge module per data domain. Each .md
file is kept exactly as delivered (2026-10-02); the code reads the text of its
first fenced block -- "Prompt (copy as-is)" or "Module text" -- and the rest
of the file (usage notes, changelog) is documentation.

Changing a prompt means replacing its file, never editing the text in code.
Where the code needs something the files do not have, it is added in
judge.py by explicit, checked replacements (see judge.ADAPTATIONS).
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.DOTALL | re.MULTILINE)


@cache
def block(name: str) -> str:
    """Text of the first fenced block of `<name>.md`."""
    text = (_DIR / f"{name}.md").read_text(encoding="utf-8")
    found = _FENCE.search(text)
    if not found:
        raise RuntimeError(f"{name}.md has no fenced prompt block")
    return found.group(1).rstrip("\n")
