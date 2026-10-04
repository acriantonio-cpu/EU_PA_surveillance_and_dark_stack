#!/usr/bin/env python3
"""
08_classifica_tipo_ente.py

Idea 4 (classificazione del tipo di ente dal testo della homepage): per ogni
hostname in data/processed/<paese>/siti.csv scarica la homepage e ne
classifica il TIPO DI ENTE (comune, provincia, regione, scuola, università,
sanità, polizia, vigili del fuoco, camera di commercio, ministero,
giustizia) confrontando titolo/meta description/corpo pagina con le parole
chiave in data/rules/tipo_ente_rules.yaml — in TUTTE le 24 lingue ufficiali
UE, non solo quella del Paese misurato: un ente bilingue (Alto Adige,
Finlandia...) può autodichiararsi in più di una lingua nella stessa pagina,
e classificare "alla cieca" su tutte le lingue evita di dover indovinare
prima la lingua del sito.

Perché è UTILE avere questo dato, oltre al 'sector' che 01_fetch_ipa_comuni.py
eredita da IndicePA: 'sector' esiste solo per l'Italia (viene dal
Codice_Categoria ufficiale IPA) — per qualunque altro Paese la colonna è
VUOTA, perché lì siti.csv è preparato a mano senza una tassonomia standard
(vedi data/processed/AT/siti.csv). Questo script dà una categorizzazione
UNIFORME, automatica e comparabile fra tutti i Paesi, non solo l'Italia:
è quello che rende possibile un confronto "quante scuole/comuni/ospedali per
Paese" nel report (vedi 09_confronto_paesi.py). Per l'Italia, dove 'sector'
esiste già, il confronto fra le due fonti (vedi 'accordo_con_sector' nel
report di 03_analyze.py) è anche un controllo di qualità sul classificatore
stesso.

LIMITE METODOLOGICO: è un classificatore a PAROLE CHIAVE su testo statico
(stesso limite di 05/06: JavaScript non eseguito), non un modello linguistico
— un ente che non si autodichiara nella homepage (sito minimale, homepage
solo immagini, testo generato via JS) resta 'non_classificato'. Le 463
parole chiave coprono i casi più comuni ma non sono esaustive in nessuna
delle 24 lingue: vedi data/rules/tipo_ente_rules.yaml per estenderle.

MISURA AGGIUNTIVA (bonus di questa stessa raccolta dati): a differenza di
02_probe_infra.py (che misura il tempo di risposta SOLO fino agli header,
senza scaricare il corpo — vedi commento su fetch_http_headers() in quel
file) e di 05/06 (che scaricano il corpo ma non ne misurano il tempo), qui
il corpo pagina serve comunque per la classificazione: cronometrarne il
download COMPLETO (fino a --max-bytes) è quindi un dato in più che viene
"gratis" dalla stessa richiesta di rete, non un giro extra dedicato. Risponde
a una domanda diversa da quella di 02 ("quanto ci mette il server a
rispondere") — qui è "quanto ci mette un utente/browser a scaricare
l'intera homepage", un proxy di UX/accessibilità digitale più che di salute
del server, interessante da confrontare fra tipo di ente e fra Paese (un
ente con una homepage da 3 MB non ottimizzata pesa di più su chi ha una
connessione lenta).

SEGNALE AGGIUNTIVO — possibile ente NON PUBBLICO (errore nella lista di
partenza): calcolato sulla STESSA pagina già scaricata per la
classificazione (nessuna richiesta di rete in più), cerca forme societarie
private ("S.r.l.", "GmbH", "Ltd"...) e terminologia da e-commerce/vetrina
commerciale, in data/rules/tipo_ente_rules.yaml (sezioni
forme_societarie/commerciale). Motivato da un caso REALE trovato in
data/processed/IT/siti.csv: "Giochi24 S.r.l." è presente con
sector=camera_commercio (le camere di commercio elencano anche aziende
private iscritte, non solo uffici pubblici) — un esempio concreto di come un
hostname privato possa finire nella lista di partenza senza che sia un
errore di questo script. Il segnale (colonna 'probabile_non_pubblico') è un
CANDIDATO da rivedere a mano, non un'esclusione automatica: una società
partecipata pubblica (es. "Farmacie Comunali S.p.A.") ha legittimamente una
forma societaria privata pur essendo a controllo pubblico — vedi il
commento nel file di regole. Le righe con questo segnale vengono scritte
ANCHE in un file separato (stesso nome di --out con suffisso
"_possibili_non_pubblici"), pensato per essere rivisto/escluso senza
toccare né il file principale né la lista di partenza: risponde a "farlo
insieme o separatamente?" con "insieme per il fetch (stessa pagina, nessun
costo di rete aggiuntivo), separato nell'output (così chi vuole ripulire la
lista non deve rileggere tutte le altre colonne)".

Passo facoltativo della pipeline, attivabile/disattivabile da config.yaml
(chiave misurazioni.tipo_ente_classificazione).

Va eseguito dalla cartella principale del progetto, dopo 01_fetch_ipa_comuni.py.

Esempi:
    python src\\08_classifica_tipo_ente.py --limit 20
    python src\\08_classifica_tipo_ente.py --resume
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
from bs4 import BeautifulSoup

import config as cfgmod

warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES = PROJECT_ROOT / "data" / "rules" / "tipo_ente_rules.yaml"  # condivise fra tutti i paesi
UA = {"User-Agent": "Mozilla/5.0 (compatible; osservatorio-tipo-ente/1.0)"}

# Riusa pagina_probabilmente_bloccata() da 05_scrape_dark_stack.py invece di
# duplicarla (stesso schema di riuso già usato da 06_fingerprint_cms.py):
# è una funzione pura (status + html -> bool), niente a che fare con la
# cronometrazione che serve solo qui.
_spec = importlib.util.spec_from_file_location(
    "_dark_stack_shared", Path(__file__).resolve().parent / "05_scrape_dark_stack.py"
)
_dark_stack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dark_stack)


def dati_processed(paese: str) -> Path:
    return PROJECT_ROOT / "data" / "processed" / paese


def dati_results(paese: str, run_id: str) -> Path:
    """Vedi 02_probe_infra.py, stessa funzione."""
    return PROJECT_ROOT / "data" / "results" / paese / run_id


FIELDS = [
    "hostname", "comune", "regione", "codice_ipa",
    "http_status_classificazione", "http_errore_classificazione",
    "tipo_ente_rilevato", "punteggio_top", "categorie_rilevate_punteggi",
    "pagina_probabilmente_bloccata",
    # Segnale di possibile ente non pubblico (vedi docstring): CANDIDATO da
    # rivedere a mano, non un'esclusione automatica.
    "probabile_non_pubblico", "segnali_non_pubblico_rilevati",
    # Misura aggiuntiva (vedi docstring): sottoprodotto della stessa
    # richiesta di rete usata per la classificazione, non un giro extra.
    "tempo_download_pagina_ms", "peso_pagina_kb", "pagina_troncata",
]
CAMPI_PER_ENTE = {"hostname", "comune", "regione", "codice_ipa"}
CAMPI_PER_HOSTNAME = [f for f in FIELDS if f not in CAMPI_PER_ENTE]


def load_rules(path):
    with open(path, encoding="utf-8") as f:
        dati = yaml.safe_load(f) or {}
    categorie = dati.get("categorie", {})
    # Precompila: categoria -> lista di parole chiave (tutte le lingue
    # appiattite insieme, minuscole). La lingua di provenienza non serve al
    # punteggio (vedi docstring: si classifica "alla cieca" su tutte le
    # lingue insieme), solo il conteggio dei match per categoria.
    out = {}
    for categoria, lingue in categorie.items():
        parole = []
        for lista in (lingue or {}).values():
            parole.extend(p.lower() for p in (lista or []))
        out[categoria] = sorted(set(parole), key=len, reverse=True)  # più lunghe prima: match più specifico prima di uno più generico che la contiene
    return out


def load_non_pubblico_rules(path):
    with open(path, encoding="utf-8") as f:
        dati = yaml.safe_load(f) or {}
    forme = sorted({p.lower() for lista in (dati.get("forme_societarie") or {}).values() for p in (lista or [])}, key=len, reverse=True)
    commerciale = sorted({p.lower() for lista in (dati.get("commerciale") or {}).values() for p in (lista or [])}, key=len, reverse=True)
    # Le forme societarie sono spesso sigle corte (ab, sa, kg...): un match a
    # sottostringa libera come per le categorie darebbe troppi falsi positivi
    # (es. "sa" dentro "Comune di Salerno"). Qui serve un confine di parola
    # vero, quindi vengono precompilate come regex con \b, punteggiatura
    # interna della sigla (es. "s.r.l.") ESCLUSA dal bordo di parola stesso
    # (re.escape gestisce i punti letterali).
    forme_regex = [re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE) for p in forme]
    return forme_regex, commerciale


def rileva_non_pubblico(titolo: str, meta_desc: str, corpo: str, forme_regex, parole_commerciali):
    """Ritorna (bool, lista_segnali_testuali). Guarda titolo/meta/corpo
    insieme: una forma societaria compare più spesso in fondo al <title> o
    nel footer (dentro il corpo), i termini commerciali soprattutto nel
    corpo (call-to-action). Non pesato/graduato come classifica(): qui basta
    UN segnale per marcare il candidato da rivedere, la lista dei segnali
    trovati serve a chi rivede a capire perché è stato marcato."""
    testo = f"{titolo} {meta_desc} {corpo}"
    segnali = []
    for rx in forme_regex:
        m = rx.search(testo)
        if m:
            segnali.append(f"forma_societaria:{m.group(0).lower()}")
    testo_low = testo.lower()
    for parola in parole_commerciali:
        if parola in testo_low:
            segnali.append(f"commerciale:{parola}")
    return bool(segnali), segnali


def fetch_pagina(hostname: str, timeout: float, max_bytes: int = 2_000_000, user_agent: str = None):
    """Come fetch_html() di 05_scrape_dark_stack.py, ma cronometra il
    download del corpo COMPLETO (fino a max_bytes), non solo la richiesta:
    è il dato che serve per la misura aggiuntiva (vedi docstring del
    modulo). Non riusa fetch_html() perché quella funzione non ritorna
    l'informazione di tempo/troncamento che serve qui (stessa scelta già
    fatta da 06_fingerprint_cms.py con fetch_html_and_headers, per lo stesso
    motivo: serve un dato in più che la funzione condivisa non espone).

    Ritorna (status, html, tempo_ms, peso_bytes, troncata, errore)."""
    headers = {"User-Agent": user_agent or UA["User-Agent"]}
    ultimo_errore = None
    for scheme in ("https", "http"):
        inizio = time.perf_counter()
        try:
            resp = requests.get(
                f"{scheme}://{hostname}/", timeout=timeout, verify=False,
                allow_redirects=True, headers=headers, stream=True,
            )
            raw = resp.raw.read(max_bytes + 1, decode_content=True)  # +1: per sapere se abbiamo troncato
            tempo_ms = round((time.perf_counter() - inizio) * 1000, 1)
            troncata = len(raw) > max_bytes
            raw = raw[:max_bytes]
            encoding = resp.encoding or "utf-8"
            try:
                html = raw.decode(encoding, errors="replace")
            except (LookupError, TypeError):
                html = raw.decode("utf-8", errors="replace")
            status = resp.status_code
            resp.close()
            return status, html, tempo_ms, len(raw), troncata, None
        except requests.RequestException as e:
            ultimo_errore = type(e).__name__
            continue
    return None, "", None, None, False, ultimo_errore


def estrai_testo_pesato(html: str):
    """Ritorna (titolo, meta_description, testo_corpo_troncato). Titolo e
    meta description pesano di più nel punteggio (vedi classifica()) perché
    un ente pubblico tipicamente si autodichiara lì; il corpo è troncato a
    20000 caratteri di testo VISIBILE (non markup): oltre non aggiunge
    segnale utile per questo scopo e rallenta il parsing su pagine pesanti."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return "", "", ""
    titolo = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta_desc = ""
    tag_meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    if tag_meta and tag_meta.get("content"):
        meta_desc = tag_meta["content"]
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    corpo = soup.get_text(" ", strip=True)[:20_000]
    return titolo, meta_desc, corpo


def classifica(titolo: str, meta_desc: str, corpo: str, regole: dict):
    """Punteggio per categoria: ogni match in titolo/meta_desc vale 3, ogni
    match nel corpo vale 1 — un ente che si autodichiara nell'intestazione
    ("Comune di Roma" nel <title>) è un segnale più forte di una parola
    chiave che compare una volta in mezzo al testo (potrebbe essere un link
    a un altro ente, una menzione di cronaca, ecc.). Ritorna (categoria_top,
    punteggio_top, dict_punteggi_non_zero) — categoria_top è None se tutti i
    punteggi sono zero."""
    testo_forte = f"{titolo} {meta_desc}".lower()
    testo_debole = corpo.lower()
    punteggi = {}
    for categoria, parole in regole.items():
        p = 0
        for parola in parole:
            if parola in testo_forte:
                p += 3
            elif parola in testo_debole:
                p += 1
        if p > 0:
            punteggi[categoria] = p
    if not punteggi:
        return None, 0, {}
    top = max(punteggi, key=punteggi.get)
    return top, punteggi[top], punteggi


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paese", default="IT",
        help="Codice paese ISO a due lettere: determina i percorsi di default "
             "data/processed/<paese>/siti.csv e data/results/<paese>/tipo_ente.csv (default: IT).",
    )
    ap.add_argument("--in", dest="infile", default=None, help="CSV di input (default: data/processed/<paese>/siti.csv)")
    ap.add_argument("--out", default=None, help="CSV di output (default: data/results/<paese>/<run-id>/tipo_ente.csv)")
    ap.add_argument("--run-id", default=None, help="Vedi 02_probe_infra.py --help (default: data UTC odierna)")
    ap.add_argument("--config", default=None, help="Percorso di config.yaml (default: config.yaml nella project root)")
    ap.add_argument("--rules", default=str(DEFAULT_RULES), help=f"Regole YAML, condivise fra i paesi (default: {DEFAULT_RULES})")
    ap.add_argument("--delay", type=float, default=None, help="Pausa (s) tra un sito e il successivo (default: da config.yaml, delay_secondi.tipo_ente_classificazione)")
    ap.add_argument("--http-timeout", type=float, default=None, help="Timeout (s) per il download della homepage (default: da config.yaml, timeout_secondi.tipo_ente_classificazione_http)")
    ap.add_argument("--max-bytes", type=int, default=2_000_000, help="Byte massimi scaricati per homepage (default: 2 MB, come 06_fingerprint_cms.py)")
    ap.add_argument("--limit", type=int, default=None, help="Limita a N righe di input (per test rapidi)")
    ap.add_argument("--resume", action="store_true", help="Salta i codice_ipa già presenti in --out")
    args = ap.parse_args()

    cfg = cfgmod.load_config(args.config)
    if not cfgmod.flag(cfg, "tipo_ente_classificazione"):
        print(
            "tipo_ente_classificazione=false in config.yaml: esco senza scrivere output. "
            "Metti il flag a true per attivare questo passo.",
            file=sys.stderr,
        )
        sys.exit(0)

    delay = args.delay if args.delay is not None else cfgmod.delay_di(cfg, "tipo_ente_classificazione", 0.5)
    http_timeout = args.http_timeout if args.http_timeout is not None else cfgmod.timeout_di(cfg, "tipo_ente_classificazione_http", 8.0)
    user_agent = cfgmod.user_agent_di(cfg, UA["User-Agent"])

    paese = args.paese.strip().upper()
    run_id = args.run_id or (cfgmod.leggi_puntatore_latest(paese) if args.resume else None) or cfgmod.run_id_default()
    infile = args.infile or str(dati_processed(paese) / "siti.csv")
    out_arg = args.out or str(dati_results(paese, run_id) / "tipo_ente.csv")
    Path(out_arg).parent.mkdir(parents=True, exist_ok=True)

    if not Path(infile).exists():
        print(f"ERRORE: non trovo {infile}. Esegui prima 01_fetch_ipa_comuni.py", file=sys.stderr)
        sys.exit(1)

    regole = load_rules(args.rules)
    forme_regex, parole_commerciali = load_non_pubblico_rules(args.rules)
    n_parole_totali = sum(len(v) for v in regole.values())
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
        "n_categorie": len(regole),
        "n_parole_chiave_totali": n_parole_totali,
        "max_bytes": args.max_bytes,
        "http_user_agent": user_agent,
        "python_version": platform.python_version(),
        "os": platform.platform(),
        "script": "08_classifica_tipo_ente.py",
        "limite_metodologico": (
            "Classificatore a parole chiave su testo statico (HTML pre-JavaScript), non un "
            "modello linguistico. Un ente che non si autodichiara nella homepage resta "
            "'non_classificato'. Le 24 lingue UE coperte hanno profondità di copertura diversa, "
            "vedi data/rules/tipo_ente_rules.yaml."
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
                status, html, tempo_ms, peso_bytes, troncata, err = fetch_pagina(
                    hostname, http_timeout, max_bytes=args.max_bytes, user_agent=user_agent,
                )
                row = {f: "" for f in CAMPI_PER_HOSTNAME}
                row["http_status_classificazione"] = status if status is not None else ""
                row["http_errore_classificazione"] = err or ""
                row["pagina_probabilmente_bloccata"] = _dark_stack.pagina_probabilmente_bloccata(status, html)
                row["tempo_download_pagina_ms"] = tempo_ms if tempo_ms is not None else ""
                row["peso_pagina_kb"] = round(peso_bytes / 1024, 1) if peso_bytes is not None else ""
                row["pagina_troncata"] = troncata
                if html:
                    titolo, meta_desc, corpo = estrai_testo_pesato(html)
                    top, punteggio_top, punteggi = classifica(titolo, meta_desc, corpo, regole)
                    row["tipo_ente_rilevato"] = top or "non_classificato"
                    row["punteggio_top"] = punteggio_top
                    row["categorie_rilevate_punteggi"] = ";".join(f"{c}:{p}" for c, p in sorted(punteggi.items(), key=lambda kv: -kv[1]))
                    probabile_non_pubblico, segnali = rileva_non_pubblico(titolo, meta_desc, corpo, forme_regex, parole_commerciali)
                    row["probabile_non_pubblico"] = probabile_non_pubblico
                    row["segnali_non_pubblico_rilevati"] = ";".join(segnali)
                else:
                    row["tipo_ente_rilevato"] = "non_classificato"
                    row["punteggio_top"] = 0
                    row["probabile_non_pubblico"] = False
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

    # File secondario con i soli candidati "possibile ente non pubblico"
    # (vedi docstring): letto dall'INTERO out_arg appena scritto (non solo
    # dalle righe di questo run), così resta corretto anche eseguendo lo
    # script più volte con --resume su blocchi diversi.
    tutto = pd.read_csv(out_path, dtype=str)
    candidati = tutto[tutto["probabile_non_pubblico"].astype(str).str.lower() == "true"]
    possibili_path = str(out_arg).rsplit(".", 1)[0] + "_possibili_non_pubblici.csv"
    candidati.to_csv(possibili_path, index=False, encoding="utf-8")
    print(
        f"{len(candidati)}/{len(tutto)} hostname con segnali di possibile ente non pubblico "
        f"(forma societaria privata e/o terminologia commerciale nella homepage): elenco in "
        f"{possibili_path}. Sono CANDIDATI da rivedere a mano, non un'esclusione automatica "
        f"(vedi limite metodologico nella docstring dello script).",
        file=sys.stderr,
    )

    cfgmod.aggiorna_puntatore_latest(paese, run_id)


if __name__ == "__main__":
    main()
