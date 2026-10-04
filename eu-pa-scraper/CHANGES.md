# Registro delle modifiche — 24/09/2026 (due tornate + audit finale)

> **English summary: changelog of 24 September 2026.** The numbers in brackets refer to the 30 failure modes found by the initial audit.
> - **Round 2**: robots.txt and Crawl-delay respected by default, with an identifiable User-Agent; a crawler that runs in parallel per host; an incremental SQLite checkpoint in place of a JSON file rewritten after every page; documentation; a final audit with its findings and residual risks.
> - **Round 1**: fixes to the HTTP session (size and read limits), TLD matching, URL canonicalisation, 404 handling, detection of redirects, challenges and JS-rendered pages, atomic CSV writes, and the new `classify_sites.py`.
>
> Each item also says what was deliberately **not** changed.

Riferimenti: i numeri tra parentesi sono quelli della tabella dei 30 failure modes dell'audit iniziale.
Guida operativa: `GUIDA_PASSO_PASSO.md`.

## TORNATA 2 (richiesta: 13, 19, 20 + documentazione + audit)

### (13) robots.txt e User-Agent — corretto, con flag
- **`respect_robots` (default `true`)** nel config di ogni paese `site_crawl` e in `config_classify.yaml` (stadio 3):
  legge robots.txt **una volta per host**, applica divieti (con caratteri jolly `*`/`$`, libreria `protego`) e `Crawl-delay`
  (tetto `max_crawl_delay`, 30 s). Con `false` nessun robots.txt viene letto. Regole: assente/4xx → consentito; 5xx → host
  escluso; irraggiungibile per rete → consentito. Implementazione in `src/polite.py`.
- **`identify_crawler` (default `true`)**: User-Agent `eu-pa-scraper/1.0 (+contatto)`; contatto da `contact` o da `SCRAPER_CONTACT`
  (`.env`, ora caricato da `main.py` e `classify_sites.py`). Il contatto è sanificato (solo ASCII, niente a-capo).
- **Conseguenza da conoscere:** `service.bund.de` vieta i bot: con il default il crawl DE salta i seed e il log dice
  `TUTTI i seed sono vietati da robots.txt`. Per continuare lo stesso: fonte alternativa (Anschriftenverzeichnis), accordo col
  gestore, oppure `respect_robots: false` in `DE.yaml` (scelta tua). I testi di `DE.yaml`, `BE.yaml` e del README che dicevano
  "il tool non applica robots.txt" sono stati aggiornati.

### (20) Crawler parallelo — corretto (velocizza)
- Architettura: un **thread coordinatore** (unico a toccare lo stato: nessun lock) + `workers` thread che fanno solo rete e
  parsing. Coda per host con giro round-robin (`HostScheduler`); `sleep_seconds` diventa la pausa minima **per host**;
  `max_concurrent_per_host` (1). Cooldown per dominio, gestione 4xx, redirect, challenge, JS e sitemap invariati.
- Misure (server locale con latenza 0,15 s, 12 host): **1 worker 19,4 s → 4 worker 5,2 s → 8 worker 4,9 s** (il limite è la
  cortesia per host). Senza latenza il motore regge ~250 pagine/s (9.151 pagine in 36,6 s).
- `http_retries` (default 2): meno retry immediati sullo stesso host; prima un 503 persistente bloccava un worker ~6 s a pagina.
- Limite noto: `follow_pagination` (default `false`, non usato da nessun paese) resta **sequenziale** e blocca gli altri worker mentre lavora.

### (19) Checkpoint incrementale — corretto
- Nuovo `src/crawl_store.py`: stato del crawl in **SQLite (WAL)**, `crawl_state.sqlite`; ogni pagina scrive solo le proprie differenze
  (visitati, coda del livello successivo, registro domini, occorrenze di terze parti, ...). Misurato su 9.151 pagine: 1,4 MB di stato
  (il vecchio metodo avrebbe riscritto un JSON da ~0,9 MB per pagina, ~8 GB cumulativi; a 50.000 pagine ~90 GB).
- `crawl_checkpoint.json` (formato storico, letto da `tools/postprocess_checkpoints.py`, **verificato**) viene esportato solo a fine
  livello, a fine crawl e all'interruzione.
- **Compatibilità**: un vecchio `crawl_checkpoint.json` viene ancora accettato da `--resume` e migrato nello store. Uno store SQLite
  corrotto è messo da parte come `*.corrupt-<ora>` e non ferma il crawl.
- Il `--resume` riparte da "coda del livello − pagine già visitate" (al più si rifanno le pagine in volo all'interruzione).

### Documentazione
- **`GUIDA_PASSO_PASSO.md`** (nuova): installazione, `.env`, primo run, paesi a file manuali, crawler (semi, opzioni con default, stime di
  tempo, resume, file prodotti), robots.txt, ricerca del sito ufficiale, classificazione (stadi, Ollama, calibrazione), post-processing,
  ricetta completa, troubleshooting. `README.md` aggiornato (link, §6, note su robots.txt).
- `requirements.txt` (+ `protego`), `requirements-dev.txt` (pytest), `.env.example` (+ `SCRAPER_CONTACT`), `.gitignore` (+ `*.sqlite*`).

### AUDIT FINALE — cosa ho controllato e cosa ha trovato
| Verifica | Esito |
|---|---|
| 85 test automatici (server HTTP/HTTPS locali, Wikidata e Ollama simulati) | passano; ripetuti più volte per escludere test instabili |
| Stress: 16 worker, 40 host, errori 503/403 casuali, robots vari, 3 run | nessuna pagina scaricata due volte, **nessun divieto robots violato**, crawl completato ogni volta |
| Carico: 9.151 pagine senza latenza | 250 pagine/s, stato 1,4 MB |
| Compatibilità con `tools/postprocess_checkpoints.py` | ok (report, terze parti, link sospetti generati dal JSON esportato) |
| Analisi statica (pyflakes) | nessun nome indefinito; rimossi gli import inutilizzati |
| Installazione pulita da `requirements.txt` in un virtualenv nuovo + test | ok |
Problemi trovati **durante** l'audit e già corretti: (1) 503 persistenti bloccavano i worker (→ `http_retries`); (2) i testi obsoleti su robots.txt in README/YAML/docstring
contraddicevano il comportamento; (3) mancava la gestione di uno store SQLite corrotto; (4) il contatto nell'User-Agent poteva contenere caratteri non validi per un header;
(5) mancava un avviso esplicito quando TUTTI i seed sono vietati da robots.txt.

### Rischi residui (non risolti)
- `respect_robots: true` cambia i risultati di DE (vedi sopra) e potrebbe ridurre la copertura su altri siti; è la scelta richiesta, ma va valutata per paese.
- `identify_crawler: true` può far ricevere più 403 da siti che rifiutano gli UA non da browser: nel caso, `identify_crawler: false` per quel paese.
- SQLite in WAL su cartelle di rete o sincronizzate (OneDrive) può dare problemi di lock: tieni `data/` in un disco locale.
- Con `crawl_depth: 8` e 10–15k pagine per livello (invariati come richiesto) il tempo resta lungo: la parallelizzazione aiuta solo se gli host sono molti.
- Ctrl+C: i worker terminano la richiesta in corso (fino a ~10–20 s) prima che il processo esca.
- Lessico e soglie del classificatore non sono calibrati su un gold set; DuckDuckGo, Wikidata (SPARQL) e Ollama non sono stati provati in rete reale (sandbox senza rete).

---

## TORNATA 1

### Volutamente NON toccato
- (3) DE: i `.de` restano "interni" ed esplorati; con `record_internal_links: false` non vengono scritti in output.
- (4) HR/HU/PL/PT/SE: `site_crawl` con il vecchio `field_mapping` (`hostname` del CSV vuoto), chiavi YAML duplicate,
  `input_filename: crawl_service_bund.json`. `load_config` ora AVVISA delle chiavi duplicate ma non blocca nulla. Il classificatore,
  se `siti.csv` non ha hostname, ripiega sul JSON del crawl (`data/input/{ISO}/crawl_*.json`).
- (5) `crawl_depth: 8`, `max_pages_per_level: 10000/15000`.

### Corretto
| # | Cosa |
|---|---|
| 1 | `HttpSession.get` accetta `max_bytes`, `skip_non_html`, `read_timeout`, `allow_insecure_fallback`, `retries`. Il crawler cattura solo `requests.RequestException`: un bug (es. `TypeError`) non è più scambiato per errore di rete; se si ripete `max_internal_errors_in_a_row` (10) volte di fila il crawl si ferma. **Se sul tuo PC hai una `base_fetcher.py` più recente, confrontala prima di sovrascrivere.** |
| 2 | `_tld_matches`: solo sul suffisso (`de.wikipedia.org`, `be.linkedin.com`, `gov.evil.com` non sono più "interni"); `gov/gob/gouv/gv` valgono anche subito prima di un ccTLD di 2 lettere. Le code salvate da versioni precedenti vengono ri-filtrate al momento di scaricare. |
| 6 | selezione del livello round-robin per host + log delle URL scartate (`max_pages_per_level` resta il tetto TOTALE del livello). |
| 7 | URL canonicalizzati (fragment, utm/fbclid/gclid, jsessionid, host minuscolo/punycode, query ordinata, slash finale). Guardie anti-trappola: URL > 2000 caratteri, percorsi > 14 livelli, segmenti ripetuti, `max_pages_per_pattern` (500, 0 = disattivo). |
| 8 | host via `urlparse().hostname`, IDNA→punycode, IP letterali mai registrati come domini. |
| 9 | il registro tiene `observed_host`, `observed_url`, `observed_hosts` (FQDN realmente linkati, max 50). `hostname` resta `https://<dominio_radice>`; i FQDN sono in `host_osservati`. |
| 10 | nuove colonne `dominio_radice`, `host_osservati`, `found_on`, `source_seed`, `tls_error` (in coda a `siti.csv`). BE e DE non scrivono più `found_on` in `sector` né `external_domain` in `codice_ipa`. |
| 11 | 4xx (tranne 403/408/425/429) non ritentati e non mettono in pausa il dominio, salvo `domain_cooldown_after_dead_links` (5) di fila; niente sleep dopo l'ultimo tentativo; `Retry-After` rispettato. |
| 12 | fallback TLS `verify=False` SOLO per discovery/homepage, con flag `tls_error`. I download dei dati ufficiali restano con verifica. |
| 14 | pagine challenge/captcha (Cloudflare, Akamai, Incapsula, captcha in pagina breve) riconosciute: trattate come blocco. |
| 15 | pagine JS-rendered riconosciute (`needs_js`); si prova `sitemap.xml` (`js_sitemap_fallback`). Playwright NON incluso. |
| 16 | redirect: link relativi risolti sull'URL finale, dominio finale registrato (`scope: redirect`); blocklist infrastrutturale (`use_default_infra_blocklist`). |
| 17 | TLD regionali (`.brussels`, `.vlaanderen`, `.cat`, `.eus`, `.gal`, `.bzh`, `.berlin`, `.bayern`…) uniti ad `allowed_tlds` (`regional_tlds: false` per disattivare). `.eu` volutamente escluso. |
| 18 | encoding: `<meta charset>`/rilevamento senza charset in header; CSV: encoding configurato, poi `encoding_fallbacks`, poi l'encoding tipico del paese (cp1250 PL/CZ/HU/HR/SK/SI/RO, cp1251 BG…), poi rilevamento; `U+FFFD` solo come ultima spiaggia e con errore nel log. |
| 21 | `ApiJson`: `page_size` conta solo con `page_size_param`; pagine identiche = stop con avviso; avviso a `max_pages`; `expected_min_records` opzionale. |
| 22 | `nan`, `n/d`, email → vuoto; valori multipli separati; dedupe case-insensitive ignorando schema e `www.`; ID Excel `12345.0` → `12345`; `dominio_radice` per tutti i paesi. |
| 23 | XLSX cartella in streaming; FR scarica su disco in streaming; XML con `defusedxml`; NL `_first_text` ignora i sotto-alberi di contatti (**non verificato su un export reale**). |
| 24 | DDG: decodifica `uddg`, riconosce captcha/202, punteggio nome↔dominio, backend `wikidata`; cache + salvataggio incrementale; stop dopo 5 blocchi di fila. |
| 25 | `write_csv`/`enrich_websites` atomici; se il CSV è aperto in Excel salva in `siti.<timestamp>.csv`. |
| 26 | `main.py`: exit code 1 se un paese fallisce, `data/output/_run_summary.json`. |
| 27 | `python-dotenv` in `requirements.txt`, `.env.example`, `.env` NON incluso. **Ruota le due chiavi che erano nello zip iniziale.** |
| 28 | le chiavi API sono oscurate (`key=***`) nei messaggi d'errore di `threat_check`. |
| 29 | `gsb_limite_batch_giornaliero` 15 → 5000; il verdetto "pulito" ora dice "nessun segnale… non è una garanzia". |
| 30 | `.gitignore` esteso; cartelle spurie `{config…` e `__pycache__` rimosse. |
| extra | la paginazione numerica non viene più espansa per `<img>/<script>` né per domini esterni. |

### Nuovo: classificazione (`classify_sites.py`, `GUIDA_PASSO_PASSO.md` §10)
Stadi 0, 2, 3, 4 opzionali, **tutti OFF per default**. Non implementati: stadio 1 (join con i registri ufficiali) e 5 (revisione umana; c'è `da_rivedere`).
