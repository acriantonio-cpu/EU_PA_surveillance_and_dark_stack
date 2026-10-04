# EN: Small helpers shared by base_fetcher and fetchers (registrable domain via the bundled Public
# Suffix List, no runtime network).
"""Piccole funzioni condivise tra base_fetcher e i fetcher (evita import circolari)."""

from __future__ import annotations

from .urlutil import host_of, is_ip_host

try:
    import tldextract

    # Solo lo snapshot della Public Suffix List incluso nel pacchetto: nessuna
    # chiamata di rete a runtime.
    _TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())
except ImportError:  # pragma: no cover
    tldextract = None
    _TLD_EXTRACTOR = None


def root_domain_of(url_or_host: str) -> str:
    """Dominio radice (eTLD+1) di un URL/host. '' per IP letterali, host senza
    punto o input non interpretabile."""
    raw = (url_or_host or "").strip()
    host = host_of(raw if "://" in raw else "//" + raw)
    if not host or is_ip_host(host):
        return ""
    if _TLD_EXTRACTOR is None:  # fallback grezzo
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else ""
    ext = _TLD_EXTRACTOR(host)
    if ext.suffix and ext.domain:
        return f"{ext.domain}.{ext.suffix}".lower()
    return ""
