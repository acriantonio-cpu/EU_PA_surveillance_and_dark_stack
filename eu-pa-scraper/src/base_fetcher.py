"""
Classi base condivise da tutti i fetcher (uno per paese, ma pilotati dalla
stessa engine generica + config YAML).

Flusso comune per ogni paese:
    1. download()   -> scarica il file grezzo (o pagina HTML) in data/input/{ISO}/
    2. parse()      -> trasforma il grezzo in una lista di dict (record raw)
    3. normalize()  -> mappa i record raw sullo schema comune (Record)
"""

from __future__ import annotations

import logging
import os
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable

import requests

from .schema import Record, now_iso
from .urlutil import dedupe_key_for_url, normalize_url_values

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    # NOTA: header "da browser" (Chrome su Windows) invece di uno User-Agent
    # che si autodichiara scraper. Motivo: molti siti (visto con il Belgio,
    # es. chancellery.belgium.be -> 403, ibz.be -> connessione interrotta)
    # usano firewall/WAF che bloccano automaticamente User-Agent non
    # riconosciuti o header incompleti (mancava Accept-Language,
    # Accept-Encoding, ecc.), indipendentemente da un'intenzione specifica
    # di bloccare la ricerca. Con header "completi" da browser molti di
    # questi blocchi generici si evitano. Restano BLOCCHI DELIBERATI (es.
    # robots.txt che nega l'accesso, o un vero anti-bot avanzato) che
    # nessun header può aggirare, ed è corretto che sia così: in quei casi
    # la scelta di procedere comunque spetta a chi esegue lo scraping (vedi
    # i disclaimer nei config dei singoli paesi). Per ripristinare uno User-
    # Agent che si identifica esplicitamente come questo tool, passare
    # 'headers={"User-Agent": "..."}' al costruttore di HttpSession.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8,nl;q=0.7,de;q=0.6",
    # NOTA: niente 'br' (Brotli) qui di proposito. 'requests' dichiara di
    # accettare Brotli ma sa DECOMPRIMERLO solo se il pacchetto 'brotli' (o
    # 'brotlicffi') è installato — che di default NON lo è in questo
    # progetto (non è in requirements.txt). Se un sito risponde in Brotli e
    # il pacchetto manca, 'requests' passa i byte COMPRESSI GREZZI a
    # resp.text, che il parser HTML scambia per markup corrotto (causa
    # riscontrata di ParserRejectedMarkup con bytes senza senso tipo
    # '<!["\x0c&...' su alcuni siti SE/DE). gzip/deflate bastano per la
    # stragrande maggioranza dei siti e sono supportati nativamente, senza
    # dipendenze aggiuntive.
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3
DEFAULT_SLEEP_BETWEEN_RETRIES = 2.0
MAX_RETRY_AFTER_SECONDS = 60.0

# Codici HTTP per cui ha senso ritentare: il problema può essere transitorio.
# Tutti gli altri 4xx (404, 410, 400, 401, 403, 451...) NON vengono ritentati:
# un link morto resta morto, e ritentarlo 3 volte con backoff sprecava ~12 s
# per ogni 404. 403 non è ritentato qui perché di norma è un blocco (ritentare
# subito non serve): la gestione "pausa e riprova più tardi" è del crawler.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

_HTMLISH = ("text/html", "application/xhtml", "text/xml", "application/xml", "text/plain")
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([a-zA-Z0-9_\-:]+)", re.IGNORECASE)


class NonRetryableHTTPError(requests.HTTPError):
    """HTTPError che HttpSession.get NON ha ritentato (4xx definitivo)."""


_DNS_ERROR_MARKERS = (
    "name or service not known", "nodename nor servname", "no address associated",
    "temporary failure in name resolution", "getaddrinfo failed", "name resolution error",
    "nameresolutionerror",
)


def _is_dns_error(exc: BaseException) -> bool:
    """Un ConnectionError il cui messaggio indica un fallimento di risoluzione DNS. A differenza di
    un timeout o di un rifiuto di connessione (spesso transitori: firewall/WAF sotto carico), un
    hostname che non risolve resta tale per tutta la durata del run: ritentarlo 2-3 volte con
    backoff non serve a nulla, spreca tempo e allunga i log senza cambiare l'esito."""
    return isinstance(exc, requests.exceptions.ConnectionError) and any(
        m in str(exc).lower() for m in _DNS_ERROR_MARKERS
    )


def _suppress_insecure_warning() -> None:
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:  # noqa: BLE001
        pass


def _guess_encoding(content: bytes, header_encoding: str | None, content_type: str) -> str:
    """Encoding più plausibile per 'content'. Ordine: BOM, charset esplicito
    nell'header, <meta charset> nei primi KB, rilevamento statistico, utf-8.
    Evita il default ISO-8859-1 di requests per text/* senza charset (mojibake)."""
    if content.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    ct = (content_type or "").lower()
    explicit = "charset=" in ct
    if explicit and header_encoding:
        return header_encoding
    m = _META_CHARSET_RE.search(content[:8192])
    if m:
        try:
            enc = m.group(1).decode("ascii", "ignore").strip()
            "".encode(enc)  # valida che il codec esista
            return enc
        except (LookupError, ValueError):
            pass
    try:
        content[:200_000].decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(content[:200_000]).best()
        if best is not None and best.encoding:
            return best.encoding
    except Exception:  # noqa: BLE001
        pass
    return "utf-8"


class HttpSession:
    """Wrapper su requests.Session con retry intelligente, download con tetto
    di dimensione, controllo del Content-Type PRIMA di scaricare il corpo,
    fallback TLS opzionale e correzione dell'encoding.

    get() accetta, oltre ai normali kwargs di requests:
      max_bytes=N            legge al massimo N byte del corpo (resp.truncated=True se tagliato)
      skip_non_html=True     se il Content-Type dichiarato non è HTML/XML/testo NON scarica
                             il corpo (resp.content == b'', header intatti): chi chiama
                             controlla il Content-Type come prima
      read_timeout=S         timeout di lettura (connect resta a min(10, timeout))
      allow_insecure_fallback=True
                             se il certificato TLS non è verificabile, riprova UNA volta con
                             verify=False e marca resp.tls_error=True. Da usare solo per
                             pagine di discovery/homepage, MAI per il download dei dati ufficiali.
    """

    def __init__(self, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.max_redirects = 8
        if headers:
            self.session.headers.update(headers)
        self.timeout = timeout

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _retry_after_seconds(resp: requests.Response | None) -> float | None:
        if resp is None:
            return None
        ra = resp.headers.get("Retry-After")
        if not ra:
            return None
        try:
            return max(0.0, min(float(ra), MAX_RETRY_AFTER_SECONDS))
        except ValueError:
            return None  # formato data HTTP: si usa il backoff normale

    def _one_request(
        self, url: str, *, timeout: Any, max_bytes: int | None, skip_non_html: bool, verify: bool, **kwargs
    ) -> requests.Response:
        explicit_stream = bool(kwargs.pop("stream", False))
        stream = bool(max_bytes or skip_non_html or explicit_stream)
        resp = self.session.get(url, timeout=timeout, stream=stream, verify=verify, **kwargs)
        ctype = resp.headers.get("Content-Type", "")
        if skip_non_html and ctype and not any(t in ctype.lower() for t in _HTMLISH):
            resp.close()
            resp._content = b""  # noqa: SLF001
            resp._content_consumed = True  # noqa: SLF001
            resp.truncated = False  # type: ignore[attr-defined]
            resp.raise_for_status()
            return resp
        if stream:
            chunks: list[bytes] = []
            total = 0
            truncated = False
            try:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if max_bytes and total >= max_bytes:
                        truncated = True
                        break
            finally:
                resp.close()
            content = b"".join(chunks)
            if max_bytes:
                content = content[:max_bytes]
            resp._content = content  # noqa: SLF001
            resp._content_consumed = True  # noqa: SLF001
            resp.truncated = truncated  # type: ignore[attr-defined]
        else:
            resp.truncated = False  # type: ignore[attr-defined]
        resp.raise_for_status()
        if any(t in ctype.lower() for t in _HTMLISH) or not ctype:
            resp.encoding = _guess_encoding(resp.content, resp.encoding, ctype)
        return resp

    # -- API pubblica ----------------------------------------------------
    def get(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        skip_non_html: bool = False,
        read_timeout: float | None = None,
        allow_insecure_fallback: bool = False,
        retries: int = DEFAULT_RETRIES,
        **kwargs,
    ) -> requests.Response:
        timeout: Any = self.timeout
        if read_timeout is not None:
            timeout = (min(10.0, float(self.timeout)), float(read_timeout))
        last_exc: Exception | None = None
        verify = True
        tls_error = False
        attempt = 0
        while attempt < retries:
            attempt += 1
            try:
                resp = self._one_request(
                    url, timeout=timeout, max_bytes=max_bytes, skip_non_html=skip_non_html,
                    verify=verify, **dict(kwargs),
                )
                resp.tls_error = tls_error  # type: ignore[attr-defined]
                return resp
            except requests.exceptions.SSLError as exc:
                last_exc = exc
                if allow_insecure_fallback and verify:
                    logger.info("TLS non verificabile per %s (%s): riprovo senza verifica (solo discovery)", url, exc.__class__.__name__)
                    verify = False
                    tls_error = True
                    _suppress_insecure_warning()
                    attempt -= 1  # il tentativo senza verifica non consuma un retry
                    continue
                break  # errore di certificato: ritentare uguale non serve
            except requests.HTTPError as exc:
                last_exc = exc
                status = exc.response.status_code if exc.response is not None else None
                if status not in RETRYABLE_STATUS:
                    # 4xx definitivo (404/410/403...): niente retry, niente sleep.
                    nre = NonRetryableHTTPError(str(exc), response=exc.response)
                    raise nre from exc
                wait = self._retry_after_seconds(exc.response)
                logger.warning("GET %s -> HTTP %s (tentativo %d/%d)", url, status, attempt, retries)
                if attempt < retries:
                    time.sleep(wait if wait is not None else DEFAULT_SLEEP_BETWEEN_RETRIES * attempt)
            except requests.RequestException as exc:  # pragma: no cover
                last_exc = exc
                if _is_dns_error(exc):
                    # DNS non transitorio: un solo tentativo, niente sleep, log una riga sola.
                    logger.warning("GET %s: host non risolvibile (DNS): %s", url, exc)
                    break
                logger.warning("GET %s fallito (tentativo %d/%d): %s", url, attempt, retries, exc)
                if attempt < retries:  # niente sleep DOPO l'ultimo tentativo
                    time.sleep(DEFAULT_SLEEP_BETWEEN_RETRIES * attempt)
        raise last_exc  # type: ignore[misc]

    def download_to_file(self, url: str, dest: Path, *, chunk_size: int = 1 << 20, retries: int = DEFAULT_RETRIES) -> int:
        """Scarica 'url' direttamente su disco in streaming (nessun caricamento
        in RAM: per file da centinaia di MB, es. archivio FR). Scrive su un file
        temporaneo e lo rinomina solo a download completo; un retry riparte da zero.
        Ritorna i byte scritti."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                with self.session.get(url, timeout=(10, max(self.timeout, 60)), stream=True) as resp:
                    resp.raise_for_status()
                    written = 0
                    with tmp.open("wb") as f:
                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                written += len(chunk)
                _replace_with_retry(tmp, dest)
                return written
            except requests.RequestException as exc:
                last_exc = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if _is_dns_error(exc):
                    logger.warning("download %s: host non risolvibile (DNS): %s", url, exc)
                    break
                logger.warning("download %s fallito (tentativo %d/%d): %s", url, attempt, retries, exc)
                if isinstance(exc, requests.HTTPError) and status not in RETRYABLE_STATUS:
                    break
                if attempt < retries:
                    time.sleep(DEFAULT_SLEEP_BETWEEN_RETRIES * attempt)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        raise last_exc  # type: ignore[misc]


def _replace_with_retry(src: Path, dst: Path, attempts: int = 5) -> None:
    """os.replace con qualche ritentativo: su Windows antivirus/OneDrive/Excel
    possono tenere il file di destinazione aperto per un istante."""
    for i in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts:
                raise
            time.sleep(0.4 * i)


class BaseFetcher(ABC):
    """Interfaccia comune. Ogni fetcher concreto implementa i 3 step."""

    country_code: str = ""
    source_name: str = ""

    def __init__(self, config: dict, input_dir: Path, output_dir: Path):
        self.config = config
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.http = HttpSession()
        # Impostato da main.py quando l'utente lancia con '--resume': i
        # fetcher che supportano un checkpoint su disco (es. SiteCrawlFetcher)
        # lo leggono per riprendere un'analisi interrotta invece di ripartire
        # da zero. Ignorato dai fetcher che non lo supportano.
        self.resume: bool = False

    @abstractmethod
    def download(self) -> Path:
        """Scarica il/i file grezzi in self.input_dir e ne ritorna il path."""

    @abstractmethod
    def parse(self, raw_path: Path) -> list[dict[str, Any]]:
        """Ritorna una lista di record grezzi (dict con le colonne originali)."""

    def normalize(self, raw_rows: Iterable[dict[str, Any]]) -> list[Record]:
        """
        Mappa i record raw sullo schema comune usando 'field_mapping' dal
        config YAML: {output_field: source_field_or_dotted_path}.
        Sovrascrivere per logiche di mapping più complesse (es. XML nested).
        """
        mapping: dict[str, str] = self.config.get("field_mapping", {})
        records: list[Record] = []
        ts = now_iso()
        valid_fields = set(Record.__dataclass_fields__)
        for row in raw_rows:
            rec = Record(country_code=self.country_code, source_name=self.source_name, retrieved_at=ts)
            for out_field, src_field in mapping.items():
                if not src_field or out_field not in valid_fields:
                    continue
                value = _dig(row, src_field)
                text = _cell_to_text(value)
                if text is not None:
                    setattr(rec, out_field, text)
            records.append(rec)
        return records

    def postprocess_records(self, records: list[Record]) -> list[Record]:
        """Normalizzazione comune a TUTTI i paesi, applicata dopo normalize():
        - 'hostname' ripulito (URL valido con schema, host minuscolo/punycode);
          se la cella conteneva più URL ('a.es; b.es') il primo va in 'hostname'
          e gli altri in 'host_osservati' (non si perdono);
        - 'dominio_radice' (eTLD+1) calcolato per ogni record con hostname.
        Un valore che non è un sito ('nan', 'n/d', un'email) diventa ''."""
        from .fetchers_common import root_domain_of  # import locale: evita cicli

        for rec in records:
            raw = rec.hostname
            if not raw:
                continue
            urls = normalize_url_values(raw)
            if not urls:
                rec.hostname = ""
                continue
            rec.hostname = urls[0]
            extra = [u for u in urls[1:]]
            if extra:
                merged = [x for x in (rec.host_osservati.split("|") if rec.host_osservati else []) if x]
                for u in extra:
                    if u not in merged:
                        merged.append(u)
                rec.host_osservati = "|".join(merged)
            if not rec.dominio_radice:
                rec.dominio_radice = root_domain_of(rec.hostname)
        return records

    def run(self) -> list[Record]:
        raw_path = self.download()
        raw_rows = self.parse(raw_path)
        logger.info("[%s] %d record grezzi estratti", self.country_code, len(raw_rows))
        records = self.normalize(raw_rows)
        records = self.postprocess_records(records)

        dedupe_field = self.config.get("dedupe_by")
        if dedupe_field:
            seen: set[str] = set()
            deduped: list[Record] = []
            for rec in records:
                raw_key = getattr(rec, dedupe_field, "") or ""
                # chiave case-insensitive; per gli URL si ignora schema e 'www.'
                key = dedupe_key_for_url(raw_key) if dedupe_field == "hostname" else str(raw_key).strip().lower()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                deduped.append(rec)
            logger.info(
                "[%s] dedupe_by='%s': %d -> %d record",
                self.country_code, dedupe_field, len(records), len(deduped),
            )
            records = deduped

        return records


def _cell_to_text(value: Any) -> str | None:
    """Valore di cella -> testo pulito. None/NaN -> None (campo lasciato vuoto).
    Un float intero (tipico di Excel: 12345.0) diventa '12345', non '12345.0'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        if value.is_integer():
            return str(int(value))
    if isinstance(value, (list, tuple)):
        return "|".join(str(v).strip() for v in value if v is not None and str(v).strip())
    return str(value).strip()


def _dig(row: dict, dotted_path: str) -> Any:
    """Accede a row['a']['b'] tramite 'a.b'. Ritorna None se manca."""
    cur: Any = row
    for part in dotted_path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur
