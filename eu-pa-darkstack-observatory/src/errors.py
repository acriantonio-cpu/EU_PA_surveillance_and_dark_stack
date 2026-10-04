#!/usr/bin/env python3
# EN: Shared helpers: map exception names to a small canonical error taxonomy (rate_limited,
# timeout, connection refused, DNS not resolved, TLS failed, HTTP blocked, unknown) and a
# generic retry/backoff.
"""
errors.py

OTTIMIZZAZIONE (seconda passata, settembre 2026): due utility condivise fra
gli script della pipeline, estratte qui per non duplicarle:

1. classifica_errore(nome_eccezione, dettaglio) — dato il nome di una classe
   di eccezione Python (quello già scritto in colonne come 'errori', es.
   'HTTPLookupError', 'Timeout', 'ConnectionError'...) ritorna un CODICE
   CANONICO piccolo e stabile (rate_limited, timeout, connessione_rifiutata,
   dns_non_risolto, tls_fallito, bloccato_http, sconosciuto).

   Perché serve, dato che 'errori' distingue già per CAMPO (es.
   'asn_a_lookup_failed' vs 'tls_failed'): il campo dice DOVE è fallita la
   misurazione, non PERCHÉ. Due campi diversi possono fallire per la STESSA
   ragione di fondo (rate limit di un registro RDAP che colpisce sia
   'asn_a_lookup_failed' che 'asn_mx_lookup_failed'), e la stessa eccezione
   Python può comparire con nomi leggermente diversi a seconda della
   libreria (ipwhois.HTTPLookupError vs requests.exceptions.Timeout vs
   dns.exception.Timeout sono tutte, di fatto, 'timeout'). Raggruppare per
   causa canonica invece che per nome-di-classe rende il riepilogo in
   03_analyze.py (summarize_errori) utile per decidere COSA sistemare per
   primo, non solo quanto è sporco il campione.

   Fail-safe per costruzione: non lancia mai eccezioni, un nome non
   riconosciuto ritorna 'sconosciuto' invece di far fallire l'analisi.

2. ritenta_con_backoff(fn, ip_o_argomento, tentativi, backoff_base) —
   wrapper generico di retry con backoff esponenziale + jitter, per
   funzioni di lookup che oggi non ne hanno nessuno (es. resolve_asn() in
   02_probe_infra.py, il fallback RDAP finale: oggi un solo tentativo, un
   timeout transitorio consuma un lookup senza nessuna seconda possibilità
   — diverso da asn_bulk._cymru_bulk_query(), che il retry ce l'ha già).

   Riprova SOLO sugli errori canonici genuinamente transitori (timeout,
   rate_limited, connessione_rifiutata): un errore come 'IPDefinedError'
   (IP privato/riservato, non risolvibile per definizione) o
   'ASNRegistryError' (il registro RDAP competente non ce l'ha) non
   migliora riprovando, e riprovare sprecherebbe solo tempo — la funzione
   passata deve ritornare (risultato, nome_eccezione_o_None) con la stessa
   convenzione già usata da resolve_asn() e query(), così ritenta_con_backoff
   può decidere se vale la pena riprovare guardando nome_eccezione.
"""

from __future__ import annotations

import random
import time

# --------------------------------------------------------------------------
# 1. Classificazione canonica
# --------------------------------------------------------------------------

# Ogni voce: (sottostringa cercata case-insensitive nel nome eccezione o nel
# dettaglio, codice canonico). Ordine rilevante: la prima che matcha vince,
# quindi le sottostringhe più specifiche vanno prima di quelle generiche.
_REGOLE_CLASSIFICAZIONE = [
    ("ratelimit", "rate_limited"),
    ("rate_limit", "rate_limited"),
    ("too many requests", "rate_limited"),
    ("429", "rate_limited"),
    ("timeout", "timeout"),
    ("timed out", "timeout"),
    ("connectionrefused", "connessione_rifiutata"),
    ("connectionreset", "connessione_rifiutata"),
    ("connectionerror", "connessione_rifiutata"),
    ("connection refused", "connessione_rifiutata"),
    ("nxdomain", "dns_non_risolto"),
    ("noanswer", "dns_non_risolto"),
    ("nonameservers", "dns_non_risolto"),
    ("sslerror", "tls_fallito"),
    ("sslcertverificationerror", "tls_fallito"),
    ("handshake", "tls_fallito"),
    ("certificateerror", "tls_fallito"),
    ("ipdefinederror", "ip_non_pubblico"),  # IP privato/riservato: non è un errore di rete, è atteso
    ("asnregistryerror", "registro_asn_sconosciuto"),
    ("httplookuperror", "lookup_http_fallito"),
    ("403", "bloccato_http"),
    ("406", "bloccato_http"),
    ("503", "bloccato_http"),
    ("forbidden", "bloccato_http"),
]

# Codici canonici per cui ritenta_con_backoff() ritiene valga la pena
# riprovare: un problema plausibilmente transitorio. Gli altri (ip privato,
# registro sconosciuto, DNS che risponde 'non esiste' in modo pulito...)
# non migliorano riprovando.
CODICI_RITENTABILI = {"rate_limited", "timeout", "connessione_rifiutata", "lookup_http_fallito"}


def classifica_errore(nome_eccezione: str, dettaglio: str = "") -> str:
    """Ritorna un codice canonico stabile a partire dal nome di una classe
    di eccezione (e, opzionalmente, un dettaglio testuale aggiuntivo, es. il
    corpo di una risposta HTTP). Non lancia mai eccezioni."""
    try:
        testo = f"{nome_eccezione or ''} {dettaglio or ''}".lower()
    except Exception:
        return "sconosciuto"
    if not testo.strip():
        return "sconosciuto"
    for sottostringa, codice in _REGOLE_CLASSIFICAZIONE:
        if sottostringa in testo:
            return codice
    return "sconosciuto"


# --------------------------------------------------------------------------
# 2. Retry con backoff esponenziale + jitter
# --------------------------------------------------------------------------

def ritenta_con_backoff(fn, argomento, tentativi: int = 3, backoff_base: float = 0.5,
                         backoff_max: float = 8.0):
    """Chiama fn(argomento), che deve ritornare una tupla la cui ULTIMA
    posizione è 'errore' (None se andata bene, altrimenti il nome della
    classe di eccezione — stessa convenzione di resolve_asn()/query() in
    02_probe_infra.py). Se l'errore è in CODICI_RITENTABILI (vedi sopra),
    riprova fino a 'tentativi' volte con backoff esponenziale (backoff_base
    * 2**tentativo, con jitter casuale +/-30% per non sincronizzare più
    thread sullo stesso ritmo, e un tetto backoff_max per non far esplodere
    l'attesa su tentativi tardivi). Ritorna il risultato dell'ULTIMO
    tentativo (riuscito o no) — mai un'eccezione propagata verso il
    chiamante, coerente con lo stile fail-open del resto del progetto."""
    risultato = fn(argomento)
    for tentativo in range(1, max(1, tentativi)):
        errore = risultato[-1] if isinstance(risultato, tuple) else None
        if not errore or classifica_errore(errore) not in CODICI_RITENTABILI:
            break
        attesa = min(backoff_max, backoff_base * (2 ** (tentativo - 1)))
        attesa *= random.uniform(0.7, 1.3)  # jitter: evita che molti thread ritentino tutti insieme
        time.sleep(attesa)
        risultato = fn(argomento)
    return risultato
