from __future__ import annotations
from typing import Optional
from server.app.core.config import RegularExp

def extract_theory_name(text: str) -> Optional[str]:
    m = RegularExp.THEORY_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)