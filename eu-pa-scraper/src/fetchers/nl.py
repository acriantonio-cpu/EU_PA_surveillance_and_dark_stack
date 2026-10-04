# EN: Netherlands - Register van Overheidsorganisaties (KOOP) daily XML export parser.
"""
Paesi Bassi — Register van Overheidsorganisaties (KOOP).
Export XML giornaliero: https://organisaties.overheid.nl/archive/exportOO.xml

La struttura XML reale può variare leggermente nel tempo: questo parser usa
ricerche 'contains-tag' tolleranti (namespace-agnostic) invece di xpath rigidi,
e va verificato contro un export corrente prima dell'uso in produzione.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

try:  # parser sicuro (no entity expansion) se defusedxml è installato
    from defusedxml import ElementTree as _SafeET
except ImportError:  # pragma: no cover
    _SafeET = None
from pathlib import Path
from typing import Any

from ..base_fetcher import BaseFetcher, now_iso
from ..schema import Record

logger = logging.getLogger(__name__)


def _local(tag: str) -> str:
    """Rimuove il namespace da un tag XML: '{ns}foo' -> 'foo'."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


# Sotto-elementi che descrivono PERSONE/contatti dell'organizzazione, non
# l'organizzazione stessa: un 'naam' o un 'url' lì dentro NON è il nome/sito dell'ente.
_CONTACT_TAGS = frozenset({
    "contactpersoon", "contact", "persoon", "medewerker", "functionaris", "bestuurder",
    "contactgegevens", "bezoekadres", "postadres",
})


def _first_text(el: ET.Element, candidates: list[str]) -> str | None:
    """Cerca il testo per i tag in 'candidates': prima tra i FIGLI DIRETTI, poi tra i
    discendenti saltando i sotto-alberi di contatti/persone (prima prendeva il primo
    discendente in assoluto, che poteva essere il nome di un contatto)."""
    for child in el:
        if _local(child.tag) in candidates and child.text and child.text.strip():
            return child.text.strip()

    def _walk(node: ET.Element) -> str | None:
        for child in node:
            local = _local(child.tag)
            if local in _CONTACT_TAGS:
                continue
            if local in candidates and child.text and child.text.strip():
                return child.text.strip()
            found = _walk(child)
            if found:
                return found
        return None

    return _walk(el)


class NLFetcher(BaseFetcher):
    country_code = "NL"
    source_name = "Register van Overheidsorganisaties (KOOP)"

    def download(self) -> Path:
        url = self.config["source_url"]
        out = self.input_dir / self.config.get("input_filename", "exportOO.xml")
        resp = self.http.get(url)
        out.write_bytes(resp.content)
        logger.info("[NL] scaricato %s -> %s", url, out)
        return out

    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        tree = (_SafeET or ET).parse(raw_path)
        root = tree.getroot()

        record_tag = self.config.get("record_tag", "organisatie")
        raw_rows: list[dict[str, Any]] = []
        for el in root.iter():
            if _local(el.tag) != record_tag:
                continue
            row = {
                "naam": _first_text(el, ["naam", "officieleNaam", "naamOrganisatie"]),
                "website": _first_text(el, ["internetadres", "website", "url"]),
                "plaats": _first_text(el, ["plaats", "vestigingsplaats", "gemeente"]),
                "provincie": _first_text(el, ["provincie"]),
                "identificatie": _first_text(el, ["identificatie", "OIN", "code"]),
                "classificatie": _first_text(el, ["classificatie", "organisatietype", "soort"]),
            }
            raw_rows.append(row)
        return raw_rows

    def normalize(self, raw_rows) -> list[Record]:
        ts = now_iso()
        records = []
        for row in raw_rows:
            records.append(
                Record(
                    hostname=(row.get("website") or "").strip(),
                    comune=(row.get("plaats") or "").strip(),
                    regione=(row.get("provincie") or "").strip(),
                    codice_ipa=(row.get("identificatie") or "").strip(),
                    sector=(row.get("classificatie") or "").strip(),
                    country_code=self.country_code,
                    source_name=self.source_name,
                    retrieved_at=ts,
                )
            )
        return records
