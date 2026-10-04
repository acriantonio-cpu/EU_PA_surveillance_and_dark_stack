from __future__ import annotations

import csv

import audit_sample as A


def _rows(*triples):
    """[(host, natura, tipo), ...] -> righe come da CSV di classify_sites.py."""
    return [{"host_classificato": h, "natura": n, "tipo": t, "confidenza_natura": "0.5", "confidenza_tipo": "0.5"}
            for h, n, t in triples]


def test_stratified_sample_caps_per_group_and_covers_rare_groups():
    rows = _rows(*[(f"comune-{i}.it", "pubblico", "comune") for i in range(50)],
                 ("rara.it", "terzo_settore", "altro"))
    sample = A.stratified_sample(rows, per_group=3, seed=0)
    comuni = [r for r in sample if r["tipo"] == "comune"]
    rare = [r for r in sample if r["host_classificato"] == "rara.it"]
    assert len(comuni) == 3                 # capping: mai più di per_group per gruppo, anche se ce n'erano 50
    assert len(rare) == 1                    # il gruppo raro (1 sola riga) c'è comunque, non annegato nella maggioranza


def test_stratified_sample_is_deterministic_given_seed():
    rows = _rows(*[(f"h{i}.it", "pubblico", "comune") for i in range(20)])
    s1 = A.stratified_sample(rows, per_group=5, seed=42)
    s2 = A.stratified_sample(rows, per_group=5, seed=42)
    assert [r["host_classificato"] for r in s1] == [r["host_classificato"] for r in s2]


def test_diff_sample_only_returns_changed_rows():
    old = _rows(("a.it", "pubblico", "comune"), ("b.it", "privato", "sconosciuto"), ("c.it", "pubblico", "sanita"))
    new = _rows(("a.it", "pubblico", "comune"),           # invariato: NON deve comparire
                ("b.it", "privato", "altro"),              # cambiato: deve comparire
                ("c.it", "non_classificabile", "sconosciuto"))  # cambiato: deve comparire
    sample, n_changed = A.diff_sample(new, old, per_group=5, seed=0)
    hosts = {r["host_classificato"] for r in sample}
    assert hosts == {"b.it", "c.it"}
    assert n_changed == 2
    b = next(r for r in sample if r["host_classificato"] == "b.it")
    assert b["_prima"] == "privato/sconosciuto" and b["_dopo"] == "privato/altro"


def test_diff_sample_ignores_hosts_missing_from_old_run():
    old = _rows(("a.it", "pubblico", "comune"))
    new = _rows(("a.it", "pubblico", "comune"), ("nuovo.it", "privato", "altro"))
    sample, n_changed = A.diff_sample(new, old, per_group=5, seed=0)
    assert sample == [] and n_changed == 0     # 'nuovo.it' non era nel run vecchio: non e' un "cambiamento", si ignora


def test_cli_writes_expected_columns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(A, "PROJECT_ROOT", tmp_path)
    in_path = tmp_path / "siti.csv"
    with in_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["host_classificato", "natura", "tipo", "confidenza_natura", "confidenza_tipo"])
        w.writeheader()
        w.writerows(_rows(("a.it", "pubblico", "comune"), ("b.it", "privato", "sconosciuto")))

    out_path = tmp_path / "out.csv"
    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = ["audit_sample.py", "--country", "ZZ", "--input", str(in_path), "--out", str(out_path)]
    try:
        rc = A.main()
    finally:
        _sys.argv = old_argv
    assert rc == 0 and out_path.exists()

    with out_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert set(rows[0]) >= {"host_classificato", "natura", "tipo", "natura_vera", "tipo_vero"}
    assert rows[0]["natura_vera"] == "" and rows[0]["tipo_vero"] == ""    # da riempire a mano, vuote di partenza


def test_cli_missing_input_gives_clean_error(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "PROJECT_ROOT", tmp_path)
    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = ["audit_sample.py", "--country", "ZZ"]
    try:
        rc = A.main()
    finally:
        _sys.argv = old_argv
    assert rc == 2
