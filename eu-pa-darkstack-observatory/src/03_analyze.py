#!/usr/bin/env python3
# EN: Step 03 - analysis: HHI operator concentration (by ASN) for hosting/mail/NS and third-party
# trackers, adoption rates, composite 0-5 per-entity risk score (enti_a_rischio.csv),
# technology-dependency index, report.json and plain-language report_sintesi.md.
"""
03_analyze.py

Legge data/results/risultati.csv (output di 02_probe_infra.py) e calcola:
- concentrazione degli operatori DNS/mail/nameserver: indice
  Herfindahl-Hirschman (HHI), raggruppato per ASN (identificatore stabile),
  NON per la stringa organizzazione RDAP (che varia per lo stesso operatore
  su blocchi IP diversi, es. "Aruba S.p.A." vs "ARUBA S.P.A." su due /24
  diversi: raggrupparla per nome frammenterebbe artificialmente la
  concentrazione, che è esattamente la metrica che questo report vuole
  misurare con precisione). Ogni HHI è accompagnato da un'etichetta di
  concentrazione (soglie stile DOJ/FTC) e da un flag di affidabilità basato
  sul numero di dati validi, non solo sulla dimensione del gruppo: un HHI
  calcolato su 1-2 dati validi viene segnalato come non affidabile invece di
  comparire come se fosse un dato solido.
- la STESSA concentrazione ricalcolata deduplicando gli hostname condivisi
  da più enti (vista "per hostname unico", accanto a quella "per ente"):
  un hostname usato da 8 comuni pesa 8 volte nella vista per-ente e 1 sola
  volta in quella per-hostname-unico — sono due letture legittime e diverse
  dello stesso fenomeno, non un errore da scegliere fra le due.
- copertura DNSSEC, SPF, DMARC (con distribuzione completa della policy
  DMARC, non solo il binario "blocca/non blocca") sul campione
- quota di enti senza ridondanza sui nameserver
- riepilogo errori di misurazione (RDAP rate limit, timeout, ecc.): quanti
  enti hanno almeno un errore e quali sono i tipi più comuni, perché un
  campione con molti errori va letto diversamente da uno pulito
- certificati TLS: distribuzione versione/emittente, certificati scaduti o
  in scadenza entro 30 giorni (dato azionabile, non solo descrittivo)
- distribuzione codici di stato HTTP
- HHI aggregato per settore e per regione
- riepilogo Dark Stack (idea 1) e cookie di profilazione, se
  data/results/dark_stack.csv è disponibile
- tempi di risposta HTTP (media/mediana/p95), se la colonna
  http_response_time_ms è nel risultati.csv (02_probe_infra.py con
  tempi_risposta=true)
- robustezza TLS (voto tls_grade, protocolli deboli ancora attivi, copertura
  HSTS), se le colonne sono nel risultati.csv (02_probe_infra.py con
  tls_avanzato=true)
- CDN extra-UE: incrocio esplicito fra cdn_rilevato e giurisdizione_holding
  già presenti nel risultati.csv, nessun dato aggiuntivo richiesto
- aggregazione geografica esplicita di giurisdizione_holding per macro-area
  (USA, Italia, Francia, ecc.), non solo l'elenco delle singole etichette
- censimento software/licenze, se data/results/cms_fingerprint.csv è
  disponibile (06_fingerprint_cms.py)

Scrive anche, oltre a report.json:
- enti_a_rischio.csv: un rigo per ente con un punteggio di rischio composito
  (niente ridondanza NS + DMARC non attivo + niente DNSSEC + certificato in
  scadenza/scaduto + TLS grade basso), ordinato dal più a rischio
- report_sintesi.md: riepilogo in linguaggio naturale dei numeri principali,
  pensato per essere letto da chi non deve interpretare un JSON

Ognuna di queste sezioni viene calcolata SOLO se le colonne/i file
corrispondenti sono presenti: un risultati.csv prodotto con una misurazione
disattivata in config.yaml non causa un errore, semplicemente quella
sezione del report manca (invece di comparire piena di valori vuoti/fuorvianti).

Va eseguito dalla cartella principale del progetto.

Esempio:
    python src\\03_analyze.py
    python src\\03_analyze.py --salva-storico   # tiene anche una copia datata di report.json
"""

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config as cfgmod
import errors as _errors

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Spazio Economico Europeo (UE27 + Islanda, Liechtenstein, Norvegia): stessa
# definizione/costante di 05_scrape_dark_stack.py (duplicata invece che
# importata per tenere questo script eseguibile anche da solo, senza
# dipendere dalla presenza degli altri file di script).
SEE_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "IS", "LI", "NO",
}

# Colonne ASN: vanno SEMPRE lette come testo. Se non forzate, pandas le
# legge come float64 appena una riga ha un ASN mancante (NaN), e un ASN
# come "15169" diventa la stringa "15169.0" ovunque venga poi mostrato o
# concatenato — bug scoperto proprio leggendo un'estrazione reale.
COLONNE_ASN_TESTO = ["codice_ipa", "asn_a", "asn_mx", "asn_ns"]

# Sotto questa soglia di dati validi, un HHI viene comunque calcolato (è
# aritmetica, non fallisce) ma marcato esplicitamente come inaffidabile: con
# 1-2 punti dati un HHI può risultare 10000 (concentrazione "massima") per
# puro effetto del campione minuscolo, non perché il mercato sia davvero
# in mano a un solo operatore.
SOGLIA_MINIMA_HHI = 10


def hhi_categoria(valore: float) -> str:
    """Soglie di concentrazione stile DOJ/FTC (Horizontal Merger
    Guidelines), usate qui solo come riferimento per dare un'etichetta
    leggibile all'HHI, non come giudizio antitrust in senso stretto."""
    if valore < 1500:
        return "non concentrato"
    if valore < 2500:
        return "moderatamente concentrato"
    return "altamente concentrato"


def dati_results(paese: str, run_id: str) -> Path:
    return PROJECT_ROOT / "data" / "results" / paese / run_id


def dati_processed(paese: str) -> Path:
    return PROJECT_ROOT / "data" / "processed" / paese


def hhi(counter: Counter) -> float:
    """HHI standard: somma dei quadrati delle quote di mercato (%), range 0-10000."""
    total = sum(counter.values())
    if total == 0:
        return 0.0
    return sum((n / total * 100) ** 2 for n in counter.values())


def summarize(df: pd.DataFrame, col_org: str, col_asn: str):
    """Concentrazione raggruppata per ASN. 'organizzazione' nel top10 è
    l'etichetta RDAP più frequente osservata per quell'ASN nel campione,
    solo per leggibilità: il conteggio/HHI usa l'ASN, non la stringa.

    Include sempre: affidabilita_copertura (basata su copertura_pct: quanti
    dati mancano) e hhi_affidabile (basata su con_dato_valido: con pochi
    punti dati l'HHI è aritmeticamente valido ma statisticamente rumoroso,
    vedi SOGLIA_MINIMA_HHI) — due segnali diversi, entrambi utili a chi
    legge il numero senza fermarsi al valore assoluto."""
    valid = df[[col_asn, col_org]].copy()
    valid = valid[valid[col_asn].notna()]  # esplicito: .astype(str) su NaN non dà sempre "nan" (dipende dal backend pandas)
    valid[col_asn] = valid[col_asn].astype(str).str.strip()
    valid = valid[valid[col_asn] != ""]

    asn_counts = Counter(valid[col_asn])
    n_valid = int(sum(asn_counts.values()))
    n_total = len(df)
    copertura_pct = round(100 * n_valid / n_total, 1) if n_total else 0

    if copertura_pct >= 80:
        affidabilita_copertura = "alta"
    elif copertura_pct >= 50:
        affidabilita_copertura = "media"
    else:
        affidabilita_copertura = "bassa"

    label_per_asn = {}
    for asn, group in valid.groupby(col_asn)[col_org]:
        nomi = Counter(o for o in group if isinstance(o, str) and o.strip())
        label_per_asn[asn] = nomi.most_common(1)[0][0] if nomi else asn

    top10 = asn_counts.most_common(10)
    valore_hhi = round(hhi(asn_counts), 1)
    return {
        "dimensione_campione": n_total,
        "con_dato_valido": n_valid,
        "copertura_pct": copertura_pct,
        "affidabilita_copertura": affidabilita_copertura,
        "n_asn_distinti": len(asn_counts),
        "hhi_operatori": valore_hhi,
        "hhi_categoria": hhi_categoria(valore_hhi),
        "hhi_affidabile": n_valid >= SOGLIA_MINIMA_HHI,
        "hhi_nota": (
            None if n_valid >= SOGLIA_MINIMA_HHI else
            f"Calcolato su solo {n_valid} dati validi (soglia minima consigliata: "
            f"{SOGLIA_MINIMA_HHI}): il valore è aritmeticamente corretto ma può essere "
            "molto distante dalla concentrazione reale, va letto con cautela."
        ),
        "top10_operatori": [
            {
                "asn": asn,
                "organizzazione": label_per_asn.get(asn, ""),
                "n_enti": n,
                "quota_pct": round(100 * n / n_valid, 1) if n_valid else 0,
            }
            for asn, n in top10
        ],
    }


def summarize_deduplicato(df: pd.DataFrame, col_org: str, col_asn: str, col_hostname: str = "hostname"):
    """Come summarize(), ma dopo aver tenuto una sola riga per hostname
    distinto: risponde alla domanda 'quante infrastrutture fisiche diverse
    ci sono', mentre summarize() sul dataframe intero risponde a 'da quanti
    enti dipende ciascun operatore' — entrambe legittime, diverse."""
    if col_hostname not in df.columns:
        return None
    dedup = df.drop_duplicates(subset=[col_hostname])
    risultato = summarize(dedup, col_org, col_asn)
    risultato["nota"] = (
        f"Stessa metrica di sopra ma su {len(dedup)} hostname distinti invece di "
        f"{len(df)} righe/enti (un hostname condiviso da più enti pesa qui UNA sola "
        "volta, non una per ogni ente che lo usa)."
    )
    return risultato


def summarize_errori(df: pd.DataFrame):
    """Riepilogo degli errori di misurazione registrati in 02_probe_infra.py
    (colonna 'errori', formato 'tipo1:dettaglio;tipo2:dettaglio'). Un
    campione con molti errori RDAP/timeout ha metriche di concentrazione
    meno affidabili di uno pulito: questo riepilogo lo rende visibile invece
    di lasciarlo nascosto in una colonna che nessuno riassume.

    OTTIMIZZAZIONE (seconda passata, settembre 2026): oltre al breakdown per
    CAMPO già esistente (top10_tipi_errore: dove è fallita la misurazione,
    es. 'asn_a_lookup_failed'), aggiunge un secondo breakdown per CAUSA
    CANONICA (top10_categorie_errore: perché è fallita, es. 'rate_limited',
    'timeout' — vedi errors.classifica_errore()). I due dicono cose diverse
    e complementari: il primo dice quale colonna del CSV guardare, il
    secondo dice quale intervento in asn_bulk.py/config.yaml (alzare i
    timeout? il problema sono i rate limit RDAP? abbassare la concorrenza?)
    ha più probabilità di aiutare — additivo, non sostituisce il primo."""
    if "errori" not in df.columns:
        return None
    n = len(df)
    serie_errori = df["errori"].fillna("").astype(str)
    con_errori = serie_errori.str.strip() != ""
    tipi = Counter()
    categorie = Counter()
    for cella in serie_errori[con_errori]:
        for parte in cella.split(";"):
            parte = parte.strip()
            if parte:
                tipo, _, dettaglio = parte.partition(":")
                tipi[tipo] += 1
                categorie[_errors.classifica_errore(dettaglio or tipo)] += 1
    return {
        "n_enti_con_almeno_un_errore": int(con_errori.sum()),
        "pct_enti_con_almeno_un_errore": round(100 * con_errori.sum() / n, 1) if n else 0,
        "top10_tipi_errore": [{"tipo": t, "n_occorrenze": v} for t, v in tipi.most_common(10)],
        "top10_categorie_errore": [{"categoria": c, "n_occorrenze": v} for c, v in categorie.most_common(10)],
        "nota": (
            "Un errore su un campo (es. asn_a_lookup_failed) non impedisce le altre "
            "misurazioni sulla stessa riga: guarda le colonne specifiche per capire "
            "cosa manca su un singolo ente. 'top10_tipi_errore' dice DOVE (quale campo), "
            "'top10_categorie_errore' dice PERCHÉ in termini canonici (rate_limited, timeout, "
            "connessione_rifiutata, ...): rate_limited frequente indica rate limiting dei "
            "registri RDAP/RIPEstat, non un problema dei siti misurati."
        ),
    }


def summarize_dmarc(df: pd.DataFrame):
    """Distribuzione COMPLETA della policy DMARC, non solo il binario
    presente/reject-quarantine: la fascia 'none' (monitoraggio senza
    blocco) è la più interessante da segnalare a parte perché indica enti
    che hanno iniziato ad adottare DMARC ma non protegge ancora da spoofing."""
    n = len(df)
    presente = (df["dmarc_presente"] == True)
    policy = df["dmarc_policy"].fillna("").astype(str).str.strip().str.lower()
    distribuzione = {
        "reject": int((policy == "reject").sum()),
        "quarantine": int((policy == "quarantine").sum()),
        "none": int((policy == "none").sum()),
        "assente_o_non_determinato": int(n - presente.sum()),
    }
    return {
        "dmarc_presente_pct": round(100 * presente.sum() / n, 1) if n else 0,
        "distribuzione_policy": distribuzione,
        "distribuzione_policy_pct": {
            k: round(100 * v / n, 1) if n else 0 for k, v in distribuzione.items()
        },
    }


def summarize_certificati(df: pd.DataFrame):
    """Distribuzione versione/emittente TLS e certificati scaduti o in
    scadenza entro 30 giorni: dato azionabile (serve rinnovarli), non solo
    descrittivo, oggi non riassunto da nessuna parte nel report."""
    if "tls_version" not in df.columns:
        return None
    n = len(df)
    versioni = Counter(v for v in df["tls_version"] if isinstance(v, str) and v.strip())
    emittenti = Counter(v for v in df["tls_issuer"] if isinstance(v, str) and v.strip())
    giorni = pd.to_numeric(df.get("tls_giorni_scadenza"), errors="coerce")
    scaduti = giorni[giorni < 0]
    in_scadenza_30gg = giorni[(giorni >= 0) & (giorni <= 30)]
    return {
        "n_enti_con_certificato_letto": int(giorni.notna().sum()),
        "distribuzione_versione_tls": [{"versione": v, "n_enti": c} for v, c in versioni.most_common()],
        "top10_emittenti": [{"emittente": e, "n_enti": c} for e, c in emittenti.most_common(10)],
        "certificati_scaduti": {
            "n_enti": int(len(scaduti)),
            "pct_su_campione": round(100 * len(scaduti) / n, 1) if n else 0,
        },
        "certificati_in_scadenza_entro_30_giorni": {
            "n_enti": int(len(in_scadenza_30gg)),
            "pct_su_campione": round(100 * len(in_scadenza_30gg) / n, 1) if n else 0,
        },
    }


def summarize_http_status(df: pd.DataFrame):
    if "http_status" not in df.columns:
        return None
    n = len(df)
    status = pd.to_numeric(df["http_status"], errors="coerce")
    con_dato = status.notna()
    fasce = {
        "2xx": int(((status >= 200) & (status < 300)).sum()),
        "3xx": int(((status >= 300) & (status < 400)).sum()),
        "4xx": int(((status >= 400) & (status < 500)).sum()),
        "5xx": int(((status >= 500) & (status < 600)).sum()),
        "nessuna_risposta": int((~con_dato).sum()),
    }
    codici = Counter(int(s) for s in status.dropna())
    return {
        "n_enti_con_risposta": int(con_dato.sum()),
        "pct_su_campione": round(100 * con_dato.sum() / n, 1) if n else 0,
        "distribuzione_fasce": fasce,
        "top10_codici": [{"codice": c, "n_enti": v} for c, v in codici.most_common(10)],
        "nota": (
            "403/406/429/503 possono indicare un WAF/anti-bot che blocca la richiesta "
            "(vedi User-Agent in config.yaml, http_user_agent) invece di un problema "
            "reale del sito: non trattare automaticamente come 'sito rotto'."
        ),
    }


# Estrae la macro-area geografica da un'etichetta 'giurisdizione_holding'
# tipo "USA (Cloudflare)" -> "USA", "Italia (Aruba)" -> "Italia". Le
# etichette non riconosciute dall'euristica ("(non riconosciuto; Paese ASN:
# XX)") vengono raggruppate per Paese ASN invece che scartate.
_RE_GIURISDIZIONE = re.compile(r"^([^(]+)\s*\(")
_RE_NON_RICONOSCIUTO = re.compile(r"Paese ASN:\s*([A-Z]{2})")


def macro_area_giurisdizione(etichetta: str) -> str:
    m = _RE_NON_RICONOSCIUTO.search(etichetta)
    if m:
        return f"Non riconosciuto ({m.group(1)})"
    m = _RE_GIURISDIZIONE.match(etichetta)
    if m:
        return m.group(1).strip()
    return etichetta.strip()


def summarize_giurisdizione_aggregata(df: pd.DataFrame):
    if "giurisdizione_holding" not in df.columns:
        return None
    n = len(df)
    etichette = [g for g in df["giurisdizione_holding"] if isinstance(g, str) and g.strip()]
    macro = Counter(macro_area_giurisdizione(g) for g in etichette)
    n_con_dato = sum(macro.values())
    return {
        "nota": "Aggregazione automatica delle etichette di giurisdizione_holding per macro-area geografica.",
        "n_enti_con_dato": n_con_dato,
        "pct_su_campione": round(100 * n_con_dato / n, 1) if n else 0,
        "distribuzione": [
            {"macro_area": m, "n_enti": v, "pct_su_campione": round(100 * v / n, 1) if n else 0}
            for m, v in macro.most_common()
        ],
    }


def calcola_rischio(df: pd.DataFrame) -> pd.DataFrame:
    """Punteggio di rischio composito, 0-5, un punto per ciascuna condizione
    (colonne mancanti = quella condizione non contribuisce, non fa fallire
    lo script): niente ridondanza NS, DMARC non attivo (assente o 'none'),
    niente DNSSEC, certificato scaduto o in scadenza entro 30gg, tls_grade
    C/D. È un indicatore per dare priorità a un intervento manuale, non un
    giudizio automatico definitivo su un singolo ente."""
    r = pd.DataFrame(index=df.index)
    r["punteggio_rischio"] = 0
    motivi = pd.Series([[] for _ in range(len(df))], index=df.index)

    if "ns_diversi_secondlevel" in df.columns:
        cond = pd.to_numeric(df["ns_diversi_secondlevel"], errors="coerce").fillna(0) <= 1
        r["punteggio_rischio"] += cond.astype(int)
        motivi[cond] = motivi[cond].apply(lambda l: l + ["nessuna ridondanza NS"])

    if "dmarc_policy" in df.columns:
        policy = df["dmarc_policy"].fillna("").astype(str).str.lower()
        cond = ~policy.isin(["reject", "quarantine"])
        r["punteggio_rischio"] += cond.astype(int)
        motivi[cond] = motivi[cond].apply(lambda l: l + ["DMARC non attivo (assente o solo osservazione)"])

    if "dnssec_ds_presente" in df.columns:
        cond = df["dnssec_ds_presente"] != True
        r["punteggio_rischio"] += cond.astype(int)
        motivi[cond] = motivi[cond].apply(lambda l: l + ["DNSSEC assente"])

    if "tls_giorni_scadenza" in df.columns:
        giorni = pd.to_numeric(df["tls_giorni_scadenza"], errors="coerce")
        cond = giorni.notna() & (giorni <= 30)
        r["punteggio_rischio"] += cond.astype(int)
        motivi[cond] = motivi[cond].apply(lambda l: l + ["certificato TLS scaduto o in scadenza entro 30 giorni"])

    if "tls_grade" in df.columns:
        cond = df["tls_grade"].isin(["C", "D"])
        r["punteggio_rischio"] += cond.astype(int)
        motivi[cond] = motivi[cond].apply(lambda l: l + ["voto TLS basso (C/D: cipher debole o protocolli deprecati attivi)"])

    r["motivi_rischio"] = motivi.apply(lambda l: "; ".join(l))
    return r


def summarize_dark_stack(ds: pd.DataFrame):
    n = len(ds)
    con_terze_parti = ds[ds["n_terze_parti"].fillna(0) > 0]
    n_con_terze_parti = len(con_terze_parti)
    n_con_extra_see = int((ds["n_extra_see"].fillna(0) > 0).sum())

    tutte_categorie = Counter()
    tutti_domini = Counter()
    for cats in ds["categorie_rilevate"].dropna():
        for c in str(cats).split(";"):
            if c:
                tutte_categorie[c] += 1
    for doms in ds["domini_terzi"].dropna():
        for d in str(doms).split(";"):
            if d:
                tutti_domini[d] += 1

    riepilogo = {
        "dimensione_campione": n,
        "con_almeno_una_terza_parte": n_con_terze_parti,
        "con_almeno_una_terza_parte_pct": round(100 * n_con_terze_parti / n, 1) if n else 0,
        "con_almeno_una_chiamata_extra_see": n_con_extra_see,
        "con_almeno_una_chiamata_extra_see_pct": round(100 * n_con_extra_see / n, 1) if n else 0,
        "media_terze_parti_per_sito": round(ds["n_terze_parti"].fillna(0).mean(), 1) if n else 0,
        "top10_categorie": [{"categoria": c, "n_siti": v} for c, v in tutte_categorie.most_common(10)],
        "top15_domini_terzi": [{"dominio": d, "n_siti": v} for d, v in tutti_domini.most_common(15)],
    }

    # Cookie di profilazione (cookie_tracker=true): colonne presenti solo se
    # 05_scrape_dark_stack.py è stato eseguito con quella misurazione attiva.
    if "n_cookie_profilazione" in ds.columns:
        n_prof = pd.to_numeric(ds["n_cookie_profilazione"], errors="coerce").fillna(0)
        n_tot = pd.to_numeric(ds["n_cookie_totali"], errors="coerce").fillna(0)
        con_profilazione = int((n_prof > 0).sum())
        nomi_profilazione = Counter()
        for nomi in ds["cookie_profilazione_nomi"].dropna():
            for nm in str(nomi).split(";"):
                if nm:
                    nomi_profilazione[nm] += 1
        riepilogo["cookie"] = {
            "nota": (
                "Classificazione euristica sul NOME del cookie (vedi data/rules/cookie_rules.yaml), "
                "non una valutazione giuridica GDPR/ePrivacy. Cattura solo i cookie impostati dalla "
                "prima risposta HTTP, non quelli impostati via JavaScript dopo il consenso."
            ),
            "con_almeno_un_cookie_di_profilazione": con_profilazione,
            "con_almeno_un_cookie_di_profilazione_pct": round(100 * con_profilazione / n, 1) if n else 0,
            "media_cookie_totali_per_sito": round(n_tot.mean(), 1) if n else 0,
            "media_cookie_profilazione_per_sito": round(n_prof.mean(), 1) if n else 0,
            "top10_cookie_profilazione": [{"nome": nm, "n_siti": v} for nm, v in nomi_profilazione.most_common(10)],
        }

    return riepilogo


def summarize_tracker_hhi(ds: pd.DataFrame):
    """OTTIMIZZAZIONE (seconda passata, settembre 2026): concentrazione dei
    tracker/terze parti per ORGANIZZAZIONE MADRE (Google, Meta, Cloudflare,
    Microsoft, ...), non per singolo dominio — stesso approccio usato da
    Singh et al. (2026) per i siti governativi, e complementare a
    summarize_dark_stack() (che elenca i domini singoli più comuni, utile
    ma non direttamente un indice di concentrazione).

    Richiede la colonna 'organizzazioni_madri_terzi' (aggiunta in questa
    stessa passata a 05_scrape_dark_stack.py): un dark_stack.csv prodotto
    da una versione precedente dello script non ce l'ha, in quel caso la
    funzione ritorna None invece di fallire (stesso pattern già usato altrove
    in questo file per le colonne aggiunte in versioni successive).

    Unità di conteggio: PRESENZA per ente (un ente che carica due domini
    Google diversi conta una sola volta per 'Google', non due) — coerente
    con la domanda che l'HHI vuole rispondere ('quanti enti dipendono da
    questo operatore', non 'quante risorse di questo operatore ci sono in
    totale'), e con lo stesso principio già applicato a hosting/mail in
    summarize() (un ASN per ente, non un IP per ente)."""
    if "organizzazioni_madri_terzi" not in ds.columns:
        return None
    n = len(ds)
    org_counts = Counter()
    n_con_tracker = 0
    for celle in ds["organizzazioni_madri_terzi"].fillna(""):
        orgs = {o.strip() for o in str(celle).split(";") if o.strip()}
        if orgs:
            n_con_tracker += 1
        for o in orgs:
            org_counts[o] += 1

    copertura_pct = round(100 * n_con_tracker / n, 1) if n else 0
    valore_hhi = round(hhi(org_counts), 1)
    return {
        "dimensione_campione": n,
        "con_almeno_un_tracker_di_terze_parti": n_con_tracker,
        "copertura_pct": copertura_pct,
        "n_organizzazioni_distinte": len(org_counts),
        "hhi_organizzazioni_madri": valore_hhi,
        "hhi_categoria": hhi_categoria(valore_hhi),
        "hhi_affidabile": n_con_tracker >= SOGLIA_MINIMA_HHI,
        "hhi_nota": (
            None if n_con_tracker >= SOGLIA_MINIMA_HHI else
            f"Calcolato su solo {n_con_tracker} enti con almeno un tracker rilevato (soglia minima "
            f"consigliata: {SOGLIA_MINIMA_HHI}): il valore è aritmeticamente corretto ma può essere "
            "molto distante dalla concentrazione reale, va letto con cautela."
        ),
        "top10_organizzazioni": [
            {
                "organizzazione": o,
                "n_enti": v,
                "quota_pct": round(100 * v / n_con_tracker, 1) if n_con_tracker else 0,
            }
            for o, v in org_counts.most_common(10)
        ],
        "nota_metodologica": (
            "Concentrazione per organizzazione madre (non per singolo dominio/vendor), sullo stesso "
            "principio usato da Singh et al. (2026) per i siti governativi. 'organizzazione_madre' è "
            "assegnata solo quando la proprietà societaria è nota e non ambigua (vedi "
            "data/rules/dark_stack_rules.yaml): i domini senza una regola specifica contano come "
            "organizzazione a sé, quindi questo HHI è probabilmente una SOTTOSTIMA della vera "
            "concentrazione (più regole aggiunte alla community = stima più precisa nel tempo)."
        ),
    }

def summarize_cms_fingerprint(cf: pd.DataFrame):
    n = len(cf)
    con_software = int((pd.to_numeric(cf["n_software_rilevati"], errors="coerce").fillna(0) > 0).sum())
    tutti_software = Counter()
    for softs in cf["software_rilevato"].dropna():
        for s in str(softs).split(";"):
            if s:
                tutti_software[s] += 1
    licenze = Counter()
    for lic in cf["licenze_rilevate"].dropna():
        for l in str(lic).split(";"):
            if l:
                licenze[l] += 1
    n_tutti_open_source = int((cf["tutti_open_source"] == True).sum())
    return {
        "nota": (
            "Fingerprint da tracce esterne (HTML/header HTTP), non ispezione del codice installato: "
            "misura 'software in uso e licenza generale', non conformità legale agli obblighi della "
            "licenza. Un mancato riconoscimento non significa 'nessun software', vedi 06_fingerprint_cms.py."
        ),
        "dimensione_campione": n,
        "con_almeno_un_software_riconosciuto": con_software,
        "con_almeno_un_software_riconosciuto_pct": round(100 * con_software / n, 1) if n else 0,
        "con_tutto_il_software_rilevato_open_source": n_tutti_open_source,
        "con_tutto_il_software_rilevato_open_source_pct": round(100 * n_tutti_open_source / n, 1) if n else 0,
        "top15_software": [{"software": s, "n_siti": v} for s, v in tutti_software.most_common(15)],
        "distribuzione_licenze": [{"licenza": l, "n_siti": v} for l, v in licenze.most_common(15)],
    }


def summarize_resilienza(rf: pd.DataFrame):
    """Riepilogo di resilience.csv (07_resilience.py, opzionale). Non
    ricalcola nulla di HHI: solo medie/distribuzioni sul punteggio già
    calcolato da 07 per hostname."""
    n = len(rf)
    punteggi = pd.to_numeric(rf.get("punteggio_resilienza_combinato"), errors="coerce").dropna()
    incoerenze = pd.to_numeric(rf.get("ns_mx_incoerenti"), errors="coerce").fillna(0)
    return {
        "nota": (
            "Ridondanza EFFETTIVAMENTE OSSERVATA (ogni NS interrogato separatamente, ogni "
            "endpoint A/AAAA testato individualmente), non solo il singolo indirizzo/nameserver "
            "misurato da 02_probe_infra.py — vedi 07_resilience.py."
        ),
        "dimensione_campione": n,
        "con_punteggio_valido": int(len(punteggi)),
        "punteggio_medio": round(float(punteggi.mean()), 4) if len(punteggi) else None,
        "punteggio_mediano": round(float(punteggi.median()), 4) if len(punteggi) else None,
        "n_hostname_con_mx_incoerente_tra_ns": int((incoerenze > 0).sum()),
    }


def summarize_indice_dipendenza_tecnologica(report: dict, resilienza: dict | None):
    """Indice sintetico 0-100 di ESPOSIZIONE a concentrazione/dipendenza
    tecnologica osservata (NON una probabilità di compromissione, NON un
    giudizio di sicurezza puntuale su un singolo ente).

    Deliberatamente NON ricalcola nessun HHI da zero: combina solo numeri
    GIÀ presenti altrove in questo stesso report.json (hosting_dns,
    tracker_concentrazione_hhi, software_licenze), più resilience.csv se
    07_resilience.py è stato eseguito. Il motivo è evitare due fonti di
    verità per lo stesso numero (es. un HHI ricalcolato qui con una soglia
    di affidabilità diversa da quella di summarize() sopra) — un rischio
    concreto quando si integra codice esterno che non conosce
    SOGLIA_MINIMA_HHI/hhi_affidabile.

    Ogni dimensione entra nella media pesata SOLO se il dato sottostante è
    'hhi_affidabile' (o comunque presente per resilienza/software, che non
    sono HHI): una dimensione assente o inaffidabile viene esclusa dal
    denominatore, non trattata silenziosamente come 0 (= 'nessuna
    concentrazione'), che sarebbe un errore nella direzione peggiore
    (nasconderebbe l'incertezza facendo sembrare il campione più sano di
    quanto i dati permettano di dire)."""
    dimensioni = {}
    esclusioni = []

    hosting = report.get("hosting_dns")
    if hosting and hosting.get("hhi_affidabile"):
        dimensioni["concentrazione_hosting"] = min(1.0, hosting["hhi_operatori"] / 10000)
    elif hosting:
        esclusioni.append("concentrazione_hosting (hhi_affidabile=false, campione troppo piccolo)")

    tracker = report.get("tracker_concentrazione_hhi")
    if tracker and tracker.get("hhi_affidabile"):
        dimensioni["concentrazione_tracker"] = min(1.0, tracker["hhi_organizzazioni_madri"] / 10000)
    elif tracker:
        esclusioni.append("concentrazione_tracker (hhi_affidabile=false, campione troppo piccolo)")

    software = report.get("software_licenze")
    if software and software.get("dimensione_campione", 0) > 0:
        pct_tutto_oss = software.get("con_tutto_il_software_rilevato_open_source_pct")
        if pct_tutto_oss is not None:
            dimensioni["esposizione_software_proprietario"] = max(0.0, min(1.0, 1 - pct_tutto_oss / 100))

    if resilienza and resilienza.get("con_punteggio_valido", 0) > 0 and resilienza.get("punteggio_medio") is not None:
        dimensioni["esposizione_bassa_resilienza"] = max(0.0, min(1.0, 1 - resilienza["punteggio_medio"]))
    elif resilienza is not None:
        esclusioni.append("esposizione_bassa_resilienza (resilience.csv presente ma senza punteggi validi)")

    pesi = {
        "concentrazione_hosting": 0.35,
        "concentrazione_tracker": 0.20,
        "esposizione_software_proprietario": 0.15,
        "esposizione_bassa_resilienza": 0.30,
    }
    presenti = [(k, pesi[k]) for k in dimensioni if k in pesi]
    peso_totale = sum(w for _, w in presenti)
    if not peso_totale:
        return {
            "nota": "Nessuna dimensione sottostante disponibile/affidabile in questo run: indice non calcolato.",
            "dimensioni_escluse": esclusioni,
        }
    punteggio = round(100 * sum(dimensioni[k] * w for k, w in presenti) / peso_totale, 1)
    banda = "basso" if punteggio < 25 else "moderato" if punteggio < 50 else "alto" if punteggio < 75 else "molto alto"
    return {
        "nota": (
            "Indice composito di ESPOSIZIONE osservata a concentrazione/dipendenza tecnologica, "
            "0-100: valore alto = più dimensioni concentrate su pochi operatori/fornitori/versioni "
            "e/o ridondanza dichiarata ma non effettivamente osservabile. NON è una probabilità di "
            "incidente né un giudizio di sicurezza su un singolo ente — combina solo dati già "
            "presenti nel resto di questo report, ripesati sul sottoinsieme di dimensioni "
            "effettivamente disponibili e affidabili in questo run (vedi 'dimensioni_incluse'/"
            "'dimensioni_escluse')."
        ),
        "indice_dipendenza_tecnologica": punteggio,
        "banda": banda,
        "dimensioni_incluse": {k: round(dimensioni[k] * 100, 1) for k, _ in presenti},
        "peso_ripesato_su": round(peso_totale, 2),
        "dimensioni_escluse": esclusioni,
    }


def summarize_tipo_ente(tf: pd.DataFrame, siti_path: str):
    """Riepilogo di tipo_ente.csv (08_classifica_tipo_ente.py, opzionale):
    distribuzione delle categorie rilevate, la misura aggiuntiva
    tempo/peso pagina completa (diversa da tempi_risposta_http, che si
    ferma agli header — vedi docstring di 08), il conteggio dei segnali di
    possibile ente non pubblico, e — solo dove 'sector' è disponibile
    (oggi solo IT, viene da IndicePA) — l'accordo fra le due fonti come
    controllo di qualità sul classificatore stesso."""
    n = len(tf)
    distribuzione = Counter(tf["tipo_ente_rilevato"].fillna("non_classificato"))
    pct_classificato = round(100 * (n - distribuzione.get("non_classificato", 0)) / n, 1) if n else 0.0

    tempi = pd.to_numeric(tf.get("tempo_download_pagina_ms"), errors="coerce").dropna()
    pesi = pd.to_numeric(tf.get("peso_pagina_kb"), errors="coerce").dropna()
    non_pubblico = tf.get("probabile_non_pubblico", pd.Series(dtype=object)).astype(str).str.lower() == "true"

    out = {
        "nota": (
            "Classificazione a parole chiave sul testo della homepage (24 lingue UE), non un "
            "modello linguistico — vedi limite metodologico in 08_classifica_tipo_ente.py."
        ),
        "dimensione_campione": n,
        "distribuzione_categorie": dict(distribuzione.most_common()),
        "pct_classificato": pct_classificato,
        "tempo_download_pagina_completa": {
            "nota": "Tempo di download del CORPO COMPLETO della homepage (fino al limite --max-bytes), diverso da tempi_risposta_http sopra (che misura solo fino agli header, corpo non scaricato — vedi 02_probe_infra.py).",
            "n_enti_con_dato": int(len(tempi)),
            "media_ms": round(float(tempi.mean()), 1) if len(tempi) else None,
            "mediana_ms": round(float(tempi.median()), 1) if len(tempi) else None,
            "p95_ms": round(float(tempi.quantile(0.95)), 1) if len(tempi) else None,
        },
        "peso_pagina": {
            "n_enti_con_dato": int(len(pesi)),
            "media_kb": round(float(pesi.mean()), 1) if len(pesi) else None,
            "mediana_kb": round(float(pesi.median()), 1) if len(pesi) else None,
            "p95_kb": round(float(pesi.quantile(0.95)), 1) if len(pesi) else None,
        },
        "possibile_ente_non_pubblico": {
            "nota": (
                "CANDIDATI da rivedere a mano (forma societaria privata e/o terminologia "
                "e-commerce nella homepage), non un'esclusione automatica: una società "
                "partecipata pubblica può legittimamente avere una forma societaria privata. "
                "Elenco completo nel file *_possibili_non_pubblici.csv accanto a tipo_ente.csv."
            ),
            "n_hostname_segnalati": int(non_pubblico.sum()),
            "pct_del_campione": round(100 * non_pubblico.sum() / n, 1) if n else 0.0,
        },
    }

    if Path(siti_path).exists():
        siti = pd.read_csv(siti_path, dtype={"codice_ipa": str})
        if "sector" in siti.columns and "codice_ipa" in tf.columns:
            confronto = tf.merge(siti[["codice_ipa", "sector"]], on="codice_ipa", how="left")
            confronto = confronto[confronto["sector"].notna() & (confronto["sector"].astype(str).str.strip() != "")]
            if len(confronto):
                # 'altro'/'ministero' in sector non hanno una corrispondenza 1:1 pulita con le
                # categorie di questo classificatore (sector='altro' è un contenitore residuo di
                # IndicePA, non una categoria testuale riconoscibile): l'accordo viene calcolato
                # solo sulle categorie condivise dal vocabolario (comune/provincia/regione/scuola/
                # universita/sanita), le altre righe di 'sector' vengono escluse dal denominatore
                # invece di essere contate come disaccordo — sarebbe un confronto sleale fra un
                # classificatore testuale e una categoria residua che non gli corrisponde.
                categorie_comparabili = {"comune", "provincia", "regione", "scuola", "universita", "sanita"}
                comparabile = confronto[confronto["sector"].isin(categorie_comparabili)]
                if len(comparabile):
                    accordo = (comparabile["tipo_ente_rilevato"] == comparabile["sector"]).mean()
                    out["accordo_con_sector"] = {
                        "nota": (
                            "Confronto solo sulle categorie con vocabolario condiviso fra le due fonti "
                            "(comune/provincia/regione/scuola/universita/sanita): 'sector' con altri "
                            "valori (es. 'altro', 'ministero', 'camera_commercio') è escluso dal "
                            "denominatore, non contato come disaccordo."
                        ),
                        "n_righe_comparabili": int(len(comparabile)),
                        "pct_accordo": round(100 * accordo, 1),
                    }
    return out


def genera_sintesi_markdown(report: dict, paese: str) -> str:
    """Riepilogo in linguaggio naturale dei numeri principali del report,
    pensato per chi non vuole/deve leggere il JSON. Frasi già scritte in
    italiano, riempite con i numeri: non è un modello linguistico, sono
    template a slot — riproducibile e verificabile riga per riga."""
    righe = [f"# Sintesi — osservatorio infrastruttura pubblica ({paese})", ""]
    righe.append(f"Generato il {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
                  f"su {report.get('n_enti_analizzati', 0)} enti misurati.")
    righe.append("")

    hd = report.get("hosting_dns")
    if hd:
        righe.append(
            f"**Hosting web**: {hd['n_asn_distinti']} operatori distinti, indice di concentrazione "
            f"HHI {hd['hhi_operatori']} ({hd['hhi_categoria']}"
            + ("" if hd["hhi_affidabile"] else ", ma calcolato su un campione di dati troppo piccolo per essere affidabile")
            + f"). Copertura del dato: {hd['copertura_pct']}% ({hd['affidabilita_copertura']})."
        )
        if hd["top10_operatori"]:
            primo = hd["top10_operatori"][0]
            righe.append(f"Il primo operatore per numero di enti è **{primo['organizzazione']}** con il {primo['quota_pct']}%.")
        righe.append("")

    hm = report.get("hosting_mail")
    if hm:
        righe.append(
            f"**Posta elettronica**: HHI {hm['hhi_operatori']} ({hm['hhi_categoria']}), "
            f"copertura {hm['copertura_pct']}%."
        )
        righe.append("")

    if "dmarc_completo" in report:
        dm = report["dmarc_completo"]
        blocco_pct = dm["distribuzione_policy_pct"]["reject"] + dm["distribuzione_policy_pct"]["quarantine"]
        righe.append(
            f"**DMARC**: presente sul {dm['dmarc_presente_pct']}% degli enti, ma con policy che "
            f"blocca davvero lo spoofing (reject/quarantine) solo su {blocco_pct:.1f}% "
            f"— il {dm['distribuzione_policy_pct']['none']}% ha DMARC solo in modalità osservazione (policy 'none')."
        )
        righe.append("")

    if "dnssec_attivo_pct" in report:
        righe.append(f"**DNSSEC** attivo sul {report['dnssec_attivo_pct']}% del campione.")
        righe.append("")

    if "senza_ridondanza_ns" in report:
        sr = report["senza_ridondanza_ns"]
        righe.append(f"**Ridondanza DNS**: {sr['pct']}% degli enti ha un solo operatore di nameserver (nessun backup).")
        righe.append("")

    if "errori_misurazione" in report:
        em = report["errori_misurazione"]
        righe.append(
            f"**Qualità del campione**: {em['pct_enti_con_almeno_un_errore']}% delle righe ha almeno un "
            "errore di misurazione — da tenere presente leggendo le percentuali sopra."
        )
        righe.append("")

    if "certificati_tls" in report:
        ct = report["certificati_tls"]
        righe.append(
            f"**Certificati TLS**: {ct['certificati_scaduti']['n_enti']} già scaduti, "
            f"{ct['certificati_in_scadenza_entro_30_giorni']['n_enti']} in scadenza entro 30 giorni "
            "— questi due gruppi meritano un controllo prioritario."
        )
        righe.append("")

    if "giurisdizione_holding_aggregata" in report:
        gg = report["giurisdizione_holding_aggregata"]
        if gg["distribuzione"]:
            top = gg["distribuzione"][0]
            righe.append(
                f"**Giurisdizione hosting**: la macro-area più comune è **{top['macro_area']}** "
                f"con il {top['pct_su_campione']}% del campione."
            )
            righe.append("")

    if "tracker_concentrazione_hhi" in report:
        th = report["tracker_concentrazione_hhi"]
        righe.append(
            f"**Concentrazione tracker/terze parti**: HHI {th['hhi_organizzazioni_madri']} "
            f"({th['hhi_categoria']}"
            + ("" if th["hhi_affidabile"] else ", ma calcolato su un campione di dati troppo piccolo per essere affidabile")
            + f") su {th['n_organizzazioni_distinte']} organizzazioni madri distinte "
            f"({th['con_almeno_un_tracker_di_terze_parti']} enti con almeno un tracker rilevato)."
        )
        if th["top10_organizzazioni"]:
            primo = th["top10_organizzazioni"][0]
            righe.append(f"La prima organizzazione per numero di enti è **{primo['organizzazione']}** con il {primo['quota_pct']}%.")
        righe.append("")

    if "software_licenze" in report:
        sl = report["software_licenze"]
        righe.append(
            f"**Software/licenze**: software riconosciuto su {sl['con_almeno_un_software_riconosciuto_pct']}% "
            f"degli enti, di cui {sl['con_tutto_il_software_rilevato_open_source_pct']}% interamente open source "
            "(fra quelli con software riconosciuto)."
        )
        righe.append("")

    righe.append("---")
    righe.append("*Generato automaticamente da 03_analyze.py. Per il dettaglio completo vedi report.json; "
                  "per gli enti da controllare per primi vedi enti_a_rischio.csv.*")
    return "\n".join(righe)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paese", default="IT",
        help="Codice paese ISO a due lettere: determina i percorsi di default sotto "
             "data/results/<paese> e data/processed/<paese> (default: IT).",
    )
    ap.add_argument("--in", dest="infile", default=None, help="CSV di input (default: data/results/<paese>/risultati.csv)")
    ap.add_argument("--siti", default=None, help="Output fase 1, per il breakdown per settore (default: data/processed/<paese>/siti.csv)")
    ap.add_argument("--dark-stack", default=None, help="Output 05_scrape_dark_stack.py (default: data/results/<paese>/dark_stack.csv)")
    ap.add_argument("--cms-fingerprint", default=None, help="Output 06_fingerprint_cms.py (default: data/results/<paese>/cms_fingerprint.csv)")
    ap.add_argument("--resilience", default=None, help="Output 07_resilience.py, opzionale (default: data/results/<paese>/<run-id>/resilience.csv)")
    ap.add_argument("--tipo-ente", default=None, help="Output 08_classifica_tipo_ente.py, opzionale (default: data/results/<paese>/<run-id>/tipo_ente.csv)")
    ap.add_argument("--config", default=None, help="Percorso di config.yaml (default: config.yaml nella project root) — usato solo per il flag 'misurazioni.tracker_hhi' (vedi sotto).")
    ap.add_argument("--out", default=None, help="JSON di output (default: data/results/<paese>/<run-id>/report.json)")
    ap.add_argument(
        "--run-id", default=None,
        help="Vedi 02_probe_infra.py --help. Default se assente: l'ultimo run noto da "
             "data/results/<paese>/latest_run.json, altrimenti la data UTC odierna — 03_analyze.py è "
             "tipicamente un CONSUMER di dati già raccolti, quindi 'l'ultimo run completato' è un "
             "default più sensato di 'oggi' quando i due non coincidono (es. rianalisi senza rilanciare "
             "la sonda lo stesso giorno).",
    )
    ap.add_argument(
        "--salva-storico", action="store_true",
        help="Oltre a report.json, salva anche una copia datata in data/results/<paese>/<run-id>/storico/ "
             "per poter confrontare run diverse nel tempo (non attivo di default per non accumulare "
             "file su ogni giro di prova). NOTA (seconda passata): da quando ogni run ha già la propria "
             "cartella <run-id>, questo flag è per lo più ridondante per il confronto FRA run (che ora "
             "si ottiene semplicemente guardando cartelle <run-id> diverse) — resta utile solo per "
             "confrontare più analisi fatte nello STESSO giorno/run.",
    )
    args = ap.parse_args()

    paese = args.paese.strip().upper()
    cfg = cfgmod.load_config(args.config)
    run_id = args.run_id or cfgmod.leggi_puntatore_latest(paese) or cfgmod.run_id_default()
    infile = args.infile or str(dati_results(paese, run_id) / "risultati.csv")
    siti_path = args.siti or str(dati_processed(paese) / "siti.csv")
    dark_stack_path = args.dark_stack or str(dati_results(paese, run_id) / "dark_stack.csv")
    cms_fingerprint_path = args.cms_fingerprint or str(dati_results(paese, run_id) / "cms_fingerprint.csv")
    resilience_path = args.resilience or str(dati_results(paese, run_id) / "resilience.csv")
    tipo_ente_path = args.tipo_ente or str(dati_results(paese, run_id) / "tipo_ente.csv")
    out_arg = args.out or str(dati_results(paese, run_id) / "report.json")

    if not Path(infile).exists():
        print(
            f"ERRORE: non trovo {infile}. Esegui prima 02_probe_infra.py, "
            "oppure passa --in percorso\\al\\tuo\\risultati.csv",
            file=sys.stderr,
        )
        sys.exit(1)

    df = pd.read_csv(infile, dtype={c: str for c in COLONNE_ASN_TESTO})
    n = len(df)
    if n == 0:
        print("ERRORE: file di risultati vuoto", file=sys.stderr)
        sys.exit(1)

    report = {
        "paese": paese,
        "run_id": run_id,
        "metodo_operatore": "asn",  # concentrazione raggruppata per ASN, non per stringa RDAP (vedi docstring)
        "n_enti_analizzati": n,
        "hosting_dns": summarize(df, "asn_org_a", "asn_a"),
        "hosting_mail": summarize(df, "asn_org_mx", "asn_mx"),
        "dnssec_attivo_pct": round(100 * (df["dnssec_ds_presente"] == True).sum() / n, 1),
        "spf_presente_pct": round(100 * (df["spf_presente"] == True).sum() / n, 1),
        "dmarc_presente_pct": round(100 * (df["dmarc_presente"] == True).sum() / n, 1),
        "dmarc_policy_reject_o_quarantine_pct": round(
            100 * df["dmarc_policy"].isin(["reject", "quarantine"]).sum() / n, 1
        ),
        "dmarc_completo": summarize_dmarc(df),
        "senza_ridondanza_ns": {
            "n_enti_con_un_solo_ns_secondlevel": int((df["ns_diversi_secondlevel"] <= 1).sum()),
            "pct": round(100 * (df["ns_diversi_secondlevel"] <= 1).sum() / n, 1),
        },
    }

    errori_summ = summarize_errori(df)
    if errori_summ:
        report["errori_misurazione"] = errori_summ

    # Vista deduplicata per hostname (hostname condivisi da più enti contano
    # una sola volta): accanto alla vista per-ente, non al posto di essa.
    dedup_dns = summarize_deduplicato(df, "asn_org_a", "asn_a")
    if dedup_dns:
        report["hosting_dns_per_hostname_unico"] = dedup_dns
    dedup_mail = summarize_deduplicato(df, "asn_org_mx", "asn_mx")
    if dedup_mail:
        report["hosting_mail_per_hostname_unico"] = dedup_mail

    # 'asn_ns'/'cdn_rilevato'/'giurisdizione_holding' esistono solo con la
    # versione di 02_probe_infra.py aggiornata: se il risultati.csv in input
    # è di una run precedente, saltiamo queste sezioni invece di andare in
    # errore (l'utente può comunque leggere il resto del report).
    if "asn_ns" in df.columns and "asn_org_ns" in df.columns:
        report["hosting_nameserver"] = summarize(df, "asn_org_ns", "asn_ns")
        dedup_ns = summarize_deduplicato(df, "asn_org_ns", "asn_ns")
        if dedup_ns:
            report["hosting_nameserver_per_hostname_unico"] = dedup_ns

    if "cdn_rilevato" in df.columns:
        con_cdn = df["cdn_rilevato"].fillna("").astype(str).str.strip() != ""
        cdn_counter = Counter()
        for v in df.loc[con_cdn, "cdn_rilevato"]:
            for c in str(v).split(";"):
                if c:
                    cdn_counter[c] += 1
        report["cdn_proxy_rilevati"] = {
            "n_enti_con_cdn_rilevato": int(con_cdn.sum()),
            "pct": round(100 * con_cdn.sum() / n, 1),
            "distribuzione": [{"cdn": c, "n_enti": v} for c, v in cdn_counter.most_common(10)],
        }

    if "giurisdizione_holding" in df.columns:
        giur_counter = Counter(g for g in df["giurisdizione_holding"] if isinstance(g, str) and g.strip())
        report["giurisdizione_holding_hosting"] = {
            "nota": "Euristica su nome operatore RDAP, non un dato giuridico verificato (vedi 02_probe_infra.py)",
            "distribuzione": [{"giurisdizione": g, "n_enti": v} for g, v in giur_counter.most_common(15)],
        }
        aggregata = summarize_giurisdizione_aggregata(df)
        if aggregata:
            report["giurisdizione_holding_aggregata"] = aggregata

    # --- CDN extra-UE/SEE: incrocio esplicito fra cdn_rilevato e il Paese
    # ASN dell'IP web (asn_country_a, dato RDAP, non l'euristica sul nome
    # della holding: qui vogliamo un criterio geografico verificabile, non
    # "chi controlla l'azienda"). SEE = Spazio Economico Europeo (UE27 +
    # Islanda, Liechtenstein, Norvegia), stessa definizione usata in
    # 05_scrape_dark_stack.py per i domini di terze parti.
    if "cdn_rilevato" in df.columns and "asn_country_a" in df.columns:
        con_cdn = df["cdn_rilevato"].fillna("").astype(str).str.strip() != ""
        fuori_see = ~df["asn_country_a"].fillna("").astype(str).isin(SEE_COUNTRIES)
        con_dato_paese = df["asn_country_a"].fillna("").astype(str).str.strip() != ""
        con_cdn_extra_see = con_cdn & fuori_see & con_dato_paese
        report["cdn_extra_ue"] = {
            "nota": (
                "'Extra-UE' qui = IP del sito (record A) geolocalizzato via RDAP in un Paese fuori "
                "dallo Spazio Economico Europeo, incrociato con la presenza di una CDN nota rilevata "
                "dagli header HTTP. È un criterio geografico sull'IP, non sulla nazionalità della "
                "società che gestisce la CDN (vedi 'giurisdizione_holding_hosting' sopra per quello)."
            ),
            "n_enti_con_cdn_e_ip_extra_see": int(con_cdn_extra_see.sum()),
            "pct_su_enti_con_cdn": round(100 * con_cdn_extra_see.sum() / con_cdn.sum(), 1) if con_cdn.sum() else 0,
            "pct_su_campione_totale": round(100 * con_cdn_extra_see.sum() / n, 1),
        }

    # --- Tempi di risposta HTTP, se misurati (tempi_risposta=true) ---
    if "http_response_time_ms" in df.columns:
        tempi = pd.to_numeric(df["http_response_time_ms"], errors="coerce").dropna()
        if len(tempi):
            report["tempi_risposta_http"] = {
                "n_enti_con_dato": int(len(tempi)),
                "pct_su_campione": round(100 * len(tempi) / n, 1),
                "media_ms": round(tempi.mean(), 1),
                "mediana_ms": round(tempi.median(), 1),
                "p95_ms": round(tempi.quantile(0.95), 1),
                "max_ms": round(float(tempi.max()), 1),
            }

    # --- Robustezza TLS avanzata, se misurata (tls_avanzato=true) ---
    if "tls_grade" in df.columns:
        grade_counter = Counter(g for g in df["tls_grade"] if isinstance(g, str) and g.strip())
        n_con_grade = sum(grade_counter.values())
        deboli = df["tls_protocolli_deboli_attivi"].fillna("").astype(str).str.strip() != ""
        hsts = (df["tls_hsts_presente"] == True)
        report["tls_robustezza"] = {
            "n_enti_con_dato": n_con_grade,
            "distribuzione_grade": {g: grade_counter.get(g, 0) for g in ["A", "B", "C", "D"]},
            "n_enti_con_protocolli_deboli_attivi": int(deboli.sum()),
            "pct_con_protocolli_deboli_attivi": round(100 * deboli.sum() / n, 1),
            "n_enti_con_hsts": int(hsts.sum()),
            "pct_con_hsts": round(100 * hsts.sum() / n, 1),
        }

    # --- Certificati TLS: distribuzione + scaduti/in scadenza ---
    cert_summ = summarize_certificati(df)
    if cert_summ:
        report["certificati_tls"] = cert_summ

    # --- Distribuzione codici di stato HTTP ---
    http_summ = summarize_http_status(df)
    if http_summ:
        report["http_status"] = http_summ

    # --- Tipo di ente dal testo homepage (08_classifica_tipo_ente.py), se disponibile ---
    # NOTA POSIZIONE: va qui, PRIMA del breakdown per_settore/per_tipo_ente
    # subito sotto (che usa tf_tipo_ente) — non insieme a resilienza/
    # software_licenze più in basso, dove concettualmente starebbe in mezzo
    # alle altre misurazioni opzionali ma userebbe tf_tipo_ente prima che
    # sia definita.
    tf_tipo_ente = None
    if Path(tipo_ente_path).exists():
        tf_tipo_ente = pd.read_csv(tipo_ente_path, dtype={"codice_ipa": str})
        if len(tf_tipo_ente):
            report["tipo_ente"] = summarize_tipo_ente(tf_tipo_ente, siti_path)

    # --- Breakdown per settore (richiede sector da siti.csv) ---
    if Path(siti_path).exists():
        siti = pd.read_csv(siti_path, dtype={"codice_ipa": str})
        if "sector" in siti.columns and "codice_ipa" in df.columns:
            df_sect = df.merge(siti[["codice_ipa", "sector"]], on="codice_ipa", how="left")
            per_settore = {}
            for settore, gruppo in df_sect.groupby("sector"):
                sintesi = summarize(gruppo, "asn_org_a", "asn_a")
                if sintesi["con_dato_valido"] < SOGLIA_MINIMA_HHI:
                    continue  # campione con troppo pochi dati validi: l'HHI non sarebbe leggibile
                per_settore[settore] = {"n_enti": len(gruppo), "hosting_dns": sintesi}
            report["per_settore"] = per_settore

    # --- Breakdown per tipo di ente rilevato dal testo (08, opzionale) ---
    # Stesso schema di per_settore sopra, ma sulla categoria TESTUALE
    # (disponibile per qualunque Paese, non solo IT) invece del 'sector'
    # ufficiale IPA. Include anche la nuova misura tempo/peso pagina per
    # categoria: risponde a "le scuole hanno homepage più pesanti dei
    # comuni?" ecc., una domanda che per_settore da solo non può porre
    # perché non ha tempo_download_pagina_ms/peso_pagina_kb.
    if tf_tipo_ente is not None and "codice_ipa" in df.columns:
        df_tipo = df.merge(
            tf_tipo_ente[["codice_ipa", "tipo_ente_rilevato", "tempo_download_pagina_ms", "peso_pagina_kb"]],
            on="codice_ipa", how="inner",
        )
        per_tipo_ente = {}
        for categoria, gruppo in df_tipo.groupby("tipo_ente_rilevato"):
            sintesi_hosting = summarize(gruppo, "asn_org_a", "asn_a")
            tempi_cat = pd.to_numeric(gruppo["tempo_download_pagina_ms"], errors="coerce").dropna()
            pesi_cat = pd.to_numeric(gruppo["peso_pagina_kb"], errors="coerce").dropna()
            voce = {"n_enti": len(gruppo)}
            if sintesi_hosting["con_dato_valido"] >= SOGLIA_MINIMA_HHI:
                voce["hosting_dns"] = sintesi_hosting
            if len(tempi_cat):
                voce["tempo_download_pagina_mediana_ms"] = round(float(tempi_cat.median()), 1)
            if len(pesi_cat):
                voce["peso_pagina_mediana_kb"] = round(float(pesi_cat.median()), 1)
            per_tipo_ente[categoria] = voce
        report["per_tipo_ente"] = per_tipo_ente

    # --- Breakdown per regione (già disponibile direttamente in risultati.csv) ---
    if "regione" in df.columns:
        per_regione = {}
        for regione, gruppo in df.groupby("regione"):
            if not isinstance(regione, str) or not regione.strip():
                continue
            sintesi = summarize(gruppo, "asn_org_a", "asn_a")
            if sintesi["con_dato_valido"] < SOGLIA_MINIMA_HHI:
                continue
            per_regione[regione] = {"n_enti": len(gruppo), "hosting_dns": sintesi}
        report["per_regione"] = per_regione

    # --- Riepilogo Dark Stack (idea 1) + cookie, se disponibile ---
    if Path(dark_stack_path).exists():
        ds = pd.read_csv(dark_stack_path)
        if len(ds):
            report["dark_stack"] = summarize_dark_stack(ds)
            if cfgmod.flag(cfg, "tracker_hhi"):
                tracker_hhi = summarize_tracker_hhi(ds)
                if tracker_hhi:
                    report["tracker_concentrazione_hhi"] = tracker_hhi

    # --- Rimando al dettaglio terze parti per la verifica manuale (quarta
    # passata, aggiunta su richiesta, settembre 2026) ---
    # Il contenuto pesante (elenco pagine, URL esatte) resta VOLUTAMENTE nei
    # file separati scritti da 05_scrape_dark_stack.py (preferenza esplicita:
    # file separato invece che dentro report.json) — qui solo un rimando +
    # i domini più rari (di solito i più interessanti da controllare per
    # primi), per chi legge report.json a sapere che il dettaglio esiste
    # senza doverlo cercare.
    verifica_path = dati_results(paese, run_id) / "terze_parti_da_verificare.csv"
    if verifica_path.exists():
        tv = pd.read_csv(verifica_path)
        if len(tv):
            report["terze_parti_da_verificare"] = {
                "nota": (
                    "Dettaglio completo (URL esatta di ogni prima comparsa, elenco di tutte le "
                    "pagine) nei file separati terze_parti_da_verificare.csv/.md accanto a questo "
                    "report.json — comparire nell'elenco non significa che un dominio sia malevolo, "
                    "vedi nota in testa al file .md."
                ),
                "file_dettaglio_csv": "terze_parti_da_verificare.csv",
                "file_leggibile_md": "terze_parti_da_verificare.md",
                "n_domini_terzi_distinti": len(tv),
                "domini_piu_rari_da_controllare_per_primi": (
                    tv.nsmallest(5, "n_hostname_distinti")[["terza_parte_host", "n_hostname_distinti", "primo_url_risorsa_trovata"]]
                    .to_dict("records")
                ),
            }

    # --- Censimento software/licenze (06_fingerprint_cms.py), se disponibile ---
    if Path(cms_fingerprint_path).exists():
        cf = pd.read_csv(cms_fingerprint_path, dtype={"codice_ipa": str})
        if len(cf):
            report["software_licenze"] = summarize_cms_fingerprint(cf)

    # --- Ridondanza NS/endpoint (07_resilience.py), se disponibile e attivata ---
    resilienza_summ = None
    if Path(resilience_path).exists():
        rf = pd.read_csv(resilience_path, dtype={"codice_ipa": str})
        if len(rf):
            resilienza_summ = summarize_resilienza(rf)
            report["resilienza_dinamica"] = resilienza_summ

    # --- Indice sintetico di dipendenza tecnologica (terza passata, settembre 2026) ---
    # Va DOPO tutte le sezioni sopra: riusa solo numeri già scritti in 'report'.
    if cfgmod.flag(cfg, "indice_dipendenza_tecnologica"):
        report["indice_dipendenza_tecnologica"] = summarize_indice_dipendenza_tecnologica(report, resilienza_summ)

    Path(out_arg).parent.mkdir(parents=True, exist_ok=True)
    cfgmod.backup_se_esiste(Path(out_arg))  # non perdere il report.json precedente se lo stesso run_id viene rianalizzato
    with open(out_arg, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    if args.salva_storico:
        storico_dir = Path(out_arg).parent / "storico"
        storico_dir.mkdir(parents=True, exist_ok=True)
        marca_tempo = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        copia = storico_dir / f"report_{marca_tempo}.json"
        shutil.copyfile(out_arg, copia)
        print(f"[storico] copia salvata in {copia}", file=sys.stderr)

    # --- Lista enti a rischio (CSV separato, ordinato dal più a rischio) ---
    identificativi = [c for c in ["codice_ipa", "hostname", "comune", "regione"] if c in df.columns]
    rischio = calcola_rischio(df)
    df_rischio = pd.concat([df[identificativi], rischio], axis=1)
    df_rischio = df_rischio.sort_values("punteggio_rischio", ascending=False)
    rischio_path = Path(out_arg).parent / "enti_a_rischio.csv"
    cfgmod.backup_se_esiste(rischio_path)
    df_rischio.to_csv(rischio_path, index=False, encoding="utf-8")

    # --- Sintesi in markdown ---
    sintesi_path = Path(out_arg).parent / "report_sintesi.md"
    cfgmod.backup_se_esiste(sintesi_path)
    with open(sintesi_path, "w", encoding="utf-8") as f:
        f.write(genera_sintesi_markdown(report, paese))

    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport scritto in {out_arg}", file=sys.stderr)
    print(f"Lista enti a rischio scritta in {rischio_path}", file=sys.stderr)
    print(f"Sintesi leggibile scritta in {sintesi_path}", file=sys.stderr)
    cfgmod.aggiorna_puntatore_latest(paese, run_id)


if __name__ == "__main__":
    main()
