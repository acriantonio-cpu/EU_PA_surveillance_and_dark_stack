"""
Riconoscimento di situazioni in cui una risposta HTTP "riuscita" (200) NON è
il contenuto reale del sito, più la blocklist di domini infrastrutturali.

Usato sia dal crawler (generic.py) sia dal classificatore (src/classify/).

  detect_challenge()   pagina anti-bot (Cloudflare, Akamai, Incapsula, captcha)
  looks_js_rendered()  pagina che senza JavaScript è (quasi) vuota
  detect_parked()      dominio parcheggiato / in vendita
  detect_default_page()pagina di default del web server (nginx, Apache, IIS)
  is_infrastructure()  dominio che non è mai un ente (CDN, social, shortener,
                       store di app, widget...)
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------
# Blocklist infrastrutturale
# --------------------------------------------------------------------------

URL_SHORTENERS = frozenset({
    "bit.ly", "t.co", "goo.gl", "tinyurl.com", "ow.ly", "is.gd", "buff.ly", "lnkd.in",
    "cutt.ly", "rebrand.ly", "rb.gy", "shorturl.at", "t.ly", "s.id", "tiny.cc", "youtu.be",
    "fb.me", "wa.me", "t.me", "amzn.to", "linktr.ee", "lnk.bio", "smarturl.it",
})

# Domini (match per suffisso) che compaiono spesso come <a href> nei siti PA ma
# non sono mai l'ente: software, widget, store, social, CDN, standard.
INFRASTRUCTURE_DOMAINS = frozenset({
    # software / vendor
    "adobe.com", "acrobat.com", "microsoft.com", "office.com", "live.com", "microsoftonline.com",
    "apple.com", "mozilla.org", "mozilla.com", "oracle.com", "java.com", "zoom.us", "skype.com",
    "dropbox.com", "slack.com", "atlassian.net", "github.com", "gitlab.com", "sourceforge.net",
    # standard / CMS
    "w3.org", "wordpress.org", "wordpress.com", "wp.com", "joomla.org", "drupal.org", "typo3.org",
    "creativecommons.org", "schema.org", "whatwg.org", "opensource.org",
    # store / messaggistica
    "whatsapp.com", "telegram.org", "telegram.me", "signal.org", "play.google.com",
    "apps.apple.com", "itunes.apple.com", "microsoft.com",
    # CDN / statici / widget
    "cloudflare.com", "cloudfront.net", "akamaihd.net", "akamaized.net", "fastly.net",
    "googleapis.com", "gstatic.com", "googleusercontent.com", "googletagmanager.com",
    "google-analytics.com", "doubleclick.net", "recaptcha.net", "addthis.com", "sharethis.com",
    "addtoany.com", "disqus.com", "gravatar.com", "fontawesome.com", "jsdelivr.net", "unpkg.com",
    "bootstrapcdn.com", "jquery.com", "hotjar.com", "matomo.cloud", "statcounter.com",
    "vimeo.com", "soundcloud.com", "spotify.com", "issuu.com", "scribd.com", "slideshare.net",
    "flickr.com", "giphy.com", "imgur.com", "bing.com", "yahoo.com", "duckduckgo.com",
    "openstreetmap.org", "maps.app.goo.gl",
    # social (in aggiunta a quelli già nel config)
    "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.com", "reddit.com", "tumblr.com", "xing.com",
    "snapchat.com", "threads.net", "mastodon.social", "bsky.app", "bsky.social", "twitch.tv",
    "medium.com", "wikipedia.org", "wikimedia.org", "wikidata.org", "archive.org",
}) | URL_SHORTENERS

# Se il label del dominio radice è uno di questi, lo è con QUALUNQUE TLD
# (google.de, google.fr, facebook.net...).
INFRASTRUCTURE_LABELS = frozenset({
    "google", "facebook", "youtube", "twitter", "instagram", "linkedin", "microsoft", "apple",
    "whatsapp", "tiktok", "cloudflare", "amazonaws",
})


def _root_label(host: str) -> str:
    parts = host.lower().split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def is_infrastructure(host: str, extra: set[str] | frozenset[str] | None = None) -> bool:
    """True se 'host' (o un suo suffisso) è un dominio infrastrutturale noto."""
    h = (host or "").lower().rstrip(".")
    if not h:
        return False
    doms = INFRASTRUCTURE_DOMAINS if not extra else (INFRASTRUCTURE_DOMAINS | set(extra))
    if any(h == d or h.endswith("." + d) for d in doms):
        return True
    return _root_label(h) in INFRASTRUCTURE_LABELS


# --------------------------------------------------------------------------
# Challenge / anti-bot
# --------------------------------------------------------------------------

_CHALLENGE_TITLE_RE = re.compile(
    r"<title[^>]*>\s*(?:just a moment|attention required|access denied|please wait|"
    r"checking your browser|verifying you are human|are you a robot|security check|"
    r"one more step|ddos protection|request rejected|pardon our interruption|"
    r"you have been blocked|error 1020|403 forbidden)",
    re.IGNORECASE,
)
_CHALLENGE_BODY_MARKERS = (
    "cf-chl", "_cf_chl_opt", "cf-browser-verification", "challenge-platform",
    "/cdn-cgi/challenge", "incapsula incident", "request unsuccessful. incapsula",
    "px-captcha", "perimeterx", "datadome", "ddos protection by", "akamai reference",
    "errors.edgesuite.net", "sucuri website firewall", "wordfence",
)


def detect_challenge(html: str, headers: dict[str, Any] | None = None, status_code: int = 200) -> str | None:
    """Se la risposta sembra una pagina anti-bot/captcha ritorna il motivo
    (stringa breve), altrimenti None. Euristico: privilegia la precisione
    (pagine lunghe con la parola 'captcha' in un form di contatto NON contano)."""
    h = {k.lower(): str(v) for k, v in (headers or {}).items()}
    if h.get("cf-mitigated", "").lower() == "challenge":
        return "cloudflare (header cf-mitigated)"
    sample = (html or "")[:60_000]
    low = sample.lower()
    m = _CHALLENGE_TITLE_RE.search(sample)
    if m:
        return f"titolo da challenge: {m.group(0)[7:60].strip()!r}"
    for marker in _CHALLENGE_BODY_MARKERS:
        if marker in low:
            return f"marker anti-bot: {marker}"
    # captcha in una pagina CORTA (una pagina di contenuto vero è molto più lunga)
    if len(low) < 6000 and ("captcha" in low or "g-recaptcha" in low or "h-captcha" in low):
        return "captcha in pagina breve"
    return None


# --------------------------------------------------------------------------
# JS-rendered
# --------------------------------------------------------------------------

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|template)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SPA_ROOT_RE = re.compile(
    r'<div[^>]+id=["\'](?:app|root|__next|__nuxt|svelte|q-app|ng-app)["\']|__NEXT_DATA__|ng-version=|data-reactroot|window\.__NUXT__',
    re.IGNORECASE,
)
_NOSCRIPT_JS_RE = re.compile(r"<noscript[^>]*>.{0,400}?(?:javascript|js)\b.{0,200}?</noscript>", re.IGNORECASE | re.DOTALL)


def visible_text_length(html: str) -> int:
    """Lunghezza approssimativa del testo visibile (senza script/style/tag)."""
    stripped = _SCRIPT_STYLE_RE.sub(" ", html or "")
    stripped = _TAG_RE.sub(" ", stripped)
    return len(re.sub(r"\s+", " ", stripped).strip())


def looks_js_rendered(html: str, n_links: int | None = None, min_text: int = 250) -> bool:
    """True se il contenuto statico è troppo povero e ci sono i marker tipici
    di una SPA / dell'avviso 'abilita JavaScript'. 'n_links' (se noto) rafforza
    il segnale: una pagina con molti link non è una SPA vuota."""
    if n_links is not None and n_links >= 10:
        return False
    if visible_text_length(html) >= min_text:
        return False
    return bool(_SPA_ROOT_RE.search(html or "") or _NOSCRIPT_JS_RE.search(html or ""))


# --------------------------------------------------------------------------
# Parcheggiato / pagina di default
# --------------------------------------------------------------------------

_PARKED_MARKERS = (
    "this domain is for sale", "domain is for sale", "buy this domain", "this domain may be for sale",
    "domain parking", "parked free", "parked domain", "sedoparking", "hugedomains", "dan.com/buy",
    "domein te koop", "dieses domain steht zum verkauf", "diese domain steht zum verkauf",
    "dominio en venta", "ce nom de domaine est à vendre", "ce domaine est à vendre",
    "dominio in vendita", "domínio à venda", "domena na sprzedaż", "doména na prodej",
    "domain registered at", "future home of something quite cool", "godaddy.com/domains",
)
_DEFAULT_PAGE_MARKERS = (
    "welcome to nginx", "apache2 ubuntu default page", "apache2 debian default page",
    "it works!", "iis windows server", "internet information services", "test page for the apache",
    "default web site page", "index of /", "plesk default page", "welcome to your new website",
    "site under construction", "coming soon", "website is under construction",
    "sito in costruzione", "site en construction", "seite im aufbau", "en construcción",
)


def detect_parked(html: str) -> bool:
    low = (html or "")[:40_000].lower()
    return any(m in low for m in _PARKED_MARKERS)


def detect_default_page(html: str) -> bool:
    low = (html or "")[:20_000].lower()
    if visible_text_length(html) > 1500:  # pagina vera con footer "coming soon"
        return False
    return any(m in low for m in _DEFAULT_PAGE_MARKERS)
