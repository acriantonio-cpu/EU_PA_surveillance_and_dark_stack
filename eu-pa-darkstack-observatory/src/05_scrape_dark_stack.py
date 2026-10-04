#!/usr/bin/env python3
# EN: Step 05 - 'dark stack': fetch each homepage and list third-party dependencies in its static
# HTML (CDN/fonts, analytics, captcha, chat, video, SDKs; rules in
# data/rules/dark_stack_rules.yaml), resolve their country and flag non-EEA ones; classify
# homepage cookies (technical vs profiling).
"""
05_scrape_dark_stack.py

Idea 1 (Dark Stack): per ogni hostname in data/processed/siti.csv scarica la
homepage e ne analizza l'HTML statico per elencare le dipendenze di terze
parti caricate dal browser: CDN/font, analytics/tracking, captcha, chat di
supporto, video embed, SDK generici (vedi data/rules/dark_stack_rules.yaml,
estendibile senza toccare il codice). Per ogni dominio terzo rilevato risolve
il Paese via ASN/RDAP e marca se è fuori dallo Spazio Economico Europeo (SEE).

Raccoglie anche i cookie impostati dalla risposta della homepage (sezione
cookie_tracker), classificati tecnico/profilazione per NOME via
data/rules/cookie_rules.yaml: stesso principio delle regole dark stack,
estendibile senza toccare il codice.

LIMITE IMPORTANTE (va tenuto a mente leggendo i risultati): questa è
un'analisi dell'HTML/risposta HTTP statici restituiti dal server, non una
cattura del traffico reale del browser. Risorse o cookie iniettati
dinamicamente via JavaScript dopo il caricamento, o impostati solo dopo che
l'utente ha dato consenso su un banner cookie (es. un chat widget caricato
da un altro script, un redirect lato client) non vengono visti. Per una
mappatura completa servirebbe un browser headless (Playwright/Selenium) che
esegue il JS e registra le richieste/i cookie effettivi nel tempo: fuori
dallo scope di questa versione. La classificazione tecnico/profilazione è
un'euristica sul nome del cookie (vedi cookie_rules.yaml), non una
valutazione giuridica GDPR/ePrivacy.

Le due sezioni (terze parti da HTML e cookie) sono attivabili/disattivabili
separatamente da config.yaml nella project root (dark_stack_terze_parti e
cookie_tracker): cookie_tracker riusa la stessa richiesta HTTP già fatta per
le terze parti, quindi richiede dark_stack_terze_parti=true per avere
effetto (se dark_stack_terze_parti=false l'intero passo viene saltato).

DETTAGLIO PER-RISORSA E VERIFICA MANUALE (quarta passata, aggiunta su
richiesta, settembre 2026): dark_stack.csv sopra AGGREGA le terze parti per
hostname (conteggi, liste unite da ';') — utile per statistiche, ma non
basta per verificare a mano un dominio sospetto ("ho trovato pagine
probabilmente compromesse, mi serve la URL esatta"). Per questo lo script
scrive ANCHE, gratis sulla stessa richiesta HTTP (nessun fetch aggiuntivo):
  - dark_stack_link_dettaglio.csv: una riga per ogni (hostname scansionato,
    dominio di terza parte trovato), con la URL ESATTA della pagina letta
    (dopo eventuali redirect) e la URL esatta della risorsa che referenzia
    quel dominio;
  - terze_parti_da_verificare.csv/.md: costruiti RILEGGENDO l'intero file
    di dettaglio (non solo le righe di questo run), un riepilogo per
    dominio di terza parte con la PRIMA pagina in cui è comparso e
    l'elenco di TUTTE le pagine in cui compare — esattamente nel formato
    "dominio.com, trovato nei link: sito1.it/..., sito2.it/..., ...",
    ordinato dal dominio più raro (il più interessante da controllare a
    mano) al più diffuso. Comparire in questo elenco NON significa che un
    dominio sia malevolo (vedi nota in testa al file .md): è un aiuto alla
    revisione manuale, non un verdetto automatico. Attivabile/disattivabile
    da config.yaml (terze_parti_dettaglio_link), default true.

Va eseguito dalla cartella principale del progetto, dopo 01_fetch_ipa_comuni.py.

Esempi:
    python src\\05_scrape_dark_stack.py --limit 20
    python src\\05_scrape_dark_stack.py --resume
"""

import argparse
import csv
import importlib.util
import json
import platform
import re
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import dns.resolver
import pandas as pd
import requests
import urllib3
import yaml
from bs4 import BeautifulSoup

import config as cfgmod
import rate_limiter

warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def dati_processed(paese: str) -> Path:
    return PROJECT_ROOT / "data" / "processed" / paese


def dati_results(paese: str, run_id: str) -> Path:
    """Vedi 02_probe_infra.py, stessa funzione: risultati ora sotto
    data/results/<paese>/<run_id>/, non più direttamente in
    data/results/<paese>/."""
    return PROJECT_ROOT / "data" / "results" / paese / run_id


DEFAULT_RULES = PROJECT_ROOT / "data" / "rules" / "dark_stack_rules.yaml"  # condivise fra tutti i paesi
DEFAULT_COOKIE_RULES = PROJECT_ROOT / "data" / "rules" / "cookie_rules.yaml"  # condivise fra tutti i paesi

# Riusa make_resolver()/resolve_asn() da 02_probe_infra.py invece di
# duplicarli: stesso identico comportamento (timeout/failover/gestione
# errori RDAP) già testato lì. Import via file path perché il nome del
# modulo inizia per cifra (non è un identificatore Python valido per
# 'import 02_probe_infra'). Non esegue main() di 02 (protetto da
# if __name__ == "__main__").
_spec = importlib.util.spec_from_file_location(
    "_probe_infra_shared", Path(__file__).resolve().parent / "02_probe_infra.py"
)
_probe_infra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_probe_infra)
make_resolver = _probe_infra.make_resolver
resolve_asn = _probe_infra.resolve_asn

# OTTIMIZZAZIONE (settembre 2026): stesso resolver ASN bulk (Team Cymru,
# fallback RDAP) usato da 02_probe_infra.py — vedi src/asn_bulk.py. Qui i
# domini terzi sono comunque risolti uno alla volta (non si conosce
# l'intera lista in anticipo come nel campione principale), ma il bulk resta
# più robusto del solo RDAP anche per un IP alla volta (resolve_one prova
# comunque il bulk prima del fallback RDAP).
#
# BUGFIX (audit a freddo, settembre 2026): l'istanza va creata DENTRO
# main(), non qui a livello di modulo — a questo punto config.yaml non è
# ancora stato caricato, quindi una creazione qui ignorerebbe SEMPRE il
# flag 'asn_metodo' impostato dall'utente (es. 'rdap' su una rete che
# blocca la porta 43): 02_probe_infra.py lo avrebbe rispettato, questo
# script no, in modo silenzioso — inconsistenza pericolosa fra i due
# script. Vedi l'istanziazione reale dentro main(), più sotto.
import asn_bulk as _asn_bulk

# Spazio Economico Europeo (UE27 + Islanda, Liechtenstein, Norvegia): usato
# per marcare le chiamate "extra-SEE" come da idea di progetto originale.
SEE_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "IS", "LI", "NO",
}

FIELDS = [
    "hostname", "comune", "regione", "codice_ipa",
    "http_status_scrape", "http_errore_scrape",
    "n_terze_parti", "categorie_rilevate", "domini_terzi",
    # OTTIMIZZAZIONE (seconda passata): lista parallela a domini_terzi, un
    # nome di organizzazione madre per ciascun dominio (stesso ordine,
    # stesso separatore ';') — vedi load_rules()/classify() sopra. Usata da
    # summarize_tracker_hhi() in 03_analyze.py.
    "organizzazioni_madri_terzi",
    "n_extra_see", "domini_extra_see", "n_paese_sconosciuto",
    "n_cookie_totali", "n_cookie_tecnici", "n_cookie_profilazione",
    "cookie_profilazione_nomi", "n_cookie_sconosciuti",
    "pagina_probabilmente_bloccata",
]
CAMPI_PER_ENTE = {"hostname", "comune", "regione", "codice_ipa"}
CAMPI_PER_HOSTNAME = [f for f in FIELDS if f not in CAMPI_PER_ENTE]

# Colonne del file di DETTAGLIO per-risorsa (nuovo, quarta passata,
# settembre 2026): dark_stack.csv sopra AGGREGA le terze parti per hostname
# (conteggi, liste semicolon-joined) — utile per statistiche, ma per
# verificare a mano un dominio sospetto serve la riga singola con l'URL
# ESATTA in cui è stato trovato, non un conteggio. Vedi scrivi_dettaglio_e_verifica()
# in fondo al file.
FIELDS_DETTAGLIO = [
    "hostname", "comune", "regione", "codice_ipa",
    "pagina_scansionata", "terza_parte_host", "url_risorsa_trovata",
    "categoria", "vendor", "organizzazione_madre", "timestamp_utc",
]

TAG_ATTR = [("script", "src"), ("link", "href"), ("iframe", "src"), ("img", "src")]
UA = {"User-Agent": "Mozilla/5.0 (compatible; osservatorio-dark-stack/1.0)"}

# Codici di stato tipici di un blocco WAF/anti-bot piuttosto che di un
# problema reale del sito. Non bastano da soli (un 404 vero è comunque un
# 404): serve anche l'indizio nel corpo della risposta, vedi sotto.
STATUS_TIPICI_DI_BLOCCO = {403, 406, 429, 503}

# Frammenti di testo tipici delle pagine di blocco dei WAF/anti-bot più
# comuni (Cloudflare, Akamai, generici). Elenco non esaustivo per
# costruzione: si estende quando se ne incontrano di nuovi, esattamente come
# le regole in dark_stack_rules.yaml.
INDIZI_PAGINA_BLOCCO = [
    "attention required", "cf-error", "cf-wrapper", "checking your browser",
    "just a moment", "access denied", "request blocked", "you have been blocked",
    "sorry, you have been blocked", "please verify you are a human",
    "ray id", "incident id", "bot detection", "automated request",
]


def pagina_probabilmente_bloccata(status, html: str) -> bool:
    """Euristica dichiarata, non una certezza: True se lo status HTTP è tra
    quelli tipici di un blocco WAF/anti-bot E il corpo della risposta è
    corto (una pagina di blocco è quasi sempre minuscola rispetto a una
    homepage vera) o contiene uno degli indizi testuali sopra. Serve a
    distinguere 'non abbiamo visto niente perché il sito è stato bloccato'
    da 'non abbiamo visto niente perché il sito è davvero senza terze parti/
    software riconoscibile' — le due cose sarebbero altrimenti indistinguibili
    guardando solo n_terze_parti=0 o n_software_rilevati=0."""
    try:
        status_num = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_num = None
    if status_num not in STATUS_TIPICI_DI_BLOCCO:
        return False
    corpo = (html or "").lower()
    if len(corpo) < 2000:
        return True
    return any(indizio in corpo for indizio in INDIZI_PAGINA_BLOCCO)


def load_rules(path: str):
    """OTTIMIZZAZIONE (seconda passata, settembre 2026): ogni regola ora
    porta anche 'organizzazione_madre' (opzionale nel YAML): la società che
    controlla davvero il vendor, quando diversa e nota con certezza (es.
    vendor 'Google Fonts' -> organizzazione_madre 'Google'). Se assente nel
    YAML, ricade sul vendor stesso — comportamento identico a prima per
    qualunque regola scritta prima di questa modifica, retrocompatibile per
    costruzione. Serve a summarize_tracker_hhi() in 03_analyze.py: la
    letteratura (Singh et al., 2026) calcola la concentrazione dei tracker
    per organizzazione madre, non per singolo dominio/vendor, perché lo
    stesso operatore spesso possiede più domini/prodotti distinti."""
    with open(path, encoding="utf-8") as f:
        rules = yaml.safe_load(f) or []
    return [
        (r["pattern"].lower(), r["categoria"], r["vendor"], r.get("organizzazione_madre") or r["vendor"])
        for r in rules
    ]


def load_cookie_rules(path: str):
    """Stesso formato di load_rules() ma per cookie_rules.yaml."""
    with open(path, encoding="utf-8") as f:
        rules = yaml.safe_load(f) or []
    return [(r["pattern"].lower(), r["categoria"], r["vendor"]) for r in rules]


def classify_cookie(nome_cookie: str, cookie_rules):
    """Ritorna (categoria, vendor). categoria è 'tecnico', 'profilazione' o
    'sconosciuto' (nessuna regola matcha: non contato in nessuna delle due
    statistiche, vedi commento in cookie_rules.yaml).

    Per i pattern molto corti (<=4 caratteri, es. 'fr', 'nid', 'ide') il
    match è sul NOME INTERO del cookie, non su sottostringa: altrimenti un
    pattern come 'fr' matcherebbe per caso dentro nomi di cookie del tutto
    estranei. Per i pattern più lunghi resta il match a sottostringa (stesso
    comportamento di classify() in dark_stack_rules.yaml), utile per varianti
    tipo '_ga_XXXXXXX' generate da Google Analytics 4."""
    nome = nome_cookie.lower()
    for pattern, categoria, vendor in cookie_rules:
        if len(pattern) <= 4:
            if nome == pattern:
                return categoria, vendor
        elif pattern in nome:
            return categoria, vendor
    return "sconosciuto", nome_cookie


def extract_cookies(resp):
    """Estrae i cookie dalla risposta HTTP usando resp.cookies (RequestsCookieJar,
    che già gestisce correttamente il caso di più header Set-Cookie distinti,
    a differenza di resp.headers.get('Set-Cookie') che li concatena in una
    stringa unica difficile da separare in modo affidabile). Ritorna una
    lista di nomi di cookie (bastano per la classificazione per nome)."""
    try:
        return [c.name for c in resp.cookies]
    except Exception:
        return []


def second_level(hostname: str) -> str:
    """Stessa euristica (senza Public Suffix List) usata in 02_probe_infra.py
    per stimare l'operatore: qui serve a capire se un dominio è 'lo stesso
    sito' o una terza parte. Limite noto: su domini tipo 'comune.milano.it'
    riconosce correttamente 'milano.it' come radice, ma una vera PSL
    (pubblicsuffix.org) sarebbe più precisa sui pochi casi limite."""
    h = hostname.rstrip(".").lower()
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def fetch_html(hostname: str, timeout: float, max_bytes: int = 3_000_000, user_agent: str = None):
    """Ritorna (status, html, cookie_names, pagina_finale, errore).
    cookie_names è la lista dei nomi dei cookie impostati dalla risposta
    (resp.cookies, popolato indipendentemente dal fatto che il corpo venga
    letto in streaming sotto). 'pagina_finale' è resp.url: l'URL EFFETTIVA
    dopo eventuali redirect (es. http://comune.it/ -> https://www.comune.it/
    home) — serve a chi deve verificare un link sospetto trovato in questa
    pagina: aprendo esattamente 'pagina_finale' vede la stessa pagina che ha
    prodotto la riga nel report, non un URL di partenza che potrebbe aver
    fatto redirect altrove nel frattempo. 'user_agent' sovrascrivibile da
    config.yaml (http_user_agent), vedi nota in 02_probe_infra.py."""
    headers = {"User-Agent": user_agent or UA["User-Agent"]}
    ultimo_errore = None
    for scheme in ("https", "http"):
        try:
            resp = requests.get(
                f"{scheme}://{hostname}/", timeout=timeout, verify=False,
                allow_redirects=True, headers=headers, stream=True,
            )
            raw = resp.raw.read(max_bytes, decode_content=True)
            encoding = resp.encoding or "utf-8"
            try:
                html = raw.decode(encoding, errors="replace")
            except (LookupError, TypeError):
                html = raw.decode("utf-8", errors="replace")
            cookie_names = extract_cookies(resp)
            pagina_finale = resp.url
            resp.close()
            return resp.status_code, html, cookie_names, pagina_finale, None
        except requests.RequestException as e:
            ultimo_errore = type(e).__name__
            continue
    return None, "", [], None, ultimo_errore


def extract_third_party_resources(html: str, own_hostname: str):
    """Ritorna una lista di (host, url_completo) per ogni risorsa esterna
    nell'HTML. Si tiene anche l'URL intero (non solo l'host) perché alcune
    regole di classificazione (es. reCAPTCHA) dipendono dal path, non solo
    dal dominio: 'www.gstatic.com/recaptcha/...' è tutt'altra cosa rispetto
    a un generico asset statico sullo stesso host."""
    own_root = second_level(own_hostname)
    risorse = []
    seen = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return risorse
    for tag_name, attr in TAG_ATTR:
        for tag in soup.find_all(tag_name):
            val = tag.get(attr)
            if not val or not re.match(r"^(https?:)?//", val):
                continue  # scarta risorse relative/locali: sono first-party per definizione
            if val.startswith("//"):
                val = "https:" + val
            host = urlsplit(val).netloc.split(":")[0].lower()
            if not host or second_level(host) == own_root:
                continue
            key = (host, val.lower())
            if key in seen:
                continue
            seen.add(key)
            risorse.append((host, val))
    return risorse


def classify(resource_url_or_domain: str, rules):
    d = resource_url_or_domain.lower()
    for pattern, categoria, vendor, org_madre in rules:
        if pattern in d:
            return categoria, vendor, org_madre
    # Nessuna regola: come prima, categoria generica e 'vendor' = il dominio
    # stesso; organizzazione_madre segue lo stesso fallback (un dominio non
    # classificato conta come la propria org, non finisce raggruppato con
    # nessun altro — evita di gonfiare artificialmente l'HHI dei tracker
    # mettendo insieme domini scorrelati sotto un'etichetta generica).
    return "altro_terzo_parte", resource_url_or_domain, resource_url_or_domain


def scrivi_terze_parti_da_verificare(dettaglio_arg: str):
    """Legge l'INTERO file di dettaglio (non solo le righe scritte in questo
    run — stesso motivo per cui 08_classifica_tipo_ente.py rilegge il file
    intero per _possibili_non_pubblici.csv: deve restare corretto anche
    lanciando lo script più volte con --resume su blocchi diversi) e
    produce, per ciascun dominio di terza parte, la PRIMA volta in cui è
    stato osservato (per timestamp, non per ordine di riga: con più
    invocazioni --resume in giorni diversi l'ordine nel file non è detto
    coincida con l'ordine cronologico) e l'elenco di TUTTE le pagine in cui
    compare — il formato richiesto per verificare a mano un dominio sospetto:
    'wix.com, trovato nei link: giochi24.it/..., comune.milano.it/..., ...'.

    Ordina per RARITÀ crescente (n_hostname_distinti): un dominio di terza
    parte visto su un solo sito, in mezzo a centinaia di enti, è un
    candidato più interessante da controllare a mano di uno (es. Google
    Fonts) visto su tutti — non è un giudizio di malevolenza, solo un ordine
    di lettura pensato per chi deve rivedere l'elenco a mano.

    Scrive DUE file separati accanto al file di dettaglio: un CSV
    (terze_parti_da_verificare.csv, per ulteriore filtraggio/analisi) e un
    Markdown (terze_parti_da_verificare.md, leggibile aprendo direttamente
    il file, nel formato richiesto)."""
    dettaglio_path = Path(dettaglio_arg)
    if not dettaglio_path.exists():
        return
    df = pd.read_csv(dettaglio_path, dtype=str)
    if not len(df):
        return
    cartella = dettaglio_path.parent
    csv_path = cartella / "terze_parti_da_verificare.csv"
    md_path = cartella / "terze_parti_da_verificare.md"

    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], errors="coerce", utc=True)
    df = df.sort_values("timestamp_utc", kind="stable")

    righe_csv = []
    blocchi_md = []
    for dominio, gruppo in df.groupby("terza_parte_host"):
        primo = gruppo.iloc[0]
        pagine = sorted(gruppo["pagina_scansionata"].dropna().unique())
        primo_ts = primo["timestamp_utc"]
        primo_ts_leggibile = primo_ts.strftime("%Y-%m-%d %H:%M UTC") if pd.notna(primo_ts) else "n/d"
        righe_csv.append({
            "terza_parte_host": dominio,
            "categoria": primo.get("categoria", ""),
            "vendor": primo.get("vendor", ""),
            "organizzazione_madre": primo.get("organizzazione_madre", ""),
            "n_hostname_distinti": len(pagine),
            "primo_hostname_scansionato": primo.get("hostname", ""),
            "primo_url_risorsa_trovata": primo.get("url_risorsa_trovata", ""),
            "primo_timestamp_utc": primo_ts.isoformat() if pd.notna(primo_ts) else "",
            "tutte_le_pagine": ";".join(pagine),
        })
        blocchi_md.append(
            f"## {dominio}\n\n"
            f"- Categoria: {primo.get('categoria', '')} — vendor: {primo.get('vendor', '')} "
            f"— organizzazione madre: {primo.get('organizzazione_madre', '')}\n"
            f"- Trovato per la prima volta il {primo_ts_leggibile} su "
            f"{primo.get('url_risorsa_trovata', dominio)}\n"
            f"- Presente in {len(pagine)} pagina/e:\n"
            + "".join(f"  - {p}\n" for p in pagine)
        )

    df_out = pd.DataFrame(righe_csv).sort_values("n_hostname_distinti", kind="stable")
    cfgmod.backup_se_esiste(csv_path)
    df_out.to_csv(csv_path, index=False, encoding="utf-8")

    ordine_md = df_out["terza_parte_host"].tolist()
    blocchi_per_dominio = dict(zip((r["terza_parte_host"] for r in righe_csv), blocchi_md))
    cfgmod.backup_se_esiste(md_path)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(
            "# Terze parti trovate — da verificare\n\n"
            "Elenco di ogni dominio di terza parte trovato nelle homepage analizzate, con la "
            "PRIMA pagina esatta in cui è comparso e l'elenco completo delle pagine in cui è "
            "presente. Ordinato dal dominio più raro (visto su meno pagine — di solito il più "
            "interessante da controllare a mano) al più diffuso. Aprire la pagina indicata e "
            "cercare il dominio nel sorgente per verificare/certificare la presenza.\n\n"
            "**Nota**: comparire in questo elenco non significa che un dominio sia malevolo — "
            "fornitori di font, mappe, video o analytics compaiono legittimamente su moltissimi "
            "siti. È un elenco pensato per aiutare la revisione manuale, non un verdetto "
            "automatico.\n\n---\n\n"
        )
        for dominio in ordine_md:
            f.write(blocchi_per_dominio[dominio] + "\n---\n\n")

    print(
        f"{len(df_out)} domini di terza parte distinti riepilogati in:\n  {csv_path}\n  {md_path}",
        file=sys.stderr,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paese", default="IT",
        help="Codice paese ISO a due lettere: determina i percorsi di default "
             "data/processed/<paese>/siti.csv e data/results/<paese>/dark_stack.csv (default: IT).",
    )
    ap.add_argument("--in", dest="infile", default=None, help="CSV di input (default: data/processed/<paese>/siti.csv)")
    ap.add_argument("--out", default=None, help="CSV di output (default: data/results/<paese>/<run-id>/dark_stack.csv)")
    ap.add_argument("--run-id", default=None, help="Vedi 02_probe_infra.py --help (default: data UTC odierna)")
    ap.add_argument("--config", default=None, help="Percorso di config.yaml (default: config.yaml nella project root)")
    ap.add_argument("--rules", default=str(DEFAULT_RULES), help=f"Regole terze parti YAML, condivise fra i paesi (default: {DEFAULT_RULES})")
    ap.add_argument("--cookie-rules", default=str(DEFAULT_COOKIE_RULES), help=f"Regole cookie YAML, condivise fra i paesi (default: {DEFAULT_COOKIE_RULES})")
    ap.add_argument("--delay", type=float, default=None, help="Pausa (s) tra un sito e il successivo (default: da config.yaml, delay_secondi.dark_stack)")
    ap.add_argument("--http-timeout", type=float, default=None, help="Timeout (s) per il download della homepage (default: da config.yaml, timeout_secondi.dark_stack_http)")
    ap.add_argument("--dns-timeout", type=float, default=None, help="Timeout (s) per risolvere i domini terzi (default: da config.yaml, timeout_secondi.dark_stack_dns)")
    ap.add_argument(
        "--asn-metodo", choices=["cymru_bulk", "rdap"], default=None,
        help="Come risolvere ASN/operatore per i domini terzi (default: da config.yaml, chiave "
             "'asn_metodo'). Vedi 02_probe_infra.py --help e src/asn_bulk.py.",
    )
    ap.add_argument("--nameserver", action="append", default=None, help="Vedi 02_probe_infra.py --help")
    ap.add_argument("--limit", type=int, default=None, help="Limita a N righe di input (per test rapidi)")
    ap.add_argument("--resume", action="store_true", help="Salta i codice_ipa già presenti in --out")
    args = ap.parse_args()

    cfg = cfgmod.load_config(args.config)
    misure = {
        "dark_stack_terze_parti": cfgmod.flag(cfg, "dark_stack_terze_parti"),
        "cookie_tracker": cfgmod.flag(cfg, "cookie_tracker"),
    }
    if not misure["dark_stack_terze_parti"]:
        print(
            "dark_stack_terze_parti=false in config.yaml: non c'è nulla da fare in questo script "
            "(cookie_tracker dipende dalla stessa richiesta HTTP). Esco senza scrivere output.",
            file=sys.stderr,
        )
        sys.exit(0)

    delay = args.delay if args.delay is not None else cfgmod.delay_di(cfg, "dark_stack", 0.5)
    http_timeout = args.http_timeout if args.http_timeout is not None else cfgmod.timeout_di(cfg, "dark_stack_http", 8.0)
    user_agent = cfgmod.user_agent_di(cfg, UA["User-Agent"])
    dns_timeout = args.dns_timeout if args.dns_timeout is not None else cfgmod.timeout_di(cfg, "dark_stack_dns", 5.0)

    paese = args.paese.strip().upper()
    # BUGFIX (seconda passata): vedi 02_probe_infra.py, stessa correzione.
    run_id = args.run_id or (cfgmod.leggi_puntatore_latest(paese) if args.resume else None) or cfgmod.run_id_default()
    infile = args.infile or str(dati_processed(paese) / "siti.csv")
    out_arg = args.out or str(dati_results(paese, run_id) / "dark_stack.csv")
    dettaglio_arg = str(out_arg).rsplit(".", 1)[0] + "_link_dettaglio.csv"

    Path(out_arg).parent.mkdir(parents=True, exist_ok=True)
    if not Path(infile).exists():
        print(f"ERRORE: non trovo {infile}. Esegui prima 01_fetch_ipa_comuni.py", file=sys.stderr)
        sys.exit(1)

    rules = load_rules(args.rules)
    cookie_rules = load_cookie_rules(args.cookie_rules) if misure["cookie_tracker"] else []
    df_in = pd.read_csv(infile, dtype={"codice_ipa": str, "hostname": str})

    # ROBUSTEZZA (auto-normalizzazione hostname, settembre 2026): vedi
    # config.normalizza_e_filtra_hostname() per il perché. Va fatto PRIMA di
    # --limit, cosicché un run di prova sondi davvero N hostname utilizzabili
    # invece di N righe che potrebbero rivelarsi tutte da scartare.
    out_stem = str(out_arg).rsplit(".", 1)[0]
    df_in, n_hostname_normalizzati, n_hostname_scartati, scartate_path = (
        cfgmod.normalizza_e_filtra_hostname(df_in, infile, out_stem)
    )

    if args.limit:
        df_in = df_in.head(args.limit)

    resolver = make_resolver(dns_timeout, nameservers=args.nameserver)

    # Vedi commento a livello di modulo sul perché questa istanza si crea
    # QUI (dopo aver caricato cfg) e non a import-time: deve rispettare
    # 'asn_metodo' come fa 02_probe_infra.py, non ignorarlo.
    asn_metodo = args.asn_metodo if args.asn_metodo is not None else cfgmod.asn_metodo_di(cfg)
    # OTTIMIZZAZIONE (seconda passata): stesso livello intermedio RIPEstat +
    # rate limiter condiviso usati in 02_probe_infra.py (vedi lì per il
    # perché) — questo script fa lookup ASN per i domini di TERZE PARTI, un
    # servizio esterno condiviso proprio come RDAP/RIPEstat, quindi merita
    # la stessa protezione.
    rate_limiter_asn = rate_limiter.LimitatoreVelocita(
        richieste_al_secondo=cfgmod.asn_rate_limit_di(cfg, 5.0)
    )

    def _resolve_asn_ripestat_con_limite(ip):
        return _asn_bulk.resolve_asn_ripestat(ip, timeout=dns_timeout + http_timeout, rate_limiter=rate_limiter_asn)

    def _resolve_asn_rdap_con_limite(ip):
        rate_limiter_asn.attendi()
        return resolve_asn(ip)

    asn_resolver_dark_stack = _asn_bulk.BulkASNResolver(
        resolve_asn_fallback=_resolve_asn_rdap_con_limite, usa_bulk=(asn_metodo == "cymru_bulk"),
        resolve_asn_ripestat=(
            _resolve_asn_ripestat_con_limite if cfgmod.asn_ripestat_abilitato_di(cfg) else None
        ),
    )

    # Cache GLOBALE per dominio terzo (non per sito): "fonts.googleapis.com"
    # comparirà identico su migliaia di siti diversi, non ha senso rifare la
    # query DNS+RDAP ogni volta. Enormemente più efficace della sola cache
    # per hostname-sito usata in 02_probe_infra.py.
    domain_country_cache = {}

    def paese_di(domain: str) -> str | None:
        """None = non ancora determinabile/sconosciuto (non vuol dire SEE)."""
        if domain in domain_country_cache:
            return domain_country_cache[domain]
        ip_list, _err = _probe_infra.query(resolver, domain, "A")
        country = None
        if ip_list:
            _asn, _org, country, _err2 = asn_resolver_dark_stack.resolve_one(ip_list[0])
        domain_country_cache[domain] = country
        return country

    host_cache = {}
    host_dettaglio_cache: dict[str, list[dict]] = {}
    already_done_codici = set()
    out_path = Path(out_arg)
    dettaglio_path = Path(dettaglio_arg)
    write_header = True
    if args.resume and out_path.exists():
        prev = pd.read_csv(out_path, dtype=str)
        already_done_codici = set(prev["codice_ipa"].astype(str))
        write_header = False
        for _, prow in prev.iterrows():
            h = str(prow["hostname"])
            if h not in host_cache:
                host_cache[h] = {f: prow.get(f, "") for f in CAMPI_PER_HOSTNAME}
        # Precarico anche il file di dettaglio del run precedente: senza
        # questo, un hostname condiviso da più enti che è già stato
        # scansionato in un'invocazione precedente non avrebbe righe di
        # dettaglio per il NUOVO ente che lo condivide in QUESTA invocazione
        # (i dati non andrebbero persi — restano nel file precedente — ma
        # mancherebbe il collegamento hostname->nuovo ente in questo run).
        if dettaglio_path.exists():
            prev_dett = pd.read_csv(dettaglio_path, dtype=str)
            colonne_dett = [c for c in FIELDS_DETTAGLIO if c not in CAMPI_PER_ENTE]
            for h, gruppo in prev_dett.groupby("hostname"):
                if h not in host_dettaglio_cache:
                    host_dettaglio_cache[h] = gruppo[colonne_dett].to_dict("records")
        print(f"[resume] {len(already_done_codici)} enti già presenti, li salto.", file=sys.stderr)
        df_in = df_in[~df_in["codice_ipa"].astype(str).isin(already_done_codici)]

    manifest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "paese": paese,
        "run_id": run_id,
        "n_righe_da_processare_questo_run": len(df_in),
        "n_hostname_normalizzati": n_hostname_normalizzati,
        "n_hostname_scartati_invalidi": n_hostname_scartati,
        "scartate_hostname_invalido_path": scartate_path,
        "resume": bool(args.resume),
        "misurazioni": misure,
        "rules_file": str(args.rules),
        "n_regole": len(rules),
        "cookie_rules_file": str(args.cookie_rules) if misure["cookie_tracker"] else None,
        "n_cookie_regole": len(cookie_rules),
        "http_user_agent": user_agent,
        "python_version": platform.python_version(),
        "os": platform.platform(),
        "script": "05_scrape_dark_stack.py",
        "limite_metodologico": (
            "Analisi statica dell'HTML/risposta HTTP restituiti dal server; non cattura risorse o "
            "cookie iniettati via JavaScript dopo il caricamento, né cookie impostati solo dopo il "
            "consenso su un banner (servirebbe un browser headless). La classificazione tecnico/"
            "profilazione dei cookie è un'euristica sul nome, non una valutazione giuridica GDPR."
        ),
    }
    manifest_path = str(out_arg).rsplit(".", 1)[0] + "_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    total = len(df_in)
    n_written = n_riusati = 0
    if write_header:
        # vedi config.py: stesso --run-id rilanciato senza --resume, non
        # perdere i checkpoint precedenti di entrambi i file.
        cfgmod.backup_se_esiste(out_path)
        cfgmod.backup_se_esiste(dettaglio_path)
    mode = "w" if write_header else "a"
    with open(out_path, mode, newline="", encoding="utf-8") as fh, \
         open(dettaglio_arg, mode, newline="", encoding="utf-8") as fh_dett:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer_dett = csv.DictWriter(fh_dett, fieldnames=FIELDS_DETTAGLIO)
        if write_header:
            writer.writeheader()
            writer_dett.writeheader()
        for i, r in enumerate(df_in.itertuples(index=False), start=1):
            hostname = getattr(r, "hostname")
            if hostname in host_cache:
                print(f"[{i}/{total}] {hostname} (già analizzato, riuso il risultato)", file=sys.stderr)
                row = dict(host_cache[hostname])
                n_riusati += 1
            else:
                print(f"[{i}/{total}] {hostname}", file=sys.stderr)
                status, html, cookie_names, pagina_finale, err = fetch_html(hostname, http_timeout, user_agent=user_agent)
                row = {f: "" for f in CAMPI_PER_HOSTNAME}
                row["http_status_scrape"] = status if status is not None else ""
                row["http_errore_scrape"] = err or ""
                row["pagina_probabilmente_bloccata"] = pagina_probabilmente_bloccata(status, html)
                if html:
                    risorse = extract_third_party_resources(html, hostname)
                    domini = sorted({h for h, _ in risorse})
                    # NUOVO (quarta passata): prima URL esatta (fra le risorse
                    # trovate su QUESTA pagina) per ciascun host di terza
                    # parte, in ordine di apparizione nell'HTML — è quella che
                    # finisce nel file di dettaglio, cosa diversa dalla "URL
                    # più informativa per la classificazione" che sceglie
                    # classif_per_host qui sotto (possono differire: la prima
                    # in ordine di tag non è detto sia quella con il path più
                    # specifico usato per riconoscere la categoria).
                    prima_url_per_host = {}
                    for h, url in risorse:
                        prima_url_per_host.setdefault(h, url)
                    # per ogni host, classificazione della PRIMA risorsa che matcha una
                    # regola specifica (non generica): se un host serve sia un asset
                    # generico che, es., reCAPTCHA su un path diverso, vince la categoria
                    # più informativa invece di quella trovata per prima in ordine di tag.
                    classif_per_host = {}
                    for h, url in risorse:
                        cat, vendor, org_madre = classify(url, rules)
                        attuale = classif_per_host.get(h)
                        if attuale is None or attuale[0] == "altro_terzo_parte":
                            classif_per_host[h] = (cat, vendor, org_madre)
                    classificati = [(h,) + classif_per_host.get(h, ("altro_terzo_parte", h, h)) for h in domini]
                    categorie = sorted({c for _, c, _, _ in classificati})
                    extra_see, sconosciuti = [], 0
                    for d, _cat, _vendor, _org_madre in classificati:
                        paese_dominio = paese_di(d)
                        if paese_dominio is None:
                            sconosciuti += 1
                        elif paese_dominio not in SEE_COUNTRIES:
                            extra_see.append(f"{d}({paese_dominio})")
                    row["n_terze_parti"] = len(domini)
                    row["categorie_rilevate"] = ";".join(categorie)
                    row["domini_terzi"] = ";".join(d for d, _, _, _ in classificati)
                    row["organizzazioni_madri_terzi"] = ";".join(om for _, _, _, om in classificati)
                    row["n_extra_see"] = len(extra_see)
                    row["domini_extra_see"] = ";".join(extra_see)
                    row["n_paese_sconosciuto"] = sconosciuti
                    # File di dettaglio (quarta passata, vedi FIELDS_DETTAGLIO
                    # a inizio file): una riga per ciascun host di terza parte
                    # trovato SU QUESTA pagina, con l'URL esatta dove è stato
                    # trovato — per la verifica manuale di link sospetti.
                    ora_utc = datetime.now(timezone.utc).isoformat()
                    host_dettaglio_cache[hostname] = [
                        {
                            "pagina_scansionata": pagina_finale or f"https://{hostname}/",
                            "terza_parte_host": h,
                            "url_risorsa_trovata": prima_url_per_host.get(h, ""),
                            "categoria": cat,
                            "vendor": vendor,
                            "organizzazione_madre": org_madre,
                            "timestamp_utc": ora_utc,
                        }
                        for h, cat, vendor, org_madre in classificati
                    ]
                else:
                    row["n_terze_parti"] = 0
                    host_dettaglio_cache[hostname] = []

                if misure["cookie_tracker"]:
                    tecnici = profilazione = sconosciuti_cookie = 0
                    nomi_profilazione = []
                    for nome in cookie_names:
                        cat, _vendor = classify_cookie(nome, cookie_rules)
                        if cat == "tecnico":
                            tecnici += 1
                        elif cat == "profilazione":
                            profilazione += 1
                            nomi_profilazione.append(nome)
                        else:
                            sconosciuti_cookie += 1
                    row["n_cookie_totali"] = len(cookie_names)
                    row["n_cookie_tecnici"] = tecnici
                    row["n_cookie_profilazione"] = profilazione
                    row["cookie_profilazione_nomi"] = ";".join(sorted(nomi_profilazione))
                    row["n_cookie_sconosciuti"] = sconosciuti_cookie

                host_cache[hostname] = dict(row)
                time.sleep(delay)
            row["hostname"] = hostname
            row["comune"] = getattr(r, "comune", "")
            row["regione"] = getattr(r, "regione", "")
            row["codice_ipa"] = getattr(r, "codice_ipa", "")
            writer.writerow(row)
            fh.flush()
            for riga_dett in host_dettaglio_cache.get(hostname, []):
                writer_dett.writerow({
                    "hostname": hostname, "comune": row["comune"], "regione": row["regione"],
                    "codice_ipa": row["codice_ipa"], **riga_dett,
                })
            fh_dett.flush()
            n_written += 1

    print(
        f"\nFatto. {n_written} righe scritte in questo run ({n_riusati} riusate da hostname già "
        f"analizzato; {len(domain_country_cache)} domini terzi distinti risolti in totale) in "
        f"{out_arg}. Manifesto: {manifest_path}",
        file=sys.stderr,
    )

    if cfgmod.flag(cfg, "terze_parti_dettaglio_link"):
        scrivi_terze_parti_da_verificare(dettaglio_arg)

    cfgmod.aggiorna_puntatore_latest(paese, run_id)


if __name__ == "__main__":
    main()
