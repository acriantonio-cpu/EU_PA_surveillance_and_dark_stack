# EU PA Scraper — Osservatorio infrastruttura tecnica PA (UE, esclusa Italia)

> **English overview.** `eu-pa-scraper` builds the list of public-administration websites for the **26 EU countries other than Italy**, which already has the official IndicePA registry. One engine (`main.py` + `src/`) is driven by one YAML file per country (`config/countries/{ISO}.yaml`). The available source types are `bulk_csv`, `bulk_xlsx`, `bulk_xlsx_folder`, `bulk_xml`, `api_json`, `html_scrape` and `site_crawl`, the generic layered crawler that starts from `seeds.txt`. There are also custom fetchers for France and the Netherlands.
>
> Output: `data/output/{ISO}/siti.csv`, in a common schema (`src/schema.py`).
>
> Sections, in order:
> - *0 / 0-bis*: changelog (the full log is in `CHANGES.md`).
> - *1 Setup*: installation.
> - *2 Uso*: usage (`python main.py --list`, `--country XX`, `--all --skip-todo`, `--resume`).
> - *3 Stato per paese*: status per country (`ready` or `todo`).
> - *4–5*: how to complete a country, with a per-country guide.
> - *6*: the layered crawler.
> - *7*: automatic search for an entity's official website.
> - *10*: site classification (`classify_sites.py`).
>
> The threat-screening post-processor is `tools/postprocess_checkpoints.py`. The [glossary](../GLOSSARY.md) translates the terms; `GUIDA_PASSO_PASSO.md` is the step-by-step guide. Tests: `python -m pytest tests -q`, 109 tests that run offline.

Tool unico in Python per estrarre, da 26 paesi UE (tutti tranne l'Italia,
già coperta da IndicePA), l'elenco degli enti pubblici con:

```
hostname, comune, regione, codice_ipa, sector, country_code, source_name, retrieved_at
```

> **Per eseguire il progetto passo passo: vedi [`GUIDA_PASSO_PASSO.md`](GUIDA_PASSO_PASSO.md).**

## 0-bis. Novità del 24/09/2026 (audit e correzioni)

Vedi **CHANGES.md** per l'elenco completo (punto per punto, con cosa NON è stato toccato).
In breve: `HttpSession.get` ora supporta `max_bytes`/`skip_non_html`/`read_timeout` (prima il
crawler falliva su ogni pagina), il match `allowed_tlds` è solo sul suffisso, URL canonicalizzati,
i 404 non mettono più in pausa i domini, redirect/challenge/JS-rendered riconosciuti, output con
`host_osservati`, scritture CSV atomiche, e il nuovo **`classify_sites.py`** (§10, tutti gli stadi OFF
di default). **Seconda tornata (stesso giorno):** robots.txt e `Crawl-delay` rispettati di default
(`respect_robots: true`), User-Agent identificabile, crawler **parallelo per host** (`workers: 8`) e stato
del crawl **incrementale su SQLite** invece del JSON riscritto a ogni pagina. Test: `python -m pytest tests -q`.

## 0. Novità di questo aggiornamento (13/09/2026)

- **Fix dopo un run reale su BE/DE** (nessuno bloccava lo script, ma
  riducevano la copertura):
  - **Bruxelles**: il dominio `www.brussels.brussels` che avevo messo nella
    lista iniziale **non esiste** — corretto in `be.brussels` (verificato).
  - **Header HTTP più "da browser"** (`src/base_fetcher.py`): riduce i
    blocchi generici di WAF/firewall su siti che rifiutavano uno User-Agent
    che si autodichiarava scraper (403 su chancellery.belgium.be,
    connessione interrotta su ibz.be). Non elimina blocchi anti-bot
    deliberati o robots.txt (giustamente).
    **Aggiornamento 24/09/2026:** il crawler e il classificatore ora si
    identificano di default (`identify_crawler: true`, UA
    `eu-pa-scraper/1.0 (+contatto)`) e rispettano robots.txt
    (`respect_robots: true`); i download dei dati ufficiali (CSV/XLSX/API) usano
    ancora l'header "da browser". Vedi `GUIDA_PASSO_PASSO.md` §7.
  - **Bug del crawler risolto**: ora rispetta il tag HTML `<base href="...">`
    quando presente, che spiegava gli URL "innestati male" (404) visti nei
    log della Germania.
- **Crawler a livelli (🇩🇪 DE / 🇧🇪 BE / futuri) — output a livello di
  dominio radice.** L'output è la lista dei **domini radice** (eTLD+1, via
  `tldextract`) coinvolti, una sola riga per dominio unico anche se
  scoperto su più pagine diverse, includendo anche i domini radice dei
  semi stessi (`tipo_link="seed"`). Vedi §6.

Aggiornamenti precedenti (12/09/2026), dettagli nei paragrafi dedicati più
sotto:

- **🇫🇷 Francia — bug risolto (aggiornato 2 volte).** L'endpoint usato in
  precedenza (`data.gouv.fr/api/1/datasets/?q=...`) è l'API di *ricerca nel
  catalogo* dei dataset, non l'annuario: per questo tornava vuoto. Il
  fetcher scarica ora l'URL "stable" corretto — che si è scoperto fare un
  redirect verso un **archivio tar.bz2** (~350 MB) contenente un file con
  chiave `"service"` dove ogni ente può avere **più link** (`site_internet`
  è una lista): il fetcher estrae solo il file utile dall'archivio (in
  memoria, senza scompattare la cartella dei comuni che non contiene link)
  e produce **una riga per ogni link trovato**, classificato nel nuovo campo
  `tipo_link`. Vedi §5 → 🇫🇷 FR.
- **🇩🇪 Germania — crawler a livelli.** Fetcher generico `site_crawl`: parte
  dalle due pagine di ricerca indicate, resta sullo stesso dominio per 2
  livelli, poi raccoglie i link uscenti (esclusi Facebook/Twitter-X/
  LinkedIn/Xing). **Attenzione**: `service.bund.de` risulta bloccato da
  robots.txt per l'accesso automatico: con `respect_robots: true` (default) il crawler
  lo rispetta e salta le pagine vietate — vedi il disclaimer in §5 → 🇩🇪 DE.
- **🇧🇪 Belgio — nuovo crawler a livelli.** Stessa logica generale del
  crawler tedesco, generalizzata: parte dai ~21 portali ufficiali indicati
  (belgium.be, monarchie.be, i vari SPF/FOD, le regioni, NBB, KBO,
  Statbel...), e per `n` livelli "investiga" ogni pagina trovata in cerca di
  altri link — sia verso gli stessi domini istituzionali (che continuano a
  essere esplorati) sia verso domini nuovi (registrati come scoperta, non
  esplorati oltre), escludendo i social media. **Generalizzato**: i semi si
  leggono ora da un file `seeds.txt` nella cartella di input del paese
  (non più solo dallo YAML) — vedi §6 per attivare la stessa strategia
  su qualunque altro paese. Vedi §5 → 🇧🇪 BE — **prima di
  un run completo, conferma i parametri di profondità/ampiezza**, impostati
  a valori prudenti di default.
- **🇪🇸 Spagna — import multi-file + ricerca sito ufficiale.** Nuovo tipo di
  fetcher generico `bulk_xlsx_folder`: importa tutti i file .xlsx presenti
  in `data/input/ES/` con lo schema del file DIR3 "Unidades Institucionales"
  (colonna C = nome ente), poi cerca in automatico il sito ufficiale di ogni
  ente a partire dal nome. Vedi §5 → 🇪🇸 ES e §7 (ricerca sito ufficiale).

Aggiunti anche due nuovi campi comuni a tutti i paesi: **`nome_ente`** (nome
dell'ente/unità) e **`tipo_link`** (classificazione del link quando un ente
ha più siti/collegamenti, usato per ora solo dalla Francia). Utili sia di
per sé sia come input per la ricerca del sito ufficiale.


**Un solo motore di scraping** (`main.py` + `src/`), ma **config, input e
output separati per paese**:

```
eu_pa_scraper/
  main.py
  enrich_websites.py               <- rilancia solo la ricerca sito ufficiale su un CSV già generato
  requirements.txt
  config/countries/{ISO}.yaml     <- sorgente ufficiale + mapping campi, per paese
  data/input/{ISO}/                <- file grezzi scaricati (cache), per paese
  data/output/{ISO}/siti.csv       <- risultato normalizzato, per paese
  src/
    schema.py                      <- schema comune di output
    base_fetcher.py                <- classi base + sessione HTTP con retry
    registry.py                    <- legge i config YAML e istanzia il fetcher giusto
    website_finder.py              <- ricerca euristica del sito ufficiale da nome ente
    fetchers/
      generic.py                   <- fetcher generici pilotati da YAML:
                                       bulk_csv, bulk_xlsx, bulk_xlsx_folder, bulk_xml,
                                       api_json, html_scrape, site_crawl
      nl.py, fr.py                 <- fetcher custom per i pochi casi con logica ad-hoc
  logs/run.log                     <- log di ogni esecuzione
```

## 1. Setup su Windows 11 + VS Code

1. Installa Python 3.11+ da python.org (spunta "Add python.exe to PATH").
2. Apri la cartella `eu_pa_scraper` in VS Code (File → Apri cartella...).
3. Apri il terminale integrato (Ctrl+`) e crea un virtualenv:

   ```powershell
   py -3 -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   Se PowerShell blocca l'attivazione dello script, esegui una volta (come
   utente, non serve admin):

   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   ```

4. Installa le dipendenze:

   ```powershell
   pip install -r requirements.txt
   ```

5. In VS Code, seleziona l'interprete del virtualenv: Ctrl+Shift+P →
   "Python: Select Interpreter" → `.venv`.

## 2. Uso

Elenca i paesi configurati:

```powershell
python main.py --list
```

Esegui un paese (già pronto all'uso, vedi §3):

```powershell
python main.py --country NL
python main.py --country FR --country HR
```

Esegui tutti i paesi configurati (salta quelli non ancora completati):

```powershell
python main.py --all --skip-todo
```

Output: `data/output/{ISO}/siti.csv`. File grezzi scaricati restano in
`data/input/{ISO}/` come cache/evidenza. Log completo in `logs/run.log`.

## 3. Stato di completamento per paese

Ogni `config/countries/{ISO}.yaml` ha un campo `status`:

- `status: ready` → config completo (o quasi), eseguibile da subito. In
  molti casi il campo `hostname` (URL del sito) resta vuoto perché la fonte
  bulk ufficiale non lo contiene (vedi il campo `notes` in ciascun config
  per la fonte secondaria consigliata per colmare il gap, come da relazione
  di partenza).
- `status: todo` → servono passi manuali prima del primo run: es.
  registrazione per ottenere credenziali/URL di download (Polonia, Belgio),
  individuazione di selettori CSS per lo scraping HTML (Bulgaria, Malta,
  Cipro, Lussemburgo), o verifica dell'endpoint API esatto (Lettonia,
  Estonia, Grecia, Ungheria).

Con `--all --skip-todo` gli `status: todo` vengono saltati automaticamente;
senza quel flag, il tool segnala l'errore con le istruzioni su cosa completare.

## 4. Come aggiungere/completare un paese

La maggior parte dei paesi usa la **engine generica** (`src/fetchers/generic.py`),
pilotata solo dal file YAML — non serve scrivere codice Python. I 5 tipi
disponibili (`source_type`):

| source_type       | Per quando la fonte è...                         | Parametri chiave nel YAML |
|-------------------|-----------------------------------------------------|----------------------------|
| `bulk_csv`        | un CSV scaricabile in blocco                       | `source_url`, `delimiter`, `field_mapping` |
| `bulk_xlsx`       | un file Excel                                      | `source_url`, `sheet_name`, `field_mapping` |
| `bulk_xlsx_folder`| più file Excel con lo stesso schema, messi a mano in `data/input/{ISO}/` | `sheet_name`, `data_start_row`, `field_mapping` (per LETTERA di colonna) |
| `bulk_xml`        | un file XML                                        | `source_url`, `record_xpath`, `field_xpaths` o `field_mapping` |
| `api_json`        | un'API REST che risponde in JSON                   | `source_url`, `records_path`, `paginate`, `field_mapping` |
| `html_scrape`     | una directory web da raschiare (nessun export)     | `source_url`/`list_urls`, `list_selector`, `field_selectors` |
| `site_crawl`      | un portale aggregatore che linka siti esterni (es. bacheche annunci), o una rete di siti da esplorare in profondità | `seed_urls`, `crawl_depth`, `allowed_domains`, `exclude_domains`, `record_internal_links`, `follow_pagination` |

`field_mapping` mappa i campi di output (`hostname`, `comune`, `regione`,
`codice_ipa`, `sector`) sui nomi di colonna/tag/chiave della fonte
originale (supporta path annidati con la notazione `a.b.c` per JSON/XML).

Per i pochi casi con logica davvero non riconducibile a un semplice mapping
(es. Paesi Bassi: XML annidato con namespace variabili; Francia: paginazione
API + normalizzazione custom), si scrive un piccolo modulo dedicato in
`src/fetchers/{iso_lower}.py` con una classe `{ISO}Fetcher(BaseFetcher)`, e
lo si referenzia nel config con `custom_module: {iso_lower}` (vedi `NL.yaml`
e `FR.yaml` come esempio completo).

### Passi tipici per completare un config "todo"

1. Apri `config/countries/{ISO}.yaml` e la sezione `notes` per il contesto.
2. Apri l'URL della fonte nel browser, individua il link diretto al file di
   export (o, per `html_scrape`, apri DevTools → Elements per copiare i
   selettori CSS della riga-ente e dei singoli campi).
3. Aggiorna `source_url` con il link diretto (non la pagina indice).
4. Scarica una volta il file manualmente e apri le prime righe per
   verificare i nomi reali delle colonne/campi; aggiorna `field_mapping`
   (o `field_selectors` per lo scraping HTML) di conseguenza.
5. Cambia `status: todo` in `status: ready`.
6. Esegui `python main.py --country {ISO} -v` (il flag `-v` mostra i log
   di debug) e controlla `data/output/{ISO}/siti.csv`.

## 5. Guida passo-passo per ciascun paese

Per ogni paese: cosa aprire nel browser, cosa verificare/cercare, cosa
modificare nel file `config/countries/{ISO}.yaml`, e il comando da lanciare
in VS Code. I paesi già `status: ready` si possono lanciare subito (il
passo di verifica resta comunque consigliato, perché i nomi di colonna
reali vanno confermati sul file scaricato).

### 🇦🇹 AT — Austria
1. Apri `data.statistik.gv.at`, dataset "Gemeindeverzeichnis" (OGDEXT_GEM_1), e copia il link diretto al CSV/ODS.
2. Incollalo in `source_url` di `config/countries/AT.yaml`; apri il file scaricato e controlla i nomi colonna (`Gemeindename`, `Bundesland`, `GKZ`).
3. Nessun campo sito web ufficiale: `hostname` resterà vuoto salvo integrazione manuale con il dataset di terze parti citato nelle `notes`.
4. Esegui: `python main.py --country AT -v`

### 🇧🇪 BE — Belgio (nuovo: crawler a livelli sui portali ufficiali)
1. **Fai prima un test piccolo**: apri `BE.yaml` e abbassa temporaneamente `max_pages_per_level` (es. a 10) e `crawl_depth` (es. a 1). Esegui `python main.py --country BE -v` e controlla `data/output/BE/siti.csv`: la colonna `tipo_link` dice se ogni link è "interno" (stesso network di domini istituzionali) o "esterno" (dominio nuovo scoperto), `sector` riporta la pagina su cui è stato trovato.
2. Se i risultati sono quelli attesi, alza gradualmente `crawl_depth` (2-3) e `max_pages_per_level` (es. 100-300) in `BE.yaml`. **Attenzione ai tempi**: con `sleep_seconds: 1.0` e 21 portali di partenza, un run a `crawl_depth: 2` con `max_pages_per_level: 100` può visitare fino a ~2.100 pagine (21 semi + fino a 2.100 pagine di livello 2), quindi anche 30-40 minuti; alza questi numeri con cautela.
3. Se non ti interessano i link "interni" (solo le scoperte di domini nuovi, come nel crawler tedesco), metti `record_internal_links: false` in `BE.yaml`.
4. Esegui il run completo: `python main.py --country BE -v`
5. Il crawler legge e rispetta robots.txt (e `Crawl-delay`) di ogni host (`respect_robots: true`, default). Restano da verificare a campione i termini d'uso dei singoli portali prima di un run su larga scala.

**Come funziona**: livello 1 = link trovati sulle 21 pagine seed; livello 2 = link trovati sulle pagine di livello 1 che appartengono ai domini "di casa" elencati in `allowed_domains` (es. `belgium.be`, `fgov.be`: il match è per suffisso, quindi copre automaticamente tutti i sottodomini come `finances.belgium.be`); i domini scoperti fuori da questa lista vengono comunque registrati (spesso sono le scoperte più utili — agenzie specifiche non elencate esplicitamente) ma non ulteriormente esplorati, per restare in un perimetro gestibile invece di rischiare di espandersi su tutto il web belga.

**Paginazione numerica (`follow_pagination`)**: se un elenco su uno dei portali è paginato con URL tipo `.../page_1`, `.../page_2`, ... normalmente il crawler li seguirebbe solo come link interni normali, soggetti a `crawl_depth` e senza garanzia di arrivare all'ultima pagina. Con `follow_pagination: true` in `BE.yaml` (o `DE.yaml`), non appena il crawler incontra un link che combacia con un pattern di paginazione riconosciuto genera ed esplora ESPLICITAMENTE l'intera sequenza (indipendentemente da `crawl_depth`), estraendo tutti i link annidati da ciascuna pagina, e si ferma da solo quando rileva la fine della sequenza (pagina non raggiungibile, o contenuto/link identici alla precedente per `pagination_stop_after_empty` pagine di fila). Vedi il docstring di `SiteCrawlFetcher` in `src/fetchers/generic.py` per l'elenco completo delle opzioni (`pagination_regex`, `pagination_start_page`, `pagination_max_pages`, `pagination_stop_after_empty`, `pagination_sleep_seconds`).

**TLD di fiducia (`allowed_tlds`)**: oggi, se un seed (es. `belgium.be`) punta a un dominio non elencato in `allowed_domains` (es. `vlaanderen.be`), quel dominio viene registrato come scoperta ma NON esplorato oltre — eventuali link suoi verso enti più specifici (es. singoli comuni) restano quindi invisibili. Ogni config paese ha ora un default `allowed_tlds: [<ccTLD del paese>, "gov"]` (es. `["be", "gov"]` per il Belgio): un dominio scoperto è considerato "interno" — e quindi esplorato anche ai livelli successivi — se combacia con `allowed_domains` OPPURE se il suo TLD nazionale (o un label `gov`, utile per domini tipo `gov.ie`) compare tra i suoi label, non solo se è elencato esplicitamente. Si può personalizzare (o svuotare, mettendo `allowed_tlds: []`) in ciascun `{ISO}.yaml`.

Accanto al checkpoint (pensato per la ripresa, non per essere letto a mano), viene mantenuto anche `data/input/{ISO}/crawl_progress.json`: un riepilogo leggibile, aggiornato a ogni livello completato e a ogni interruzione, con — PER OGNI SEED di partenza e PER OGNI LIVELLO — quante pagine sono state analizzate e quali domini sono stati trovati (`seeds[].levels[].pages_visited`, `.domains_found`, `.domains`). A differenza del checkpoint, questo file NON viene cancellato a fine crawl: resta come riepilogo finale del run, utile anche solo per capire a colpo d'occhio quale portale ha prodotto quali scoperte, senza dover decifrare `crawl_belgium.json`.

**Il crawl non si interrompe più per un singolo intoppo**: tre categorie di problemi, viste su run reali (HU/DE: markup non standard che mandava in crash il parser HTML con `ParserRejectedMarkup`; SE/DE: contenuto binario spazzatura — causato dal client che dichiarava di accettare compressione Brotli (`br`) senza avere il pacchetto per decomprimerla, ora rimosso da `Accept-Encoding`; PT: un singolo `href` con IPv6 malformato che mandava in crash `urljoin`; BE: `PermissionError` di Windows nel salvare il checkpoint, tipicamente antivirus/OneDrive che tengono il file in mano per un istante) — non fanno più fallire l'intero paese:
- **Markup/contenuto**: `_extract_links` prova in sequenza `html.parser`, `lxml`, `html5lib`; se nessuno riesce, o un singolo link è malformato, quella pagina/link contribuisce 0 link invece di far crashare il crawl. Un controllo sul `Content-Type` evita anche di dare in pasto al parser contenuto chiaramente non-HTML (PDF, immagini, ...).
- **Scritture su disco** (checkpoint, report): "best-effort" — un paio di ritentativi, poi si prosegue comunque con un avviso invece di fermarsi.
- **Alternanza anti-blocco per dominio** (`domain_cooldown_after_errors`, `domain_cooldown_calls`, `domain_cooldown_max_retries`): alcuni siti rispondono con errori (spesso 404, a volte 403/429) quando si fanno troppe richieste ravvicinate. Un dominio che dà errore viene messo in pausa per un tot di chiamate ad ALTRI domini (si resta sullo stesso livello, alternando con gli altri siti già in coda) e poi ritentato; oltre un tetto di tentativi (default 3) si rinuncia definitivamente per quel dominio, invece di rallentare indefinitamente il livello.

Tutti gli errori — sia quelli ritentati sia le rinunce definitive — finiscono in **`data/input/{ISO}/crawl_report.md`**: un riepilogo in Markdown (pensato per essere aperto e letto, a differenza del checkpoint/progress JSON) con pagine analizzate, breakdown per seed, domini attualmente in pausa/in coda per essere ritentati, ed elenco completo degli errori con URL/tipo/messaggio/esito. Aggiornato a ogni livello, a ogni interruzione, e a fine crawl — non viene mai cancellato.

**Riprendere un crawl interrotto (`--resume`)**: un crawl su 21 portali può richiedere 30-40 minuti (vedi punto 2 sopra); interromperlo (Ctrl+C, chiusura del terminale, un errore imprevisto — ormai raro, vedi sopra) a metà, senza un checkpoint, farebbe perdere tutto il lavoro fatto fino a quel momento. `SiteCrawlFetcher` salva quindi lo stato in `data/input/{ISO}/crawl_checkpoint.json` dopo OGNI singola pagina scaricata (anche a metà di una sequenza `follow_pagination` lunga, e anche mentre un dominio è in pausa per l'alternanza anti-blocco). Basta rilanciare lo stesso comando con `python main.py --country BE --resume`: il crawl riprende dalla pagina successiva a quella in corso al momento dell'interruzione — nel caso peggiore si riprocessa al più quell'unica pagina, MAI un intero livello o un intero seed da capo, perché ogni pagina già scaricata resta marcata come "visitata" indipendentemente da quale seed l'ha originata. Senza `--resume`, un checkpoint trovato viene segnalato nei log e sovrascritto (si riparte da zero, comportamento predefinito). Se cambi seed/parametri del crawl tra un run e l'altro, il checkpoint non corrisponde più e viene ignorato con un avviso invece di riprendere alla cieca. **Il checkpoint non viene mai cancellato automaticamente**, nemmeno a crawl completato: resta su disco come traccia permanente dell'ultimo run (marcato `"completed": true`). Se rilanci con `--resume` un crawl già completato con le stesse identiche impostazioni, l'output viene rigenerato all'istante dal checkpoint invece di ripetere tutto il crawl da capo; senza `--resume` si comporta come sempre (ignorato, nuova scansione da zero). Per forzare una pulizia manuale basta cancellare `crawl_checkpoint.json`.

### 🇧🇬 BG — Bulgaria (status: todo)
1. Apri `iisda.government.bg` nel browser e trova la pagina con l'elenco enti.
2. Apri DevTools (F12) → tab "Elements", passa il mouse su una riga-ente e copia il selettore CSS del contenitore; fai lo stesso per nome e link del sito.
3. Incolla i selettori in `list_selector` e `field_selectors` di `BG.yaml`; aggiorna `source_url`.
4. Metti `status: ready`.
5. Esegui: `python main.py --country BG -v` e controlla che `data/output/BG/siti.csv` non sia vuoto.

### 🇭🇷 HR — Croazia
1. Apri `data.gov.hr`, dataset "Popis tijela javne vlasti"; copia il link diretto al CSV (non la pagina del dataset).
2. Incollalo in `source_url` di `HR.yaml`; apri il CSV e verifica i nomi colonna esatti (es. `Županija` con dieresi).
3. Esegui: `python main.py --country HR -v`

### 🇨🇾 CY — Cipro (status: todo)
1. Apri `gov.cy` (versione EN o EL) e trova la pagina indice di ministeri/dipartimenti/enti.
2. Con DevTools copia i selettori CSS per riga-ente e link del sito (`a::attr(href)`).
3. Aggiorna `list_selector`/`field_selectors`/`source_url` in `CY.yaml`; opzionale: incrocia con `data.gov.cy` (Datastore API) per anagrafica aggiuntiva.
4. Metti `status: ready`.
5. Esegui: `python main.py --country CY -v`

### 🇨🇿 CZ — Repubblica Ceca
1. Apri il portale Czech POINT/DIA e trova il file indice XML del SOVM (Seznam orgánů veřejné moci).
2. Incolla il link in `source_url` di `CZ.yaml`; apri il file e controlla i tag XML reali (`record_xpath`, `field_xpaths`).
3. Se il campo `web` non è nell'indice ma solo nei file di dettaglio per IČO, valuta di trasformare in un `custom_module` (vedi §4) che scarica anche i dettagli.
4. Esegui: `python main.py --country CZ -v`

### 🇩🇰 DK — Danimarca
1. Consulta la documentazione di DataCVR/Datafordeleren (`datacvr.virk.dk`) e verifica se serve una registrazione per l'accesso API.
2. Aggiorna `source_url`/eventuali header di autenticazione in `DK.yaml` (se serve una API key, aggiungila come header nel costruttore `HttpSession` in `src/base_fetcher.py` o come parametro nel config).
3. Filtra i risultati per forma giuridica pubblica; verifica il path JSON reale in `records_path`/`field_mapping`.
4. Esegui: `python main.py --country DK -v`

### 🇪🇪 EE — Estonia (status: todo)
1. Verifica se RIHA (`riha.eesti.ee`) è ancora attivo o se è già stato migrato al nuovo Data Portal RIA.
2. Aggiorna `source_url` e `records_path` in `EE.yaml` di conseguenza.
3. Metti `status: ready`.
4. Esegui: `python main.py --country EE -v`

### 🇫🇮 FI — Finlandia
1. Apri la documentazione API di Suomi.fi-palvelutietovaranto (PTV) e verifica la versione corrente dell'endpoint (`/api/v11/...` potrebbe essere cambiato).
2. Verifica la struttura reale della risposta JSON (in particolare dove si trovano i canali web) e adatta `field_mapping` in `FI.yaml` (es. `webPages.0.url`).
3. Esegui: `python main.py --country FI -v`

### 🇫🇷 FR — Francia (bug risolto, formato reale: archivio tar.bz2)
1. **Non serve fare nulla di manuale**: il fetcher scarica l'URL "stable" ufficiale, che fa un redirect verso un archivio `tar.bz2` (~350 MB), lo tiene in `data/input/FR/annuaire_local.tar.bz2`, ed estrae **in memoria** solo il file utile (senza scompattare su disco la cartella con un file per comune, che non contiene link).
2. Ogni ente può avere **più link** (sito principale + es. un link di prenotazione appuntamenti): il tool produce **una riga di output per ogni link**, con lo stesso ente ripetuto e il campo `tipo_link` che riporta la classificazione originale della fonte (es. "Prise de rendez-vous en ligne") o "sito ufficiale" quando non specificata. Gli enti senza alcun link producono comunque una riga con `hostname` vuoto.
3. Esegui: `python main.py --country FR -v`. Il download di ~350 MB può richiedere qualche minuto; l'estrazione/parsing è quasi istantanea perché legge un solo file dall'archivio.
4. **Se il download automatico dovesse smettere di funzionare** (es. firewall aziendale che blocca uno dei due domini), scarica tu stesso il file: apri `https://www.data.gouv.fr/datasets/service-public-gouv-fr-annuaire-de-ladministration-base-de-donnees-locales`, nella sezione "Fichiers" clicca "Télécharger" sul file **json** (in realtà è il tar.bz2, il browser lo salverà come `all_latest.tar.bz2`), rinominalo/mettilo in `data/input/FR/annuaire_local.tar.bz2`, e rilancia il comando: il fetcher lo troverà già pronto e salterà il download.
5. Per le scuole, ripeti con l'Annuaire de l'Éducation Nationale (dataset separato su data.gouv.fr) — verificane però il formato reale con lo stesso metodo (log dettagliati con `-v`) prima di assumere che sia identico a questo.

**Cos'era andato storto (in ordine)**: (a) l'URL di ricerca-catalogo non è l'annuario; (b) l'URL "stable" corretto non serve JSON direttamente ma fa un redirect verso un archivio tar.bz2, con struttura interna a più livelli (chiave `"service"`, link multipli per ente) diversa da quella ipotizzata inizialmente. Il fetcher ora gestisce automaticamente tar.bz2 → JSON con fallback a JSON/NDJSON diretto se la fonte cambia di nuovo formato in futuro.

### 🇩🇪 DE — Germania (nuovo: crawler a livelli)
1. **Verifica preliminare IMPORTANTE**: `service.bund.de` ha risposto con un blocco robots.txt a un tentativo di accesso automatico (verificato il 12/09/2026). Prima di eseguire il crawler su larga scala, apri tu stesso `https://www.service.bund.de/robots.txt` nel browser per leggere le regole aggiornate, e valuta se procedere è compatibile con i termini d'uso del sito (eventualmente contattando il gestore per un accesso concordato). Dal 24/09/2026 il crawler applica robots.txt di default (`respect_robots: true`): con questo valore il crawl di `service.bund.de` verrà bloccato in partenza. Le alternative sono (a) usare l'Anschriftenverzeichnis ufficiale (nota in `DE.yaml`), (b) chiedere un accesso concordato al gestore, (c) impostare esplicitamente `respect_robots: false` in `DE.yaml` — scelta e responsabilità di chi esegue il crawl.
2. Apri le due pagine seed nel browser ed esegui una ricerca vuota/di default: verifica se l'URL della pagina **risultati** (es. con suffisso "Trefferliste" o parametri di query) è diverso dall'URL del modulo "Formular.html". Se sì, sostituisci `seed_urls` in `DE.yaml` con l'URL dei risultati (altrimenti il crawler potrebbe trovare solo link di navigazione del sito, non i singoli annunci).
3. Esegui prima un test contenuto: abbassa temporaneamente `max_pages_per_level` (es. a 10) e lancia `python main.py --country DE -v`; controlla in `data/output/DE/siti.csv` se la colonna `hostname` si popola con URL esterni plausibili (siti di comuni/enti) e non solo rumore.
4. Se i risultati sono buoni, alza `max_pages_per_level` per una copertura più ampia (attenzione ai tempi: con `sleep_seconds: 1.0` il crawl è volutamente lento per cortesia verso il server).
5. **Alternativa più semplice per il perimetro federale (Bund)**: se ti basta l'elenco degli enti federali (non Länder/Kommunen), valuta l'"Anschriftenverzeichnis des Bundes" — un export ufficiale XLSX/CSV/JSON aggiornato mensilmente, citato in `DE.yaml` → `notes`, che evita del tutto lo scraping.

### 🇪🇸 ES — Spagna (nuovo: import multi-file + ricerca sito ufficiale)
1. Copia in `data/input/ES/` **tutti** i file `.xlsx` DIR3 che vuoi importare, con la stessa struttura del file di esempio (foglio "Unidades INST V+T", intestazioni in riga 2, dati da riga 3). Puoi metterne quanti ne vuoi: il tool li legge e li unisce tutti, deduplicando per codice unità (colonna B).
2. Il nome dell'ente (colonna C) viene letto in automatico nel campo `nome_ente`; il tipo ente (colonna E) nel campo `sector` (vedi `ES.yaml` → `notes` per la tabella dei codici: AY=Ayuntamiento, CA=Comunidad Autónoma, MN=Ministerio, UN=Universidad, ecc.).
3. Esegui: `python main.py --country ES -v`. Al termine dell'import, se `website_search.enabled: true` (default), il tool cerca in automatico il sito ufficiale di ogni ente a partire dal nome (vedi §7 più sotto per come funziona e come tararlo).
4. Il primo run è limitato a `max_queries: 50` ricerche (per verificare la qualità prima di lanciarne migliaia): controlla `data/output/ES/siti.csv`, e se i risultati in `hostname` sono buoni, alza `max_queries` in `ES.yaml` (o rilancia solo la ricerca con `enrich_websites.py`, vedi §7) per completare gli enti restanti.
5. Nota: questo file DIR3 non contiene comune/provincia. Se ti serve, aggiungi separatamente il catalogo "Localidades" DIR3 (verificane prima lo schema: potrebbe non coincidere con quello delle "Unidades" e richiedere un fetcher dedicato).


### 🇬🇷 EL — Grecia (status: todo)
1. Apri `apografi.gov.gr` e individua l'export Excel del registro enti (versione EL o EN).
2. Aggiorna `source_url` in `EL.yaml`; apri il file e completa `field_mapping` con i nomi colonna reali.
3. In alternativa esplora `data.gov.gr` per un dataset più diretto con campo sito web.
4. Metti `status: ready`.
5. Esegui: `python main.py --country EL -v`

### 🇭🇺 HU — Ungheria (status: todo)
1. Apri `ksh.hu` e cerca l'export del registro statistico organizzazioni (GSZR) e i dati territoriali dei comuni.
2. Aggiorna `source_url` in `HU.yaml`; completa `field_mapping` in base ai nomi colonna reali.
3. Metti `status: ready`.
4. Esegui: `python main.py --country HU -v`

### 🇮🇪 IE — Irlanda
1. Apri il Register of Public Sector Bodies del CSO (`cso.ie`) e copia il link diretto al file XLS.
2. Incollalo in `source_url` di `IE.yaml`.
3. Per gli URL dei siti, valuta di aggiungere un secondo fetch `html_scrape` su `gov.ie` (elenco dei 31 local authorities + agenzie) e unire i due dataset per nome ente.
4. Esegui: `python main.py --country IE -v`

### 🇱🇹 LT — Lituania
1. Apri `data.gov.lt`, dataset JAR (Juridinių asmenų registras); verifica se preferisci l'API "Saugyklos" o direttamente un export CSV/JSONL (più semplice).
2. Se scegli il CSV, cambia `source_type` in `bulk_csv` e usa `BulkCSVFetcher` (aggiorna anche `custom_module`/rimuovilo se presente).
3. Filtra per settore istituzionale pubblico; aggiorna `field_mapping` in `LT.yaml`.
4. Esegui: `python main.py --country LT -v`

### 🇱🇺 LU — Lussemburgo (status: todo)
1. Apri `annuaire.public.lu` (versione FR) e individua come è strutturata la navigazione (per categoria/lettera).
2. Con DevTools copia i selettori CSS di riga-ente e link; aggiorna `LU.yaml` (`list_selector`, `field_selectors`, eventualmente `list_urls` con più pagine da raschiare).
3. Metti `status: ready`.
4. Esegui: `python main.py --country LU -v`

### 🇱🇻 LV — Lettonia (status: todo)
1. Apri `data.gov.lv` (portale CKAN) e cerca, con l'API `package_search`, il dataset con l'elenco enti pubblici (eventualmente collegato a UR - Uznemumu registrs).
2. Aggiorna `source_url`/`records_path` in `LV.yaml` in base al dataset trovato.
3. Metti `status: ready`.
4. Esegui: `python main.py --country LV -v`

### 🇲🇹 MT — Malta (status: todo)
1. Apri `gov.mt` e trova la pagina con l'elenco dei 68 local councils (o `servizz.gov.mt`).
2. Con DevTools copia i selettori CSS di riga-ente e link del sito; aggiorna `MT.yaml`.
3. Metti `status: ready`.
4. Esegui: `python main.py --country MT -v`

### 🇳🇱 NL — Paesi Bassi
1. Nessuna modifica di solito necessaria: `source_url` punta già all'export giornaliero `exportOO.xml`.
2. Consigliato: scarica una volta il file a mano, aprilo con un editor di testo e controlla che i tag XML citati in `src/fetchers/nl.py` (`_first_text`, candidati come `naam`, `internetadres`, `plaats`) corrispondano alla struttura reale — la funzione è già tollerante a variazioni di namespace, ma i nomi dei tag possono cambiare.
3. Esegui: `python main.py --country NL -v`

### 🇵🇱 PL — Polonia (status: todo)
1. Registrati via email sul servizio web TERYT ws1 (`eteryt.stat.gov.pl`) per ottenere le credenziali.
2. Il servizio è probabilmente SOAP/XML e non REST puro: valuta di scrivere un `custom_module` dedicato (`src/fetchers/pl.py`) invece di riusare `ApiJsonFetcher`.
3. Per gli URL, prevedi un secondo step di incrocio con i BIP (Biuletyn Informacji Publicznej) di ogni ente o con `dane.gov.pl`.
4. Metti `status: ready` solo a implementazione completata.
5. Esegui: `python main.py --country PL -v`

### 🇵🇹 PT — Portogallo
1. Apri `dados.gov.pt` e cerca lo slug esatto del dataset "Lista de entidades que integram as Administrações Públicas" (INE, settore S.13); verifica se è disponibile come CSV/API o solo PDF/Excel.
2. Aggiorna `source_url`/`records_path` in `PT.yaml` di conseguenza (se è solo PDF, valuta `bulk_xlsx` se esiste una versione Excel, o un fetch manuale).
3. Esegui: `python main.py --country PT -v`

### 🇷🇴 RO — Romania
1. Apri `data.gov.ro`, dataset "Date de contact instituții publice"; copia il link diretto al file XLSX.
2. Incollalo in `source_url` di `RO.yaml`; apri il file e verifica i nomi colonna reali (in particolare la colonna sito web, se presente).
3. Esegui: `python main.py --country RO -v`

### 🇸🇰 SK — Slovacchia
1. Apri la documentazione dell'API REST RPO (`rpo.statistics.sk`) e verifica endpoint/parametri di ricerca e paginazione esatti.
2. Filtra per "orgány verejnej moci" (organi del potere pubblico); aggiorna `SK.yaml` se i nomi parametro differiscono.
3. Esegui: `python main.py --country SK -v`

### 🇸🇮 SI — Slovenia
1. Apri `podatki.gov.si`, dataset RPU/iRPU (Register proračunskih uporabnikov); copia il link diretto al file CSV/testo.
2. Incollalo in `source_url` di `SI.yaml`; verifica i nomi colonna reali (in sloveno).
3. Esegui: `python main.py --country SI -v`

### 🇸🇪 SE — Svezia
1. Apri `myndighetsregistret.scb.se` e trova il link di export (CSV/Excel/open data API) del Myndighetsregistret.
2. Incollalo in `source_url` di `SE.yaml`; verifica i nomi colonna reali (`webbadress`, `organisationsnummer`).
3. Ricorda: copre solo le 449 myndigheter statali, non comuni/regioni (serve una fonte SKR/SCB separata per quelli).
4. Esegui: `python main.py --country SE -v`

## 6. Attivare il crawler a livelli su qualunque paese

La strategia usata per 🇩🇪 DE e 🇧🇪 BE (`source_type: site_crawl`, vedi la
tabella in §4) è **generale**: puoi attivarla su un paese qualsiasi, e per
cambiare quali siti vengono "investigati" non serve toccare né lo YAML né
il codice — basta modificare un file di testo nella cartella di input del
paese.

**Come attivarla su un nuovo paese (es. `XX`):**

1. In `config/countries/XX.yaml`, imposta:
   ```yaml
   source_type: site_crawl
   seeds_file: "seeds.txt"       # opzionale, è già il default
   allowed_domains: ["dominio1.xx", "dominio2.xx"]   # domini "di casa" da continuare a esplorare (match per suffisso)
   crawl_depth: 2
   max_pages_per_level: 100
   sleep_seconds: 1.0
   record_internal_links: true    # true = registra anche i link tra domini "di casa"; false = solo le scoperte esterne (comportamento stile Germania)
   exclude_domains: ["facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com", "youtube.com", "tiktok.com"]
   field_mapping:
     hostname: "external_url"
     tipo_link: "scope"           # "seed"/"interno"/"esterno"/"redirect"
     dominio_radice: "external_domain"
     host_osservati: "observed_hosts"   # FQDN realmente linkati, separati da '|'
     found_on: "found_on"
     source_seed: "source_seed"
     tls_error: "tls_error"
   dedupe_by: hostname
   # opzionali (valori di default): workers: 8, sleep_seconds (per host), respect_robots: true,
   # identify_crawler: true — vedi GUIDA_PASSO_PASSO.md §7.6
   ```
2. Crea il file `data/input/XX/seeds.txt` con un URL per riga (righe vuote o che iniziano con `#` vengono ignorate):
   ```
   # Semi per il crawl di XX
   https://www.sito-ufficiale-1.xx
   https://www.sito-ufficiale-2.xx
   ```
3. Esegui: `python main.py --country XX -v`

**Per cambiare/aggiornare la lista dei siti da esplorare in futuro** (per XX
o per DE/BE già pronti): apri semplicemente `data/input/{ISO}/seeds.txt`,
aggiungi/rimuovi righe, e rilancia il comando — non serve toccare altro.
Se il file non esiste (o è vuoto), il fetcher usa come riserva l'eventuale
`seed_urls` nello YAML; se non c'è nessuno dei due, si ferma con un
messaggio che dice esattamente quale file creare.

**Output = lista di domini radice, senza duplicati, semi inclusi.** Il
fetcher non produce una riga per ogni URL/pagina scoperta, ma UNA RIGA PER
OGNI DOMINIO RADICE UNICO (eTLD+1, calcolato con la libreria `tldextract` —
gestisce correttamente casi come "finances.belgium.be" → "belgium.be" o
domini con suffissi multi-livello come ".co.uk"). Se lo stesso dominio
viene raggiunto tramite più pagine/URL diversi durante il crawl, compare
comunque **una sola volta** in output (si tiene la prima occorrenza). I
domini radice dei semi stessi (`seed_urls`/`seeds_file`) vengono aggiunti
per primi, con `tipo_link = "seed"`, così finiscono in output anche se il
crawl non scoprisse nulla di nuovo.

**Le due modalità disponibili** (impostate da `record_internal_links`):
- **`false`** (stile 🇩🇪 Germania): i semi sono un portale/bacheca che si
  attraversa solo per arrivare a siti esterni; in output finiscono i domini
  dei semi (`tipo_link="seed"`) e SOLO le scoperte esterne ai domini "di
  casa" (`tipo_link="esterno"`).
- **`true`** (stile 🇧🇪 Belgio): i semi sono già i siti di interesse; in
  output finiscono i domini dei semi, PIÙ quelli "interni" scoperti durante
  il crawl (altri sotto-domini/pagine dello stesso network istituzionale,
  utili per trovare enti specifici non elencati esplicitamente nei semi)
  PIÙ quelli "esterni", distinti dal campo `tipo_link`.

## 7. Ricerca automatica del sito ufficiale (da nome ente)

Quando una fonte non contiene l'URL del sito (es. Spagna/DIR3, ma la
funzione è generica e riusabile per qualunque paese), il tool può cercarlo
in automatico a partire dal campo `nome_ente`, usando il modulo
`src/website_finder.py`.

**Come attivarla per un paese**: aggiungi al suo `config/countries/{ISO}.yaml`:

```yaml
website_search:
  enabled: true
  backend: duckduckgo          # unico backend incluso di default
  query_suffix: "sitio oficial" # testo aggiunto al nome ente nella ricerca
  max_results: 5                # quanti risultati considerare per ogni ricerca
  sleep_seconds: 2.0             # pausa tra una ricerca e l'altra
  max_queries: 50                # tetto di sicurezza per il singolo run
```

Con questo attivo, `python main.py --country {ISO}` cerca automaticamente il
sito per ogni record con `nome_ente` valorizzato e `hostname` vuoto, prima
di scrivere il CSV finale.

**Per rilanciare SOLO la ricerca** su un CSV già generato (utile dopo
un'interruzione, o per aumentare `max_queries` senza rifare l'import):

```powershell
python enrich_websites.py --country ES
python enrich_websites.py --country ES --max-queries 300 --sleep 3
```

Fa un backup (`siti.csv.bak`) prima di sovrascrivere l'output.

**Limiti da conoscere prima di un uso su larga scala:**
- Il backend `duckduckgo` incluso fa scraping best-effort della pagina
  risultati HTML di DuckDuckGo (non è un'API pubblica supportata): va bene
  per decine/centinaia di ricerche con pause, non per migliaia in rapida
  sequenza — rischia di rallentare o interrompersi.
- Il filtro anti-rumore (`is_plausible_official` in `src/website_finder.py`)
  scarta i domini più ovvi (social, Wikipedia, portali di lavoro/elenchi
  commerciali) ma è euristico: **verifica a campione** i risultati prima di
  considerarli definitivi, specialmente per nomi di enti molto generici o
  comuni ad altri soggetti (es. una piccola "Secretaría General" interna).
- Per un uso intensivo o professionale, sostituisci il backend con un vero
  servizio di ricerca (Bing Web Search API, Google Programmable Search
  Engine, SerpApi, ecc.): basta scrivere una funzione con la stessa firma di
  `search_duckduckgo(query, session, max_results)` e registrarla nel dict
  `SEARCH_BACKENDS` in `src/website_finder.py`, poi usare quel nome in
  `backend:` nel config.
- Verifica sempre i Termini di Servizio del motore di ricerca scelto prima
  di un uso sistematico/massivo.

## 8. Comando riepilogativo

Dopo aver completato i paesi che ti interessano, per rigenerare tutto insieme:

```powershell
python main.py --all --skip-todo -v
```

## 9. Note importanti

- **Rispetta i termini d'uso** di ogni sito: il crawler `site_crawl` e il
  classificatore applicano robots.txt di default (`respect_robots`); per le fonti
  `html_scrape` verifica `robots.txt` e le condizioni d'uso prima di uno scraping su
  larga scala; preferisci sempre, dove esiste, l'export/API ufficiale.
- **Rate limiting**: `base_fetcher.py` include già retry con backoff; per
  scraping intensivo aggiungere eventualmente una pausa tra richieste
  (parametro `sleep_between_requests` da implementare se serve).
- **Aggiornamento fonti**: molte fonti citate cambiano URL/formato nel
  tempo (release trimestrali/annuali). Il campo `notes` di ogni config
  riporta i caveat noti al momento della stesura (settembre 2026).
- Il tool NON copre l'Italia (IndicePA), come da richiesta.


## 10. Classificazione dei siti (natura + tipo di ente) — `classify_sites.py`

Per ogni sito di `data/output/{ISO}/siti.csv` (o del JSON del crawl) assegna:
- **natura**: `pubblico` / `privato` / `terzo_settore` (associazioni, fondazioni, ODV/APS, ONG, sindacati...) / `incerto` / `infrastruttura` (CDN, social, shortener...) / `non_classificabile`
- **tipo**: `comune`, `regione_provincia`, `governo_centrale`, `polizia_sicurezza`, `scuola`, `universita`,
  `sanita`, `giustizia`, `parlamento`, `agenzia_authority`, `emergenza`, `cultura`, `trasporti`, `ambiente`, `altro`

I due assi sono indipendenti (una scuola può essere privata). **Tutti gli stadi sono disattivati per
default**; si attivano con `--stages` o in `config_classify.yaml`:

| Stadio | Cosa fa | Costo |
|---|---|---|
| 0 | regole su hostname/suffisso (`gob.es`, `gemeente-...`, `polizei-...`; blocklist infrastrutturale) | zero rete |
| 2 | Wikidata: sito ufficiale (P856) → classi dell'entità | poche richieste in batch |
| 3 | apre la homepage (host osservati, www/apex, https→http), estrae un "pacchetto di evidenza" (title, og:site_name, description, h1, JSON-LD, menu, footer, testo senza banner cookie, eventuale pagina Impressum/Mentions légales) e lo valuta con regole pesate multilingue | 1–2 richieste per sito, in parallelo con limite per host |
| 4 | LLM locale (Ollama) sul pacchetto di evidenza, solo sui siti ancora incerti; output vincolato da JSON schema, temperatura 0 | 1 chiamata LLM per sito residuo |

```powershell
python classify_sites.py --country BE --stages 0            # solo hostname
python classify_sites.py --country BE --stages 0,2,3 --limit 200 -v   # prova su 200 siti
python classify_sites.py --country BE --stages 0,2,3,4      # con LLM (serve 'ollama serve' e il modello)
python classify_sites.py --country HR --input data/input/HR/crawl_service_bund.json --stages 0,3
```

Uscita: `data/output/{ISO}/siti_classificati.csv` = colonne originali + `host_classificato`, `natura`, `tipo`,
`confidenza_natura`, `confidenza_tipo`, `stadi`, `evidenza`, `homepage_status`, `final_url`, `lang`,
`wikidata`, `llm_model`, `da_rivedere`, `classified_at`.

- `homepage_status` dice PERCHÉ un sito non è stato classificato: `blocked`, `needs_js`, `parked`,
  `default_page`, `timeout`, `dns_error`, `tls_error`, `http_error`, `non_html`. Un sito non leggibile non
  viene mai "indovinato".
- Le evidenze e le risposte dell'LLM sono in `data/input/{ISO}/classify_cache.jsonl`: rilanciare non rifà le
  richieste, e modificando `config/classify_lexicon.yaml` le homepage vengono **rivalutate senza riscaricarle**.
- Le confidenze sono euristiche e le soglie (`stop_confidence`, `review_below_*`, `only_if_below`) vanno
  **calibrate su un gold set annotato a mano** (consigliati 400–600 siti stratificati per paese e classe).
- `respect_robots` (stadio 3) è `true` di default, come per il crawler: robots.txt e `Crawl-delay` sono rispettati; una homepage vietata ottiene `homepage_status: robots`.
