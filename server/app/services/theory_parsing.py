# server/app/services/theory_parsing.py
from __future__ import annotations
import re
from typing import Optional

THEORY_RE = re.compile(
    r'(?ms)^[ \t]*theory\s+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))'
)

def extract_theory_name(text: str) -> Optional[str]:
    m = THEORY_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)