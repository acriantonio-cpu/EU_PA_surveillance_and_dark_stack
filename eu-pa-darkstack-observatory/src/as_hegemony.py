#!/usr/bin/env python3
# EN: EXPERIMENTAL, off by default - transit-AS dependency (AS Hegemony) via the IHR (Internet
# Health Report) API, per origin ASN.
"""
as_hegemony.py

SPERIMENTALE (seconda passata, settembre 2026) — NON attivo di default
(misurazioni.as_hegemony: false in config.yaml). Va attivato consapevolmente.

CORREZIONE IMPORTANTE (vedi cronologia): una prima bozza di questo modulo
ipotizzava un endpoint 'as-hegemony' su stat.ripe.net (RIPEstat) — verifica
successiva della documentazione ha mostrato che non esiste: la metrica AS
Hegemony è calcolata e pubblicata da IHR (Internet Health Report, IIJ
Research Lab — https://ihr.iijlab.net), un progetto DIVERSO da RIPEstat
(anche se parzialmente finanziato dal RIPE NCC Community Projects Fund).
Questo modulo usa l'API REST reale di IHR (ihr.iijlab.net/ihr/api), non
RIPEstat. Riferimento: Fontugne, Shah, Aben, "The (thin) Bridges of AS
Connectivity: Measuring Dependency using AS Hegemony", PAM 2018 — già
citato nella ricerca di stato dell'arte del progetto.

Cosa fa, quando attivo: dato un AS di ORIGINE (l'ASN che ospita/annuncia
l'IP misurato — quello già risolto da asn_bulk.py, non serve una nuova
risoluzione IP->ASN), chiede a IHR quali AS di TRANSITO intermedi hanno la
quota più alta di dipendenza per raggiungerlo. È un passo oltre la semplice
"in che Paese ha sede l'operatore" (giurisdizione_holding in
02_probe_infra.py): due hostname possono avere hosting in Paesi diversi ma
dipendere dallo stesso, unico AS di transito, il che è un tipo di
concentrazione che la sola etichetta di giurisdizione non cattura (vedi
Fontugne et al., 2017; Kastanakis et al., 2026, citati nella ricerca di
stato dell'arte del progetto).

L'API IHR lavora per INTERVALLO DI TEMPO (timebin__gte/timebin__lte), non
per istante singolo: i punteggi sono calcolati periodicamente, quindi la
finestra di query qui sotto usa un intervallo di qualche giorno finendo
'oggi' per avere buone probabilità di trovare il punteggio più recente
disponibile, non un singolo istante che potrebbe cadere fra due calcoli.

PERCHÉ è sperimentale e SPENTO di default (tre ragioni indipendenti):
  1. Richiesta di rete aggiuntiva per ASN di origine distinto — un costo in
     più che non tutti vogliono pagare su un campione grande, anche se la
     cache per-ASN (vedi HegemonyResolver più sotto) lo rende comunque
     molto più economico di 'una richiesta per hostname'.
  2. Non è stato possibile verificarlo contro il servizio reale
     ihr.iijlab.net in questo ambiente (nessun accesso alla rete pubblica
     durante lo sviluppo). La logica di parsing è stata verificata solo
     contro un mock locale che riproduce la forma di risposta documentata
     dal progetto IHR (vedi i test eseguiti, descritti nell'audit).
  3. È un servizio di terze parti (IIJ Research Lab) indipendente sia da
     RIPE NCC che dal bulk Cymru/RDAP già in uso altrove nel progetto:
     un'altra dipendenza esterna da valutare consapevolmente, non da
     aggiungere silenziosamente.

Se attivato e il servizio si comporta diversamente da quanto documentato,
la funzione fallisce in modo pulito (ritorna un errore, non un'eccezione),
esattamente come il resto del progetto.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

IHR_BASE_URL_DEFAULT = "https://ihr.iijlab.net"
IHR_TIMEOUT_DEFAULT = 8.0
# Finestra temporale della query: gli ultimi N giorni fino ad oggi, per avere
# buone probabilità di includere l'ultimo punteggio calcolato (vedi docstring
# del modulo). Non è un dato scientifico, è un compromesso pratico.
IHR_FINESTRA_GIORNI = 7

# Sotto questa quota di 'hege' (0.0-1.0, frazione delle rotte osservate che
# passano da quell'AS) un AS di transito non è considerato "dipendenza
# principale": è normale che una rotta passi da molti AS con quote minori,
# quello che interessa qui è se ce n'è uno dominante.
SOGLIA_DIPENDENZA_PRINCIPALE = 0.5


def hegemony_per_asn(origin_asn: str, timeout: float = IHR_TIMEOUT_DEFAULT,
                      base_url: str = IHR_BASE_URL_DEFAULT,
                      rate_limiter=None, session: "requests.Session | None" = None):
    """Ritorna (asn_dipendenza_principale, quota, errore) per l'AS di
    origine dato. None per asn_dipendenza_principale se nessun AS di
    transito supera SOGLIA_DIPENDENZA_PRINCIPALE, o se la chiamata fallisce
    (rete irraggiungibile, formato inatteso, nessun dato per questo ASN).
    Non lancia mai eccezioni verso il chiamante.

    Nota su af=4: interroga solo IPv4 di default (stessa scelta di default
    del resto della pipeline, che tratta 'a' come indirizzo primario e
    'aaaa' come dato accessorio separato)."""
    origin_asn = str(origin_asn or "").strip().lstrip("AS").strip()
    if not origin_asn or not origin_asn.isdigit():
        return None, None, None
    http = session or requests
    try:
        if rate_limiter is not None:
            rate_limiter.attendi()
        fine = datetime.now(timezone.utc)
        inizio = fine - timedelta(days=IHR_FINESTRA_GIORNI)
        r = http.get(
            f"{base_url}/ihr/api/hegemony/",
            params={
                "originasn": origin_asn,
                "af": 4,
                "timebin__gte": inizio.strftime("%Y-%m-%dT%H:%M"),
                "timebin__lte": fine.strftime("%Y-%m-%dT%H:%M"),
            },
            timeout=timeout,
        )
        r.raise_for_status()
        corpo = r.json() or {}
        risultati = corpo.get("results") if isinstance(corpo, dict) else corpo
        if not risultati:
            return None, None, "as_hegemony_nessun_dato"
        # Escludiamo la riga 'asn == originasn' (un AS dipende sempre al
        # 100% da se stesso, non è informativo) e quella del grafo globale
        # (asn=0, se presente): interessano solo i VERI AS di transito
        # terzi. Poi prendiamo la quota 'hege' più alta fra quelle rimaste.
        candidati = [
            r for r in risultati
            if str(r.get("asn")) not in (origin_asn, "0") and r.get("hege") is not None
        ]
        if not candidati:
            return None, None, None
        migliore = max(candidati, key=lambda r: r.get("hege", 0) or 0)
        quota = migliore.get("hege")
        if quota is None or quota < SOGLIA_DIPENDENZA_PRINCIPALE:
            return None, quota, None
        return str(migliore.get("asn")), quota, None
    except Exception as e:
        return None, None, f"as_hegemony_{type(e).__name__}"


class HegemonyResolver:
    """Cache per ASN DI ORIGINE (non per hostname/IP): decine di migliaia di
    hostname condividono un numero molto più piccolo di ASN distinti, quindi
    questa cache riduce enormemente le chiamate reali a IHR — stessa idea
    della cache /24-/48 di asn_bulk.py, applicata qui a livello di ASN
    invece che di blocco IP (qui l'unità naturale è l'ASN, non l'IP: IHR
    lavora per AS di origine, vedi docstring del modulo)."""

    def __init__(self, **kwargs_hegemony_per_asn):
        self._kwargs = kwargs_hegemony_per_asn
        self.cache = {}  # asn_origine -> (asn_dipendenza, quota, errore)

    def per_asn(self, origin_asn: str):
        if not origin_asn:
            return None, None, None
        if origin_asn in self.cache:
            return self.cache[origin_asn]
        risultato = hegemony_per_asn(origin_asn, **self._kwargs)
        self.cache[origin_asn] = risultato
        return risultato


def colonne_extra_sicure(origin_asn: str, resolver: "HegemonyResolver") -> dict:
    """Wrapper 'sicuro per design' pensato per essere chiamato direttamente
    da 02_probe_infra.py quando misurazioni.as_hegemony=true: qualunque
    eccezione imprevista (oltre a quelle già gestite da hegemony_per_asn)
    viene comunque contenuta qui, così un problema in questo modulo
    sperimentale non può mai far fallire l'intera riga/il run — coerente
    con lo stile fail-open del resto del progetto."""
    try:
        asn, quota, err = resolver.per_asn(origin_asn)
        return {
            "as_hegemony_dipendenza_principale": asn or "",
            "as_hegemony_quota": round(quota, 3) if quota is not None else "",
            "as_hegemony_errore": err or "",
        }
    except Exception as e:
        return {
            "as_hegemony_dipendenza_principale": "",
            "as_hegemony_quota": "",
            "as_hegemony_errore": f"as_hegemony_{type(e).__name__}",
        }
