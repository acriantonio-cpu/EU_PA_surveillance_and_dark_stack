#!/usr/bin/env python3
# EN: Step 06 - software/licence census: recognise CMS/framework/web server from HTML markup and
# HTTP headers (data/rules/cms_signatures.yaml), with version when exposed and licence (open
# source or proprietary).
"""
06_fingerprint_cms.py

Idea 3 (censimento software/licenze): per ogni hostname in
data/processed/siti.csv scarica la homepage e ne riconosce il software in
uso (CMS, framework, server web) da tracce esterne — markup HTML tipico
(percorsi come /wp-content/, meta generator) e header HTTP (Server,
X-Powered-By) — confrontandole con le firme in
data/rules/cms_signatures.yaml (estendibile senza toccare il codice). Per
ogni software riconosciuto riporta anche la licenza (se open source, quale)
e, quando esposta dal sito stesso, la versione.

LIMITE METODOLOGICO IMPORTANTE (va tenuto a mente leggendo i risultati):
questo è un fingerprint da TRACCE ESTERNE, non un'ispezione del codice
sorgente installato. Misura "quale software risulta in uso e se quel
software, IN GENERALE, è open source con quale licenza": NON stabilisce se
l'ente rispetta gli obblighi della licenza (attribuzione, ridistribuzione
delle modifiche per licenze copyleft, ecc.) — un audit di conformità legale
richiede accesso al codice e non è automatizzabile da fuori. Leggi questi
dati come "censimento tecnologico del campione", non come verifica di
conformità. Molti siti personalizzano o rimuovono deliberatamente le tracce
di fingerprint per motivi di sicurezza: un mancato riconoscimento NON
significa "nessun software installato", significa "nessuna firma nota
trovata" (vedi n_software_rilevati=0 nel CSV).

Riusa fetch_html()/second_level() da 05_scrape_dark_stack.py (stesso schema
di riuso già usato lì per 02_probe_infra.py) invece di duplicarli.

Va eseguito dalla cartella principale del progetto, dopo 01_fetch_ipa_comuni.py.
Passo facoltativo della pipeline, attivabile/disattivabile da config.yaml
(chiave misurazioni.cms_fingerprint).

Esempi:
    python src\\06_fingerprint_cms.py --limit 20
    python src\\06_fingerprint_cms.py --resume
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

import pandas as pd
import requests
import urllib3
import yaml

import config as cfgmod

warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def dati_processed(paese: str) -> Path:
    return PROJECT_ROOT / "data" / "processed" / paese


def dati_results(paese: str, run_id: str) -> Path:
    """Vedi 02_probe_infra.py, stessa funzione."""
    return PROJECT_ROOT / "data" / "results" / paese / run_id


DEFAULT_RULES = PROJECT_ROOT / "data" / "rules" / "cms_signatures.yaml"  # condivise fra tutti i paesi

# Riusa fetch_html() da 05_scrape_dark_stack.py invece di duplicarlo: stesso
# comportamento (timeout/retry https->http/decodifica) già testato lì.
_spec = importlib.util.spec_from_file_location(
    "_dark_stack_shared", Path(__file__).resolve().parent / "05_scrape_dark_stack.py"
)
_dark_stack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dark_stack)

UA = {"User-Agent": "Mozilla/5.0 (compatible; osservatorio-software-pa/1.0)"}

FIELDS = [
    "hostname", "comune", "regione", "codice_ipa",
    "http_status_fingerprint", "http_errore_fingerprint",
    "n_software_rilevati", "software_rilevato", "categorie_software",
    "versioni_rilevate", "licenze_rilevate", "tutti_open_source",
    "pagina_probabilmente_bloccata",
]
CAMPI_PER_ENTE = {"hostname", "comune", "regione", "codice_ipa"}
CAMPI_PER_HOSTNAME = [f for f in FIELDS if f not in CAMPI_PER_ENTE]


def load_signatures(path: str):
    with open(path, encoding="utf-8") as f:
        regole = yaml.safe_load(f) or []
    out = []
    for r in regole:
        out.append({
            "pattern": r["pattern"].lower(),
            "fonte": r["fonte"],
            "software": r["software"],
            "categoria": r["categoria"],
            "licenza": r["licenza"],
            "open_source": r.get("open_source"),
            "version_regex": r.get("version_regex"),
        })
    return out


def fetch_html_and_headers(hostname: str, timeout: float, max_bytes: int = 2_000_000, user_agent: str = None):
    """Come fetch_html() di 05_scrape_dark_stack.py ma tiene anche gli
    header di risposta (servono per le firme fonte: header_server /
    header_powered_by). Richiesta separata e leggera (non condivisa con 05
    per tenere gli script indipendenti l'uno dall'altro: puoi eseguire 06
    senza aver mai eseguito 05, e viceversa). 'user_agent' sovrascrivibile
    da config.yaml (http_user_agent), vedi nota in 02_probe_infra.py."""
    headers_req = {"User-Agent": user_agent or UA["User-Agent"]}
    ultimo_errore = None
    for scheme in ("https", "http"):
        try:
            resp = requests.get(
                f"{scheme}://{hostname}/", timeout=timeout, verify=False,
                allow_redirects=True, headers=headers_req, stream=True,
            )
            raw = resp.raw.read(max_bytes, decode_content=True)
            encoding = resp.encoding or "utf-8"
            try:
                html = raw.decode(encoding, errors="replace")
            except (LookupError, TypeError):
                html = raw.decode("utf-8", errors="replace")
            headers = dict(resp.headers)
            resp.close()
            return resp.status_code, headers, html, None
        except requests.RequestException as e:
            ultimo_errore = type(e).__name__
            continue
    return None, {}, "", ultimo_errore


def fingerprint(html: str, headers: dict, regole):
    """Ritorna una lista di dict (uno per software distinto riconosciuto):
    {'software', 'categoria', 'licenza', 'open_source', 'versione'}.
    Se più regole matchano lo stesso 'software' (es. due pattern diversi per
    WordPress), viene contato una sola volta, tenendo la prima versione
    trovata se una qualsiasi delle regole la estrae."""
    # Normalizza gli apici singoli in doppi PRIMA del match: alcuni CMS
    # generano <meta name='generator' content='...'> con apici singoli
    # invece dei doppi standard. I pattern in cms_signatures.yaml sono
    # scritti con doppi apici: senza questa normalizzazione mancherebbero
    # il match sulle varianti con apici singoli.
    html_low = html.lower().replace("'", '"')
    server_low = str(headers.get("Server", "")).lower()
    powered_low = str(headers.get("X-Powered-By", "")).lower()
    fonti = {"html": html_low, "header_server": server_low, "header_powered_by": powered_low}
    # Stessa normalizzazione apici applicata al testo usato per version_regex,
    # ma con il case originale preservato (i numeri di versione non sono
    # case-sensitive comunque, ma il resto del markup potrebbe servire a
    # debug futuri se si allarga la regex).
    fonti_originali = {
        "html": html.replace("'", '"'),
        "header_server": str(headers.get("Server", "")),
        "header_powered_by": str(headers.get("X-Powered-By", "")),
    }

    trovati = {}  # software -> dict
    for regola in regole:
        testo = fonti.get(regola["fonte"], "")
        if not testo or regola["pattern"] not in testo:
            continue
        nome = regola["software"]
        versione = ""
        if regola["version_regex"]:
            m = re.search(regola["version_regex"], fonti_originali.get(regola["fonte"], ""), re.IGNORECASE)
            if m:
                versione = m.group(1)
        if nome not in trovati:
            trovati[nome] = {
                "software": nome,
                "categoria": regola["categoria"],
                "licenza": regola["licenza"],
                "open_source": regola["open_source"],
                "versione": versione,
            }
        elif versione and not trovati[nome]["versione"]:
            trovati[nome]["versione"] = versione
    return list(trovati.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paese", default="IT",
        help="Codice paese ISO a due lettere: determina i percorsi di default "
             "data/processed/<paese>/siti.csv e data/results/<paese>/cms_fingerprint.csv (default: IT).",
    )
    ap.add_argument("--in", dest="infile", default=None, help="CSV di input (default: data/processed/<paese>/siti.csv)")
    ap.add_argument("--out", default=None, help="CSV di output (default: data/results/<paese>/<run-id>/cms_fingerprint.csv)")
    ap.add_argument("--run-id", default=None, help="Vedi 02_probe_infra.py --help (default: data UTC odierna)")
    ap.add_argument("--config", default=None, help="Percorso di config.yaml (default: config.yaml nella project root)")
    ap.add_argument("--rules", default=str(DEFAULT_RULES), help=f"Regole YAML, condivise fra i paesi (default: {DEFAULT_RULES})")
    ap.add_argument("--delay", type=float, default=None, help="Pausa (s) tra un sito e il successivo (default: da config.yaml, delay_secondi.cms_fingerprint)")
    ap.add_argument("--http-timeout", type=float, default=None, help="Timeout (s) per il download della homepage (default: da config.yaml, timeout_secondi.cms_fingerprint_http)")
    ap.add_argument("--limit", type=int, default=None, help="Limita a N righe di input (per test rapidi)")
    ap.add_argument("--resume", action="store_true", help="Salta i codice_ipa già presenti in --out")
    args = ap.parse_args()

    cfg = cfgmod.load_config(args.config)
    if not cfgmod.flag(cfg, "cms_fingerprint"):
        print(
            "cms_fingerprint=false in config.yaml: esco senza scrivere output. "
            "Metti il flag a true per attivare questo passo.",
            file=sys.stderr,
        )
        sys.exit(0)

    delay = args.delay if args.delay is not None else cfgmod.delay_di(cfg, "cms_fingerprint", 0.5)
    http_timeout = args.http_timeout if args.http_timeout is not None else cfgmod.timeout_di(cfg, "cms_fingerprint_http", 8.0)
    user_agent = cfgmod.user_agent_di(cfg, UA["User-Agent"])

    paese = args.paese.strip().upper()
    # BUGFIX (seconda passata): vedi 02_probe_infra.py, stessa correzione.
    run_id = args.run_id or (cfgmod.leggi_puntatore_latest(paese) if args.resume else None) or cfgmod.run_id_default()
    infile = args.infile or str(dati_processed(paese) / "siti.csv")
    out_arg = args.out or str(dati_results(paese, run_id) / "cms_fingerprint.csv")
    Path(out_arg).parent.mkdir(parents=True, exist_ok=True)

    if not Path(infile).exists():
        print(f"ERRORE: non trovo {infile}. Esegui prima 01_fetch_ipa_comuni.py", file=sys.stderr)
        sys.exit(1)

    regole = load_signatures(args.rules)
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

    host_cache = {}
    already_done_codici = set()
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
        "rules_file": str(args.rules),
        "n_regole": len(regole),
        "http_user_agent": user_agent,
        "python_version": platform.python_version(),
        "os": platform.platform(),
        "script": "06_fingerprint_cms.py",
        "limite_metodologico": (
            "Fingerprint da tracce esterne (HTML/header HTTP), non ispezione del codice "
            "installato. Misura 'software in uso e licenza generale', non conformità legale "
            "agli obblighi della licenza (attribuzione, ridistribuzione modifiche, ecc.)."
        ),
    }
    manifest_path = str(out_arg).rsplit(".", 1)[0] + "_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    total = len(df_in)
    n_written = n_riusati = 0
    if write_header and out_path.exists():
        cfgmod.backup_se_esiste(out_path)  # vedi config.py: non perdere il checkpoint di un --run-id rilanciato senza --resume
    mode = "w" if write_header else "a"
    with open(out_path, mode, newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        for i, r in enumerate(df_in.itertuples(index=False), start=1):
            hostname = getattr(r, "hostname")
            if hostname in host_cache:
                print(f"[{i}/{total}] {hostname} (già analizzato, riuso il risultato)", file=sys.stderr)
                row = dict(host_cache[hostname])
                n_riusati += 1
            else:
                print(f"[{i}/{total}] {hostname}", file=sys.stderr)
                status, headers, html, err = fetch_html_and_headers(hostname, http_timeout, user_agent=user_agent)
                row = {f: "" for f in CAMPI_PER_HOSTNAME}
                row["http_status_fingerprint"] = status if status is not None else ""
                row["http_errore_fingerprint"] = err or ""
                row["pagina_probabilmente_bloccata"] = _dark_stack.pagina_probabilmente_bloccata(status, html)
                if html or headers:
                    trovati = fingerprint(html, headers, regole)
                    row["n_software_rilevati"] = len(trovati)
                    row["software_rilevato"] = ";".join(t["software"] for t in trovati)
                    row["categorie_software"] = ";".join(sorted({t["categoria"] for t in trovati}))
                    row["versioni_rilevate"] = ";".join(f"{t['software']}:{t['versione']}" for t in trovati if t["versione"])
                    row["licenze_rilevate"] = ";".join(sorted({t["licenza"] for t in trovati}))
                    # None (dipende) NON conta come "tutti open source": solo True esplicito
                    row["tutti_open_source"] = bool(trovati) and all(t["open_source"] is True for t in trovati)
                else:
                    row["n_software_rilevati"] = 0
                host_cache[hostname] = dict(row)
                time.sleep(delay)
            row["hostname"] = hostname
            row["comune"] = getattr(r, "comune", "")
            row["regione"] = getattr(r, "regione", "")
            row["codice_ipa"] = getattr(r, "codice_ipa", "")
            writer.writerow(row)
            fh.flush()
            n_written += 1

    print(
        f"\nFatto. {n_written} righe scritte in questo run ({n_riusati} riusate da hostname già "
        f"analizzato) in {out_arg}. Manifesto: {manifest_path}",
        file=sys.stderr,
    )
    cfgmod.aggiorna_puntatore_latest(paese, run_id)


if __name__ == "__main__":
    main()
