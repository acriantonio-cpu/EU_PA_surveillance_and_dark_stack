# EN: Draw a stratified random sample (per nature x type) of classify_sites.py output for manual
# human audit of the classification.
"""
Campiona a caso righe da un output di classify_sites.py (siti_classificati.csv) per la revisione
manuale: l'unico modo onesto per sapere se una classificazione è corretta è guardarne un campione
a mano, non controllare quanto è "d'accordo con se stessa" (questo progetto ha già dimostrato, col
segnale a embedding, che l'accordo con la classificazione esistente NON prova che sia giusta).

DUE MODALITÀ:

1) Campione STRATIFICATO (default): al massimo --per-group righe per ogni combinazione
   (natura, tipo) presente nell'output, così anche le categorie rare (parlamento, emergenza...)
   finiscono nel campione, non solo quelle più numerose.

       python audit_sample.py --country NL --out campione_NL.csv

2) Campione della DIFFERENZA tra due run (--compare-to): quando cambi il lessico e vuoi sapere se
   ha *migliorato* le cose, il campione più informativo non è uno a caso qualsiasi, ma le righe
   che sono CAMBIATE tra il run vecchio e quello nuovo — è lì che si vede l'effetto della modifica.

       python audit_sample.py --country NL --compare-to siti_classificati_vecchio.csv --out diff_NL.csv

In entrambi i casi il CSV prodotto ha due colonne vuote (natura_vera, tipo_vero) da riempire a
mano; poi lo si manda indietro per il confronto (precisione/richiamo per categoria, non solo un
numero complessivo — vedi la cronologia del progetto per un esempio di come si legge).
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def stratified_sample(rows: list[dict], per_group: int, seed: int) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r.get("natura", ""), r.get("tipo", ""))].append(r)
    rng = random.Random(seed)
    out = []
    for key in sorted(groups):
        g = groups[key]
        out.extend(rng.sample(g, min(per_group, len(g))))
    rng.shuffle(out)
    return out


def diff_sample(rows_new: list[dict], rows_old: list[dict], per_group: int, seed: int) -> list[dict]:
    """Righe dove (natura, tipo) è CAMBIATO tra vecchio e nuovo run, stratificate per il tipo di
    cambiamento (es. 'non_classificabile/sconosciuto -> privato/altro'): è il campione più
    informativo per capire se una modifica al lessico ha aiutato o peggiorato le cose."""
    old_by_host = {r["host_classificato"]: r for r in rows_old}
    changed: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows_new:
        old = old_by_host.get(r.get("host_classificato", ""))
        if old is None:
            continue
        old_key = (old.get("natura", ""), old.get("tipo", ""))
        new_key = (r.get("natura", ""), r.get("tipo", ""))
        if old_key != new_key:
            r = dict(r)
            r["_prima"] = f"{old_key[0]}/{old_key[1]}"
            r["_dopo"] = f"{new_key[0]}/{new_key[1]}"
            changed[(old_key, new_key)].append(r)
    rng = random.Random(seed)
    out = []
    for key in sorted(changed, key=str):
        g = changed[key]
        out.extend(rng.sample(g, min(per_group, len(g))))
    rng.shuffle(out)
    return out, sum(len(g) for g in changed.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--country", "-c", required=True, help="Codice ISO (legge data/output/{ISO}/siti_classificati.csv)")
    ap.add_argument("--input", help="Percorso alternativo al CSV (default: data/output/{country}/siti_classificati.csv)")
    ap.add_argument("--compare-to", help="CSV di un run PRECEDENTE dello stesso paese: campiona le righe CAMBIATE")
    ap.add_argument("--per-group", type=int, default=5, help="Righe al massimo per ogni gruppo (default 5)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", help="CSV di output (default: audit_{country}.csv o diff_{country}.csv)")
    args = ap.parse_args()

    country = args.country.upper()
    in_path = Path(args.input) if args.input else PROJECT_ROOT / "data" / "output" / country / "siti_classificati.csv"
    if not in_path.exists():
        print(f"[errore] non trovo {in_path} — hai già lanciato classify_sites.py per {country}?", file=sys.stderr)
        return 2
    rows = load_csv(in_path)
    print(f"[{country}] {len(rows)} righe lette da {in_path}")

    if args.compare_to:
        old_path = Path(args.compare_to)
        if not old_path.exists():
            print(f"[errore] non trovo {old_path}", file=sys.stderr)
            return 2
        rows_old = load_csv(old_path)
        sample, n_changed = diff_sample(rows, rows_old, args.per_group, args.seed)
        print(f"[{country}] {n_changed} righe sono cambiate tra i due run; campione: {len(sample)} righe")
        out_path = Path(args.out) if args.out else PROJECT_ROOT / f"diff_{country}.csv"
        extra_cols = ["_prima", "_dopo"]
    else:
        sample = stratified_sample(rows, args.per_group, args.seed)
        n_groups = len({(r.get("natura", ""), r.get("tipo", "")) for r in rows})
        print(f"[{country}] {n_groups} combinazioni (natura,tipo) distinte; campione: {len(sample)} righe "
              f"(fino a {args.per_group} per combinazione)")
        out_path = Path(args.out) if args.out else PROJECT_ROOT / f"audit_{country}.csv"
        extra_cols = []

    fieldnames = ["host_classificato", "natura", "tipo", "confidenza_natura", "confidenza_tipo",
                  *extra_cols, "natura_vera", "tipo_vero"]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in sample:
            row = dict(r)
            row["natura_vera"] = ""
            row["tipo_vero"] = ""
            w.writerow(row)

    print(f"[{country}] scritto {out_path} — riempi 'natura_vera'/'tipo_vero' a mano e rimandalo indietro")
    return 0


if __name__ == "__main__":
    sys.exit(main())
