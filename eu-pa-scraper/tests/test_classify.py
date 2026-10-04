# EN: Tests for the site classifier (stages 0, 2, 3, 4) against local fake servers.
"""Test del classificatore (stadi 0, 2, 3, 4) contro server locali finti."""

from __future__ import annotations

import csv
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from src.classify import stage3_homepage as s3
from src.classify.lexicon import load_lexicon
from src.classify.pipeline import Cache, Pipeline, load_config
from src.classify.sites import Site, load_rows, sites_from_rows
from src.classify.stage0 import run_stage0
from src.classify.stage2_wikidata import build_query, map_class_labels, run_stage2
from src.classify.stage4_llm import OllamaClient
from src.classify.taxonomy import Vote, combine

LEX = load_lexicon()


def nat(votes):
    return {(v.field, v.value) for v in votes}


# ---------------------------------------------------------------- lessico/tassonomia
def test_lexicon_loads_and_validates():
    assert LEX.tipo_patterns["scuola"] is not None and LEX.gov_suffix
    assert set(LEX.tipo_patterns) >= {"comune", "regione_provincia", "polizia_sicurezza", "governo_centrale", "scuola", "universita", "sanita"}


def test_combine_noisy_or_and_conflict():
    v = combine([Vote("tipo", "comune", 0.6, "stadio0"), Vote("tipo", "comune", 0.7, "stadio3"), Vote("natura", "pubblico", 0.9, "stadio0")])
    assert v.tipo == "comune" and v.conf_tipo > 0.8 and v.natura == "pubblico" and not v.conflict
    c = combine([Vote("tipo", "scuola", 0.7, "stadio3"), Vote("tipo", "sanita", 0.65, "stadio4")])
    assert c.conflict and c.conf_tipo < 0.5
    i = combine([Vote("natura", "infrastruttura", 0.95, "stadio0")])
    assert i.tipo == "non_applicabile"
    assert combine([]).natura == "non_classificabile" and combine([]).tipo == "sconosciuto"


# ------------------------------------------------------------------------ stadio 0
@pytest.mark.parametrize("host,expect", [
    ("www.google.de", {("natura", "infrastruttura")}),
    ("sede.gob.es", {("natura", "pubblico")}),
    ("www.bmi.bund.de", {("natura", "pubblico")}),
    ("gemeente-utrecht.nl", {("tipo", "comune"), ("natura", "pubblico")}),
    ("www.polizei-berlin.de", {("tipo", "polizia_sicurezza")}),
    ("uni-heidelberg.de", {("tipo", "universita")}),
    ("ziekenhuis-x.be", {("tipo", "sanita")}),
    ("um.warszawa.pl", {("tipo", "comune")}),
    ("mairie-de-lyon.fr", {("tipo", "comune")}),
])
def test_stage0(host, expect):
    votes, _ = run_stage0(host, LEX)
    assert expect <= nat(votes), (host, nat(votes))


def test_stage0_exclusions_and_no_false_public():
    votes, _ = run_stage0("stadtwerke-musterstadt.de", LEX)
    assert ("tipo", "comune") not in nat(votes)
    votes, _ = run_stage0("school-supplies-shop.com", LEX)
    assert ("natura", "pubblico") not in nat(votes)      # 'school' dà il tipo ma NON rende pubblico
    votes, _ = run_stage0("www.example.com", LEX)
    assert votes == []


# ------------------------------------------------------------------ stadio 3 (offline)
def evidence(html: str, url="https://x.test/"):
    return s3.extract_evidence(html, url, LEX)


def test_stage3_municipality_dutch():
    ev = evidence("<html lang='nl'><head><title>Gemeente Utrecht - Home</title><meta name='description' content='Officiële website van de gemeente Utrecht'></head>"
                  "<body><nav><a>Burgemeester en wethouders</a><a>Afval</a></nav><h1>Welkom bij de gemeente</h1></body></html>")
    ev["status"] = "ok"
    v = combine(s3.score_evidence(ev, LEX))
    assert v.tipo == "comune" and v.natura == "pubblico" and v.conf_tipo > 0.55


def test_stage3_school_and_private_shop_and_polish_police():
    school = evidence("<html lang='es'><head><title>Colegio San José</title></head><body><h1>Colegio San José - Educación Primaria</h1><p>curso escolar 2026</p></body></html>")
    school["status"] = "ok"
    assert combine(s3.score_evidence(school, LEX)).tipo == "scuola"
    shop = evidence("<html><head><title>ACME GmbH - Online Shop</title></head><body><a>Add to cart</a><p>Buy now, free trial, our clients</p></body></html>")
    shop["status"] = "ok"
    assert combine(s3.score_evidence(shop, LEX)).natura == "privato"
    pl = evidence("<html lang='pl'><head><title>Komenda Powiatowa Policji w Krakowie</title></head><body><h1>Policja</h1></body></html>")
    pl["status"] = "ok"
    v = combine(s3.score_evidence(pl, LEX))
    assert v.tipo == "polizia_sicurezza" and v.natura == "pubblico"


def test_stage3_ambiente_is_consistent_across_languages():
    """Autorità idriche/ambientali con nomi completamente diversi (NL/IT/IT) devono finire
    sotto lo STESSO tipo, indipendentemente dalla lingua."""
    nl = evidence("<html><head><title>Waterschap Rivierenland</title></head><body>waterbeheer dijkgraaf</body></html>")
    nl["status"] = "ok"
    it = evidence("<html><head><title>Consorzio di Bonifica della Romagna</title></head>"
                   "<body>autorita di bacino gestione idrica</body></html>")
    it["status"] = "ok"
    arpa = evidence("<html><head><title>ARPA Lombardia</title></head>"
                     "<body>agenzia regionale per la protezione dell ambiente</body></html>")
    arpa["status"] = "ok"
    for ev in (nl, it, arpa):
        v = combine(s3.score_evidence(ev, LEX))
        assert v.tipo == "ambiente" and v.natura == "pubblico", ev["title"]


def test_stage3_ggd_style_regional_health_service():
    ev = evidence("<html><head><title>GGD IJsselland</title></head><body>GGD publieke gezondheid</body></html>")
    ev["status"] = "ok"
    assert combine(s3.score_evidence(ev, LEX)).tipo == "sanita"


def test_stage3_inspectorate_and_tax_authority():
    insp = evidence("<html><head><title>Inspectie Justitie en Veiligheid</title></head><body>rijksinspectie</body></html>")
    insp["status"] = "ok"
    assert combine(s3.score_evidence(insp, LEX)).tipo == "agenzia_authority"
    tax = evidence("<html><head><title>Belastingdienst</title></head><body>belastingdienst aangifte</body></html>")
    tax["status"] = "ok"
    assert combine(s3.score_evidence(tax, LEX)).tipo == "agenzia_authority"


def test_stage3_terzo_settore_is_distinct_from_public_and_private():
    """Associazioni/fondazioni/club (potenzialmente sovvenzionati, ma non organi statali) devono
    finire in una natura A SE', non 'pubblico' e non 'privato' — a prescindere dalla lingua."""
    it = evidence("<html><head><title>Associazione Pescatori del Ticino</title></head>"
                   "<body>Associazione sportiva dilettantistica, pesca sportiva, tesseramento soci</body></html>")
    it["status"] = "ok"
    de = evidence("<html><head><title>Angelverein Rheinperle e.V.</title></head>"
                   "<footer>Eingetragener Verein, Angelverein, Satzung, Mitgliederversammlung</footer></html>")
    de["status"] = "ok"
    for ev in (it, de):
        v = combine(s3.score_evidence(ev, LEX))
        assert v.natura == "terzo_settore", (ev["title"], v.natura)
    # non deve invadere il territorio di 'privato' (shop) né di 'pubblico' (comune)
    shop = evidence("<html><head><title>ACME GmbH - Online Shop</title></head><body><a>Add to cart</a><p>Buy now, free trial, our clients</p></body></html>")
    shop["status"] = "ok"
    assert combine(s3.score_evidence(shop, LEX)).natura == "privato"
    com = evidence("<html lang='nl'><head><title>Gemeente Utrecht - Home</title><meta name='description' content='Officiële website van de gemeente Utrecht'></head>"
                    "<body><nav><a>Burgemeester en wethouders</a><a>Afval</a></nav><h1>Welkom bij de gemeente</h1></body></html>")
    com["status"] = "ok"
    assert combine(s3.score_evidence(com, LEX)).natura == "pubblico"


def test_lexicon_audit_all_24_eu_languages_have_state_name_marker():
    """Il nome ufficiale dello Stato ('Repubblica'/'Regno'...) e' il segnale piu' forte per
    natura=pubblico: verifica che ESISTA una grafia per ciascuna delle 24 lingue ufficiali UE,
    non solo per le famiglie romanza/germanica occidentale scoperte per prime."""
    from src.classify.textnorm import fold
    official_state_names = {
        "it": "Repubblica Italiana", "fr": "Republique Francaise", "es": "Reino de Espana",
        "de": "Bundesrepublik Deutschland", "nl": "Koninkrijk der Nederlanden", "pt": "Republica Portuguesa",
        "pl": "Rzeczpospolita Polska", "cs": "Ceska republika", "sk": "Slovenska republika",
        "hu": "Magyar Koztarsasag", "ro": "Republica Romania", "bg": "Република България",
        "hr": "Republika Hrvatska", "sl": "Republika Slovenija", "et": "Eesti Vabariik",
        "lv": "Latvijas Republika", "lt": "Lietuvos Respublika", "el": "Ελληνική Δημοκρατία",
        "fi": "Suomen tasavalta", "sv": "Konungariket Sverige", "da": "Kongeriget Danmark",
        "mt": "Repubblika ta' Malta", "ga": "Republic of Ireland",
    }
    pattern = LEX.public_pattern
    for lang, name in official_state_names.items():
        assert distinct_matches_ok(pattern, fold(name)), (lang, name)


def distinct_matches_ok(pattern, text):
    from src.classify.textnorm import distinct_matches
    return bool(distinct_matches(pattern, text))


def test_lexicon_audit_no_false_positive_collisions():
    """Collisioni verificate esplicitamente durante l'audit del lessico: parole aggiunte per
    coprire una lingua non devono scattare su parole comuni di un'ALTRA lingua/contesto."""
    from src.classify.textnorm import distinct_matches, fold
    cases = [
        (LEX.tipo_patterns["polizia_sicurezza"], "this page discusses political and politics topics"),
        (LEX.tipo_patterns["parlamento"], "please read the supplement to this document"),
        (LEX.tipo_patterns["giustizia"], "il sud della francia e le regioni del sud italia"),
        (LEX.terzo_settore_pattern, "mtu friedrichshafen produce motori diesel"),
        # trovato con audit_sample.py su un run reale (www.afp.com, tipo=agenzia_authority conf 0.90):
        # "agenzia!"/"agence!"/"agencia!" da soli intercettavano anche agenzie di stampa/viaggi/immobiliari
        (LEX.tipo_patterns["agenzia_authority"], "agence de presse, agenzia di viaggi, agencia inmobiliaria"),
    ]
    for pattern, text in cases:
        assert distinct_matches(pattern, fold(text)) == set(), text


def test_lexicon_audit_new_multilingual_entries_fire():
    """Voci multilingua aggiunte con l'audit: devono effettivamente scattare (non solo esistere)."""
    def ev(title):
        return {"title": title, "site_name": "", "h1": [], "description": "", "jsonld_types": [],
                "jsonld_names": [], "legal": "", "footer": "", "nav": [], "body": "", "status": "ok"}
    cases = [
        ("Politiet Danmark", "polizia_sicurezza"),
        ("Nationalrat Osterreich", "parlamento"),
        ("Parlamentul Romaniei", "parlamento"),
        ("Kita Sonnenschein", "scuola"),
        ("Conservatorio di Musica", "universita"),
    ]
    for title, expected_tipo in cases:
        v = combine(s3.score_evidence(ev(title), LEX))
        assert v.tipo == expected_tipo, (title, v.tipo)
    for title in ("Dansk Cykel Forening", "Stowarzyszenie Wedkarskie"):
        assert combine(s3.score_evidence(ev(title), LEX)).natura == "terzo_settore", title
    for title in ("Rzeczpospolita Polska", "Kongeriget Danmark"):
        assert combine(s3.score_evidence(ev(title), LEX)).natura == "pubblico", title


def test_specialized_public_bodies_get_natura_pubblico_without_generic_words():
    """528 righe del run reale dell'utente avevano gia' il TIPO corretto (sanita/scuola/cultura/
    universita/agenzia_authority) ma natura=non_classificabile, perche' un GGD/museo/scuola
    comunale non scrive mai 'governo' o 'ministero' in homepage. I marcatori specifici (non le
    parole generiche 'ospedale'/'scuola', che vanno bene anche per il privato) devono bastare."""
    ggd = evidence("<html><head><title>GGD West-Brabant</title></head>"
                    "<footer>GGD West-Brabant is een gemeenschappelijke regeling van 16 gemeenten</footer></html>")
    ggd["status"] = "ok"
    v = combine(s3.score_evidence(ggd, LEX))
    assert v.tipo == "sanita" and v.natura == "pubblico"

    rijksmuseum = evidence("<html><head><title>Rijksmuseum Amsterdam</title></head>"
                            "<footer>Het Rijksmuseum is het nationale museum van Nederland</footer></html>")
    rijksmuseum["status"] = "ok"
    assert combine(s3.score_evidence(rijksmuseum, LEX)).natura == "pubblico"

    # un museo/scuola/ospedale PRIVATO non deve diventare pubblico solo perche' e' un museo/scuola
    private_museum = evidence("<html><head><title>Museum of Modern Art - Private Collection</title></head>"
                               "<body><a>Add to cart</a><p>Buy tickets now, our clients, free trial</p></body></html>")
    private_museum["status"] = "ok"
    assert combine(s3.score_evidence(private_museum, LEX)).natura != "pubblico"


def test_dutch_zbo_and_intermunicipal_bodies_agenzia_authority():
    rekenkamer = evidence("<html><head><title>Algemene Rekenkamer</title></head></html>")
    rekenkamer["status"] = "ok"
    assert combine(s3.score_evidence(rekenkamer, LEX)).tipo == "agenzia_authority"

    werkvoorziening = evidence("<html><head><title>Werkvoorzieningschap Noordoost-Brabant</title></head></html>")
    werkvoorziening["status"] = "ok"
    assert combine(s3.score_evidence(werkvoorziening, LEX)).tipo == "agenzia_authority"


def test_music_school_and_regional_taxi_get_a_tipo():
    music = evidence("<html><head><title>Muziekschool Zeeland</title></head></html>")
    music["status"] = "ok"
    assert combine(s3.score_evidence(music, LEX)).tipo == "scuola"

    taxi = evidence("<html><head><title>Regiotaxi Noordoost-Brabant</title></head></html>")
    taxi["status"] = "ok"
    assert combine(s3.score_evidence(taxi, LEX)).tipo == "trasporti"


def test_news_media_gets_a_tipo_not_just_a_natura():
    """Prima: NewsMediaOrganization dava solo natura=privato, il tipo restava sconosciuto."""
    reuters_jsonld = evidence("<html><head><title>Reuters</title></head></html>")
    reuters_jsonld["status"] = "ok"
    reuters_jsonld["jsonld_types"] = ["NewsMediaOrganization"]
    v = combine(s3.score_evidence(reuters_jsonld, LEX))
    assert v.tipo == "altro" and v.natura == "privato"

    press_agency = evidence("<html><head><title>Associated Press - a global news agency</title></head></html>")
    press_agency["status"] = "ok"
    assert combine(s3.score_evidence(press_agency, LEX)).tipo == "altro"


def test_nl_acronyms_need_both_country_and_lang():
    """RDW/UWV sono troppo corte per essere sicure a livello globale (potrebbero essere le
    iniziali di qualunque azienda altrove): devono scattare SOLO con country=NL e lang=nl insieme."""
    rdw = evidence("<html lang='nl'><head><title>RDW</title></head>"
                    "<footer>RDW is de rijksdienst voor het wegverkeer</footer></html>")
    rdw["status"] = "ok"
    assert combine(s3.score_evidence(rdw, LEX, country="NL")).tipo == "agenzia_authority"
    assert combine(s3.score_evidence(rdw, LEX)).tipo != "agenzia_authority"                  # senza country: niente
    assert combine(s3.score_evidence(rdw, LEX, country="DE")).tipo != "agenzia_authority"     # country sbagliato: niente

    rdw_no_lang = evidence("<html><head><title>RDW</title></head>"
                            "<footer>RDW is de rijksdienst voor het wegverkeer</footer></html>")
    rdw_no_lang["status"] = "ok"
    assert combine(s3.score_evidence(rdw_no_lang, LEX, country="NL")).tipo != "agenzia_authority"  # senza lang: niente


def test_allowed_tlds_weak_signal_not_decisive_alone():
    """TLD 'atipico' per il country dichiarato: un voto DEBOLE verso privato, mai un'esclusione,
    mai capace da solo di battere un segnale pubblico più forte trovato altrove."""
    votes, _ = run_stage0("www.eurosport.de", LEX, country="NL")
    v = combine(votes)
    assert v.natura == "privato" and v.conf_natura < 0.4     # debole: confidenza bassa, non un verdetto forte

    votes, _ = run_stage0("innovatielabs.org", LEX, country="NL")     # .org è tra gli ammessi per NL
    assert combine(votes).natura != "privato"

    votes, _ = run_stage0("www.rijksoverheid.nl", LEX, country="NL")  # .nl è ammesso
    assert combine(votes).natura != "privato"

    # senza country, o con un country che non ha dichiarato allowed_tlds: nessun segnale, MAI
    votes, _ = run_stage0("www.eurosport.de", LEX)
    assert combine(votes).natura != "privato"
    votes, _ = run_stage0("www.eurosport.de", LEX, country="PL")      # PL non ha allowed_tlds dichiarati
    assert combine(votes).natura != "privato"

    # un voto pubblico più forte trovato altrove (es. stadio3) prevale comunque sul TLD estraneo
    from src.classify.taxonomy import Vote as V
    combined = combine(votes + [V("natura", "pubblico", 0.8, "stadio3", "marcatori pubblici forti")])
    assert combined.natura == "pubblico"


def test_country_overrides_apply_only_to_their_country_and_language():
    """'sad!'/'sud!' sono troppo rischiosi a livello globale (collidono con l'inglese 'sad' e il
    francese 'sud'): devono scattare SOLO quando country E lingua della pagina combaciano ENTRAMBI
    con quanto dichiarato in country_overrides — non uno dei due da solo."""
    pl_court = evidence("<html lang='pl-PL'><head><title>Sad Rejonowy w Warszawie</title></head></html>")
    pl_court["status"] = "ok"
    assert combine(s3.score_evidence(pl_court, LEX)).tipo != "giustizia"                   # senza country: niente
    assert combine(s3.score_evidence(pl_court, LEX, country="FR")).tipo != "giustizia"      # country sbagliato: niente
    assert combine(s3.score_evidence(pl_court, LEX, country="pl")).tipo == "giustizia"      # country giusto (case-insensitive)

    # 'nl-NL' / 'PL' / 'pl-PL': grafie diverse dello stesso sottotag, devono combaciare comunque
    pl_court_upper = evidence("<html lang='PL'><head><title>Sad Rejonowy w Warszawie</title></head></html>")
    pl_court_upper["status"] = "ok"
    assert combine(s3.score_evidence(pl_court_upper, LEX, country="PL")).tipo == "giustizia"

    # country giusto ma LINGUA sbagliata (es. la versione inglese dello stesso sito): niente
    pl_court_en = evidence("<html lang='en'><head><title>Sad Rejonowy w Warszawie</title></head></html>")
    pl_court_en["status"] = "ok"
    assert combine(s3.score_evidence(pl_court_en, LEX, country="PL")).tipo != "giustizia"

    # nessuna lingua dichiarata: prudenza, l'override non scatta (anche con country giusto)
    pl_court_nolang = evidence("<html><head><title>Sad Rejonowy w Warszawie</title></head></html>")
    pl_court_nolang["status"] = "ok"
    assert combine(s3.score_evidence(pl_court_nolang, LEX, country="PL")).tipo != "giustizia"

    hr_court = evidence("<html lang='hr'><head><title>Opcinski sud u Zagrebu</title></head></html>")
    hr_court["status"] = "ok"
    assert combine(s3.score_evidence(hr_court, LEX, country="HR")).tipo == "giustizia"

    # il lessico globale (nessun override) continua a funzionare invariato con o senza country
    school = evidence("<html lang='es'><head><title>Colegio San Jose</title></head><body><h1>Colegio San Jose - Educacion Primaria</h1><p>curso escolar 2026</p></body></html>")
    school["status"] = "ok"
    assert combine(s3.score_evidence(school, LEX, country="ES")).tipo == "scuola"


def test_country_override_language_gate_closes_the_known_false_positive():
    """Il falso positivo trovato nell'audit precedente (un testo francese su un sito con
    country=HR faceva scattare 'giustizia' per via di 'sud'=sud geografico): con il filtro sulla
    lingua ORA si chiude, perche' la pagina francese dichiara lang='fr', non 'hr'."""
    french_text_on_hr_site = evidence(
        "<html lang='fr-FR'><head><title>Office du tourisme du Sud de la France</title></head></html>")
    french_text_on_hr_site["status"] = "ok"
    v = combine(s3.score_evidence(french_text_on_hr_site, LEX, country="HR"))
    assert v.tipo != "giustizia"
    # limite residuo, onesto: se la pagina francese non dichiara ALCUNA lingua, il filtro non ha
    # nulla da confrontare e per prudenza blocca comunque l'override (fail-safe, non fail-open)
    french_text_no_lang = evidence(
        "<html><head><title>Office du tourisme du Sud de la France</title></head></html>")
    french_text_no_lang["status"] = "ok"
    assert combine(s3.score_evidence(french_text_no_lang, LEX, country="HR")).tipo != "giustizia"


def test_pipeline_passes_country_down_to_stage3(fake_hosts, tmp_path):
    w = fake_hosts
    w.add("sad-rejonowy.pl", "/", "<html lang='pl'><head><title>Sad Rejonowy Krakow</title></head></html>")
    rows = [{"hostname": "https://sad-rejonowy.pl"}]
    sites = sites_from_rows(rows)
    cfg = load_config(None, {"stage3": True})
    cfg["stages"]["stage3"]["per_host_delay"] = 0
    pipe = Pipeline(cfg, LEX, Cache(tmp_path / "c.jsonl", 90), country="PL")
    verdicts = pipe.run(sites)
    assert verdicts["sad-rejonowy.pl"].tipo == "giustizia"


def test_primary_lang_normalizes_bcp47_variants():
    from src.classify.textnorm import primary_lang
    assert primary_lang("nl-NL") == primary_lang("NL") == primary_lang("nl") == "nl"
    assert primary_lang("hr_HR") == "hr"
    assert primary_lang("") == primary_lang(None) == ""


def test_stage3_greek_and_jsonld():
    gr = evidence("<html lang='el'><head><title>Δήμος Αθηναίων</title></head><body><h1>Δήμος Αθηναίων</h1></body></html>")
    gr["status"] = "ok"
    assert combine(s3.score_evidence(gr, LEX)).tipo == "comune"
    ld = evidence('<html><head><title>Home</title><script type="application/ld+json">{"@context":"https://schema.org","@type":"CollegeOrUniversity","name":"X"}</script></head><body>hi</body></html>')
    ld["status"] = "ok"
    assert combine(s3.score_evidence(ld, LEX)).tipo == "universita"


def test_cookie_banner_does_not_dominate_and_wrapper_survives():
    html = ("<html><head><title>Comune di Bruges</title></head><body>"
            "<div id='cookie-banner'>We use cookies school school school hospital police</div>"
            "<div class='cmp-container'><h1>Gemeente Brugge</h1><p>" + "informazioni sul comune " * 30 + "</p></div></body></html>")
    ev = evidence(html)
    assert "cookies" not in ev["body"] and "Gemeente Brugge" in ev["body"]
    ev["status"] = "ok"
    assert combine(s3.score_evidence(ev, LEX)).tipo == "comune"


def test_stage3_unreadable_gives_no_votes():
    for st in ("blocked", "parked", "dns_error", "timeout", "default_page"):
        assert s3.score_evidence({"status": st, "title": "Comune di X"}, LEX) == []


# ---------------------------------------------------- stadio 3 (rete finta) + pipeline
@pytest.fixture
def fake_hosts(web, monkeypatch):
    monkeypatch.setattr(s3, "_candidate_urls", lambda hosts, n: [f"http://{h}:{web.port}/" for h in hosts[:3]])
    return web


def make_site(host, rows=None):
    return Site(key=host, hosts=[host], rows=rows if rows is not None else [{"hostname": f"https://{host}"}])


def test_collect_evidence_statuses(fake_hosts):
    w = fake_hosts
    w.add("gemeente-x.nl", "/", "<html lang='nl'><head><title>Gemeente X</title></head><body><a href='/impressum'>Impressum</a><h1>Gemeente X</h1></body></html>")
    w.add("spa.nl", "/", "<html><head><title>Portale</title></head><body><div id='root'></div><noscript>enable JavaScript</noscript></body></html>")
    w.add("blocked.nl", "/", "<html><head><title>Just a moment...</title></head><body>cf-chl</body></html>")
    w.add("parked.nl", "/", "<html><body>This domain is for sale</body></html>")
    w.add("default.nl", "/", "<html><head><title>Welcome to nginx!</title></head><body>Welcome to nginx!</body></html>")
    w.add("pdf.nl", "/", b"%PDF-1.4", **{"Content-Type": "application/pdf"})
    w.add("forbidden.nl", "/", "no", status=403)
    limiter = s3.HostLimiter(0)
    cfg = {"per_host_delay": 0}
    got = {h: s3.collect_evidence([h], LEX, cfg, limiter)["status"] for h in
           ["gemeente-x.nl", "spa.nl", "blocked.nl", "parked.nl", "default.nl", "pdf.nl", "forbidden.nl", "nonexistent-404.nl"]}
    assert got == {"gemeente-x.nl": "ok", "spa.nl": "needs_js", "blocked.nl": "blocked", "parked.nl": "parked",
                   "default.nl": "default_page", "pdf.nl": "non_html", "forbidden.nl": "blocked", "nonexistent-404.nl": "http_error"}


def test_legal_page_is_followed_only_when_needed(fake_hosts):
    w = fake_hosts
    w.add("generic.de", "/", "<html><head><title>Startseite</title></head><body><a href='/impressum'>Impressum</a>Willkommen</body></html>")
    w.add("generic.de", "/impressum", "<html><head><title>Impressum</title></head><body>Herausgeber: Stadtverwaltung Musterstadt, Rathaus, Bürgermeister Müller</body></html>")
    ev = s3.collect_evidence(["generic.de"], LEX, {"per_host_delay": 0}, s3.HostLimiter(0))
    assert "/impressum" in w.hit_paths("generic.de") and "Stadtverwaltung" in ev["legal"]
    ev["status"] = "ok"
    assert combine(s3.score_evidence(ev, LEX)).tipo == "comune"


def test_pipeline_cascade_end_to_end_and_cache(fake_hosts, tmp_path, monkeypatch):
    w = fake_hosts
    w.add("gemeente-utrecht.nl", "/", "should not be fetched: stadio 0 is enough?")
    w.add("info.example.nl", "/", "<html lang='nl'><head><title>Ziekenhuis Noord - Patiënten</title></head><body><h1>Ziekenhuis Noord</h1><p>ziekenhuis patienten afspraak</p></body></html>")
    w.add("shop.example.nl", "/", "<html><head><title>Shop</title></head><body><a>Add to cart</a><a>Buy now</a> GmbH pricing</body></html>")
    rows = [{"hostname": "https://gemeente-utrecht.nl", "found_on": "x"}, {"hostname": "https://info.example.nl", "found_on": "x"},
            {"hostname": "https://shop.example.nl", "found_on": "x"}, {"hostname": "https://www.facebook.com", "found_on": "x"},
            {"hostname": "", "found_on": ""}]
    sites = sites_from_rows(rows)
    assert len(sites) == 4                                    # la riga senza host non genera un sito
    cfg = load_config(None, {"stage0": True, "stage3": True})
    cfg["stages"]["stage3"]["per_host_delay"] = 0
    cache = Cache(tmp_path / "cache.jsonl", 90)
    pipe = Pipeline(cfg, LEX, cache)
    verdicts = pipe.run(sites)
    assert verdicts["www.facebook.com"].natura == "infrastruttura"
    assert verdicts["info.example.nl"].tipo == "sanita"
    assert verdicts["shop.example.nl"].natura == "privato"
    assert "www.facebook.com" not in w.hit_paths("www.facebook.com")            # l'infrastruttura non viene aperta
    assert w.hit_paths("gemeente-utrecht.nl") == [] or verdicts["gemeente-utrecht.nl"].tipo == "comune"
    # output
    out = tmp_path / "siti_classificati.csv"
    pipe.write_output(rows, sites, verdicts, out)
    res = list(csv.DictReader(out.open(encoding="utf-8-sig")))
    assert [r["natura"] for r in res][3:] == ["infrastruttura", "non_classificabile"]
    assert res[4]["da_rivedere"] == "si" and res[3]["da_rivedere"] == "no"
    assert res[1]["tipo"] == "sanita" and res[1]["homepage_status"] == "ok" and "3" in res[1]["stadi"]
    assert res[1]["ente_rilevato"] == "Ziekenhuis Noord - Patiënten"   # og:site_name assente -> titolo homepage
    # seconda esecuzione: zero nuove richieste (cache di evidenza)
    n_before = len(w.hits)
    Pipeline(cfg, LEX, Cache(tmp_path / "cache.jsonl", 90)).run(sites)
    assert len(w.hits) == n_before


def test_stages_off_by_default():
    cfg = load_config(None)
    assert not any(cfg["stages"][s]["enabled"] for s in ("stage0", "stage2", "stage3", "stage4"))
    p = Pipeline(cfg, LEX, Cache(Path("/tmp/none.jsonl"), 1))
    assert p.run([make_site("gemeente-utrecht.nl")])["gemeente-utrecht.nl"].natura == "non_classificabile"


# ---------------------------------------------------------------- stadio 2: Wikidata
class _Sparql(BaseHTTPRequestHandler):
    queries: list[str] = []
    def log_message(self, *a): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        _Sparql.queries.append(body)
        payload = {"results": {"bindings": [
            {"url": {"value": "https://www.uni-heidelberg.de/"}, "item": {"value": "http://www.wikidata.org/entity/Q1"}, "classLabel": {"value": "public university"}},
            {"url": {"value": "https://www.uni-heidelberg.de/"}, "item": {"value": "http://www.wikidata.org/entity/Q1"}, "classLabel": {"value": "government agency"}},
            {"url": {"value": "https://gemeente-x.nl/"}, "item": {"value": "http://www.wikidata.org/entity/Q2"},
             "itemLabel": {"value": "Gemeente X"}, "classLabel": {"value": "municipality of the Netherlands"}},
        ]}}
        data = json.dumps(payload).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


def test_stage2_wikidata_mapping_and_query():
    _Sparql.queries = []
    srv = HTTPServer(("127.0.0.1", 0), _Sparql)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        res = run_stage2(["uni-heidelberg.de", "gemeente-x.nl", "nothing.example.org"], LEX,
                         endpoint=f"http://127.0.0.1:{srv.server_address[1]}/sparql", sleep_seconds=0)
    finally:
        srv.shutdown()
    assert nat(res["uni-heidelberg.de"][0]) >= {("tipo", "universita"), ("natura", "pubblico")}
    assert ("tipo", "agenzia_authority") in nat(res["uni-heidelberg.de"][0])     # 'government agency' batte 'government'
    assert ("tipo", "governo_centrale") not in nat(res["uni-heidelberg.de"][0])
    assert nat(res["gemeente-x.nl"][0]) >= {("tipo", "comune"), ("natura", "pubblico")}
    assert res["nothing.example.org"][0] == []                 # non trovato = nessun voto
    assert "<https://www.uni-heidelberg.de/>" in _Sparql.queries[0].replace("%3C", "<").replace("%3E", ">").replace("%3A", ":").replace("%2F", "/")
    # nome "leggibile" dell'ente (etichetta dell'item, non della classe): risposta a "che ente è?"
    assert res["gemeente-x.nl"][1]["wikidata_label"] == "Gemeente X"
    assert res["uni-heidelberg.de"][1]["wikidata_label"] == ""   # nessuna itemLabel in fixture per Q1: niente inventato


def test_wikidata_longest_keyword_wins():
    v = map_class_labels(["government agency"], LEX)
    assert ("tipo", "agenzia_authority") in nat(v) and ("tipo", "governo_centrale") not in nat(v)
    v = map_class_labels(["private university"], LEX)
    assert ("natura", "privato") in nat(v) and ("natura", "pubblico") not in nat(v)


# -------------------------------------------------------------------- stadio 4: LLM
class _Ollama(BaseHTTPRequestHandler):
    chats: list[dict] = []
    answer = {"natura": "pubblico", "tipo": "scuola", "motivo": "title says primary school"}
    def log_message(self, *a): pass
    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        self._send({"models": [{"name": "gemma4:e2b_q8"}]})
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Ollama.chats.append(req)
        self._send({"message": {"content": json.dumps(_Ollama.answer)}})


@pytest.fixture
def ollama():
    _Ollama.chats = []
    srv = HTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_ollama_client_check_and_constrained_request(ollama):
    good = OllamaClient(ollama, "gemma4:e2b_q8")
    assert good.check()[0]
    bad = OllamaClient(ollama, "modello-inesistente")
    ok, msg = bad.check()
    assert not ok and "gemma4:e2b_q8" in msg                      # elenca i modelli disponibili
    ans = good.classify("scuola-x.it", {"title": "Istituto", "status": "ok"})
    assert ans["tipo"] == "scuola"
    req = _Ollama.chats[-1]
    assert req["options"]["temperature"] == 0 and req["stream"] is False
    assert "enum" in json.dumps(req["format"]) and req["format"]["required"] == ["natura", "tipo", "motivo"]


def test_stage4_only_on_residual_and_cached(fake_hosts, ollama, tmp_path):
    w = fake_hosts
    w.add("vague.example.nl", "/", "<html><head><title>Istituto Rossi</title></head><body>" + "testo generico " * 30 + "</body></html>")
    w.add("gemeente-y.nl", "/", "<html><head><title>Gemeente Y</title></head><body><h1>Gemeente Y</h1>burgemeester gemeentehuis</body></html>")
    sites = sites_from_rows([{"hostname": "https://vague.example.nl", "found_on": "x"}, {"hostname": "https://gemeente-y.nl", "found_on": "x"}])
    cfg = load_config(None, {"stage0": True, "stage3": True, "stage4": True})
    cfg["stages"]["stage3"]["per_host_delay"] = 0
    cfg["stages"]["stage4"]["ollama_url"] = ollama
    cache = Cache(tmp_path / "c.jsonl", 90)
    v = Pipeline(cfg, LEX, cache).run(sites)
    assert len(_Ollama.chats) == 1                                        # solo il sito vago va all'LLM
    assert v["vague.example.nl"].tipo == "scuola" and v["vague.example.nl"].conf_tipo <= 0.66
    assert v["gemeente-y.nl"].tipo == "comune"
    Pipeline(cfg, LEX, Cache(tmp_path / "c.jsonl", 90)).run(sites)
    assert len(_Ollama.chats) == 1                                        # cache: nessuna nuova chiamata


def test_stage4_skipped_gracefully_when_ollama_down(fake_hosts, tmp_path):
    fake_hosts.add("vague2.example.nl", "/", "<html><head><title>Istituto</title></head><body>" + "x " * 200 + "</body></html>")
    cfg = load_config(None, {"stage3": True, "stage4": True})
    cfg["stages"]["stage3"]["per_host_delay"] = 0
    cfg["stages"]["stage4"]["ollama_url"] = "http://127.0.0.1:1"
    v = Pipeline(cfg, LEX, Cache(tmp_path / "c.jsonl", 90)).run(sites_from_rows([{"hostname": "https://vague2.example.nl", "found_on": "x"}]))
    assert v["vague2.example.nl"].natura in ("non_classificabile", "incerto", "pubblico", "privato")   # nessuna eccezione


# ---------------------------------------------------------------------- input
def test_load_rows_falls_back_to_crawl_json(tmp_path):
    (tmp_path / "data" / "output" / "HR").mkdir(parents=True)
    (tmp_path / "data" / "input" / "HR").mkdir(parents=True)
    with (tmp_path / "data" / "output" / "HR" / "siti.csv").open("w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=["hostname", "nome_ente"]); wr.writeheader(); wr.writerow({"hostname": "", "nome_ente": ""})
    (tmp_path / "data" / "input" / "HR" / "crawl_service_bund.json").write_text(json.dumps([
        {"external_url": "https://x.hr", "external_domain": "x.hr", "found_on": "u", "scope": "esterno", "observed_hosts": ["www.x.hr"], "tls_error": True}]), encoding="utf-8")
    (tmp_path / "data" / "input" / "HR" / "crawl_checkpoint.json").write_text("{}", encoding="utf-8")
    rows, src = load_rows("HR", tmp_path)
    sites = sites_from_rows(rows)
    assert "JSON del crawl" in src and sites[0].key == "www.x.hr" and "x.hr" in sites[0].hosts       # host osservato per primo
    assert rows[0]["tls_error"] == "si"
