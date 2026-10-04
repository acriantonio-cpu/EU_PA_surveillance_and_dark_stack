# EN: Stage 4 - local LLM (Ollama) on the compact evidence bundle, only for still-unresolved
# sites; closed labels, JSON-schema output, temperature 0, fixed conservative confidence.
"""Stadio 4 - LLM locale (Ollama) sul pacchetto di evidenza, solo sul residuo.

Principi:
  - l'LLM NON legge la homepage grezza: legge un pacchetto compatto (titolo, descrizione, h1,
    lingua, host, voci di menu, footer, inizio del testo) di ~400-600 token;
  - etichette CHIUSE + output vincolato con JSON schema ('format' di Ollama), temperatura 0;
  - la risposta 'incerto'/'sconosciuto' è lecita: meglio astenersi che inventare;
  - la confidenza dichiarata da un modello piccolo NON è affidabile: lo stadio vota con una
    confidenza fissa e prudente (llm_confidence) e l'accordo con gli altri stadi la alza
    (combinazione dei voti in taxonomy.combine).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from .taxonomy import VOTABLE_NATURE, VOTABLE_TYPES, Vote

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
DEFAULT_URL = "http://localhost:11434"

SYSTEM_PROMPT = """You classify websites of European organisations from a short evidence packet taken from their homepage.
Answer ONLY with the requested JSON.

Field "natura" (nature of the organisation):
- pubblico: public administration / public-sector body (state, region, municipality, police, public school, public university, public hospital, agency...)
- privato: private company, private school/university/clinic, for-profit business
- terzo_settore: association, foundation, NGO, charity, trade union, sports/hunting/fishing club, volunteer organisation — NOT a state body, but often publicly funded/subsidised
- incerto: cannot tell, or mixed / publicly-owned company
- infrastruttura: not an organisation site (CDN, social network, URL shortener, app store, widget...)

Field "tipo" (type of organisation):
- comune: municipality / city / town / commune / village administration
- regione_provincia: region, province, county, department, federal state, regional government
- governo_centrale: national government, ministry, head of state/government office
- polizia_sicurezza: police, gendarmerie, security forces
- scuola: school (any level below university)
- universita: university, polytechnic, higher-education institution
- sanita: hospital, health authority, clinic
- giustizia: courts, prosecutors, justice administration
- parlamento: parliament, senate, legislative assembly
- agenzia_authority: national agency, authority, regulator, institute, central bank, statistics office
- emergenza: fire brigade, civil protection, emergency services
- cultura: museum, library, archive, theatre
- trasporti: public transport, railway, airport
- ambiente: environmental protection agency, water board / river-basin authority, environment agency
- altro: something else
- non_applicabile: only if natura is infrastruttura

Rules: base the answer on the evidence only. If the evidence is too generic, use natura "incerto". "motivo" is one short sentence (max 25 words) citing the decisive clue."""


def build_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "natura": {"type": "string", "enum": list(VOTABLE_NATURE)},
            "tipo": {"type": "string", "enum": list(VOTABLE_TYPES) + ["non_applicabile"]},
            "motivo": {"type": "string"},
        },
        "required": ["natura", "tipo", "motivo"],
    }


def evidence_packet(host: str, ev: dict[str, Any], max_chars: int = 2200) -> str:
    """Pacchetto compatto per il prompt (testo, non JSON: meno token)."""
    lines = [
        f"host: {host}",
        f"final_host: {ev.get('final_host', '')}",
        f"html_lang: {ev.get('lang', '')}",
        f"title: {ev.get('title', '')}",
        f"site_name: {ev.get('site_name', '')}",
        f"description: {ev.get('description', '')}",
        f"h1: {' | '.join(ev.get('h1', []))}",
        f"schema.org types: {', '.join(ev.get('jsonld_types', []))}",
        f"schema.org names: {' | '.join(ev.get('jsonld_names', []))}",
        f"menu: {' | '.join(ev.get('nav', [])[:20])}",
        f"footer: {ev.get('footer', '')[:400]}",
        f"legal/about page: {ev.get('legal', '')[:500]}",
        f"text: {ev.get('body', '')[:800]}",
    ]
    return "\n".join(l for l in lines if not l.endswith(": "))[:max_chars]


class OllamaClient:
    def __init__(self, url: str = DEFAULT_URL, model: str = "gemma4:e2b_q8", timeout: float = 120.0,
                 num_ctx: int = 4096, session: requests.Session | None = None):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.s = session or requests.Session()

    def check(self) -> tuple[bool, str]:
        """(pronto, messaggio). Verifica che il server risponda e che il modello esista."""
        try:
            r = self.s.get(self.url + "/api/tags", timeout=10)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
        except (requests.RequestException, ValueError) as exc:
            return False, f"Ollama non raggiungibile su {self.url} ({exc}). Avvialo con 'ollama serve'."
        if self.model not in names and not any(n.split(":")[0] == self.model for n in names):
            return False, f"Modello '{self.model}' non trovato in Ollama. Modelli disponibili: {', '.join(names) or '(nessuno)'}"
        return True, "ok"

    def classify(self, host: str, ev: dict[str, Any]) -> dict[str, Any] | None:
        body = {
            "model": self.model,
            "stream": False,
            "format": build_schema(),
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Evidence packet:\n" + evidence_packet(host, ev)},
            ],
        }
        for attempt in (1, 2):
            try:
                r = self.s.post(self.url + "/api/chat", json=body, timeout=self.timeout)
                r.raise_for_status()
                content = r.json().get("message", {}).get("content", "")
                data = json.loads(content)
                if data.get("natura") in VOTABLE_NATURE and data.get("tipo") in (*VOTABLE_TYPES, "non_applicabile"):
                    return data
                logger.warning("LLM: risposta fuori schema per %s: %s", host, str(content)[:120])
            except (requests.RequestException, ValueError) as exc:
                logger.warning("LLM: errore su %s (tentativo %d/2): %s", host, attempt, exc)
        return None


def votes_from_llm(answer: dict[str, Any], confidence: float) -> list[Vote]:
    """Risposta -> voti. 'incerto' non è un voto forte: pesa metà."""
    votes: list[Vote] = []
    motivo = str(answer.get("motivo", ""))[:140]
    nat = answer["natura"]
    votes.append(Vote("natura", nat, confidence * (0.5 if nat == "incerto" else 1.0), "stadio4", motivo))
    tipo = answer["tipo"]
    if tipo not in ("non_applicabile",) and nat != "infrastruttura":
        votes.append(Vote("tipo", tipo, confidence * (0.6 if tipo == "altro" else 1.0), "stadio4", motivo))
    return votes
