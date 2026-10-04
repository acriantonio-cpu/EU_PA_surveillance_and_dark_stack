"""
Persistenza incrementale dello stato del crawler (checkpoint) su SQLite.

Perché: il vecchio checkpoint riscriveva per intero un JSON (con tutte le pagine visitate) dopo OGNI
pagina: a 50.000 pagine sono ~3,5 MB per salvataggio, ~90 GB di scritture cumulative. Ora ogni pagina
scrive SOLO le proprie differenze (poche righe) in un database SQLite in modalità WAL: costo
costante per pagina, transazionale (un crash non lascia mai uno stato a metà), e il resume ricostruisce
tutto da qui.

Il JSON `crawl_checkpoint.json` (formato storico, letto da tools/postprocess_checkpoints.py) viene
ancora prodotto, ma solo a fine livello, a fine crawl e quando il crawl viene interrotto
(`export_json`). Un checkpoint JSON storico (di una versione precedente) può ancora essere usato per
riprendere: `import_legacy_json`.

Solo il thread coordinatore del crawler tocca lo store: nessun lock necessario.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS visited(url TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS level_queue(pos INTEGER PRIMARY KEY, url TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS next_level(pos INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS registry(root TEXT PRIMARY KEY, data TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS resource(root TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(root, data));
CREATE TABLE IF NOT EXISTS origin(url TEXT PRIMARY KEY, seed TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS pages_seed(seed TEXT NOT NULL, level INTEGER NOT NULL, n INTEGER NOT NULL, PRIMARY KEY(seed, level));
CREATE TABLE IF NOT EXISTS templates(t TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
"""

TABLES = ("meta", "visited", "level_queue", "next_level", "registry", "resource", "origin", "pages_seed", "templates", "errors")
ERROR_LOG_KEEP = 2000


def replace_with_retry(src: Path, dst: Path, attempts: int = 5) -> None:
    """os.replace con qualche ritentativo (su Windows antivirus/OneDrive tengono il file per un istante)."""
    for i in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts:
                raise
            time.sleep(0.4 * i)


class CrawlStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)   # transazioni gestite a mano
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.executescript(SCHEMA)
        self._ops: dict[str, list[tuple]] = {}

    # -- ciclo di vita ---------------------------------------------------------
    def exists_with_data(self) -> bool:
        return self.get_meta("fingerprint") is not None

    def reset(self) -> None:
        """Svuota tutte le tabelle (crawl da zero)."""
        self._ops.clear()
        self.conn.execute("BEGIN")
        for t in TABLES:
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.execute("COMMIT")

    def close(self) -> None:
        try:
            self.commit()
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.conn.close()

    # -- meta ------------------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, **kv: Any) -> None:
        for k, v in kv.items():
            self._add("meta", (k, json.dumps(v, ensure_ascii=False)))

    # -- scritture bufferizzate (eseguite da commit()) ------------------------------
    def _add(self, table: str, row: tuple) -> None:
        self._ops.setdefault(table, []).append(row)

    def add_visited(self, url: str) -> None:
        self._add("visited", (url,))

    def add_next_level(self, url: str) -> None:
        self._add("next_level", (url,))

    def put_registry(self, root: str, entry: dict[str, Any]) -> None:
        self._add("registry", (root, json.dumps(entry, ensure_ascii=False)))

    def add_resource(self, root: str, entry: dict[str, Any]) -> None:
        self._add("resource", (root, json.dumps(entry, ensure_ascii=False, sort_keys=True)))

    def add_origin(self, url: str, seed: str) -> None:
        self._add("origin", (url, seed))

    def bump_pages_seed(self, seed: str, level: int) -> None:
        self._add("pages_seed", (seed, level))

    def add_template(self, t: str) -> None:
        self._add("templates", (t,))

    def add_error(self, entry: dict[str, Any]) -> None:
        self._add("errors", (json.dumps(entry, ensure_ascii=False),))

    def begin_level(self, level: int, queue: list[str]) -> None:
        """Inizio di un livello: salva la coda (in ordine) e azzera next_level. Transazione a sé."""
        self.commit()
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM level_queue")
        self.conn.execute("DELETE FROM next_level")
        self.conn.executemany("INSERT INTO level_queue(pos, url) VALUES (?, ?)", list(enumerate(queue)))
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('level', ?)", (json.dumps(level),))
        self.conn.execute("COMMIT")

    def sync_pages_seed(self, pages_by_seed_level: dict[str, dict[str, int]]) -> None:
        """Riallinea il contatore pagine per seed/livello (usato dopo l'espansione di una paginazione,
        che lo modifica direttamente)."""
        self.commit()
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM pages_seed")
        self.conn.executemany(
            "INSERT INTO pages_seed(seed, level, n) VALUES (?, ?, ?)",
            [(s, int(lv), n) for s, d in pages_by_seed_level.items() for lv, n in d.items()],
        )
        self.conn.execute("COMMIT")

    def commit(self) -> None:
        if not self._ops:
            return
        ops, self._ops = self._ops, {}
        c = self.conn
        c.execute("BEGIN")
        try:
            if "visited" in ops:
                c.executemany("INSERT OR IGNORE INTO visited(url) VALUES (?)", ops["visited"])
            if "next_level" in ops:
                c.executemany("INSERT OR IGNORE INTO next_level(url) VALUES (?)", ops["next_level"])
            if "registry" in ops:
                c.executemany("INSERT OR REPLACE INTO registry(root, data) VALUES (?, ?)", ops["registry"])
            if "resource" in ops:
                c.executemany("INSERT OR IGNORE INTO resource(root, data) VALUES (?, ?)", ops["resource"])
            if "origin" in ops:
                c.executemany("INSERT OR IGNORE INTO origin(url, seed) VALUES (?, ?)", ops["origin"])
            if "pages_seed" in ops:
                c.executemany(
                    "INSERT INTO pages_seed(seed, level, n) VALUES (?, ?, 1) "
                    "ON CONFLICT(seed, level) DO UPDATE SET n = n + 1", ops["pages_seed"],
                )
            if "templates" in ops:
                c.executemany("INSERT OR IGNORE INTO templates(t) VALUES (?)", ops["templates"])
            if "errors" in ops:
                c.executemany("INSERT INTO errors(data) VALUES (?)", ops["errors"])
            if "meta" in ops:
                c.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ops["meta"])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    # -- lettura (resume / export) ------------------------------------------------------
    def load(self) -> dict[str, Any] | None:
        """Stato completo nel formato storico del checkpoint JSON (+ 'queue' = coda del livello
        MENO le pagine già visitate, nell'ordine originale)."""
        self.commit()
        fp = self.get_meta("fingerprint")
        if fp is None:
            return None
        c = self.conn
        visited = {r[0] for r in c.execute("SELECT url FROM visited")}
        queue = [r[0] for r in c.execute("SELECT url FROM level_queue ORDER BY pos") if r[0] not in visited]
        pages_by_seed_level: dict[str, dict[str, int]] = {}
        for seed, level, n in c.execute("SELECT seed, level, n FROM pages_seed"):
            pages_by_seed_level.setdefault(seed, {})[str(level)] = n
        resource_index: dict[str, list[dict[str, Any]]] = {}
        for root, data in c.execute("SELECT root, data FROM resource"):
            resource_index.setdefault(root, []).append(json.loads(data))
        errors = [json.loads(r[0]) for r in c.execute("SELECT data FROM errors ORDER BY id DESC LIMIT ?", (ERROR_LOG_KEEP,))]
        errors.reverse()
        return {
            "fingerprint": fp,
            "fingerprint_fields": self.get_meta("fingerprint_fields"),
            "level": int(self.get_meta("level", 1)),
            "queue": queue,
            "visited": sorted(visited),
            "next_level": [r[0] for r in c.execute("SELECT url FROM next_level ORDER BY pos")],
            "domain_registry": {root: json.loads(d) for root, d in c.execute("SELECT root, data FROM registry")},
            "resource_index": resource_index,
            "pagination_templates_seen": sorted(r[0] for r in c.execute("SELECT t FROM templates")),
            "page_origin_seed": {u: s for u, s in c.execute("SELECT url, seed FROM origin")},
            "pages_by_seed_level": pages_by_seed_level,
            "domain_consecutive_errors": self.get_meta("domain_consecutive_errors", {}),
            "domain_cooldown_until_call": self.get_meta("domain_cooldown_until_call", {}),
            "domain_cooldown_retries_used": self.get_meta("domain_cooldown_retries_used", {}),
            "global_call_index": int(self.get_meta("global_call_index", 0)),
            "error_log": errors,
            "completed": bool(self.get_meta("completed", False)),
            "saved_at": self.get_meta("saved_at"),
        }

    def export_json(self, path: Path) -> None:
        """Scrive il checkpoint nel formato JSON storico (per tools/postprocess_checkpoints.py e per
        ispezione a mano). Best-effort: un errore qui NON deve mai fermare il crawl."""
        try:
            data = self.load()
            if data is None:
                return
            path = Path(path)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            replace_with_retry(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("impossibile esportare il checkpoint JSON in %s (%s): non fatale", path, exc)

    def import_legacy_json(self, data: dict[str, Any]) -> None:
        """Migra nello store un checkpoint JSON storico (formato delle versioni precedenti), così un
        --resume su un vecchio checkpoint continua a funzionare e da lì in poi si salva in SQLite."""
        self.reset()
        c = self.conn
        c.execute("BEGIN")
        c.executemany("INSERT OR IGNORE INTO visited(url) VALUES (?)", [(u,) for u in data.get("visited", [])])
        queue = list(data.get("queue", data.get("this_level_pages", [])))
        c.executemany("INSERT INTO level_queue(pos, url) VALUES (?, ?)", list(enumerate(queue)))
        c.executemany("INSERT OR IGNORE INTO next_level(url) VALUES (?)", [(u,) for u in data.get("next_level", [])])
        c.executemany("INSERT OR REPLACE INTO registry(root, data) VALUES (?, ?)",
                      [(k, json.dumps(v, ensure_ascii=False)) for k, v in data.get("domain_registry", {}).items()])
        c.executemany("INSERT OR IGNORE INTO resource(root, data) VALUES (?, ?)",
                      [(root, json.dumps(e, ensure_ascii=False, sort_keys=True))
                       for root, lst in data.get("resource_index", {}).items() for e in lst])
        c.executemany("INSERT OR IGNORE INTO origin(url, seed) VALUES (?, ?)", list(data.get("page_origin_seed", {}).items()))
        c.executemany("INSERT OR REPLACE INTO pages_seed(seed, level, n) VALUES (?, ?, ?)",
                      [(s, int(lv), n) for s, d in data.get("pages_by_seed_level", {}).items() for lv, n in d.items()])
        c.executemany("INSERT OR IGNORE INTO templates(t) VALUES (?)", [(t,) for t in data.get("pagination_templates_seen", [])])
        c.executemany("INSERT INTO errors(data) VALUES (?)", [(json.dumps(e, ensure_ascii=False),) for e in data.get("error_log", [])])
        meta = {
            "fingerprint": data.get("fingerprint"), "fingerprint_fields": data.get("fingerprint_fields"),
            "level": data.get("level", 1), "completed": bool(data.get("completed", False)),
            "saved_at": data.get("saved_at"), "global_call_index": data.get("global_call_index", 0),
            "domain_consecutive_errors": data.get("domain_consecutive_errors", {}),
            "domain_cooldown_until_call": data.get("domain_cooldown_until_call", {}),
            "domain_cooldown_retries_used": data.get("domain_cooldown_retries_used", {}),
        }
        c.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                      [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
        c.execute("COMMIT")
