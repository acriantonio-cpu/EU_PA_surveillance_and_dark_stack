# EN: Reputation enrichment (never a filter) of third-party domains via Google Safe Browsing
# (batched) and VirusTotal, with cache and daily quota; API keys read from .env.
"""
tools/threat_check.py

Arricchimento (MAI un filtro) dei domini di terza parte già raccolti da
postprocess_checkpoints.py con un verdetto di reputazione, usando due
servizi con tier gratuito:

  - Google Safe Browsing (threatMatches:find): batch fino a 500 domini per
    UNA SOLA chiamata HTTP -> con 10.000 richieste/giorno gratuite, anche
    decine di migliaia di domini unici stanno comodamente sotto quota,
    perché il costo è per BATCH, non per dominio.
  - VirusTotal (GET /api/v3/domains/{dominio}): legge il report già
    esistente su VT per quel dominio (nessuna nuova scansione richiesta).
    Tier gratuito: 4 richieste/minuto, 500/giorno — molto più stretto,
    quindi va usato solo su un SOTTOINSIEME di domini (vedi
    `_seleziona_subset_vt`), non su tutto l'universo.

Principi seguiti (concordati con l'utente):
  1. Un dominio viene controllato AL MASSIMO una volta, poi il verdetto
     resta in cache su disco (`cache_minacce.json`, percorso configurabile)
     e non viene richiesto di nuovo finché non scade `ttl_giorni_cache`.
  2. Il controllo è un CAMPO INFORMATIVO aggiunto in più alle righe già
     prodotte da postprocess_checkpoints.py — non esclude/nasconde MAI un
     dominio dai report, coerente col fatto che un link raro o "insolito"
     non è automaticamente una minaccia (e viceversa).
  3. La quota giornaliera (soprattutto quella stretta di VirusTotal) è
     tracciata persistentemente nella stessa cache: se un run la esaurisce,
     i domini restanti restano marcati 'non_controllato' e vengono ripresi
     al run successivo, MAI in errore fatale per l'intero post-processing.

Se le chiavi API non sono impostate (variabili d'ambiente, vedi
`_api_key_da_env`), il modulo si disattiva da solo: tutti i domini restano
'non_controllato' e il resto del post-processing prosegue normalmente.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SECRET_QS_RE = re.compile(r"([?&](?:key|apikey|api_key|x-apikey)=)[^&\s'\"]+", re.IGNORECASE)


def _fmt_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


class _Progress:
    """Log leggibile ('N/tot (P%) - v/s - ETA') a INTERVALLI DI TEMPO, non ogni tot elementi:
    utile soprattutto per VirusTotal, dove ogni chiamata è volutamente distanziata di ~16s per
    restare sotto la quota gratuita — senza questo, con poche decine di domini lo script sembra
    fermo per minuti perché non c'è nessun log a scandire l'attesa."""

    def __init__(self, prefix: str, total: int, every_seconds: float = 10.0):
        self.prefix = prefix
        self.total = max(total, 1)
        self.every = every_seconds
        self.start = time.monotonic()
        self.last_log = self.start
        self.done = 0

    def tick(self, n: int = 1) -> None:
        self.done += n
        now = time.monotonic()
        if self.done >= self.total or (now - self.last_log) >= self.every:
            self.last_log = now
            elapsed = now - self.start
            rate = self.done / elapsed if elapsed > 0 else 0.0
            pct = 100.0 * self.done / self.total
            eta = (self.total - self.done) / rate if rate > 0 else 0.0
            logger.info("%s: %d/%d (%.0f%%) - %.2f/s - ETA %s", self.prefix, self.done, self.total, pct, rate, _fmt_eta(eta))


def _redact(text: Any) -> str:
    """Toglie le chiavi API dai messaggi d'errore: requests include l'URL completo
    (con '?key=...') nel testo di HTTPError, e finiva nei log."""
    return _SECRET_QS_RE.sub(r"\1***", str(text))


try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


DEFAULT_THREAT_CFG: dict[str, Any] = {
    "abilitato": True,
    "cache_file": "cache_minacce.json",
    "ttl_giorni_cache": 90,
    # Google Safe Browsing: applicato a TUTTI i domini non esclusi/non in
    # cache fresca (costo per batch, non per dominio -> pochissimo impatto
    # sulla quota anche con molte migliaia di domini).
    "gsb_dimensione_batch": 500,
    "gsb_limite_batch_giornaliero": 5000,  # tetto sotto le 10.000 richieste/giorno gratuite (1 richiesta = 1 batch da <=500 domini)
    # VirusTotal: SOLO su un sottoinsieme (vedi soglie sotto), per restare
    # sotto le 500 query/giorno del tier gratuito.
    "vt_limite_giornaliero": 480,  # margine sotto le 500/giorno
    "vt_secondi_tra_chiamate": 16,  # margine sotto le 4/minuto (15s teorici)
    # NOTA: peso_tipo_riferimento di default assegna già >=1 a QUALSIASI
    # tipo di riferimento (anche <img>/<a href>) — una soglia di 1
    # manderebbe quindi a VT quasi ogni dominio, vanificando il filtro.
    # 3 intercetta solo script/iframe (dipendenza eseguibile/embedded) o
    # somme di più euristiche minori insieme (es. no-https + TLD sospetto).
    "vt_solo_se_punteggio_euristico_almeno": 3,
    "vt_solo_se_raro_sotto_soglia_pagine": True,
    "vt_controlla_anche_se_flag_gsb": True,
    "timeout_secondi": 15,
    "nomi_variabili_ambiente": {
        "google_safe_browsing": "GOOGLE_SAFE_BROWSING_API_KEY",
        "virustotal": "VIRUSTOTAL_API_KEY",
    },
}


def _api_key_da_env(cfg: dict[str, Any], servizio: str) -> str | None:
    var_name = cfg.get("nomi_variabili_ambiente", {}).get(
        servizio, DEFAULT_THREAT_CFG["nomi_variabili_ambiente"][servizio]
    )
    return os.environ.get(var_name) or None


# --------------------------------------------------------------------------
# Cache persistente (verdetti + quota giornaliera)
# --------------------------------------------------------------------------

def _oggi_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"_meta": {}, "domini": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("cache minacce %s illeggibile (%s): riparto da vuota", path, exc)
        return {"_meta": {}, "domini": {}}
    data.setdefault("_meta", {})
    data.setdefault("domini", {})
    return data


def save_cache(cache: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _reset_quota_se_nuovo_giorno(meta: dict[str, Any]) -> None:
    oggi = _oggi_iso()
    if meta.get("data_quota") != oggi:
        meta["data_quota"] = oggi
        meta["gsb_batch_usati_oggi"] = 0
        meta["vt_chiamate_usate_oggi"] = 0


def _timestamp_scaduto(timestamp_iso: str | None, ttl_giorni: int) -> bool:
    if not timestamp_iso:
        return True
    try:
        dt = datetime.fromisoformat(timestamp_iso)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - dt > timedelta(days=ttl_giorni)


# --------------------------------------------------------------------------
# Google Safe Browsing — batch, primo filtro su tutti i domini
# --------------------------------------------------------------------------

def _query_gsb_batch(domains: list[str], api_key: str, timeout: int) -> dict[str, list[str]]:
    """Ritorna {dominio: [tipi_minaccia]} SOLO per i domini flaggati (gli
    altri, per come funziona l'API, semplicemente non compaiono nella
    risposta = puliti secondo GSB)."""
    if requests is None:
        raise RuntimeError("libreria 'requests' non installata (pip install requests)")

    url = f"https://safebrowsing.googleapis.com/v4/threatMatches:find?key={api_key}"
    body = {
        "client": {"clientId": "eu-pa-scraper-threat-check", "clientVersion": "1.0"},
        "threatInfo": {
            "threatTypes": [
                "MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION",
            ],
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            # GSB vuole URL, non nudi hostname: un https:// generico basta,
            # l'API confronta per host/prefisso, non serve un path reale.
            "threatEntries": [{"url": f"https://{d}/"} for d in domains],
        },
    }
    resp = requests.post(url, json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    flagged: dict[str, list[str]] = {}
    for match in data.get("matches", []):
        matched_url = match.get("threat", {}).get("url", "")
        host = matched_url.split("://", 1)[-1].split("/", 1)[0]
        # Il dominio originale (senza schema/slash finale) usato come chiave,
        # per farlo combaciare con l'elenco passato in input.
        for d in domains:
            if host == d or host.endswith("." + d) or d.endswith("." + host):
                flagged.setdefault(d, []).append(match.get("threatType", "SCONOSCIUTO"))
    return flagged


def _run_gsb(domains: list[str], cfg: dict[str, Any], cache: dict[str, Any], cache_path: Path) -> None:
    api_key = _api_key_da_env(cfg, "google_safe_browsing")
    meta = cache["_meta"]
    _reset_quota_se_nuovo_giorno(meta)

    if not api_key:
        logger.info("Google Safe Browsing: chiave API non impostata, salto (domini restano 'non_controllato' su questo fronte)")
        return

    batch_size = int(cfg.get("gsb_dimensione_batch", 500))
    limite_batch = int(cfg.get("gsb_limite_batch_giornaliero", 15))
    timeout = int(cfg.get("timeout_secondi", 15))

    batches = [domains[i:i + batch_size] for i in range(0, len(domains), batch_size)]
    progress = _Progress("Google Safe Browsing", len(batches))
    try:
        for batch in batches:
            if meta["gsb_batch_usati_oggi"] >= limite_batch:
                logger.warning(
                    "Google Safe Browsing: limite di %d batch/giorno raggiunto, %d domini restanti "
                    "verranno ripresi al prossimo run (oggi, se la quota si libera, o domani)",
                    limite_batch, len(domains),
                )
                break
            try:
                flagged = _query_gsb_batch(batch, api_key, timeout)
            except Exception as exc:  # noqa: BLE001 - mai fatale per il resto del post-processing
                logger.warning("Google Safe Browsing: errore sul batch (%s), domini di questo batch restano 'non_controllato' su questo fronte", _redact(exc))
                meta["gsb_batch_usati_oggi"] += 1
                save_cache(cache, cache_path)
                progress.tick()
                continue
            meta["gsb_batch_usati_oggi"] += 1

            for d in batch:
                voce = cache["domini"].setdefault(d, {})
                voce["gsb_controllato_il"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                voce["gsb_flag"] = d in flagged
                voce["gsb_minacce"] = flagged.get(d, [])

            # Checkpoint dopo OGNI batch: se il processo viene interrotto
            # (Ctrl+C, crash, rete che cade) subito dopo, si perde al
            # massimo il batch in corso al momento dell'interruzione, mai
            # quelli già completati.
            save_cache(cache, cache_path)
            progress.tick()
    except KeyboardInterrupt:
        save_cache(cache, cache_path)
        logger.warning(
            "Google Safe Browsing: interrotto dall'utente (Ctrl+C) — il lavoro fatto fino a qui è "
            "salvato in %s, rilancia lo stesso comando per riprendere da dove si era interrotto",
            cache_path,
        )
        raise


# --------------------------------------------------------------------------
# VirusTotal — solo su subset selezionato, rate-limited
# --------------------------------------------------------------------------

def _e_ip_letterale(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _query_vt_domain(domain: str, api_key: str, timeout: int) -> dict[str, Any]:
    """Interroga VirusTotal per un host: usa l'endpoint 'domains' per un
    nome a dominio normale, l'endpoint 'ip_addresses' se l'host è un
    indirizzo IP letterale (i due endpoint NON sono intercambiabili — VT
    risponde 400 Bad Request se si manda un IP all'endpoint sbagliato)."""
    if requests is None:
        raise RuntimeError("libreria 'requests' non installata (pip install requests)")

    if _e_ip_letterale(domain):
        url = f"https://www.virustotal.com/api/v3/ip_addresses/{domain}"
    else:
        url = f"https://www.virustotal.com/api/v3/domains/{domain}"

    resp = requests.get(url, headers={"x-apikey": api_key}, timeout=timeout)
    if resp.status_code == 404:
        return {"trovato": False}
    resp.raise_for_status()
    data = resp.json()
    stats = data.get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
    return {
        "trovato": True,
        "motori_maligni": int(stats.get("malicious", 0)),
        "motori_sospetti": int(stats.get("suspicious", 0)),
        "motori_totali": sum(stats.get(k, 0) for k in ("harmless", "malicious", "suspicious", "undetected", "timeout")),
    }


def _seleziona_subset_vt(
    domains_meta: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
    cache: dict[str, Any],
) -> list[str]:
    """domains_meta: {dominio: {"punteggio_euristico": int, "n_pagine_distinte": int}}
    Seleziona i domini da mandare a VT, in ordine di priorità:
    già flaggati da GSB in questo run > punteggio euristico alto > rarità.
    NON manda a VT domini con punteggio 0 e non rari, a meno che GSB non li
    abbia flaggati: sono la maggioranza (CDN/vendor diffusi e "puliti" per
    euristica) e controllarli tutti esaurirebbe la quota di VT in un run
    solo, lasciando il resto scoperto senza motivo."""
    soglia_punteggio = int(cfg.get("vt_solo_se_punteggio_euristico_almeno", 1))
    usa_rarita = bool(cfg.get("vt_solo_se_raro_sotto_soglia_pagine", True))
    soglia_rarita = int(cfg.get("_soglia_pagine_rarita_ereditata", 5))
    usa_gsb_flag = bool(cfg.get("vt_controlla_anche_se_flag_gsb", True))

    candidati = []
    for d, meta in domains_meta.items():
        gsb_flag = cache["domini"].get(d, {}).get("gsb_flag", False)
        motivo_gsb = usa_gsb_flag and gsb_flag
        motivo_score = meta.get("punteggio_euristico", 0) >= soglia_punteggio
        motivo_rarita = usa_rarita and meta.get("n_pagine_distinte", 999) <= soglia_rarita
        if motivo_gsb or motivo_score or motivo_rarita:
            priorita = (0 if motivo_gsb else 1, -meta.get("punteggio_euristico", 0), meta.get("n_pagine_distinte", 999))
            candidati.append((priorita, d))

    candidati.sort(key=lambda t: t[0])
    return [d for _, d in candidati]


def _run_vt(domains: list[str], cfg: dict[str, Any], cache: dict[str, Any], cache_path: Path) -> None:
    api_key = _api_key_da_env(cfg, "virustotal")
    meta = cache["_meta"]
    _reset_quota_se_nuovo_giorno(meta)

    if not api_key:
        logger.info("VirusTotal: chiave API non impostata, salto (domini restano 'non_controllato' su questo fronte)")
        return

    limite = int(cfg.get("vt_limite_giornaliero", 480))
    attesa = float(cfg.get("vt_secondi_tra_chiamate", 16))
    timeout = int(cfg.get("timeout_secondi", 15))

    controllati_in_questo_run = 0
    progress = _Progress("VirusTotal", len(domains))
    try:
        for d in domains:
            if meta["vt_chiamate_usate_oggi"] >= limite:
                logger.warning(
                    "VirusTotal: limite di %d chiamate/giorno raggiunto, %d domini candidati restanti "
                    "verranno ripresi al prossimo run (oggi, se la quota si libera, o domani)",
                    limite, len(domains) - controllati_in_questo_run,
                )
                break
            try:
                risultato = _query_vt_domain(d, api_key, timeout)
            except Exception as exc:  # noqa: BLE001 - mai fatale
                logger.warning("VirusTotal: errore su %s (%s), resta 'non_controllato' su questo fronte", d, _redact(exc))
                meta["vt_chiamate_usate_oggi"] += 1
                controllati_in_questo_run += 1
                save_cache(cache, cache_path)
                progress.tick()
                if controllati_in_questo_run < len(domains):
                    time.sleep(attesa)
                continue

            meta["vt_chiamate_usate_oggi"] += 1
            controllati_in_questo_run += 1

            voce = cache["domini"].setdefault(d, {})
            voce["vt_controllato_il"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            voce["vt_trovato"] = risultato["trovato"]
            if risultato["trovato"]:
                voce["vt_motori_maligni"] = risultato["motori_maligni"]
                voce["vt_motori_sospetti"] = risultato["motori_sospetti"]
                voce["vt_motori_totali"] = risultato["motori_totali"]

            # Checkpoint dopo OGNI dominio: essendo già rate-limited a ~4
            # al minuto, il costo di un salvataggio su disco in più è
            # trascurabile rispetto al beneficio — un'interruzione perde
            # al massimo la query in corso, mai quelle già fatte.
            save_cache(cache, cache_path)
            progress.tick()

            if controllati_in_questo_run < len(domains):
                time.sleep(attesa)
    except KeyboardInterrupt:
        save_cache(cache, cache_path)
        logger.warning(
            "VirusTotal: interrotto dall'utente (Ctrl+C) — il lavoro fatto fino a qui è salvato in "
            "%s, rilancia lo stesso comando per riprendere da dove si era interrotto", cache_path,
        )
        raise


# --------------------------------------------------------------------------
# Orchestrazione + calcolo verdetto finale leggibile
# --------------------------------------------------------------------------

def _calcola_verdetto(voce: dict[str, Any]) -> str:
    """Etichetta di sintesi, SEMPRE accompagnata dai dati grezzi nel report
    — non sostituisce mai la revisione umana, la ordina."""
    gsb_flag = voce.get("gsb_flag", False)
    vt_maligni = voce.get("vt_motori_maligni")
    vt_sospetti = voce.get("vt_motori_sospetti")

    if gsb_flag or (vt_maligni is not None and vt_maligni > 0):
        return "sospetto"
    if vt_sospetti is not None and vt_sospetti > 0:
        return "da_verificare"
    if "gsb_controllato_il" in voce or "vt_controllato_il" in voce:
        return "pulito"
    return "non_controllato"


def enrich_domains(
    domains_meta: dict[str, dict[str, Any]],
    esclusi_sempre: list[str],
    threat_cfg: dict[str, Any],
    out_dir: Path,
    soglia_pagine_rarita: int,
) -> dict[str, dict[str, Any]]:
    """Punto di ingresso chiamato da postprocess_checkpoints.py.

    domains_meta: {dominio: {"punteggio_euristico": int, "n_pagine_distinte": int}}
    Ritorna {dominio: {verdetto, gsb_flag, gsb_minacce, vt_motori_maligni,
    vt_motori_totali, ultimo_check}} per OGNI dominio passato in input
    (inclusi quelli esclusi/non controllati, con verdetto 'escluso' o
    'non_controllato' — mai assenti dal risultato, cosi il chiamante può
    sempre aggiungere la colonna al report senza casi speciali)."""
    cfg = dict(DEFAULT_THREAT_CFG)
    cfg.update(threat_cfg or {})
    cfg["_soglia_pagine_rarita_ereditata"] = soglia_pagine_rarita

    risultato: dict[str, dict[str, Any]] = {}

    if not cfg.get("abilitato", True):
        for d in domains_meta:
            risultato[d] = {"verdetto": "controllo_disabilitato"}
        return risultato

    esclusi_set_suffissi = [e.lower() for e in esclusi_sempre]

    def _escluso(d: str) -> bool:
        dl = d.lower()
        return any(dl == e or dl.endswith("." + e) for e in esclusi_set_suffissi)

    cache_path = out_dir / cfg.get("cache_file", "cache_minacce.json")
    cache = load_cache(cache_path)
    ttl_giorni = int(cfg.get("ttl_giorni_cache", 90))

    da_controllare_gsb: list[str] = []
    for d in domains_meta:
        if _escluso(d):
            risultato[d] = {"verdetto": "escluso_lista_vendor_noti"}
            continue
        voce_cache = cache["domini"].get(d, {})
        if not _timestamp_scaduto(voce_cache.get("gsb_controllato_il"), ttl_giorni):
            continue  # già fresco, non richiedere di nuovo a GSB
        da_controllare_gsb.append(d)

    if da_controllare_gsb:
        logger.info("Controllo minacce: %d domini da interrogare su Google Safe Browsing (batch da %s)",
                    len(da_controllare_gsb), cfg.get("gsb_dimensione_batch"))
        _run_gsb(da_controllare_gsb, cfg, cache, cache_path)  # si salva da sé dopo ogni batch

    # Subset per VT: solo fra i domini non esclusi, ricalcolando la
    # "freschezza" (un dominio con GSB fresco ma VT mai fatto o scaduto va
    # comunque considerato).
    subset_meta = {
        d: meta for d, meta in domains_meta.items()
        if not _escluso(d) and _timestamp_scaduto(cache["domini"].get(d, {}).get("vt_controllato_il"), ttl_giorni)
    }
    subset_vt = _seleziona_subset_vt(subset_meta, cfg, cache)
    if subset_vt:
        logger.info("Controllo minacce: %d domini candidati per VirusTotal (subset filtrato, non tutto l'universo)", len(subset_vt))
        _run_vt(subset_vt, cfg, cache, cache_path)  # si salva da sé dopo ogni dominio

    for d in domains_meta:
        if d in risultato:
            continue  # escluso, già valorizzato sopra
        voce = cache["domini"].get(d, {})
        risultato[d] = {
            "verdetto": _calcola_verdetto(voce),
            "gsb_flag": voce.get("gsb_flag", False),
            "gsb_minacce": voce.get("gsb_minacce", []),
            "vt_trovato": voce.get("vt_trovato"),
            "vt_motori_maligni": voce.get("vt_motori_maligni"),
            "vt_motori_sospetti": voce.get("vt_motori_sospetti"),
            "vt_motori_totali": voce.get("vt_motori_totali"),
            "ultimo_check": voce.get("vt_controllato_il") or voce.get("gsb_controllato_il"),
        }

    return risultato
