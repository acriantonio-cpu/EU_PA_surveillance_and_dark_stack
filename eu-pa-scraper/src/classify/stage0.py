# EN: Stage 0 - hostname/suffix rules: no network, high precision, low coverage.
"""Stadio 0 - regole su hostname/suffisso. Zero rete, alta precisione, bassa copertura."""

from __future__ import annotations

import re

from ..web_signals import is_infrastructure
from .lexicon import Lexicon
from .taxonomy import PUBLIC_BY_NATURE, Vote
from .textnorm import fold

TOKEN_CONF = 0.6
TOKEN_NATURE_CONF = 0.5
TLD_WEAK_CONF = 0.35


def run_stage0(host: str, lex: Lexicon, extra_infra: set[str] | None = None, country: str | None = None) -> tuple[list[Vote], dict]:
    votes: list[Vote] = []
    host = (host or "").lower()
    if is_infrastructure(host, extra_infra):
        votes.append(Vote("natura", "infrastruttura", 0.95, "stadio0", "dominio infrastrutturale noto"))
        return votes, {}

    for pat in lex.gov_suffix:
        if pat.search(host):
            votes.append(Vote("natura", "pubblico", lex.gov_suffix_conf, "stadio0", f"suffisso {pat.pattern}"))
            break

    # Prima si divide l'host in label ('.', '-', '_'), poi si normalizza ciascuno (fold cancellerebbe i separatori).
    labels = [fold(p).replace(" ", "") for p in re.split(r"[.\-_]", host)]
    labels = [p for p in labels if p and p != "www"]
    for tipo, tokens in lex.host_tokens.items():
        hit = None
        for label in labels:
            if any(label.startswith(ex) for ex in lex.host_exclusions):
                continue
            for tok, exact in tokens:
                if not tok:
                    continue
                if (label == tok) if exact else label.startswith(tok):
                    hit = label
                    break
            if hit:
                break
        if hit:
            votes.append(Vote("tipo", tipo, TOKEN_CONF, "stadio0", f"token host '{hit}'"))
            if tipo in PUBLIC_BY_NATURE:
                votes.append(Vote("natura", "pubblico", TOKEN_NATURE_CONF, "stadio0", f"token host '{hit}'"))

    # Segnale debole basato sul TLD, SOLO se il paese ha 'allowed_tlds' dichiarati in country_overrides
    # (opt-in esplicito: senza quella voce, questo blocco non fa nulla). Su un campione REALE ed
    # etichettato a mano di 60 host 'sconosciuto' di NL, i domini .nl/.org erano pubblico/terzo
    # settore nel 57% dei casi, contro il 2,4% di .com/.fr/.de/.fi/.uk — un segnale reale, non
    # circolare, ma su un campione ancora piccolo: per questo resta debole (TOKEN_NATURE_CONF più
    # basso) e non decisivo da solo, non un filtro che esclude nulla a priori. Un sito con prove
    # forti altrove (lessico, Wikidata) non viene comunque spinto verso 'privato' da questo solo.
    tld = labels[-1] if labels else ""
    allowed = lex.allowed_tlds_for(country) if country else None
    if allowed is not None and tld and tld not in allowed:
        votes.append(Vote("natura", "privato", TLD_WEAK_CONF, "stadio0",
                           f"TLD .{tld} non tra quelli tipici per {country} ({','.join(sorted(allowed))})"))
    return votes, {}
