# Glossary: Italian → English

The code, file names, CSV columns and documentation of both projects are written in **Italian**. This glossary helps English-speaking reviewers read the outputs and source code without translating each term. Every source file and Italian document also starts with a short English summary.

## Common words in identifiers

| Italian | English | Italian | English |
|---|---|---|---|
| ente / enti | public entity / entities | sito / siti | website(s) |
| comune | municipality | regione / provincia | region / province |
| paese | country | tipo (di ente) | (entity) type |
| natura | nature (public/private/…) | terza parte / terze parti | third party / parties |
| organizzazione madre | parent organisation | rilevato / rilevati | detected |
| presente | present | assente | absent |
| scadenza | expiry | giorni | days |
| deboli | weak | errore / errori | error(s) |
| punteggio | score | motivi | reasons |
| rischio | risk | ridondanza | redundancy |
| resilienza | resilience | raggiungibili | reachable |
| dichiarati | declared | incoerenti | inconsistent |
| diversi | distinct | quota | share |
| giurisdizione | jurisdiction | pagina | page |
| probabilmente bloccata | probably blocked (WAF/anti-bot) | troncata | truncated |
| peso (pagina) | (page) weight | tempo | time |
| da verificare | to be verified | verdetto | verdict |
| sospetto | suspicious | pulito | clean |
| non controllato | not checked | minaccia | threat |
| confermata da umano | confirmed by a human | revisione | review |
| misurazioni | measurements | concorrenza | concurrency |
| run / esecuzione | run | scartate | discarded |
| sintesi | summary | confronto paesi | cross-country comparison |
| SEE (Spazio Economico Europeo) | EEA (European Economic Area) | extra SEE | outside the EEA |

## Pipeline scripts (`eu-pa-darkstack-observatory/src/`)

| Script | Purpose |
|---|---|
| `01_fetch_ipa_comuni.py` | Load the Italian IndicePA registry of public entities and build the site list |
| `02_probe_infra.py` | DNS / mail / hosting-ASN / TLS / HTTP-CDN probe, one row per entity |
| `03_analyze.py` | Aggregation: HHI concentration, composite risk score, summary report |
| `04_export_enti_esteso.py` | Export the full registry joined with all measurements to one spreadsheet |
| `05_scrape_dark_stack.py` | Third-party ("dark stack") dependencies and cookies from homepage HTML |
| `06_fingerprint_cms.py` | CMS/software fingerprint and licence (open source or proprietary) |
| `07_resilience.py` | Real DNS/endpoint redundancy: queries each nameserver and each IP separately |
| `08_classifica_tipo_ente.py` | Classify the entity type from homepage text in 24 EU languages |
| `09_confronto_paesi.py` | Cross-country comparison |
| `run_pipeline.py` | Run all the steps above in sequence |

## Output files (`eu-pa-darkstack-observatory/data/results/<COUNTRY>/<RUN_DATE>/`)

| File | English meaning |
|---|---|
| `risultati.csv` | Main results: one row per entity with every infrastructure measurement |
| `report.json` | Full aggregated statistics (HHI, top-N operators, adoption rates, …) |
| `report_sintesi.md` | Plain-language summary of the run (an English translation sits next to it as `report_summary_EN.md`) |
| `enti_a_rischio.csv` | Entities at risk: composite risk score 0–5 and the reasons |
| `dark_stack.csv` | Per entity: third-party domains, parent organisations, non-EEA domains and cookies |
| `dark_stack_link_dettaglio.csv` | Detail: one row per third-party resource found (exact URL, category, vendor) |
| `terze_parti_da_verificare.csv/.md` | Third parties to verify: every third-party domain, with the first page where it was seen (for manual review) |
| `cms_fingerprint.csv` | Detected software, versions and licences |
| `resilience.csv` | Nameserver and endpoint redundancy that can actually be observed |
| `tipo_ente.csv` | Detected entity type, plus page weight and download time |
| `tipo_ente_possibili_non_pubblici.csv` | Rows that may not be public bodies, for manual review |
| `*_manifest.json` | Parameters of the run, for reproducibility |
| `latest_run.json` | Pointer to the most recent run of that country |

## Columns of `risultati.csv`

| Column | Meaning |
|---|---|
| `hostname`, `comune`, `regione`, `codice_ipa` | Host; municipality; region; registry identifier. For France, `regione` holds the département number and `codice_ipa` holds the Annuaire de l'administration UUID. |
| `ns`, `n_ns`, `ns_diversi_secondlevel` | Nameservers, how many there are, and how many distinct operator domains they belong to |
| `mx`, `n_mx`, `mx_diversi_secondlevel` | The same for mail servers |
| `a`, `aaaa` | IPv4 / IPv6 addresses |
| `dnssec_ds_presente` | DS record present in the parent zone (DNSSEC) |
| `caa` | CAA record |
| `spf_presente`, `spf_record` | SPF present, and the raw SPF record |
| `dmarc_presente`, `dmarc_policy` | DMARC present, and its policy (none / quarantine / reject) |
| `asn_a`, `asn_org_a`, `asn_country_a` | ASN, organisation and country of the web host. The `_mx` and `_ns` columns give the same for mail and nameservers. |
| `tls_issuer`, `tls_version`, `tls_giorni_scadenza` | Certificate issuer, negotiated TLS version, days until the certificate expires |
| `tls_protocolli_deboli_attivi` | Deprecated protocols still accepted (TLS 1.0 / 1.1) |
| `tls_cipher`, `tls_cipher_bit`, `tls_hsts_presente`, `tls_grade` | Cipher, key bits, HSTS present, synthetic A–D grade |
| `http_status`, `http_server`, `http_via`, `cdn_rilevato`, `http_response_time_ms` | HTTP status, Server/Via headers, detected CDN, response time |
| `giurisdizione_holding` | Heuristic jurisdiction of the hosting operator's holding company |
| `as_hegemony_*` | Experimental AS-hegemony transit dependency (disabled by default) |
| `errori` | Measurement errors for this row |

## Other result columns

| Column | Meaning |
|---|---|
| `punteggio_rischio`, `motivi_rischio` | Composite risk score (0–5) and its reasons (see below) |
| `n_terze_parti`, `domini_terzi`, `categorie_rilevate` | Number of third parties, their domains, and the detected categories |
| `organizzazioni_madri_terzi` | Parent organisations of the third parties (e.g. Google, Meta) |
| `n_extra_see`, `domini_extra_see` | Third parties hosted outside the EEA |
| `n_paese_sconosciuto` | Third parties whose country is unknown |
| `n_cookie_totali / tecnici / profilazione / sconosciuti`, `cookie_profilazione_nomi` | Cookies: total / technical / profiling / unknown, and the names of the profiling cookies |
| `pagina_scansionata`, `terza_parte_host`, `url_risorsa_trovata`, `vendor` | Scanned page, third-party host, exact resource URL found, vendor |
| `n_hostname_distinti`, `primo_hostname_scansionato`, `primo_url_risorsa_trovata`, `tutte_le_pagine` | Number of distinct sites, first site / resource where it was found, all pages |
| `software_rilevato`, `categorie_software`, `versioni_rilevate`, `licenze_rilevate`, `tutti_open_source` | Detected software, categories, versions, licences, whether all of it is open source |
| `n_ns_dichiarati`, `n_ns_raggiungibili`, `rapporto_ns_raggiungibili` | Nameservers declared / reachable / ratio |
| `ns_mx_incoerenti` | Nameservers giving inconsistent answers |
| `n_endpoint_*_ok`, `rapporto_endpoint_http_efficace` | Endpoints passing TCP / TLS / HTTP, and the effective HTTP ratio |
| `latenza_http_mediana_ms`, `latenza_http_p95_ms` | Median and 95th-percentile HTTP latency |
| `punteggio_resilienza_combinato` | Combined resilience score |
| `tipo_ente_rilevato`, `punteggio_top`, `categorie_rilevate_punteggi` | Detected entity type, its top score, and the scores per category |
| `probabile_non_pubblico`, `segnali_non_pubblico_rilevati` | Probably not a public body, and the signals behind that |
| `tempo_download_pagina_ms`, `peso_pagina_kb`, `pagina_troncata` | Full-page download time, page weight, page truncated |

### Risk reasons (`motivi_rischio`)

| Italian | English |
|---|---|
| nessuna ridondanza NS | no nameserver redundancy (a single NS operator) |
| DMARC non attivo (assente o solo osservazione) | DMARC not enforcing (absent or `p=none`) |
| DNSSEC assente | DNSSEC absent |
| certificato TLS scaduto o in scadenza entro 30 giorni | TLS certificate expired or expiring within 30 days |
| voto TLS basso (C/D: …) | low TLS grade (C/D: weak cipher or deprecated protocols) |

### Values and categories

| Italian | English |
|---|---|
| `cdn_font`, `chat_supporto`, `sdk_generico`, `altro_terzo_parte` | CDN/fonts, support chat, generic SDK, other third party |
| `tecnico` / `profilazione` (cookies) | technical / profiling |
| `server_web` | web server |
| `comune`, `regione_provincia`, `governo_centrale`, `polizia_sicurezza`, `scuola`, `universita`, `sanita`, `giustizia`, `parlamento`, `agenzia_authority`, `emergenza`, `cultura`, `trasporti`, `ambiente`, `altro`, `sconosciuto` | municipality, region/province, central government, police/security, school, university, healthcare, justice, parliament, agency/authority, emergency services, culture, transport, environment, other, unknown |
| `pubblico`, `privato`, `terzo_settore`, `incerto`, `infrastruttura`, `non_classificabile` | public, private, non-profit sector, uncertain, infrastructure, not classifiable |

## Site discovery (`eu-pa-scraper/`)

| Item | Meaning |
|---|---|
| `data/output/{ISO}/siti.csv` | Output per country: one row per public-sector website |
| `tipo_link`, `nome_ente`, `dominio_radice`, `host_osservati` | Link type, entity name, registrable root domain (eTLD+1), other hosts observed for the same entity |
| `status: ready / todo` (country YAML) | Country config working / still needs manual completion |
| `GUIDA_PASSO_PASSO.md` | Step-by-step guide |
| `CHANGES.md` (Registro delle modifiche) | Changelog |
| `classify_sites.py` → `siti_classificati.csv` | Site classification: `host_classificato`, `natura`, `tipo`, `ente_rilevato`, `confidenza_natura/tipo` (confidence), `stadi` (stages used), `evidenza` (evidence), `da_rivedere` (needs review) |
| `audit_sample.py` | Draws a stratified sample of classified sites for manual audit |
| `tools/postprocess_checkpoints.py` | Post-processes crawler checkpoints into reports and the suspicious-link shortlist |
| `tools/threat_check.py` | Google Safe Browsing / VirusTotal reputation lookup |
| `report_generale.md/json` | General crawl report |
| `tutte_le_terze_parti.csv` | Every third-party occurrence, one row per page × domain, with its HTML reference type |
| `link_sospetti.json/.csv/.md` | Suspicious-link shortlist for human review |
| `link_sospetto`, `pagine_in_cui_e_presente`, `tipi_riferimento` | Suspicious domain, pages where it appears, HTML reference types (`script`, `iframe`, `img`, `link`, `link_risorsa` = `<link href>`) |
| `punteggio_euristico`, `motivi_euristica`, `n_pagine_distinte` | Heuristic score (reading order only, **not** a verdict), its reasons, number of distinct pages |
| `verdetto_controllo_automatico` | Automatic-check verdict: `sospetto` (suspicious) / `da_verificare` (to verify) / `pulito` (clean) / `non_controllato` (not checked) |
| `gsb_flag`, `vt_motori_maligni`, `vt_motori_totali` | Flagged by Google Safe Browsing; VirusTotal engines reporting malicious / total engines |
| `minaccia_confermata_da_umano`, `note` | Human-confirmed threat flag and notes. Both persist across runs. |
| `seeds.txt`, `site_crawl` | Seed URLs and the generic layered crawler |
