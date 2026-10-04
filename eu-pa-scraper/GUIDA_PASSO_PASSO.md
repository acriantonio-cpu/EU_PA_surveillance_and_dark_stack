# Guida passo passo — come eseguire il progetto

> **English summary: step-by-step guide.** It covers the whole workflow, from installation to the first `siti.csv`:
> - a first run on a country with automatic download (§5);
> - countries whose files must be copied by hand, such as Spain (§6);
> - the layered crawler: seeds, launching, interrupting and resuming with `--resume`, output files, all options, time estimates (§7);
> - robots.txt and User-Agent policy (`respect_robots`, default **true**) (§8);
> - the official-website finder (§9);
> - site classification with its stages, Ollama, the lexicon and `audit_sample.py` validation (§10);
> - post-processing and the Google Safe Browsing/VirusTotal threat check (§11);
> - a complete end-to-end recipe for Belgium (§12), troubleshooting (§13), a security reminder (§14) and the roadmap (§15).
>
> Each step states **what to run**, **what should happen** and **what to do if it does not**.

Questa guida ti porta dall'installazione al primo `siti.csv`, poi al crawl, alla classificazione, alla sua
convalida e ai controlli finali. Ogni passo dice **cosa lanciare**, **cosa deve succedere** e **cosa fare se non
succede**. Per il dettaglio di ogni singolo paese vedi `README.md` §5; per la lista delle modifiche `CHANGES.md`.

Comandi in PowerShell (Windows 11 + VS Code, come nel README). Su macOS/Linux sono identici, tranne
l'attivazione del virtualenv (`source .venv/bin/activate`) e `py -3` → `python3`.

---

## 1. Cosa fa il progetto (in 30 secondi)

```
                ┌─────────────────────────────┐
  fonti ufficiali│ main.py --country XX        │  data/output/XX/siti.csv
  (CSV/XLSX/API/ │  scarica → normalizza →     │ ───────────────────────────►  elenco siti + colonne
   XML) o crawl  │  dedupe → CSV               │
                └─────────────────────────────┘
                              │ (opzionale)
                              ▼
                ┌─────────────────────────────┐
                │ classify_sites.py           │  data/output/XX/siti_classificati.csv
                │  natura + tipo di ente      │ ───────────────────────────►  pubblico/privato + comune/scuola/...
                └─────────────────────────────┘
                              │ (consigliato dopo ogni modifica al lessico)
                              ▼
                ┌─────────────────────────────┐
                │ audit_sample.py             │  campione da controllare a mano
                │  campione + confronto verità│ ───────────────────────────►  la classificazione è DAVVERO giusta?
                └─────────────────────────────┘
                ┌─────────────────────────────┐
                │ tools/postprocess_...py     │  data/output/_postprocess/
                │  report, terze parti, GSB/VT│ ───────────────────────────►  report e link sospetti
                └─────────────────────────────┘
```

Tre tipi di paese: **a download automatico** (NL, FR, AT, CZ, DK, FI, IE, LT, RO, SI, SK),
**a file manuali** (ES: i file DIR3 vanno copiati a mano) e **a crawl** (BE, DE, HR, HU, PL, PT, SE:
si parte da una lista di siti "semi" e si scoprono i domini linkati).

---

## 2. Requisiti

| Cosa | Note |
|---|---|
| Python **3.11+** | testato con 3.12 |
| Windows 11 / macOS / Linux | |
| Connessione internet | per scaricare le fonti e (facoltativo) Wikidata |
| Spazio disco | FR scarica un archivio da ~350 MB; un crawl grande produce qualche decina di MB di stato |
| **Ollama** (solo per lo stadio 4 della classificazione) | facoltativo, vedi §10 |

---

## 3. Installazione

1. Apri la cartella del progetto in VS Code (File → Apri cartella…) e apri il terminale (Ctrl+`).
2. Crea e attiva il virtualenv:
   ```powershell
   py -3 -m venv .venv
   .venv\Scripts\Activate.ps1
   ```
   Se PowerShell blocca lo script: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (una volta sola, senza admin).
3. Installa le dipendenze:
   ```powershell
   pip install -r requirements.txt
   pip install -r requirements-dev.txt      # solo per lanciare i test
   ```
4. **Verifica l'installazione** (deve finire con `passed`, senza errori; usa solo server locali, nessuna rete):
   ```powershell
   python -m pytest tests -q
   ```
5. In VS Code: Ctrl+Shift+P → "Python: Select Interpreter" → `.venv`.

---

## 4. Configurazione iniziale

### 4.1 File `.env` (consigliato)
Copia `.env.example` in `.env` e compila:

```
SCRAPER_CONTACT=nome.cognome@esempio.org      # email o URL: compare nello User-Agent del crawler
GOOGLE_SAFE_BROWSING_API_KEY=                 # solo per il controllo minacce (§11)
VIRUSTOTAL_API_KEY=                           # idem
```

`SCRAPER_CONTACT` permette ai gestori dei siti di scrivervi invece di bannare l'IP. Se manca, il crawler parte
lo stesso ma scrive un avviso nel log. **Non condividere mai il file `.env`** (né in zip né su git; è già in `.gitignore`).

### 4.2 Struttura delle cartelle
Le cartelle vengono create automaticamente, ma è utile sapere dove guardare:

```
data/input/{ISO}/     file grezzi scaricati, seeds.txt, stato e report del crawl, cache
data/output/{ISO}/    siti.csv (output principale), siti_classificati.csv
data/output/_postprocess/   report del post-processing
logs/run.log          log completo di main.py  (logs/classify.log per il classificatore)
```

---

## 5. Primo run: un paese a download automatico

```powershell
python main.py --list                 # elenca i paesi e il loro stato (ready / todo)
python main.py --country NL -v        # -v = log dettagliato
```

**Cosa deve succedere:** il file viene scaricato in `data/input/NL/`, normalizzato, e scritto
`data/output/NL/siti.csv`; l'ultima riga del terminale dice `[NL] OK — N record scritti`.

**Controlla il risultato:** apri `siti.csv` (UTF-8 con BOM: Excel lo apre bene). Colonne:

| Colonna | Significato |
|---|---|
| `hostname` | URL del sito (normalizzato: schema, host minuscolo; `nan`/`n/d`/email diventano vuoti) |
| `tipo_link` | (crawl) `seed` / `interno` / `esterno` / `redirect` |
| `nome_ente`, `comune`, `regione`, `codice_ipa`, `sector` | dati della fonte ufficiale (dove esistono) |
| `country_code`, `source_name`, `retrieved_at` | provenienza |
| `dominio_radice` | dominio radice (eTLD+1) dell'hostname |
| `host_osservati` | altri host/URL visti per lo stesso ente, separati da `|` |
| `found_on`, `source_seed` | (crawl) pagina e seed da cui è stato scoperto |
| `tls_error` | (crawl) `si` se il certificato TLS non era verificabile |

Più paesi o tutti: `python main.py --country FR --country AT` — `python main.py --all --skip-todo`.
Il codice di uscita è **0 se tutto è andato bene, 1 se almeno un paese è fallito**; il riepilogo è anche in
`data/output/_run_summary.json`.

**Se qualcosa va storto:**
- `Paesi non configurati: [...]` → il codice ISO è sbagliato (vedi `--list`).
- Errore di download → l'URL della fonte è cambiato: aprilo nel browser, copia il link diretto in `source_url`
  del file `config/countries/{ISO}.yaml` (istruzioni per paese in `README.md` §5).
- `PermissionError` sul CSV → è aperto in Excel: il risultato viene salvato comunque in `siti.<data-ora>.csv`.

---

## 6. Paesi con file da copiare a mano (esempio: Spagna)

1. Copia **tutti** i file `.xlsx` DIR3 nella cartella `data/input/ES/` (foglio "Unidades INST V+T",
   intestazioni in riga 2, dati da riga 3).
2. `python main.py --country ES -v`.
3. La Spagna non ha il sito web nel file: la ricerca automatica del sito ufficiale (`website_search`) lo cerca
   partendo dal nome. È **limitata a 50 ricerche** per prudenza (`max_queries` in `ES.yaml`): fai un primo run piccolo,
   controlla la qualità, poi alza il limite. Dettagli e opzioni (backend `wikidata`) in §9.

---

## 7. Il crawler (BE, DE, HR, HU, PL, PT, SE)

### 7.1 Idea
Si parte da una lista di **semi** (siti istituzionali). Per `crawl_depth` livelli si aprono le pagine, si
raccolgono i link e si registra **un dominio radice per riga**, senza duplicati. I domini "di casa" (definiti da
`allowed_domains` / `allowed_tlds`) vengono esplorati; gli altri vengono solo registrati.

### 7.2 Preparare i semi
Crea/modifica `data/input/{ISO}/seeds.txt`, un URL per riga (righe vuote e `#commenti` ignorati):

```
# Semi per il Belgio
https://www.belgium.be
https://finances.belgium.be
```

Se il file non esiste il crawler usa `seed_urls` dello YAML; se non c'è nessuno dei due si ferma spiegando cosa creare.

### 7.3 Lanciare
```powershell
python main.py --country BE -v
```

Ogni 30 secondi il log mostra l'avanzamento, ad esempio:
`[BE] livello 2: 840/3100 pagine, 8 in corso, 512 domini radice, 6.4 pagine/s (2210 pagine in totale)`.

### 7.4 Interrompere e riprendere
- **Ctrl+C** ferma il crawl: attendi qualche secondo (i download in corso terminano), lo stato è salvato.
- **Riprendi** con lo stesso comando + `--resume`:
  ```powershell
  python main.py --country BE --resume
  ```
  Riparte dalle pagine non ancora visitate del livello in corso (al più si rifanno le pagine che erano "in volo"
  al momento dell'interruzione). Senza `--resume` un crawl non finito viene **ignorato e sovrascritto**.
- Se hai cambiato semi, `crawl_depth`, `allowed_domains`, `allowed_tlds`, `exclude_domains`,
  `record_internal_links` o la paginazione, il checkpoint non è più valido e il log ti dice esattamente cosa è cambiato
  (si riparte da zero).
- Un crawl già completato + `--resume` rigenera solo l'output, senza ripetere il crawl.

### 7.5 File prodotti in `data/input/{ISO}/`

| File | Cosa contiene |
|---|---|
| `crawl_results.json` | i domini radice trovati (da qui nasce `siti.csv`) |
| `crawl_state.sqlite` | stato incrementale per il `--resume` (poche righe scritte per pagina). Non cancellarlo se vuoi poter riprendere. Se risulta corrotto viene messo da parte come `*.corrupt-<ora>` e si riparte da uno stato vuoto (o dal JSON storico, se c'è) |
| `crawl_checkpoint.json` | stessa informazione in JSON, esportata a fine livello, a fine crawl e all'interruzione (la legge `tools/postprocess_checkpoints.py`) |
| `crawl_progress.json` / `crawl_report.md` | avanzamento per seed e livello, errori, domini in pausa: **il modo più rapido per capire come sta andando** |

### 7.6 Tutte le opzioni del crawler (nel file `config/countries/{ISO}.yaml`)

| Opzione | Default | Cosa fa |
|---|---|---|
| `seeds_file` | `seeds.txt` | file dei semi in `data/input/{ISO}/` |
| `crawl_depth` | 2 | livelli da esplorare (oltre i semi) |
| `max_pages_per_level` | 100 | tetto **totale** di pagine per livello; le eccedenti sono scartate con un avviso (selezione round-robin per host) |
| `allowed_domains` | domini dei semi | host "di casa" da esplorare (match per suffisso) |
| `allowed_tlds` | ccTLD del paese + `gov` | anche i domini con questi TLD sono "di casa" (match sul suffisso, non su un label qualsiasi) |
| `regional_tlds` | true | unisce i TLD regionali del paese (`.brussels`, `.cat`, `.bayern`…) |
| `exclude_domains` | `[]` | domini mai registrati né seguiti |
| `use_default_infra_blocklist` | true | esclude anche CDN, social, shortener, store di app, widget (`src/web_signals.py`) |
| `record_internal_links` | false | true = registra anche i domini "di casa" scoperti (modalità Belgio) |
| **`workers`** | **8** | pagine scaricate **in parallelo** (host diversi) |
| **`sleep_seconds`** | 0.5 | pausa minima tra due richieste **allo stesso host** |
| `max_concurrent_per_host` | 1 | richieste simultanee ammesse per host |
| **`respect_robots`** | **true** | legge e rispetta robots.txt (§8) |
| **`identify_crawler`** | **true** | User-Agent onesto `eu-pa-scraper/1.0 (+contatto)`; `false` = header da browser |
| `contact` | — | contatto nell'User-Agent (alternativa a `SCRAPER_CONTACT` nel `.env`) |
| `max_crawl_delay` | 30 | tetto (s) al `Crawl-delay` richiesto da un sito |
| `http_retries` | 2 | tentativi immediati per pagina (1 = nessun retry immediato) |
| `domain_cooldown_after_errors` | 1 | errori di rete/403/429/5xx di fila dopo i quali un dominio va "in pausa" |
| `domain_cooldown_after_dead_links` | 5 | link morti (404…) **di fila** prima di sospettare un blocco |
| `domain_cooldown_calls` | 30 | durata della pausa, in chiamate ad altri domini |
| `domain_cooldown_max_retries` | 3 | quante pause prima di rinunciare a un dominio |
| `max_pages_per_pattern` | 500 | anti-trappola: max pagine con la stessa "forma" di URL (calendari, filtri); 0 = disattivo |
| `js_sitemap_fallback` | true | per i siti che richiedono JavaScript prova `sitemap.xml` |
| `max_internal_errors_in_a_row` | 10 | dopo N bug interni consecutivi il crawl si ferma invece di produrre un output vuoto |
| `status_log_seconds` | 30 | ogni quanto scrivere l'avanzamento nel log |
| `follow_pagination` | false | espansione esplicita di sequenze `page_1, page_2…` (**sequenziale**: blocca gli altri worker mentre lavora) |

### 7.7 Quanto ci mette (stima)
Velocità ≈ `min(workers, host_attivi) ÷ (tempo_risposta + sleep_seconds)` pagine al secondo.
Esempio (BE, 21 semi, risposta media 0,5 s, `sleep_seconds: 1.0`): ≈ 21 ÷ 1,5 = 14 pag/s teorici, limitati a
`workers: 8` → ~5 pag/s; 30.000 pagine ≈ **1,7 ore** invece delle ~12,5 ore di un crawler sequenziale.
**Il limite vero è la cortesia verso ciascun host**: se quasi tutte le pagine stanno su un solo host, alzare `workers`
non serve. `crawl_depth: 8` con 10.000–15.000 pagine per livello (valori attuali di BE/DE/HR/…) può richiedere
molte ore: guarda `crawl_report.md` per capire se ne vale la pena e ricorda che puoi fermarti e riprendere.

### 7.8 Prova rapida prima di un run grande
1. Metti temporaneamente nello YAML del paese `crawl_depth: 1` e `max_pages_per_level: 20`.
2. Lancia `python main.py --country BE -v` e controlla `data/input/BE/crawl_report.md` e `data/output/BE/siti.csv`.
3. Ripristina i valori originali e rilancia **senza** `--resume`: cambiando `crawl_depth` il vecchio stato non è più
   valido (il log lo dice) e il run vero parte da zero, come previsto.

### 7.9 Nota su HR / HU / PL / PT / SE
Questi paesi usano `site_crawl` ma hanno ancora il `field_mapping` della vecchia fonte: il loro `siti.csv` avrà
`hostname` vuoto. I domini scoperti sono comunque in `crawl_results.json`, e il classificatore (§10) li legge da lì
in automatico se il CSV non ha hostname.

---

## 8. robots.txt e User-Agent (flag `respect_robots`, default **true**)

- **`respect_robots: true`** (default, sia nel crawler sia nel classificatore): per ogni host il programma legge
  **una volta** `robots.txt` e ne applica divieti (compresi i caratteri jolly `*` e `$`) e `Crawl-delay` (fino a
  `max_crawl_delay`). Le pagine vietate sono saltate e annotate nel log/report come `RobotsDisallowed`.
- **`respect_robots: false`**: robots.txt non viene letto. È una scelta esplicita e la responsabilità è di chi esegue.
- Regole applicate: robots.txt assente o con errore 4xx → tutto consentito; errore **5xx** → host escluso per prudenza;
  irraggiungibile per rete/DNS → consentito.
- **Attenzione, Germania:** `service.bund.de` vieta l'accesso automatico. Con il default, i seed di `DE` vengono saltati e il
  log stampa `TUTTI i seed sono vietati da robots.txt`. Scelte possibili: usare l'**Anschriftenverzeichnis** ufficiale
  (fonte alternativa citata in `DE.yaml`), chiedere un accesso concordato al gestore, oppure mettere
  **`respect_robots: false`** in `config/countries/DE.yaml` sapendo che è una decisione tua.
- **`identify_crawler: true`** (default): User-Agent `eu-pa-scraper/1.0 (+contatto)`. Alcuni siti rifiutano gli
  UA non da browser (403): se te ne accorgi dal log, puoi mettere `identify_crawler: false` per quel paese.
- I **download dei dati ufficiali** (CSV/XLSX/API/XML) non sono un crawl e usano ancora l'header da browser.

---

## 9. Ricerca del sito ufficiale a partire dal nome (es. Spagna)

Nel config del paese:

```yaml
website_search:
  enabled: true
  backend: duckduckgo        # oppure: wikidata
  max_queries: 50
  sleep_seconds: 2.0
  language: es               # solo wikidata: lingua della ricerca
```

- **`wikidata`** è l'API pubblica di Wikidata: stabile, ma copre bene comuni/ministeri/università e meno scuole e piccoli enti.
- **`duckduckgo`** è uno scraping best-effort: si ferma da solo dopo 5 blocchi/captcha di fila.
- I risultati sono in **cache** (`data/input/{ISO}/website_search_cache.json`) e il CSV viene salvato **ogni 25 ricerche**:
  un'interruzione non fa perdere il lavoro; rilanciando, i nomi già cercati non vengono richiesti di nuovo.
- Lo stesso fine si ottiene su un CSV esistente con:
  `python enrich_websites.py --country ES --backend wikidata --language es --max-queries 200`.
- Per ogni nome si sceglie il risultato che **somiglia di più al nome dell'ente** (host/percorso), non semplicemente il primo.

---

## 10. Classificazione dei siti (natura + tipo di ente)

Assegna a ogni sito: **natura** (`pubblico` / `privato` / `terzo_settore` / `incerto` / `infrastruttura` / `non_classificabile`) e
**tipo** (`comune`, `regione_provincia`, `governo_centrale`, `polizia_sicurezza`, `scuola`, `universita`, `sanita`,
`giustizia`, `parlamento`, `agenzia_authority`, `emergenza`, `cultura`, `trasporti`, `ambiente`, `altro`).
**Tutti gli stadi sono disattivati per default**: senza `--stages` il programma non fa nulla e lo dice.

### 10.1 Passi consigliati (dal più economico al più costoso)

```powershell
# 1) solo regole sull'hostname (zero rete, secondi)
python classify_sites.py --country BE --stages 0

# 2) + Wikidata (poche richieste in batch)
python classify_sites.py --country BE --stages 0,2

# 3) + apertura delle homepage e regole multilingue — PROVA PRIMA su 200 siti
python classify_sites.py --country BE --stages 0,2,3 --limit 200 -v
python classify_sites.py --country BE --stages 0,2,3          # poi tutto

# 4) + LLM locale sul residuo (serve Ollama, vedi 10.2)
python classify_sites.py --country BE --stages 0,2,3,4
```

Ogni stadio lavora **solo sui siti ancora incerti** (sotto `stop_confidence`). Se `siti.csv` non ha hostname
(HR/HU/PL/PT/SE) il programma usa il JSON del crawl, oppure indica tu il file: `--input data/input/HR/crawl_service_bund.json`.

### 10.2 Preparare Ollama (solo stadio 4)
1. Installa Ollama e avvia il server: `ollama serve` (di solito parte da solo).
2. Scarica il modello: `ollama pull <nome-modello>` e controlla il nome esatto con `ollama list`.
3. Metti quel nome in `config_classify.yaml` → `stages.stage4.model` (il default è `gemma4:e2b_q8`, come da tua indicazione).
   Lo stadio 4 controlla all'avvio che il server risponda e che il modello esista, altrimenti **salta lo stadio**
   con un messaggio che elenca i modelli disponibili (gli altri stadi proseguono).
4. L'LLM riceve solo un "pacchetto di evidenza" compatto (titolo, descrizione, h1, menu, footer, inizio del testo),
   risponde con un JSON vincolato (temperatura 0) e viene chiamato solo se `min(confidenza) < only_if_below` (0,8).

### 10.3 Il file `config_classify.yaml`
Per attivare gli stadi in modo stabile metti `enabled: true`; per il resto:

| Chiave | Default | Note |
|---|---|---|
| `stages.stage3.workers` | 8 | thread paralleli per aprire le homepage (limite per host sempre rispettato) |
| `stages.stage3.per_host_delay` | 1.5 | secondi minimi tra due richieste allo stesso host |
| `stages.stage3.respect_robots` | **true** | rispetta robots.txt e `Crawl-delay` (§8); homepage vietata → `homepage_status: robots` |
| `stages.stage3.identify_crawler` / `contact` | true / null | User-Agent identificabile |
| `stop_confidence` | 0,8 | sopra questa confidenza (natura **e** tipo) un sito non passa agli stadi successivi |
| `review_below_natura` / `review_below_tipo` | 0,6 / 0,5 | soglie per `da_rivedere` |
| `cache_ttl_days` | 90 | validità della cache |

### 10.4 Leggere l'output `siti_classificati.csv`
Colonne originali + `host_classificato`, `natura`, `tipo`, **`ente_rilevato`**, `confidenza_natura`, `confidenza_tipo`, `stadi`
(quali stadi hanno votato: `0+3+4`), `evidenza` (la ragione in breve), `homepage_status`, `final_url`, `lang`,
`wikidata`, `llm_model`, **`da_rivedere`** (`si` = guardalo a mano), `classified_at`.

`ente_rilevato` è il nome leggibile dell'ente (non entra nel punteggio natura/tipo, serve solo a capire a colpo
d'occhio a chi ti riferisci): la prima fonte disponibile tra `nome_ente` del registro di origine (se il paese ne
ha uno), l'etichetta ufficiale dell'item Wikidata (stadio 2), il nome che il sito dà di sé stesso in homepage
(`og:site_name` → nome nel JSON-LD → `<title>`, stadio 3).

`homepage_status` spiega perché un sito **non** è stato classificato: `blocked` (anti-bot), `robots` (vietato da robots.txt),
`needs_js` (sito che richiede JavaScript: si classifica solo con titolo/meta se bastano), `parked`, `default_page`,
`timeout`, `dns_error`, `tls_error`, `http_error`, `non_html`. **Un sito non leggibile non viene mai "indovinato".**

### 10.5 Cache e miglioramento del lessico
- `data/input/{ISO}/classify_cache.jsonl` conserva evidenze e risposte: rilanciare non rifà le richieste già fatte
  (`--refresh` le ignora).
- Le homepage vengono **rivalutate a ogni run** dalle evidenze in cache: se modifichi `config/classify_lexicon.yaml`
  (parole chiave per lingua, suffissi, classi Wikidata) rilanci e vedi l'effetto **senza riscaricare nulla**.

### 10.6 Meccanismi specifici per paese: `country_overrides` e `allowed_tlds`
`config/classify_lexicon.yaml` ha una sezione `country_overrides` per parole/segnali validi **solo** quando
`classify_sites.py` gira con quel `--country`. Servono per due casi che il lessico "globale" non può coprire da solo:

**a) Parole ambigue in un'altra lingua.** Es. "sąd" (tribunale in polacco) collide con l'inglese "sad" (triste);
"sud" (tribunale in croato/slovacco) collide col francese "sud" (punto cardinale). Bandirle dal lessico globale le
perde ovunque; metterle globali senza filtro crea falsi positivi. La soluzione: valgono solo se **country E lingua
della pagina combaciano entrambi** (non basta il solo `--country`: una pagina francese su un sito polacco non fa
scattare "sąd"). Struttura nello YAML:
```yaml
country_overrides:
  PL:
    lang: pl                       # <html lang="pl">/"pl-PL"/"PL"... tutti equivalenti (normalizzato)
    tipo_keywords:
      giustizia: ["sad!"]
```
Senza `lang` dichiarato, il default è il codice paese in minuscolo. Una pagina che non dichiara affatto `lang`
non fa scattare l'override (per prudenza: meglio perdere un match vero che rischiare un falso positivo).

**b) Il segnale del TLD (`allowed_tlds`).** Per un paese crawlato, moltissimi domini "sconosciuto" sono in realtà
rumore del crawler (link esterni raccolti incidentalmente: giornali, aziende private di altri paesi). Su un
campione reale etichettato a mano per NL, i domini `.com/.fr/.de/.fi/.uk` erano enti pubblici solo nel **2,4%**
dei casi, contro il **57%** di `.nl/.org`. Con `allowed_tlds` dichiarato per un paese, un TLD **non** in lista dà
un voto **debole** (confidenza 0.35, mai decisivo da solo) verso `natura=privato`:
```yaml
country_overrides:
  NL:
    allowed_tlds: [nl, org, eu]
```
Un sito con prove pubbliche forti altrove (lessico, Wikidata) non viene comunque spinto verso privato da questo
solo segnale — vedi §10.7 per come è stato verificato che funziona davvero (92% di precisione su dati reali).

### 10.7 Convalidare le classificazioni: `audit_sample.py`
**L'accordo di un segnale con la classificazione che il programma ha già prodotto non prova che sia corretto** —
sta solo verificando che sia d'accordo con se stesso. L'unico modo onesto di sapere se una modifica al lessico ha
aiutato è guardare un campione a mano e confrontarlo con la verità. `audit_sample.py` (nella cartella principale)
automatizza la parte noiosa: campiona, tu etichetti, si confronta.

**Modalità 1 — campione stratificato** (per farsi un'idea generale della qualità):
```powershell
python audit_sample.py --country NL --per-group 4
```
Scrive `audit_NL.csv` con fino a `--per-group` righe per ogni combinazione (natura, tipo) presente nell'output —
così anche le categorie rare (parlamento, emergenza...) finiscono nel campione, non solo quelle enormi. Due colonne
vuote, `natura_vera` e `tipo_vero`, da riempire a mano guardando ogni sito.

**Modalità 2 — differenza tra due run** (per isolare l'effetto di UNA modifica al lessico):
```powershell
python audit_sample.py --country NL --compare-to siti_classificati_prima_della_modifica.csv
```
Prima di modificare il lessico, **copia da parte** il `siti_classificati.csv` corrente (è il "prima"). Dopo la
modifica e un nuovo run, questo comando campiona **solo le righe che sono cambiate** tra i due — il campione più
informativo per capire se la modifica ha aiutato o peggiorato le cose, invece di controllare a caso su tutto il
dataset (dove le righe cambiate potrebbero essere una minoranza sepolta nel resto).

**Esempio reale** (paese NL, sessione del 29-30/09/2026): il segnale `allowed_tlds` è stato validato su un
campione di 56 host etichettati a mano, risultato **92% di precisione** sulle righe dove il segnale ha
effettivamente agito; una correzione al lessico (parola troppo generica `"agenzia!"` che classificava agenzie di
stampa come enti pubblici) è stata isolata con `--compare-to` a 9 righe cambiate, **9/9 corrette**. Nessuna delle
due cose si sarebbe potuta sapere guardando solo le percentuali di `natura`/`tipo` prima e dopo.

**Quando farlo**: dopo ogni modifica a `config/classify_lexicon.yaml` che ti aspetti cambi molte righe (nuova
lingua, nuovo `allowed_tlds`, nuova categoria), non solo all'inizio. Un campione di 50-60 righe dà già un'idea
ragionevole (margine d'errore ~±13% su una proporzione, al 95% di confidenza); per numeri più solidi servono
campioni più grandi (200-400), specie se vuoi calcolare precisione **per singola categoria** e non solo generale.

---

## 11. Post-processing e controllo minacce (facoltativo)

```powershell
python tools/postprocess_checkpoints.py --country BE
```

Legge `crawl_checkpoint.json` (nessuna nuova richiesta ai siti) e scrive in `data/output/_postprocess/`:
`report_generale.md/json`, `tutte_le_terze_parti.csv` (ogni dominio esterno e il tipo di riferimento: link, script, iframe…),
`link_sospetti.md/csv/json`. Con le chiavi nel `.env` controlla i domini su Google Safe Browsing e VirusTotal,
rispettando le quote giornaliere (rilancia il giorno dopo per continuare). **"nessun segnale" non è una garanzia di sicurezza.**

**Stato attuale: elabora TUTTI i domini di terza parte trovati dal crawl, senza distinguere per natura/tipo.**
Questo script lavora sui dati del **crawler** (`crawl_checkpoint.json`), che non sa nulla di `natura`/`tipo` — quel
concetto esiste solo in `siti_classificati.csv`, prodotto da `classify_sites.py` (§10), uno strumento separato.
Oggi non c'è quindi modo di dire "controlla le minacce solo sui siti pubblici": vedi §15.1 per il piano.

VirusTotal in particolare è **lento apposta** (rate limit gratuito, ~16 secondi tra una chiamata e l'altra): con
molti domini da controllare l'attesa può durare ore. Il programma logga l'avanzamento (`N/tot (P%) - v/s - ETA`)
ogni pochi secondi — se non vedi nulla per minuti, controlla di aver installato correttamente le dipendenze e che
il log non sia silenziosamente vuoto (versioni di questo progetto precedenti al 29/09/2026 avevano un bug che
sopprimeva questi log: assicurati di avere la versione aggiornata).

---

## 12. Ricetta completa tipica (Belgio, dall'inizio alla fine)

```powershell
py -3 -m venv .venv ; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env                       # poi compila SCRAPER_CONTACT
python -m pytest tests -q                    # verifica installazione (aggiungi requirements-dev.txt)
notepad data\input\BE\seeds.txt              # semi (un URL per riga)
python main.py --country BE -v               # crawl (Ctrl+C per fermare, --resume per riprendere)
type data\input\BE\crawl_report.md           # com'è andata?
python classify_sites.py --country BE --stages 0,2,3 --limit 200 -v      # prova
python classify_sites.py --country BE --stages 0,2,3                     # tutto
copy data\output\BE\siti_classificati.csv siti_classificati_prima.csv    # tieni da parte per confronti futuri (§10.7)
python audit_sample.py --country BE --per-group 4                        # campione da controllare a mano
python tools/postprocess_checkpoints.py --country BE                     # report terze parti
```

---

## 13. Risoluzione dei problemi

| Sintomo | Causa probabile | Cosa fare |
|---|---|---|
| `Nessun seed trovato` | manca `seeds.txt` | crealo in `data/input/{ISO}/` (§7.2) |
| Il crawl trova solo i semi e il log dice `TUTTI i seed sono vietati da robots.txt` | il sito vieta i bot | §8: fonte alternativa, contatto col gestore, oppure `respect_robots: false` |
| Molti `403` / `dominio ... messo in pausa` | il sito blocca lo User-Agent o il ritmo | alza `sleep_seconds`, prova `identify_crawler: false`, controlla `crawl_report.md` |
| `ERRORE INTERNO (non di rete)` ripetuto e il crawl si ferma | bug o versione incompatibile | il traceback è nel log: segnalalo; non è un problema di rete |
| Il crawl è lento | un solo host domina, o `sleep_seconds` alto | la cortesia per host è il limite; alza `workers` solo se hai molti host diversi |
| `--resume` riparte da zero | è cambiata una impostazione del perimetro | leggi l'avviso nel log: elenca cosa è cambiato |
| Un sito con certificato scaduto | catena TLS incompleta | il crawler lo apre comunque **solo per la discovery** e segna `tls_error = si` |
| `Impossibile sovrascrivere siti.csv` | file aperto in Excel | usa il file `siti.<data-ora>.csv` creato, o chiudi Excel e rilancia |
| Stadio 4 saltato: `Ollama non raggiungibile` / `Modello ... non trovato` | server spento o nome modello sbagliato | `ollama serve`, `ollama list`, correggi `stage4.model` |
| Wikidata `429` / batch falliti | troppe richieste | riprova più tardi: i batch falliti non vengono messi in cache |
| Dopo Ctrl+C il terminale resta occupato qualche secondo | i worker terminano la richiesta in corso | è normale (max ~10–20 s) |
| HR/HU/PL/PT/SE: `hostname` vuoto nel CSV | vecchio `field_mapping` (§7.9) | usa il JSON del crawl con il classificatore, o allinea il mapping |
| Windows: `PermissionError` su file di stato | antivirus/OneDrive | sposta il progetto fuori da OneDrive; i salvataggi ritentano da soli |
| VirusTotal sembra "fermo" per minuti | è voluto: rate limit gratuito (~16s/dominio) | guarda il log (`N/tot ... ETA ...`); se non compare nulla, la versione è vecchia (§11) |
| `audit_sample.py`: "non trovo .../siti_classificati.csv" | `classify_sites.py` non è ancora stato lanciato per quel paese | lancialo prima (§10.1) |
| `country_overrides` non sembra scattare | manca `lang` sulla pagina, o `--country` non combacia | vedi §10.6: senza `<html lang="...">` l'override non scatta di proposito (prudenza) |

---

## 14. Verifica e sicurezza — promemoria

- `python -m pytest tests -q` prima di un run importante e dopo ogni modifica al codice.
- Non committare né condividere `.env`; se una chiave è finita in un archivio, **ruotala**.
- I file in `data/` sono cache e risultati: non sono nel repository (`.gitignore`).
- Il crawler è **educato per impostazione predefinita** (robots.txt, `Crawl-delay`, pausa per host, UA identificabile):
  disattivare queste protezioni è possibile ma è una scelta esplicita e responsabilità di chi esegue.

---

## 15. Prossimi passi / Roadmap (non ancora implementati)

Due estensioni pianificate ma non ancora scritte. Le documento qui perché servono a capire **dove si inseriscono**
nel flusso esistente prima di scriverle, non come funzionalità già disponibili.

### 15.1 Controllo minacce solo su pubblico/terzo settore

**Obiettivo**: far girare `tools/postprocess_checkpoints.py` (§11) solo sui domini di terza parte che compaiono su
pagine di siti con `natura` = `pubblico` o `terzo_settore` in `siti_classificati.csv` — i siti privati non
interessano ai fini di un audit di sicurezza della PA.

**Perché non è banale**: come già detto in §11, `postprocess_checkpoints.py` lavora sui dati del **crawler**
(`crawl_checkpoint.json`: pagine visitate, link/script/iframe trovati su ciascuna), che non ha alcuna nozione di
natura/tipo — quello lo calcola `classify_sites.py` **dopo**, in un file completamente separato
(`siti_classificati.csv`). Oggi i due strumenti sono indipendenti: si può lanciare il post-processing anche senza
aver mai classificato nulla.

**Cosa serve implementare** (in `tools/postprocess_checkpoints.py`):
1. **Nuovo ordine di dipendenza esplicito**: `classify_sites.py --country XX` deve girare (almeno stadio 0, meglio 0+2+3)
   **prima** del post-processing filtrato — oggi non è un requisito, dovrebbe diventarlo per questa modalità.
2. **Join per hostname**: leggere `data/output/{ISO}/siti_classificati.csv`, costruire una mappa
   `host_classificato -> natura`, e quando `collect_occurrences()` scorre il `resource_index` del checkpoint,
   scartare le occorrenze la cui **pagina di origine** (non il dominio di terza parte trovato: quello resta
   "chiunque sia", è la pagina che lo linka a dover essere pubblica) appartiene a un host con natura diversa da
   `pubblico`/`terzo_settore`.
3. **Nuova opzione**, sia a riga di comando sia in `config_postprocess.yaml`, per attivare il filtro — deve restare
   **facoltativo** (default: comportamento attuale, tutti i domini) per non rompere chi già usa lo strumento senza
   aver mai classificato nulla. Proposta: `--solo-natura pubblico,terzo_settore` (CLI) /
   `link_sospetti.solo_natura: [pubblico, terzo_settore]` (YAML), coerente con come sono già strutturate le altre
   opzioni di `config_postprocess.yaml`.
4. **Caso di bordo da decidere**: una pagina di origine che compare nel checkpoint ma **non** compare (o non è
   ancora stata classificata) in `siti_classificati.csv` — la si include per prudenza (comportamento attuale) o la
   si scarta? Prudente: includerla (meglio un falso positivo in più nel report che perdere un sito pubblico non
   ancora classificato).

### 15.2 Output per `dark_stack_toolkit`

**Obiettivo**: produrre, a partire da `siti_classificati.csv` (§10.4), un file con le stesse informazioni ma
**colonne riorganizzate/rinominate** nel formato atteso da un'altra applicazione, `dark_stack_toolkit`, con cui
questo progetto deve interfacciarsi.

**Perché non lo implemento ora**: non ho a disposizione lo schema di output che `dark_stack_toolkit` si aspetta
(nomi colonna, ordine, formato dei valori — es. se `natura`/`tipo` vanno passati come stringa italiana così come
sono oggi, o rimappati su codici propri di quell'applicazione, se vuole un CSV o un JSON, se ci sono campi
obbligatori non presenti nell'output attuale). Scrivere qualcosa adesso vorrebbe dire indovinare una struttura
che quasi certamente andrebbe riscritta.

**Cosa serve prima di poterlo implementare**:
1. Lo schema di output di `dark_stack_toolkit` (elenco colonne attese, tipo di ciascuna, eventuale documentazione
   o file di esempio già prodotto a mano).
2. Sapere se `dark_stack_toolkit` legge un **file** (CSV/JSON su disco, in che cartella) o si aspetta una
   **chiamata** (script che scrive e poi lo invoca, API, altro).
3. La mappatura dei valori: es. se le categorie di `tipo` (`comune`, `agenzia_authority`...) restano quelle di
   questo progetto o vanno tradotte in una tassonomia diversa già usata da `dark_stack_toolkit`.

**Come lo implementerei, una volta nota la risposta**: uno script dedicato a livello di cartella principale
(sul modello di `audit_sample.py` o `embedding_report.py` visti in questa guida: legge un CSV, non tocca la rete,
scrive un altro CSV/JSON), che prende `siti_classificati.csv` in input e scrive l'output nel formato richiesto —
senza toccare `classify_sites.py` stesso, per non mescolare "come classifico" con "come esporto per un consumatore
esterno" (stessa logica di separazione già usata tra crawl, classificazione e post-processing in questo progetto).
