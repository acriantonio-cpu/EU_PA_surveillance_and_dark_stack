# EU-PA DarkStack Observatory

An open-source observatory of the infrastructure behind **European public-administration (PA) websites**. It measures who controls their DNS, mail, hosting (ASN and jurisdiction), TLS, third-party ("dark stack") dependencies, and CMS/software licence. It also measures how concentrated these dependencies are across a small number of operators.

This repository holds two independent projects. Each one sits in its own folder with its own README, requirements and licence:

| Folder | What it does |
|---|---|
| [`eu-pa-scraper/`](eu-pa-scraper/) | **Site discovery.** Builds the list of PA websites for the 26 EU countries other than Italy, which already has the official IndicePA registry. The engine is configured per country through YAML (`config/countries/{ISO}.yaml`). It supports bulk CSV/XLSX/XML, JSON API and HTML scraping, a layered crawler, and an official-website finder. A post-processor (`tools/postprocess_checkpoints.py`) screens every third-party domain found on PA pages against Google Safe Browsing and VirusTotal, and keeps a persistent human-review flag. |
| [`eu-pa-darkstack-observatory/`](eu-pa-darkstack-observatory/) | **Infrastructure measurement.** For each public entity it collects DNS (NS/MX/DNSSEC/CAA/SPF/DMARC), hosting/ASN via RDAP plus Team Cymru bulk, TLS, HTTP/CDN, dark-stack third parties, cookies, CMS/licence fingerprint, real per-nameserver DNS redundancy, and entity-type classification in 24 EU languages. It then computes HHI concentration, a composite 0–5 risk score per entity, and a cross-country comparison (`src/run_pipeline.py`). |

## Language note

Most of the code, identifiers, file names and detailed documentation are written in **Italian**. To make them readable for English-speaking reviewers:
- [`GLOSSARY.md`](GLOSSARY.md) translates the Italian file names, CSV columns, values and terms;
- every Python source file starts with an `# EN:` comment describing what it does;
- every configuration and rules YAML file starts with an `# EN:` comment;
- every Italian Markdown document opens with an **English summary** block;
- the two run summaries are translated into English (`report_summary_EN.md`).

## Preliminary results included

The real runs cited in the Restack application are in [`eu-pa-darkstack-observatory/data/results/`](eu-pa-darkstack-observatory/data/results/):

- **Italy:** 500 entities, run `IT/2026-09-11`. Summary in [`report_summary_EN.md`](eu-pa-darkstack-observatory/data/results/IT/2026-09-11/report_summary_EN.md), full figures in `report.json`, and per-entity data in `risultati.csv`, `dark_stack.csv`, `cms_fingerprint.csv` and `enti_a_rischio.csv`.
- **France:** 2,000 entities, run `FR/2026-09-16`. Summary in [`report_summary_EN.md`](eu-pa-darkstack-observatory/data/results/FR/2026-09-16/report_summary_EN.md), with the same outputs, plus `resilience.csv`, `tipo_ente.csv` and `terze_parti_da_verificare.*`.

See [`data/results/README.md`](eu-pa-darkstack-observatory/data/results/README.md) for notes for reviewers. Every run has a `*_manifest.json` that records its parameters. Large input lists that can be regenerated (the IndicePA export, data.gouv.fr dumps, `siti.csv`) are **not** versioned. The pipeline downloads or rebuilds them (see each project's README).

> Note: some PA pages embed public Google API keys belonging to those sites, for example for Maps widgets. The crawler captured them in third-party URLs. In the published result files they are replaced with `AIza_REDACTED`.

## Quick start

Each project is self-contained (Python ≥ 3.11):

```bash
# site discovery
cd eu-pa-scraper
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests -q          # 109 offline tests
python main.py --help

# infrastructure measurement
cd ../eu-pa-darkstack-observatory
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python src/run_pipeline.py --help
```

The detailed documentation is in Italian, with an English summary at the top of each file:
- `eu-pa-scraper/README.md` and `GUIDA_PASSO_PASSO.md`
- `eu-pa-darkstack-observatory/README.md`, `README_aggiornato.md` and `docs/`

`docs/MAPPATURA_RESTACK.md` maps each measurement to the goals of the Restack call. `docs/AI_USAGE.md` documents where generative AI was used, following the NLnet GenAI policy.

Optional threat-screening API keys go in a local `.env` file. Copy it from `eu-pa-scraper/.env.example`. It is git-ignored and must never be committed.

## Licence

Both projects are released under the **European Union Public Licence v1.2 (EUPL-1.2)**. See the `LICENSE` file in each folder.
