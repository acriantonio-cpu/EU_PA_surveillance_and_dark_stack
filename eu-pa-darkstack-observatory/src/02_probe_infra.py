#!/usr/bin/env python3
# EN: Step 02 - infrastructure probe per entity: DNS (NS/MX/A/AAAA/DNSSEC/CAA/SPF/DMARC), hosting
# ASN/operator (Team Cymru bulk, RIPEstat, RDAP fallback), TLS (issuer/expiry/weak
# protocols/cipher/HSTS/grade), HTTP headers/CDN/response time. Output: risultati.csv.
"""
02_probe_infra.py

Sonda B: per ogni riga di data/processed/siti.csv (colonna 'hostname'),
raccoglie:
- DNS: NS, MX, A, AAAA, DNSSEC (DS), CAA, SPF, DMARC
- ASN/operatore (via RDAP) per IP web (A), mail (MX) e nameserver (NS)
- TLS: emittente del certificato, versione negoziata, giorni alla scadenza
  (letto SENZA validare la catena: un certificato scaduto o self-signed è
  comunque un dato interessante, non un motivo per non leggerlo)
- TLS avanzato: protocolli deprecati (SSLv3/TLS1.0/1.1) ancora accettati dal
  server, cipher suite e bit della sessione negoziata, presenza di HSTS, e
  un voto sintetico tls_grade (A-D) calcolato da questi tre dati
- HTTP: header Server/Via, rilevamento euristico di CDN/proxy noti, tempo di
  risposta in millisecondi
- euristica di giurisdizione della holding che gestisce l'hosting (utile per
  la lettura "server in UE ma operatore soggetto a legge extra-UE")

Ogni gruppo di misurazioni può essere attivato/disattivato da config.yaml
nella project root (chiavi sotto 'misurazioni'): utile perché tls_avanzato in
particolare è sensibilmente più lento delle altre (fino a 4 connessioni TLS
extra per hostname). Vedi config.py per i dettagli di come viene letto.

Scrive un CSV e un manifesto della misurazione (timestamp, versioni, config)
per riproducibilità.

Enti diversi possono condividere lo stesso hostname (unione di comuni,
portale scolastico comune...): questo script sonda ogni hostname UNA sola
volta e riusa il risultato per tutte le righe di input che lo condividono
(vedi 01_fetch_ipa_comuni.py, colonna hostname_condiviso_con_altri_enti).

Va eseguito dalla cartella principale del progetto.

Esempi:
    python src\\02_probe_infra.py --limit 20
    python src\\02_probe_infra.py --delay 0.5
    python src\\02_probe_infra.py --resume
    python src\\02_probe_infra.py --skip-http   # solo DNS, molto più veloce
    python src\\02_probe_infra.py --concorrenza 25   # più veloce su un campione grande
    python src\\02_probe_infra.py --asn-metodo rdap   # se la porta 43/TCP è bloccata dalla rete
"""

import argparse
import csv
import json
import platform
import socket
import ssl
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import dns.resolver
import dns.name
import dns.rdatatype
import dns.version
import pandas as pd
import requests
import urllib3
from cryptography import x509
from cryptography.x509.oid import NameOID
from ipwhois import IPWhois
from ipwhois.exceptions import IPDefinedError, ASNRegistryError, HTTPLookupError

import asn_bulk
import as_hegemony
import config as cfgmod
import rate_limiter

warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
# NOTA: il filtro DeprecationWarning per SSLv3/TLSv1/TLSv1_1 non va messo
# qui a livello di modulo con module="ssl" — non funziona, perché Python
# attribuisce l'avviso al modulo CHIAMANTE (questo script, __main__ quando
# lanciato da riga di comando), non al modulo 'ssl'. Il filtro che funziona
# davvero è locale, dentro _prova_singolo_protocollo() più sotto (bug
# scoperto grazie a un run reale che riempiva la console di avvisi).

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def dati_processed(paese: str) -> Path:
    return PROJECT_ROOT / "data" / "processed" / paese


def dati_results(paese: str, run_id: str) -> Path:
    """OTTIMIZZAZIONE (seconda passata, settembre 2026): i risultati di un
    run vanno ora in data/results/<paese>/<run_id>/, non più direttamente
    in data/results/<paese>/ — vedi config.py, sezione 'esecuzioni con
    timestamp', per il perché (in breve: senza questo, un run successivo
    sovrascriveva il precedente e non si poteva costruire uno storico)."""
    return PROJECT_ROOT / "data" / "results" / paese / run_id

FIELDS = [
    "hostname", "comune", "regione", "codice_ipa",
    "ns", "n_ns", "ns_diversi_secondlevel",
    "mx", "n_mx", "mx_diversi_secondlevel",
    "a", "aaaa",
    "dnssec_ds_presente",
    "caa",
    "spf_presente", "spf_record",
    "dmarc_presente", "dmarc_policy",
    "asn_a", "asn_org_a", "asn_country_a",
    "asn_mx", "asn_org_mx", "asn_country_mx",
    "asn_ns", "asn_org_ns", "asn_country_ns",
    "tls_issuer", "tls_version", "tls_giorni_scadenza",
    "tls_protocolli_deboli_attivi", "tls_cipher", "tls_cipher_bit",
    "tls_hsts_presente", "tls_grade",
    "http_status", "http_server", "http_via", "cdn_rilevato",
    "http_response_time_ms",
    "giurisdizione_holding",
    # OTTIMIZZAZIONE (seconda passata, settembre 2026): SPERIMENTALE, vuote a
    # meno che misurazioni.as_hegemony non sia esplicitamente True in
    # config.yaml (default False) — vedi src/as_hegemony.py.
    "as_hegemony_dipendenza_principale", "as_hegemony_quota",
    "errori",
]

# Colonne che dipendono SOLO dall'hostname (non dall'ente): sono quelle che
# vengono riusate quando più righe di input condividono lo stesso hostname.
CAMPI_PER_ENTE = {"hostname", "comune", "regione", "codice_ipa"}
CAMPI_PER_HOSTNAME = [f for f in FIELDS if f not in CAMPI_PER_ENTE]


# --------------------------------------------------------------------------
# DNS
# --------------------------------------------------------------------------

def make_resolver(timeout: float, nameservers=None) -> dns.resolver.Resolver:
    r = dns.resolver.Resolver()
    if nameservers:
        r.nameservers = nameservers
    r.timeout = timeout  # per singolo tentativo/nameserver
    # lifetime = budget totale su TUTTI i nameserver configurati: deve essere
    # più ampio di timeout, altrimenti se il primo nameserver non risponde
    # (adattatore VPN/virtuale morto, tipico su laptop con più interfacce)
    # il budget si esaurisce prima di poter fare failover sul secondo.
    r.lifetime = timeout * max(1, len(r.nameservers))
    return r


# OTTIMIZZAZIONE (settembre 2026, seconda passata): un dns.resolver.Resolver
# per thread invece che uno per hostname. Costruire un Resolver rilegge la
# configurazione DNS di sistema da disco (/etc/resolv.conf o equivalente
# Windows): innocuo su una chiamata, ma su un campione di migliaia di host
# con parallelismo (ThreadPoolExecutor riusa gli stessi thread per più
# task) erano migliaia di letture inutili. thread-local perché un singolo
# Resolver NON è garantito sicuro se usato da PIÙ thread contemporaneamente
# (qui invece ogni thread ha sempre e solo il proprio)."""
_thread_local = threading.local()


def resolver_del_thread(timeout: float, nameservers=None) -> dns.resolver.Resolver:
    r = getattr(_thread_local, "resolver", None)
    if r is None:
        r = make_resolver(timeout, nameservers=nameservers)
        _thread_local.resolver = r
    return r


def query(resolver, name, rdtype):
    """Ritorna (risposte, errore). risposte è lista di stringhe (vuota se
    NXDOMAIN/NoAnswer: dominio/record che semplicemente non esiste). errore è
    None se la query è andata a buon fine, altrimenti il nome della classe di
    eccezione (es. 'LifetimeTimeout', 'NoNameservers') così un fallimento è
    diagnosticabile guardando il CSV, senza dover rileggere il codice."""
    try:
        answer = resolver.resolve(name, rdtype, raise_on_no_answer=False)
        if answer.rrset is None:
            return [], None
        return [r.to_text() for r in answer], None
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return [], None
    except Exception as e:
        return [], type(e).__name__


def second_level(hostname: str, ns_or_mx_hosts):
    """Euristica semplice (senza Public Suffix List) per stimare quanti
    operatori distinti stanno dietro ai NS/MX."""
    out = set()
    for h in ns_or_mx_hosts:
        h = h.rstrip(".").lower()
        parts = h.split(".")
        if len(parts) >= 2:
            out.add(".".join(parts[-2:]))
        elif parts:
            out.add(parts[0])
    return out


def check_dnssec(resolver, hostname: str):
    """Presenza di un DS record: indicatore minimale di DNSSEC attivo (non
    valida la catena di fiducia completa). Ritorna (True/False/None, errore)."""
    try:
        name = dns.name.from_text(hostname)
        answer = resolver.resolve(name, "DS", raise_on_no_answer=False)
        return answer.rrset is not None, None
    except Exception as e:
        return None, type(e).__name__


def check_spf(txt_records):
    for t in txt_records or []:
        clean = t.strip('"').replace('" "', "")
        if clean.lower().startswith("v=spf1"):
            return True, clean
    return False, None


def check_dmarc(resolver, hostname: str):
    txt, _err = query(resolver, f"_dmarc.{hostname}", "TXT")
    if not txt:
        return False, None
    for t in txt:
        clean = t.strip('"').replace('" "', "")
        if clean.lower().startswith("v=dmarc1"):
            policy = None
            for part in clean.split(";"):
                part = part.strip()
                if part.lower().startswith("p="):
                    policy = part.split("=", 1)[1]
            return True, policy
    return False, None


# --------------------------------------------------------------------------
# ASN / RDAP
# --------------------------------------------------------------------------

def resolve_asn(ip: str):
    """RDAP lookup via ipwhois. Ritorna (asn, org, country, errore)."""
    if not ip:
        return None, None, None, None
    try:
        obj = IPWhois(ip)
        res = obj.lookup_rdap(depth=1, rate_limit_timeout=5)
        asn = res.get("asn")
        org = res.get("asn_description") or (res.get("network") or {}).get("name")
        country = res.get("asn_country_code")
        return asn, org, country, None
    except (IPDefinedError, ASNRegistryError, HTTPLookupError) as e:
        return None, None, None, type(e).__name__
    except Exception as e:
        return None, None, None, type(e).__name__


# NOTA STORICA (pre-settembre 2026): qui c'erano _prefisso_cache() e
# resolve_asn_cached(), una cache /24-/48 sopra resolve_asn() (RDAP puro).
# Restano utili come principio (evitare di richiedere due volte lo stesso
# blocco IP) ma NON bastavano da sole a evitare il rate limit RDAP su un
# campione nazionale: la cache riduce le richieste RIPETUTE, ma il primo
# lookup di ciascuno dei tanti blocchi IP distinti di un campione grande
# resta comunque un lookup RDAP separato. resolve_asn() resta qui sotto
# invariata (usata anche da 05_scrape_dark_stack.py, e come fallback dal
# nuovo resolver bulk), ma il chiamante principale (main(), più in basso)
# ora usa asn_bulk.BulkASNResolver, che raggruppa i lookup in poche query
# bulk verso Team Cymru invece di farne uno per blocco IP: stessa idea
# della vecchia cache, ma applicata PRIMA di interrogare la rete, non dopo.
# Vedi src/asn_bulk.py per i dettagli e il motivo del cambio.


# Euristica di giurisdizione della holding: NON è un dato giuridico
# verificato, è un pattern-match sul nome dell'operatore RDAP per capire
# rapidamente "chi controlla davvero l'hosting", che può differire dal
# Paese fisico dell'ASN (es. datacenter AWS a Milano = ASN italiano, ma
# la holding è statunitense e soggetta a US CLOUD Act). Va estesa quando si
# incontrano operatori non coperti: se non matcha nulla, si usa il Paese
# ASN come fallback esplicito, marcato come tale.
GIURISDIZIONE_HOLDING_RULES = [
    ("amazon", "USA (Amazon/AWS)"), ("aws", "USA (Amazon/AWS)"),
    ("microsoft", "USA (Microsoft/Azure)"), ("azure", "USA (Microsoft/Azure)"),
    ("google", "USA (Google)"),
    ("cloudflare", "USA (Cloudflare)"),
    ("oracle", "USA (Oracle Cloud)"),
    ("digitalocean", "USA (DigitalOcean)"),
    ("akamai", "USA (Akamai)"), ("linode", "USA (Akamai/Linode)"),
    ("meta platforms", "USA (Meta)"),
    ("ovh", "Francia (OVH)"),
    ("hetzner", "Germania (Hetzner)"),
    ("ionos", "Germania (IONOS)"),
    ("aruba", "Italia (Aruba)"),
    ("seeweb", "Italia (Seeweb)"),
    ("register.it", "Italia (Register.it)"),
    ("telecom italia", "Italia (TIM)"), ("tim s.p.a", "Italia (TIM)"),
    ("fastweb", "Italia (Fastweb)"),
    ("leaseweb", "Paesi Bassi (Leaseweb)"),
    ("hostinger", "Lituania (Hostinger)"),
]


def giurisdizione_holding(org: str, country_asn: str) -> str:
    if org:
        org_low = org.lower()
        for pattern, giurisdizione in GIURISDIZIONE_HOLDING_RULES:
            if pattern in org_low:
                return giurisdizione
    if country_asn:
        return f"(non riconosciuto; Paese ASN: {country_asn})"
    return ""


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------

def get_tls_info(hostname: str, timeout: float = 5.0):
    """Legge il certificato TLS su porta 443 SENZA validarlo (self-signed,
    scaduto o con hostname non corrispondente non devono impedire la lettura:
    l'emittente e la scadenza sono dati interessanti a prescindere).
    Ritorna (issuer, tls_version, giorni_alla_scadenza, errore)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((hostname, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                der = ssock.getpeercert(binary_form=True)
                tls_version = ssock.version()
        if not der:
            return "", "", None, "certificato_assente"
        cert = x509.load_der_x509_certificate(der)
        issuer_attrs = cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        if not issuer_attrs:
            issuer_attrs = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
        issuer = issuer_attrs[0].value if issuer_attrs else ""
        try:
            not_after = cert.not_valid_after_utc
        except AttributeError:  # cryptography < 42
            not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
        giorni = (not_after - datetime.now(timezone.utc)).days
        return issuer, tls_version or "", giorni, None
    except Exception as e:
        return "", "", None, type(e).__name__


# Protocolli considerati deboli/deprecati: se il server accetta ANCHE questi
# (oltre a versioni più recenti) è un dato rilevante indipendentemente da
# quale versione viene negoziata di default da un client moderno (get_tls_info
# sopra usa sempre la versione più alta disponibile, quindi da sola non lo
# vedrebbe mai). SSLv3/TLS1.0/TLS1.1 sono deprecati da RFC 8996 (marzo 2021).
PROTOCOLLI_DEBOLI = ["SSLv3", "TLSv1", "TLSv1.1"]
PROTOCOLLI_TUTTI = ["SSLv3", "TLSv1", "TLSv1.1", "TLSv1.2", "TLSv1.3"]

# Mappa nome protocollo -> costante ssl.TLSVersion (SSLv3 non è quasi mai
# disponibile nel build OpenSSL di sistema: lo includiamo comunque, fallisce
# silenziosamente con 'ValueError'/'AttributeError' che intercettiamo sotto).
_TLS_VERSION_MAP = {
    "SSLv3": getattr(ssl.TLSVersion, "SSLv3", None),
    "TLSv1": getattr(ssl.TLSVersion, "TLSv1", None),
    "TLSv1.1": getattr(ssl.TLSVersion, "TLSv1_1", None),
    "TLSv1.2": getattr(ssl.TLSVersion, "TLSv1_2", None),
    "TLSv1.3": getattr(ssl.TLSVersion, "TLSv1_3", None),
}


def _prova_singolo_protocollo(hostname: str, protocollo: str, timeout: float) -> bool:
    """True se il server ACCETTA una connessione forzata a questo esatto
    protocollo (minimum_version == maximum_version == protocollo). Serve a
    scoprire versioni deboli ancora attive che un client moderno normale
    non negozierebbe mai (e quindi get_tls_info() non le vedrebbe)."""
    versione = _TLS_VERSION_MAP.get(protocollo)
    if versione is None:
        return False  # non supportato dal build OpenSSL locale: non possiamo saperlo, non "non presente sul server"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        # Il filtro globale in testa al file (module="ssl") non basta: Python
        # attribuisce il DeprecationWarning al modulo CHIAMANTE (__main__,
        # cioè questo stesso script quando lanciato da riga di comando), non
        # al modulo 'ssl' — quindi qui serve un filtro locale esplicito,
        # altrimenti su un run grande la console si riempie di migliaia di
        # righe identiche ("ssl.TLSVersion.SSLv3 is deprecated" ecc.). È
        # normale che Python avvisi: stiamo chiedendo esplicitamente
        # protocolli deprecati, ma è proprio lo scopo di questa funzione
        # (vedere se il server li accetta ancora), non un errore.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = versione
            ctx.maximum_version = versione
    except (ValueError, OSError):
        return False  # OpenSSL locale compilato senza questo protocollo
    try:
        with socket.create_connection((hostname, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname):
                return True
    except Exception:
        return False


def scan_tls_protocols(hostname: str, timeout: float):
    """Prova una connessione per ciascun protocollo in PROTOCOLLI_TUTTI.
    Costa fino a 5 connessioni TCP+TLS extra per hostname (oltre a quella
    già fatta da get_tls_info): è la parte più lenta della fase 2, per
    questo è dietro il flag 'tls_avanzato' nel config.yaml. Ritorna
    (set_protocolli_supportati, errore)."""
    supportati = set()
    errore = None
    for protocollo in PROTOCOLLI_TUTTI:
        try:
            if _prova_singolo_protocollo(hostname, protocollo, timeout):
                supportati.add(protocollo)
        except Exception as e:
            errore = type(e).__name__
    return supportati, errore


def get_tls_cipher_info(hostname: str, timeout: float):
    """Cipher suite e dimensione della chiave di sessione negoziate di
    default (versione più alta disponibile, come un browser). Ritorna
    (nome_cipher, bit_chiave, errore)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((hostname, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cipher = ssock.cipher()  # (nome, versione_protocollo, bit_segreti) o None
        if not cipher:
            return "", None, "cipher_assente"
        nome, _versione, bit = cipher
        return nome or "", bit, None
    except Exception as e:
        return "", None, type(e).__name__


def calcola_tls_grade(protocolli_supportati, cipher_bit, hsts_presente) -> str:
    """Voto sintetico A/B/C/D, euristica semplice e dichiarata (NON è il
    metodo di SSL Labs, che pesa molte più variabili): pensata per dare un
    colpo d'occhio su un campione grande, non per audit di sicurezza puntuali.
    - D: protocolli deprecati (SSLv3/TLS1.0/1.1) ancora accettati dal server
    - C: nessun protocollo debole, ma cifratura della sessione < 128 bit
    - B: cifratura adeguata (>=128 bit) ma senza HSTS
    - A: cifratura adeguata, niente protocolli deboli, HSTS presente
    - '' (vuoto): dati insufficienti per esprimere un voto (es. TLS non raggiungibile)
    """
    if protocolli_supportati is None or cipher_bit is None:
        return ""
    if protocolli_supportati & set(PROTOCOLLI_DEBOLI):
        return "D"
    if cipher_bit < 128:
        return "C"
    if not hsts_presente:
        return "B"
    return "A"


# --------------------------------------------------------------------------
# HTTP / rilevamento CDN
# --------------------------------------------------------------------------

# Sovrascrivibile da config.yaml (chiave http_user_agent): alcuni siti dietro
# CDN/WAF (Cloudflare in particolare) bloccano con 403 uno User-Agent che si
# dichiara esplicitamente come bot. Il default resta onesto/trasparente
# (coerente con lo scopo del progetto, un osservatorio di interesse
# pubblico): chi preferisce un tasso di blocchi più basso a costo di uno
# User-Agent meno onesto può cambiarlo in config.yaml, la scelta è esplicita
# e documentata lì, non imposta di nascosto qui.
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; osservatorio-infra-pubblica/1.0)"

CDN_SIGNATURES = [
    ("Cloudflare", lambda h: "cf-ray" in h or "cloudflare" in h.get("server", "")),
    ("Akamai", lambda h: "akamai" in h.get("server", "") or "x-akamai-transformed" in h),
    ("Amazon CloudFront", lambda h: "x-amz-cf-id" in h or "cloudfront" in h.get("via", "")),
    ("Fastly", lambda h: "x-fastly-request-id" in h or "fastly" in h.get("via", "") or ("x-served-by" in h and "cache-" in h.get("x-served-by", ""))),
    ("Google Frontend", lambda h: h.get("server", "") in ("gws", "esf", "sffe", "gse")),
    ("Varnish (generico)", lambda h: "varnish" in h.get("via", "")),
    ("Sucuri", lambda h: "x-sucuri-id" in h),
    ("Imperva/Incapsula", lambda h: "x-iinfo" in h or "incapsula" in h.get("x-cdn", "")),
]


def detect_cdn(headers: dict):
    h = {str(k).lower(): str(v).lower() for k, v in headers.items()}
    return [nome for nome, test in CDN_SIGNATURES if test(h)]


def fetch_http_headers(hostname: str, timeout: float = 6.0, user_agent: str = None):
    """Richiesta HTTP leggera (solo per header, non scarica il corpo) per
    rilevare CDN/proxy e misurare il tempo di risposta. Prova prima HTTPS poi
    HTTP in chiaro. Ritorna (status, headers_dict, tempo_ms, errore).

    'user_agent' è configurabile (config.yaml, chiave http_user_agent):
    alcuni siti dietro CDN/WAF (Cloudflare in particolare) bloccano con 403
    lo User-Agent di default, che si dichiara esplicitamente come bot — vedi
    la nota in config.yaml su questo compromesso fra trasparenza e tasso di
    blocchi. Se non specificato, usa lo User-Agent di default qui sotto.

    Il tempo_ms è quello dell'intero round-trip fino alla ricezione degli
    header (resp.elapsed di requests), incluso il TLS handshake quando c'è:
    è quindi un'approssimazione del tempo "percepito" da un utente che apre
    la homepage, non un ping di rete puro. Su un unico campione va letto
    come ordine di grandezza, non come benchmark di performance: dipende
    anche da dove gira lo script rispetto al sito misurato. NOTA: se il
    sito risponde con una pagina di blocco WAF (vedi http_status 403/406/
    429/503), questo tempo misura quanto ci mette il WAF a rispondere, non
    quanto ci metterebbe la homepage reale."""
    ua = {"User-Agent": user_agent or DEFAULT_USER_AGENT}
    ultimo_errore = None
    for scheme in ("https", "http"):
        try:
            resp = requests.get(
                f"{scheme}://{hostname}/", timeout=timeout, verify=False,
                allow_redirects=True, headers=ua, stream=True,
            )
            tempo_ms = resp.elapsed.total_seconds() * 1000
            resp.close()  # non ci serve il corpo qui
            return resp.status_code, dict(resp.headers), tempo_ms, None
        except requests.RequestException as e:
            ultimo_errore = type(e).__name__
            continue
    return None, {}, None, ultimo_errore


# --------------------------------------------------------------------------
# Sonda per singolo hostname
# --------------------------------------------------------------------------
#
# OTTIMIZZAZIONE (settembre 2026): la vecchia probe_host() unica è stata
# divisa in tre fasi, orchestrate da main() più sotto:
#   1. probe_host_dns()       — solo DNS, NIENTE lookup ASN
#   2. (in main) asn_bulk.BulkASNResolver.resolve_many() UNA VOLTA su tutti
#      gli IP raccolti dalla fase 1 di un intero chunk di hostname
#   3. completa_asn() + probe_host_tls_http() — leggono/completano usando
#      la cache ASN già popolata (quasi sempre un cache hit) e sondano
#      TLS/HTTP, che non dipendono da DNS/ASN
#
# Il motivo di questa separazione è duplice:
#  - i lookup ASN via RDAP-per-IP sono il collo di bottiglia principale
#    (rate limit) su un campione grande: raggrupparli in poche query bulk
#    (asn_bulk.py) è l'ottimizzazione con il maggiore impatto;
#  - fase 1 e fase 3 sono fatte in PARALLELO su più hostname alla volta
#    (ThreadPoolExecutor in main(), grado di parallelismo = 'concorrenza'
#    in config.yaml): sono chiamate di rete indipendenti fra un hostname e
#    l'altro, non c'è motivo di farle una alla volta come nella versione
#    originale (che usava un semplice ciclo sequenziale + sleep(delay)).


def probe_host_dns(resolver, hostname: str, misure: dict) -> dict:
    """Fase 1: solo DNS (NS/MX/A/AAAA/DNSSEC/CAA/SPF/DMARC), più le query A
    per il primo MX e il primo NS trovati (servono per sapere QUALI IP
    passare alla risoluzione ASN in blocco: sono comunque query DNS, non
    RDAP, quindi restano qui). Nessun lookup ASN in questa funzione: è
    deliberatamente rimandato a dopo che main() ha raccolto gli IP di TUTTI
    gli hostname del chunk corrente (vedi asn_bulk.py sul perché).

    Pensata per essere chiamata da più thread in parallelo (un'istanza di
    dns.resolver.Resolver per chiamata: costruirla non costa una query di
    rete, la fa make_resolver() del chiamante — vedi main()).

    Ritorna (row_parziale, errori, ip_per_asn) dove ip_per_asn è un dict
    {'a': ip_o_None, 'mx': ip_o_None, 'ns': ip_o_None}: gli IP di cui
    servirà l'operatore/ASN più avanti."""
    errori = []
    row = {f: "" for f in CAMPI_PER_HOSTNAME}
    ip_per_asn = {"a": None, "mx": None, "ns": None}

    ns, mx_hosts, a = [], [], []

    if misure.get("dns_base", True):
        ns, err = query(resolver, hostname, "NS")
        if err:
            errori.append(f"ns_query_failed:{err}")
        row["ns"] = ";".join(sorted(n.rstrip(".") for n in ns))
        row["n_ns"] = len(ns)
        row["ns_diversi_secondlevel"] = len(second_level(hostname, ns))

        mx, err = query(resolver, hostname, "MX")
        if err:
            errori.append(f"mx_query_failed:{err}")
        mx_hosts = [m.split(" ", 1)[1].rstrip(".") if " " in m else m.rstrip(".") for m in mx]
        # MX nullo (RFC 7505, record letterale "0 ."): dichiarazione esplicita
        # "questo dominio non riceve posta", non un vero mail server. Dopo lo
        # strip del punto finale diventa stringa vuota: va scartato, altrimenti
        # verrebbe contato come "1 operatore mail" e si tenterebbe pure una
        # query A sulla root zone (hostname vuoto).
        mx_hosts = [h for h in mx_hosts if h]
        row["mx"] = ";".join(sorted(mx_hosts))
        row["n_mx"] = len(mx_hosts)
        row["mx_diversi_secondlevel"] = len(second_level(hostname, mx_hosts))

        a, err_a = query(resolver, hostname, "A")
        if err_a:
            errori.append(f"a_query_failed:{err_a}")
        aaaa, _err = query(resolver, hostname, "AAAA")
        row["a"] = ";".join(sorted(a))
        row["aaaa"] = ";".join(sorted(aaaa))

        dnssec, err = check_dnssec(resolver, hostname)
        row["dnssec_ds_presente"] = "" if dnssec is None else str(dnssec)
        if err:
            errori.append(f"dnssec_check_failed:{err}")

        caa, _err = query(resolver, hostname, "CAA")
        row["caa"] = ";".join(sorted(caa))

        txt, _err = query(resolver, hostname, "TXT")
        spf_present, spf_record = check_spf(txt)
        row["spf_presente"] = spf_present
        row["spf_record"] = spf_record or ""

        dmarc_present, dmarc_policy = check_dmarc(resolver, hostname)
        row["dmarc_presente"] = dmarc_present
        row["dmarc_policy"] = dmarc_policy or ""

    if misure.get("asn_hosting", True) and a:
        ip_per_asn["a"] = a[0]
    if misure.get("asn_hosting", True) and mx_hosts:
        mx_a, err = query(resolver, mx_hosts[0], "A")
        if err:
            errori.append(f"mx_a_query_failed:{err}")
        if mx_a:
            ip_per_asn["mx"] = mx_a[0]
    if misure.get("asn_hosting", True) and ns:
        ns_a, err = query(resolver, ns[0].rstrip("."), "A")
        if err:
            errori.append(f"ns_a_query_failed:{err}")
        if ns_a:
            ip_per_asn["ns"] = ns_a[0]

    return row, errori, ip_per_asn


def completa_asn(row: dict, ip_per_asn: dict, resolver_asn: "asn_bulk.BulkASNResolver",
                  hegemony_resolver=None) -> list:
    """Fase 2b (dopo che main() ha già chiamato UNA VOLTA
    resolver_asn.resolve_many() su tutti gli IP del chunk corrente): legge
    dalla cache già popolata i risultati per gli IP di QUESTO hostname —
    quasi sempre un cache hit istantaneo, non una nuova chiamata di rete
    (tranne i pochi IP non coperti dal bulk, vedi asn_bulk.py). Scrive
    dentro 'row' (mutato in place) e ritorna la lista di eventuali nuovi
    errori.

    'hegemony_resolver' (opzionale, as_hegemony.HegemonyResolver): SPERIMENTALE,
    None a meno che misurazioni.as_hegemony non sia True in config.yaml (vedi
    main()) — se passato, dopo aver risolto l'ASN del sito (asn_a) chiede
    anche la sua dipendenza di transito principale via IHR. Fallisce in
    modo contenuto per costruzione (vedi as_hegemony.colonne_extra_sicure):
    un suo errore finisce nella colonna 'errori' condivisa, come tutte le
    altre misurazioni, non in un'eccezione."""
    errori = []
    asn_org_a = None

    ip_a = ip_per_asn.get("a")
    if ip_a:
        asn, org, country, err = resolver_asn.resolve_one(ip_a)
        row["asn_a"], row["asn_org_a"], row["asn_country_a"] = asn or "", org or "", country or ""
        asn_org_a = org
        if err:
            errori.append(f"asn_a_lookup_failed:{err}")
        if hegemony_resolver is not None and asn:
            extra = as_hegemony.colonne_extra_sicure(asn, hegemony_resolver)
            row["as_hegemony_dipendenza_principale"] = extra["as_hegemony_dipendenza_principale"]
            row["as_hegemony_quota"] = extra["as_hegemony_quota"]
            if extra["as_hegemony_errore"]:
                errori.append(f"as_hegemony_failed:{extra['as_hegemony_errore']}")

    ip_mx = ip_per_asn.get("mx")
    if ip_mx:
        asn, org, country, err = resolver_asn.resolve_one(ip_mx)
        row["asn_mx"], row["asn_org_mx"], row["asn_country_mx"] = asn or "", org or "", country or ""
        if err:
            errori.append(f"asn_mx_lookup_failed:{err}")

    ip_ns = ip_per_asn.get("ns")
    if ip_ns:
        asn, org, country, err = resolver_asn.resolve_one(ip_ns)
        row["asn_ns"], row["asn_org_ns"], row["asn_country_ns"] = asn or "", org or "", country or ""
        if err:
            errori.append(f"asn_ns_lookup_failed:{err}")

    row["giurisdizione_holding"] = giurisdizione_holding(asn_org_a, row.get("asn_country_a", ""))
    return errori


def probe_host_tls_http(hostname: str, misure: dict, http_timeout: float, tls_timeout: float, user_agent: str) -> dict:
    """Fase 3: TLS + HTTP. Indipendente da DNS/ASN (usa solo 'hostname'),
    quindi eseguibile in un thread separato da probe_host_dns() — vedi
    main(). Ritorna (row_parziale, errori)."""
    errori = []
    row = {}
    headers = {}

    if misure.get("tls_base", True):
        issuer, tls_version, giorni, err = get_tls_info(hostname, timeout=tls_timeout)
        row["tls_issuer"], row["tls_version"] = issuer, tls_version
        row["tls_giorni_scadenza"] = giorni if giorni is not None else ""
        if err:
            errori.append(f"tls_failed:{err}")

    protocolli_supportati = None
    cipher_bit = None
    if misure.get("tls_avanzato", True):
        protocolli_supportati, err = scan_tls_protocols(hostname, timeout=tls_timeout)
        if err:
            errori.append(f"tls_scan_protocolli_failed:{err}")
        deboli_attivi = sorted(protocolli_supportati & set(PROTOCOLLI_DEBOLI)) if protocolli_supportati else []
        row["tls_protocolli_deboli_attivi"] = ";".join(deboli_attivi)

        cipher_nome, cipher_bit, err = get_tls_cipher_info(hostname, timeout=tls_timeout)
        row["tls_cipher"] = cipher_nome
        row["tls_cipher_bit"] = cipher_bit if cipher_bit is not None else ""
        if err:
            errori.append(f"tls_cipher_failed:{err}")

    if misure.get("http_headers_cdn", True):
        status, headers, tempo_ms, err = fetch_http_headers(hostname, timeout=http_timeout, user_agent=user_agent)
        row["http_status"] = status if status is not None else ""
        row["http_server"] = headers.get("Server", "")
        row["http_via"] = headers.get("Via", "")
        row["cdn_rilevato"] = ";".join(detect_cdn(headers))
        if err:
            errori.append(f"http_failed:{err}")

        if misure.get("tempi_risposta", True):
            row["http_response_time_ms"] = round(tempo_ms, 1) if tempo_ms is not None else ""

    if misure.get("tls_avanzato", True):
        hsts_presente = "strict-transport-security" in {k.lower() for k in headers}
        row["tls_hsts_presente"] = hsts_presente
        row["tls_grade"] = calcola_tls_grade(protocolli_supportati, cipher_bit, hsts_presente)

    return row, errori


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _elabora_chunk(righe_chunk, host_cache, misure, dns_timeout, http_timeout, tls_timeout,
                    nameservers, user_agent, resolver_asn, executor, delay, limitatore_blocco,
                    stato_avvisi=None, hegemony_resolver=None):
    """Elabora un chunk di righe di input (liste di namedtuple da
    df_in.itertuples()) e ritorna (righe_pronte_per_il_csv, n_hostname_nuovi,
    n_risposte_bloccate).

    Tre fasi (vedi anche i commenti sopra probe_host_dns()):
      1. DNS di tutti gli hostname NUOVI del chunk, IN PARALLELO
         ('executor', condiviso e persistente per l'intero run — vedi
         main() — non un ThreadPoolExecutor nuovo per ogni chunk: i thread
         restano gli stessi, così resolver_del_thread() li riusa davvero)
      2. UNA sola chiamata bulk (asn_bulk.BulkASNResolver.resolve_many) su
         tutti gli IP raccolti dalla fase 1 — non uno per hostname.
         Avvolta in try/except: un errore imprevisto qui non deve far
         crashare l'intero run, solo lasciare vuoti gli ASN di questo chunk
         (completa_asn() farà comunque un tentativo per singolo IP dopo).
      3. TLS/HTTP di tutti gli hostname nuovi, IN PARALLELO, con
         'limitatore_blocco' che tiene sotto controllo quante connessioni
         simultanee vanno verso lo STESSO blocco IP (non solo il totale
         globale) — vedi asn_bulk.LimitatorePerBlocco.

    host_cache è condiviso e aggiornato in place fra i chunk (hostname
    condivisi da più enti restano sondati una volta sola per l'intero run,
    non solo dentro il chunk corrente — stesso comportamento della versione
    sequenziale originale)."""
    hostname_nuovi = []
    visti = set()
    for r in righe_chunk:
        h = getattr(r, "hostname")
        if h not in host_cache and h not in visti:
            hostname_nuovi.append(h)
            visti.add(h)

    dns_result = {}

    def _sonda_dns(hostname):
        if delay:
            time.sleep(delay)
        resolver_thread = resolver_del_thread(dns_timeout, nameservers=nameservers)
        return probe_host_dns(resolver_thread, hostname, misure)

    if hostname_nuovi:
        futures = {executor.submit(_sonda_dns, h): h for h in hostname_nuovi}
        for fut in as_completed(futures):
            h = futures[fut]
            try:
                dns_result[h] = fut.result()
            except Exception as e:
                dns_result[h] = (
                    {f: "" for f in CAMPI_PER_HOSTNAME},
                    [f"dns_probe_crashed:{type(e).__name__}"],
                    {"a": None, "mx": None, "ns": None},
                )

    # Fase 2: risoluzione ASN in blocco — UNA chiamata per tutto il chunk,
    # non una per hostname. Questo è il cuore dell'ottimizzazione: vedi
    # src/asn_bulk.py. try/except: un errore qui (es. eccezione imprevista
    # non già intercettata da asn_bulk.py) non deve interrompere l'intero
    # run — completa_asn() più sotto ritenterà comunque IP per IP per gli
    # hostname di questo chunk, solo più lentamente.
    if misure.get("asn_hosting", True):
        tutti_ip = []
        for h in hostname_nuovi:
            _row, _err, ip_per_asn = dns_result[h]
            tutti_ip.extend(v for v in ip_per_asn.values() if v)
        try:
            resolver_asn.resolve_many(tutti_ip)
        except Exception as e:
            # BUGFIX (audit a freddo, settembre 2026): se questo errore si
            # ripete a ogni chunk (bug sistematico, non un blip isolato),
            # stamparlo ogni volta inonda il log su un run di migliaia di
            # host. Un avviso esplicito una sola volta, poi silenzio (ma il
            # comportamento degradato — fallback IP per IP via completa_asn
            # — resta identico a ogni chunk, solo senza ripetere il messaggio).
            if stato_avvisi is not None and not stato_avvisi.get("asn_bulk_crash_avvisato"):
                stato_avvisi["asn_bulk_crash_avvisato"] = True
                print(
                    f"ATTENZIONE: risoluzione ASN in blocco fallita ({type(e).__name__}: {e}) — "
                    "proseguo, completa_asn() ritenterà IP per IP per gli hostname di ogni chunk "
                    "(più lento, ma il run non si interrompe). Questo avviso compare una sola volta "
                    "anche se l'errore si ripete nei prossimi chunk.",
                    file=sys.stderr,
                )

    tlshttp_result = {}
    misure_tls_http_attive = any(
        misure.get(k, True) for k in ("tls_base", "tls_avanzato", "http_headers_cdn")
    )

    def _sonda_tls_http(hostname, ip_target):
        if delay:
            time.sleep(delay)
        with limitatore_blocco.limita(ip_target):
            return probe_host_tls_http(hostname, misure, http_timeout, tls_timeout, user_agent)

    if hostname_nuovi and misure_tls_http_attive:
        futures = {}
        for h in hostname_nuovi:
            _row, _err, ip_per_asn = dns_result[h]
            futures[executor.submit(_sonda_tls_http, h, ip_per_asn.get("a"))] = h
        for fut in as_completed(futures):
            h = futures[fut]
            try:
                tlshttp_result[h] = fut.result()
            except Exception as e:
                tlshttp_result[h] = ({}, [f"tlshttp_probe_crashed:{type(e).__name__}"])
    else:
        for h in hostname_nuovi:
            tlshttp_result[h] = ({}, [])

    CODICI_BLOCCO = {403, 429, 503}
    n_bloccate = 0
    for h in hostname_nuovi:
        row, errori_dns, ip_per_asn = dns_result[h]
        errori_asn = completa_asn(row, ip_per_asn, resolver_asn, hegemony_resolver=hegemony_resolver) if misure.get("asn_hosting", True) else []
        row_tlshttp, errori_tlshttp = tlshttp_result.get(h, ({}, []))
        row.update(row_tlshttp)
        row["errori"] = ";".join(errori_dns + errori_asn + errori_tlshttp)
        host_cache[h] = row
        if row.get("http_status") in CODICI_BLOCCO:
            n_bloccate += 1

    righe_out = []
    for r in righe_chunk:
        h = getattr(r, "hostname")
        row = dict(host_cache[h])
        row["hostname"] = h
        row["comune"] = getattr(r, "comune", "")
        row["regione"] = getattr(r, "regione", "")
        row["codice_ipa"] = getattr(r, "codice_ipa", "")
        righe_out.append(row)

    return righe_out, len(hostname_nuovi), n_bloccate


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paese", default="IT",
        help="Codice paese ISO a due lettere: determina i percorsi di default "
             "data/processed/<paese>/siti.csv e data/results/<paese>/risultati.csv (default: IT).",
    )
    ap.add_argument("--in", dest="infile", default=None, help="CSV di input (default: data/processed/<paese>/siti.csv)")
    ap.add_argument("--out", default=None, help="CSV di output (default: data/results/<paese>/<run-id>/risultati.csv)")
    ap.add_argument(
        "--run-id", default=None,
        help="Identificatore del run, usato come sottocartella di data/results/<paese>/ (default: data "
             "UTC odierna, AAAA-MM-GG — vedi config.py). Passare lo STESSO --run-id a 02/05/06/03/04 per "
             "farli lavorare sullo stesso run (run_pipeline.py lo fa già in automatico). Impostalo a mano "
             "solo per casi speciali: più run nello stesso giorno senza sovrascriverli, o backfill di una "
             "data passata.",
    )
    ap.add_argument("--config", default=None, help="Percorso di config.yaml (default: config.yaml nella project root)")
    ap.add_argument(
        "--delay", type=float, default=None,
        help="Pausa (s) applicata da ogni thread prima di ogni richiesta (default: da config.yaml, "
             "delay_secondi.probe_infra). Con più thread in parallelo (vedi --concorrenza) NON è più "
             "una pausa 'tra un host e il successivo' come nella versione sequenziale originale: "
             "regola quanto essere prudenti verso i siti sondati, la velocità complessiva dipende "
             "soprattutto da --concorrenza.",
    )
    ap.add_argument("--timeout", type=float, default=None, help="Timeout (s) per singolo tentativo DNS (default: da config.yaml, timeout_secondi.dns)")
    ap.add_argument("--http-timeout", type=float, default=None, help="Timeout (s) per la richiesta HTTP (default: da config.yaml, timeout_secondi.http)")
    ap.add_argument("--tls-timeout", type=float, default=None, help="Timeout (s) per la connessione TLS (default: da config.yaml, timeout_secondi.tls)")
    ap.add_argument(
        "--concorrenza", type=int, default=None,
        help="Quanti hostname sondare IN PARALLELO nella fase DNS e nella fase TLS/HTTP (default: da "
             "config.yaml, chiave 'concorrenza', altrimenti 15). L'OTTIMIZZAZIONE PRINCIPALE per la "
             "velocità: alzalo su un campione grande, abbassalo se la tua rete o i siti sondati "
             "faticano con troppe connessioni simultanee.",
    )
    ap.add_argument(
        "--concorrenza-per-provider", type=int, default=None,
        help="Quante connessioni TLS/HTTP simultanee al MASSIMO verso lo STESSO blocco IP (/24 IPv4, "
             "/48 IPv6), indipendentemente da --concorrenza (default: da config.yaml, chiave "
             "'concorrenza_per_provider', altrimenti 3). Protegge un singolo operatore che ospita "
             "molti enti del campione (càpita: un consorzio di hosting per piccoli comuni) dall'essere "
             "colpito da --concorrenza connessioni tutte insieme.",
    )
    ap.add_argument(
        "--chunk-size", type=int, default=200,
        help="Quanti hostname elaborare per 'chunk' prima di scrivere e fare il flush su disco "
             "(default: 200). Un chunk più piccolo scrive più spesso (meno lavoro perso se il run si "
             "interrompe, utile insieme a --resume); un chunk più grande fa lookup ASN bulk più "
             "efficienti (una query bulk copre più IP in un colpo solo). 200 è un compromesso "
             "ragionevole per un campione di migliaia di host.",
    )
    ap.add_argument(
        "--asn-metodo", choices=["cymru_bulk", "rdap"], default=None,
        help="Come risolvere ASN/operatore per IP (default: da config.yaml, chiave 'asn_metodo', "
             "altrimenti cymru_bulk). 'cymru_bulk': poche query bulk verso Team Cymru invece di un "
             "lookup RDAP per IP (vedi src/asn_bulk.py) — elimina la quasi totalità degli errori "
             "'..._lookup_failed:HTTPRateLimitError' su campioni grandi. 'rdap': comportamento "
             "originale pre-settembre 2026, un lookup RDAP per IP.",
    )
    ap.add_argument(
        "--skip-http", action="store_true",
        help="Scorciatoia equivalente a mettere a False in config.yaml: tls_base, tls_avanzato, "
             "http_headers_cdn, tempi_risposta (solo DNS/RDAP, molto più veloce). Ha la precedenza "
             "sul config.yaml.",
    )
    ap.add_argument(
        "--nameserver", action="append", default=None,
        help="IP di un nameserver da usare (ripetibile per più di uno). Default: quelli di sistema "
             "(vedi 'resolver_nameservers' nel manifest se vuoi controllare quali sono).",
    )
    ap.add_argument("--limit", type=int, default=None, help="Limita a N righe di input (per test rapidi)")
    ap.add_argument(
        "--resume", action="store_true",
        help="Se --out esiste già, salta i codice_ipa già presenti e continua ad appendere "
             "(utile su campioni grandi dopo un'interruzione).",
    )
    args = ap.parse_args()

    cfg = cfgmod.load_config(args.config)
    paese = args.paese.strip().upper()
    # BUGFIX (seconda passata, settembre 2026): con --resume e senza --run-id
    # esplicito, il default NON deve essere 'oggi' ma l'ultimo run noto dal
    # puntatore — vedi run_pipeline.py per il perché (in breve: un campione
    # nazionale può durare più di un giorno, --resume che scavalla la
    # mezzanotte UTC non deve aprire silenziosamente una cartella nuova).
    run_id = args.run_id or (cfgmod.leggi_puntatore_latest(paese) if args.resume else None) or cfgmod.run_id_default()
    infile = args.infile or str(dati_processed(paese) / "siti.csv")
    out_arg = args.out or str(dati_results(paese, run_id) / "risultati.csv")
    Path(out_arg).parent.mkdir(parents=True, exist_ok=True)

    delay = args.delay if args.delay is not None else cfgmod.delay_di(cfg, "probe_infra", 0.5)
    if delay < 0:
        print(f"ATTENZIONE: --delay {delay} negativo non è valido, uso 0 invece.", file=sys.stderr)
        delay = 0.0
    dns_timeout = args.timeout if args.timeout is not None else cfgmod.timeout_di(cfg, "dns", 5.0)
    http_timeout = args.http_timeout if args.http_timeout is not None else cfgmod.timeout_di(cfg, "http", 6.0)
    tls_timeout = args.tls_timeout if args.tls_timeout is not None else cfgmod.timeout_di(cfg, "tls", 5.0)
    concorrenza = args.concorrenza if args.concorrenza is not None else cfgmod.concorrenza_di(cfg, 15)
    # BUGFIX (audit a freddo, settembre 2026): cfgmod.concorrenza_di() blinda
    # già il valore letto da config.yaml (max(1, ...)), ma un valore passato
    # da riga di comando saltava quel controllo — '--concorrenza 0' o
    # negativo mandava in crash ThreadPoolExecutor con un ValueError poco
    # comprensibile ("max_workers must be greater than 0") invece di un
    # messaggio chiaro legato all'errore reale dell'utente.
    if concorrenza < 1:
        print(
            f"ATTENZIONE: --concorrenza {concorrenza} non valido, uso 1 (nessun parallelismo) invece.",
            file=sys.stderr,
        )
        concorrenza = 1
    concorrenza_per_provider = (
        args.concorrenza_per_provider if args.concorrenza_per_provider is not None
        else cfgmod.concorrenza_per_provider_di(cfg, 3)
    )
    if concorrenza_per_provider < 1:
        print(
            f"ATTENZIONE: --concorrenza-per-provider {concorrenza_per_provider} non valido, uso 1 invece.",
            file=sys.stderr,
        )
        concorrenza_per_provider = 1
    asn_metodo = args.asn_metodo if args.asn_metodo is not None else cfgmod.asn_metodo_di(cfg)
    chunk_size = max(1, args.chunk_size)

    # Flag per-misurazione: config.yaml è la sorgente, --skip-http (se passato)
    # forza a False in blocco le 4 voci HTTP/TLS indipendentemente dal file.
    misure = {
        "dns_base": cfgmod.flag(cfg, "dns_base"),
        "asn_hosting": cfgmod.flag(cfg, "asn_hosting"),
        "tls_base": cfgmod.flag(cfg, "tls_base") and not args.skip_http,
        "tls_avanzato": cfgmod.flag(cfg, "tls_avanzato") and not args.skip_http,
        "http_headers_cdn": cfgmod.flag(cfg, "http_headers_cdn") and not args.skip_http,
        "tempi_risposta": cfgmod.flag(cfg, "tempi_risposta") and not args.skip_http,
        # SPERIMENTALE (seconda passata): vedi src/as_hegemony.py. Default
        # False in config.py; non toccato da --skip-http (è un dato ASN, non
        # TLS/HTTP), ma comunque no-op se asn_hosting è disattivato (nessun
        # asn_a da interrogare — vedi hegemony_resolver più sotto).
        "as_hegemony": cfgmod.flag(cfg, "as_hegemony"),
    }

    if not Path(infile).exists():
        print(
            f"ERRORE: non trovo {infile}. Esegui prima 01_fetch_ipa_comuni.py, "
            "oppure passa --in percorso\\al\\tuo\\siti.csv",
            file=sys.stderr,
        )
        sys.exit(1)

    df_in = pd.read_csv(infile, dtype={"codice_ipa": str, "hostname": str})
    if "hostname" not in df_in.columns or "codice_ipa" not in df_in.columns:
        print("ERRORE: il CSV di input deve avere le colonne 'hostname' e 'codice_ipa' (vedi 01_fetch_ipa_comuni.py)", file=sys.stderr)
        sys.exit(1)

    # ROBUSTEZZA (auto-normalizzazione hostname, settembre 2026): vedi
    # config.normalizza_e_filtra_hostname() per il perché. Va fatto PRIMA di
    # --limit, cosicché un run di prova con --limit 20 sondi davvero 20
    # hostname utilizzabili invece di 20 righe che potrebbero rivelarsi
    # tutte da scartare.
    out_stem = str(out_arg).rsplit(".", 1)[0]
    df_in, n_hostname_normalizzati, n_hostname_scartati, scartate_path = (
        cfgmod.normalizza_e_filtra_hostname(df_in, infile, out_stem)
    )

    if args.limit:
        df_in = df_in.head(args.limit)

    already_done_codici = set()
    host_cache = {}  # hostname -> dict con i campi CAMPI_PER_HOSTNAME già calcolati
    user_agent = cfgmod.user_agent_di(cfg, DEFAULT_USER_AGENT)
    out_path = Path(out_arg)
    write_header = True
    if args.resume and out_path.exists():
        prev = pd.read_csv(out_path, dtype=str)
        already_done_codici = set(prev["codice_ipa"].astype(str))
        write_header = False
        for _, prow in prev.iterrows():
            h = str(prow["hostname"])
            if h not in host_cache:
                host_cache[h] = {f: prow.get(f, "") for f in CAMPI_PER_HOSTNAME}
        print(
            f"[resume] {len(already_done_codici)} enti già presenti in {out_path} "
            f"({len(host_cache)} hostname distinti già misurati), li salto.",
            file=sys.stderr,
        )
        df_in = df_in[~df_in["codice_ipa"].astype(str).isin(already_done_codici)]

    # resolve_asn (RDAP puro) resta come fallback ULTIMO, sia per il metodo
    # 'rdap' esplicito sia per gli IP che né il bulk Cymru né (se attivo)
    # RIPEstat riescono a risolvere. Vedi src/asn_bulk.py.
    #
    # OTTIMIZZAZIONE (seconda passata): livello intermedio RIPEstat, e un
    # rate limiter condiviso davanti a RIPEstat/RDAP (non davanti al bulk
    # Cymru, che ha già il suo protocollo/retry a parte in asn_bulk.py) —
    # senza, con 'asn_metodo: rdap' o con molti IP non risolti dal bulk,
    # fino a 'concorrenza' thread potrebbero chiamare questi servizi tutti
    # insieme, lo stesso pattern che genera gli 'HTTPRateLimitError'
    # documentati nei commenti di asn_bulk.py.
    rate_limiter_asn = rate_limiter.LimitatoreVelocita(
        richieste_al_secondo=cfgmod.asn_rate_limit_di(cfg, 5.0)
    )

    def _resolve_asn_ripestat_con_limite(ip):
        return asn_bulk.resolve_asn_ripestat(ip, timeout=dns_timeout + http_timeout, rate_limiter=rate_limiter_asn)

    def _resolve_asn_rdap_con_limite(ip):
        rate_limiter_asn.attendi()
        return resolve_asn(ip)

    resolver_asn = asn_bulk.BulkASNResolver(
        resolve_asn_fallback=_resolve_asn_rdap_con_limite, usa_bulk=(asn_metodo == "cymru_bulk"),
        resolve_asn_ripestat=(
            _resolve_asn_ripestat_con_limite if cfgmod.asn_ripestat_abilitato_di(cfg) else None
        ),
    )
    # SPERIMENTALE (seconda passata): None (default) se misurazioni.as_hegemony
    # è False in config.yaml — completa_asn() salta del tutto la chiamata a
    # IHR quando hegemony_resolver è None, quindi nessun costo/rischio in più
    # per chi non lo attiva. Riusa lo stesso rate limiter di RDAP/RIPEstat:
    # è un servizio esterno diverso, ma lo stesso principio di prudenza vale
    # (vedi rate_limiter.py).
    hegemony_resolver = (
        as_hegemony.HegemonyResolver(rate_limiter=rate_limiter_asn)
        if misure.get("as_hegemony") else None
    )
    limitatore_blocco = asn_bulk.LimitatorePerBlocco(max_per_blocco=concorrenza_per_provider)
    stato_avvisi = {"asn_bulk_crash_avvisato": False}

    manifest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "paese": paese,
        "run_id": run_id,
        "n_righe_input": len(df_in) + len(already_done_codici),
        "n_righe_da_processare_questo_run": len(df_in),
        "n_hostname_normalizzati": n_hostname_normalizzati,
        "n_hostname_scartati_invalidi": n_hostname_scartati,
        "scartate_hostname_invalido_path": scartate_path if n_hostname_scartati else None,
        "resume": bool(args.resume),
        "skip_http": bool(args.skip_http),
        "misurazioni": misure,
        "asn_metodo": asn_metodo,
        "concorrenza": concorrenza,
        "concorrenza_per_provider": concorrenza_per_provider,
        "chunk_size": chunk_size,
        "delay_seconds": delay,
        "timeout_seconds": dns_timeout,
        "http_timeout_seconds": http_timeout,
        "tls_timeout_seconds": tls_timeout,
        "http_user_agent": user_agent,
        "dnspython_version": dns.version.version,
        "python_version": platform.python_version(),
        "os": platform.platform(),
        "resolver_nameservers": make_resolver(dns_timeout, nameservers=args.nameserver).nameservers,
        "script": "02_probe_infra.py",
    }
    manifest_path = str(out_arg).rsplit(".", 1)[0] + "_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    total = len(df_in)
    n_written = 0
    n_nuovi_totale = 0
    n_bloccate_totale = 0
    avviso_bulk_dato = False
    SOGLIA_CANARY_MIN_CAMPIONI = 10  # non giudicare il bulk su troppo pochi IP tentati
    SOGLIA_CANARY_TASSO_MINIMO = 0.5
    if write_header and out_path.exists():
        cfgmod.backup_se_esiste(out_path)  # vedi config.py: stesso --run-id rilanciato senza --resume, non perdere il checkpoint precedente
    mode = "w" if write_header else "a"
    inizio = time.monotonic()
    righe_tutte = list(df_in.itertuples(index=False))

    with open(out_path, mode, newline="", encoding="utf-8") as fh, \
            ThreadPoolExecutor(max_workers=concorrenza) as executor:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()

        for inizio_chunk in range(0, total, chunk_size):
            righe_chunk = righe_tutte[inizio_chunk:inizio_chunk + chunk_size]
            righe_out, n_nuovi, n_bloccate = _elabora_chunk(
                righe_chunk, host_cache, misure, dns_timeout, http_timeout, tls_timeout,
                args.nameserver, user_agent, resolver_asn, executor, delay, limitatore_blocco,
                stato_avvisi, hegemony_resolver,
            )
            for row in righe_out:
                writer.writerow(row)
            fh.flush()
            n_written += len(righe_out)
            n_nuovi_totale += n_nuovi
            n_bloccate_totale += n_bloccate

            # Canary check (una volta sola, appena si hanno abbastanza dati):
            # se il bulk Cymru sta fallendo quasi sempre, meglio saperlo ora
            # (porta 43 bloccata dalla rete? servizio giù?) che scoprirlo a
            # fine run leggendo le statistiche finali.
            if (
                not avviso_bulk_dato and asn_metodo == "cymru_bulk"
                and misure.get("asn_hosting", True)
            ):
                tasso = resolver_asn.tasso_successo_bulk()
                totale_tentato = resolver_asn.stat_bulk_ok + resolver_asn.stat_fallback_rdap
                if tasso is not None and totale_tentato >= SOGLIA_CANARY_MIN_CAMPIONI:
                    avviso_bulk_dato = True
                    if tasso < SOGLIA_CANARY_TASSO_MINIMO:
                        print(
                            f"ATTENZIONE: il bulk Team Cymru ha risolto solo il {tasso*100:.0f}% degli "
                            f"IP tentati finora ({resolver_asn.stat_bulk_ok}/{totale_tentato}) — molto "
                            "sotto quanto ci si aspetterebbe. Possibili cause: la porta 43/TCP in uscita "
                            "è bloccata dalla tua rete (firewall aziendale/scolastico, alcune reti "
                            "pubbliche), o whois.cymru.com è temporaneamente irraggiungibile. Il run "
                            "prosegue comunque via fallback RDAP (più lento, stesso rischio di rate "
                            "limit della versione originale) — se vuoi evitarlo del tutto, interrompi "
                            "(Ctrl+C, --resume per riprendere) e prova con --asn-metodo rdap per "
                            "confermare, o verifica la connessione alla porta 43.",
                            file=sys.stderr,
                        )

            if n_bloccate > 0:
                tasso_blocco = 100 * n_bloccate_totale / n_written if n_written else 0
                nota_blocco = f", {n_bloccate} risposte 403/429/503 in questo chunk ({tasso_blocco:.0f}% sul totale finora)"
            else:
                nota_blocco = ""

            trascorso = time.monotonic() - inizio
            velocita = n_written / trascorso if trascorso > 0 else 0.0
            rimanenti = total - n_written
            eta_s = rimanenti / velocita if velocita > 0 else None
            eta_txt = f", ETA ~{eta_s/60:.0f} min" if eta_s is not None else ""
            print(
                f"[{n_written}/{total}] chunk completato ({n_nuovi} hostname nuovi sondati, "
                f"{velocita:.1f} host/s{eta_txt}{nota_blocco})",
                file=sys.stderr,
            )

    n_riusati = n_written - n_nuovi_totale
    print(
        f"\nFatto. {n_written} righe scritte in questo run ({n_riusati} riusate da hostname "
        f"già misurato in questo stesso run; totale nel file: {n_written + len(already_done_codici)}) "
        f"in {out_arg}. Manifesto: {manifest_path}",
        file=sys.stderr,
    )
    # OTTIMIZZAZIONE (seconda passata): aggiorna data/results/<paese>/latest_run.json
    # solo se lo script è arrivato fin qui (nessuna eccezione non gestita
    # prima): un run interrotto a metà non deve far puntare 'latest' a
    # un'esecuzione parziale.
    cfgmod.aggiorna_puntatore_latest(paese, run_id)
    if misure.get("asn_hosting", True):
        print(
            f"Risoluzione ASN ({asn_metodo}): {resolver_asn.stat_bulk_ok} IP risolti via bulk Team "
            f"Cymru, {resolver_asn.stat_ripestat_ok} via fallback RIPEstat, "
            f"{resolver_asn.stat_fallback_rdap} via fallback RDAP per-IP "
            f"({resolver_asn.stat_fallback_errori} falliti anche col fallback), "
            f"{len(resolver_asn.cache)} blocchi IP distinti in cache totale.",
            file=sys.stderr,
        )
    if n_bloccate_totale:
        print(
            f"Risposte HTTP 403/429/503 (possibili blocchi WAF/rate-limit): {n_bloccate_totale} su "
            f"{n_written} ({100*n_bloccate_totale/n_written:.1f}%). Se questa quota è alta o cresce nei "
            "run successivi, valuta di abbassare --concorrenza e/o --concorrenza-per-provider, o "
            "personalizzare http_user_agent in config.yaml (vedi README).",
            file=sys.stderr,
        )
    if not args.resume:
        print(
            "Suggerimento: su campioni grandi, se il run si interrompe puoi ripartire con --resume "
            "invece di rifare tutto da capo (ogni chunk da --chunk-size righe viene scritto e messo "
            "in flush su disco appena pronto). Con --skip-http salti TLS/HTTP e vai molto più veloce "
            "(solo DNS/ASN). Per disattivare singole misurazioni (es. solo tls_avanzato, che è la più "
            "lenta) modifica config.yaml nella project root invece di --skip-http. Alza --concorrenza "
            "per andare più veloce (ma vedi --concorrenza-per-provider per non essere troppo aggressivo "
            "verso un singolo operatore); --asn-metodo rdap torna al vecchio comportamento "
            "un-IP-alla-volta.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()