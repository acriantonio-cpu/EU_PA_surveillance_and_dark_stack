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
import io
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from ..base_fetcher import BaseFetcher, _dig
from ..schema import now_iso

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

try:
    import tldextract
    # Usa SOLO lo snapshot della Public Suffix List incluso nel pacchetto
    # (nessuna chiamata di rete a runtime): rende _root_domain() robusto e
    # riutilizzabile per qualunque paese/TLD senza dipendere da una
    # connessione internet disponibile al momento del crawl.
    _TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())
except ImportError:  # pragma: no cover
    tldextract = None
    _TLD_EXTRACTOR = None


def _root_domain(url_or_host: str) -> str:
    """
    Dominio radice (eTLD+1) di un URL o hostname, calcolato con la Public
    Suffix List (via tldextract) per gestire correttamente le convenzioni
    di ciascun paese: es. 'finances.belgium.be' -> 'belgium.be',
    'kbopub.economie.fgov.be' -> 'fgov.be', ma anche suffissi multi-livello
    come '.co.uk' -> 'esempio.co.uk' (non gestibile con un semplice
    "ultime 2 etichette"). Se tldextract non è installato, usa un fallback
    grezzo (ultime 2 etichette del dominio).
    """
    if _TLD_EXTRACTOR is None:
        host = urlparse(url_or_host).netloc.lower().split(":")[0] or str(url_or_host).lower()
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    ext = _TLD_EXTRACTOR(url_or_host)
    if ext.suffix:
        return f"{ext.domain}.{ext.suffix}".lower()
    return (ext.domain or "").lower()


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
COUNTRY_TLDS: dict[str, str] = {
    "AT": "at", "BE": "be", "BG": "bg", "CY": "cy", "CZ": "cz", "DE": "de",
    "DK": "dk", "EE": "ee", "EL": "gr", "ES": "es", "FI": "fi", "FR": "fr",
    "HR": "hr", "HU": "hu", "IE": "ie", "IT": "it", "LT": "lt", "LU": "lu",
    "LV": "lv", "MT": "mt", "NL": "nl", "PL": "pl", "PT": "pt", "RO": "ro",
    "SE": "se", "SI": "si", "SK": "sk",
}


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
        encoding = self.config.get("encoding", "utf-8-sig")
        with raw_path.open("r", encoding=encoding, errors="replace") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
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
        tree = ET.parse(raw_path)
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

            for _ in range(max_pages):
                params = {page_param: page}
                if page_size_param:
                    params[page_size_param] = page_size
                resp = self.http.get(self.config["source_url"], params=params)
                data = resp.json()
                batch = _dig(data, self.config["records_path"]) if self.config.get("records_path") else data
                if not batch:
                    break
                all_records.extend(batch)
                if len(batch) < page_size:
                    break
                page += 1
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
        for f in xlsx_files:
            wb = openpyxl.load_workbook(f, data_only=True)
            ws = wb[sheet_name] if sheet_name else wb.active
            n_before = len(all_rows)
            for r in range(data_start_row, ws.max_row + 1):
                row_vals = [ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)]
                if all(v is None for v in row_vals):
                    continue
                row_dict: dict[str, Any] = {"_source_file": f.name}
                for c, val in enumerate(row_vals, start=1):
                    col_letter = openpyxl.utils.get_column_letter(c)
                    row_dict[col_letter] = val
                all_rows.append(row_dict)
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

    ATTENZIONE: rispettare robots.txt e i termini d'uso del sito target è
    responsabilità di chi esegue il crawl — questo fetcher non controlla
    automaticamente robots.txt. Verificarlo manualmente prima di un run su
    larga scala (vedi note nel config del paese).

    --resume E REPORT DI PROGRESSO: vedi il blocco di commenti sopra
    _checkpoint_path()/_write_progress_report() più sotto in questo file, e
    la sezione dedicata in README.md, per i dettagli su come riprendere un
    crawl interrotto (data/input/{ISO}/crawl_checkpoint.json, a grana di
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
        return urlparse(url).netloc.lower().split(":")[0]

    @staticmethod
    def _domain_matches(dom: str, domain_list: set[str]) -> bool:
        """Match per suffisso: 'finances.belgium.be' combacia con 'belgium.be'."""
        return any(dom == d or dom.endswith("." + d) for d in domain_list)

    @staticmethod
    def _tld_matches(dom: str, tlds: set[str]) -> bool:
        """
        Vero se 'dom' appartiene a uno dei TLD/label indicati (usato per
        'allowed_tlds'). Ogni voce può essere:
          - un singolo label (es. 'be', 'gov'): combacia se compare come uno
            QUALSIASI dei label del dominio, non solo l'ultimo — copre sia
            il caso ccTLD puro ('vlaanderen.be' -> label 'be' in fondo) sia
            i domini "gov.<cctld>" tipici di alcuni paesi (es. Irlanda:
            'revenue.gov.ie' -> label 'gov' in posizione intermedia);
          - un suffisso multi-livello con un punto (es. 'gov.uk'): combacia
            per suffisso classico (endswith).
        """
        labels = dom.lower().split(".")
        for raw in tlds:
            t = raw.lower().lstrip(".")
            if not t:
                continue
            if "." in t:
                if dom.lower() == t or dom.lower().endswith("." + t):
                    return True
            elif t in labels:
                return True
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

    def _save_checkpoint(
        self,
        *,
        fingerprint: str,
        fingerprint_fields: dict[str, Any],
        level: int,
        queue: list[str],
        visited: set[str],
        next_level: list[str],
        domain_registry: dict[str, dict[str, Any]],
        resource_index: dict[str, list[dict[str, Any]]] | None = None,
        pagination_templates_seen: set[str],
        page_origin_seed: dict[str, str],
        pages_by_seed_level: dict[str, dict[str, int]],
        domain_consecutive_errors: dict[str, int],
        domain_cooldown_until_call: dict[str, int],
        domain_cooldown_retries_used: dict[str, int],
        global_call_index: int,
        error_log: list[dict[str, Any]],
        completed: bool = False,
    ) -> None:
        """
        Scrittura ATOMICA (file temporaneo + rename) per evitare un
        checkpoint corrotto/a metà se il processo viene interrotto proprio
        durante il salvataggio.

        "Best-effort" per design: dato che viene chiamata dopo OGNI pagina,
        un problema qui (visto su Windows: PermissionError sul rename,
        tipicamente antivirus/OneDrive che tengono per un istante il file in
        mano) non deve MAI far fallire il crawl — nel peggiore dei casi si
        perde solo l'ultimo checkpoint (il precedente, di una pagina fa,
        resta comunque valido su disco). Si ritenta un paio di volte con una
        breve pausa prima di arrendersi con un semplice avviso.
        """
        data = {
            "fingerprint": fingerprint,
            "fingerprint_fields": fingerprint_fields,
            "level": level,
            "queue": queue,
            "visited": sorted(visited),
            "next_level": next_level,
            "domain_registry": domain_registry,
            "resource_index": resource_index or {},
            "pagination_templates_seen": sorted(pagination_templates_seen),
            "page_origin_seed": page_origin_seed,
            "pages_by_seed_level": pages_by_seed_level,
            "domain_consecutive_errors": domain_consecutive_errors,
            "domain_cooldown_until_call": domain_cooldown_until_call,
            "domain_cooldown_retries_used": domain_cooldown_retries_used,
            "global_call_index": global_call_index,
            # Troncato alle ultime 2000 voci: un log di errori illimitato su
            # un crawl molto lungo/problematico potrebbe far crescere il
            # checkpoint senza limite; le voci più vecchie sono comunque
            # meno rilevanti di quelle recenti per capire lo stato attuale.
            "error_log": error_log[-2000:],
            "completed": completed,
            "saved_at": now_iso(),
        }
        path = self._checkpoint_path()
        tmp_path = path.with_suffix(".tmp")
        try:
            payload = json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001 - non deve MAI far fallire il crawl
            logger.warning(
                "[%s] impossibile serializzare il checkpoint (%s): salto questo salvataggio, il crawl "
                "prosegue comunque (resterà valido l'ultimo checkpoint riuscito in precedenza)",
                self.country_code, exc,
            )
            return
        for attempt in range(1, 4):
            try:
                tmp_path.write_text(payload, encoding="utf-8")
                os.replace(tmp_path, path)  # rename atomico sullo stesso filesystem
                return
            except OSError as exc:
                if attempt == 3:
                    logger.warning(
                        "[%s] impossibile scrivere il checkpoint in %s dopo %d tentativi (%s): il crawl "
                        "prosegue comunque, ma un'eventuale interruzione ora riprenderebbe dall'ultimo "
                        "checkpoint riuscito in precedenza, non da questo punto esatto",
                        self.country_code, path, attempt, exc,
                    )
                    return
                time.sleep(0.5 * attempt)

    def _load_checkpoint(self) -> dict[str, Any] | None:
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
            links.append(abs_url)
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
            page_origin_seed.setdefault(page_url, origin_seed)
            pages_by_seed_level.setdefault(origin_seed, {})
            lvl_key = str(level)
            pages_by_seed_level[origin_seed][lvl_key] = pages_by_seed_level[origin_seed].get(lvl_key, 0) + 1
            try:
                resp = self.http.get(page_url, max_bytes=3_000_000, skip_non_html=True, read_timeout=8.0)
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
                if not dom or self._domain_matches(dom, exclude_domains):
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

    def download(self) -> Path:
        try:
            from bs4 import BeautifulSoup  # noqa: F401  (verifica disponibilità libreria)
        except ImportError as exc:
            raise RuntimeError("beautifulsoup4 non installato: 'pip install beautifulsoup4'") from exc

        seeds = self._load_seeds()
        depth = int(self.config.get("crawl_depth", 2))
        exclude_domains = {d.lower() for d in self.config.get("exclude_domains", [])}
        max_per_level = int(self.config.get("max_pages_per_level", 100))
        sleep_s = float(self.config.get("sleep_seconds", 0.5))
        record_internal = bool(self.config.get("record_internal_links", False))
        allowed_domains = {d.lower() for d in self.config.get("allowed_domains", [])} or {
            self._domain_of(u) for u in seeds
        }

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

        # --- opzioni di espansione esplicita della paginazione numerica ---
        follow_pagination = bool(self.config.get("follow_pagination", False))
        custom_regex = self.config.get("pagination_regex")
        pagination_regex = (
            re.compile(custom_regex, re.IGNORECASE) if custom_regex else DEFAULT_PAGINATION_REGEX
        )
        pagination_start_page = int(self.config.get("pagination_start_page", 1))
        pagination_max_pages = int(self.config.get("pagination_max_pages", 300))
        pagination_stop_after_empty = int(self.config.get("pagination_stop_after_empty", 2))
        pagination_sleep_s = float(self.config.get("pagination_sleep_seconds", sleep_s))
        # Un template (es. '.../page_{}') viene espanso UNA SOLA VOLTA per
        # tutto il crawl, indipendentemente da quante volte/pagine diverse
        # linkano una sua istanza (es. sia page_2 che page_5 dello stesso
        # elenco puntano allo stesso template).
        pagination_templates_seen: set[str] = set()

        # --- alternanza per domini che sembrano bloccarci -------------------
        #
        # Alcuni siti rispondono con errori (spesso 404, a volte 403/429)
        # quando si fanno troppe richieste ravvicinate, invece di un più
        # onesto "429 Too Many Requests": indistinguibile da un vero errore
        # se non si osserva il PATTERN (fallisce ripetutamente proprio
        # quando lo si contatta di fila). Strategia: appena un dominio dà
        # 'domain_cooldown_after_errors' errori consecutivi, lo si mette "in
        # pausa" per 'domain_cooldown_calls' chiamate ad ALTRI domini (si
        # continua a lavorare sullo STESSO livello nel frattempo, mai si
        # passa al livello successivo per questo), poi lo si ritenta.
        # 'domain_cooldown_max_retries' limita quante volte un dominio può
        # essere rimesso in pausa: oltre quella soglia si rinuncia (è
        # verosimilmente morto/bloccato in modo permanente, non solo
        # rate-limit) invece di rallentare indefinitamente il livello.
        domain_cooldown_after_errors = int(self.config.get("domain_cooldown_after_errors", 1))
        domain_cooldown_calls = int(self.config.get("domain_cooldown_calls", 30))
        domain_cooldown_max_retries = int(self.config.get("domain_cooldown_max_retries", 3))

        # Registro dei DOMINI RADICE già visti (chiave = dominio radice):
        # garantisce che l'output non abbia mai due righe per lo stesso
        # dominio, indipendentemente da quanti URL/pagine diverse di quel
        # dominio vengano scoperti durante il crawl. Si tiene solo la PRIMA
        # occorrenza (dove/quando è stato scoperto per la prima volta).
        # In caso di --resume, viene rimpiazzato dal contenuto del
        # checkpoint (vedi sotto) invece di ripartire vuoto.
        domain_registry: dict[str, dict[str, Any]] = {}

        # Indice delle OCCORRENZE di ogni dominio di terza parte (esterno):
        # a differenza di 'domain_registry' sopra (che tiene solo la PRIMA
        # occorrenza di ogni dominio radice, per l'output principale a un
        # rigo per dominio), questo tiene TUTTE le pagine/tag in cui un
        # dominio esterno compare — input del post-processing "link
        # sospetti" (tools/postprocess_checkpoints.py). Non altera in alcun
        # modo il comportamento/output esistente: è un indice AGGIUNTIVO,
        # salvato nel checkpoint come chiave a parte ('resource_index').
        # Troncato a max 200 occorrenze per dominio (oltre, il post-
        # processing ha già abbastanza segnale: un dominio visto su 200+
        # pagine non trae beneficio dall'elencarle tutte, e si evita che il
        # checkpoint cresca senza limite su un crawl molto grande).
        resource_index: dict[str, list[dict[str, Any]]] = {}
        MAX_OCCORRENZE_PER_DOMINIO = 200

        def _register_resource(url: str, tipo: str, found_on: str, level: int) -> None:
            root = _root_domain(url)
            if not root or "." not in root:
                return
            occorrenze = resource_index.setdefault(root, [])
            if len(occorrenze) >= MAX_OCCORRENZE_PER_DOMINIO:
                return
            entry = {"pagina": found_on, "tipo": tipo, "url_risorsa": url, "livello": level}
            # Evita duplicati esatti (stessa pagina, stesso tipo, stessa
            # risorsa) — puo' capitare con paginazione/pagine molto simili.
            if entry not in occorrenze:
                occorrenze.append(entry)

        def _register(url: str, scope: str, found_on: str, level: int, source_seed: str | None = None) -> None:
            root = _root_domain(url)
            # Rete di sicurezza aggiuntiva: un dominio radice vero ha sempre
            # almeno un punto (label.tld). Un valore senza punto (es. "send",
            # "compose") è quasi certamente il residuo del parsing di un URL
            # con uno schema "app" non-http (vedi filtro per schema in
            # _extract_links) sfuggito da qualche altro punto d'ingresso: lo
            # si scarta invece di inquinare l'output con un falso dominio.
            if not root or "." not in root or root in domain_registry:
                return
            domain_registry[root] = {
                "external_url": f"https://{root}",
                "external_domain": root,
                "found_on": found_on,
                "level": level,
                "scope": scope,
                # A quale seed di partenza risale questa scoperta (per il
                # report di progresso per seed+livello, vedi
                # _write_progress_report). Se non passato esplicitamente,
                # si risale tramite 'page_origin_seed' (propagato durante il
                # crawl: ogni pagina eredita il seed della pagina che l'ha
                # scoperta).
                "source_seed": source_seed if source_seed is not None else page_origin_seed.get(found_on, found_on),
            }

        # --- checkpoint: riprendo una run interrotta, o parto da zero? -----
        fingerprint_fields = self._checkpoint_fingerprint_fields(seeds)
        fingerprint = self._checkpoint_fingerprint(seeds)
        checkpoint = self._load_checkpoint()
        resume_state: dict[str, Any] | None = None
        if checkpoint is not None:
            if checkpoint.get("fingerprint") != fingerprint:
                logger.warning(
                    "[%s] trovato un checkpoint (%s) ma non corrisponde più al config/seed attuali: lo "
                    "ignoro e riparto da zero. Dettaglio:",
                    self.country_code, self._checkpoint_path(),
                )
                self._log_fingerprint_mismatch(seeds, checkpoint.get("fingerprint_fields"))
            elif checkpoint.get("completed"):
                # Il checkpoint non viene più cancellato a fine crawl (resta
                # come traccia permanente, vedi fondo del metodo): se il
                # fingerprint combacia ED è marcato 'completed', l'ultimo
                # run è già arrivato in fondo con QUESTE stesse
                # impostazioni. Con '--resume' non ha senso rifare da capo
                # tutto il crawl solo per ottenere lo stesso risultato:
                # rigenero l'output all'istante dal 'domain_registry' già
                # salvato. Senza '--resume' si comporta come sempre
                # (ignorato, si riparte comunque da zero).
                if self.resume:
                    logger.info(
                        "[%s] il checkpoint (salvato il %s) risulta di un crawl GIÀ COMPLETATO con queste "
                        "stesse impostazioni: rigenero l'output dai %d domini già trovati senza ripetere "
                        "il crawl",
                        self.country_code, checkpoint.get("saved_at"), len(checkpoint.get("domain_registry", {})),
                    )
                    out = self.input_dir / self.config.get("input_filename", "crawl_results.json")
                    deduped = list(checkpoint["domain_registry"].values())
                    out.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
                    return out
                logger.info(
                    "[%s] trovato il checkpoint di un crawl già completato in precedenza (salvato il %s, "
                    "conservato su disco, non cancellato) — procedo comunque con una nuova scansione da "
                    "zero (nessun '--resume' specificato)",
                    self.country_code, checkpoint.get("saved_at"),
                )
            elif not self.resume:
                logger.info(
                    "[%s] trovato un checkpoint di una run precedente NON completata (livello %s, "
                    "%d pagine già visitate, salvato il %s) — rilancia con --resume per riprenderla; "
                    "procedo da zero e lo sovrascrivo",
                    self.country_code, checkpoint.get("level"), len(checkpoint.get("visited", [])),
                    checkpoint.get("saved_at"),
                )
            else:
                resume_state = checkpoint
                logger.info(
                    "[%s] --resume: riprendo dal livello %d (%d pagine già visitate, %d domini già "
                    "registrati, checkpoint salvato il %s) — vedi anche %s per il dettaglio per seed",
                    self.country_code, resume_state["level"], len(resume_state["visited"]),
                    len(resume_state["domain_registry"]), resume_state.get("saved_at"),
                    self._progress_report_path(),
                )

        if resume_state is not None:
            domain_registry.update(resume_state["domain_registry"])
            # 'resource_index' è più recente del meccanismo di checkpoint
            # originale: un checkpoint scritto da una versione precedente
            # del tool non lo contiene, .get(..., {}) mantiene il resume
            # compatibile anche in quel caso (si riparte con l'indice
            # vuoto, si ripopola man mano dal livello corrente in poi).
            resource_index.update({k: list(v) for k, v in resume_state.get("resource_index", {}).items()})
            visited: set[str] = set(resume_state["visited"])
            pagination_templates_seen: set[str] = set(resume_state["pagination_templates_seen"])
            page_origin_seed: dict[str, str] = dict(resume_state.get("page_origin_seed", {}))
            pages_by_seed_level: dict[str, dict[str, int]] = {
                k: dict(v) for k, v in resume_state.get("pages_by_seed_level", {}).items()
            }
            domain_consecutive_errors: dict[str, int] = dict(resume_state.get("domain_consecutive_errors", {}))
            domain_cooldown_until_call: dict[str, int] = dict(resume_state.get("domain_cooldown_until_call", {}))
            domain_cooldown_retries_used: dict[str, int] = dict(resume_state.get("domain_cooldown_retries_used", {}))
            global_call_index: int = int(resume_state.get("global_call_index", 0))
            error_log: list[dict[str, Any]] = list(resume_state.get("error_log", []))
            start_level = int(resume_state["level"])
            # 'queue' è il nome nuovo; 'this_level_pages' è mantenuto come
            # fallback per leggere un checkpoint scritto da una versione
            # precedente di questo tool.
            resumed_queue: list[str] | None = list(resume_state.get("queue", resume_state.get("this_level_pages", [])))
            next_level: list[str] = list(resume_state["next_level"])
        else:
            # Ogni seed è "l'origine" di se stesso: usato per attribuire a
            # QUEL seed (nel report di progresso) tutte le pagine/domini
            # scoperti a valle, mano a mano che il crawl si propaga.
            page_origin_seed = {s: s for s in seeds}
            pages_by_seed_level = {}
            domain_consecutive_errors = {}
            domain_cooldown_until_call = {}
            domain_cooldown_retries_used = {}
            global_call_index = 0
            error_log = []
            # I domini radice dei SEED entrano subito in output (livello 0),
            # anche se il crawl non dovesse scoprire nulla di nuovo.
            for seed_url in seeds:
                _register(seed_url, "seed", seed_url, 0, source_seed=seed_url)
            visited = set()
            pagination_templates_seen = set()
            start_level = 1
            resumed_queue = None
            next_level = []

        to_visit = list(dict.fromkeys(seeds))
        status = "in corso"

        try:
            for level in range(start_level, depth + 1):
                if level == start_level and resumed_queue is not None:
                    # Livello ripreso a metà da un checkpoint: uso la stessa
                    # coda (nello stesso ordine) salvata al momento
                    # dell'interruzione, e mantengo 'next_level' già
                    # parzialmente popolato.
                    queue: deque[str] = deque(resumed_queue)
                else:
                    queue = deque(to_visit[:max_per_level])
                    next_level = []

                def _checkpoint_now(extra_next_level: list[str] | None = None) -> None:
                    combined = next_level + (extra_next_level or [])
                    self._save_checkpoint(
                        fingerprint=fingerprint,
                        fingerprint_fields=fingerprint_fields,
                        level=level,
                        queue=list(queue),
                        visited=visited,
                        next_level=list(dict.fromkeys(combined)),
                        domain_registry=domain_registry,
                        resource_index=resource_index,
                        pagination_templates_seen=pagination_templates_seen,
                        page_origin_seed=page_origin_seed,
                        pages_by_seed_level=pages_by_seed_level,
                        domain_consecutive_errors=domain_consecutive_errors,
                        domain_cooldown_until_call=domain_cooldown_until_call,
                        domain_cooldown_retries_used=domain_cooldown_retries_used,
                        global_call_index=global_call_index,
                        error_log=error_log,
                    )

                # 'stalled_pops' conta quante volte DI FILA si è ripescata
                # dalla coda una pagina ancora "in pausa" senza fare nessuna
                # chiamata reale nel frattempo: se supera la lunghezza
                # attuale della coda vuol dire che TUTTE le pagine rimaste
                # in questo livello appartengono a domini in pausa
                # contemporaneamente — un vero stallo, altrimenti mai
                # risolvibile (il tempo "passa" solo contando chiamate). In
                # quel caso si sblocca forzatamente il dominio più vicino
                # alla scadenza, piuttosto che restare bloccati per sempre.
                stalled_pops = 0

                while queue:
                    page_url = queue.popleft()
                    if page_url in visited:
                        continue
                    if _estensione_da_evitare(page_url):
                        # Copre anche il caso di un url già in coda PRIMA
                        # che questo filtro esistesse (checkpoint salvato
                        # da un run precedente, ripreso con --resume): il
                        # filtro al momento della scoperta del link (sopra,
                        # dove si popola next_level) non rivede ciò che è
                        # già in coda, quindi serve ricontrollare anche QUI,
                        # subito prima del fetch vero e proprio — unico
                        # punto che garantisce la protezione a prescindere
                        # da quando/come l'url è finito in coda.
                        visited.add(page_url)
                        logger.debug(
                            "[%s] estensione non-pagina in coda (probabile resume da checkpoint "
                            "precedente al filtro), salto senza scaricare: %s",
                            self.country_code, page_url,
                        )
                        continue
                    dom = self._domain_of(page_url) or ""

                    ready_at = domain_cooldown_until_call.get(dom)
                    if ready_at is not None and global_call_index < ready_at:
                        queue.append(page_url)
                        stalled_pops += 1
                        if stalled_pops > len(queue):
                            remaining_doms = {self._domain_of(u) or "" for u in queue}
                            if remaining_doms:
                                soonest = min(
                                    remaining_doms, key=lambda d: domain_cooldown_until_call.get(d, 0)
                                )
                                logger.warning(
                                    "[%s] tutte le %d pagine rimaste nel livello %d appartengono a domini "
                                    "attualmente in pausa: sblocco forzatamente %s per non restare bloccato",
                                    self.country_code, len(queue), level, soonest,
                                )
                                domain_cooldown_until_call[soonest] = global_call_index
                            stalled_pops = 0
                        continue
                    stalled_pops = 0

                    origin_seed = page_origin_seed.get(page_url, page_url)
                    global_call_index += 1

                    try:
                        resp = self.http.get(page_url, max_bytes=3_000_000, skip_non_html=True, read_timeout=8.0)
                    except Exception as exc:  # noqa: BLE001 - errore di rete/HTTP: candidato all'alternanza
                        dom_errs = domain_consecutive_errors.get(dom, 0) + 1
                        domain_consecutive_errors[dom] = dom_errs
                        retries_used = domain_cooldown_retries_used.get(dom, 0)
                        if dom_errs >= domain_cooldown_after_errors and retries_used < domain_cooldown_max_retries:
                            domain_cooldown_retries_used[dom] = retries_used + 1
                            domain_cooldown_until_call[dom] = global_call_index + domain_cooldown_calls
                            outcome = f"in pausa (tentativo {retries_used + 1}/{domain_cooldown_max_retries})"
                            logger.warning(
                                "[%s] %s su %s: dominio '%s' probabilmente in blocco/rate-limit (%d errori "
                                "consecutivi) — messo in pausa per %d chiamate ad altri domini, poi "
                                "ritentato (tentativo %d/%d)",
                                self.country_code, exc.__class__.__name__, page_url, dom, dom_errs,
                                domain_cooldown_calls, retries_used + 1, domain_cooldown_max_retries,
                            )
                            queue.append(page_url)  # NON aggiunta a visited: verrà ritentata
                        else:
                            outcome = "rinuncia definitiva"
                            logger.warning(
                                "[%s] errore su %s (rinuncio: %d/%d tentativi di alternanza sul dominio "
                                "già usati): %s",
                                self.country_code, page_url, retries_used, domain_cooldown_max_retries, exc,
                            )
                            visited.add(page_url)
                            pages_by_seed_level.setdefault(origin_seed, {})
                            lvl_key = str(level)
                            pages_by_seed_level[origin_seed][lvl_key] = (
                                pages_by_seed_level[origin_seed].get(lvl_key, 0) + 1
                            )
                        error_log.append({
                            "url": page_url, "level": level, "seed": origin_seed,
                            "error_type": exc.__class__.__name__, "message": str(exc),
                            "timestamp": now_iso(), "outcome": outcome,
                        })
                        _checkpoint_now()
                        time.sleep(sleep_s)
                        continue

                    # Richiesta riuscita: il dominio non è (più) bloccato.
                    domain_consecutive_errors[dom] = 0
                    visited.add(page_url)
                    pages_by_seed_level.setdefault(origin_seed, {})
                    lvl_key = str(level)
                    pages_by_seed_level[origin_seed][lvl_key] = (
                        pages_by_seed_level[origin_seed].get(lvl_key, 0) + 1
                    )

                    # Estrazione/elaborazione dei link: avvolta A PARTE.
                    # Un problema qui è di CONTENUTO (markup corrotto,
                    # encoding inatteso, bug su un link specifico), non di
                    # rete — ritentare non cambierebbe il contenuto, quindi
                    # NON si mette in pausa il dominio: si segna l'errore e
                    # si prosegue, la pagina resta "visitata" con 0 link da
                    # essa invece di far fallire l'intero crawl.
                    try:
                        if not self._looks_like_html(resp):
                            logger.info(
                                "[%s] %s ha Content-Type '%s' (non HTML): nessun link estratto",
                                self.country_code, page_url, resp.headers.get("Content-Type", ""),
                            )
                            links_found: list[str] = []
                            resource_refs: list[tuple[str, str]] = []
                        else:
                            links_found = self._extract_links(resp.text, page_url)
                            # Stesso HTML già in memoria, nessuna richiesta
                            # aggiuntiva: vedi _extract_resource_refs() per
                            # il razionale (input del post-processing "link
                            # sospetti", tools/postprocess_checkpoints.py).
                            resource_refs = self._extract_resource_refs(resp.text, page_url)

                        for abs_url in links_found:
                            dom2 = self._domain_of(abs_url)
                            if not dom2:
                                continue
                            if self._domain_matches(dom2, exclude_domains):
                                continue

                            is_internal = self._domain_matches(dom2, allowed_domains) or self._tld_matches(
                                dom2, allowed_tlds
                            )
                            if is_internal:
                                if abs_url not in visited and not _estensione_da_evitare(abs_url):
                                    next_level.append(abs_url)
                                elif _estensione_da_evitare(abs_url):
                                    # Registrato per completezza (compare comunque
                                    # nell'audit "interno"), ma MAI messo in coda:
                                    # niente richiesta HTTP verrà mai tentata su
                                    # questo url (vedi NON_CRAWLABLE_EXTENSIONS).
                                    visited.add(abs_url)
                                    logger.debug(
                                        "[%s] estensione non-pagina, salto senza scaricare: %s",
                                        self.country_code, abs_url,
                                    )
                                page_origin_seed.setdefault(abs_url, origin_seed)
                                if record_internal:
                                    _register(abs_url, "interno", page_url, level)
                            else:
                                _register(abs_url, "esterno", page_url, level)

                        # Popola l'indice occorrenze SOLO per risorse
                        # esterne (terze parti): quelle interne non sono
                        # rilevanti per il post-processing "link sospetti" e
                        # gonfierebbero inutilmente il checkpoint.
                        for tipo, abs_url in resource_refs:
                            dom3 = self._domain_of(abs_url)
                            if not dom3 or self._domain_matches(dom3, exclude_domains):
                                continue
                            is_internal3 = self._domain_matches(dom3, allowed_domains) or self._tld_matches(
                                dom3, allowed_tlds
                            )
                            if not is_internal3:
                                _register_resource(abs_url, tipo, page_url, level)

                            # Paginazione numerica: se il link scoperto fa
                            # parte di una sequenza (page_1, page_2, ...),
                            # non ci si limita a seguirlo come singolo link
                            # interno soggetto a 'crawl_depth' — si scarica
                            # ESPLICITAMENTE l'intera sequenza (vedi
                            # _expand_pagination_sequence) e si aggiungono
                            # al livello successivo tutti i link annidati
                            # trovati su ciascuna delle sue pagine.
                            if follow_pagination:
                                template = self._pagination_template(abs_url, pagination_regex)
                                if template and template.lower() not in pagination_templates_seen:
                                    pagination_templates_seen.add(template.lower())
                                    logger.info(
                                        "[%s] paginazione rilevata su %s: espando sequenza per template %s",
                                        self.country_code, page_url, template,
                                    )
                                    found_via_pagination = self._expand_pagination_sequence(
                                        template,
                                        found_on=page_url,
                                        level=level,
                                        visited=visited,
                                        exclude_domains=exclude_domains,
                                        allowed_domains=allowed_domains,
                                        allowed_tlds=allowed_tlds,
                                        record_internal=record_internal,
                                        register=_register,
                                        sleep_s=pagination_sleep_s,
                                        start_page=pagination_start_page,
                                        max_pages=pagination_max_pages,
                                        stop_after_empty=pagination_stop_after_empty,
                                        page_origin_seed=page_origin_seed,
                                        origin_seed=origin_seed,
                                        pages_by_seed_level=pages_by_seed_level,
                                        checkpoint_cb=_checkpoint_now,
                                    )
                                    next_level.extend(u for u in found_via_pagination if u not in next_level)
                    except Exception as exc:  # noqa: BLE001 - errore di CONTENUTO: mai fatale
                        logger.warning(
                            "[%s] errore inatteso nell'analizzare il contenuto di %s (%s): pagina segnata "
                            "come visitata con 0 link da essa, il crawl prosegue: %s",
                            self.country_code, page_url, exc.__class__.__name__, exc,
                        )
                        error_log.append({
                            "url": page_url, "level": level, "seed": origin_seed,
                            "error_type": exc.__class__.__name__, "message": str(exc),
                            "timestamp": now_iso(), "outcome": "errore di contenuto, pagina saltata",
                        })

                    # Checkpoint dopo OGNI pagina processata: è il grado di
                    # copertura di --resume (nel caso peggiore si riprocessa
                    # al massimo l'ultima pagina in corso al momento
                    # dell'interruzione, mai più di quella).
                    _checkpoint_now()
                    time.sleep(sleep_s)

                to_visit = list(dict.fromkeys(next_level))
                logger.info(
                    "[%s] livello %d completato: %d pagine visitate finora, %d domini radice unici registrati finora",
                    self.country_code, level, len(visited), len(domain_registry),
                )
                self._safe_report(
                    self._write_progress_report,
                    seeds=seeds, visited=visited, domain_registry=domain_registry,
                    pages_by_seed_level=pages_by_seed_level, depth=depth, current_level=level + 1,
                )
                self._safe_report(
                    self._write_markdown_report,
                    seeds=seeds, visited=visited, domain_registry=domain_registry,
                    pages_by_seed_level=pages_by_seed_level, depth=depth, queue=[],
                    domain_cooldown_until_call=domain_cooldown_until_call,
                    domain_cooldown_retries_used=domain_cooldown_retries_used,
                    global_call_index=global_call_index, error_log=error_log, status=status,
                )
        except KeyboardInterrupt:
            status = "interrotto dall'utente (Ctrl+C)"
            self._safe_report(
                self._write_progress_report,
                seeds=seeds, visited=visited, domain_registry=domain_registry,
                pages_by_seed_level=pages_by_seed_level, depth=depth, current_level=level,
            )
            self._safe_report(
                self._write_markdown_report,
                seeds=seeds, visited=visited, domain_registry=domain_registry,
                pages_by_seed_level=pages_by_seed_level, depth=depth, queue=list(queue),
                domain_cooldown_until_call=domain_cooldown_until_call,
                domain_cooldown_retries_used=domain_cooldown_retries_used,
                global_call_index=global_call_index, error_log=error_log, status=status,
            )
            logger.warning(
                "[%s] crawl interrotto dall'utente: checkpoint salvato in %s (dettaglio per seed in %s, "
                "report in %s) — rilancia lo stesso comando con --resume per riprendere da qui",
                self.country_code, self._checkpoint_path(), self._progress_report_path(),
                self._report_md_path(),
            )
            raise
        except Exception:
            # Con la gestione errori per-pagina sopra, questo blocco copre
            # solo problemi STRUTTURALI inattesi (bug nel codice di
            # transizione tra livelli, errori nella scrittura dei report,
            # ecc.) — non più i normali intoppi di rete/contenuto di una
            # singola pagina, che ormai non fanno più fallire il crawl.
            status = "interrotto da un errore imprevisto"
            self._safe_report(
                self._write_progress_report,
                seeds=seeds, visited=visited, domain_registry=domain_registry,
                pages_by_seed_level=pages_by_seed_level, depth=depth, current_level=level,
            )
            self._safe_report(
                self._write_markdown_report,
                seeds=seeds, visited=visited, domain_registry=domain_registry,
                pages_by_seed_level=pages_by_seed_level, depth=depth, queue=list(queue),
                domain_cooldown_until_call=domain_cooldown_until_call,
                domain_cooldown_retries_used=domain_cooldown_retries_used,
                global_call_index=global_call_index, error_log=error_log, status=status,
            )
            logger.warning(
                "[%s] crawl interrotto da un errore imprevisto: checkpoint salvato in %s (dettaglio per "
                "seed in %s, report in %s) — rilancia lo stesso comando con --resume per riprendere da qui",
                self.country_code, self._checkpoint_path(), self._progress_report_path(),
                self._report_md_path(),
            )
            raise

        out = self.input_dir / self.config.get("input_filename", "crawl_results.json")
        deduped = list(domain_registry.values())
        out.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
        # Crawl completato: il checkpoint NON viene più cancellato (prima
        # veniva eliminato qui) — resta su disco, marcato 'completed: true',
        # come traccia permanente dell'ultimo run riuscito. Un successivo
        # '--resume' lo riconosce come già concluso e rigenera l'output
        # all'istante dal 'domain_registry' salvato, senza ripetere il
        # crawl (vedi il controllo su 'completed' più sopra in questo
        # metodo). Senza '--resume' si ignora e si riparte comunque da
        # zero, come per un checkpoint normale.
        status = "completato"
        self._safe_report(
            self._save_checkpoint,
            fingerprint=fingerprint, fingerprint_fields=fingerprint_fields, level=depth, queue=[],
            visited=visited, next_level=[], domain_registry=domain_registry,
            pagination_templates_seen=pagination_templates_seen, page_origin_seed=page_origin_seed,
            pages_by_seed_level=pages_by_seed_level, domain_consecutive_errors=domain_consecutive_errors,
            domain_cooldown_until_call=domain_cooldown_until_call,
            domain_cooldown_retries_used=domain_cooldown_retries_used,
            global_call_index=global_call_index, error_log=error_log, completed=True,
        )
        self._safe_report(
            self._write_progress_report,
            seeds=seeds, visited=visited, domain_registry=domain_registry,
            pages_by_seed_level=pages_by_seed_level, depth=depth, current_level=None,
        )
        self._safe_report(
            self._write_markdown_report,
            seeds=seeds, visited=visited, domain_registry=domain_registry,
            pages_by_seed_level=pages_by_seed_level, depth=depth, queue=[],
            domain_cooldown_until_call=domain_cooldown_until_call,
            domain_cooldown_retries_used=domain_cooldown_retries_used,
            global_call_index=global_call_index, error_log=error_log, status=status,
        )
        logger.info(
            "[%s] crawl completato: %d pagine visitate, %d domini radice unici (inclusi i seed) -> %s "
            "(report: %s, checkpoint conservato: %s)",
            self.country_code, len(visited), len(deduped), out, self._report_md_path(),
            self._checkpoint_path(),
        )
        return out

    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        return json.loads(raw_path.read_text(encoding="utf-8"))


FETCHER_REGISTRY = {
    "bulk_csv": BulkCSVFetcher,
    "bulk_xlsx": BulkXLSXFetcher,
    "bulk_xlsx_folder": BulkXlsxFolderFetcher,
    "bulk_xml": BulkXMLFetcher,
    "api_json": ApiJsonFetcher,
    "html_scrape": HtmlScrapeFetcher,
    "site_crawl": SiteCrawlFetcher,
}
