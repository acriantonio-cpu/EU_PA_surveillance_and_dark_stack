"""Modello 'sito da classificare' e caricamento degli input (CSV di output o JSON del crawl)."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..urlutil import host_of, is_ip_host, normalize_host

logger = logging.getLogger(__name__)

CRAWL_MARKERS = {"seed", "interno", "esterno", "redirect"}


@dataclass
class Site:
    key: str                       # host principale (FQDN) usato come chiave di cache
    hosts: list[str]               # host candidati da provare (osservati prima)
    root: str = ""
    nome_ente: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)   # righe di input che condividono la chiave


def _hosts_from_value(value: str) -> list[str]:
    out: list[str] = []
    for part in (value or "").split("|"):
        part = part.strip()
        if not part:
            continue
        h = host_of(part) if "://" in part else normalize_host(part.split("/")[0])
        if h and not is_ip_host(h) and "." in h and h not in out:
            out.append(h)
    return out


def sites_from_rows(rows: list[dict[str, Any]]) -> list[Site]:
    """Una riga di input -> un host chiave. Righe con lo stesso host confluiscono nello stesso Site."""
    by_key: dict[str, Site] = {}
    for row in rows:
        primary = _hosts_from_value(str(row.get("hostname") or ""))
        observed = _hosts_from_value(str(row.get("host_osservati") or ""))
        is_crawl = bool(row.get("found_on")) or (row.get("tipo_link") in CRAWL_MARKERS)
        # Per le righe del crawl 'hostname' è l'apex ricostruito (https://<dominio_radice>): l'host
        # realmente linkato è più affidabile e va provato per primo.
        hosts = (observed + primary) if is_crawl else (primary + observed)
        seen: list[str] = []
        for h in hosts:
            if h not in seen:
                seen.append(h)
        if not seen:
            continue
        key = seen[0]
        site = by_key.get(key)
        if site is None:
            site = by_key[key] = Site(
                key=key, hosts=seen, root=str(row.get("dominio_radice") or ""), nome_ente=str(row.get("nome_ente") or "")
            )
        else:
            for h in seen:
                if h not in site.hosts:
                    site.hosts.append(h)
        site.rows.append(row)
    return list(by_key.values())


def _rows_from_registry_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for e in data:
        hosts = e.get("observed_hosts") or []
        if isinstance(hosts, list):
            hosts = "|".join(hosts)
        rows.append({
            "hostname": e.get("external_url", ""), "host_osservati": hosts, "dominio_radice": e.get("external_domain", ""),
            "found_on": e.get("found_on", ""), "tipo_link": e.get("scope", ""), "source_seed": e.get("source_seed", ""),
            "tls_error": "si" if e.get("tls_error") else "", "country_code": "",
        })
    return rows


def load_rows(country: str, project_root: Path, explicit_input: Path | None = None) -> tuple[list[dict[str, Any]], str]:
    """Ritorna (righe, descrizione della sorgente). Ordine: --input esplicito; altrimenti
    data/output/{ISO}/siti.csv; se quel CSV non ha NESSUN hostname (es. paesi il cui
    field_mapping non è quello del crawler) si ripiega sul JSON del crawl in
    data/input/{ISO}/crawl_*.json (escluse checkpoint/progress)."""
    if explicit_input:
        p = Path(explicit_input)
        if p.suffix.lower() == ".json":
            return _rows_from_registry_json(p), f"{p} (JSON del crawl)"
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f)), str(p)

    csv_path = project_root / "data" / "output" / country.upper() / "siti.csv"
    rows: list[dict[str, Any]] = []
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    if any((r.get("hostname") or "").strip() or (r.get("host_osservati") or "").strip() for r in rows):
        return rows, str(csv_path)

    in_dir = project_root / "data" / "input" / country.upper()
    candidates = sorted(
        (p for p in in_dir.glob("crawl_*.json") if not any(x in p.name for x in ("checkpoint", "progress"))),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if candidates:
        logger.warning(
            "[%s] %s non contiene hostname: uso il JSON del crawl %s", country.upper(), csv_path, candidates[0]
        )
        return _rows_from_registry_json(candidates[0]), f"{candidates[0]} (JSON del crawl)"
    return rows, str(csv_path)
