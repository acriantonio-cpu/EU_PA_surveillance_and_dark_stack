"""Caricamento e compilazione di config/classify_lexicon.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .textnorm import compile_stems, fold, primary_lang

DEFAULT_LEXICON_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "classify_lexicon.yaml"


@dataclass
class Lexicon:
    raw: dict[str, Any]
    gov_suffix: list[re.Pattern[str]] = field(default_factory=list)
    gov_suffix_conf: float = 0.9
    host_tokens: dict[str, list[tuple[str, bool]]] = field(default_factory=dict)   # tipo -> [(token, exact)]
    host_exclusions: tuple[str, ...] = ()
    tipo_patterns: dict[str, re.Pattern[str] | None] = field(default_factory=dict)
    public_pattern: re.Pattern[str] | None = None
    private_pattern: re.Pattern[str] | None = None
    terzo_settore_pattern: re.Pattern[str] | None = None
    legal_pattern: re.Pattern[str] | None = None
    field_weights: dict[str, float] = field(default_factory=dict)
    max_distinct: int = 3
    min_type_score: float = 3.0
    min_nature_score: float = 3.0
    jsonld_types: dict[str, dict[str, str]] = field(default_factory=dict)
    wikidata_keywords: list[dict[str, Any]] = field(default_factory=list)
    # parole valide solo per un country_code specifico (es. "sad!" per il tribunale polacco:
    # collide con l'inglese "sad"=triste, ma è sicura quando si sa già che il batch è polacco).
    # Struttura: {COUNTRY: {"tipo_keywords": {tipo: [voci]}, "public_markers": [...], ...}}
    country_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    # lingua/e attese per country (sottotag BCP-47 primario, es. {"PL": {"pl"}}): se la homepage
    # dichiara una lingua diversa (o nessuna), l'override per quel country NON si applica — vedi
    # _lang_ok. Di default, se il country non ha una voce 'lang' esplicita nello YAML, si assume
    # il codice paese in minuscolo (PL -> pl); non è quindi obbligatorio dichiararla per ognuno.
    _country_lang: dict[str, set[str]] = field(default_factory=dict, repr=False, compare=False)
    _country_cache: dict[tuple[str, str, str | None], re.Pattern[str] | None] = field(
        default_factory=dict, repr=False, compare=False
    )

    def _lang_ok(self, country: str, page_lang: str | None) -> bool:
        """True se non c'è nessuna restrizione di lingua per questo country, oppure se la lingua
        della pagina (sottotag primario) è tra quelle attese. Lingua della pagina assente o
        sconosciuta -> False: in dubbio, l'override NON scatta (si torna al lessico globale)."""
        expected = self._country_lang.get(country)
        if not expected:
            return True
        lang = primary_lang(page_lang or "")
        return bool(lang) and lang in expected

    def _for_country(self, family: str, base: re.Pattern[str] | None, base_stems_key: str,
                      tipo: str | None, country: str | None, page_lang: str | None) -> re.Pattern[str] | None:
        """Pattern compilato per 'family' (host_tokens escluso: quelli restano solo globali),
        con in più le voci di country_overrides valide SOLO per 'country' E SOLO se la lingua
        della pagina combacia con quella attesa per quel country (vedi _lang_ok). Senza country,
        senza override per quel country/tipo, o con lingua non combaciante, ritorna il pattern
        globale già compilato (nessun ricalcolo, nessuna voce paese-specifica applicata)."""
        if not country:
            return base
        country = country.upper()
        if not self._lang_ok(country, page_lang):
            return base
        cache_key = (family, tipo or "", country)
        if cache_key in self._country_cache:
            return self._country_cache[cache_key]
        node = self.country_overrides.get(country, {}).get(family, {})
        extra = node.get(tipo, []) if tipo else (node if isinstance(node, list) else [])
        if not extra:
            self._country_cache[cache_key] = base
            return base
        base_stems = self.raw.get(base_stems_key, {})
        if tipo:
            base_stems = (base_stems or {}).get(tipo, [])
        pat = compile_stems(list(base_stems or []) + list(extra))
        self._country_cache[cache_key] = pat
        return pat

    def tipo_pattern_for(self, tipo: str, country: str | None, page_lang: str | None = None) -> re.Pattern[str] | None:
        return self._for_country("tipo_keywords", self.tipo_patterns.get(tipo), "tipo_keywords", tipo, country, page_lang)

    def public_pattern_for(self, country: str | None, page_lang: str | None = None) -> re.Pattern[str] | None:
        return self._for_country("public_markers", self.public_pattern, "public_markers", None, country, page_lang)

    def private_pattern_for(self, country: str | None, page_lang: str | None = None) -> re.Pattern[str] | None:
        return self._for_country("private_markers", self.private_pattern, "private_markers", None, country, page_lang)

    def terzo_settore_pattern_for(self, country: str | None, page_lang: str | None = None) -> re.Pattern[str] | None:
        return self._for_country("terzo_settore_markers", self.terzo_settore_pattern, "terzo_settore_markers",
                                  None, country, page_lang)

    def allowed_tlds_for(self, country: str | None) -> frozenset[str] | None:
        """TLD 'tipici' per questo country (country_overrides.PAESE.allowed_tlds), oppure None se
        il paese non ha questa voce dichiarata (nessuna restrizione: opt-in esplicito, non un
        default silenzioso). Le voci nello YAML sono senza il punto iniziale, es. 'nl', 'org'."""
        if not country:
            return None
        node = self.country_overrides.get(country.upper(), {})
        tlds = node.get("allowed_tlds")
        return frozenset(t.lower().lstrip(".") for t in tlds) if tlds else None


def load_lexicon(path: Path | str | None = None) -> Lexicon:
    p = Path(path) if path else DEFAULT_LEXICON_PATH
    if not p.exists():
        raise FileNotFoundError(f"Lessico del classificatore non trovato: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    lex = Lexicon(raw=raw)
    lex.gov_suffix = [re.compile(x) for x in raw.get("gov_suffix_patterns", [])]
    lex.gov_suffix_conf = float(raw.get("gov_suffix_confidence", 0.9))
    for tipo, toks in (raw.get("host_tokens") or {}).items():
        lex.host_tokens[tipo] = [(fold(t.rstrip("!")).replace(" ", ""), t.endswith("!")) for t in toks]
    lex.host_exclusions = tuple(fold(x).replace(" ", "") for x in raw.get("host_token_exclusions", []))
    for tipo, stems in (raw.get("tipo_keywords") or {}).items():
        lex.tipo_patterns[tipo] = compile_stems(stems)
    lex.public_pattern = compile_stems(raw.get("public_markers", []))
    lex.private_pattern = compile_stems(raw.get("private_markers", []))
    lex.terzo_settore_pattern = compile_stems(raw.get("terzo_settore_markers", []))
    lex.legal_pattern = compile_stems(raw.get("legal_link_patterns", []))
    lex.field_weights = {k: float(v) for k, v in (raw.get("field_weights") or {}).items()}
    lex.max_distinct = int(raw.get("max_distinct_per_field", 3))
    lex.min_type_score = float(raw.get("min_type_score", 3.0))
    lex.min_nature_score = float(raw.get("min_nature_marker_score", 3.0))
    lex.jsonld_types = raw.get("jsonld_types") or {}
    lex.wikidata_keywords = sorted(
        raw.get("wikidata_class_keywords") or [], key=lambda e: len(str(e.get("kw", ""))), reverse=True
    )
    lex.country_overrides = {str(c).upper(): (v or {}) for c, v in (raw.get("country_overrides") or {}).items()}
    for country, node in lex.country_overrides.items():
        lang_spec = node.get("lang")
        if lang_spec is None:
            expected = {country.lower()}                     # default: codice paese == sottotag lingua
        elif isinstance(lang_spec, str):
            expected = {lang_spec.strip().lower()}
        else:
            expected = {str(x).strip().lower() for x in lang_spec}
        lex._country_lang[country] = expected
    # validazione: tipi ammessi (anche dentro gli override per paese)
    from .taxonomy import TYPES
    for tipo in list(lex.tipo_patterns) + list(lex.host_tokens):
        if tipo not in TYPES:
            raise ValueError(f"lessico: tipo sconosciuto '{tipo}' (validi: {TYPES})")
    for country, node in lex.country_overrides.items():
        for tipo in (node.get("tipo_keywords") or {}):
            if tipo not in TYPES:
                raise ValueError(f"lessico: country_overrides.{country} usa un tipo sconosciuto '{tipo}'")
    return lex
