# EN: URL utilities: canonicalisation for deduplication, robust host extraction, IDNA
# normalisation.
"""
Utility URL condivise da crawler, fetcher e classificatore.

Tre gruppi di funzioni:

1. canonicalize_url()   -> forma canonica di un URL per deduplicare le
   pagine da visitare (strip fragment, tracking, jsessionid, host in
   minuscolo/punycode, query ordinata, slash finale). Evita che
   '/p#a', '/p#b', '/p/', '/p?utm_source=x', 'HTTP://Example.be/p' siano
   contati come 5 pagine diverse.
2. host_of() / is_ip_host() -> estrazione robusta dell'host (niente
   split(":") che rompe su 'user:pw@host'), normalizzazione IDNA.
3. split_url_values() / normalize_url_value() -> pulizia dei valori
   "hostname" provenienti dalle fonti ufficiali (Excel/CSV/JSON):
   'nan', 'n/d', valori multipli separati da ';', email, senza schema...
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Parametri di query che identificano la SESSIONE o il TRACKING e non il
# contenuto della pagina.
_TRACKING_PARAM_PREFIXES = ("utm_", "mtm_", "pk_", "hsa_", "vero_")
_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid", "_ga", "_gl",
    "igshid", "ref_src", "sessionid", "session_id", "sid", "phpsessid", "jsessionid",
    "cfid", "cftoken", "aspsessionid", "sessid",
})
_JSESSION_RE = re.compile(r";jsessionid=[^/?#;]*", re.IGNORECASE)
_ANY_PATH_PARAM_RE = re.compile(r";[^/?#]*")

_NULL_TOKENS = frozenset({
    "", "nan", "none", "null", "n/a", "na", "n/d", "nd", "-", "--", "—", "?", "#n/a",
    "non disponibile", "non disponible", "geen", "keine", "ninguno", "nincs",
})


def is_ip_host(host: str) -> bool:
    """True se 'host' è un indirizzo IP letterale (v4 o v6)."""
    h = (host or "").strip("[]")
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def normalize_host(host: str) -> str:
    """Host in minuscolo, senza punto finale, IDN convertito in punycode.
    Se la conversione IDNA fallisce (host malformato) ritorna l'host così com'è."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return ""
    if is_ip_host(h):
        return h.strip("[]")
    try:
        return h.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return h


def host_of(url: str) -> str:
    """Host normalizzato di un URL, '' se non estraibile. Usa
    urlparse().hostname (gestisce userinfo e porta), mai split(':')."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:  # es. "Invalid IPv6 URL"
        return ""
    return normalize_host(host)


def canonicalize_url(url: str) -> str:
    """Forma canonica per la deduplicazione. Ritorna l'input invariato se
    non è un URL http(s) interpretabile."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url
    scheme = (p.scheme or "").lower()
    if scheme not in ("http", "https") or not p.netloc:
        return url
    host = normalize_host(p.hostname or "")
    if not host:
        return url
    port = p.port if _safe_port(p) else None
    netloc = host
    if ":" in host and not host.startswith("["):  # IPv6 nudo
        netloc = f"[{host}]"
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"

    path = _JSESSION_RE.sub("", p.path or "/")
    # Altri parametri di percorso ';x=y' (es. ';jsessionid' già tolto) su un
    # segmento sono quasi sempre sessioni/tracking nei CMS legacy.
    path = _ANY_PATH_PARAM_RE.sub("", path)
    path = re.sub(r"/{2,}", "/", path) or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query_pairs = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if kl in _TRACKING_PARAMS or kl.startswith(_TRACKING_PARAM_PREFIXES):
            continue
        query_pairs.append((k, v))
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def _safe_port(p) -> bool:
    try:
        p.port  # noqa: B018 - può sollevare ValueError su porta non numerica
        return True
    except ValueError:
        return False


def url_pattern_signature(url: str) -> str:
    """Firma 'strutturale' di un URL: host + path con i numeri sostituiti da
    '#' + chiavi della query (senza i valori). Serve al limite anti-spider-trap:
    /calendario/2026/10/03?month=10 e /calendario/2026/11/04?month=11 hanno la
    stessa firma."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    path = re.sub(r"\d+", "#", p.path or "/")
    keys = ",".join(sorted({k.lower() for k, _ in parse_qsl(p.query, keep_blank_values=True)}))
    return f"{(p.hostname or '').lower()}{path}?{keys}"


def looks_like_trap(url: str, *, max_len: int = 2000, max_depth: int = 14) -> bool:
    """Euristiche a costo zero contro le trappole tipiche: URL lunghissimi,
    percorsi profondissimi, segmenti che si ripetono (/a/b/a/b/a/b)."""
    if len(url) > max_len:
        return True
    try:
        path = urlparse(url).path
    except ValueError:
        return True
    segs = [s for s in path.split("/") if s]
    if len(segs) > max_depth:
        return True
    # stesso segmento ripetuto >= 3 volte (o coppia ripetuta): loop di link relativi
    for s in set(segs):
        n = segs.count(s)
        if (n >= 3 and len(s) > 1) or n >= 4:
            return True
    return False


# --------------------------------------------------------------------------
# Valori "hostname" dalle fonti ufficiali
# --------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"[;\n\r\t|]+|,\s+|\s{2,}|\s+(?:e|et|und|y|and|en|och|og|ja)\s+(?=(?:https?://|www\.))", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s/]+@[^@\s/]+\.[a-z]{2,}$", re.IGNORECASE)
_HOSTLIKE_RE = re.compile(r"^(?:[a-z0-9\u00a1-\uffff](?:[a-z0-9\u00a1-\uffff\-]{0,61}[a-z0-9\u00a1-\uffff])?\.)+[a-z\u00a1-\uffff]{2,}$", re.IGNORECASE)


def split_url_values(raw) -> list[str]:
    """Spezza un valore che può contenere più URL ('www.a.es; www.b.es')."""
    if raw is None:
        return []
    s = str(raw).strip()
    if s.lower() in _NULL_TOKENS:
        return []
    parts = [x.strip().strip("<>\"' ") for x in _SPLIT_RE.split(s)]
    # anche gli spazi singoli separano URL diversi se ciascun pezzo sembra un URL/host
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if " " in part:
            sub = [x for x in part.split() if x]
            if len(sub) > 1 and all(("." in x) for x in sub):
                out.extend(sub)
                continue
        out.append(part)
    return out


def normalize_url_value(raw) -> str:
    """Un singolo valore -> 'scheme://host[/path]' pulito, oppure '' se non è
    un sito valido (email, 'nan', testo libero...). Se manca lo schema si
    assume https. Il fragment viene rimosso, host in minuscolo/punycode."""
    s = str(raw).strip().strip("<>\"' ") if raw is not None else ""
    if s.lower() in _NULL_TOKENS:
        return ""
    low = s.lower()
    if low.startswith(("mailto:", "tel:", "javascript:")) or _EMAIL_RE.match(s):
        return ""
    if "://" not in s:
        if s.startswith("//"):
            s = "https:" + s
        else:
            s = "https://" + s
    try:
        p = urlparse(s)
    except ValueError:
        return ""
    if p.scheme.lower() not in ("http", "https"):
        return ""
    host = normalize_host(p.hostname or "")
    if not host or (not is_ip_host(host) and not _HOSTLIKE_RE.match(host)):
        return ""
    netloc = host
    try:
        port = p.port
    except ValueError:
        port = None
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    path = p.path or ""
    if path in ("", "/"):
        path = ""
    else:
        path = re.sub(r"/{2,}", "/", path)
        path = path.rstrip("/")
    return urlunparse((p.scheme.lower(), netloc, path, "", p.query, ""))


def normalize_url_values(raw) -> list[str]:
    """Tutti gli URL validi e distinti (ordine di comparsa) presenti in 'raw'."""
    seen: set[str] = set()
    out: list[str] = []
    for part in split_url_values(raw):
        n = normalize_url_value(part)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def dedupe_key_for_url(url: str) -> str:
    """Chiave di dedupe per URL: host senza 'www.' + path, schema ignorato."""
    n = normalize_url_value(url)
    if not n:
        return (url or "").strip().lower()
    p = urlparse(n)
    host = p.netloc
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{p.path}".lower()
