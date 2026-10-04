"""
Schema comune di output per tutti i paesi.

Ogni record normalizzato ha esattamente questi campi, nell'ordine in cui
vengono scritti nel CSV di output (data/output/{ISO}/siti.csv).

Colonne aggiunte (in coda, così chi legge per posizione non si rompe):
  dominio_radice   eTLD+1 dell'hostname
  host_osservati   altri host/URL visti per lo stesso ente ('|' come separatore)
  found_on         (crawl) pagina su cui il link è stato trovato
  source_seed      (crawl) seed da cui discende la scoperta
  tls_error        (crawl) 'si' se il certificato TLS non era verificabile
"""

from __future__ import annotations

import csv
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

FIELDNAMES = [
    "hostname",
    "tipo_link",
    "nome_ente",
    "comune",
    "regione",
    "codice_ipa",
    "sector",
    "country_code",
    "source_name",
    "retrieved_at",
    "dominio_radice",
    "host_osservati",
    "found_on",
    "source_seed",
    "tls_error",
]


@dataclass
class Record:
    hostname: str = ""
    tipo_link: str = ""
    nome_ente: str = ""
    comune: str = ""
    regione: str = ""
    codice_ipa: str = ""
    sector: str = ""
    country_code: str = ""
    source_name: str = ""
    retrieved_at: str = ""
    dominio_radice: str = ""
    host_osservati: str = ""
    found_on: str = ""
    source_seed: str = ""
    tls_error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_csv(rows: Iterable[dict], fieldnames: list[str], output_path: Path) -> int:
    """Scrive un CSV UTF-8 (BOM, per Excel) in modo ATOMICO: prima su un file
    temporaneo, poi os.replace. Un crash a metà non corrompe l'output
    precedente. Se la destinazione è bloccata (tipico su Windows: CSV aperto in
    Excel) dopo qualche ritentativo si salva comunque in
    '<nome>.<timestamp>.csv' invece di perdere il lavoro. Ritorna le righe scritte."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    count = 0
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    for attempt in range(1, 6):
        try:
            os.replace(tmp, output_path)
            return count
        except PermissionError:
            if attempt < 5:
                time.sleep(0.5 * attempt)
    alt = output_path.with_name(f"{output_path.stem}.{datetime.now().strftime('%Y%m%d-%H%M%S')}{output_path.suffix}")
    os.replace(tmp, alt)
    logger.warning(
        "Impossibile sovrascrivere %s (file aperto in un altro programma, es. Excel?): "
        "il risultato è stato salvato in %s", output_path, alt,
    )
    return count


def write_csv(records: Iterable[Record], output_path: Path) -> int:
    """Scrive i record normalizzati in un CSV UTF-8 (con BOM per Excel), in modo atomico."""
    return atomic_write_csv((r.as_dict() for r in records), FIELDNAMES, output_path)
