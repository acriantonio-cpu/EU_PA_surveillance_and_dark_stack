"""
Francia — Annuaire de l'administration et des services publics (DILA).

STORIA (per chi legge questo file in futuro):

1) Il primo tentativo (`data.gouv.fr/api/1/datasets/?q=...`) era l'API di
   RICERCA nel catalogo dataset di data.gouv.fr, non l'annuario: per questo
   tornava "total": 0.

2) L'URL "stable" giusto (`data.gouv.fr/api/1/datasets/r/{id}`) NON serve
   direttamente un JSON: fa un redirect 302 verso
   `lecomarquage.service-public.gouv.fr/donnees_locales_v4/all_latest.tar.bz2`
   — un archivio **tar compresso bz2** di ~350 MB (verificato il 12/09/2026),
   che contiene:
     - un file tipo "{data}-data.gouv_local.json": un OGGETTO JSON con una
       chiave "service" (non "data"/"results"/"records") contenente la lista
       di TUTTI gli enti/sportelli, con struttura annidata (indirizzo,
       telefono, e soprattutto "site_internet" come LISTA di
       {"libelle": ..., "valeur": <url>} — un ente può avere più siti/link,
       es. il sito principale + un link di prenotazione appuntamenti).
     - una cartella "{data}-data.gouv_commune/" con un file per comune
       (es. "77188.json"): dati anagrafici del comune, SENZA link utili
       (verificato dall'utente il 12/09/2026) — questo fetcher la ignora
       completamente per non perdere tempo/spazio a estrarla.

Questo modulo scarica l'archivio, estrae in memoria SOLO il file
"*_data.gouv_local.json" (senza scompattare l'intero tar.bz2 su disco), e
normalizza estraendo TUTTI i link trovati in "site_internet" per ciascun
ente: se un ente ha più link, produce PIÙ righe di output (stesso ente,
'hostname' diverso, 'tipo_link' che riporta la relativa 'libelle' — es.
"Prise de rendez-vous en ligne" — o "sito ufficiale" se non specificata).
Se un ente non ha alcun link, produce comunque una riga con hostname vuoto
(per non perdere il conteggio degli enti censiti).

Include anche un fallback per il caso in cui in futuro la fonte torni a
servire JSON semplice (diretto o NDJSON) invece del tar.bz2.
"""

from __future__ import annotations

import json
import logging
import tarfile
from pathlib import Path
from typing import Any

from ..base_fetcher import BaseFetcher, now_iso
from ..schema import Record

logger = logging.getLogger(__name__)

# Nome (suffisso) del file che contiene i dati enti dentro l'archivio.
LOCAL_JSON_SUFFIX = "data.gouv_local.json"


class FRFetcher(BaseFetcher):
    country_code = "FR"
    source_name = "Annuaire de l'administration (DILA / data.gouv.fr) — base de données locales"

    def download(self) -> Path:
        out = self.input_dir / self.config.get("input_filename", "annuaire_local.tar.bz2")

        urls = self.config.get("source_urls") or [self.config["source_url"]]
        last_exc: Exception | None = None
        for url in urls:
            try:
                # Streaming su disco: l'archivio è ~350 MB, con get()+resp.content stava
                # tutto in RAM (e un retry lo riscaricava per intero da capo).
                n_bytes = self.http.download_to_file(url, out)
                logger.info("[FR] scaricato %s -> %s (%d byte)", url, out, n_bytes)
                return out
            except Exception as exc:  # noqa: BLE001
                logger.warning("[FR] fallito il download da %s: %s", url, exc)
                last_exc = exc
        raise RuntimeError(
            "Impossibile scaricare l'annuario da nessuno degli URL configurati "
            f"in 'source_urls'/'source_url'. Ultimo errore: {last_exc}"
        )

    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        try:
            return _parse_tar_bz2(raw_path)
        except tarfile.ReadError:
            # Fallback: la fonte è tornata a servire JSON/NDJSON diretto
            # invece del tar.bz2 (non osservato finora, ma gestito per
            # robustezza se DILA cambia di nuovo il formato di export).
            logger.info(
                "[FR] il file non è un archivio tar.bz2 valido: provo a "
                "interpretarlo come JSON/NDJSON diretto"
            )
            raw_bytes = raw_path.read_bytes()
            text = _decode_bytes(raw_bytes, context=f"FR file {raw_path.name}")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return _parse_ndjson(text)
            return _extract_container(data)

    def normalize(self, raw_rows: list[dict[str, Any]]) -> list[Record]:
        ts = now_iso()
        records: list[Record] = []

        for row in raw_rows:
            nome_ente = _pick(row, ["nom", "nom_courant", "raison_sociale", "libelle"])
            adresse = _first_dict(row.get("adresse"))
            comune = _pick(adresse, ["nom_commune", "commune"])
            code_insee = _pick(row, ["code_insee_commune"]) or _pick(adresse, ["code_insee_commune"])
            regione = _departement_from_code_insee(code_insee)

            categorie = _pick(row, ["categorie"])
            pivot = _first_dict(row.get("pivot"))
            type_service = _pick(pivot, ["type_service_local"])
            sector = "/".join(p for p in (categorie, type_service) if p)

            codice_ipa = _pick(row, ["id", "siret", "siren", "ancien_code_pivot"])

            base_kwargs = dict(
                nome_ente=nome_ente,
                comune=comune,
                regione=regione,
                codice_ipa=codice_ipa,
                sector=sector,
                country_code=self.country_code,
                source_name=self.source_name,
                retrieved_at=ts,
            )

            links = _extract_links(row.get("site_internet"))
            if not links:
                records.append(Record(hostname="", tipo_link="", **base_kwargs))
                continue

            for url, tipo_link in links:
                records.append(Record(hostname=url, tipo_link=tipo_link, **base_kwargs))

        return records


# --------------------------------------------------------------------------
# Helper di estrazione/parsing
# --------------------------------------------------------------------------

def _parse_tar_bz2(raw_path: Path) -> list[dict[str, Any]]:
    """
    Apre l'archivio tar.bz2 in modalità streaming ed estrae in MEMORIA solo
    il membro '*_data.gouv_local.json' (non scompatta l'intera cartella
    'data.gouv_commune' su disco: non contiene link e sprecherebbe tempo).
    """
    with tarfile.open(raw_path, mode="r:bz2") as tar:
        target_bytes: bytes | None = None
        target_name: str | None = None
        for member in tar:  # iterazione in streaming: non richiede seek
            if member.isfile() and member.name.endswith(LOCAL_JSON_SUFFIX):
                f = tar.extractfile(member)
                if f is not None:
                    target_bytes = f.read()
                    target_name = member.name
                break  # trovato: interrompiamo subito, non serve altro

        if target_bytes is None:
            raise ValueError(
                f"Nessun file '*{LOCAL_JSON_SUFFIX}' trovato nell'archivio "
                f"tar.bz2 '{raw_path.name}'. Verificare manualmente il "
                f"contenuto dell'archivio (potrebbe essere cambiato il nome "
                f"del file principale)."
            )

    logger.info("[FR] estratto dall'archivio: %s (%.1f MB)", target_name, len(target_bytes) / (1024 * 1024))
    text = _decode_bytes(target_bytes, context=f"FR archivio member {target_name}")
    data = json.loads(text)
    return _extract_container(data)


def _extract_container(data: Any) -> list[dict[str, Any]]:
    """Il file può essere una lista diretta di enti, oppure un oggetto con
    chiave contenitore 'service' (formato osservato) o 'data'/'results'/
    'records'/'organismes' (formati alternativi visti in altre fonti DILA)."""
    if isinstance(data, list):
        return data
    for key in ("service", "data", "results", "records", "organismes"):
        if isinstance(data.get(key), list):
            return data[key]
    raise ValueError(
        "Struttura JSON non riconosciuta: attesa una lista di enti o un "
        "oggetto con chiave 'service'/'data'/'results'/'records'/'organismes'. "
        "Ispezionare manualmente il file estratto in data/input/FR/."
    )


def _extract_links(site_internet: Any) -> list[tuple[str, str]]:
    """
    'site_internet' è una lista di {'libelle': ..., 'valeur': <url>}.
    Ritorna una lista di (url, tipo_link) per OGNI link non vuoto trovato,
    usando 'libelle' come classificazione quando presente, altrimenti
    "sito ufficiale".
    """
    if not isinstance(site_internet, list):
        return []
    out = []
    for entry in site_internet:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("valeur") or "").strip()
        if not url:
            continue
        libelle = (entry.get("libelle") or "").strip()
        out.append((url, libelle or "sito ufficiale"))
    return out


def _first_dict(value: Any) -> dict:
    """Molti campi DILA sono liste con zero o un elemento (es. 'adresse',
    'pivot'): ritorna il primo dict se presente, altrimenti {}."""
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    if isinstance(value, dict):
        return value
    return {}


def _departement_from_code_insee(code_insee: str) -> str:
    """Codice dipartimento a partire dal codice INSEE comune: 2 cifre per
    la Francia metropolitana, 3 per i DOM (Guadalupa, Martinica, Guyana,
    Riunione, Mayotte: prefissi 97x/98x)."""
    if not code_insee:
        return ""
    if code_insee.startswith(("97", "98")) and len(code_insee) >= 3:
        return code_insee[:3]
    return code_insee[:2]


def _decode_bytes(raw: bytes, context: str = "") -> str:
    """Decodifica bytes provando più encoding in ordine, perché alcuni
    export governativi francesi non sono in UTF-8 (spesso Windows-1252/
    Latin-1 per via di trattini lunghi o virgolette tipografiche)."""
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc != "utf-8":
                logger.warning(
                    "%s: non è UTF-8 valido, decodificato come %s (verificare che i "
                    "caratteri accentati risultino corretti nell'output finale)",
                    context, enc,
                )
            return text
        except UnicodeDecodeError:
            continue
    logger.warning(
        "%s: nessun encoding standard ha funzionato, decodifico come UTF-8 "
        "sostituendo i byte non validi (alcuni caratteri potrebbero risultare "
        "corrotti)", context,
    )
    return raw.decode("utf-8", errors="replace")


def _parse_ndjson(text: str) -> list[dict[str, Any]]:
    """Parsa un file 'NDJSON' (un oggetto JSON per riga) — fallback usato
    solo se la fonte non è più un tar.bz2 né un unico JSON valido."""
    rows: list[dict[str, Any]] = []
    n_errors = 0
    for i, line in enumerate(text.splitlines(), start=1):
        line = line.strip().rstrip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            n_errors += 1
            if n_errors <= 5:
                logger.warning("[FR] riga %d non parsabile come JSON, saltata", i)
    if n_errors:
        logger.warning("[FR] totale righe non parsabili saltate: %d", n_errors)
    if not rows:
        raise ValueError(
            "Il file non è né un tar.bz2, né un JSON valido, né un NDJSON "
            "valido. Ispezionare manualmente il file in data/input/FR/."
        )
    logger.info("[FR] parsato come NDJSON: %d record", len(rows))
    return rows


def _pick(d: dict, keys: list[str]) -> str:
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if v:
            return str(v).strip()
    return ""
