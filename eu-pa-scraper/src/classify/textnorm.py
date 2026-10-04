"""Normalizzazione del testo e matching di lessici multilingue."""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable


def fold(text: str) -> str:
    """minuscolo, senza accenti/segni diacritici (NFKD), punteggiatura -> spazio.
    Cirillico e greco restano (i toni greci vengono tolti)."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def compile_stems(stems: Iterable[str]) -> re.Pattern[str] | None:
    """Compila un lessico in un'unica regex. Sintassi delle voci (già in forma 'fold'
    o no: vengono normalizzate):
       'comune di'   inizio parola (prefisso):  \\bcomune\\s+di
       'sindaco!'    parola INTERA:             \\bsindaco\\b
    Più voci -> alternanza; findall restituisce le voci trovate."""
    parts: list[str] = []
    for raw in stems:
        if not raw:
            continue
        whole = raw.endswith("!")
        s = fold(raw.rstrip("!"))
        if not s:
            continue
        body = r"\s+".join(re.escape(w) for w in s.split())
        parts.append(body + (r"\b" if whole else ""))
    if not parts:
        return None
    parts.sort(key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(parts) + ")", re.UNICODE)


def distinct_matches(pattern: re.Pattern[str] | None, text: str) -> set[str]:
    if pattern is None or not text:
        return set()
    return {m.group(0) for m in pattern.finditer(text)}


def primary_lang(raw: str) -> str:
    """Sottotag PRINCIPALE di un codice lingua BCP-47: 'nl-NL' -> 'nl', 'PL' -> 'pl',
    'hr_HR' -> 'hr'. Case-insensitive, tollera sia '-' che '_' come separatore.
    Stringa vuota se 'raw' è vuoto o non valorizzato."""
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    return re.split(r"[-_]", raw, maxsplit=1)[0]
