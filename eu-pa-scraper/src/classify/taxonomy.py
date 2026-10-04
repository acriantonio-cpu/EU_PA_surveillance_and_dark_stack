"""Etichette, voti e combinazione dei voti dei vari stadi."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

NATURE = ("pubblico", "privato", "terzo_settore", "incerto", "infrastruttura", "non_classificabile")

TYPES = (
    "comune", "regione_provincia", "governo_centrale", "polizia_sicurezza", "scuola", "universita",
    "sanita", "giustizia", "parlamento", "agenzia_authority", "emergenza", "cultura", "trasporti",
    "ambiente", "altro", "non_applicabile", "sconosciuto",
)

# Tipi che sono (quasi) sempre pubblici: un voto su questi porta un voto debole su natura=pubblico.
PUBLIC_BY_NATURE = frozenset({
    "comune", "regione_provincia", "governo_centrale", "polizia_sicurezza", "giustizia", "parlamento",
    "emergenza", "ambiente",
})

# Valori che i singoli stadi possono votare (esclusi 'non_classificabile' / 'sconosciuto' /
# 'non_applicabile', che sono esiti finali, non voti).
VOTABLE_NATURE = ("pubblico", "privato", "terzo_settore", "incerto", "infrastruttura")
VOTABLE_TYPES = tuple(t for t in TYPES if t not in ("non_applicabile", "sconosciuto"))


@dataclass
class Vote:
    field: str            # 'natura' | 'tipo'
    value: str
    conf: float           # 0..1
    stage: str            # 'stadio0' | 'stadio2' | 'stadio3' | 'stadio4'
    evidence: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Vote":
        return Vote(d["field"], d["value"], float(d["conf"]), d["stage"], d.get("evidence", ""))


@dataclass
class Verdict:
    natura: str = "non_classificabile"
    tipo: str = "sconosciuto"
    conf_natura: float = 0.0
    conf_tipo: float = 0.0
    stages: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    conflict: bool = False


def _resolve(votes: list[Vote]) -> tuple[str | None, float, bool]:
    """noisy-OR per valore: conf(v) = 1 - prod(1 - c_i). Sceglie il valore con conf
    maggiore; la confidenza finale è penalizzata dal secondo (0.5 * conf del secondo)."""
    by_value: dict[str, float] = {}
    for v in votes:
        c = min(max(v.conf, 0.0), 0.99)
        by_value[v.value] = 1.0 - (1.0 - by_value.get(v.value, 0.0)) * (1.0 - c)
    if not by_value:
        return None, 0.0, False
    ranked = sorted(by_value.items(), key=lambda kv: kv[1], reverse=True)
    best_v, best_c = ranked[0]
    second_c = ranked[1][1] if len(ranked) > 1 else 0.0
    conflict = second_c >= 0.5 * best_c and second_c >= 0.4
    return best_v, max(0.0, best_c - 0.5 * second_c), conflict


def combine(votes: list[Vote]) -> Verdict:
    """Combina i voti di tutti gli stadi eseguiti in un verdetto."""
    nat_votes = [v for v in votes if v.field == "natura"]
    tipo_votes = [v for v in votes if v.field == "tipo"]
    natura, cn, conf_n = _resolve(nat_votes)
    tipo, ct, conf_t = _resolve(tipo_votes)
    verdict = Verdict(conflict=conf_n or conf_t)
    if natura:
        verdict.natura, verdict.conf_natura = natura, round(cn, 3)
    if natura == "infrastruttura":
        verdict.tipo, verdict.conf_tipo = "non_applicabile", verdict.conf_natura
    elif tipo:
        verdict.tipo, verdict.conf_tipo = tipo, round(ct, 3)
    stages = []
    for v in votes:
        if v.stage not in stages:
            stages.append(v.stage)
    verdict.stages = stages
    winners = {natura, tipo}
    ev = [f"{v.stage}:{v.evidence}" for v in sorted(votes, key=lambda v: -v.conf) if v.value in winners and v.evidence]
    verdict.evidence = ev[:4]
    return verdict


def squash(score: float, scale: float = 6.0) -> float:
    """Punteggio grezzo (>=0) -> 0..1 con saturazione."""
    return 1.0 - math.exp(-max(score, 0.0) / scale)
