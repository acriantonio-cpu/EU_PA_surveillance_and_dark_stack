#!/usr/bin/env python3
"""
classify_pa_sites.py — classifica i domini scoperti dal crawler a livelli
(SiteCrawlFetcher, source_type: site_crawl) come "pubblico"/"da verificare"/
"probabile non pubblico", e ne rileva il TIPO DI ENTE (comune, scuola,
sanità, università, ...), scrivendo un siti.csv coerente con lo schema
comune del progetto (src/schema.py::FIELDNAMES).

PERCHÉ SERVE (gap reale nel codice attuale)
--------------------------------------------
Il crawler a livelli (site_crawl) distingue SOLO "interno" da "esterno":
un dominio è "interno" se il suo hostname combacia con 'allowed_domains'
OPPURE con 'allowed_tlds' (di default: il ccTLD del paese + il label
"gov" — vedi COUNTRY_TLDS e SiteCrawlFetcher in src/fetchers/generic.py).
È un'euristica sulla STRUTTURA del dominio, decisa DURANTE il crawl, non
una verifica sul CONTENUTO della pagina: un dominio può finire "interno"
solo perché contiene ".gov." o il ccTLD nazionale, senza che nessuno
abbia mai controllato che la homepage sia davvero di un ente pubblico.
Le scoperte "esterno" sono ancora più deboli: sono semplicemente ogni
link uscente incontrato, poi passate allo screening minacce
(tools/postprocess_checkpoints.py) — che verifica se un dominio è
MALEVOLO, non se è un ente pubblico.

La classificazione per TIPO DI ENTE (comune/scuola/ospedale/...) esiste
già, ma solo a valle, in dst/src/08_classifica_tipo_ente.py — pensata per
liste GIÀ assunte pubbliche (IndicePA, o un registro bulk ufficiale). Non
c'è oggi, in eu_pa_scraper, uno script che faccia la STESSA cosa
sull'output grezzo del crawler, dove questa verifica serve di più (non
viene da un registro ufficiale, viene da link scoperti navigando).

Questo script chiude il gap SENZA duplicare la logica: riusa lo stesso
file di regole (data/rules/tipo_ente_rules.yaml, 11 categorie x 24 lingue
UE) già usato da dst/08_classifica_tipo_ente.py — unica fonte di verità
condivisa fra i due repository, stesso principio già scelto dal progetto
per cms_signatures.yaml e dark_stack_rules.yaml. Punta --rules alla copia
in dst (o incolla il file qui): NON creare una seconda copia delle regole,
per evitare che le due repository divergano nel tempo.

COME FUNZIONA
-------------
Per ogni hostname (da crawl_results.json grezzo, o da un siti.csv già
esistente con 'sector'/'nome_ente' vuoti):
  1. Euristica di dominio (segnale debole, "punteggio_dominio"): il ccTLD
     nazionale o il label "gov" nell'hostname contano come primo indizio
     — stessa logica di 'allowed_tlds' nel crawler, qui usata come
     SEGNALE aggiuntivo, non come filtro definitivo.
  2. Scarica la homepage (https poi http, come base_fetcher.HttpSession)
     e classifica titolo/meta description/corpo con le stesse parole
     chiave di tipo_ente_rules.yaml (match in titolo/meta = 3 punti,
     match nel corpo = 1 punto — un ente che si autodichiara
     nell'intestazione è un segnale più forte di una menzione persa nel
     testo). Stessa logica di dst/08_classifica_tipo_ente.py::classifica().
  3. Cerca ANCHE segnali di ente NON pubblico (forma societaria privata:
     S.r.l./GmbH/Ltd..., o terminologia commerciale/e-commerce) sulla
     stessa pagina già scaricata — stessa logica di
     dst/08_classifica_tipo_ente.py::rileva_non_pubblico().
  4. Verdetto finale per riga:
       - "pubblico_verificato"     : almeno una categoria riconosciuta
                                     E nessun segnale di ente privato
       - "probabile_non_pubblico"  : segnali di ente privato rilevati
                                     (a prescindere dalla categoria)
       - "da_verificare_a_mano"    : nessuna categoria riconosciuta e
                                     nessun segnale privato (pagina
                                     minimale, JS-rendered, sito bloccato,
                                     errore di rete, ecc.)

LIMITE METODOLOGICO (lo stesso di dst/08_classifica_tipo_ente.py, onestà
prima di tutto): è un classificatore a PAROLE CHIAVE su testo statico
(HTML pre-JavaScript), non un modello linguistico. Un ente che non si
autodichiara nella homepage resta 'da_verificare_a_mano' — NON viene mai
scartato automaticamente, solo segnalato per revisione umana, stesso
principio già usato per le minacce in link_sospetti.md.

OUTPUT (due file, stesso pattern di dst/08_classifica_tipo_ente.py)
---------------------------------------------------------------------
  <out>                 : siti.csv "stretto", ESATTAMENTE le 10 colonne
                           di src/schema.py::FIELDNAMES, nell'ordine:
                           hostname,tipo_link,nome_ente,comune,regione,
                           codice_ipa,sector,country_code,source_name,
                           retrieved_at
                           + colonne diagnostiche IN CODA (non rompono un
                           DictReader che legge solo le 10 canoniche):
                           verdetto, punteggio_categoria, punteggio_dominio,
                           segnali_non_pubblico
  <out>_da_rivedere.csv : solo le righe con verdetto diverso da
                           "pubblico_verificato" — CANDIDATE da rivedere a
                           mano, non un'esclusione automatica (stesso
                           principio di link_sospetti e di
                           _possibili_non_pubblici in dst).
  <out>_manifest.json   : stesso pattern di manifest già usato altrove nel
                           progetto (parametri del run, conteggio regole,
                           limite metodologico dichiarato).

USO
---
  # da un crawl_results.json grezzo (site_crawl):
  python tools/classify_pa_sites.py \\
      --in data/input/DE/crawl_service_bund.json \\
      --paese DE --country-code DE --source-name "service.bund.de (crawl)" \\
      --rules ../dst/data/rules/tipo_ente_rules.yaml \\
      --out data/output/DE/siti_classificati.csv

  # da un siti.csv già prodotto (per ri-classificare 'sector'/verdetto):
  python tools/classify_pa_sites.py \\
      --in data/output/BE/siti.csv --paese BE \\
      --rules ../dst/data/rules/tipo_ente_rules.yaml \\
      --out data/output/BE/siti_classificati.csv

NOTA A MARGINE (bug reale trovato preparando questo script): in DE.yaml e
BE.yaml, field_mapping mappa 'hostname: "external_url"'. Ma
_register() in src/fetchers/generic.py scrive
external_url = f"https://{root}" (CON schema) mentre external_domain è il
dominio nudo. Il risultato è che 'hostname' nel siti.csv di questi due
paesi contiene oggi "https://esempio.de" invece di "esempio.de" — questo
script, quando riceve un CSV già mappato così, lo ripulisce da solo
(vedi _hostname_pulito), ma vale la pena correggere field_mapping in
DE.yaml/BE.yaml da 'external_url' a 'external_domain' alla fonte, così
anche il siti.csv "grezzo" resta corretto senza bisogno di questo script.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup

try:
    import tldextract
    _TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())
except ImportError:  # pragma: no cover
    tldextract = None
    _TLD_EXTRACTOR = None

# Stesse 10 colonne di src/schema.py::FIELDNAMES — NON modificare l'ordine,
# è il contratto condiviso con dst/01_fetch_ipa_comuni.py e con tutto il
# resto della pipeline a valle.
SCHEMA_FIELDNAMES = [
    "hostname", "tipo_link", "nome_ente", "comune", "regione",
    "codice_ipa", "sector", "country_code", "source_name", "retrieved_at",
]
DIAGNOSTIC_FIELDNAMES = [
    "verdetto", "punteggio_categoria", "punteggio_dominio", "segnali_non_pubblico",
]
OUT_FIELDNAMES = SCHEMA_FIELDNAMES + DIAGNOSTIC_FIELDNAMES

# Stesso default di allowed_tlds nel crawler (src/fetchers/generic.py):
# ccTLD nazionale + label "gov" come segnale di dominio istituzionale.
COUNTRY_TLDS: dict[str, str] = {
    "AT": "at", "BE": "be", "BG": "bg", "CY": "cy", "CZ": "cz", "DE": "de",
    "DK": "dk", "EE": "ee", "EL": "gr", "ES": "es", "FI": "fi", "FR": "fr",
    "HR": "hr", "HU": "hu", "IE": "ie", "IT": "it", "LT": "lt", "LU": "lu",
    "LV": "lv", "MT": "mt", "NL": "nl", "PL": "pl", "PT": "pt", "RO": "ro",
    "SE": "se", "SI": "si", "SK": "sk",
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; eu-pa-scraper-classify/1.0)"}


def _root_domain(url_or_host: str) -> str:
    """Stessa funzione di src/fetchers/generic.py::_root_domain (duplicata
    qui per rendere questo script eseguibile anche da solo, fuori dal
    pacchetto src/ — vedi nota sulle regole condivise nella docstring)."""
    if _TLD_EXTRACTOR is None:
        host = urlparse(url_or_host).netloc.lower().split(":")[0] or str(url_or_host).lower()
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    ext = _TLD_EXTRACTOR(url_or_host)
    if ext.suffix:
        return f"{ext.domain}.{ext.suffix}".lower()
    return (ext.domain or "").lower()


def _hostname_pulito(valore: str) -> str:
    """Ripulisce un campo 'hostname' che potrebbe contenere ancora lo
    schema (bug di field_mapping in DE.yaml/BE.yaml, vedi docstring): se
    valore è un URL completo, ne estrae il dominio radice; se è già un
    hostname nudo, lo restituisce inalterato (minuscolo, senza spazi)."""
    valore = (valore or "").strip()
    if "://" in valore:
        return _root_domain(valore)
    return valore.lower()


def carica_regole_categorie(path: Path) -> dict[str, list[str]]:
    """Stesso formato/logica di dst/08_classifica_tipo_ente.py::load_rules:
    appiattisce tutte le lingue insieme per categoria (si classifica
    "alla cieca" su tutte le 24 lingue UE contemporaneamente)."""
    with open(path, encoding="utf-8") as f:
        dati = yaml.safe_load(f) or {}
    out: dict[str, list[str]] = {}
    for categoria, lingue in (dati.get("categorie") or {}).items():
        parole: list[str] = []
        for lista in (lingue or {}).values():
            parole.extend(p.lower() for p in (lista or []))
        out[categoria] = sorted(set(parole), key=len, reverse=True)
    return out


def carica_regole_non_pubblico(path: Path) -> tuple[list[re.Pattern], list[str]]:
    """Stesso formato/logica di dst/08_classifica_tipo_ente.py::load_non_pubblico_rules."""
    with open(path, encoding="utf-8") as f:
        dati = yaml.safe_load(f) or {}
    forme = sorted(
        {p.lower() for lista in (dati.get("forme_societarie") or {}).values() for p in (lista or [])},
        key=len, reverse=True,
    )
    commerciale = sorted(
        {p.lower() for lista in (dati.get("commerciale") or {}).values() for p in (lista or [])},
        key=len, reverse=True,
    )
    forme_regex = [re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE) for p in forme]
    return forme_regex, commerciale


def punteggio_dominio(hostname: str, paese: str) -> int:
    """Segnale debole (0, 1 o 2) sulla sola struttura del dominio: ccTLD
    nazionale = +1, label 'gov' fra i sotto-label del dominio = +1. Stesso
    principio di 'allowed_tlds' nel crawler, qui usato come indizio in
    più, non come filtro (vedi verdetto finale)."""
    host = hostname.lower()
    p = 0
    cctld = COUNTRY_TLDS.get(paese.upper())
    if cctld and host.endswith("." + cctld):
        p += 1
    if re.search(r"(?<![a-z0-9])gov(?![a-z0-9])", host):
        p += 1
    return p


def scarica_homepage(hostname: str, timeout: float = 8.0, max_bytes: int = 2_000_000):
    """Stessa strategia di dst/08_classifica_tipo_ente.py::fetch_pagina:
    prova https poi http, stream fino a max_bytes, non fallisce mai in
    modo fatale (ritorna html='' se entrambi gli schemi falliscono)."""
    for scheme in ("https", "http"):
        try:
            resp = requests.get(
                f"{scheme}://{hostname}/", timeout=timeout, verify=False,
                allow_redirects=True, headers=UA, stream=True,
            )
            raw = resp.raw.read(max_bytes + 1, decode_content=True)[:max_bytes]
            encoding = resp.encoding or "utf-8"
            try:
                html = raw.decode(encoding, errors="replace")
            except (LookupError, TypeError):
                html = raw.decode("utf-8", errors="replace")
            resp.close()
            return resp.status_code, html, None
        except requests.RequestException as exc:
            ultimo_errore = type(exc).__name__
            continue
    return None, "", ultimo_errore


def estrai_testo_pesato(html: str) -> tuple[str, str, str]:
    """Identica a dst/08_classifica_tipo_ente.py::estrai_testo_pesato."""
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


def classifica_categoria(titolo: str, meta_desc: str, corpo: str, regole: dict[str, list[str]]):
    """Identica a dst/08_classifica_tipo_ente.py::classifica: match in
    titolo/meta = 3 punti, match nel corpo = 1 punto. Ritorna
    (categoria_top_o_None, punteggio_top, dict_punteggi_non_zero)."""
    forte = f"{titolo} {meta_desc}".lower()
    debole = corpo.lower()
    punteggi: dict[str, int] = {}
    for categoria, parole in regole.items():
        p = 0
        for parola in parole:
            if parola in forte:
                p += 3
            elif parola in debole:
                p += 1
        if p > 0:
            punteggi[categoria] = p
    if not punteggi:
        return None, 0, {}
    top = max(punteggi, key=punteggi.get)
    return top, punteggi[top], punteggi


def rileva_non_pubblico(titolo: str, meta_desc: str, corpo: str, forme_regex, parole_commerciali) -> tuple[bool, list[str]]:
    """Identica a dst/08_classifica_tipo_ente.py::rileva_non_pubblico."""
    testo = f"{titolo} {meta_desc} {corpo}"
    segnali: list[str] = []
    for rx in forme_regex:
        m = rx.search(testo)
        if m:
            segnali.append(f"forma_societaria:{m.group(0).lower()}")
    testo_low = testo.lower()
    for parola in parole_commerciali:
        if parola in testo_low:
            segnali.append(f"commerciale:{parola}")
    return bool(segnali), segnali


def carica_input(path: Path) -> list[dict[str, Any]]:
    """Legge sia un crawl_results.json grezzo (SiteCrawlFetcher) sia un
    siti.csv già nello schema comune, normalizzando entrambi in un'unica
    forma interna: {hostname, tipo_link, source_seed_o_found_on}."""
    righe: list[dict[str, Any]] = []
    if path.suffix.lower() == ".json":
        dati = json.loads(path.read_text(encoding="utf-8"))
        for rec in dati:
            righe.append({
                "hostname": rec.get("external_domain") or _root_domain(rec.get("external_url", "")),
                "tipo_link": rec.get("scope", ""),
            })
    else:
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                righe.append({
                    "hostname": _hostname_pulito(row.get("hostname", "")),
                    "tipo_link": row.get("tipo_link", ""),
                })
    # Dedup per hostname, preservando il primo tipo_link incontrato.
    visti: dict[str, dict[str, Any]] = {}
    for r in righe:
        h = r["hostname"]
        if h and h not in visti:
            visti[h] = r
    return list(visti.values())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="infile", required=True, help="crawl_results.json grezzo OPPURE un siti.csv già nello schema comune")
    ap.add_argument("--out", required=True, help="CSV di output (siti.csv 'stretto' + colonne diagnostiche)")
    ap.add_argument("--paese", required=True, help="Codice paese ISO a due lettere (es. DE, BE) — usato per il ccTLD e per country_code in output")
    ap.add_argument("--rules", required=True, help="Percorso a tipo_ente_rules.yaml — punta alla copia in dst, non duplicarlo")
    ap.add_argument("--country-code", default=None, help="Default: uguale a --paese")
    ap.add_argument("--source-name", default="site_crawl (classificato)", help="Valore per la colonna 'source_name'")
    ap.add_argument("--http-timeout", type=float, default=8.0)
    ap.add_argument("--delay", type=float, default=0.5, help="Pausa (s) fra un hostname e il successivo — cortesia verso i server")
    ap.add_argument("--limit", type=int, default=None, help="Limita a N hostname (per test rapidi)")
    args = ap.parse_args()

    paese = args.paese.strip().upper()
    country_code = (args.country_code or paese).strip().upper()
    rules_path = Path(args.rules)
    if not rules_path.exists():
        sys.exit(f"ERRORE: non trovo {rules_path}. Punta --rules alla copia di tipo_ente_rules.yaml in dst.")

    regole_categorie = carica_regole_categorie(rules_path)
    forme_regex, parole_commerciali = carica_regole_non_pubblico(rules_path)

    righe_in = carica_input(Path(args.infile))
    if args.limit:
        righe_in = righe_in[: args.limit]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rivedere_path = out_path.with_name(out_path.stem + "_da_rivedere.csv")
    manifest_path = out_path.with_name(out_path.stem + "_manifest.json")

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_pubblico = n_da_rivedere = n_non_pubblico = n_errori = 0

    with out_path.open("w", newline="", encoding="utf-8-sig") as f_out, \
         rivedere_path.open("w", newline="", encoding="utf-8-sig") as f_rev:
        writer = csv.DictWriter(f_out, fieldnames=OUT_FIELDNAMES)
        writer.writeheader()
        writer_rev = csv.DictWriter(f_rev, fieldnames=OUT_FIELDNAMES)
        writer_rev.writeheader()

        totale = len(righe_in)
        for i, r in enumerate(righe_in, start=1):
            hostname = r["hostname"]
            print(f"[{i}/{totale}] {hostname}", file=sys.stderr)

            p_dominio = punteggio_dominio(hostname, paese)
            status, html, errore = scarica_homepage(hostname, timeout=args.http_timeout)

            if html:
                titolo, meta_desc, corpo = estrai_testo_pesato(html)
                categoria, punteggio_cat, _ = classifica_categoria(titolo, meta_desc, corpo, regole_categorie)
                non_pubblico, segnali = rileva_non_pubblico(titolo, meta_desc, corpo, forme_regex, parole_commerciali)
            else:
                categoria, punteggio_cat, segnali, non_pubblico = None, 0, [], False
                n_errori += 1

            if non_pubblico:
                verdetto = "probabile_non_pubblico"
                sector = categoria or "probabile_non_pubblico"
                n_non_pubblico += 1
            elif categoria:
                verdetto = "pubblico_verificato"
                sector = categoria
                n_pubblico += 1
            else:
                verdetto = "da_verificare_a_mano"
                sector = "non_classificato"
                n_da_rivedere += 1

            row = {
                "hostname": hostname,
                "tipo_link": r.get("tipo_link", ""),
                "nome_ente": (titolo if html else "").strip()[:200] if html else "",
                "comune": "",
                "regione": "",
                "codice_ipa": "",
                "sector": sector,
                "country_code": country_code,
                "source_name": args.source_name,
                "retrieved_at": ts,
                "verdetto": verdetto,
                "punteggio_categoria": punteggio_cat,
                "punteggio_dominio": p_dominio,
                "segnali_non_pubblico": ";".join(segnali),
            }
            writer.writerow(row)
            if verdetto != "pubblico_verificato":
                writer_rev.writerow(row)
            f_out.flush()
            time.sleep(args.delay)

    manifest = {
        "timestamp_utc": ts,
        "paese": paese,
        "script": "classify_pa_sites.py",
        "input": str(args.infile),
        "rules_file": str(rules_path),
        "n_hostname_totali": len(righe_in),
        "n_pubblico_verificato": n_pubblico,
        "n_da_verificare_a_mano": n_da_rivedere,
        "n_probabile_non_pubblico": n_non_pubblico,
        "n_errori_download_homepage": n_errori,
        "limite_metodologico": (
            "Classificatore a parole chiave su testo statico (HTML pre-JavaScript), stesso "
            "principio e stesse regole di dst/08_classifica_tipo_ente.py. Un ente che non si "
            "autodichiara nella homepage resta 'da_verificare_a_mano', MAI escluso in automatico. "
            "'nome_ente' e' il <title> grezzo della pagina, non una denominazione ufficiale ripulita."
        ),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"\nFatto. {len(righe_in)} hostname processati: "
        f"{n_pubblico} pubblico_verificato, {n_da_rivedere} da_verificare_a_mano, "
        f"{n_non_pubblico} probabile_non_pubblico, {n_errori} con errore di download.\n"
        f"Output completo: {out_path}\n"
        f"Solo le righe da rivedere: {rivedere_path}\n"
        f"Manifesto: {manifest_path}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
