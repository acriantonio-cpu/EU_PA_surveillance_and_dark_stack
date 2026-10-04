"""
Registry centrale: legge config/countries/{ISO}.yaml e istanzia il fetcher
giusto — generico (pilotato da YAML) oppure custom (modulo dedicato in
src/fetchers/{iso_lower}.py) se il config specifica 'custom_module'.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import yaml

from .base_fetcher import BaseFetcher
from .fetchers.generic import FETCHER_REGISTRY

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config" / "countries"
DATA_INPUT_DIR = PROJECT_ROOT / "data" / "input"
DATA_OUTPUT_DIR = PROJECT_ROOT / "data" / "output"


def list_countries() -> list[str]:
    return sorted(p.stem.upper() for p in CONFIG_DIR.glob("*.yaml"))


class _DupKeyLoader(yaml.SafeLoader):
    """SafeLoader che segnala (senza bloccare) le chiavi duplicate: in YAML l'ULTIMA
    vince in silenzio, il che ha già prodotto config con 'input_filename',
    'license' o 'exclude_domains' sovrascritti senza che nessuno se ne accorgesse."""


def _construct_mapping_warn_dups(loader: _DupKeyLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            logger.warning(
                "%s: chiave YAML duplicata '%s' (riga %d): vale l'ULTIMA occorrenza",
                getattr(loader, "_source_name", "config"), key, key_node.start_mark.line + 1,
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_DupKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_warn_dups)


def load_config(country_code: str) -> dict:
    path = CONFIG_DIR / f"{country_code.upper()}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Config non trovato per paese '{country_code}': {path}")
    with path.open("r", encoding="utf-8") as f:
        loader = _DupKeyLoader(f)
        loader._source_name = path.name  # type: ignore[attr-defined]
        try:
            data = loader.get_single_data()
        finally:
            loader.dispose()
    return data or {}


def build_fetcher(country_code: str) -> BaseFetcher:
    country_code = country_code.upper()
    config = load_config(country_code)

    if config.get("status") == "todo":
        raise NotImplementedError(
            f"[{country_code}] Config marcato come 'todo': completare "
            f"'source_type'/'field_mapping' (o selettori HTML) in "
            f"config/countries/{country_code}.yaml prima di eseguire."
        )

    input_dir = DATA_INPUT_DIR / country_code
    output_dir = DATA_OUTPUT_DIR / country_code

    custom_module = config.get("custom_module")
    if custom_module:
        mod = importlib.import_module(f"src.fetchers.{custom_module}")
        # Convenzione: il modulo custom espone una classe '<ISO>Fetcher'
        class_name = f"{country_code}Fetcher"
        fetcher_cls = getattr(mod, class_name)
        return fetcher_cls(config, input_dir, output_dir)

    source_type = config.get("source_type")
    if source_type not in FETCHER_REGISTRY:
        raise ValueError(
            f"[{country_code}] source_type '{source_type}' non riconosciuto. "
            f"Valori validi: {list(FETCHER_REGISTRY)} oppure 'custom_module'."
        )

    fetcher_cls = FETCHER_REGISTRY[source_type]

    class _Configured(fetcher_cls):  # type: ignore[misc, valid-type]
        country_code_ = country_code

    _Configured.country_code = country_code
    _Configured.source_name = config.get("source_name", source_type)

    return _Configured(config, input_dir, output_dir)
