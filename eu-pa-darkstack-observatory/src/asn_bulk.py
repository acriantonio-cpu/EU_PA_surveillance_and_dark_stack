#!/usr/bin/env python3
# EN: Bulk IP -> ASN/operator resolution via Team Cymru whois (with RIPEstat and per-IP RDAP
# fallbacks) to avoid RDAP rate limits on large samples; also a per-IP-block connection
# limiter (LimitatorePerBlocco).
"""
asn_bulk.py

OTTIMIZZAZIONE (settembre 2026): risoluzione ASN/operatore in blocco via
Team Cymru whois bulk (whois.cymru.com:43) invece di un lookup RDAP per
ogni singolo IP.

Perché: su un campione grande (centinaia/migliaia di hostname) il lookup
RDAP "uno alla volta" fatto da resolve_asn() in 02_probe_infra.py va in
rate limit molto presto — è la causa documentata della maggioranza degli
errori 'asn_*_lookup_failed' visti in risultati.csv (fino al 55-75% delle
righe su alcuni campioni reali). Il fix non è "aspettare di più tra una
richiesta e l'altra": è non fare più migliaia di richieste separate.

Team Cymru offre un servizio whois bulk pensato esattamente per questo:
una singola connessione TCP, si manda l'intera lista di IP in un colpo
solo, si ottiene ASN/organizzazione/Paese per tutti in un'unica risposta.
Nessuna registrazione richiesta, nessuna API key, protocollo testuale
molto semplice (netcat-friendly). Documentazione del servizio:
https://team-cymru.com/community-services/ip-asn-mapping/

Strategia usata da 02_probe_infra.py (vedi lì la funzione main()):
  1. una prima passata SOLO DNS su tutto il campione raccoglie gli IP da
     risolvere (web/mail/nameserver), senza fare alcun lookup ASN;
  2. UNA sola chiamata a BulkASNResolver.resolve_many() con TUTTI gli IP
     raccolti: parte una manciata di query bulk (una ogni
     CYMRU_MAX_PER_BATCH IP, non una per IP) invece di centinaia/migliaia
     di lookup RDAP singoli;
  3. gli IP che il bulk non è riuscito a risolvere (càpita: IP privati,
     problemi di rete transitori, prefissi non in tabella di routing
     globale) vengono ripescati uno a uno via RDAP — stessa funzione
     resolve_asn() di prima, ma applicata a una minoranza di IP invece
     che alla totalità.

Nota: Team Cymru copre l'ASN "di instradamento" (BGP), che è la stessa
informazione che interessa qui ("chi ospita tecnicamente questo IP"), non
necessariamente l'org RDAP registrata carattere per carattere identica a
quella restituita da un RIR — per questo motivo il fallback RDAP resta
disponibile e viene usato in automatico per tutto ciò che il bulk non sa
rispondere, così il formato dei dati in output (asn_org_a, asn_country_a,
...) resta lo stesso di prima.

Se la porta 43/TCP in uscita è bloccata (proxy aziendali restrittivi,
alcune reti pubbliche), il bulk fallisce silenziosamente e si ricade
sull'RDAP per-IP come faceva lo script originale: nessuna eccezione si
propaga al chiamante. Per tornare esplicitamente al comportamento
originale (RDAP puro, utile per confrontare o per reti dove il bulk non
funziona), impostare `asn_metodo: rdap` in config.yaml.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import socket
import threading
import time

import requests

import errors as _errors

CYMRU_HOST = "whois.cymru.com"
CYMRU_PORT = 43
CYMRU_TIMEOUT_DEFAULT = 15.0
# Prudenziale: una singola richiesta bulk con troppi IP rischia timeout più
# facilmente di qualche richiesta più piccola. 500 è un compromesso, non un
# limite imposto da Cymru (il servizio pubblico non ne documenta uno fisso).
CYMRU_MAX_PER_BATCH = 500
# OTTIMIZZAZIONE (settembre 2026, seconda passata): un batch può fallire per
# un blip di rete transitorio (connessione rifiutata, timeout momentaneo) e
# non per un vero blocco (porta 43 chiusa dalla rete, servizio giù): un
# singolo retry con una breve pausa evita di far ricadere fino a
# CYMRU_MAX_PER_BATCH IP sul fallback RDAP per un problema che si sarebbe
# risolto da solo un secondo dopo.
CYMRU_TENTATIVI_DEFAULT = 2
CYMRU_BACKOFF_SECONDI = 0.8


def prefisso_cache(ip: str):
    """Blocco usato come chiave di cache per il lookup ASN: /24 per IPv4,
    /48 per IPv6 (stessa euristica già usata per RDAP: grandezza tipica di
    un'allocazione, compromesso fra hit-rate della cache e rischio di
    confondere due operatori diversi su blocchi adiacenti). Ritorna None
    se l'IP non è valido (un IP grezzo letto da un record DNS potrebbe non
    esserlo). Funzione condivisa fra RDAP e bulk: 02_probe_infra.py la
    importa da qui invece di duplicarla."""
    try:
        rete = ipaddress.ip_network(ip, strict=False)
        prefisso = 24 if rete.version == 4 else 48
        return str(ipaddress.ip_network(f"{ip}/{prefisso}", strict=False))
    except ValueError:
        return None


def _org_da_cymru(as_name: str, cc: str) -> str:
    """Costruisce l'etichetta organizzazione a partire dai campi Cymru,
    evitando di duplicare il Paese se 'as_name' lo contiene già (Cymru
    restituisce spesso forme tipo 'GOOGLE, US': aggiungere di nuovo '(US)'
    sarebbe ridondante). Resta comunque un formato DIVERSO da quello RDAP
    (che include la ragione sociale, es. 'GOOGLE - Google LLC, US'): è
    un'etichetta leggibile, non un identificativo — 03_analyze.py raggruppa
    gli operatori per ASN NUMERICO (uguale sia da Cymru che da RDAP), non
    per questa stringa, quindi la concentrazione/HHI non ne risente; solo
    l'etichetta mostrata nei report può avere un formato diverso a seconda
    di quale dei due metodi ha risolto quell'operatore."""
    as_name = (as_name or "").strip()
    cc = (cc or "").strip()
    if not as_name:
        return ""
    if cc and cc.upper() not in as_name.upper():
        return f"{as_name} ({cc})"
    return as_name


def _cymru_bulk_query(ips: list, timeout: float = CYMRU_TIMEOUT_DEFAULT,
                       tentativi: int = CYMRU_TENTATIVI_DEFAULT) -> dict:
    """Un'unica connessione TCP bulk verso whois.cymru.com:43. Manda
    'begin', 'verbose' (per avere anche il nome dell'AS e il Paese, non
    solo il numero), un IP per riga, poi 'end'; legge la risposta fino a
    chiusura connessione. Non solleva mai eccezioni verso il chiamante: in
    caso di qualunque problema di rete (dopo aver esaurito i retry) ritorna
    un dict vuoto, così chi chiama fa automaticamente fallback su RDAP per
    tutti gli IP di questo batch. Ritorna {ip: (asn, org_con_paese, country_code)}.

    Riprova fino a 'tentativi' volte con una breve pausa (CYMRU_BACKOFF_SECONDI
    * numero del tentativo) prima di arrendersi: un blip di rete transitorio
    non deve far ricadere l'intero batch sul fallback RDAP-per-IP, che è
    proprio il collo di bottiglia che questo modulo vuole evitare."""
    if not ips:
        return {}
    richiesta = "begin\nverbose\n" + "\n".join(ips) + "\nend\n"

    testo = None
    for tentativo in range(1, max(1, tentativi) + 1):
        try:
            with socket.create_connection((CYMRU_HOST, CYMRU_PORT), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(richiesta.encode("ascii", errors="ignore"))
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                pezzi = []
                while True:
                    pezzo = sock.recv(65536)
                    if not pezzo:
                        break
                    pezzi.append(pezzo)
            testo = b"".join(pezzi).decode("utf-8", errors="replace")
            break
        except OSError:
            if tentativo < tentativi:
                time.sleep(CYMRU_BACKOFF_SECONDI * tentativo)
            else:
                return {}

    if testo is None:
        return {}

    # Formato risposta 'verbose' (pipe-delimited), una riga di intestazione
    # seguita da una riga per IP:
    #   AS | IP | BGP Prefix | CC | Registry | Allocated | AS Name
    # Un IP non trovato in tabella di routing torna con AS = "NA".
    risultati = {}
    for riga in testo.splitlines():
        riga = riga.strip()
        if not riga or "|" not in riga:
            continue
        campi = [c.strip() for c in riga.split("|")]
        if len(campi) < 7:
            continue
        asn, ip, _prefix, cc, _registry, _allocated, as_name = campi[:7]
        if asn.upper() in ("AS", "NA", ""):
            continue  # riga di intestazione, o IP non risolto da Cymru
        risultati[ip] = (asn, _org_da_cymru(as_name, cc), cc if cc and cc != "NA" else "")
    return risultati


# --------------------------------------------------------------------------
# OTTIMIZZAZIONE (seconda passata, settembre 2026): fallback RIPEstat.
#
# Fra "bulk Team Cymru" (veloce, ma non un servizio ufficiale dei registri)
# e "RDAP per-IP" (autorevole, ma un lookup alla volta e soggetto a rate
# limit sui registri) manca una via di mezzo: RIPEstat, l'API pubblica di
# RIPE NCC. Copre bene proprio l'area geografica di questo progetto (RIPE
# NCC è il RIR di Europa/Medio Oriente/Asia centrale, quindi UE = quasi
# sempre dati "propri" per RIPE NCC, non un mirror di un altro RIR), non
# richiede API key, e i suoi endpoint 'network-info' e 'as-overview' non
# hanno un rate limit pubblicato aggressivo come l'RDAP per-IP dei singoli
# registri (fonte: documentazione RIPEstat Data API, stat.ripe.net/docs/).
#
# Due chiamate per IP (a differenza del bulk Cymru, che ne fa una sola per
# batch): 1) network-info risolve IP -> lista ASN annuncianti + prefisso;
# 2) as-overview risolve ASN -> nome descrittivo dell'holder (stessa idea
# della 'org' RDAP). Per questo resta un FALLBACK per la minoranza di IP che
# il bulk Cymru non risolve, non un sostituto del bulk per l'intero
# campione: chiamarlo per ogni IP del campione perderebbe il vantaggio del
# bulk (poche richieste totali).
#
# ATTENZIONE (dichiarato esplicitamente, stesso spirito di AUDIT_IMPLEMENTAZIONE.md):
# la logica di parsing qui sotto è stata verificata contro un mock HTTP
# locale che riproduce la forma di risposta documentata da RIPEstat (vedi
# test in tests/), NON contro il servizio reale stat.ripe.net — l'ambiente
# in cui questo codice è stato scritto non ha accesso alla rete pubblica.
# Prima di un run massivo, verifica con --limit 20 e controlla la colonna
# 'errori' per eventuali 'ripestat_*_failed' imprevisti.
RIPESTAT_BASE_URL_DEFAULT = "https://stat.ripe.net"
RIPESTAT_TIMEOUT_DEFAULT = 6.0

# Pattern per estrarre un codice Paese ISO a due lettere maiuscole da una
# stringa 'holder' RIPEstat quando presente (es. "GOOGLE-AS - Google LLC, US"
# o "AMAZON-02, US"): non tutti gli holder ce l'hanno (dipende da come il
# registro l'ha registrato), in quel caso country resta vuoto — stesso
# comportamento "meglio vuoto che inventato" del resto del progetto.
_PATTERN_COUNTRY_HOLDER = re.compile(r",\s*([A-Z]{2})\s*$")


def _org_e_country_da_holder(holder: str):
    """Estrae (org, country) da un 'holder' RIPEstat. Se non trova un
    codice Paese a fine stringa, ritorna country=''. Non lancia mai
    eccezioni: un formato inatteso ritorna semplicemente (holder, '')."""
    holder = (holder or "").strip()
    if not holder:
        return "", ""
    m = _PATTERN_COUNTRY_HOLDER.search(holder)
    if m:
        return holder, m.group(1)
    return holder, ""


def resolve_asn_ripestat(ip: str, timeout: float = RIPESTAT_TIMEOUT_DEFAULT,
                          base_url: str = RIPESTAT_BASE_URL_DEFAULT,
                          rate_limiter=None, session: "requests.Session | None" = None):
    """Fallback RIPEstat: due chiamate GET (network-info, poi as-overview),
    entrambe JSON, nessuna API key. Ritorna (asn, org, country, errore) con
    la STESSA firma di resolve_asn() (02_probe_infra.py) e della tupla
    ritornata dal bulk Cymru, così il chiamante (BulkASNResolver più sotto)
    può trattarle in modo intercambiabile.

    'rate_limiter' (opzionale, vedi rate_limiter.LimitatoreVelocita): se
    passato, attende un token prima di OGNI chiamata HTTP — condiviso fra
    thread, per non generare fino a --concorrenza richieste simultanee
    verso lo stesso servizio (vedi rate_limiter.py per il perché).

    Non lancia mai eccezioni verso il chiamante: qualunque problema (rete,
    JSON malformato, campi mancanti) ritorna (None, None, None, nome_errore),
    così il chiamante ricade sul fallback successivo (RDAP per-IP) esattamente
    come farebbe se questo fallback non esistesse."""
    if not ip:
        return None, None, None, None
    http = session or requests
    try:
        if rate_limiter is not None:
            rate_limiter.attendi()
        r1 = http.get(
            f"{base_url}/data/network-info/data.json",
            params={"resource": ip}, timeout=timeout,
        )
        r1.raise_for_status()
        dati1 = (r1.json() or {}).get("data") or {}
        asns = dati1.get("asns") or []
        if not asns:
            return None, None, None, "ripestat_no_asn"
        asn = str(asns[0]).strip()
        if not asn:
            return None, None, None, "ripestat_no_asn"

        if rate_limiter is not None:
            rate_limiter.attendi()
        r2 = http.get(
            f"{base_url}/data/as-overview/data.json",
            params={"resource": f"AS{asn}"}, timeout=timeout,
        )
        r2.raise_for_status()
        dati2 = (r2.json() or {}).get("data") or {}
        holder = dati2.get("holder")
        org, country = _org_e_country_da_holder(holder) if holder else ("", "")
        return asn, org, country, None
    except Exception as e:
        return None, None, None, f"ripestat_{type(e).__name__}"


class LimitatorePerBlocco:
    """Limita quante richieste HTTP/TLS simultanee vengono fatte verso lo
    STESSO blocco IP (/24 IPv4, /48 IPv6 — stessa euristica di
    prefisso_cache()), indipendentemente dal grado di concorrenza globale
    (--concorrenza in 02_probe_infra.py).

    Perché serve: --concorrenza limita quanti HOSTNAME si sondano in
    parallelo, ma diversi hostname possono condividere lo stesso backend
    fisico — un consorzio di hosting per piccoli comuni è il caso tipico
    (vedi README, un singolo operatore può ospitare oltre il 30% di un
    campione). Senza questo limite, un batch di hostname che ricadono tutti
    sullo stesso server può bombardarlo con --concorrenza connessioni
    simultanee: esattamente il comportamento aggressivo che un
    "osservatorio che si dichiara onestamente" (vedi commento su
    http_user_agent in config.yaml) vuole evitare.

    Uso: 'with limitatore.limita(ip): ...' attorno alla chiamata di rete
    verso quell'IP. Se ip è None/vuoto (hostname senza IP noto), non limita
    nulla."""

    def __init__(self, max_per_blocco: int = 3):
        self._max = max(1, max_per_blocco)
        self._semafori = {}
        self._lock = threading.Lock()

    def limita(self, ip: str):
        if not ip:
            return contextlib.nullcontext()
        prefisso = prefisso_cache(ip) or ip
        with self._lock:
            sem = self._semafori.get(prefisso)
            if sem is None:
                sem = threading.Semaphore(self._max)
                self._semafori[prefisso] = sem
        return sem


class BulkASNResolver:
    """Risolutore ASN con cache /24-/48 condivisa, che preferisce il bulk
    Team Cymru e ripiega su RDAP (una funzione 'resolve_asn_fallback'
    passata dal chiamante, tipicamente resolve_asn() di 02_probe_infra.py)
    solo per ciò che il bulk non sa risolvere.

    Espone due modalità d'uso:
      - resolve_many(ips): pensata per il caso comune di 02_probe_infra.py,
        dove SI CONOSCONO IN ANTICIPO tutti gli IP di un intero campione.
        Fa partire il minor numero possibile di query bulk (una ogni
        CYMRU_MAX_PER_BATCH IP distinti), poi il fallback RDAP solo per i
        pochi IP rimasti scoperti. Da chiamare UNA VOLTA con la lista
        completa, non IP per IP (altrimenti si perde il vantaggio del
        bulk: vedi resolve_one() per quel caso).
      - resolve_one(ip): un IP alla volta, con la stessa firma di
        resolve_asn_cached() nella versione precedente dello script. Utile
        per 05_scrape_dark_stack.py, che risolve pochi domini terzi per
        volta e non ha comunque a disposizione l'intera lista in anticipo.
    """

    def __init__(self, resolve_asn_fallback, batch_size: int = CYMRU_MAX_PER_BATCH,
                 timeout: float = CYMRU_TIMEOUT_DEFAULT, usa_bulk: bool = True,
                 resolve_asn_ripestat=None, retry_fallback: bool = True):
        self._fallback = resolve_asn_fallback
        self._batch_size = max(1, batch_size)
        self._timeout = timeout
        self._usa_bulk = usa_bulk
        # OTTIMIZZAZIONE (seconda passata): livello intermedio opzionale fra
        # il bulk Cymru e l'RDAP per-IP finale (vedi resolve_asn_ripestat()
        # sopra per il perché). None (default) = comportamento identico a
        # prima, nessun cambiamento per chi non lo passa esplicitamente.
        self._ripestat = resolve_asn_ripestat
        # OTTIMIZZAZIONE (seconda passata): il fallback finale (tipicamente
        # RDAP per-IP, resolve_asn() in 02_probe_infra.py) oggi ha un solo
        # tentativo. Se retry_fallback=True (default), lo si avvolge con
        # errors.ritenta_con_backoff — un timeout/rate-limit transitorio sul
        # SINGOLO IP che arriva fin qui (già la minoranza non risolta né dal
        # bulk né da RIPEstat) ha così una seconda possibilità prima di
        # essere marcato definitivamente fallito.
        self._fallback_con_retry = (
            (lambda ip: _errors.ritenta_con_backoff(self._fallback, ip, tentativi=3))
            if retry_fallback else self._fallback
        )
        self.cache = {}  # prefisso (/24 o /48) -> (asn, org, country, errore)
        self.stat_bulk_ok = 0
        self.stat_ripestat_ok = 0
        self.stat_fallback_rdap = 0
        self.stat_fallback_errori = 0

    def _risolvi_non_bulk(self, ip: str):
        """Prova RIPEstat (se configurato), poi il fallback finale con
        retry. Usata sia da resolve_many() che da resolve_one() per non
        duplicare la stessa sequenza due volte."""
        if self._ripestat is not None:
            asn, org, country, err = self._ripestat(ip)
            if asn:
                self.stat_ripestat_ok += 1
                return asn, org, country, None
        asn, org, country, err = self._fallback_con_retry(ip)
        self.stat_fallback_rdap += 1
        if err:
            self.stat_fallback_errori += 1
        return asn, org, country, err

    def tasso_successo_bulk(self):
        """Frazione di lookup risolti dal bulk Cymru rispetto al totale
        tentato (bulk riuscito + fallback RDAP), o None se non è ancora
        stato tentato nulla. Usata da 02_probe_infra.py per il 'canary
        check' a inizio run: se il bulk sta fallendo quasi sempre (porta 43
        bloccata, servizio giù), è meglio avvisare l'utente subito invece
        di scoprirlo a fine run guardando le statistiche finali."""
        # OTTIMIZZAZIONE (seconda passata): il denominatore include ora
        # anche stat_ripestat_ok, non solo stat_fallback_rdap — altrimenti
        # questo tasso migliorerebbe artificialmente ogni volta che
        # RIPEstat assorbe un lookup che prima sarebbe finito nel conteggio
        # RDAP, senza che il bulk Cymru stia realmente andando meglio.
        totale = self.stat_bulk_ok + self.stat_ripestat_ok + self.stat_fallback_rdap
        if totale == 0:
            return None
        return self.stat_bulk_ok / totale

    def resolve_many(self, ips) -> None:
        """Popola self.cache per TUTTI gli IP passati (iterabile, anche con
        ripetizioni: gli IP nello stesso blocco /24 o /48 contano come uno
        solo). Non ritorna nulla: dopo la chiamata, resolve_one() per
        ciascuno di questi IP è quasi sempre un cache hit istantaneo."""
        ip_rappresentativo_per_prefisso = {}
        for ip in ips:
            if not ip:
                continue
            prefisso = prefisso_cache(ip)
            if prefisso is None or prefisso in self.cache:
                continue
            ip_rappresentativo_per_prefisso.setdefault(prefisso, ip)

        ip_pendenti = list(ip_rappresentativo_per_prefisso.values())

        if self._usa_bulk:
            for i in range(0, len(ip_pendenti), self._batch_size):
                batch = ip_pendenti[i:i + self._batch_size]
                trovati = _cymru_bulk_query(batch, timeout=self._timeout)
                for ip in batch:
                    prefisso = prefisso_cache(ip)
                    if ip in trovati:
                        asn, org, country = trovati[ip]
                        self.cache[prefisso] = (asn, org, country, None)
                        self.stat_bulk_ok += 1

        # RIPEstat poi RDAP solo per i prefissi che il bulk non ha risolto
        # (bulk disattivato, IP non trovato, porta 43 bloccata dalla rete...).
        for prefisso, ip in ip_rappresentativo_per_prefisso.items():
            if prefisso in self.cache:
                continue
            asn, org, country, err = self._risolvi_non_bulk(ip)
            self.cache[prefisso] = (asn, org, country, err)

    def resolve_one(self, ip: str):
        """Un IP alla volta: cache hit se già risolto da resolve_many() (o
        da una chiamata precedente a resolve_one() sullo stesso blocco),
        altrimenti un mini-batch bulk di un solo IP, poi fallback RDAP.
        Ritorna (asn, org, country, errore), stessa firma della vecchia
        resolve_asn_cached()."""
        if not ip:
            return None, None, None, None
        prefisso = prefisso_cache(ip)
        if prefisso is not None and prefisso in self.cache:
            return self.cache[prefisso]

        risultato = None
        if self._usa_bulk:
            trovati = _cymru_bulk_query([ip], timeout=self._timeout)
            if ip in trovati:
                asn, org, country = trovati[ip]
                risultato = (asn, org, country, None)
                self.stat_bulk_ok += 1
        if risultato is None:
            risultato = self._risolvi_non_bulk(ip)

        if prefisso is not None:
            self.cache[prefisso] = risultato
        return risultato
