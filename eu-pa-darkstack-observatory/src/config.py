#!/usr/bin/env python3
"""
config.py

Carica il file di configurazione unico del progetto (config.yaml nella
project root) e lo espone a tutti gli script della pipeline: flag
"misurazioni" (True/False) + timeout/delay di default.

Il config.yaml è la SORGENTE DI DEFAULT, non un vincolo assoluto: ogni
script continua ad accettare i propri argomenti da riga di comando (es.
--timeout, --skip-http) che, se passati esplicitamente, hanno la precedenza
sul file. Se config.yaml manca del tutto (repository clonata senza il file,
o percorso custom passato altrove) si torna ai DEFAULTS qui sotto, che sono
gli stessi valori del config.yaml di esempio (tutti i flag a True): uno
script non si rifiuta mai di partire per colpa della configurazione.

Non è un modulo eseguibile: viene importato dagli altri script (funziona
perché la cartella dello script chiamante è in sys.path[0] quando lo lanci
con 'python src\\02_probe_infra.py ...', quindi 'import config' trova questo
file senza bisogno di installare il progetto come pacchetto).
"""

from pathlib import Path
from urllib.parse import urlsplit
import sys

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

DEFAULTS = {
    "paese": "IT",
    "http_user_agent": None,  # None = usa il default hard-coded nello script (vedi DEFAULT_USER_AGENT in 02_probe_infra.py)
    # OTTIMIZZAZIONE (settembre 2026): metodo di risoluzione ASN/operatore.
    # 'cymru_bulk' (default) raggruppa i lookup in poche query bulk verso
    # Team Cymru invece di un lookup RDAP per IP — vedi src/asn_bulk.py per
    # il perché (in breve: l'RDAP per-IP va in rate limit molto presto su
    # campioni grandi). 'rdap' torna al comportamento originale (un lookup
    # RDAP per IP, nessun bulk), utile per confronto o su reti dove la
    # porta 43/TCP in uscita è bloccata.
    "asn_metodo": "cymru_bulk",
    # Numero massimo di hostname sondati in parallelo nella fase DNS e nella
    # fase TLS/HTTP di 02_probe_infra.py (vedi src/asn_bulk.py per l'ASN,
    # risolto separatamente in blocco). Più alto = più veloce ma più
    # aggressivo verso i siti sondati; 15-20 è un compromesso ragionevole
    # per un osservatorio che si dichiara onestamente (vedi http_user_agent
    # sopra), non per una scansione massiva.
    "concorrenza": 15,
    # OTTIMIZZAZIONE (settembre 2026, seconda passata): tetto massimo di
    # connessioni TLS/HTTP simultanee verso lo STESSO blocco IP (/24 IPv4,
    # /48 IPv6), indipendente dal grado di concorrenza globale sopra. Vedi
    # asn_bulk.LimitatorePerBlocco per il perché: un singolo operatore può
    # ospitare una quota rilevante del campione (consorzi di hosting per
    # piccoli comuni), e senza questo limite finirebbe colpito da fino a
    # 'concorrenza' connessioni tutte insieme.
    "concorrenza_per_provider": 3,
    # OTTIMIZZAZIONE (seconda passata, settembre 2026): fallback RIPEstat fra
    # il bulk Cymru e l'RDAP per-IP finale — vedi src/asn_bulk.py,
    # resolve_asn_ripestat(), per il perché. Attivo di default: è puramente
    # ADDITIVO (si prova solo per i pochi IP che il bulk non risolve, e
    # qualunque errore ricade comunque sull'RDAP di prima), quindi il
    # comportamento peggiore possibile con questo flag a True è identico a
    # prima (RIPEstat irraggiungibile -> fallback RDAP come sempre).
    "asn_ripestat_abilitato": True,
    # Tetto di richieste al secondo verso i servizi RDAP/RIPEstat CONDIVISI
    # (non il bulk Cymru, che ha già il suo protocollo a parte): vedi
    # src/rate_limiter.py. 0 o negativo disattiva il limite.
    "asn_rate_limit_per_secondo": 5,
    "misurazioni": {
        "dns_base": True,
        "asn_hosting": True,
        "tls_base": True,
        "tls_avanzato": True,
        "http_headers_cdn": True,
        "tempi_risposta": True,
        "dark_stack_terze_parti": True,
        "cookie_tracker": True,
        "cms_fingerprint": True,
        # OTTIMIZZAZIONE (seconda passata): concentrazione dei domini terzi
        # per organizzazione madre (Google, Meta, Cloudflare, Microsoft...),
        # calcolata da 03_analyze.py sui dati GIÀ raccolti da
        # dark_stack_terze_parti — nessuna richiesta di rete aggiuntiva,
        # quindi resta True di default anche quando si vuole un giro veloce.
        "tracker_hhi": True,
        # SPERIMENTALE (seconda passata): dipendenza dall'AS di transito via
        # RIPEstat AS-hegemony. Default False perché (a) richiede una
        # chiamata di rete aggiuntiva per IP non ancora presente nella
        # cache primaria, e (b) non è stato possibile verificarla contro il
        # servizio reale in questo ambiente (vedi src/as_hegemony.py) — va
        # attivata consapevolmente, non di default.
        "as_hegemony": False,
        # TERZA PASSATA (integrazione estensioni, settembre 2026): probe
        # dinamici di ridondanza (07_resilience.py) — interroga OGNI server
        # NS dichiarato separatamente e testa OGNI indirizzo A/AAAA
        # pubblicato (non solo il primo, come fa 02). Default False perché
        # aggiunge transazioni TCP/TLS in più oltre a quelle già fatte da
        # 02_probe_infra.py per lo stesso hostname: va attivata
        # consapevolmente, non di default, coerente con l'approccio già
        # usato per as_hegemony sopra.
        "resilienza_dinamica": False,
        # Composizione dell'indice sintetico 0-100 in 03_analyze.py (vedi
        # summarize_indice_dipendenza_tecnologica): combina SOLO le
        # dimensioni già calcolate altrove nel report (hosting_dns,
        # tracker_concentrazione_hhi, software_licenze) più resilience.csv
        # se presente. Nessuna richiesta di rete propria: gratis se le
        # misurazioni sottostanti sono già attive, vuoto altrimenti.
        "indice_dipendenza_tecnologica": True,
        # QUARTA PASSATA (settembre 2026): classificazione del tipo di ente
        # (comune/scuola/ospedale/...) dal testo della homepage, in tutte le
        # 24 lingue UE — vedi 08_classifica_tipo_ente.py e
        # data/rules/tipo_ente_rules.yaml. Default True come 05/06 (stesso
        # ordine di costo: un fetch completo per hostname).
        "tipo_ente_classificazione": True,
        # QUARTA PASSATA (settembre 2026, aggiunta su richiesta): file di
        # dettaglio per-risorsa da 05_scrape_dark_stack.py (URL esatta in cui
        # ogni dominio di terza parte è stato trovato, per la verifica
        # manuale di link sospetti/pagine compromesse) + il riepilogo
        # terze_parti_da_verificare.csv/.md. GRATIS se dark_stack_terze_parti
        # è già attivo: nessuna richiesta di rete propria, solo scrittura di
        # dati già calcolati e prima scartati dopo l'aggregazione per
        # hostname. Default True.
        "terze_parti_dettaglio_link": True,
    },
    "timeout_secondi": {
        "dns": 5.0,
        "http": 6.0,
        "tls": 5.0,
        "tls_avanzato": 5.0,
        "dark_stack_http": 8.0,
        "dark_stack_dns": 5.0,
        "cms_fingerprint_http": 8.0,
        "resilienza_dns": 5.0,
        "resilienza_tcp": 5.0,
        "tipo_ente_classificazione_http": 8.0,
    },
    "delay_secondi": {
        "probe_infra": 0.5,
        "dark_stack": 0.5,
        "cms_fingerprint": 0.5,
        "tipo_ente_classificazione": 0.5,
    },
}


def _merge(base: dict, override: dict) -> dict:
    """Merge ricorsivo: le chiavi presenti in override vincono, quelle
    assenti restano ai DEFAULTS (così aggiungere una nuova misurazione in
    futuro non richiede di riscrivere tutti i config.yaml già in giro)."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None) -> dict:
    """Carica config.yaml (o il percorso passato in 'path'). Ritorna sempre
    un dict completo (merge con DEFAULTS), mai un errore se il file manca."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    with open(cfg_path, encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    return _merge(DEFAULTS, user_cfg)


def flag(cfg: dict, nome: str) -> bool:
    """Valore di un flag sotto 'misurazioni'. True se assente (fail-open:
    una voce non ancora prevista nel config.yaml dell'utente resta attiva)."""
    return bool(cfg.get("misurazioni", {}).get(nome, True))


def timeout_di(cfg: dict, nome: str, fallback: float) -> float:
    return float(cfg.get("timeout_secondi", {}).get(nome, fallback))


def delay_di(cfg: dict, nome: str, fallback: float) -> float:
    return float(cfg.get("delay_secondi", {}).get(nome, fallback))


def user_agent_di(cfg: dict, fallback: str) -> str:
    """Ritorna http_user_agent da config.yaml, o 'fallback' se non
    impostato/vuoto (None o stringa vuota contano come 'non impostato')."""
    valore = cfg.get("http_user_agent")
    return valore if valore else fallback


def asn_metodo_di(cfg: dict) -> str:
    """'cymru_bulk' (default) o 'rdap'. Qualunque altro valore viene
    trattato come 'cymru_bulk' (fail-safe verso l'opzione più veloce/robusta)."""
    valore = str(cfg.get("asn_metodo", "cymru_bulk")).strip().lower()
    return "rdap" if valore == "rdap" else "cymru_bulk"


def concorrenza_di(cfg: dict, fallback: int = 15) -> int:
    try:
        n = int(cfg.get("concorrenza", fallback))
    except (TypeError, ValueError):
        n = fallback
    return max(1, n)


def concorrenza_per_provider_di(cfg: dict, fallback: int = 3) -> int:
    try:
        n = int(cfg.get("concorrenza_per_provider", fallback))
    except (TypeError, ValueError):
        n = fallback
    return max(1, n)


def asn_ripestat_abilitato_di(cfg: dict) -> bool:
    return bool(cfg.get("asn_ripestat_abilitato", True))


def asn_rate_limit_di(cfg: dict, fallback: float = 5.0) -> float:
    try:
        return float(cfg.get("asn_rate_limit_per_secondo", fallback))
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------------
# ROBUSTEZZA (auto-normalizzazione hostname, settembre 2026).
#
# La colonna 'hostname' di siti.csv deve contenere un dominio NUDO (es.
# 'comune.fr'), non un URL completo — vedi 01_fetch_ipa_comuni.py, che tiene
# l'URL originale in una colonna a parte e mette solo il dominio in
# 'hostname'. Un siti.csv preparato a mano per un Paese diverso da IT (vedi
# run_pipeline.py, che per --paese != IT si aspetta che tu lo prepari
# altrove) può facilmente avere URL interi in questa colonna: senza questa
# normalizzazione, ogni query DNS/TLS/HTTP tenta di risolvere una stringa
# tipo 'https://www.comune.fr/pagina' come se fosse un hostname, fallendo
# con NXDOMAIN/gaierror (o AttributeError se il valore manca del tutto) su
# OGNI riga — un fallimento totale e silenzioso, indistinguibile da "tutti i
# siti sono irraggiungibili" leggendo solo l'output, mentre in realtà
# l'input non era nel formato atteso.
#
# Condivisa (non duplicata) fra 02_probe_infra.py, 05_scrape_dark_stack.py,
# 06_fingerprint_cms.py e 08_classifica_tipo_ente.py: sono i quattro script
# che leggono siti.csv DIRETTAMENTE. 07_resilience.py non ne ha bisogno
# perché legge risultati.csv, già ripulito da 02_probe_infra.py nello stesso
# run (vedi run_pipeline.py).
def normalizza_hostname(raw):
    """Estrae il solo dominio (scheme/porta/path/query/frammento scartati)
    da un valore della colonna 'hostname', es. 'https://www.comune.fr/pagina
    ?x=1' o 'comune.fr/pagina' -> 'www.comune.fr' / 'comune.fr'.

    Ritorna (hostname_pulito, modificato: bool):
    - hostname_pulito è None se il valore è mancante/vuoto o non
      recuperabile come hostname valido (nessun punto = nessun TLD, spazi o
      slash residui dopo l'estrazione): la riga va SCARTATA invece di
      essere sondata alla cieca, vedi normalizza_e_filtra_hostname() sotto.
    - modificato è True se il valore pulito differisce da quello originale
      (solo informativo, per il messaggio riepilogativo a schermo)."""
    if raw is None or (isinstance(raw, float) and raw != raw):  # None o NaN
        return None, False
    s = str(raw).strip()
    if not s:
        return None, False

    if "://" in s:
        host = urlsplit(s).hostname
    elif "/" in s or "?" in s:
        # Nessuno schema esplicito ma c'è comunque un path/query attaccato:
        # urlsplit riconosce l'host solo se la stringa comincia con '//',
        # quindi lo aggiungiamo solo per il parsing, non lo teniamo nel
        # risultato.
        host = urlsplit("//" + s).hostname
    else:
        host = s

    if not host:
        return None, False
    host = host.strip().strip(".").lower()
    if not host or " " in host or "/" in host or "\\" in host or host.count(".") == 0:
        return None, False

    return host, (host != s.lower())


def normalizza_e_filtra_hostname(df_in, infile: str, out_stem: str):
    """Applica normalizza_hostname() alla colonna 'hostname' di un DataFrame
    di input (siti.csv), stampa un riepilogo su stderr e SALVA A PARTE le
    righe con hostname non recuperabile invece di lasciarle nel DataFrame
    (dove produrrebbero errori silenziosi riga per riga in DNS/TLS/HTTP).

    'out_stem' è il percorso di output di QUESTO script SENZA estensione
    (es. str(out_path) con '.csv' tolto): le righe scartate vengono salvate
    in '<out_stem>_scartate_hostname_invalido.csv', accanto all'output
    normale, per la verifica manuale.

    Ritorna (df_pulito, n_normalizzati, n_scartati, scartate_path_o_None) —
    i tre valori numerici/percorso vanno idealmente riportati anche nel
    manifest.json dello script chiamante, per tracciabilità del run."""
    normalizzati = df_in["hostname"].apply(normalizza_hostname)
    puliti = [h for h, _ in normalizzati]
    modificati = [m for _, m in normalizzati]
    n_normalizzati = sum(modificati)

    df_out = df_in.copy()
    df_out["_hostname_pulito"] = puliti
    df_out["_hostname_valido"] = [h is not None for h in puliti]

    scartate = df_out[~df_out["_hostname_valido"]].drop(columns=["_hostname_pulito", "_hostname_valido"])
    n_scartati = len(scartate)

    df_out = df_out[df_out["_hostname_valido"]].copy()
    df_out["hostname"] = df_out.pop("_hostname_pulito")
    df_out = df_out.drop(columns=["_hostname_valido"])

    if n_normalizzati:
        print(
            f"[normalizzazione hostname] {n_normalizzati} valori nella colonna 'hostname' di {infile} "
            "contenevano un URL completo (scheme/porta/path/query) invece di un dominio nudo: "
            "ripuliti automaticamente prima di sondare (es. 'https://www.comune.fr/pagina' -> "
            "'www.comune.fr').",
            file=sys.stderr,
        )

    scartate_path = None
    if n_scartati:
        scartate_path = f"{out_stem}_scartate_hostname_invalido.csv"
        scartate.to_csv(scartate_path, index=False, encoding="utf-8")
        print(
            f"ATTENZIONE: {n_scartati}/{n_scartati + len(df_out)} righe di {infile} hanno un hostname "
            "mancante o non recuperabile come dominio valido: SALTATE (non sondate) e salvate a parte "
            f"in {scartate_path} per la verifica manuale (vanno completate o escluse dal siti.csv di "
            "origine). Non è un errore fatale: lo script continua con le righe rimanenti.",
            file=sys.stderr,
        )

    return df_out, n_normalizzati, n_scartati, scartate_path


# --------------------------------------------------------------------------
# OTTIMIZZAZIONE (seconda passata, settembre 2026): esecuzioni con timestamp.
#
# Prima: ogni script scriveva sempre in data/results/<paese>/<file>,
# sovrascrivendo il run precedente — impossibile costruire uno storico
# (l'osservatorio promette misurazioni ripetute ogni 3-6 mesi, ma il codice
# non conservava i run passati per confrontarli). Ora ogni run scrive in
# data/results/<paese>/<run_id>/<file>, dove run_id di default è la data
# UTC del giorno (AAAA-MM-GG): granularità sufficiente per una cadenza
# trimestrale/semestrale, e comunque compatibile con --resume nello stesso
# giorno (stesso run_id, stesso file, la logica di resume esistente non
# cambia). Un run_id esplicito (--run-id) resta possibile per casi speciali
# (backfill, run di test ripetuti nello stesso giorno senza sovrascrivere).
#
# data/results/<paese>/latest_run.json è un puntatore di comodo (non
# necessario per l'esecuzione della pipeline in sé, che passa run_id
# esplicitamente da uno script all'altro tramite run_pipeline.py): utile per
# chi consulta i risultati da fuori (dashboard, un altro script, una persona
# che apre la cartella) senza dover sapere in anticipo la data del run più
# recente.
def run_id_default() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def aggiorna_puntatore_latest(paese: str, run_id: str) -> None:
    """Scrive/aggiorna data/results/<paese>/latest_run.json. Va chiamata a
    fine run da ogni script che scrive output 'finali' (02, 05, 06, 03, 04):
    idempotente, un run interrotto a metà semplicemente non arriva a
    chiamarla, lasciando il puntatore sull'ultimo run completato con
    successo — comportamento preferibile a un puntatore che indica un run
    parziale. Non lancia mai eccezioni: un problema qui (permessi, disco
    pieno) non deve far fallire lo script che ha già scritto il suo output
    principale."""
    import json as _json
    from datetime import datetime, timezone as _timezone

    try:
        cartella = PROJECT_ROOT / "data" / "results" / paese
        cartella.mkdir(parents=True, exist_ok=True)
        percorso = cartella / "latest_run.json"
        with open(percorso, "w", encoding="utf-8") as f:
            _json.dump(
                {"run_id": run_id, "aggiornato_utc": datetime.now(_timezone.utc).isoformat()},
                f, indent=2, ensure_ascii=False,
            )
    except Exception:
        pass


def leggi_puntatore_latest(paese: str):
    """Ritorna il run_id più recente noto per 'paese', o None se il
    puntatore non esiste ancora (nessun run completato) o è illeggibile.
    Non usata dagli script della pipeline stessi (che si passano run_id
    esplicitamente), pensata per consumer esterni (es. un futuro script di
    dashboard) che vogliono 'l'ultimo dato buono' senza altri argomenti."""
    import json as _json

    try:
        percorso = PROJECT_ROOT / "data" / "results" / paese / "latest_run.json"
        with open(percorso, encoding="utf-8") as f:
            return (_json.load(f) or {}).get("run_id")
    except Exception:
        return None


def backup_se_esiste(path) -> None:
    """PROTEZIONE CHECKPOINT (quarta passata, settembre 2026, su richiesta
    esplicita): ogni script della pipeline (01-09) scrive i propri output
    per paese/run_id in modalità 'w' (sovrascrittura) quando NON gira con
    --resume. Questo è corretto per un run nuovo, ma se si rilancia lo
    STESSO --run-id senza --resume (es. lo stesso giorno, dato che
    run_id_default() è la data odierna) il file precedente per quel livello
    e quel Paese verrebbe silenziosamente sovrascritto — un checkpoint perso
    senza nessun avviso. Questa funzione va chiamata SEMPRE subito prima di
    aprire un file di output in modalità 'w' quando il file esiste già: lo
    rinomina con un suffisso di timestamp invece di lasciarlo sovrascrivere,
    così il checkpoint precedente resta sempre recuperabile su disco. Non fa
    nulla se il file non esiste (caso normale, run_id mai usato prima) e non
    lancia mai eccezioni (un problema qui — permessi, disco pieno — non deve
    impedire allo script di scrivere il suo output nuovo)."""
    import shutil
    from datetime import datetime, timezone as _timezone
    from pathlib import Path as _Path

    try:
        p = _Path(path)
        if not p.exists():
            return
        timbro = datetime.now(_timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = p.with_name(f"{p.stem}_backup-{timbro}{p.suffix}")
        shutil.move(str(p), str(backup_path))
        print(f"[checkpoint] {p.name} esisteva già: spostato in {backup_path.name} prima di riscriverlo.", file=sys.stderr)
    except Exception:
        pass
