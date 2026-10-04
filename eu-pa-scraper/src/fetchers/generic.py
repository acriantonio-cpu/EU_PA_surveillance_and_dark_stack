"""
Fetcher generici, pilotati interamente dal config YAML di ciascun paese.
Coprono la maggior parte dei casi (Fascia B/C della relazione): un unico
file bulk (CSV/XLSX/XML) o un endpoint JSON, con mapping colonna->campo
definito in YAML. Per i pochi paesi con logica davvero ad-hoc, si usano i
moduli dedicati (nl.py, fr.py, hr.py, se.py) referenziati come
'custom_module' nel config.
"""

from __future__ import annotations

import csv
import hashlib
import threading
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from types import SimpleNamespace
import io
import json
import logging
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from ..base_fetcher import BaseFetcher, HttpSession, _dig
from ..crawl_store import CrawlStore
from ..fetchers_common import root_domain_of
from ..polite import ROBOTS_AGENT, RobotsCache, identified_user_agent, make_fetch_text, warn_if_no_contact
from ..schema import now_iso
from ..urlutil import canonicalize_url, host_of, looks_like_trap, url_pattern_signature
from ..web_signals import (
    detect_challenge,
    is_infrastructure,
    looks_js_rendered,
)

try:  # parser XML sicuro (no entity expansion / billion laughs) se disponibile
    from defusedxml import ElementTree as _SafeET
except ImportError:  # pragma: no cover
    _SafeET = None

logger = logging.getLogger(__name__)

# Estensioni che NON hanno senso scaricare come "pagina da esplorare per
# link": file binari, media, stream. Prima di questo filtro il crawler
# provava a fare una GET su QUALUNQUE url interno trovato, PDF e stream
# audio inclusi — causa concreta di un blocco di ore su uno stream audio
# live (es. Shoutcast senza Content-Type dichiarato, che nessun controllo
# A RUNTIME riesce a intercettare in modo davvero affidabile: è molto più
# robusto non tentare proprio la richiesta). Il controllo è sulla sola
# ESTENSIONE nell'URL, prima di qualunque chiamata di rete: costo zero,
# nessuna dipendenza dal comportamento (a volte non standard) del server.
NON_CRAWLABLE_EXTENSIONS = frozenset({
    # audio
    ".mp3", ".m4a", ".wav", ".flac", ".aac", ".wma", ".ogg", ".oga", ".opus", ".mid", ".midi",
    # video
    ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mkv", ".webm", ".flv", ".3gp", ".ts",
    # playlist/stream
    ".m3u", ".m3u8", ".pls", ".asx", ".xspf",
    # documenti/binari (non pagine da esplorare per nuovi link, anche se
    # legittimi: PDF/DOCX ecc. sono già gestiti come RISORSE, non pagine,
    # dal resto della pipeline — vedi _extract_resource_refs)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".dmg", ".apk", ".iso",
    # immagini/font/altri asset statici
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".ico", ".tiff",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot", ".otf",
})


def _estensione_da_evitare(url: str) -> bool:
    """
    True se il path dell'url termina con un'estensione in
    NON_CRAWLABLE_EXTENSIONS (case-insensitive) — cioè non ha senso
    scaricarlo come pagina da cui estrarre link, indipendentemente da cosa
    dichiarerebbe poi il server in risposta.
    """
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in NON_CRAWLABLE_EXTENSIONS)

def _root_domain(url_or_host: str) -> str:
    """
    Dominio radice (eTLD+1) di un URL o hostname (Public Suffix List, via
    tldextract): 'finances.belgium.be' -> 'belgium.be', '.co.uk' gestito.
    Ritorna '' per indirizzi IP letterali e host senza suffisso valido.
    """
    return root_domain_of(url_or_host)


# Pattern di default per riconoscere una paginazione numerica in un URL:
# 'page_2', 'page-2', 'page/2', 'page=2', 'pagina_2', 'p=2', '/p/2'.
# Deve avere ESATTAMENTE un gruppo di cattura: il numero di pagina.
# Il lookbehind negativo evita falsi positivi su parole che finiscono
# "per caso" in '...p' seguito da un separatore e un numero (es.
# 'shop=2', 'group-2'): richiede che 'page'/'pagina'/'p' non sia preceduto
# da un carattere alfanumerico (cioè sia a inizio URL o preceduto da uno
# fra '/ ? & . - _').
DEFAULT_PAGINATION_REGEX = re.compile(
    r"(?<![a-zA-Z0-9])(?:page|pagina|p)[-_/=](\d+)(?=(?:[/?&#]|$))",
    re.IGNORECASE,
)

# ccTLD di ciascun paese coperto da config/countries/*.yaml (codice paese
# come usato nei nomi dei file YAML -> Top-Level Domain nazionale, senza
# punto). Nella quasi totalità dei casi coincide col codice paese in
# minuscolo; eccezione nota: EL (Grecia, codice EU) usa il ccTLD '.gr' (non
# '.el'). Usata come default di 'allowed_tlds' in SiteCrawlFetcher quando
# il config del paese non lo specifica esplicitamente.
@dataclass
class PageResult:
    """Esito del lavoro di un worker su UNA pagina (fetch + estrazione). Il worker NON tocca lo
    stato condiviso: lo applica il thread coordinatore (nessun lock sulle strutture del crawl)."""
    page_url: str
    dom: str
    net_exc: BaseException | None = None          # errore di rete/HTTP/challenge (RequestException)
    internal_exc: BaseException | None = None     # bug interno (NON di rete)
    internal_tb: str = ""
    content_exc: BaseException | None = None      # errore nell'estrazione dei link (contenuto)
    robots_blocked: str = ""                      # motivo, se robots.txt vieta la pagina
    crawl_delay: float = 0.0
    tls_error: bool = False
    final_url: str = ""
    final_host: str = ""
    content_type: str = ""
    html_ok: bool = False
    skip_extraction: bool = False
    links_found: list = field(default_factory=list)
    resource_refs: list = field(default_factory=list)
    needs_js: bool = False
    used_sitemap: bool = False


class HostScheduler:
    """Coda di un livello organizzata PER HOST con giro round-robin: chi pesca sceglie la prossima
    pagina il cui host è "pronto" (non occupato, non in pausa, pausa per-host trascorsa) senza
    scandire tutta la coda. Mantiene l'ordine interno di ciascun host."""

    def __init__(self, urls: list[str], domain_of: Any):
        self._domain_of = domain_of
        self._q: dict[str, deque[str]] = {}
        self._ring: deque[str] = deque()
        self._n = 0
        for u in urls:
            self.push(u)

    def __len__(self) -> int:
        return self._n

    def push(self, url: str) -> None:
        h = self._domain_of(url) or ""
        q = self._q.get(h)
        if q is None:
            q = self._q[h] = deque()
            self._ring.append(h)
        elif not q and h not in self._ring:
            self._ring.append(h)
        q.append(url)
        self._n += 1

    def pop_ready(self, is_ready: Any) -> str | None:
        for _ in range(len(self._ring)):
            h = self._ring.popleft()
            q = self._q.get(h)
            if not q:
                self._q.pop(h, None)
                continue
            if is_ready(h):
                url = q.popleft()
                self._n -= 1
                if q:
                    self._ring.append(h)     # in fondo: giro equo tra gli host
                else:
                    self._q.pop(h, None)
                return url
            self._ring.append(h)
        return None

    def hosts(self) -> list[str]:
        return [h for h, q in self._q.items() if q]

    def all_urls(self) -> list[str]:
        return [u for q in self._q.values() for u in q]


class ChallengePage(requests.RequestException):
    """La risposta è 200 ma è una pagina anti-bot/captcha, non il contenuto del sito."""


COUNTRY_TLDS: dict[str, str] = {
    "AT": "at", "BE": "be", "BG": "bg", "CY": "cy", "CZ": "cz", "DE": "de",
    "DK": "dk", "EE": "ee", "EL": "gr", "ES": "es", "FI": "fi", "FR": "fr",
    "HR": "hr", "HU": "hu", "IE": "ie", "IT": "it", "LT": "lt", "LU": "lu",
    "LV": "lv", "MT": "mt", "NL": "nl", "PL": "pl", "PT": "pt", "RO": "ro",
    "SE": "se", "SI": "si", "SK": "sk",
}

# TLD "regionali/di città" realmente usati da enti pubblici di quel paese e
# che NON sono il ccTLD: senza questi, domini come 'be.brussels', 'gencat.cat',
# 'berlin.de' (questo sì .de) o 'amsterdam' venivano trattati come esterni e
# non esplorati. Vengono uniti ad 'allowed_tlds' (disattivabile con
# 'regional_tlds: false' nel config del paese). NB: '.eu' NON è incluso di
# proposito: farebbe esplorare tutte le istituzioni UE a ogni crawl nazionale.
REGIONAL_TLDS: dict[str, tuple[str, ...]] = {
    "AT": ("wien", "tirol"),
    "BE": ("brussels", "vlaanderen", "gent"),
    "DE": ("berlin", "bayern", "hamburg", "koeln", "cologne", "nrw", "ruhr", "saarland"),
    "ES": ("cat", "eus", "gal"),
    "FR": ("bzh", "paris", "alsace", "corsica"),
    "IE": ("irish",),
    "NL": ("amsterdam", "frl"),
}

# Marcatori di "settore pubblico" usati come label di secondo livello sotto un
# ccTLD (gov.pl, gob.es, gouv.fr, gv.at, gov.ie...). Vedi _tld_matches.
GOV_SECOND_LEVEL_LABELS = frozenset({"gov", "gob", "gouv", "gv", "govt", "government", "overheid"})


# Encoding "legacy" più probabile per i file di un paese quando NON sono UTF-8. Il rilevamento
# statistico è inaffidabile sui file piccoli (cp1250 e cp1252 si somigliano molto: 'ą' diventava
# '¹'), quindi si prova prima l'encoding tipico del paese.
COUNTRY_LEGACY_ENCODINGS: dict[str, str] = {
    "PL": "cp1250", "CZ": "cp1250", "SK": "cp1250", "HU": "cp1250", "HR": "cp1250", "SI": "cp1250",
    "RO": "cp1250", "BG": "cp1251", "EL": "cp1253", "LT": "cp1257", "LV": "cp1257", "EE": "cp1257",
}


def _decode_with_fallback(
    raw: bytes, configured: str, context: str, country: str | None = None, extra: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Decodifica 'raw' provando prima l'encoding configurato in modo STRETTO. Se fallisce (es.
    file cp1250 dichiarato utf-8, tipico PL/CZ/HU/HR/RO) prova, nell'ordine: 'encoding_fallbacks'
    del config, l'encoding legacy tipico del paese, il rilevamento con charset-normalizer, cp1252.
    Solo come ultima spiaggia usa errors='replace', segnalando quanti caratteri sono stati persi
    (prima la sostituzione con U+FFFD era SILENZIOSA e irreversibile)."""
    try:
        return raw.decode(configured), configured
    except (UnicodeDecodeError, LookupError):
        pass
    candidates: list[str] = list(extra)
    if country and country.upper() in COUNTRY_LEGACY_ENCODINGS:
        candidates.append(COUNTRY_LEGACY_ENCODINGS[country.upper()])
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw[:2_000_000]).best()
        if best is not None and best.encoding:
            candidates.append(best.encoding)
    except Exception:  # noqa: BLE001
        pass
    candidates += ["cp1252", "iso-8859-2", "iso-8859-1"]
    for enc in dict.fromkeys(candidates):
        try:
            text = raw.decode(enc)
            logger.warning("%s: decodifica come '%s' fallita, uso '%s'", context, configured, enc)
            return text, enc
        except (UnicodeDecodeError, LookupError):
            continue
    text = raw.decode("utf-8", errors="replace")
    logger.error("%s: nessun encoding valido, %d caratteri sostituiti con U+FFFD", context, text.count("\ufffd"))
    return text, "utf-8(replace)"


class BulkCSVFetcher(BaseFetcher):
    """source_type: bulk_csv. Config: url, delimiter (default ';'), encoding."""

    def download(self) -> Path:
        url = self.config["source_url"]
        out = self.input_dir / self.config.get("input_filename", "source.csv")
        resp = self.http.get(url)
        out.write_bytes(resp.content)
        logger.info("[%s] scaricato %s -> %s", self.country_code, url, out)
        return out

    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        delimiter = self.config.get("delimiter", ";")
        configured = self.config.get("encoding", "utf-8-sig")
        raw = raw_path.read_bytes()
        text, used = _decode_with_fallback(
            raw, configured, f"[{self.country_code}] {raw_path.name}", self.country_code,
            tuple(self.config.get("encoding_fallbacks") or ()),
        )
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
        return [dict(row) for row in reader]


class BulkXLSXFetcher(BaseFetcher):
    """source_type: bulk_xlsx. Config: url, sheet_name (opz.), header_row (opz., 0-based)."""

    def download(self) -> Path:
        url = self.config["source_url"]
        out = self.input_dir / self.config.get("input_filename", "source.xlsx")
        resp = self.http.get(url)
        out.write_bytes(resp.content)
        logger.info("[%s] scaricato %s -> %s", self.country_code, url, out)
        return out

    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        try:
            import openpyxl
        except ImportError as exc:
            raise RuntimeError(
                "openpyxl non installato: eseguire 'pip install openpyxl'"
            ) from exc

        wb = openpyxl.load_workbook(raw_path, read_only=True, data_only=True)
        sheet_name = self.config.get("sheet_name")
        ws = wb[sheet_name] if sheet_name else wb.active

        header_row_idx = self.config.get("header_row", 0)
        rows_iter = ws.iter_rows(values_only=True)
        for _ in range(header_row_idx):
            next(rows_iter)
        headers = [str(h).strip() if h is not None else "" for h in next(rows_iter)]

        records = []
        for row in rows_iter:
            if row is None or all(v is None for v in row):
                continue
            records.append({headers[i]: row[i] for i in range(min(len(headers), len(row)))})
        return records


class BulkXMLFetcher(BaseFetcher):
    """
    source_type: bulk_xml. Config:
      - url
      - record_xpath: xpath (ElementTree syntax) dell'elemento-record, es. './/organisatie'
      - field_xpaths: {output_or_raw_key: xpath relativo al record, opzionale @attributo}
    Se field_xpaths è assente, ogni figlio diretto del record diventa una chiave raw
    (tag -> testo), pronta per il field_mapping standard.
    """

    def download(self) -> Path:
        url = self.config["source_url"]
        out = self.input_dir / self.config.get("input_filename", "source.xml")
        resp = self.http.get(url)
        out.write_bytes(resp.content)
        logger.info("[%s] scaricato %s -> %s", self.country_code, url, out)
        return out

    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        tree = (_SafeET or ET).parse(raw_path)
        root = tree.getroot()
        record_xpath = self.config["record_xpath"]
        field_xpaths: dict[str, str] = self.config.get("field_xpaths", {})

        records = []
        for el in root.findall(record_xpath):
            if field_xpaths:
                row = {}
                for key, xp in field_xpaths.items():
                    if xp.startswith("@"):
                        row[key] = el.get(xp[1:])
                    else:
                        found = el.find(xp)
                        row[key] = found.text.strip() if found is not None and found.text else None
                records.append(row)
            else:
                row = {child.tag: (child.text.strip() if child.text else None) for child in el}
                records.append(row)
        return records


class ApiJsonFetcher(BaseFetcher):
    """
    source_type: api_json. Config:
      - url (può contenere {page}/{offset} per paginazione semplice)
      - records_path: dotted path nel JSON dove si trova la lista di record
        (es. 'results.items'); vuoto se la root è già la lista
      - paginate: bool
      - page_param / page_size_param / page_size / max_pages (se paginate=true)
    """

    def download(self) -> Path:
        out = self.input_dir / self.config.get("input_filename", "source.json")
        all_records: list[Any] = []

        if self.config.get("paginate"):
            page = self.config.get("start_page", 1)
            max_pages = self.config.get("max_pages", 50)
            page_param = self.config.get("page_param", "page")
            page_size_param = self.config.get("page_size_param")
            page_size = self.config.get("page_size", 100)
            seen_pages: set[str] = set()
            ended_cleanly = False

            for _ in range(max_pages):
                params = {page_param: page}
                if page_size_param:
                    params[page_size_param] = page_size
                resp = self.http.get(self.config["source_url"], params=params)
                data = resp.json()
                batch = _dig(data, self.config["records_path"]) if self.config.get("records_path") else data
                if not batch:
                    ended_cleanly = True
                    break
                # Un'API che ignora il parametro di pagina restituisce sempre gli
                # stessi record: senza questo controllo si ripeteva fino a max_pages.
                sig = hashlib.md5(json.dumps(batch, sort_keys=True, default=str).encode("utf-8")).hexdigest()
                if sig in seen_pages:
                    logger.warning(
                        "[%s] la pagina %s è identica a una già ricevuta: l'API probabilmente ignora "
                        "'%s'. Mi fermo con %d record (verifica page_param/config).",
                        self.country_code, page, page_param, len(all_records),
                    )
                    ended_cleanly = True
                    break
                seen_pages.add(sig)
                all_records.extend(batch)
                # 'meno record del page_size' significa "ultima pagina" SOLO se
                # abbiamo davvero chiesto quel page_size (page_size_param impostato):
                # altrimenti l'API usa il suo default (es. 20) e ci si fermava alla 1ª pagina.
                if page_size_param and len(batch) < page_size:
                    ended_cleanly = True
                    break
                page += 1
            if not ended_cleanly:
                logger.warning(
                    "[%s] raggiunto max_pages=%d senza una pagina vuota/finale: i dati potrebbero essere "
                    "TRONCATI (%d record scaricati). Alza 'max_pages' nel config.",
                    self.country_code, max_pages, len(all_records),
                )
            expected = self.config.get("expected_min_records")
            if expected and len(all_records) < int(expected):
                logger.warning(
                    "[%s] scaricati %d record, meno dei %s attesi (expected_min_records)",
                    self.country_code, len(all_records), expected,
                )
        else:
            resp = self.http.get(self.config["source_url"])
            data = resp.json()
            all_records = _dig(data, self.config["records_path"]) if self.config.get("records_path") else data

        out.write_text(json.dumps(all_records, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("[%s] %d record JSON scaricati -> %s", self.country_code, len(all_records), out)
        return out

    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        return json.loads(raw_path.read_text(encoding="utf-8"))


class HtmlScrapeFetcher(BaseFetcher):
    """
    source_type: html_scrape. Config:
      - url (o list_urls: [...] per più pagine indice)
      - list_selector: selettore CSS dell'elemento che racchiude ogni singolo ente
      - field_selectors: {raw_key: selettore CSS relativo a list_selector}
        usare suffisso '::attr(href)' o '::attr(X)' per estrarre un attributo
        invece del testo, es. "a::attr(href)".
    NOTA: i selettori vanno adattati ispezionando l'HTML reale del sito (DevTools
    del browser) — quelli nei config di partenza sono placeholder da verificare.
    """

    def download(self) -> Path:
        try:
            from bs4 import BeautifulSoup  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("beautifulsoup4 non installato: 'pip install beautifulsoup4'") from exc

        urls = self.config.get("list_urls") or [self.config["source_url"]]
        out = self.input_dir / self.config.get("input_filename", "source.html")
        html_chunks = []
        for url in urls:
            resp = self.http.get(url)
            html_chunks.append(f"<!-- SOURCE: {url} -->\n" + resp.text)
        out.write_text("\n".join(html_chunks), encoding="utf-8")
        logger.info("[%s] scaricate %d pagine HTML -> %s", self.country_code, len(urls), out)
        return out

    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        from bs4 import BeautifulSoup

        html = raw_path.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")

        list_selector = self.config["list_selector"]
        field_selectors: dict[str, str] = self.config.get("field_selectors", {})

        records = []
        for item in soup.select(list_selector):
            row = {}
            for key, sel in field_selectors.items():
                attr = None
                if "::attr(" in sel:
                    sel, attr_part = sel.split("::attr(")
                    attr = attr_part.rstrip(")")
                sel = sel.strip()
                target = item.select_one(sel) if sel else item
                if target is None:
                    row[key] = None
                elif attr:
                    row[key] = target.get(attr)
                else:
                    row[key] = target.get_text(strip=True)
            records.append(row)
        return records


class BulkXlsxFolderFetcher(BaseFetcher):
    """
    source_type: bulk_xlsx_folder

    Importa TUTTI i file .xlsx presenti in data/input/{ISO}/ (nessun
    download: i file li mette manualmente l'utente nella cartella), a
    condizione che condividano la stessa struttura (stesso foglio dati,
    stessa riga di partenza dei dati). Utile per fonti come DIR3 (Spagna)
    che si esportano come più file separati con lo stesso schema.

    Config:
      sheet_name: nome del foglio dati (es. "Unidades INST V+T"); se assente
                  usa il foglio attivo di ciascun file.
      data_start_row: prima riga (1-based) con i DATI, cioè subito dopo
                      l'intestazione. Es. se l'intestazione è in riga 2, i
                      dati partono da 3.
      field_mapping: mappa output_field -> LETTERA di colonna Excel (es. "C"),
                     non nome di intestazione — evita ambiguità quando il
                     file ha intestazioni duplicate (comune nei file DIR3).
    """

    def download(self) -> Path:
        # Nessun download: i file li posiziona l'utente in data/input/{ISO}/.
        return self.input_dir

    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        try:
            import openpyxl
        except ImportError as exc:
            raise RuntimeError("openpyxl non installato: 'pip install openpyxl'") from exc

        sheet_name = self.config.get("sheet_name")
        data_start_row = int(self.config.get("data_start_row", 2))

        xlsx_files = sorted(p for p in raw_path.glob("*.xlsx") if not p.name.startswith("~$"))
        if not xlsx_files:
            logger.warning(
                "[%s] nessun file .xlsx trovato in %s — copia lì i file DIR3 da importare",
                self.country_code, raw_path,
            )
            return []

        all_rows: list[dict[str, Any]] = []
        get_letter = openpyxl.utils.get_column_letter
        for f in xlsx_files:
            # read_only + iter_rows: streaming, niente workbook intero in RAM e niente
            # ws.max_row gonfiato dalla formattazione (con i DIR3 grandi il vecchio
            # ciclo cella-per-cella su max_row poteva girare su milioni di righe vuote).
            wb = openpyxl.load_workbook(f, data_only=True, read_only=True)
            try:
                ws = wb[sheet_name] if sheet_name else wb.active
                n_before = len(all_rows)
                for row_vals in ws.iter_rows(min_row=data_start_row, values_only=True):
                    if row_vals is None or all(v is None or (isinstance(v, str) and not v.strip()) for v in row_vals):
                        continue
                    row_dict: dict[str, Any] = {"_source_file": f.name}
                    for c, val in enumerate(row_vals, start=1):
                        row_dict[get_letter(c)] = val
                    all_rows.append(row_dict)
            finally:
                wb.close()
            logger.info("[%s] %s: %d righe importate", self.country_code, f.name, len(all_rows) - n_before)
        return all_rows


class SiteCrawlFetcher(BaseFetcher):
    """
    source_type: site_crawl

    Crawler a livelli: parte da 'seed_urls' e, per 'crawl_depth' livelli,
    apre ogni pagina scoperta cercando i link che contiene ("investigare le
    URL in cerca di altre URL"). Due modalità d'uso:

      A) "Aggregatore -> siti esterni" (es. Germania): i semi sono un
         portale/bacheca che si vuole attraversare SOLO per arrivare ai
         link verso i siti ufficiali esterni. Con 'record_internal_links'
         assente o false (default), in output finiscono SOLO i link verso
         domini fuori da 'allowed_domains' (quelli dentro vengono seguiti
         ma non registrati, per non riempire l'output di link di
         navigazione interni allo stesso portale).

      B) "Rete di siti istituzionali" (es. Belgio): i semi sono già i siti
         di interesse e si vuole scoprire OGNI link che contengono, sia
         verso domini "noti" (stessi semi/sotto-domini, che vengono anche
         esplorati ai livelli successivi) sia verso domini nuovi (che
         vengono registrati ma non esplorati oltre, per restare in un
         perimetro gestibile). Con 'record_internal_links: true', in
         output finiscono ENTRAMBE le categorie, con un campo 'scope' che
         vale "interno" (dominio in allowed_domains) o "esterno".

    Config:
      seed_urls: [url1, url2, ...]                   (livello 0 - fallback,
                       vedi 'seeds_file' qui sotto per il modo preferito)
      seeds_file: "seeds.txt"                         nome del file, dentro
                       data/input/{ISO}/, da cui leggere i semi: UN URL PER
                       RIGA, righe vuote o che iniziano con '#' ignorate. Se
                       il file esiste ha SEMPRE la precedenza su 'seed_urls'
                       — è il modo pensato per cambiare/attivare il crawl su
                       un paese senza toccare lo YAML: basta creare o
                       modificare questo file nella cartella di input.
      allowed_domains: [dominio, ...]                  domini "di casa": le
                       pagine su questi domini (o su un loro SOTTODOMINIO,
                       match per suffisso, es. "belgium.be" copre anche
                       "finances.belgium.be") vengono esplorate anche ai
                       livelli successivi. Default: dominio dei seed.
      allowed_tlds: [tld, ...]                         SECONDA condizione,
                       più ampia, per continuare a esplorare un dominio
                       oltre il livello in cui è stato scoperto (si somma
                       ad 'allowed_domains', non lo sostituisce: un dominio
                       è "interno" se combacia con ALMENO UNA delle due).
                       Motivazione: se un seed (es. belgium.be) punta a un
                       ente non elencato in 'allowed_domains' (es.
                       vlaanderen.be), oggi ci si ferma lì — quel dominio
                       viene solo registrato come scoperta ma non
                       esplorato oltre, quindi eventuali link SUOI verso
                       altri enti (es. singoli comuni) non emergono mai.
                       Con 'allowed_tlds' si accetta di "fidarsi" e
                       continuare a scavare su qualunque dominio che
                       condivida il TLD nazionale o contenga il label
                       'gov' — un'assunzione ragionevole per siti
                       istituzionali, che tipicamente linkano solo altre
                       istituzioni entro il proprio spazio nazionale.
                       Ogni voce è un singolo label (es. "be", "gov":
                       combacia se compare come uno qualsiasi dei label del
                       dominio, quindi copre sia i ccTLD puri sia i domini
                       "gov.<cctld>" come nel caso dell'Irlanda) oppure un
                       suffisso multi-livello con punto (es. "gov.uk":
                       match per suffisso classico). Default se assente dal
                       config: [ccTLD del paese, "gov"] — vedi COUNTRY_TLDS
                       in questo modulo per la tabella dei ccTLD usati.
      crawl_depth: 2                                   quanti livelli di hop
      exclude_domains: [facebook.com, twitter.com, x.com, linkedin.com, ...]
                       domini MAI registrati né seguiti (match per suffisso)
      record_internal_links: false                     vedi modalità A/B sopra
      max_pages_per_level: 100                         tetto di sicurezza
      sleep_seconds: 0.5                               pausa tra le richieste

      follow_pagination: false          Se true, quando durante il crawl si
                       incontra un link che segue un pattern di paginazione
                       numerica (es. '.../page_2', '.../page=3', '?p=4'), NON
                       ci si limita a seguirlo come un normale link interno
                       (soggetto a 'crawl_depth' e senza garanzia di coprire
                       l'intera sequenza): si genera esplicitamente l'intera
                       sequenza di pagine (page_1, page_2, page_3, ...),
                       la si scarica pagina per pagina, e da OGNUNA si
                       estraggono tutti i link annidati (con la stessa
                       logica interno/esterno/exclude_domains del resto del
                       crawler), indipendentemente da 'crawl_depth'. Utile
                       per elenchi/registri paginati dove i risultati oltre
                       la prima pagina altrimenti rischiano di non essere mai
                       raggiunti (dipende da quanti link "successivo" ci
                       sono annidati entro la profondità configurata).
      pagination_regex: null            Regex custom (un solo gruppo di
                       cattura, il numero di pagina) per riconoscere il
                       pattern, es. '(?:page|pagina)[-_=/]?(\\d+)'. Se
                       assente si usa un default che copre i pattern più
                       comuni: 'page_2', 'page-2', 'page/2', 'page=2',
                       'pagina_2', '?p=2', '/p/2'.
      pagination_start_page: 1          Numero della prima pagina della
                       sequenza (alcuni siti partono da 0).
      pagination_max_pages: 300         Tetto di sicurezza sul numero di
                       pagine generate per ciascuna sequenza individuata
                       (evita loop infiniti su siti con paginazione "senza
                       fine" o parametri che restano sempre validi).
      pagination_stop_after_empty: 2    Numero di pagine consecutive senza
                       nuovi link interni utili (0 link nuovi, oppure
                       contenuto HTML identico alla pagina precedente — es.
                       il sito continua a rispondere con l'ultima pagina
                       valida per numeri oltre la fine) dopo cui si
                       interrompe quella sequenza, assumendo di aver
                       superato l'ultima pagina reale.
      pagination_sleep_seconds: null    Pausa tra le richieste di pagine di
                       paginazione; se assente usa 'sleep_seconds'.
      domain_cooldown_after_errors: 1   Quante volte DI FILA un dominio deve
                       dare errore prima di essere considerato "in blocco"
                       e messo in pausa (vedi sotto). 1 = alla prima.
      domain_cooldown_calls: 30         Quante chiamate ad ALTRI domini
                       aspettare prima di ritentare un dominio in pausa. Si
                       resta sullo stesso livello nel frattempo: non si
                       passa al livello successivo solo per far passare il
                       tempo di pausa di un dominio.
      domain_cooldown_max_retries: 3    Quante volte al massimo un dominio
                       può essere rimesso in pausa e ritentato prima di
                       rinunciare definitivamente (evita di rallentare
                       indefinitamente un livello per un dominio morto/
                       bloccato in modo permanente, non solo rate-limit).

    OUTPUT A LIVELLO DI DOMINIO RADICE: l'output non è una lista di URL
    scoperti, ma la lista dei DOMINI RADICE (eTLD+1, es. 'finances.
    belgium.be' -> 'belgium.be') coinvolti, SENZA DUPLICATI — un dominio
    compare una sola volta anche se scoperto su più pagine/URL diversi.
    La lista include anche i domini radice dei 'seed_urls'/'seeds_file'
    stessi (con scope="seed"), non solo quelli scoperti durante il crawl.

    ROBOTS.TXT E BUONE MANIERE: con 'respect_robots: true' (DEFAULT) ogni host viene
    interrogato una volta per il suo robots.txt (divieti, caratteri jolly e Crawl-delay
    rispettati, vedi src/polite.py); con 'respect_robots: false' nessun robots.txt viene letto
    (scelta esplicita e responsabilità di chi esegue il crawl). Con 'identify_crawler: true'
    (default) lo User-Agent è "eu-pa-scraper/1.0 (+contatto)" (contatto da 'contact' o dalla
    variabile SCRAPER_CONTACT). Restano da verificare i termini d'uso dei singoli portali.

    VELOCITÀ: 'workers' (default 8) pagine in parallelo; 'sleep_seconds' è la pausa minima
    tra due richieste allo STESSO host (host diversi vanno in parallelo);
    'max_concurrent_per_host' (default 1). Lo stato è salvato in modo incrementale in
    data/input/{ISO}/crawl_state.sqlite (poche righe per pagina, non più un JSON riscritto
    per intero); il JSON storico crawl_checkpoint.json viene esportato a fine livello, a fine
    crawl e all'interruzione (è ciò che legge tools/postprocess_checkpoints.py).

    --resume E REPORT DI PROGRESSO: vedi il blocco di commenti sopra
    _checkpoint_path()/_write_progress_report() più sotto in questo file, e
    la sezione dedicata in README.md, per i dettagli su come riprendere un
    crawl interrotto (data/input/{ISO}/crawl_state.sqlite, a grana di
    singola pagina) e su come leggere l'avanzamento per singolo seed e
    livello (data/input/{ISO}/crawl_progress.json, che resta anche a fine
    crawl come riepilogo leggibile).

    RESILIENZA (il crawl non si ferma più per un singolo intoppo):
      - Alternanza anti-blocco (vedi 'domain_cooldown_*' più sotto): un
        dominio che dà errori ripetuti viene messo in pausa e ritentato più
        avanti, invece di essere abbandonato al primo errore.
      - Markup non valido, encoding inatteso o un singolo link malformato
        su una pagina NON fanno più fallire l'intero crawl (vedi
        _extract_links): la pagina in questione contribuisce 0 link ed è
        registrata come errore di contenuto nel report, il crawl prosegue.
      - Un problema nello scrivere il checkpoint o il report (visto su
        Windows: antivirus/OneDrive che tengono per un istante il file in
        mano) non è più fatale: si ritenta un paio di volte poi si
        prosegue comunque con un avviso.
      - data/input/{ISO}/crawl_report.md: riepilogo leggibile in Markdown
        (pagine scraped, per seed, domini in pausa/da ritentare, elenco
        errori con esito) aggiornato a ogni livello, interruzione, e a
        fine crawl — non viene mai cancellato.

    Righe grezze prodotte (una per dominio radice unico, compatibili con
    'field_mapping' standard):
        {"external_url": "https://<dominio_radice>", "external_domain": <dominio_radice>,
         "found_on": ..., "level": ..., "scope": "seed"|"interno"|"esterno"}
    """

    def _domain_of(self, url: str) -> str:
        """Host normalizzato (minuscolo, punycode, senza porta/userinfo)."""
        return host_of(url)

    @staticmethod
    def _domain_matches(dom: str, domain_list: set[str]) -> bool:
        """Match per suffisso: 'finances.belgium.be' combacia con 'belgium.be'."""
        return any(dom == d or dom.endswith("." + d) for d in domain_list)

    @staticmethod
    def _tld_matches(dom: str, tlds: set[str]) -> bool:
        """
        Vero se 'dom' appartiene a uno dei TLD indicati (usato per
        'allowed_tlds'). Il confronto è SOLO sul suffisso, mai su un label
        qualsiasi (prima 'de.wikipedia.org', 'be.linkedin.com' e
        'gov.sito-qualsiasi.com' risultavano "interni" e il crawl usciva dal
        perimetro della PA). Ogni voce può essere:
          - un label senza punto (es. 'be', 'nrw', 'brussels'): combacia se è
            l'ULTIMO label del dominio (il TLD);
          - un marcatore di settore pubblico (es. 'gov', 'gob', 'gouv', 'gv'):
            combacia anche se è il PENULTIMO label sotto un ccTLD di due
            lettere ('revenue.gov.ie', 'sede.gob.es', 'x.gouv.fr') — ma non
            se il TLD è generico ('gov.evil.com' NON combacia);
          - un suffisso multi-livello con punto (es. 'gov.uk'): match per suffisso.
        """
        d = (dom or "").lower().rstrip(".")
        labels = d.split(".")
        if not d or len(labels) < 2:
            return False
        for raw in tlds:
            t = raw.lower().lstrip(".")
            if not t:
                continue
            if "." in t:
                if d == t or d.endswith("." + t):
                    return True
            elif labels[-1] == t:
                return True
            elif t in GOV_SECOND_LEVEL_LABELS and len(labels) >= 3 and labels[-2] == t and len(labels[-1]) == 2:
                return True
            elif t in GOV_SECOND_LEVEL_LABELS and len(labels) == 2 and labels[0] == t and len(labels[1]) == 2:
                return True  # 'gov.pl' stesso
        return False

    def _load_seeds(self) -> list[str]:
        """
        Carica i semi del crawl, in ordine di priorità:
          1) file 'seeds_file' (default 'seeds.txt') dentro data/input/{ISO}/,
             se esiste e contiene almeno un URL valido — QUESTO è il modo
             pensato per attivare/cambiare il crawl su un paese: basta
             creare o modificare questo file, senza toccare lo YAML;
          2) altrimenti, 'seed_urls' definito nel config YAML (comodo per un
             primo setup o come esempio/documentazione);
          3) se nessuno dei due è disponibile, errore chiaro con le
             istruzioni su cosa creare.
        """
        seeds_filename = self.config.get("seeds_file", "seeds.txt")
        seeds_path = self.input_dir / seeds_filename

        if seeds_path.exists():
            urls = []
            for line in seeds_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                urls.append(line)
            if urls:
                logger.info("[%s] semi caricati da %s: %d URL", self.country_code, seeds_path, len(urls))
                return urls
            logger.warning(
                "[%s] %s esiste ma non contiene URL validi (righe vuote o solo commenti): "
                "provo con 'seed_urls' da config YAML",
                self.country_code, seeds_path,
            )

        yaml_seeds = self.config.get("seed_urls")
        if yaml_seeds:
            logger.info("[%s] semi caricati da 'seed_urls' nel config YAML: %d URL", self.country_code, len(yaml_seeds))
            return list(yaml_seeds)

        raise ValueError(
            f"Nessun seed trovato per il crawl di {self.country_code}: crea il file "
            f"'{seeds_path}' (un URL per riga, righe vuote o con '#' ignorate) "
            f"oppure aggiungi 'seed_urls' nel config YAML "
            f"(config/countries/{self.country_code}.yaml)."
        )

    # --- checkpoint / --resume -------------------------------------------------
    #
    # Un crawl a livelli su decine di portali può richiedere 30-40 minuti o
    # più (vedi README §5): interromperlo (Ctrl+C, chiusura del terminale,
    # crash di rete) senza un checkpoint significa perdere TUTTO il lavoro
    # fatto fino a quel momento, perché l'unico output veniva scritto in
    # fondo a download(), a crawl completato. Per evitarlo, lo stato del
    # crawl (pagine già visitate, domini già registrati, coda del livello in
    # corso) viene salvato su disco dopo ogni pagina processata (anche
    # durante l'espansione di una sequenza di paginazione, vedi
    # 'checkpoint_cb' in _expand_pagination_sequence): lanciando lo stesso
    # comando con '--resume' si riparte esattamente da lì, pagina per
    # pagina, invece che dal seed iniziale.

    def _checkpoint_path(self) -> Path:
        return self.input_dir / "crawl_checkpoint.json"

    def _checkpoint_fingerprint_fields(self, seeds: list[str]) -> dict[str, Any]:
        """
        Impostazioni che determinano COSA viene esplorato (semi + parametri
        di perimetro/paginazione) — usate per calcolare l'impronta del
        checkpoint. VOLUTAMENTE NON includono parametri che riguardano solo
        COME si gestiscono gli errori (es. 'domain_cooldown_*'): cambiare
        quelli tra un run e l'altro (o aggiornare il tool con nuovi
        parametri di questo tipo) non rende inconsistente ciò che è già
        stato scoperto, quindi non deve invalidare un checkpoint esistente.
        """
        return {
            "seeds": sorted(seeds),
            "crawl_depth": self.config.get("crawl_depth", 2),
            "allowed_domains": sorted(self.config.get("allowed_domains", [])),
            "allowed_tlds": sorted(self.config.get("allowed_tlds", [])),
            "exclude_domains": sorted(self.config.get("exclude_domains", [])),
            "record_internal_links": bool(self.config.get("record_internal_links", False)),
            "follow_pagination": bool(self.config.get("follow_pagination", False)),
            "pagination_regex": self.config.get("pagination_regex"),
        }

    def _checkpoint_fingerprint(self, seeds: list[str]) -> str:
        """
        "Impronta" delle impostazioni che determinano COSA viene esplorato.
        Se cambia rispetto al checkpoint salvato — es. l'utente ha
        modificato 'crawl_depth' o 'allowed_domains' tra un run e l'altro —
        il checkpoint non è più affidabile (rispecchierebbe una
        configurazione diversa) e va ignorato invece di essere usato per
        riprendere alla cieca.
        """
        blob = json.dumps(self._checkpoint_fingerprint_fields(seeds), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _log_fingerprint_mismatch(self, seeds: list[str], old_fields: dict[str, Any] | None) -> None:
        """
        Diagnostica leggibile del PERCHÉ un checkpoint è stato invalidato:
        invece del solo "non corrisponde più", elenca esattamente quali
        impostazioni sono cambiate rispetto a quando è stato salvato.
        """
        if not old_fields:
            logger.warning(
                "[%s] il checkpoint non contiene le impostazioni con cui era stato salvato (formato di "
                "una versione precedente del tool): impossibile determinare cos'è cambiato, lo ignoro",
                self.country_code,
            )
            return
        new_fields = self._checkpoint_fingerprint_fields(seeds)
        diffs = []
        for key in sorted(set(old_fields) | set(new_fields)):
            old_val, new_val = old_fields.get(key, "<assente>"), new_fields.get(key, "<assente>")
            if old_val != new_val:
                diffs.append(f"'{key}': {old_val!r} -> {new_val!r}")
        if diffs:
            logger.warning(
                "[%s] impostazioni cambiate rispetto al checkpoint salvato: %s",
                self.country_code, "; ".join(diffs),
            )
        else:
            logger.warning(
                "[%s] le impostazioni risultano identiche ma l'impronta non combacia comunque — "
                "probabile aggiornamento del tool tra un run e l'altro che ha cambiato il formato "
                "interno del checkpoint",
                self.country_code,
            )

    def _store_path(self) -> Path:
        """Database SQLite con lo stato incrementale del crawl (vedi src/crawl_store.py)."""
        return self.input_dir / "crawl_state.sqlite"

    def _open_store(self) -> tuple[CrawlStore, dict[str, Any] | None]:
        """Apre lo store e ne legge lo stato. Se il file SQLite è corrotto (disco pieno, spegnimento
        improvviso, sincronizzazione OneDrive...) viene messo da parte come '*.corrupt-<ora>' e si riparte
        con uno store nuovo (con un errore ben visibile nel log) invece di far fallire il crawl."""
        path = self._store_path()
        try:
            store = CrawlStore(path)
            return store, (store.load() if store.exists_with_data() else None)
        except sqlite3.DatabaseError as exc:
            aside = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            logger.error(
                "[%s] lo stato del crawl %s è corrotto (%s): lo metto da parte in %s e riparto da uno stato vuoto. "
                "Se esiste ancora crawl_checkpoint.json (formato storico) verrà usato per --resume.",
                self.country_code, path, exc, aside.name,
            )
            for suffix in ("", "-wal", "-shm"):
                f = path.with_name(path.name + suffix)
                if f.exists():
                    try:
                        os.replace(f, aside.with_name(aside.name + suffix))
                    except OSError:
                        f.unlink(missing_ok=True)
            return CrawlStore(path), None

    def _load_checkpoint(self) -> dict[str, Any] | None:
        """Checkpoint JSON STORICO (formato delle versioni precedenti, ancora prodotto da store.export_json):
        usato solo se non esiste ancora lo store SQLite."""
        path = self._checkpoint_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "[%s] checkpoint in %s illeggibile/corrotto (%s): lo ignoro e riparto da zero",
                self.country_code, path, exc,
            )
            return None

    def _clear_checkpoint(self) -> None:
        """
        Non più chiamata automaticamente da download(): i checkpoint ora si
        conservano sempre, anche a crawl completato (vedi fondo del
        metodo). Lasciata come utility per una pulizia manuale volontaria,
        se mai servisse forzare un crawl davvero da zero cancellando ogni
        traccia precedente.
        """
        path = self._checkpoint_path()
        if path.exists():
            path.unlink()

    # --- report di progresso per seme + livello ---------------------------
    #
    # Il checkpoint sopra serve al MECCANISMO di --resume (a grana di
    # singola pagina: nessun sito "riparte da zero", nel peggiore dei casi
    # si perde solo la pagina in corso al momento dell'interruzione). Questo
    # report è invece pensato per la VISIBILITÀ: un file leggibile che dice,
    # per ciascun seed di partenza e ciascun livello, quante pagine sono
    # state analizzate e quali domini sono stati trovati — utile per capire
    # a colpo d'occhio a che punto è un run interrotto, senza dover decifrare
    # il checkpoint "tecnico". A differenza del checkpoint, NON viene
    # cancellato a fine crawl: resta come riepilogo finale del run.

    def _progress_report_path(self) -> Path:
        return self.input_dir / "crawl_progress.json"

    def _write_progress_report(
        self,
        *,
        seeds: list[str],
        visited: set[str],
        domain_registry: dict[str, dict[str, Any]],
        pages_by_seed_level: dict[str, dict[str, int]],
        depth: int,
        current_level: int | None,
    ) -> None:
        seeds_report = []
        for seed in seeds:
            seed_domain_entries = [e for e in domain_registry.values() if e.get("source_seed") == seed]
            levels_seen = sorted(
                {int(lvl) for lvl in pages_by_seed_level.get(seed, {})}
                | {e["level"] for e in seed_domain_entries}
            )
            level_reports = []
            for lvl in levels_seen:
                doms = sorted(e["external_domain"] for e in seed_domain_entries if e["level"] == lvl)
                level_reports.append({
                    "level": lvl,
                    "pages_visited": pages_by_seed_level.get(seed, {}).get(str(lvl), 0),
                    "domains_found": len(doms),
                    "domains": doms,
                })
            seeds_report.append({
                "seed": seed,
                "max_level_reached": max(levels_seen) if levels_seen else 0,
                "levels": level_reports,
                "total_domains_found": len({e["external_domain"] for e in seed_domain_entries}),
            })
        report = {
            "country": self.country_code,
            "generated_at": now_iso(),
            "crawl_depth": depth,
            "crawl_completato": current_level is None,
            "total_pages_visited": len(visited),
            "total_domains_unique": len(domain_registry),
            "seeds": seeds_report,
        }
        path = self._progress_report_path()
        tmp_path = path.with_suffix(".tmp")
        try:
            payload = json.dumps(report, ensure_ascii=False, indent=2)
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, path)
        except Exception as exc:  # noqa: BLE001 - non deve MAI far fallire il crawl
            logger.warning(
                "[%s] impossibile scrivere il report di progresso in %s (%s): non è fatale, il crawl "
                "prosegue comunque",
                self.country_code, path, exc,
            )

    def _report_md_path(self) -> Path:
        return self.input_dir / "crawl_report.md"

    def _safe_report(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        """
        Esegue una scrittura di supporto (report di progresso, report
        Markdown) senza MAI propagare eccezioni: sono un aiuto per
        l'utente, non fanno parte della logica di crawling vera e propria,
        e un problema qui (bug interno, valore inatteso, I/O) non deve mai
        poter interrompere il crawl.
        """
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[%s] scrittura di supporto '%s' fallita inaspettatamente (%s): non è fatale, il crawl "
                "prosegue comunque",
                self.country_code, getattr(fn, "__name__", fn), exc,
            )

    def _write_markdown_report(
        self,
        *,
        seeds: list[str],
        visited: set[str],
        domain_registry: dict[str, dict[str, Any]],
        pages_by_seed_level: dict[str, dict[str, int]],
        depth: int,
        queue: list[str],
        domain_cooldown_until_call: dict[str, int],
        domain_cooldown_retries_used: dict[str, int],
        global_call_index: int,
        error_log: list[dict[str, Any]],
        status: str,
    ) -> None:
        """
        Riepilogo in Markdown, pensato per essere aperto e letto a mano
        (a differenza del checkpoint JSON "tecnico"): quante pagine sono
        state scaricate, quanti domini trovati, quali domini sono
        attualmente in pausa per l'alternanza anti-blocco (e quindi in coda
        per essere ritentati) e l'elenco degli errori incontrati con il
        relativo esito. Aggiornato a ogni livello completato, a ogni
        interruzione e a fine crawl — non viene mai cancellato.
        """
        lines: list[str] = []
        lines.append(f"# Report crawl — {self.country_code}")
        lines.append("")
        lines.append(f"- Generato: {now_iso()}")
        lines.append(f"- Stato: **{status}**")
        lines.append("")
        lines.append("## Riepilogo")
        lines.append("")
        lines.append(f"- Pagine analizzate finora: **{len(visited)}**")
        lines.append(f"- Domini radice unici trovati (inclusi i seed): **{len(domain_registry)}**")
        lines.append(f"- Seed di partenza: **{len(seeds)}**")
        lines.append(f"- Livelli configurati (crawl_depth): **{depth}**")
        n_in_cooldown = sum(1 for until in domain_cooldown_until_call.values() if global_call_index < until)
        lines.append(f"- Domini attualmente in pausa (alternanza anti-blocco): **{n_in_cooldown}**")
        lines.append(f"- Pagine ancora in coda nel livello in corso (se interrotto qui): **{len(queue)}**")
        n_definitive = sum(1 for e in error_log if e.get("outcome") == "rinuncia definitiva")
        n_content_errors = sum(1 for e in error_log if "contenuto" in str(e.get("outcome", "")))
        n_retried = len(error_log) - n_definitive - n_content_errors
        lines.append(
            f"- Errori totali registrati: **{len(error_log)}** "
            f"({n_definitive} rinunce definitive, {n_retried} messi in coda/ritentati, "
            f"{n_content_errors} errori di contenuto/parsing)"
        )
        lines.append("")

        lines.append("## Per seed")
        lines.append("")
        lines.append("| Seed | Livello max raggiunto | Domini trovati |")
        lines.append("|---|---|---|")
        for seed in seeds:
            seed_entries = [e for e in domain_registry.values() if e.get("source_seed") == seed]
            levels = {int(lvl) for lvl in pages_by_seed_level.get(seed, {})} | {e["level"] for e in seed_entries}
            max_lvl = max(levels) if levels else 0
            lines.append(f"| {seed} | {max_lvl} | {len(seed_entries)} |")
        lines.append("")

        cooling = sorted(
            (dom, until) for dom, until in domain_cooldown_until_call.items() if global_call_index < until
        )
        if cooling:
            lines.append("## Domini in pausa (alternanza anti-blocco) — verranno ritentati")
            lines.append("")
            lines.append(
                "Questi domini hanno dato errori ripetuti: invece di essere abbandonati subito, sono "
                "stati rimessi in coda per essere ritentati più avanti nello stesso livello."
            )
            lines.append("")
            lines.append("| Dominio | Chiamate ad altri domini mancanti al prossimo tentativo | Tentativi di pausa usati |")
            lines.append("|---|---|---|")
            for dom, until in cooling:
                used = domain_cooldown_retries_used.get(dom, 0)
                lines.append(f"| {dom} | {until - global_call_index} | {used} |")
            lines.append("")

        if error_log:
            lines.append("## Errori")
            lines.append("")
            lines.append("| URL | Livello | Tipo errore | Messaggio | Esito |")
            lines.append("|---|---|---|---|---|")
            shown = error_log[-500:]
            for e in shown:
                msg = str(e.get("message", "")).replace("|", "\\|").replace("\n", " ").strip()
                if len(msg) > 200:
                    msg = msg[:200] + "…"
                lines.append(
                    f"| {e.get('url')} | {e.get('level')} | {e.get('error_type')} | {msg} | {e.get('outcome')} |"
                )
            if len(error_log) > len(shown):
                lines.append("")
                lines.append(
                    f"_(...e altri {len(error_log) - len(shown)} errori più vecchi non mostrati qui — "
                    f"elenco completo in crawl_checkpoint.json finché il crawl non è completato)_"
                )
            lines.append("")

        content = "\n".join(lines) + "\n"
        path = self._report_md_path()
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "[%s] impossibile scrivere il report Markdown in %s (%s): non è fatale, il crawl "
                "prosegue comunque",
                self.country_code, path, exc,
            )

    @staticmethod
    def _extract_links(html: str, page_url: str) -> list[str]:
        """
        Estrae tutti gli URL assoluti dai tag <a href> di una pagina,
        rispettando un eventuale <base href="..."> e scartando link non
        navigabili (javascript:, mailto:, #ancora, tel:). Nessun filtro di
        dominio qui: la classificazione interno/esterno/escluso resta
        a carico del chiamante (_handle_link), così questa estrazione è
        riusabile sia per le pagine normali del crawl sia per le pagine
        generate dall'espansione esplicita della paginazione.

        VOLUTAMENTE MAI SOLLEVA ECCEZIONI: una pagina con markup non
        interpretabile o con un singolo link malformato non deve mai far
        fallire l'intero crawl — nel peggiore dei casi contribuisce 0 link.
          - Markup non valido (visto su siti .gov con export da Word/Office
            che generano dichiarazioni SGML non standard tipo
            '<![IF !mso]>...<![endif]>', che 'html.parser' rifiuta con
            ParserRejectedMarkup, es. "unknown status keyword 'Y' in marked
            section"): si prova prima 'html.parser' (stdlib), poi 'lxml' e
            'html5lib' se installati, molto più tolleranti. Se nessuno dei
            tre riesce, si registra un avviso e si ritorna lista vuota.
          - Un singolo href malformato al punto da mandare in eccezione
            urljoin/urlparse (es. "ValueError: Invalid IPv6 URL" su un link
            tipo 'http://[qualcosa-di-non-valido]'): si scarta SOLO quel
            link, non l'intera pagina.
        """
        from bs4 import BeautifulSoup

        soup = None
        last_exc: Exception | None = None
        for parser_name in ("html.parser", "lxml", "html5lib"):
            try:
                soup = BeautifulSoup(html, parser_name)
                break
            except Exception as exc:  # noqa: BLE001 - qualunque parser puo' rifiutare markup invalido
                last_exc = exc
                continue
        if soup is None:
            logger.warning(
                "markup non interpretabile da nessun parser disponibile su %s (%s): 0 link estratti da "
                "questa pagina, il crawl prosegue",
                page_url, last_exc,
            )
            return []

        # Alcuni siti (spesso CMS con URL a base jsessionid, visto con
        # service.bund.de) dichiarano un tag <base href="..."> che ridefinisce
        # la base per risolvere i link RELATIVI della pagina. Ignorarlo
        # produce URL "innestati" male (es. ".../Suche/Content/DE/Home/
        # pagina.html" invece di ".../Content/DE/Home/pagina.html") che poi
        # rispondono 404.
        base_tag = soup.find("base", href=True)
        try:
            resolve_base = urljoin(page_url, base_tag["href"]) if base_tag else page_url
        except ValueError:
            resolve_base = page_url

        links = []
        for a in soup.select("a[href]"):
            href = a.get("href")
            if not href or href.startswith(("javascript:", "mailto:", "#", "tel:")):
                continue
            try:
                abs_url = urljoin(resolve_base, href)
            except ValueError:
                # es. "Invalid IPv6 URL" per un href scritto male: si scarta
                # solo questo singolo link, mai l'intera pagina.
                continue
            # Filtro per SCHEMA (allowlist, non blocklist): link con schemi
            # "app" (whatsapp://send?..., ms-outlook://compose?..., intent://
            # scan/...) hanno una sintassi "scheme://netloc/..." dove
            # urlparse tratta erroneamente "send"/"compose"/"scan" come se
            # fosse un hostname — finendo registrati come falsi "domini"
            # nell'output (visto in run reali su bundesregierung.de:
            # "compose", "send" tra i domini scoperti). Si segue solo
            # http/https.
            if urlparse(abs_url).scheme not in ("http", "https"):
                continue
            # Forma canonica (fragment/tracking/jsessionid rimossi, host minuscolo):
            # '/p#a', '/p#b', '/p/' e '/p?utm_source=x' sono la STESSA pagina.
            links.append(canonicalize_url(abs_url))
        return links

    # --- estrazione risorse di terze parti (script/iframe/img/link) -------
    #
    # AGGIUNTA (post-processing "link sospetti", vedi tools/postprocess_
    # checkpoints.py): a differenza di _extract_links() sopra (solo
    # <a href>, usata per decidere dove continuare il crawl), questo estrae
    # ANCHE le dipendenze NON navigabili incluse dalla pagina: <script src>,
    # <link href> (CSS/font/preconnect/...), <iframe src>, <img src>.
    # Nessuna richiesta HTTP aggiuntiva: riusa lo stesso HTML già scaricato
    # per _extract_links() nello stesso giro di download() — quindi è
    # "gratis" in termini di costo di rete/tempo, si paga solo un secondo
    # parsing BeautifulSoup dello stesso HTML in memoria. Serve a
    # distinguere, per ogni dominio di terza parte scoperto, SE compare
    # come:
    #   "link"    <a href="...">      semplice link cliccabile (Caso A)
    #   "script"  <script src="...">  dipendenza ESEGUIBILE      (Caso B)
    #   "iframe"  <iframe src="...">  dipendenza EMBEDDED        (Caso C)
    #   "img"     <img src="...">     dipendenza di CONTENUTO    (Caso D)
    #   "link_risorsa" <link href="..."> (CSS/font/preconnect...)
    # distinzione utile a chi deve rivedere a mano un dominio sospetto: uno
    # script di terza parte è un rischio ben diverso da un'immagine o da un
    # semplice link testuale.
    RESOURCE_TAG_ATTR = (
        ("script", "src", "script"),
        ("link", "href", "link_risorsa"),
        ("iframe", "src", "iframe"),
        ("img", "src", "img"),
    )

    def _extract_resource_refs(self, html: str, page_url: str) -> list[tuple[str, str]]:
        """Ritorna una lista di (tipo_riferimento, url_assoluto) per ogni
        <script src>, <link href>, <iframe src>, <img src> della pagina —
        PIÙ i normali <a href> (tipo_riferimento="link"), riusando
        _extract_links() per questi ultimi così la logica su <base href>,
        schemi non-http e link malformati resta unica e coerente.
        VOLUTAMENTE MAI SOLLEVA ECCEZIONI, stessa filosofia di
        _extract_links() (markup non interpretabile o un singolo attributo
        malformato non deve mai far fallire il crawl: nel peggiore dei casi
        contribuisce 0 riferimenti)."""
        from bs4 import BeautifulSoup

        refs: list[tuple[str, str]] = [("link", u) for u in self._extract_links(html, page_url)]

        soup = None
        for parser_name in ("html.parser", "lxml", "html5lib"):
            try:
                soup = BeautifulSoup(html, parser_name)
                break
            except Exception:  # noqa: BLE001 - vedi _extract_links per il razionale
                continue
        if soup is None:
            return refs

        base_tag = soup.find("base", href=True)
        try:
            resolve_base = urljoin(page_url, base_tag["href"]) if base_tag else page_url
        except ValueError:
            resolve_base = page_url

        for tag_name, attr, tipo in self.RESOURCE_TAG_ATTR:
            for tag in soup.find_all(tag_name):
                val = tag.get(attr)
                if not val or val.startswith(("javascript:", "data:", "#", "mailto:", "tel:")):
                    continue
                try:
                    abs_url = urljoin(resolve_base, val)
                except ValueError:
                    continue
                if urlparse(abs_url).scheme not in ("http", "https"):
                    continue
                refs.append((tipo, abs_url))
        return refs

    @staticmethod
    def _looks_like_html(resp: Any) -> bool:
        """
        Controllo leggero PRIMA di dare in pasto il contenuto al parser
        HTML: se il Content-Type dichiara esplicitamente qualcos'altro
        (PDF, immagine, JSON, ...) non ha senso nemmeno provare a
        interpretarlo come markup. Se il Content-Type manca del tutto si
        prova comunque (molti server governativi lo omettono per pagine
        HTML legittime): il fallback multi-parser di _extract_links resta
        comunque la rete di sicurezza finale.
        """
        ctype = resp.headers.get("Content-Type", "")
        if not ctype:
            return True
        ctype = ctype.lower()
        return any(
            t in ctype
            for t in ("text/html", "application/xhtml", "text/xml", "application/xml", "text/plain")
        )

    def _pagination_template(self, url: str, pagination_regex: "re.Pattern[str]") -> str | None:
        """
        Se 'url' combacia con il pattern di paginazione numerica, ritorna il
        TEMPLATE dell'URL con il numero di pagina sostituito da '{}' (es.
        'https://ex.com/elenco/page_3?x=1' -> 'https://ex.com/elenco/page_{}?x=1'),
        pronto per essere usato con .format(n) per generare l'intera
        sequenza. Ritorna None se non c'è match.
        """
        m = pagination_regex.search(url)
        if not m:
            return None
        return url[: m.start(1)] + "{}" + url[m.end(1):]

    def _expand_pagination_sequence(
        self,
        template: str,
        *,
        found_on: str,
        level: int,
        visited: set[str],
        exclude_domains: set[str],
        allowed_domains: set[str],
        allowed_tlds: set[str],
        record_internal: bool,
        register: Any,
        sleep_s: float,
        start_page: int,
        max_pages: int,
        stop_after_empty: int,
        page_origin_seed: dict[str, str],
        origin_seed: str,
        pages_by_seed_level: dict[str, dict[str, int]],
        checkpoint_cb: "Callable[[list[str]], None] | None" = None,
        robots: Any = None,
    ) -> list[str]:
        """
        Genera e scarica ESPLICITAMENTE l'intera sequenza di pagine di un
        pattern di paginazione numerica individuato durante il crawl (es.
        page_1, page_2, page_3, ...), indipendentemente da 'crawl_depth' e
        senza fare affidamento sul fatto che ogni pagina linki la
        successiva entro la profondità configurata. Da OGNI pagina della
        sequenza estrae tutti i link annidati con la stessa logica
        interno/esterno/escluso del resto del crawler.

        Si ferma quando: si raggiunge 'max_pages', oppure per
        'stop_after_empty' pagine consecutive non si trovano nuovi link
        interni utili o il contenuto HTML è identico alla pagina precedente
        (segnale tipico di essere andati oltre l'ultima pagina reale, con
        il sito che ricontinua a rispondere con l'ultimo risultato valido).

        Ritorna la lista dei nuovi URL interni scoperti (da aggiungere al
        livello successivo del crawl), analoga a quanto fa il ciclo
        principale di download() per una singola pagina.
        """
        discovered_internal: list[str] = []
        consecutive_empty = 0
        prev_hash: str | None = None

        for n in range(start_page, start_page + max_pages):
            page_url = template.format(n)
            if page_url in visited:
                continue
            visited.add(page_url)
            if _estensione_da_evitare(page_url):
                logger.debug(
                    "[%s] paginazione: estensione non-pagina, salto senza scaricare: %s",
                    self.country_code, page_url,
                )
                consecutive_empty += 1
                if consecutive_empty >= stop_after_empty:
                    break
                continue
            if robots is not None and not robots.check(page_url).allowed:
                logger.info("[%s] paginazione: %s vietata da robots.txt, salto", self.country_code, page_url)
                consecutive_empty += 1
                if consecutive_empty >= stop_after_empty:
                    break
                continue
            page_origin_seed.setdefault(page_url, origin_seed)
            pages_by_seed_level.setdefault(origin_seed, {})
            lvl_key = str(level)
            pages_by_seed_level[origin_seed][lvl_key] = pages_by_seed_level[origin_seed].get(lvl_key, 0) + 1
            try:
                resp = self.http.get(
                    page_url, max_bytes=3_000_000, skip_non_html=True, read_timeout=8.0,
                    allow_insecure_fallback=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "[%s] paginazione: pagina %d non raggiungibile (%s), probabile fine sequenza: %s",
                    self.country_code, n, page_url, exc,
                )
                consecutive_empty += 1
                if consecutive_empty >= stop_after_empty:
                    break
                continue

            html = resp.text
            content_hash = hashlib.md5(html.encode("utf-8", errors="replace")).hexdigest()

            new_links_here = 0
            page_links = self._extract_links(html, page_url) if self._looks_like_html(resp) else []
            for abs_url in page_links:
                dom = self._domain_of(abs_url)
                if not dom or self._is_excluded(dom, exclude_domains):
                    continue
                is_internal = self._domain_matches(dom, allowed_domains) or self._tld_matches(
                    dom, allowed_tlds
                )
                if is_internal:
                    if abs_url not in visited and abs_url not in discovered_internal:
                        discovered_internal.append(abs_url)
                        new_links_here += 1
                    page_origin_seed.setdefault(abs_url, origin_seed)
                    if record_internal:
                        register(abs_url, "interno", page_url, level)
                else:
                    register(abs_url, "esterno", page_url, level)

            if new_links_here == 0 or content_hash == prev_hash:
                consecutive_empty += 1
            else:
                consecutive_empty = 0
            prev_hash = content_hash

            time.sleep(sleep_s)

            if checkpoint_cb is not None:
                # Salva lo stato anche a metà di una sequenza di
                # paginazione (che da sola può contenere centinaia di
                # pagine, vedi 'pagination_max_pages'): senza questo,
                # un'interruzione durante una sequenza lunga perderebbe
                # tutto il progresso fatto su di essa.
                checkpoint_cb(discovered_internal)

            if consecutive_empty >= stop_after_empty:
                logger.info(
                    "[%s] paginazione: sequenza terminata a pagina %d (%d pagine senza novità) per template %s",
                    self.country_code, n, consecutive_empty, template,
                )
                break
        else:
            logger.warning(
                "[%s] paginazione: raggiunto il tetto di sicurezza pagination_max_pages=%d per il template %s "
                "senza un chiaro segnale di fine sequenza — aumentalo se la sequenza è più lunga",
                self.country_code, max_pages, template,
            )

        return discovered_internal

    # --- helper del crawl ------------------------------------------------------
    _use_infra: bool = True

    def _is_excluded(self, dom: str, exclude_domains: set[str]) -> bool:
        """Dominio da non registrare né seguire: 'exclude_domains' del config +
        (se 'use_default_infra_blocklist', default true) i domini infrastrutturali
        noti (CDN, social, shortener, store di app, standard web...)."""
        if self._domain_matches(dom, exclude_domains):
            return True
        return self._use_infra and is_infrastructure(dom)

    @staticmethod
    def _mark_visited(visited: set[str], page_url: str, final_url: str | None = None) -> None:
        """Segna come visitata la pagina nella forma richiesta, in quella canonica e
        in quella finale dopo eventuali redirect (evita di riscaricarla da un link
        scritto in modo leggermente diverso)."""
        visited.add(page_url)
        visited.add(canonicalize_url(page_url))
        if final_url:
            visited.add(final_url)
            visited.add(canonicalize_url(final_url))

    def _select_for_level(self, urls: list[str], cap: int, level: int) -> list[str]:
        """Sceglie fino a 'cap' URL da visitare al livello 'level'. Se ce ne sono
        di più, la selezione è round-robin PER HOST (prima ~ogni sito ha una pagina,
        poi la seconda...) invece di prendere i primi 'cap' in ordine di scoperta,
        che faceva monopolizzare il livello dai primi seed. Gli scartati vengono
        loggati (prima sparivano in silenzio)."""
        urls = list(urls)
        if cap <= 0 or len(urls) <= cap:
            return urls
        buckets: dict[str, deque[str]] = {}
        for u in urls:
            buckets.setdefault(self._domain_of(u) or "", deque()).append(u)
        selected: list[str] = []
        order = list(buckets)
        while len(selected) < cap and order:
            still: list[str] = []
            for h in order:
                selected.append(buckets[h].popleft())
                if len(selected) >= cap:
                    break
                if buckets[h]:
                    still.append(h)
            order = still
        logger.warning(
            "[%s] livello %d: %d URL scoperte, ne visito %d (max_pages_per_level): %d scartate. "
            "Selezione round-robin su %d host. Alza 'max_pages_per_level' per coprirle tutte.",
            self.country_code, level, len(urls), len(selected), len(urls) - len(selected), len(buckets),
        )
        return selected

    def _sitemap_links(self, page_url: str, limit: int = 200) -> list[str]:
        """URL da /sitemap.xml dello stesso host (un solo livello di sitemap-index,
        max 3 sotto-sitemap). Usato solo come ripiego per siti JS-rendered.
        Mai solleva eccezioni."""
        try:
            p = urlparse(page_url)
            base = f"{p.scheme}://{p.netloc}"
            xml_parser = _SafeET or ET

            def _fetch_locs(u: str) -> tuple[str | None, list[str]]:
                try:
                    r = self._thread_http().get(u, max_bytes=2_000_000, read_timeout=8.0, allow_insecure_fallback=True, retries=1)
                    root = xml_parser.fromstring(r.content)
                except Exception:  # noqa: BLE001
                    return None, []
                kind = root.tag.split("}")[-1]
                locs = [(el.text or "").strip() for el in root.iter() if el.tag.split("}")[-1] == "loc" and el.text]
                return kind, locs

            kind, locs = _fetch_locs(base + "/sitemap.xml")
            if kind == "sitemapindex":
                agg: list[str] = []
                for sub in locs[:3]:
                    _, sub_locs = _fetch_locs(sub)
                    agg.extend(sub_locs)
                    if len(agg) >= limit:
                        break
                locs = agg
            out: list[str] = []
            for u in locs:
                cu = canonicalize_url(u)
                if urlparse(cu).scheme in ("http", "https"):
                    out.append(cu)
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] sitemap non utilizzabile per %s: %s", self.country_code, page_url, exc)
            return []

    # --- HTTP per thread e lavoro dei worker -----------------------------------------
    _tls: Any = None

    def _make_http(self) -> HttpSession:
        """Sessione HTTP del crawler. Con 'identify_crawler' (default true) lo User-Agent dichiara chi
        siamo e come contattarci; con false si usa l'header "da browser" di default."""
        headers = None
        if self.config.get("identify_crawler", True):
            headers = {"User-Agent": identified_user_agent(self.config.get("contact"))}
        return HttpSession(headers=headers)

    def _thread_http(self) -> HttpSession:
        """Una HttpSession per thread (requests.Session non è pensata per l'uso concorrente)."""
        tls = self._tls
        if tls is None:
            return self.http
        h = getattr(tls, "http", None)
        if h is None:
            h = tls.http = self._make_http()
        return h

    def _crawl_page_task(self, page_url: str, dom: str, ctx: Any) -> PageResult:
        """Eseguito nei thread worker: robots.txt, download, riconoscimento challenge, estrazione dei
        link (ed eventuale sitemap per i siti JS). Non modifica alcuno stato condiviso e non solleva mai:
        ogni problema finisce nel PageResult."""
        res = PageResult(page_url, dom)
        http = self._thread_http()
        resp = None
        try:
            if ctx.robots is not None:
                dec = ctx.robots.check(page_url)
                res.crawl_delay = dec.crawl_delay
                if not dec.allowed:
                    res.robots_blocked = dec.reason or "vietata da robots.txt"
                    return res
            resp = http.get(
                page_url, max_bytes=3_000_000, skip_non_html=True, read_timeout=8.0, allow_insecure_fallback=True,
                retries=ctx.http_retries,
            )
            res.tls_error = bool(getattr(resp, "tls_error", False))
            res.final_url = getattr(resp, "url", None) or page_url
            res.final_host = self._domain_of(res.final_url) or dom
            res.content_type = resp.headers.get("Content-Type", "")
            res.html_ok = self._looks_like_html(resp)
            if res.html_ok:
                reason = detect_challenge(resp.text, resp.headers, resp.status_code)
                if reason:
                    raise ChallengePage(f"pagina anti-bot/captcha ({reason})")
        except requests.RequestException as exc:
            res.net_exc = exc
            return res
        except Exception as exc:  # noqa: BLE001 - bug interno: segnalato dal coordinatore
            res.internal_exc = exc
            res.internal_tb = traceback.format_exc()
            return res

        try:
            if res.final_host != dom and (ctx.is_excluded(res.final_host) or not ctx.is_internal(res.final_host)):
                # Redirect verso un altro host fuori perimetro (SSO, altro sito): niente link da lì.
                res.skip_extraction = True
            if res.html_ok and not res.skip_extraction:
                html = resp.text
                res.links_found = self._extract_links(html, res.final_url)   # relativi risolti sull'URL FINALE
                res.resource_refs = self._extract_resource_refs(html, res.final_url)
                if not res.links_found and looks_js_rendered(html, 0):
                    res.needs_js = True
                    if ctx.js_sitemap_fallback and ctx.claim_sitemap(res.final_host):
                        res.links_found = self._sitemap_links(res.final_url)
                        res.used_sitemap = bool(res.links_found)
        except Exception as exc:  # noqa: BLE001 - errore di CONTENUTO: mai fatale
            res.content_exc = exc
        return res

    def download(self) -> Path:
        try:
            from bs4 import BeautifulSoup  # noqa: F401  (verifica disponibilità libreria)
        except ImportError as exc:
            raise RuntimeError("beautifulsoup4 non installato: 'pip install beautifulsoup4'") from exc

        seeds = self._load_seeds()
        depth = int(self.config.get("crawl_depth", 2))
        exclude_domains = {d.lower() for d in self.config.get("exclude_domains", [])}
        max_per_level = int(self.config.get("max_pages_per_level", 100))
        # 'sleep_seconds' = pausa minima tra due richieste allo STESSO host (politeness per host).
        # Host diversi vengono visitati in parallelo (vedi 'workers').
        sleep_s = float(self.config.get("sleep_seconds", 0.5))
        record_internal = bool(self.config.get("record_internal_links", False))
        allowed_domains = {d.lower() for d in self.config.get("allowed_domains", [])} or {
            self._domain_of(u) for u in seeds
        }

        # --- velocità e buone maniere --------------------------------------------
        workers = max(1, int(self.config.get("workers", 8)))
        max_conc_per_host = max(1, int(self.config.get("max_concurrent_per_host", 1)))
        respect_robots = bool(self.config.get("respect_robots", True))
        identify_crawler = bool(self.config.get("identify_crawler", True))
        max_crawl_delay = float(self.config.get("max_crawl_delay", 30.0))

        self._tls = threading.local()
        self.http = self._make_http()                      # sessione del thread coordinatore
        if identify_crawler:
            warn_if_no_contact(self.config.get("contact"))
        robots: RobotsCache | None = None
        if respect_robots:
            robots = RobotsCache(
                make_fetch_text(self._thread_http), agent=self.config.get("robots_agent", ROBOTS_AGENT),
                max_crawl_delay=max_crawl_delay,
            )
        else:
            logger.warning(
                "[%s] respect_robots=false: robots.txt NON viene letto né rispettato (scelta esplicita nel config).",
                self.country_code,
            )

        # 'allowed_tlds': seconda condizione (si somma ad allowed_domains,
        # non la sostituisce) per decidere se continuare a esplorare un
        # dominio scoperto durante il crawl. Se il config del paese non la
        # specifica esplicitamente, default a [ccTLD del paese, "gov"].
        tlds_cfg = self.config.get("allowed_tlds")
        if tlds_cfg:
            allowed_tlds = {str(t).lower().lstrip(".") for t in tlds_cfg}
        else:
            country_tld = COUNTRY_TLDS.get(self.country_code.upper(), self.country_code.lower())
            allowed_tlds = {country_tld, "gov"}
        if self.config.get("regional_tlds", True):
            allowed_tlds |= set(REGIONAL_TLDS.get(self.country_code.upper(), ()))
        self._use_infra = bool(self.config.get("use_default_infra_blocklist", True))

        # --- opzioni di espansione esplicita della paginazione numerica ---
        # NB: l'espansione di una sequenza di paginazione avviene nel thread coordinatore ed è
        # SEQUENZIALE (blocca gli altri worker finché non termina): 'follow_pagination' è
        # disattivato di default e nessun config di paese lo usa.
        follow_pagination = bool(self.config.get("follow_pagination", False))
        custom_regex = self.config.get("pagination_regex")
        pagination_regex = (
            re.compile(custom_regex, re.IGNORECASE) if custom_regex else DEFAULT_PAGINATION_REGEX
        )
        pagination_start_page = int(self.config.get("pagination_start_page", 1))
        pagination_max_pages = int(self.config.get("pagination_max_pages", 300))
        pagination_stop_after_empty = int(self.config.get("pagination_stop_after_empty", 2))
        pagination_sleep_s = float(self.config.get("pagination_sleep_seconds", sleep_s))

        # --- alternanza per domini che sembrano bloccarci -------------------
        # Appena un dominio dà 'domain_cooldown_after_errors' errori consecutivi (o
        # 'domain_cooldown_after_dead_links' 404 di fila) lo si mette "in pausa" per
        # 'domain_cooldown_calls' chiamate ad ALTRI domini, poi lo si ritenta;
        # 'domain_cooldown_max_retries' limita quante volte prima di rinunciare.
        domain_cooldown_after_errors = int(self.config.get("domain_cooldown_after_errors", 1))
        domain_cooldown_calls = int(self.config.get("domain_cooldown_calls", 30))
        domain_cooldown_max_retries = int(self.config.get("domain_cooldown_max_retries", 3))
        max_per_pattern = int(self.config.get("max_pages_per_pattern", 500))
        cooldown_after_dead_links = int(self.config.get("domain_cooldown_after_dead_links", 5))
        js_sitemap_fallback = bool(self.config.get("js_sitemap_fallback", True))
        max_internal_errors_in_a_row = int(self.config.get("max_internal_errors_in_a_row", 10))
        # Tentativi HTTP immediati per pagina (1 = nessun retry immediato). Il crawler ha già il suo meccanismo
        # di pausa/ritentativo per dominio (domain_cooldown_*): tanti retry immediati sullo stesso host
        # bloccavano un worker per ~6 s a pagina e moltiplicavano le richieste verso un server in difficoltà.
        http_retries = max(1, int(self.config.get("http_retries", 2)))
        status_every_s = float(self.config.get("status_log_seconds", 30))

        # ---------------------------------------------------------------------
        # Stato del crawl. Le strutture in memoria sono la fonte di verità durante il run;
        # ogni modifica rilevante viene anche accodata allo store SQLite (solo le DIFFERENZE,
        # poche righe per pagina) e resa durevole con store.commit() a ogni pagina.
        # ---------------------------------------------------------------------
        store, stored_state = self._open_store()
        fingerprint_fields = self._checkpoint_fingerprint_fields(seeds)
        fingerprint = self._checkpoint_fingerprint(seeds)

        checkpoint: dict[str, Any] | None = None
        from_legacy_json = False
        if stored_state is not None:
            checkpoint = stored_state
        else:
            checkpoint = self._load_checkpoint()
            from_legacy_json = checkpoint is not None

        resume_state: dict[str, Any] | None = None
        if checkpoint is not None:
            if checkpoint.get("fingerprint") != fingerprint:
                logger.warning(
                    "[%s] trovato un checkpoint ma non corrisponde più al config/seed attuali: lo "
                    "ignoro e riparto da zero. Dettaglio:", self.country_code,
                )
                self._log_fingerprint_mismatch(seeds, checkpoint.get("fingerprint_fields"))
            elif checkpoint.get("completed"):
                if self.resume:
                    logger.info(
                        "[%s] il checkpoint (salvato il %s) risulta di un crawl GIÀ COMPLETATO con queste "
                        "stesse impostazioni: rigenero l'output dai %d domini già trovati senza ripetere "
                        "il crawl", self.country_code, checkpoint.get("saved_at"), len(checkpoint.get("domain_registry", {})),
                    )
                    out = self.input_dir / self.config.get("input_filename", "crawl_results.json")
                    out.write_text(json.dumps(list(checkpoint["domain_registry"].values()), ensure_ascii=False, indent=2), encoding="utf-8")
                    store.close()
                    return out
                logger.info(
                    "[%s] trovato il checkpoint di un crawl già completato (salvato il %s, conservato): "
                    "procedo comunque con una nuova scansione da zero (nessun '--resume' specificato)",
                    self.country_code, checkpoint.get("saved_at"),
                )
            elif not self.resume:
                logger.info(
                    "[%s] trovato un checkpoint di una run precedente NON completata (livello %s, %d pagine già "
                    "visitate, salvato il %s) — rilancia con --resume per riprenderla; procedo da zero e lo sovrascrivo",
                    self.country_code, checkpoint.get("level"), len(checkpoint.get("visited", [])), checkpoint.get("saved_at"),
                )
            else:
                resume_state = checkpoint
                logger.info(
                    "[%s] --resume: riprendo dal livello %d (%d pagine già visitate, %d domini già registrati, "
                    "checkpoint salvato il %s) — vedi anche %s per il dettaglio per seed",
                    self.country_code, resume_state["level"], len(resume_state["visited"]),
                    len(resume_state["domain_registry"]), resume_state.get("saved_at"), self._progress_report_path(),
                )

        if resume_state is not None:
            if from_legacy_json:
                store.import_legacy_json(resume_state)
                logger.info("[%s] checkpoint JSON storico migrato nel nuovo store SQLite (%s)", self.country_code, store.path)
        else:
            store.reset()
            store.set_meta(fingerprint=fingerprint, fingerprint_fields=fingerprint_fields, completed=False, level=1,
                           saved_at=now_iso(), global_call_index=0)
            store.commit()

        class _TrackedSet(set):
            """Insieme che accoda allo store ogni NUOVO elemento (anche quando lo modifica codice esterno)."""
            def add(self_, item):  # noqa: N805
                if item not in self_:
                    set.add(self_, item)
                    store.add_visited(item)

        class _TrackedDict(dict):
            """page_origin_seed: setdefault persistente."""
            def setdefault(self_, key, default=None):  # noqa: N805
                if key not in self_:
                    dict.__setitem__(self_, key, default)
                    store.add_origin(key, default)
                return dict.__getitem__(self_, key)

        domain_registry: dict[str, dict[str, Any]] = {}
        resource_index: dict[str, list[dict[str, Any]]] = {}
        MAX_OCCORRENZE_PER_DOMINIO = 200
        MAX_HOSTS_PER_DOMAIN = 50

        if resume_state is not None:
            domain_registry.update(resume_state["domain_registry"])
            resource_index.update({k: list(v) for k, v in resume_state.get("resource_index", {}).items()})
            visited: set[str] = _TrackedSet()
            set.update(visited, resume_state["visited"])           # NON riscrivere nello store ciò che già c'è
            pagination_templates_seen: set[str] = set(resume_state["pagination_templates_seen"])
            page_origin_seed: dict[str, str] = _TrackedDict()
            dict.update(page_origin_seed, resume_state.get("page_origin_seed", {}))
            pages_by_seed_level: dict[str, dict[str, int]] = {
                k: dict(v) for k, v in resume_state.get("pages_by_seed_level", {}).items()
            }
            domain_consecutive_errors: dict[str, int] = dict(resume_state.get("domain_consecutive_errors", {}))
            domain_cooldown_until_call: dict[str, int] = dict(resume_state.get("domain_cooldown_until_call", {}))
            domain_cooldown_retries_used: dict[str, int] = dict(resume_state.get("domain_cooldown_retries_used", {}))
            global_call_index: int = int(resume_state.get("global_call_index", 0))
            error_log: list[dict[str, Any]] = list(resume_state.get("error_log", []))
            start_level = int(resume_state["level"])
            resumed_queue: list[str] | None = list(resume_state.get("queue", []))
            next_level: list[str] = list(resume_state["next_level"])
        else:
            visited = _TrackedSet()
            page_origin_seed = _TrackedDict()
            pagination_templates_seen = set()
            pages_by_seed_level = {}
            domain_consecutive_errors = {}
            domain_cooldown_until_call = {}
            domain_cooldown_retries_used = {}
            global_call_index = 0
            error_log = []
            start_level = 1
            resumed_queue = None
            next_level = []
            for s_ in seeds:                                    # ogni seed è "l'origine" di se stesso
                page_origin_seed.setdefault(s_, s_)

        def _register_resource(url: str, tipo: str, found_on: str, level: int) -> None:
            root = _root_domain(url)
            if not root or "." not in root:
                return
            occorrenze = resource_index.setdefault(root, [])
            if len(occorrenze) >= MAX_OCCORRENZE_PER_DOMINIO:
                return
            entry = {"pagina": found_on, "tipo": tipo, "url_risorsa": url, "livello": level}
            if entry not in occorrenze:                       # niente duplicati esatti
                occorrenze.append(entry)
                store.add_resource(root, entry)

        def _register(url: str, scope: str, found_on: str, level: int, source_seed: str | None = None) -> None:
            root = _root_domain(url)
            # Un dominio radice vero ha sempre almeno un punto; '' per IP letterali / schemi non-http.
            if not root or "." not in root:
                return
            host = self._domain_of(url)
            existing = domain_registry.get(root)
            if existing is not None:
                # Dominio già noto: resta la PRIMA occorrenza come riga di output, ma si ricordano tutti gli
                # host (FQDN) osservati: servono a chi deve poi aprire davvero il sito.
                hosts = existing.setdefault("observed_hosts", [])
                if host and host not in hosts and len(hosts) < MAX_HOSTS_PER_DOMAIN:
                    hosts.append(host)
                    store.put_registry(root, existing)
                return
            domain_registry[root] = {
                "external_url": f"https://{root}",
                "external_domain": root,
                "found_on": found_on,
                "level": level,
                "scope": scope,
                "source_seed": source_seed if source_seed is not None else page_origin_seed.get(found_on, found_on),
                "observed_host": host,
                "observed_url": canonicalize_url(url),
                "observed_hosts": [host] if host else [],
                "tls_error": False,
                "needs_js": False,
            }
            store.put_registry(root, domain_registry[root])

        def _mark_registry(url: str, **flags: Any) -> None:
            root_ = _root_domain(url)
            if root_ and root_ in domain_registry:
                domain_registry[root_].update(flags)
                store.put_registry(root_, domain_registry[root_])

        def _log_error(entry: dict[str, Any]) -> None:
            error_log.append(entry)
            store.add_error(entry)
            if len(error_log) > 5000:
                del error_log[:2500]

        def _bump_pages(seed: str, level: int) -> None:
            pages_by_seed_level.setdefault(seed, {})
            k = str(level)
            pages_by_seed_level[seed][k] = pages_by_seed_level[seed].get(k, 0) + 1
            store.bump_pages_seed(seed, level)

        def _visit_page(page_url: str, final_url: str | None = None) -> None:
            visited.add(page_url)
            visited.add(canonicalize_url(page_url))
            if final_url:
                visited.add(final_url)
                visited.add(canonicalize_url(final_url))

        # I domini radice dei SEED entrano subito in output (livello 0), anche se il crawl non scoprisse nulla.
        if resume_state is None:
            for seed_url in seeds:
                _register(seed_url, "seed", seed_url, 0, source_seed=seed_url)
            store.commit()

        seed_set = set(seeds)
        pattern_counts: dict[str, int] = {}
        sitemap_tried: set[str] = set()
        sitemap_lock = threading.Lock()
        domain_success_streak: dict[str, int] = {}
        consecutive_internal_errors = 0
        trap_skipped = 0
        n_robots_blocked = 0
        pages_this_run = 0
        run_started = time.monotonic()
        last_status_log = run_started
        last_meta_save = run_started
        host_next_allowed: dict[str, float] = {}
        to_visit = list(dict.fromkeys(seeds))
        status = "in corso"
        level = start_level
        sched = HostScheduler([], self._domain_of)

        def _is_internal(d: str) -> bool:
            return self._domain_matches(d, allowed_domains) or self._tld_matches(d, allowed_tlds)

        ctx = SimpleNamespace(
            robots=robots,
            is_internal=_is_internal,
            is_excluded=lambda d: self._is_excluded(d, exclude_domains),
            js_sitemap_fallback=js_sitemap_fallback,
            http_retries=http_retries,
            claim_sitemap=None,
        )

        def _claim_sitemap(host: str) -> bool:
            with sitemap_lock:
                if host in sitemap_tried:
                    return False
                sitemap_tried.add(host)
                return True

        ctx.claim_sitemap = _claim_sitemap

        def _save_meta(completed: bool = False) -> None:
            store.set_meta(
                level=level, global_call_index=global_call_index, completed=completed, saved_at=now_iso(),
                domain_consecutive_errors=domain_consecutive_errors,
                domain_cooldown_until_call=domain_cooldown_until_call,
                domain_cooldown_retries_used=domain_cooldown_retries_used,
            )
            store.commit()

        def _export_checkpoint_json() -> None:
            _save_meta(completed=(status == "completato"))
            store.export_json(self._checkpoint_path())

        def _reports(current_level: int | None, remaining: list[str]) -> None:
            self._safe_report(
                self._write_progress_report, seeds=seeds, visited=visited, domain_registry=domain_registry,
                pages_by_seed_level=pages_by_seed_level, depth=depth, current_level=current_level,
            )
            self._safe_report(
                self._write_markdown_report, seeds=seeds, visited=visited, domain_registry=domain_registry,
                pages_by_seed_level=pages_by_seed_level, depth=depth, queue=remaining,
                domain_cooldown_until_call=domain_cooldown_until_call,
                domain_cooldown_retries_used=domain_cooldown_retries_used,
                global_call_index=global_call_index, error_log=error_log, status=status,
            )

        logger.info(
            "[%s] crawl: %d worker, pausa minima %.1fs per host, robots.txt %s, User-Agent %s",
            self.country_code, workers, sleep_s, "RISPETTATO" if respect_robots else "ignorato",
            "identificabile" if identify_crawler else "da browser",
        )

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crawl")
        try:
            for level in range(start_level, depth + 1):
                if level == start_level and resumed_queue is not None:
                    # Livello ripreso a metà: stessa coda (nello stesso ordine) meno le pagine già
                    # visitate; 'next_level' già parzialmente popolato.
                    level_urls = resumed_queue
                else:
                    level_urls = self._select_for_level(to_visit, max_per_level, level)
                    next_level = []
                    store.begin_level(level, level_urls)
                next_seen: set[str] = set(next_level)
                sched = HostScheduler(level_urls, self._domain_of)
                inflight: dict[Any, tuple[str, str]] = {}
                inflight_count: dict[str, int] = {}
                level_total = len(level_urls)
                level_done = 0

                def _ready(host: str) -> bool:
                    if inflight_count.get(host, 0) >= max_conc_per_host:
                        return False
                    until = domain_cooldown_until_call.get(host)
                    if until is not None and global_call_index < until:
                        return False
                    return time.monotonic() >= host_next_allowed.get(host, 0.0)

                def _add_next(u: str) -> bool:
                    if u in next_seen:
                        return False
                    next_seen.add(u)
                    next_level.append(u)
                    store.add_next_level(u)
                    return True

                def _apply(res: PageResult) -> None:
                    """Applica al stato condiviso il risultato di UNA pagina (solo thread coordinatore)."""
                    nonlocal consecutive_internal_errors, trap_skipped, n_robots_blocked, level_done
                    page_url, dom = res.page_url, res.dom
                    origin_seed = page_origin_seed.get(page_url, page_url)
                    level_done += 1

                    if res.crawl_delay:
                        host_next_allowed[dom] = max(host_next_allowed.get(dom, 0.0), time.monotonic() + res.crawl_delay)

                    # -- robots.txt: pagina non consentita ---------------------------------------
                    if res.robots_blocked:
                        n_robots_blocked += 1
                        visited.add(page_url)
                        _log_error({
                            "url": page_url, "level": level, "seed": origin_seed, "error_type": "RobotsDisallowed",
                            "message": res.robots_blocked, "timestamp": now_iso(),
                            "outcome": "informativo (contenuto): vietata da robots.txt, saltata",
                        })
                        return

                    # -- errore di rete / HTTP / challenge -----------------------------------------
                    if res.net_exc is not None:
                        exc = res.net_exc
                        status_code = getattr(getattr(exc, "response", None), "status_code", None)
                        is_dead = (
                            isinstance(exc, requests.HTTPError) and status_code is not None
                            and 400 <= status_code < 500 and status_code not in (403, 408, 425, 429)
                        )
                        dom_errs = domain_consecutive_errors.get(dom, 0) + 1
                        domain_consecutive_errors[dom] = dom_errs
                        domain_success_streak[dom] = 0
                        retries_used = domain_cooldown_retries_used.get(dom, 0)
                        threshold = cooldown_after_dead_links if is_dead else domain_cooldown_after_errors
                        if dom_errs >= threshold and retries_used < domain_cooldown_max_retries:
                            domain_cooldown_retries_used[dom] = retries_used + 1
                            domain_cooldown_until_call[dom] = global_call_index + domain_cooldown_calls
                            outcome = f"in pausa (tentativo {retries_used + 1}/{domain_cooldown_max_retries})"
                            logger.warning(
                                "[%s] %s su %s: dominio '%s' probabilmente in blocco/rate-limit (%d errori consecutivi) — "
                                "messo in pausa per %d chiamate ad altri domini, poi ritentato (tentativo %d/%d)",
                                self.country_code, exc.__class__.__name__, page_url, dom, dom_errs,
                                domain_cooldown_calls, retries_used + 1, domain_cooldown_max_retries,
                            )
                            sched.push(page_url)          # NON in visited: verrà ritentata
                        else:
                            outcome = f"link morto (HTTP {status_code}), saltato" if is_dead else "rinuncia definitiva"
                            logger.log(
                                logging.DEBUG if is_dead else logging.WARNING,
                                "[%s] errore su %s (%s): %s", self.country_code, page_url, outcome, exc,
                            )
                            visited.add(page_url)
                            _bump_pages(origin_seed, level)
                        _log_error({
                            "url": page_url, "level": level, "seed": origin_seed, "error_type": exc.__class__.__name__,
                            "message": str(exc), "timestamp": now_iso(), "outcome": outcome,
                        })
                        return

                    # -- bug interno (non di rete): rumoroso, e se si ripete il crawl si ferma ----------
                    if res.internal_exc is not None:
                        consecutive_internal_errors += 1
                        logger.error(
                            "[%s] ERRORE INTERNO (non di rete) su %s: %s\n%s",
                            self.country_code, page_url, res.internal_exc, res.internal_tb,
                        )
                        visited.add(page_url)
                        _log_error({
                            "url": page_url, "level": level, "seed": origin_seed,
                            "error_type": res.internal_exc.__class__.__name__, "message": str(res.internal_exc),
                            "timestamp": now_iso(), "outcome": "errore interno (bug), pagina saltata",
                        })
                        if consecutive_internal_errors >= max_internal_errors_in_a_row:
                            raise RuntimeError(
                                f"{consecutive_internal_errors} errori interni consecutivi (ultimo: "
                                f"{res.internal_exc.__class__.__name__}: {res.internal_exc}): probabile bug/incompatibilità "
                                "di versione, mi fermo invece di produrre un output vuoto"
                            ) from res.internal_exc
                        return

                    # -- richiesta riuscita ------------------------------------------------------------
                    consecutive_internal_errors = 0
                    domain_consecutive_errors[dom] = 0
                    streak = domain_success_streak.get(dom, 0) + 1
                    domain_success_streak[dom] = streak
                    if streak >= 20 and domain_cooldown_retries_used.get(dom):
                        domain_cooldown_retries_used[dom] -= 1          # un dominio che torna a funzionare "riabilita" i tentativi
                        domain_success_streak[dom] = 0
                    if res.tls_error:
                        _mark_registry(page_url, tls_error=True)
                    _visit_page(page_url, res.final_url)
                    _bump_pages(origin_seed, level)

                    if res.content_exc is not None:
                        logger.warning(
                            "[%s] errore inatteso nell'analizzare il contenuto di %s (%s): pagina segnata come visitata con "
                            "0 link da essa, il crawl prosegue: %s",
                            self.country_code, page_url, res.content_exc.__class__.__name__, res.content_exc,
                        )
                        _log_error({
                            "url": page_url, "level": level, "seed": origin_seed,
                            "error_type": res.content_exc.__class__.__name__, "message": str(res.content_exc),
                            "timestamp": now_iso(), "outcome": "errore di contenuto, pagina saltata",
                        })
                        return

                    try:
                        if res.final_host != dom and not _is_excluded_final(res.final_host):
                            if _root_domain(res.final_url) != _root_domain(page_url):
                                _register(res.final_url, "redirect", page_url, level)
                        if res.skip_extraction:
                            logger.info("[%s] %s reindirizza fuori perimetro (%s): nessun link estratto", self.country_code, page_url, res.final_host)
                        elif not res.html_ok:
                            logger.info("[%s] %s ha Content-Type '%s' (non HTML): nessun link estratto", self.country_code, page_url, res.content_type)

                        if res.needs_js:
                            _mark_registry(res.final_url, needs_js=True)
                            _log_error({
                                "url": page_url, "level": level, "seed": origin_seed, "error_type": "needs_js",
                                "message": "0 link e contenuto statico quasi vuoto: probabile sito JS-rendered",
                                "timestamp": now_iso(), "outcome": "informativo (contenuto): needs_js",
                            })
                            if res.used_sitemap:
                                logger.info("[%s] %s: sito JS, uso sitemap.xml (%d URL)", self.country_code, res.final_host, len(res.links_found))

                        for abs_url in res.links_found:
                            dom2 = self._domain_of(abs_url)
                            if not dom2 or self._is_excluded(dom2, exclude_domains):
                                continue
                            if _is_internal(dom2):
                                if _estensione_da_evitare(abs_url):
                                    # Registrato per completezza (audit "interno"), ma MAI messo in coda.
                                    visited.add(abs_url)
                                elif abs_url not in visited and abs_url not in next_seen:
                                    if looks_like_trap(abs_url):
                                        trap_skipped += 1
                                    else:
                                        sig = url_pattern_signature(abs_url)
                                        n_sig = pattern_counts.get(sig, 0)
                                        if max_per_pattern and n_sig >= max_per_pattern:
                                            trap_skipped += 1
                                            if n_sig == max_per_pattern:
                                                logger.info(
                                                    "[%s] limite di %d pagine per pattern raggiunto (%s): le altre vengono ignorate",
                                                    self.country_code, max_per_pattern, sig[:120],
                                                )
                                                pattern_counts[sig] = n_sig + 1
                                        else:
                                            pattern_counts[sig] = n_sig + 1
                                            _add_next(abs_url)
                                page_origin_seed.setdefault(abs_url, origin_seed)
                                if record_internal:
                                    _register(abs_url, "interno", page_url, level)
                            else:
                                _register(abs_url, "esterno", page_url, level)

                        # Indice occorrenze SOLO per risorse esterne (input del post-processing "link sospetti").
                        for tipo, abs_url in res.resource_refs:
                            dom3 = self._domain_of(abs_url)
                            if not dom3 or self._is_excluded(dom3, exclude_domains):
                                continue
                            is_internal3 = _is_internal(dom3)
                            if not is_internal3:
                                _register_resource(abs_url, tipo, page_url, level)
                            # Paginazione numerica: solo per LINK navigabili INTERNI.
                            if follow_pagination and tipo == "link" and is_internal3:
                                template = self._pagination_template(abs_url, pagination_regex)
                                if template and template.lower() not in pagination_templates_seen:
                                    pagination_templates_seen.add(template.lower())
                                    store.add_template(template.lower())
                                    logger.info("[%s] paginazione rilevata su %s: espando sequenza per template %s", self.country_code, page_url, template)
                                    found_via_pagination = self._expand_pagination_sequence(
                                        template, found_on=page_url, level=level, visited=visited,
                                        exclude_domains=exclude_domains, allowed_domains=allowed_domains,
                                        allowed_tlds=allowed_tlds, record_internal=record_internal, register=_register,
                                        sleep_s=pagination_sleep_s, start_page=pagination_start_page,
                                        max_pages=pagination_max_pages, stop_after_empty=pagination_stop_after_empty,
                                        page_origin_seed=page_origin_seed, origin_seed=origin_seed,
                                        pages_by_seed_level=pages_by_seed_level,
                                        checkpoint_cb=lambda extra: (
                                            [_add_next(u) for u in extra], store.commit(),
                                        ),
                                        robots=robots,
                                    )
                                    for u in found_via_pagination:
                                        _add_next(u)
                                    store.sync_pages_seed(pages_by_seed_level)
                    except Exception as exc:  # noqa: BLE001 - errore di CONTENUTO: mai fatale
                        logger.warning(
                            "[%s] errore inatteso nell'elaborare i link di %s (%s): il crawl prosegue: %s",
                            self.country_code, page_url, exc.__class__.__name__, exc,
                        )
                        _log_error({
                            "url": page_url, "level": level, "seed": origin_seed, "error_type": exc.__class__.__name__,
                            "message": str(exc), "timestamp": now_iso(), "outcome": "errore di contenuto, pagina saltata",
                        })

                def _is_excluded_final(host: str) -> bool:
                    return self._is_excluded(host, exclude_domains)

                while len(sched) or inflight:
                    # 1) riempi i worker con pagine pronte (host non occupato, non in pausa, pausa per-host trascorsa)
                    while len(inflight) < workers:
                        page_url = sched.pop_ready(_ready)
                        if page_url is None:
                            break
                        if page_url in visited:
                            continue
                        if _estensione_da_evitare(page_url):
                            visited.add(page_url)
                            logger.debug("[%s] estensione non-pagina in coda, salto senza scaricare: %s", self.country_code, page_url)
                            continue
                        dom = self._domain_of(page_url) or ""
                        # Ricontrollo del perimetro AL MOMENTO di scaricare (una coda salvata da una versione
                        # precedente può contenere host che oggi non sarebbero "interni").
                        if page_url not in seed_set and (not dom or self._is_excluded(dom, exclude_domains) or not _is_internal(dom)):
                            visited.add(page_url)
                            logger.debug("[%s] fuori perimetro in coda, salto: %s", self.country_code, page_url)
                            continue
                        global_call_index += 1
                        inflight_count[dom] = inflight_count.get(dom, 0) + 1
                        fut = pool.submit(self._crawl_page_task, page_url, dom, ctx)
                        inflight[fut] = (page_url, dom)

                    # 2) nessun lavoro in corso e nulla di pronto: attesa (pausa per host) o sblocco forzato (stallo)
                    if not inflight:
                        if not len(sched):
                            break
                        now = time.monotonic()
                        hosts_pending = sched.hosts()
                        waits = [host_next_allowed[h] - now for h in hosts_pending
                                 if host_next_allowed.get(h, 0.0) > now
                                 and not (domain_cooldown_until_call.get(h) is not None and global_call_index < domain_cooldown_until_call[h])]
                        if waits:
                            time.sleep(min(max(min(waits), 0.0), 1.0))
                            continue
                        cooling = [h for h in hosts_pending if domain_cooldown_until_call.get(h) is not None and global_call_index < domain_cooldown_until_call[h]]
                        if cooling:
                            soonest = min(cooling, key=lambda h: domain_cooldown_until_call[h])
                            logger.warning(
                                "[%s] tutte le %d pagine rimaste nel livello %d appartengono a domini in pausa: sblocco "
                                "forzatamente %s per non restare bloccato", self.country_code, len(sched), level, soonest,
                            )
                            domain_cooldown_until_call[soonest] = global_call_index
                            continue
                        break   # nulla da fare (difensivo)

                    # 3) attendi almeno un completamento e applica i risultati
                    done, _pending = wait(list(inflight), timeout=0.25, return_when=FIRST_COMPLETED)
                    for fut in done:
                        page_url, dom = inflight.pop(fut)
                        inflight_count[dom] -= 1
                        try:
                            res = fut.result()
                        except Exception as exc:  # noqa: BLE001 - non dovrebbe accadere: il task cattura tutto
                            res = PageResult(page_url, dom, internal_exc=exc, internal_tb=traceback.format_exc())
                        host_next_allowed[dom] = max(host_next_allowed.get(dom, 0.0), time.monotonic() + sleep_s)
                        _apply(res)
                        pages_this_run += 1
                        store.commit()

                    now = time.monotonic()
                    if now - last_meta_save >= 15:
                        _save_meta()
                        last_meta_save = now
                    if now - last_status_log >= status_every_s:
                        last_status_log = now
                        rate = pages_this_run / max(now - run_started, 1e-9)
                        logger.info(
                            "[%s] livello %d: %d/%d pagine, %d in corso, %d domini radice, %.1f pagine/s (%d pagine in totale)",
                            self.country_code, level, level_done, level_total, len(inflight), len(domain_registry), rate, len(visited),
                        )

                to_visit = list(dict.fromkeys(next_level))
                _save_meta()
                logger.info(
                    "[%s] livello %d completato: %d pagine visitate finora, %d domini radice unici registrati finora "
                    "(%d URL scartate da guardie anti-trappola, %d pagine vietate da robots.txt)",
                    self.country_code, level, len(visited), len(domain_registry), trap_skipped, n_robots_blocked,
                )
                if level == start_level and n_robots_blocked and n_robots_blocked >= len(seeds) and not len(domain_registry) - len(seeds) > 0:
                    logger.error(
                        "[%s] TUTTI i seed sono vietati da robots.txt (%d pagine saltate): il crawl non può scoprire nulla. "
                        "Opzioni: usare una fonte ufficiale alternativa, contattare il gestore, oppure impostare "
                        "'respect_robots: false' nel config del paese (scelta e responsabilità di chi esegue il crawl).",
                        self.country_code, n_robots_blocked,
                    )
                _reports(level + 1, [])
                store.export_json(self._checkpoint_path())
        except KeyboardInterrupt:
            status = "interrotto dall'utente (Ctrl+C)"
            pool.shutdown(wait=False, cancel_futures=True)
            _reports(level, sched.all_urls())
            _export_checkpoint_json()
            store.close()
            logger.warning(
                "[%s] crawl interrotto dall'utente: stato salvato in %s (dettaglio per seed in %s, report in %s) — "
                "rilancia lo stesso comando con --resume per riprendere da qui",
                self.country_code, store.path, self._progress_report_path(), self._report_md_path(),
            )
            raise
        except Exception:
            status = "interrotto da un errore imprevisto"
            pool.shutdown(wait=False, cancel_futures=True)
            _reports(level, sched.all_urls())
            _export_checkpoint_json()
            store.close()
            logger.warning(
                "[%s] crawl interrotto da un errore imprevisto: stato salvato in %s (dettaglio per seed in %s, report in %s) — "
                "rilancia lo stesso comando con --resume per riprendere da qui",
                self.country_code, store.path, self._progress_report_path(), self._report_md_path(),
            )
            raise
        pool.shutdown(wait=True)

        out = self.input_dir / self.config.get("input_filename", "crawl_results.json")
        deduped = list(domain_registry.values())
        out.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
        # Crawl completato: lo stato NON viene cancellato, resta marcato 'completed'. Un successivo
        # '--resume' lo riconosce come già concluso e rigenera l'output all'istante.
        status = "completato"
        _save_meta(completed=True)
        store.export_json(self._checkpoint_path())
        _reports(None, [])
        store.close()
        logger.info(
            "[%s] crawl completato: %d pagine visitate (%.1f pagine/s), %d domini radice unici (inclusi i seed) -> %s "
            "(report: %s, stato: %s)",
            self.country_code, len(visited), pages_this_run / max(time.monotonic() - run_started, 1e-9), len(deduped),
            out, self._report_md_path(), store.path,
        )
        return out

    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        rows = json.loads(raw_path.read_text(encoding="utf-8"))
        for row in rows:
            # Campi di supporto pensati per il CSV: liste e booleani -> testo.
            hosts = row.get("observed_hosts")
            if isinstance(hosts, list):
                row["observed_hosts"] = "|".join(h for h in hosts if h)
            row["tls_error"] = "si" if row.get("tls_error") else ""
            row["needs_js"] = "si" if row.get("needs_js") else ""
        return rows


FETCHER_REGISTRY = {
    "bulk_csv": BulkCSVFetcher,
    "bulk_xlsx": BulkXLSXFetcher,
    "bulk_xlsx_folder": BulkXlsxFolderFetcher,
    "bulk_xml": BulkXMLFetcher,
    "api_json": ApiJsonFetcher,
    "html_scrape": HtmlScrapeFetcher,
    "site_crawl": SiteCrawlFetcher,
}
