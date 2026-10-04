#!/usr/bin/env python3
# EN: Post-process crawler checkpoints (no new requests): general report, every third-party
# occurrence with its HTML reference type, and the link_sospetti shortlist (suspicious links)
# for human review, enriched with Google Safe Browsing/VirusTotal verdicts and a persistent
# human-confirmed-threat flag.
"""
tools/postprocess_checkpoints.py

Post-processing "gratis" sui file già prodotti dal crawler (nessuna nuova
richiesta HTTP, nessun nuovo crawl): legge, per ogni paese, i checkpoint in
data/input/{ISO}/crawl_checkpoint.json (e crawl_progress.json se presente)
e produce report leggibili, senza toccare in alcun modo il crawl stesso.

Cosa produce, in data/output/_postprocess/ (percorso configurabile):

  - report_generale.md / report_generale.json
        Statistiche aggregate: pagine visitate, domini scoperti (per scope
        seed/interno/esterno), errori, domini finiti in "cooldown"
        (alternanza anti-blocco), per paese e in totale.

  - tutte_le_terze_parti.csv
        UNA RIGA PER OCCORRENZA: ogni volta che un dominio esterno compare
        in una pagina, con il TIPO di riferimento HTML esatto:
          link          <a href="...">      semplice link (Caso A)
          script        <script src="...">  dipendenza ESEGUIBILE (Caso B)
          iframe        <iframe src="...">  dipendenza EMBEDDED (Caso C)
          img           <img src="...">     dipendenza di CONTENUTO (Caso D)
          link_risorsa  <link href="...">   CSS/font/preconnect/...
        Richiede che il crawler sia stato eseguito con la patch che scrive
        'resource_index' nel checkpoint (vedi SiteCrawlFetcher in
        src/fetchers/generic.py) — se un checkpoint non lo contiene ancora
        (run fatta con una versione precedente del tool), viene saltato con
        un avviso, il resto del post-processing prosegue comunque.

  - link_sospetti.json / .csv / .md
        Shortlist pensata per la revisione umana: un rigo per dominio
        esterno "raro" (visto su poche pagine distinte, sotto la soglia
        'soglia_pagine_rarita' di config_postprocess.yaml) o con punteggio
        euristico alto (TLD insolito, parola chiave sospetta, IP letterale,
        punycode, dipendenza eseguibile/embedded). Il punteggio è SOLO un
        ordine di lettura, NON un verdetto — la classificazione finale
        spetta sempre a un revisore umano.

        Schema di ciascuna voce (il formato richiesto, con l'aggiunta di
        qualche campo utile alla revisione):
          link_sospetto: "esempio-dominio.tld"
          pagine_in_cui_e_presente: ["paese: url", ...]
          tipi_riferimento: ["script", "link", ...]
          punteggio_euristico: 5
          motivi_euristica: ["dipendenza eseguibile (<script>)", ...]
          minaccia_confermata_da_umano: false   <- persistente, vedi sotto

        PERSISTENZA: 'minaccia_confermata_da_umano' e l'eventuale campo
        'note' NON vengono MAI sovrascritti a un run successivo per un
        dominio già presente nel file JSON esistente — si limita ad
        aggiungere i domini nuovi (default false) e ad aggiornare
        pagine/tipi per quelli già noti. Basta editare a mano
        link_sospetti.json (o il CSV, che viene riletto allo stesso modo)
        per marcare 'minaccia_confermata_da_umano: true' dopo la verifica:
        il valore resta al run successivo.

Uso:
    python tools/postprocess_checkpoints.py
    python tools/postprocess_checkpoints.py --config config_postprocess.yaml
    python tools/postprocess_checkpoints.py --country BE --country DE

Nessuna dipendenza oltre a quelle già in requirements.txt (PyYAML).
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml

try:
    from . import threat_check
except ImportError:  # eseguito come script standalone (non come pacchetto)
    import threat_check  # type: ignore[no-redef]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)

# Carica automaticamente le variabili da un file .env nella cartella
# principale del progetto (es. GOOGLE_SAFE_BROWSING_API_KEY,
# VIRUSTOTAL_API_KEY), se presente e se python-dotenv è installato.
# Del tutto opzionale: senza .env o senza python-dotenv lo script funziona
# comunque, semplicemente leggendo le variabili già nell'ambiente (o, se
# assenti, il controllo minacce resta disattivato su quel fronte).
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

DEFAULT_CONFIG: dict[str, Any] = {
    "input": {"data_input_dir": "data/input", "countries": []},
    "output": {"output_dir": "data/output/_postprocess"},
    "report_generale": {
        "includi_domini_in_pausa": True,
        "includi_errori_dettaglio": True,
        "max_errori_mostrati_per_paese": 30,
    },
    "link_sospetti": {
        "abilitato": True,
        "soglia_pagine_rarita": 5,
        "peso_tipo_riferimento": {
            "script": 3, "iframe": 3, "link_risorsa": 1, "img": 1, "link": 1,
        },
        "peso_no_https": 1,
        "peso_ip_letterale": 2,
        "peso_punycode": 2,
        "peso_tld_sospetto": 2,
        "peso_parola_chiave_sospetta": 2,
        "peso_rarita_singola_pagina": 2,
        "tld_sospetti": [
            "zip", "mov", "top", "xyz", "click", "gq", "tk", "ml", "cf",
            "work", "loan", "win", "rest", "surf",
        ],
        "parole_chiave_sospette": [
            "casino", "bet", "poker", "loan", "crypto", "free-", "prize",
            "winner", "gift-card", "airdrop", "wallet-",
        ],
        "domini_esclusi_sempre": [
            "fonts.googleapis.com", "fonts.gstatic.com", "google-analytics.com",
            "googletagmanager.com", "cdnjs.cloudflare.com", "cdn.jsdelivr.net",
            "unpkg.com", "youtube.com", "ytimg.com", "player.vimeo.com",
            "maps.googleapis.com", "maps.gstatic.com", "connect.facebook.net",
            "use.fontawesome.com", "gravatar.com",
        ],
        "file_persistente": "link_sospetti.json",
    },
    "controllo_minacce": dict(threat_check.DEFAULT_THREAT_CFG),
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy semplice
    if path and path.exists():
        with path.open(encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, user_cfg)
    elif path:
        print(f"[avviso] config {path} non trovato: uso i default interni", file=sys.stderr)
    return cfg


# --------------------------------------------------------------------------
# Caricamento checkpoint / progress per paese
# --------------------------------------------------------------------------

def discover_countries(data_input_dir: Path, wanted: list[str]) -> list[str]:
    if wanted:
        return [c.upper() for c in wanted]
    if not data_input_dir.exists():
        return []
    found = []
    for child in sorted(data_input_dir.iterdir()):
        if child.is_dir() and (child / "crawl_checkpoint.json").exists():
            found.append(child.name)
    return found


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[avviso] {path} illeggibile/corrotto ({exc}): salto", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# Report generale (statistiche di crawl)
# --------------------------------------------------------------------------

def country_stats(country: str, checkpoint: dict[str, Any], progress: dict[str, Any] | None) -> dict[str, Any]:
    domain_registry = checkpoint.get("domain_registry", {})
    by_scope: dict[str, int] = {}
    for entry in domain_registry.values():
        scope = entry.get("scope", "?")
        by_scope[scope] = by_scope.get(scope, 0) + 1

    error_log = checkpoint.get("error_log", [])
    by_outcome: dict[str, int] = {}
    for e in error_log:
        outcome = e.get("outcome", "?")
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1

    domain_cooldown_retries = checkpoint.get("domain_cooldown_retries_used", {})
    domini_in_pausa = sorted(d for d, n in domain_cooldown_retries.items() if n)

    resource_index = checkpoint.get("resource_index")

    return {
        "paese": country,
        "completato": bool(checkpoint.get("completed")),
        "livello_corrente": checkpoint.get("level"),
        "salvato_il": checkpoint.get("saved_at"),
        "pagine_visitate": len(checkpoint.get("visited", [])),
        "domini_totali": len(domain_registry),
        "domini_per_scope": by_scope,
        "n_errori": len(error_log),
        "errori_per_esito": by_outcome,
        "domini_con_almeno_un_retry_cooldown": domini_in_pausa,
        "resource_index_presente": resource_index is not None,
        "n_domini_esterni_con_occorrenze_dettagliate": len(resource_index or {}),
        "riepilogo_per_seed_disponibile": progress is not None,
    }


def write_report_generale(all_stats: list[dict[str, Any]], out_dir: Path, cfg: dict[str, Any]) -> None:
    rg_cfg = cfg["report_generale"]
    tot_pagine = sum(s["pagine_visitate"] for s in all_stats)
    tot_domini = sum(s["domini_totali"] for s in all_stats)
    tot_errori = sum(s["n_errori"] for s in all_stats)
    n_senza_resource_index = sum(1 for s in all_stats if not s["resource_index_presente"])

    report = {
        "generato_il": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "paesi_analizzati": [s["paese"] for s in all_stats],
        "totali": {
            "pagine_visitate": tot_pagine,
            "domini_scoperti": tot_domini,
            "errori": tot_errori,
            "paesi_senza_resource_index": n_senza_resource_index,
        },
        "per_paese": all_stats,
    }
    (out_dir / "report_generale.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = ["# Report generale — post-processing checkpoint\n"]
    lines.append(f"_Generato il {report['generato_il']}_\n")
    lines.append("## Totali\n")
    lines.append(f"- Paesi analizzati: {len(all_stats)}")
    lines.append(f"- Pagine visitate (somma): {tot_pagine}")
    lines.append(f"- Domini scoperti (somma, con duplicati fra paesi): {tot_domini}")
    lines.append(f"- Errori registrati (somma): {tot_errori}")
    if n_senza_resource_index:
        lines.append(
            f"- ⚠️ {n_senza_resource_index} paese/i senza 'resource_index' nel checkpoint: "
            "i link sospetti per quei paesi non includono lo spacchettamento per tipo "
            "(script/iframe/img/link) — rilanciare il crawl con la versione aggiornata "
            "di src/fetchers/generic.py per ottenerlo."
        )
    lines.append("")
    lines.append("## Dettaglio per paese\n")
    lines.append("| Paese | Completato | Pagine visitate | Domini | Seed | Interni | Esterni | Errori | In pausa (cooldown) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in all_stats:
        scope = s["domini_per_scope"]
        lines.append(
            f"| {s['paese']} | {'sì' if s['completato'] else 'no'} | {s['pagine_visitate']} | "
            f"{s['domini_totali']} | {scope.get('seed', 0)} | {scope.get('interno', 0)} | "
            f"{scope.get('esterno', 0)} | {s['n_errori']} | {len(s['domini_con_almeno_un_retry_cooldown'])} |"
        )
    lines.append("")

    if rg_cfg.get("includi_errori_dettaglio"):
        max_err = int(rg_cfg.get("max_errori_mostrati_per_paese", 30))
        for s in all_stats:
            if not s["errori_per_esito"]:
                continue
            lines.append(f"### Errori — {s['paese']}\n")
            for esito, n in sorted(s["errori_per_esito"].items(), key=lambda kv: -kv[1])[:max_err]:
                lines.append(f"- {esito}: {n}")
            lines.append("")

    if rg_cfg.get("includi_domini_in_pausa"):
        for s in all_stats:
            if not s["domini_con_almeno_un_retry_cooldown"]:
                continue
            lines.append(f"### Domini che hanno richiesto alternanza anti-blocco — {s['paese']}\n")
            for d in s["domini_con_almeno_un_retry_cooldown"]:
                lines.append(f"- {d}")
            lines.append("")

    (out_dir / "report_generale.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Euristiche di sospetto (indicative, MAI un verdetto automatico)
# --------------------------------------------------------------------------

_PUNYCODE_RE = re.compile(r"(^|\.)xn--", re.IGNORECASE)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def score_domain(domain: str, tipi: set[str], n_pagine_distinte: int, scheme_https_only: bool,
                  ls_cfg: dict[str, Any]) -> tuple[int, list[str]]:
    """Punteggio euristico + elenco leggibile dei motivi. Puramente
    indicativo (serve solo a ordinare la lista per priorità di revisione),
    non è mai usato per escludere/includere automaticamente un dominio come
    'minaccia confermata'."""
    score = 0
    motivi: list[str] = []

    pesi_tipo = ls_cfg.get("peso_tipo_riferimento", {})
    for t in tipi:
        p = pesi_tipo.get(t, 0)
        if p:
            score += p
            etichetta = {
                "script": "dipendenza eseguibile (<script>)",
                "iframe": "dipendenza embedded (<iframe>)",
                "img": "dipendenza di contenuto (<img>)",
                "link_risorsa": "risorsa <link> (CSS/font/preconnect)",
                "link": "semplice link (<a href>)",
            }.get(t, t)
            motivi.append(etichetta)

    if not scheme_https_only:
        score += int(ls_cfg.get("peso_no_https", 0))
        motivi.append("almeno un riferimento non-HTTPS")

    if _is_ip_literal(domain):
        score += int(ls_cfg.get("peso_ip_letterale", 0))
        motivi.append("host è un indirizzo IP letterale, non un nome a dominio")

    if _PUNYCODE_RE.search(domain):
        score += int(ls_cfg.get("peso_punycode", 0))
        motivi.append("dominio punycode (xn--): possibile omografo/IDN")

    labels = domain.lower().split(".")
    tld = labels[-1] if labels else ""
    if tld in {t.lower() for t in ls_cfg.get("tld_sospetti", [])}:
        score += int(ls_cfg.get("peso_tld_sospetto", 0))
        motivi.append(f"TLD tra quelli segnalati in config ('.{tld}')")

    dom_lower = domain.lower()
    for kw in ls_cfg.get("parole_chiave_sospette", []):
        if kw.lower() in dom_lower:
            score += int(ls_cfg.get("peso_parola_chiave_sospetta", 0))
            motivi.append(f"contiene la parola chiave sospetta '{kw}'")
            break

    if n_pagine_distinte == 1:
        score += int(ls_cfg.get("peso_rarita_singola_pagina", 0))
        motivi.append("visto su una sola pagina in tutto il crawl analizzato")

    return score, motivi


def _domain_excluded(domain: str, esclusi: list[str]) -> bool:
    d = domain.lower()
    return any(d == e.lower() or d.endswith("." + e.lower()) for e in esclusi)


# --------------------------------------------------------------------------
# Raccolta occorrenze (resource_index) da tutti i checkpoint
# --------------------------------------------------------------------------

def collect_occurrences(country_checkpoints: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Ritorna {dominio: [occorrenza, ...]} con occorrenza =
    {"paese", "pagina", "tipo", "url_risorsa", "livello"}, unendo il
    'resource_index' di tutti i paesi analizzati. Paesi il cui checkpoint
    non ha ancora 'resource_index' (versione precedente del crawler)
    contribuiscono semplicemente 0 occorrenze — non è un errore fatale."""
    combined: dict[str, list[dict[str, Any]]] = {}
    for country, checkpoint in country_checkpoints.items():
        resource_index = checkpoint.get("resource_index") or {}
        for domain, occorrenze in resource_index.items():
            bucket = combined.setdefault(domain, [])
            for occ in occorrenze:
                bucket.append({
                    "paese": country,
                    "pagina": occ.get("pagina"),
                    "tipo": occ.get("tipo"),
                    "url_risorsa": occ.get("url_risorsa"),
                    "livello": occ.get("livello"),
                })
    return combined


def write_tutte_le_terze_parti(
    occurrences: dict[str, list[dict[str, Any]]],
    out_dir: Path,
    threat_verdicts: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Elenco COMPLETO, senza alcuna esclusione: un dominio commerciale, un
    link pubblicitario o un dominio raro compaiono qui esattamente come
    tutti gli altri — il verdetto del controllo automatico è solo una
    colonna informativa in più, mai un motivo per non elencare una riga."""
    threat_verdicts = threat_verdicts or {}
    path = out_dir / "tutte_le_terze_parti.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "dominio", "paese", "tipo_riferimento", "livello", "pagina", "url_risorsa",
            "verdetto_controllo_automatico", "gsb_flag", "vt_motori_maligni", "vt_motori_totali",
        ])
        for domain in sorted(occurrences):
            v = threat_verdicts.get(domain, {})
            for occ in occurrences[domain]:
                writer.writerow([
                    domain, occ["paese"], occ["tipo"], occ["livello"], occ["pagina"], occ["url_risorsa"],
                    v.get("verdetto", "non_controllato"), v.get("gsb_flag", False),
                    v.get("vt_motori_maligni", ""), v.get("vt_motori_totali", ""),
                ])


# --------------------------------------------------------------------------
# Link sospetti: shortlist per revisione umana, con persistenza del flag
# --------------------------------------------------------------------------

def load_previous_link_sospetti(path: Path) -> dict[str, dict[str, Any]]:
    """Rilegge un link_sospetti.json di un run precedente (se esiste) e
    ritorna {dominio: voce}, usato per NON perdere mai un
    'minaccia_confermata_da_umano: true' (o una 'note') impostato a mano da
    un revisore fra un run e l'altro."""
    data = load_json(path)
    if not data:
        return {}
    out = {}
    for voce in data:
        dominio = voce.get("link_sospetto")
        if dominio:
            out[dominio] = voce
    return out


def compute_domain_meta(
    occurrences: dict[str, list[dict[str, Any]]],
    ls_cfg: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Calcola, per OGNI dominio (esclusi o no, sotto soglia o no — nessun
    filtro qui), rarità/tipi/punteggio euristico. Riusata sia per costruire
    lo shortlist sia per decidere il subset da mandare a VirusTotal, cosi
    il criterio 'punteggio alto o raro' resta uno solo, calcolato una volta."""
    meta: dict[str, dict[str, Any]] = {}
    for domain, occs in occurrences.items():
        pagine_distinte = sorted({f"{o['paese']}: {o['pagina']}" for o in occs if o.get("pagina")})
        tipi = {o["tipo"] for o in occs if o.get("tipo")}
        tutte_https = all((o.get("url_risorsa") or "").lower().startswith("https://") for o in occs)
        score, motivi = score_domain(domain, tipi, len(pagine_distinte), tutte_https, ls_cfg)
        meta[domain] = {
            "tipi": tipi,
            "pagine_distinte": pagine_distinte,
            "n_pagine_distinte": len(pagine_distinte),
            "tutte_https": tutte_https,
            "punteggio_euristico": score,
            "motivi_euristica": motivi,
        }
    return meta


def build_link_sospetti(
    domain_meta: dict[str, dict[str, Any]],
    ls_cfg: dict[str, Any],
    previous: dict[str, dict[str, Any]],
    threat_verdicts: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    esclusi = ls_cfg.get("domini_esclusi_sempre", [])
    soglia = int(ls_cfg.get("soglia_pagine_rarita", 5))
    threat_verdicts = threat_verdicts or {}

    voci: list[dict[str, Any]] = []
    for domain, m in domain_meta.items():
        if _domain_excluded(domain, esclusi):
            continue

        score = m["punteggio_euristico"]
        verdetto_minaccia = threat_verdicts.get(domain, {}).get("verdetto", "non_controllato")

        # Criterio di inclusione nello shortlist: raro (sotto soglia
        # pagine) OPPURE punteggio euristico positivo OPPURE il controllo
        # minacce (GSB/VirusTotal) l'ha segnalato come sospetto — anche se
        # diffuso su molte pagine, un dominio flaggato da un motore esterno
        # merita di comparire nello shortlist di revisione.
        if m["n_pagine_distinte"] > soglia and score == 0 and verdetto_minaccia not in ("sospetto", "da_verificare"):
            continue

        prec = previous.get(domain, {})
        voci.append({
            "link_sospetto": domain,
            "tipi_riferimento": sorted(m["tipi"]),
            "n_pagine_distinte": m["n_pagine_distinte"],
            "pagine_in_cui_e_presente": m["pagine_distinte"],
            "punteggio_euristico": score,
            "motivi_euristica": m["motivi_euristica"],
            # Persistente: mai reimpostato a false/"" se un run precedente
            # lo aveva già valorizzato.
            "minaccia_confermata_da_umano": bool(prec.get("minaccia_confermata_da_umano", False)),
            "note": prec.get("note", ""),
            # Campi informativi dal controllo automatico (GSB/VirusTotal):
            # SOLO un dato in più per orientare la revisione, mai un
            # sostituto di 'minaccia_confermata_da_umano'.
            "verdetto_controllo_automatico": verdetto_minaccia,
            "gsb_flag": threat_verdicts.get(domain, {}).get("gsb_flag", False),
            "vt_motori_maligni": threat_verdicts.get(domain, {}).get("vt_motori_maligni"),
            "vt_motori_totali": threat_verdicts.get(domain, {}).get("vt_motori_totali"),
        })

    # Ordine di lettura: prima i sospetti confermati dal controllo
    # automatico, poi punteggio decrescente, poi rarità crescente.
    ordine_verdetto = {"sospetto": 0, "da_verificare": 1}
    voci.sort(key=lambda v: (
        ordine_verdetto.get(v["verdetto_controllo_automatico"], 2),
        -v["punteggio_euristico"], v["n_pagine_distinte"], v["link_sospetto"],
    ))
    return voci


def write_link_sospetti(voci: list[dict[str, Any]], out_dir: Path, filename: str) -> None:
    json_path = out_dir / filename
    json_path.write_text(json.dumps(voci, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = out_dir / (Path(filename).stem + ".csv")
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "link_sospetto", "punteggio_euristico", "n_pagine_distinte", "tipi_riferimento",
            "motivi_euristica", "minaccia_confermata_da_umano", "note", "pagine_in_cui_e_presente",
            "verdetto_controllo_automatico", "gsb_flag", "vt_motori_maligni", "vt_motori_totali",
        ])
        for v in voci:
            writer.writerow([
                v["link_sospetto"], v["punteggio_euristico"], v["n_pagine_distinte"],
                ";".join(v["tipi_riferimento"]), ";".join(v["motivi_euristica"]),
                v["minaccia_confermata_da_umano"], v.get("note", ""),
                ";".join(v["pagine_in_cui_e_presente"]),
                v.get("verdetto_controllo_automatico", "non_controllato"),
                v.get("gsb_flag", False), v.get("vt_motori_maligni", ""), v.get("vt_motori_totali", ""),
            ])

    md_path = out_dir / (Path(filename).stem + ".md")
    lines = [
        "# Link sospetti — da verificare\n",
        "Shortlist di domini di terza parte da controllare a mano, ordinata per punteggio "
        "euristico decrescente e poi per rarità. **Comparire in questo elenco non significa "
        "che un dominio sia malevolo**: è un aiuto alla revisione, non un verdetto automatico. "
        "Dopo la verifica, imposta `minaccia_confermata_da_umano: true` (e opzionalmente `note`) "
        "nel file JSON o CSV corrispondente — il valore resta al run successivo.\n",
        "---\n",
    ]
    etichette_automatiche = {
        "sospetto": "🚩 sospetto (GSB e/o VirusTotal)",
        "da_verificare": "🟡 da verificare (VirusTotal: motori 'suspicious')",
        "pulito": "☑️ nessun segnale nelle fonti consultate (non è una garanzia di sicurezza)",
        "non_controllato": "⬜ non ancora controllato automaticamente",
        "escluso_lista_vendor_noti": "⬜ vendor noto, escluso dal controllo",
        "controllo_disabilitato": "⬜ controllo automatico disabilitato in config",
    }
    for v in voci:
        stato = "🔴 CONFERMATO da revisore umano" if v["minaccia_confermata_da_umano"] else "⚪ da verificare"
        lines.append(f"## {v['link_sospetto']}  —  {stato}\n")
        lines.append(f"- Punteggio euristico: {v['punteggio_euristico']}")
        lines.append(f"- Tipi di riferimento trovati: {', '.join(v['tipi_riferimento']) or 'n/d'}")
        if v["motivi_euristica"]:
            lines.append(f"- Motivi: {', '.join(v['motivi_euristica'])}")
        auto = v.get("verdetto_controllo_automatico", "non_controllato")
        lines.append(f"- Controllo automatico (GSB/VirusTotal): {etichette_automatiche.get(auto, auto)}")
        if v.get("vt_motori_totali"):
            lines.append(f"  - VirusTotal: {v.get('vt_motori_maligni', 0)}/{v['vt_motori_totali']} motori lo segnalano come maligno")
        if v.get("note"):
            lines.append(f"- Nota del revisore: {v['note']}")
        lines.append(f"- Presente in {v['n_pagine_distinte']} pagina/e:")
        for p in v["pagine_in_cui_e_presente"]:
            lines.append(f"  - {p}")
        lines.append("\n---\n")
    md_path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config_postprocess.yaml", help="Percorso del file YAML di configurazione.")
    ap.add_argument("--country", "-c", action="append", default=[], help="Limita a questi paesi (ripetibile). Default: tutti quelli trovati.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Log più dettagliati (DEBUG invece di INFO).")
    args = ap.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    cfg = load_config(config_path)

    # Senza questa chiamata i log di questo script e di threat_check (incluso l'avanzamento del
    # controllo minacce, che può durare a lungo per via del rate-limit di VirusTotal) restano
    # invisibili: 'logger.info(...)' senza nessun handler configurato non stampa NULLA, non va
    # nemmeno su stderr — non è che lo script sia bloccato, è che il progresso non veniva mostrato.
    out_dir = PROJECT_ROOT / cfg["output"]["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(out_dir / "postprocess.log", encoding="utf-8")],
    )

    data_input_dir = PROJECT_ROOT / cfg["input"]["data_input_dir"]
    countries_cfg = args.country or cfg["input"].get("countries", [])
    countries = discover_countries(data_input_dir, countries_cfg)

    if not countries:
        print(
            f"Nessun checkpoint trovato sotto {data_input_dir} (o nessuno dei paesi richiesti ha un "
            "crawl_checkpoint.json). Esegui prima main.py --country ... e riprova.",
            file=sys.stderr,
        )
        sys.exit(1)

    out_dir = PROJECT_ROOT / cfg["output"]["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    country_checkpoints: dict[str, dict[str, Any]] = {}
    all_stats = []
    for country in countries:
        cdir = data_input_dir / country
        checkpoint = load_json(cdir / "crawl_checkpoint.json")
        if checkpoint is None:
            print(f"[avviso] {country}: nessun checkpoint valido, salto", file=sys.stderr)
            continue
        progress = load_json(cdir / "crawl_progress.json")
        country_checkpoints[country] = checkpoint
        all_stats.append(country_stats(country, checkpoint, progress))

    if not all_stats:
        print("Nessun checkpoint valido caricato: nessun report generato.", file=sys.stderr)
        sys.exit(1)

    write_report_generale(all_stats, out_dir, cfg)
    print(f"Report generale scritto in {out_dir / 'report_generale.md'}")

    ls_cfg = cfg["link_sospetti"]
    if ls_cfg.get("abilitato", True):
        logger.info("Raccolgo le occorrenze di terze parti dai checkpoint...")
        occurrences = collect_occurrences(country_checkpoints)
        if not occurrences:
            print(
                "[avviso] nessun 'resource_index' trovato in nessun checkpoint: nessun link sospetto "
                "generabile. Rilancia il crawl con la versione aggiornata di src/fetchers/generic.py "
                "(vedi docstring di questo script) per ottenere questo dettaglio.",
                file=sys.stderr,
            )
        else:
            logger.info("%d domini di terza parte trovati, calcolo rarità/punteggio euristico...", len(occurrences))
            # Punteggio/rarità calcolati UNA VOLTA per ogni dominio unico
            # (mai per occorrenza): la stessa base serve sia per il
            # controllo minacce (quali domini mandare a VirusTotal) sia
            # per lo shortlist di revisione.
            domain_meta = compute_domain_meta(occurrences, ls_cfg)

            threat_cfg = cfg.get("controllo_minacce", {})
            logger.info("Avvio il controllo minacce (Google Safe Browsing / VirusTotal)...")
            threat_verdicts = threat_check.enrich_domains(
                domains_meta={d: {"punteggio_euristico": m["punteggio_euristico"], "n_pagine_distinte": m["n_pagine_distinte"]}
                              for d, m in domain_meta.items()},
                esclusi_sempre=ls_cfg.get("domini_esclusi_sempre", []),
                threat_cfg=threat_cfg,
                out_dir=out_dir,
                soglia_pagine_rarita=int(ls_cfg.get("soglia_pagine_rarita", 5)),
            )
            n_sospetti_auto = sum(1 for v in threat_verdicts.values() if v.get("verdetto") == "sospetto")
            n_controllabili = sum(1 for v in threat_verdicts.values() if v.get("verdetto") not in
                                   ("escluso_lista_vendor_noti", "controllo_disabilitato"))
            n_controllati = sum(1 for v in threat_verdicts.values() if v.get("verdetto") not in
                                 ("non_controllato", "escluso_lista_vendor_noti", "controllo_disabilitato"))
            cache_path = out_dir / threat_cfg.get("cache_file", "cache_minacce.json")
            print(
                f"Controllo minacce automatico: {n_controllati}/{n_controllabili} domini controllati "
                f"(GSB e/o VirusTotal), {n_sospetti_auto} segnalati come sospetti — cache in {cache_path}"
            )
            if n_controllati < n_controllabili:
                print(
                    f"  → {n_controllabili - n_controllati} domini non ancora controllati (quota "
                    "giornaliera esaurita e/o interruzione): il lavoro fatto finora è salvato nella "
                    "cache, rilancia lo stesso comando (oggi più tardi, se la quota si libera, o "
                    "domani) per riprendere esattamente da dove si era fermato — nessun dominio già "
                    "controllato verrà richiesto di nuovo."
                )

            write_tutte_le_terze_parti(occurrences, out_dir, threat_verdicts)
            filename = ls_cfg.get("file_persistente", "link_sospetti.json")
            previous = load_previous_link_sospetti(out_dir / filename)
            voci = build_link_sospetti(domain_meta, ls_cfg, previous, threat_verdicts)
            write_link_sospetti(voci, out_dir, filename)
            n_confermati = sum(1 for v in voci if v["minaccia_confermata_da_umano"])
            print(
                f"Link sospetti: {len(voci)} domini nello shortlist ({n_confermati} già confermati da "
                f"un revisore in un run precedente) — vedi {out_dir / filename}"
            )

    print(f"\nFatto. Output in: {out_dir}")


if __name__ == "__main__":
    main()
