#!/usr/bin/env python3
# EN: Step 07 (optional) - observable redundancy: query EVERY declared nameserver separately and
# test EVERY published A/AAAA address (TCP -> TLS -> HTTP HEAD), measuring real rather than
# declared resilience. Output: resilience.csv.
"""
07_resilience.py

Sonda C (facoltativa): a differenza di 02_probe_infra.py (che risolve UN
indirizzo per servizio via il resolver di sistema e misura quello), questo
script misura la RIDONDANZA EFFETTIVAMENTE OSSERVABILE:

- interroga ogni server NS dichiarato SEPARATAMENTE (non il resolver di
  sistema) e verifica se risponde e se la risposta è coerente con le altre;
- testa OGNI indirizzo A/AAAA pubblicato (non solo il primo raggiungibile)
  con una singola transazione TCP -> TLS -> HTTP HEAD, misurandone la
  latenza individuale.

Risponde alla domanda "se un nameserver o un endpoint cade, gli altri
rispondono davvero?", diversa da "il sito principale funziona?" già coperta
da 02. Nessun load test, fuzzing o scansione: al massimo una query DNS per
tipo/server autoritativo e una transazione TCP/TLS/HTTP per indirizzo
pubblicato — stesso spirito "buon vicinato" del resto della pipeline (vedi
concorrenza_per_provider in 02_probe_infra.py), applicato qui con lo stesso
LimitatorePerBlocco di asn_bulk.py.

Va eseguito dalla cartella principale del progetto, DOPO 02_probe_infra.py
(di cui riusa l'elenco hostname/comune/regione/codice_ipa in risultati.csv,
per evitare un'estrazione IPA duplicata).

Esempi:
    python src\\07_resilience.py --limit 20
    python src\\07_resilience.py --resume
    python src\\07_resilience.py --concorrenza 10
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import dns.resolver
import dns.version
import pandas as pd

import asn_bulk
import config as cfgmod
import rate_limiter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; osservatorio-dark-stack-resilienza/1.0)"


def dati_results(paese: str, run_id: str) -> Path:
    return PROJECT_ROOT / "data" / "results" / paese / run_id


FIELDS = [
    "hostname", "comune", "regione", "codice_ipa",
    "n_ns_dichiarati", "n_ns_raggiungibili", "rapporto_ns_raggiungibili",
    "ns_mx_incoerenti", "n_ip_distinti_tra_ns",
    "n_a", "n_aaaa", "n_endpoint_http",
    "n_endpoint_tcp_ok", "n_endpoint_tls_ok", "n_endpoint_http_ok",
    "rapporto_endpoint_http_efficace",
    "latenza_http_mediana_ms", "latenza_http_p95_ms",
    "punteggio_resilienza_combinato",
    "errori_probe",
]
CAMPI_PER_ENTE = {"hostname", "comune", "regione", "codice_ipa"}
CAMPI_PER_HOSTNAME = [f for f in FIELDS if f not in CAMPI_PER_ENTE]


def make_resolver(timeout: float, nameservers=None) -> dns.resolver.Resolver:
    r = dns.resolver.Resolver(configure=True)
    r.timeout = timeout
    r.lifetime = timeout
    if nameservers:
        r.nameservers = list(nameservers)
    return r


def query_rr(host: str, rrtype: str, timeout: float, nameserver: str | None = None):
    r = dns.resolver.Resolver(configure=True)
    r.timeout = timeout
    r.lifetime = timeout
    if nameserver:
        r.nameservers = [nameserver]
    return list(r.resolve(host, rrtype, raise_on_no_answer=False))


def authoritative_servers(host: str, timeout: float) -> list[str]:
    try:
        ns = query_rr(host, "NS", timeout)
        return sorted({str(x).rstrip(".") for x in ns})
    except Exception:
        return []


def resolve_ips(host: str, timeout: float) -> tuple[list[str], list[str]]:
    a, aaaa = [], []
    try:
        a = sorted({str(x) for x in query_rr(host, "A", timeout)})
    except Exception:
        pass
    try:
        aaaa = sorted({str(x) for x in query_rr(host, "AAAA", timeout)})
    except Exception:
        pass
    return a, aaaa


def probe_authoritative(ns_host: str, host: str, dns_timeout: float) -> dict:
    """Interroga IL server NS direttamente (non il resolver di sistema) per
    A/AAAA/MX/SOA di 'host': un NS che non risponde o risponde in modo
    diverso dagli altri è un segnale di ridondanza rotta, non visibile
    guardando solo la risposta aggregata del resolver di sistema (02)."""
    esito = {"ns": ns_host, "raggiungibile": False, "risposte": {}}
    try:
        ips_ns_a, ips_ns_aaaa = resolve_ips(ns_host, dns_timeout)
        ns_ip = (ips_ns_a + ips_ns_aaaa or [None])[0]
        if not ns_ip:
            esito["errore"] = "indirizzo_ns_non_risolto"
            return esito
        valori = {}
        for typ in ("A", "AAAA", "MX", "SOA"):
            try:
                ans = query_rr(host, typ, dns_timeout, nameserver=ns_ip)
                valori[typ] = sorted({str(x).rstrip(".") for x in ans})
            except Exception as exc:
                valori[typ] = [f"ERRORE:{type(exc).__name__}"]
        esito["raggiungibile"] = True
        esito["risposte"] = valori
        return esito
    except Exception as exc:
        esito["errore"] = f"{type(exc).__name__}"
        return esito


def probe_endpoint(host: str, ip: str, timeout: float, user_agent: str, port: int = 443) -> dict:
    """Una sola transazione TCP -> TLS -> HTTP HEAD per indirizzo. Nessun
    retry, nessuna richiesta ripetuta: lo scopo è misurare lo stato di
    QUESTO endpoint in un istante, non stressarlo."""
    inizio = time.perf_counter()
    esito = {"ip": ip, "tcp_ok": False, "tls_ok": False, "http_ok": False,
             "http_status": None, "latenza_ms": None, "errore": None}
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        esito["tcp_ok"] = True
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
            esito["tls_ok"] = True
            richiesta = (
                f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
                f"User-Agent: {user_agent}\r\n\r\n"
            ).encode()
            tls_sock.sendall(richiesta)
            dati = tls_sock.recv(4096)
        esito["latenza_ms"] = round((time.perf_counter() - inizio) * 1000, 2)
        prima_riga = dati.split(b"\r\n", 1)[0].decode("latin1", "replace")
        parti = prima_riga.split()
        if len(parti) >= 2 and parti[0].startswith("HTTP/"):
            try:
                esito["http_status"] = int(parti[1])
                esito["http_ok"] = 200 <= esito["http_status"] < 500
            except ValueError:
                pass
    except Exception as exc:
        esito["latenza_ms"] = round((time.perf_counter() - inizio) * 1000, 2)
        esito["errore"] = type(exc).__name__
    return esito


def summarize_host(host: str, dns_timeout: float, tcp_timeout: float, user_agent: str,
                    limitatore_blocco: "asn_bulk.LimitatorePerBlocco") -> dict:
    ns_hosts = authoritative_servers(host, dns_timeout)
    auth = [probe_authoritative(ns, host, dns_timeout) for ns in ns_hosts]
    reachable = [x for x in auth if x.get("raggiungibile")]
    # BUGFIX (audit/test reale, terza passata): la bozza originale confrontava
    # A/AAAA/MX/SOA per uguaglianza esatta fra NS e contava ogni differenza
    # come "incoerenza". Un test su github.com (8/8 NS raggiungibili) ha
    # mostrato A record DIVERSI fra NS per via di DNS round-robin/geo — un
    # comportamento normale e diffuso su siti con più IP, non un segnale di
    # ridondanza rotta. Il MX invece è un dato stabile per definizione
    # (instrada la posta, non bilancia traffico): un NS secondario che
    # risponde con un MX diverso o assente è un segnale reale di zona non
    # propagata/disallineata. Per questo la metrica "incoerenza" guarda SOLO
    # il MX; la diversità di A/AAAA fra NS resta visibile ma come dato
    # informativo (n_ip_distinti_tra_ns), non penalizzata nel punteggio.
    incoerenti = 0
    ip_distinti_tra_ns = set()
    if reachable:
        for x in reachable:
            for campo in ("A", "AAAA"):
                for v in x["risposte"].get(campo, []):
                    if not str(v).startswith("ERRORE:"):
                        ip_distinti_tra_ns.add(v)
    if len(reachable) >= 2:
        def _mx(risposte):
            return json.dumps(risposte.get("MX", []), sort_keys=True)
        riferimento_mx = _mx(reachable[0]["risposte"])
        incoerenti = sum(1 for x in reachable[1:] if _mx(x["risposte"]) != riferimento_mx)

    a, aaaa = resolve_ips(host, dns_timeout)
    ip_endpoints = sorted(set(a + aaaa))
    endpoint_probes = []
    for ip in ip_endpoints:
        # Stesso limitatore per-blocco IP di 02_probe_infra.py: un consorzio
        # di hosting che ospita molti enti del campione non va bombardato
        # con connessioni TLS/HTTP simultanee solo perché ha molti indirizzi.
        with limitatore_blocco.limita(ip):
            endpoint_probes.append(probe_endpoint(host, ip, tcp_timeout, user_agent))

    reachable_ns = len(reachable)
    ns_ratio = round(reachable_ns / len(auth), 4) if auth else None
    tcp_ok = sum(x["tcp_ok"] for x in endpoint_probes)
    tls_ok = sum(x["tls_ok"] for x in endpoint_probes)
    http_ok = sum(x["http_ok"] for x in endpoint_probes)
    latenze = sorted(x["latenza_ms"] for x in endpoint_probes if x["latenza_ms"] is not None)
    mediana = p95 = None
    if latenze:
        meta = len(latenze) // 2
        mediana = round(latenze[meta] if len(latenze) % 2 else (latenze[meta - 1] + latenze[meta]) / 2, 2)
        idx = min(len(latenze) - 1, max(0, round(0.95 * (len(latenze) - 1))))
        p95 = round(latenze[idx], 2)
    endpoint_ratio = round(http_ok / len(endpoint_probes), 4) if endpoint_probes else None

    combinato = None
    valori_combinato = [v for v in (ns_ratio, endpoint_ratio) if v is not None]
    if valori_combinato:
        combinato = round(sum(valori_combinato) / len(valori_combinato), 4)

    errori = sum(1 for x in endpoint_probes if x["errore"]) + sum(1 for x in auth if x.get("errore"))

    return {
        "n_ns_dichiarati": len(ns_hosts),
        "n_ns_raggiungibili": reachable_ns,
        "rapporto_ns_raggiungibili": ns_ratio,
        "ns_mx_incoerenti": incoerenti,
        "n_ip_distinti_tra_ns": len(ip_distinti_tra_ns),
        "n_a": len(a), "n_aaaa": len(aaaa),
        "n_endpoint_http": len(endpoint_probes),
        "n_endpoint_tcp_ok": int(tcp_ok), "n_endpoint_tls_ok": int(tls_ok), "n_endpoint_http_ok": int(http_ok),
        "rapporto_endpoint_http_efficace": endpoint_ratio,
        "latenza_http_mediana_ms": mediana,
        "latenza_http_p95_ms": p95,
        "punteggio_resilienza_combinato": combinato,
        "errori_probe": errori,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paese", default="IT",
        help="Codice paese ISO a due lettere: determina i percorsi di default "
             "data/results/<paese>/<run-id>/risultati.csv e .../resilience.csv (default: IT).",
    )
    ap.add_argument("--in", dest="infile", default=None, help="CSV di input (default: risultati.csv di 02_probe_infra.py per lo stesso run-id)")
    ap.add_argument("--out", default=None, help="CSV di output (default: data/results/<paese>/<run-id>/resilience.csv)")
    ap.add_argument(
        "--run-id", default=None,
        help="Vedi 02_probe_infra.py --help. Default: l'ultimo run noto (latest_run.json), altrimenti la data UTC odierna.",
    )
    ap.add_argument("--config", default=None, help="Percorso di config.yaml (default: config.yaml nella project root)")
    ap.add_argument("--dns-timeout", type=float, default=None, help="Timeout (s) per query DNS dirette ai NS (default: da config.yaml, timeout_secondi.resilienza_dns)")
    ap.add_argument("--tcp-timeout", type=float, default=None, help="Timeout (s) per la transazione TCP/TLS/HTTP per endpoint (default: da config.yaml, timeout_secondi.resilienza_tcp)")
    ap.add_argument("--concorrenza", type=int, default=None, help="Quanti hostname sondare in parallelo (default: da config.yaml, stesso valore di 02)")
    ap.add_argument("--concorrenza-per-provider", type=int, default=None, help="Tetto di connessioni simultanee per blocco IP (default: da config.yaml, stesso valore di 02)")
    ap.add_argument("--limit", type=int, default=None, help="Limita a N righe di input (per test rapidi)")
    ap.add_argument("--resume", action="store_true", help="Salta i codice_ipa già presenti in --out")
    args = ap.parse_args()

    paese = args.paese.strip().upper()
    cfg = cfgmod.load_config(args.config)

    if not cfgmod.flag(cfg, "resilienza_dinamica"):
        print(
            "resilienza_dinamica=false in config.yaml: esco senza scrivere output. "
            "È disattivata di default perché aggiunge transazioni TCP/TLS aggiuntive verso "
            "OGNI indirizzo pubblicato di ogni sito (oltre a quelle già fatte da 02_probe_infra.py): "
            "metti il flag a true in config.yaml per attivarla consapevolmente.",
            file=sys.stderr,
        )
        sys.exit(0)

    run_id = args.run_id or cfgmod.leggi_puntatore_latest(paese) or cfgmod.run_id_default()
    infile = args.infile or str(dati_results(paese, run_id) / "risultati.csv")
    out_arg = args.out or str(dati_results(paese, run_id) / "resilience.csv")

    if not Path(infile).exists():
        print(f"ERRORE: non trovo {infile}. Esegui prima 02_probe_infra.py per lo stesso --run-id.", file=sys.stderr)
        sys.exit(1)

    df_in = pd.read_csv(infile, dtype={"codice_ipa": str, "hostname": str})
    if "hostname" not in df_in.columns or "codice_ipa" not in df_in.columns:
        print("ERRORE: il CSV di input deve avere le colonne 'hostname' e 'codice_ipa' (output di 02_probe_infra.py)", file=sys.stderr)
        sys.exit(1)
    if args.limit:
        df_in = df_in.head(args.limit)

    dns_timeout = args.dns_timeout if args.dns_timeout is not None else cfgmod.timeout_di(cfg, "resilienza_dns", 5.0)
    tcp_timeout = args.tcp_timeout if args.tcp_timeout is not None else cfgmod.timeout_di(cfg, "resilienza_tcp", 5.0)
    concorrenza = args.concorrenza if args.concorrenza is not None else cfgmod.concorrenza_di(cfg, 15)
    concorrenza_per_provider = (
        args.concorrenza_per_provider if args.concorrenza_per_provider is not None
        else cfgmod.concorrenza_per_provider_di(cfg, 3)
    )
    user_agent = cfgmod.user_agent_di(cfg, DEFAULT_USER_AGENT)

    out_path = Path(out_arg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    already_done_codici = set()
    host_cache: dict[str, dict] = {}
    write_header = True
    if args.resume and out_path.exists():
        prev = pd.read_csv(out_path, dtype=str)
        already_done_codici = set(prev["codice_ipa"].astype(str))
        write_header = False
        for _, prow in prev.iterrows():
            h = str(prow["hostname"])
            if h not in host_cache:
                host_cache[h] = {f: prow.get(f, "") for f in CAMPI_PER_HOSTNAME}
        print(f"[resume] {len(already_done_codici)} enti già presenti in {out_path}, li salto.", file=sys.stderr)
        df_in = df_in[~df_in["codice_ipa"].astype(str).isin(already_done_codici)]

    hostnames = sorted(df_in["hostname"].dropna().astype(str).str.strip().unique())
    hostnames = [h for h in hostnames if h and h not in host_cache]

    limitatore_blocco = asn_bulk.LimitatorePerBlocco(max_per_blocco=concorrenza_per_provider)

    manifest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "paese": paese,
        "run_id": run_id,
        "n_hostname_da_sondare": len(hostnames),
        "resume": bool(args.resume),
        "concorrenza": concorrenza,
        "concorrenza_per_provider": concorrenza_per_provider,
        "dns_timeout_seconds": dns_timeout,
        "tcp_timeout_seconds": tcp_timeout,
        "http_user_agent": user_agent,
        "dnspython_version": dns.version.version,
        "python_version": platform.python_version(),
        "os": platform.platform(),
        "script": "07_resilience.py",
    }
    manifest_path = str(out_arg).rsplit(".", 1)[0] + "_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    if hostnames:
        with ThreadPoolExecutor(max_workers=max(1, concorrenza)) as ex:
            futs = {
                ex.submit(summarize_host, h, dns_timeout, tcp_timeout, user_agent, limitatore_blocco): h
                for h in hostnames
            }
            n_fatti = 0
            for fut in as_completed(futs):
                h = futs[fut]
                try:
                    host_cache[h] = fut.result()
                except Exception as exc:
                    host_cache[h] = {f: None for f in CAMPI_PER_HOSTNAME}
                    host_cache[h]["errori_probe"] = 1
                    print(f"AVVISO: probe fallita per {h}: {type(exc).__name__}", file=sys.stderr)
                n_fatti += 1
                if n_fatti % 50 == 0 or n_fatti == len(hostnames):
                    print(f"  ...{n_fatti}/{len(hostnames)} hostname sondati", file=sys.stderr)

    if write_header and out_path.exists():
        cfgmod.backup_se_esiste(out_path)  # vedi config.py: non perdere il checkpoint di un --run-id rilanciato senza --resume
    with open(out_arg, "a" if not write_header else "w", newline="", encoding="utf-8") as f:
        import csv
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        for _, riga in df_in.iterrows():
            h = str(riga["hostname"]).strip()
            valori_host = host_cache.get(h, {f: None for f in CAMPI_PER_HOSTNAME})
            out_row = {
                "hostname": h,
                "comune": riga.get("comune", ""),
                "regione": riga.get("regione", ""),
                "codice_ipa": riga.get("codice_ipa", ""),
                **valori_host,
            }
            writer.writerow(out_row)

    cfgmod.aggiorna_puntatore_latest(paese, run_id)
    print(f"\nFatto. {len(hostnames)} hostname sondati, output in {out_arg}. Manifesto: {manifest_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
