"""Orchestrazione della cascata di classificazione (stadi 0, 2, 3, 4 opzionali)."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from ..schema import atomic_write_csv
from .lexicon import Lexicon
from .sites import Site
from .stage0 import run_stage0
from .stage2_wikidata import run_stage2
from .stage3_homepage import DEFAULT_CFG as S3_DEFAULTS
from .stage3_homepage import HostLimiter, SCORABLE_STATUS, collect_evidence, make_robots, score_evidence
from .stage4_llm import PROMPT_VERSION, OllamaClient, votes_from_llm
from .taxonomy import Verdict, Vote, combine

logger = logging.getLogger(__name__)


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
    un contatore fisso ('ogni 100') non produce mai nulla se il batch ha meno elementi di quello,
    o se l'operazione è lenta (rete, servizi esterni rate-limited) — qui invece si vede sempre
    un avanzamento almeno ogni 'every_seconds', qualunque sia la dimensione del batch."""

    def __init__(self, logger_: logging.Logger, prefix: str, total: int, every_seconds: float = 10.0):
        self.logger = logger_
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
            self.logger.info(
                "%s: %d/%d (%.0f%%) - %.2f/s - ETA %s",
                self.prefix, self.done, self.total, pct, rate, _fmt_eta(eta),
            )

OUTPUT_FIELDS = [
    "host_classificato", "natura", "tipo", "ente_rilevato", "confidenza_natura", "confidenza_tipo", "stadi", "evidenza",
    "homepage_status", "final_url", "lang", "wikidata", "llm_model", "da_rivedere", "classified_at",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "lexicon": "config/classify_lexicon.yaml",
    "stages": {
        "stage0": {"enabled": False},
        "stage2": {"enabled": False, "endpoint": "https://query.wikidata.org/sparql", "language": "en",
                   "batch_size": 20, "sleep_seconds": 1.0},
        "stage3": {"enabled": False, "workers": 8, **{k: v for k, v in S3_DEFAULTS.items()}},
        "stage4": {"enabled": False, "ollama_url": "http://localhost:11434", "model": "gemma4:e2b_q8",
                   "only_if_below": 0.8, "confidence": 0.65, "timeout": 120, "num_ctx": 4096, "workers": 1},
    },
    "stop_confidence": 0.8,
    "review_below_natura": 0.6,
    "review_below_tipo": 0.5,
    "cache_ttl_days": 90,
    "extra_infrastructure_domains": [],
}


def load_config(path: Path | None, overrides: dict[str, bool] | None = None) -> dict[str, Any]:
    """Config = default + file YAML (se esiste) + override da riga di comando ({'stage3': True})."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and Path(path).exists():
        user = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for k, v in user.items():
            if k == "stages":
                for sname, sval in (v or {}).items():
                    cfg["stages"].setdefault(sname, {}).update(sval or {})
            else:
                cfg[k] = v
    for stage, enabled in (overrides or {}).items():
        cfg["stages"].setdefault(stage, {})["enabled"] = bool(enabled)
    return cfg


# --------------------------------------------------------------------------
# Cache append-only (JSONL): sopravvive ai crash, niente riscrittura totale a ogni sito
# --------------------------------------------------------------------------
class Cache:
    def __init__(self, path: Path, ttl_days: int):
        self.path = Path(path)
        self.ttl = timedelta(days=ttl_days)
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        bad = 0
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    self._data[(rec["host"], rec["kind"])] = rec
                except (ValueError, KeyError):
                    bad += 1
        if bad:
            logger.warning("cache %s: %d righe illeggibili ignorate", self.path, bad)

    def get(self, host: str, kind: str) -> dict[str, Any] | None:
        rec = self._data.get((host, kind))
        if rec is None:
            return None
        try:
            ts = datetime.fromisoformat(rec["ts"])
        except (KeyError, ValueError):
            return None
        if datetime.now(timezone.utc) - ts > self.ttl:
            return None
        return rec

    def put(self, host: str, kind: str, votes: list[Vote] | None = None, info: dict[str, Any] | None = None) -> None:
        rec = {"host": host, "kind": kind, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "votes": [v.as_dict() for v in (votes or [])], "info": info or {}}
        with self._lock:
            self._data[(host, kind)] = rec
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
class Pipeline:
    def __init__(self, cfg: dict[str, Any], lex: Lexicon, cache: Cache, *, ollama: OllamaClient | None = None,
                 country: str | None = None):
        self.cfg = cfg
        self.lex = lex
        self.cache = cache
        self.stages = cfg["stages"]
        self.stop = float(cfg["stop_confidence"])
        self.extra_infra = set(cfg.get("extra_infrastructure_domains") or [])
        self.ollama = ollama
        # Codice paese del batch corrente (es. "PL"): abilita le voci di lessico valide SOLO per
        # quel paese (country_overrides), altrimenti si usa solo il lessico globale.
        self.country = country.upper() if country else None
        self.votes: dict[str, list[Vote]] = {}
        self.info: dict[str, dict[str, Any]] = {}

    # -- utilità ----------------------------------------------------------
    def enabled(self, name: str) -> bool:
        return bool(self.stages.get(name, {}).get("enabled"))

    def _verdict(self, key: str) -> Verdict:
        return combine(self.votes.get(key, []))

    def _done(self, key: str) -> bool:
        v = self._verdict(key)
        if v.natura == "infrastruttura" and v.conf_natura >= 0.9:
            return True
        return v.conf_natura >= self.stop and v.conf_tipo >= self.stop and not v.conflict

    def _pending(self, sites: list[Site]) -> list[Site]:
        return [s for s in sites if not self._done(s.key)]

    def _add(self, key: str, votes: list[Vote], info: dict[str, Any] | None = None) -> None:
        self.votes.setdefault(key, []).extend(votes)
        if info:
            self.info.setdefault(key, {}).update(info)

    # -- stadi ------------------------------------------------------------
    def stage0(self, sites: list[Site]) -> None:
        for s in sites:
            votes, _ = run_stage0(s.key, self.lex, self.extra_infra, self.country)
            self._add(s.key, votes)
        logger.info("stadio 0: %d siti valutati", len(sites))

    def stage2(self, sites: list[Site]) -> None:
        cfg = self.stages["stage2"]
        todo = [s for s in sites if self.cache.get(s.key, "stage2") is None]
        if todo:
            logger.info("stadio 2 (Wikidata): %d host da interrogare (%d già in cache)", len(todo), len(sites) - len(todo))
            run_stage2(
                [s.key for s in todo], self.lex, endpoint=cfg["endpoint"], batch_size=int(cfg["batch_size"]),
                language=cfg["language"], sleep_seconds=float(cfg["sleep_seconds"]),
                on_result=lambda h, votes, info: self.cache.put(h, "stage2", votes, info),
            )
        for s in sites:
            rec = self.cache.get(s.key, "stage2")
            if rec:
                self._add(s.key, [Vote.from_dict(v) for v in rec["votes"]],
                          {"wikidata": ",".join(rec["info"].get("wikidata_items", [])),
                           "wikidata_label": rec["info"].get("wikidata_label", "")})

    def _ensure_evidence(self, sites: list[Site], workers: int) -> None:
        """Riempie la cache 'evidence' per i siti che non l'hanno (rete, in parallelo)."""
        cfg = {**S3_DEFAULTS, **self.stages.get("stage3", {})}
        todo = [s for s in sites if self.cache.get(s.key, "evidence") is None]
        if not todo:
            return
        logger.info("homepage: %d siti da aprire (%d già in cache), %d thread", len(todo), len(sites) - len(todo), workers)
        limiter = HostLimiter(float(cfg["per_host_delay"]))
        robots = make_robots(cfg)
        progress = _Progress(logger, "homepage", len(todo))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {pool.submit(collect_evidence, s.hosts, self.lex, cfg, limiter, robots): s for s in todo}
            try:
                for fut in as_completed(futs):
                    s = futs[fut]
                    try:
                        ev = fut.result()
                    except Exception as exc:  # noqa: BLE001 - un sito non deve fermare gli altri
                        logger.warning("homepage %s: errore interno %s: %s", s.key, exc.__class__.__name__, exc)
                        ev = {"status": "http_error", "detail": f"errore interno: {exc}"}
                    self.cache.put(s.key, "evidence", None, {"evidence": ev})
                    progress.tick()
            except KeyboardInterrupt:
                for f in futs:
                    f.cancel()
                raise

    def _evidence(self, key: str) -> dict[str, Any]:
        rec = self.cache.get(key, "evidence")
        return (rec or {}).get("info", {}).get("evidence", {}) or {}

    def stage3(self, sites: list[Site]) -> None:
        workers = int(self.stages["stage3"].get("workers", 8))
        self._ensure_evidence(sites, workers)
        n_scored = 0
        for s in sites:
            ev = self._evidence(s.key)
            if not ev:
                continue
            # Miglior nome "leggibile" trovato in homepage, in ordine di affidabilità: og:site_name >
            # nome nel JSON-LD (schema.org) > <title> grezzo. Serve solo a capire a colpo d'occhio a
            # quale ente si riferisce il sito; non entra nel punteggio natura/tipo.
            site_name = (
                ev.get("site_name") or (ev.get("jsonld_names") or [""])[0] or ev.get("title", "")
            ).strip()
            self._add(s.key, score_evidence(ev, self.lex, self.country), {
                "homepage_status": ev.get("status", ""), "final_url": ev.get("final_url", ""), "lang": ev.get("lang", ""),
                "site_name": site_name,
            })
            n_scored += ev.get("status") in SCORABLE_STATUS
        logger.info("stadio 3: %d siti con homepage leggibile su %d", n_scored, len(sites))

    def stage4(self, sites: list[Site]) -> None:
        cfg = self.stages["stage4"]
        client = self.ollama or OllamaClient(cfg["ollama_url"], cfg["model"], float(cfg["timeout"]), int(cfg["num_ctx"]))
        ok, msg = client.check()
        if not ok:
            logger.error("stadio 4 saltato: %s", msg)
            return
        threshold = float(cfg.get("only_if_below", self.stop))
        targets = []
        for s in sites:
            v = self._verdict(s.key)
            if min(v.conf_natura, v.conf_tipo) >= threshold and not v.conflict:
                continue
            targets.append(s)
        # serve l'evidenza: se lo stadio 3 non è attivo la raccolgo qui (senza produrre voti di stadio 3)
        self._ensure_evidence(targets, int(self.stages.get("stage3", {}).get("workers", 8)))
        kind = f"stage4:{client.model}:{PROMPT_VERSION}"
        to_call = []
        for s in targets:
            ev = self._evidence(s.key)
            if ev.get("status") not in SCORABLE_STATUS:
                continue
            rec = self.cache.get(s.key, kind)
            if rec is None:
                to_call.append(s)
        logger.info("stadio 4 (%s): %d siti da classificare con l'LLM (%d già in cache)", client.model, len(to_call), len(targets) - len(to_call))

        def call(s: Site):
            return s, client.classify(s.key, self._evidence(s.key))

        workers = max(1, int(cfg.get("workers", 1)))
        progress = _Progress(logger, "LLM", len(to_call))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for s, answer in pool.map(call, to_call):
                if answer is not None:
                    self.cache.put(s.key, kind, votes_from_llm(answer, float(cfg["confidence"])), {"answer": answer})
                progress.tick()
        for s in targets:
            rec = self.cache.get(s.key, kind)
            if rec:
                self._add(s.key, [Vote.from_dict(v) for v in rec["votes"]], {"llm_model": client.model})

    # -- esecuzione ---------------------------------------------------------
    def run(self, sites: list[Site]) -> dict[str, Verdict]:
        if self.enabled("stage0"):
            self.stage0(sites)
        if self.enabled("stage2"):
            pending = self._pending(sites)
            if pending:
                self.stage2(pending)
        if self.enabled("stage3"):
            pending = self._pending(sites)
            if pending:
                self.stage3(pending)
        if self.enabled("stage4"):
            pending = self._pending(sites)
            if pending:
                self.stage4(pending)
        return {s.key: self._verdict(s.key) for s in sites}

    # -- output -------------------------------------------------------------
    def result_row(self, site: Site | None, verdict: Verdict | None) -> dict[str, str]:
        if site is None or verdict is None:
            return {"natura": "non_classificabile", "tipo": "sconosciuto", "da_rivedere": "si", "evidenza": "riga senza host valido"}
        info = self.info.get(site.key, {})
        needs_review = (
            verdict.natura != "infrastruttura"
            and (verdict.conf_natura < float(self.cfg["review_below_natura"])
                 or verdict.conf_tipo < float(self.cfg["review_below_tipo"])
                 or verdict.conflict or verdict.natura in ("incerto", "non_classificabile"))
        )
        # "A quale ente si riferisce?": la fonte più affidabile disponibile, in ordine — nome dal
        # registro/CSV di input (quando il paese ne ha uno), poi l'etichetta dell'item Wikidata
        # (stadio 2, nome ufficiale), infine il nome che il sito dà di sé stesso in homepage (stadio 3).
        ente = (
            (site.nome_ente or "").strip()
            or str(info.get("wikidata_label", "")).strip()
            or str(info.get("site_name", "")).strip()
        )
        return {
            "host_classificato": site.key,
            "natura": verdict.natura, "tipo": verdict.tipo, "ente_rilevato": ente,
            "confidenza_natura": f"{verdict.conf_natura:.2f}", "confidenza_tipo": f"{verdict.conf_tipo:.2f}",
            "stadi": "+".join(s.replace("stadio", "") for s in verdict.stages),
            "evidenza": " || ".join(verdict.evidence)[:400],
            "homepage_status": str(info.get("homepage_status", "")), "final_url": str(info.get("final_url", "")),
            "lang": str(info.get("lang", "")), "wikidata": str(info.get("wikidata", "")),
            "llm_model": str(info.get("llm_model", "")),
            "da_rivedere": "si" if needs_review else "no",
            "classified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def write_output(self, rows: list[dict[str, Any]], sites: list[Site], verdicts: dict[str, Verdict], out_path: Path) -> Counter:
        by_row_id: dict[int, Site] = {id(r): s for s in sites for r in s.rows}
        base_fields: list[str] = []
        for r in rows:
            for k in r:
                if k not in base_fields and k not in OUTPUT_FIELDS:
                    base_fields.append(k)
        out_rows = []
        counts: Counter = Counter()
        for r in rows:
            s = by_row_id.get(id(r))
            res = self.result_row(s, verdicts.get(s.key) if s else None)
            counts[(res.get("natura", ""), res.get("tipo", ""))] += 1
            out_rows.append({**r, **res})
        atomic_write_csv(out_rows, base_fields + OUTPUT_FIELDS, out_path)
        return counts
