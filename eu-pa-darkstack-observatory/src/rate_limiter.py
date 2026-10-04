#!/usr/bin/env python3
"""
rate_limiter.py

OTTIMIZZAZIONE (seconda passata, settembre 2026): un token bucket semplice,
thread-safe, per mettere un tetto esplicito a "quante richieste al secondo"
verso un servizio esterno condiviso fra più thread.

Perché serve, dato che esiste già LimitatorePerBlocco in asn_bulk.py: quel
limitatore protegge un singolo OPERATORE OSPITATO (es. un consorzio di
hosting comunale) da troppe connessioni HTTP/TLS simultanee — è per-IP-block,
non per il servizio a monte. Il fallback RDAP-per-IP e il nuovo fallback
RIPEstat (vedi asn_bulk.py) invece parlano con UN servizio esterno condiviso
da TUTTI gli host del campione (rdap.db.ripe.net, stat.ripe.net...): con
--concorrenza N thread, senza un limite qui, si possono generare fino a N
richieste concorrenti verso quello stesso servizio, che è esattamente il
pattern che genera 'HTTPRateLimitError' già documentato nei commenti di
asn_bulk.py come causa principale di errori RDAP su un campione grande.

Uso:
    limitatore = LimitatoreVelocita(richieste_al_secondo=5)
    ...
    with limitatore:
        risposta = requests.get(url)

Implementazione: token bucket con capienza = richieste_al_secondo (permette
un piccolo burst iniziale, poi il ritmo si stabilizza), non una coda a
finestra fissa — più semplice da ragionare con più thread e non penalizza
un thread che arriva 'in anticipo' rispetto al secondo esatto.
"""

from __future__ import annotations

import threading
import time


class LimitatoreVelocita:
    """Token bucket condiviso fra thread. richieste_al_secondo <= 0 disattiva
    del tutto il limite (ogni chiamata passa subito) — utile per non dover
    if/else altrove quando il rate limiting è disattivato da config."""

    def __init__(self, richieste_al_secondo: float = 5.0):
        self._tasso = max(0.0, float(richieste_al_secondo))
        self._capienza = max(1.0, self._tasso)
        self._token = self._capienza
        self._ultimo_refill = time.monotonic()
        self._lock = threading.Lock()

    def attendi(self) -> None:
        """Blocca il thread chiamante finché non è disponibile un token.
        No-op immediato se il rate limiting è disattivato (tasso <= 0)."""
        if self._tasso <= 0:
            return
        while True:
            with self._lock:
                ora = time.monotonic()
                trascorso = ora - self._ultimo_refill
                self._ultimo_refill = ora
                self._token = min(self._capienza, self._token + trascorso * self._tasso)
                if self._token >= 1.0:
                    self._token -= 1.0
                    return
                attesa = (1.0 - self._token) / self._tasso
            time.sleep(max(0.0, attesa))

    def __enter__(self):
        self.attendi()
        return self

    def __exit__(self, exc_type, exc, tb):
        return False
