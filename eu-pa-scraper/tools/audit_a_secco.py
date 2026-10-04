# EN: Dry-run audit (no real network) of the threat check: simulated responses to verify dedup,
# vendor exclusion, cache persistence and daily quota.
"""
Audit a secco (nessuna chiamata di rete vera) del controllo minacce.
Monkeypatcha le funzioni HTTP di basso livello con risposte simulate per
verificare: dedup per dominio, esclusione vendor noti, subset VT filtrato,
persistenza cache, quota giornaliera, non-esclusione dai report.
"""
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# NOTA: lancia questo script dalla cartella PRINCIPALE del progetto reale
# (quella che contiene data/input/{ISO}/...), non da tools/. Serve solo
# un checkpoint di test in data/input/XX/crawl_checkpoint.json con un
# 'resource_index' di esempio per verificare la logica senza toccare i
# dati veri né le API reali.

os.environ["GOOGLE_SAFE_BROWSING_API_KEY"] = "chiave-finta-test"
os.environ["VIRUSTOTAL_API_KEY"] = "chiave-finta-test"

import threat_check  # noqa: E402

CHIAMATE_GSB = []
CHIAMATE_VT = []


def fake_gsb(domains, api_key, timeout):
    CHIAMATE_GSB.append(list(domains))
    # Simula GSB che flagga solo 'flaggato-da-gsb.top'
    flagged = {}
    if "flaggato-da-gsb.top" in domains:
        flagged["flaggato-da-gsb.top"] = ["SOCIAL_ENGINEERING"]
    return flagged


def fake_vt(domain, api_key, timeout):
    CHIAMATE_VT.append(domain)
    if domain == "sospetto-malware.xyz":
        return {"trovato": True, "motori_maligni": 12, "motori_sospetti": 3, "motori_totali": 70}
    if domain == "flaggato-da-gsb.top":
        return {"trovato": True, "motori_maligni": 5, "motori_sospetti": 1, "motori_totali": 70}
    return {"trovato": True, "motori_maligni": 0, "motori_sospetti": 0, "motori_totali": 70}


threat_check._query_gsb_batch = fake_gsb
threat_check._query_vt_domain = fake_vt
threat_check.time.sleep = lambda s: None  # niente attese reali nel test

import postprocess_checkpoints as pp  # noqa: E402

sys.argv = ["postprocess_checkpoints.py", "--config", str(Path(__file__).resolve().parent / "config_postprocess.yaml")]
pp.main()

print("\n--- Verifica ---")
print("Chiamate GSB (batch):", len(CHIAMATE_GSB), "contenuto batch 1:", CHIAMATE_GSB[0] if CHIAMATE_GSB else None)
print("Chiamate VT (una per dominio, mai ripetute):", CHIAMATE_VT)
assert len(CHIAMATE_VT) == len(set(CHIAMATE_VT)), "ERRORE: un dominio è stato interrogato su VT più di una volta"
assert "cdn-diffuso-pulito.example" not in CHIAMATE_VT, (
    "ERRORE: un dominio diffuso e senza punteggio euristico non doveva finire nel subset VT"
)
assert "googletagmanager.com" not in CHIAMATE_GSB[0], "ERRORE: un dominio in domini_esclusi_sempre non doveva essere controllato"

out_dir = Path("data/output/_postprocess")
tutte = (out_dir / "tutte_le_terze_parti.csv").read_text(encoding="utf-8")
print("\n--- tutte_le_terze_parti.csv (deve contenere TUTTI i domini, nessuno escluso) ---")
print(tutte)
for d in ["googletagmanager.com", "cdn-diffuso-pulito.example", "raro-commerciale.example",
          "sospetto-malware.xyz", "flaggato-da-gsb.top"]:
    assert d in tutte, f"ERRORE: {d} manca da tutte_le_terze_parti.csv (non deve MAI essere filtrato)"

link_sospetti = json.loads((out_dir / "link_sospetti.json").read_text(encoding="utf-8"))
per_dominio = {v["link_sospetto"]: v for v in link_sospetti}
print("\n--- link_sospetti.json (verdetti) ---")
for v in link_sospetti:
    print(f"  {v['link_sospetto']}: verdetto={v['verdetto_controllo_automatico']} punteggio={v['punteggio_euristico']}")

assert per_dominio["sospetto-malware.xyz"]["verdetto_controllo_automatico"] == "sospetto"
assert per_dominio["flaggato-da-gsb.top"]["verdetto_controllo_automatico"] == "sospetto"
assert per_dominio["flaggato-da-gsb.top"]["gsb_flag"] is True

# Riesecuzione: verifica che la cache eviti di richiamare di nuovo GSB/VT
CHIAMATE_GSB.clear()
CHIAMATE_VT.clear()
pp.main()
assert len(CHIAMATE_GSB) == 0, "ERRORE: la seconda esecuzione ha richiamato di nuovo GSB nonostante la cache fresca"
assert len(CHIAMATE_VT) == 0, "ERRORE: la seconda esecuzione ha richiamato di nuovo VT nonostante la cache fresca"

print("\n✅ Tutti i controlli dell'audit a secco sono passati.")
