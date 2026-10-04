# Mappatura domini, gap analysis e piano di implementazione — per README.md / proposta Restack

Questo documento serve da materiale grezzo per il `README.md` che accompagnerà
la proposta al fondo **Restack** (NLnet / Open Internet Stack, prima call:
apertura 3 settembre 2026, scadenza 3 novembre 2026 12:00 CET). Copre tre
cose, nell'ordine in cui te le sei chieste:

1. mappatura di ogni dominio di misurazione di `dst_rev3`, con l'angolo
   "perché interessa a Restack";
2. cosa è misurabile ma non ancora misurato, nel codice attuale;
3. un piano di implementazione diviso per costo (gratis in post-processing
   / quasi gratis durante il crawl riusando la stessa richiesta / costo
   aggiuntivo reale).

---

## 1. Mappatura dei domini di misurazione (dst_rev3)

| Dominio misurato | Cosa produce | Dove nel codice | Perché rilevante per Restack |
|---|---|---|---|
| **DNS base** (NS, MX, A/AAAA, DNSSEC, CAA, SPF, DMARC) | Chi gestisce DNS/mail di un ente, protezioni di base attive | `02_probe_infra.py` | Base di ogni analisi di concentrazione: Restack cita esplicitamente la dipendenza da "un limitato numero di attori" come il problema da risolvere. |
| **ASN/operatore hosting** (via RDAP, poi bulk via Team Cymru) | Azienda e Paese che ospita DNS/mail/sito | `02_probe_infra.py`, `asn_bulk.py` | Permette di quantificare — non solo affermare — il vendor lock-in infrastrutturale della PA. |
| **Concentrazione (HHI)** | Indice 0–10.000 di concentrazione per ASN, soglie stile DOJ/FTC, vista "per ente" e "per hostname unico" | `03_analyze.py` | È la metrica di sintesi che trasforma "la PA dipende da poche aziende" in un numero verificabile e ripetibile nel tempo — esattamente il tipo di "misura oggettiva" che una due diligence di sicurezza/audit richiede. |
| **TLS base + avanzato** | Emittente certificato, versione/scadenza, protocolli deboli ancora attivi, cipher, HSTS, voto sintetico A–D | `02_probe_infra.py` | Sicurezza della supply chain di trasporto; un voto aggregabile per operatore mostra se un singolo fornitore sistematicamente sotto-protegge i suoi clienti PA. |
| **HTTP headers / CDN** | Header Server/Via, CDN rilevata, tempo di risposta | `02_probe_infra.py` | Prima traccia di dipendenza da CDN extra-UE (spesso invisibile finché non cade). |
| **Dark stack (terze parti)** | Domini terzi (CDN/font, analytics, captcha, chat, video, SDK) caricati dall'HTML statico, con Paese/SEE | `05_scrape_dark_stack.py` | È la sezione che dà il nome al progetto e combacia lessicalmente con il linguaggio del bando ("dark stack components only available as volatile proprietary services"). |
| **Cookie tracker** | Cookie tecnici vs profilazione, per nome | `05_scrape_dark_stack.py` | Segnale di dipendenza da tracker di terze parti extra-UE, riusa la stessa richiesta HTTP del dark stack (gratis). |
| **CMS/software fingerprint + licenza** | Software riconosciuto (CMS/framework/server), licenza se open source | `06_fingerprint_cms.py`, `data/rules/cms_signatures.yaml` | Censimento "software supply chain" della PA: quanta PA italiana gira già su stack open (WordPress, ecc.) vs proprietario — utile per argomentare l'impatto di un investimento open-source. |
| **Ridondanza effettiva (NS/endpoint)** | Interroga OGNI nameserver/indirizzo pubblicato separatamente, non solo il resolver di sistema | `07_resilience.py` | Misura la resilienza REALE, non dichiarata: "se cade un nameserver, gli altri rispondono davvero?" — punto centrale per "operational availability" citato dal bando. |
| **Classificazione tipo ente** (da testo homepage, 24 lingue UE) | Comune/scuola/ASL/ministero/... uniforme in tutti i Paesi UE | `08_classifica_tipo_ente.py`, `data/rules/tipo_ente_rules.yaml` | Rende il dataset comparabile a livello europeo, non solo italiano — coerente con la "dimensione europea" richiesta da Restack come criterio di eleggibilità. |
| **Confronto fra Paesi** | Matrice tipo_ente × Paese, tempi/pesi pagina medi, quota di enti non pubblici | `09_confronto_paesi.py` | Prova concreta di scalabilità multi-Paese del metodo — rilevante per il criterio "Relevance/Impact/Strategic potential" (40% del punteggio). |
| **AS Hegemony (sperimentale)** | AS di transito da cui più hostname dipendono, via API IHR | `as_hegemony.py` | Dipendenza non solo da CHI ospita, ma da CHI sta "in mezzo" nel routing — argomento forte per "core internet infrastructure" nel testo del bando. |
| **eu_pa_scraper** (progetto gemello) | Elenco siti PA per i 26 Paesi UE (Italia esclusa, coperta da IndicePA), con crawl a livelli e checkpoint per resume | repo separato | Fornisce la lista di hostname su cui `dst_rev3` può essere eseguito fuori dall'Italia — è il pezzo che rende l'intero impianto "a dimensione UE" invece che solo italiano. |

---

## 2. Cosa è misurabile ma non ancora misurato

Elenco onesto — ordinato dal più semplice/economico al più costoso — di
segnali che i dati **già raccolti** permetterebbero di calcolare, o che
richiederebbero una modifica minima allo scraping già in corso.

### 2.1 Gratis in post-processing (nessun nuovo dato, solo nuova lettura di quello che c'è già)

- **Concentrazione (HHI) del *dark stack*, non solo di hosting/DNS/mail.**
  `03_analyze.py` calcola già l'HHI per ASN su hosting/mail/NS. Lo stesso
  identico calcolo, applicato a `dark_stack.csv` (colonna
  `organizzazione_madre`), risponde a "quanta PA italiana dipende da **una
  singola big tech** per font/analytics/captcha?" — probabilmente il numero
  più citabile di tutto il progetto, e non richiede una riga di scraping in
  più: i dati sono già in `data/results/*/dark_stack.csv`.
- **Distribuzione geografica pesata degli emittenti di certificati TLS**
  (Let's Encrypt vs DigiCert vs Sectigo...): `tls_base` già registra
  l'emittente per ogni hostname; oggi non se ne calcola la concentrazione.
- **Copertura IPv6 reale**: gli indirizzi AAAA sono già risolti da
  `02_probe_infra.py`; manca solo un conteggio "quota di enti dual-stack"
  nel report.
- **`minaccia_confermata_da_umano` persistente anche per `dst_rev3`.**
  `terze_parti_da_verificare.csv/.md` (in `05_scrape_dark_stack.py`) ha
  già l'elenco ordinato per rarità con tutte le pagine in cui compare un
  dominio — esattamente il formato che hai chiesto per `eu_pa_scraper` —
  ma **non porta avanti un flag di revisione umana** fra un run e l'altro:
  oggi viene rigenerato da zero leggendo l'intero file di dettaglio. Vedi
  §3 per la patch (la stessa logica di merge già scritta per
  `eu_pa_scraper/tools/postprocess_checkpoints.py`).

### 2.2 Quasi gratis durante il crawl (stessa richiesta HTTP già fatta, si estrae solo qualcosa in più da risposta/HTML già in memoria)

- **Distinzione per TIPO di tag HTML del dominio di terza parte** — è
  esattamente il punto che hai sollevato con gli esempi Caso A/B/C/D.
  `extract_third_party_resources()` in `05_scrape_dark_stack.py` itera già
  `TAG_ATTR = [("script","src"), ("link","href"), ("iframe","src"),
  ("img","src")]` ma **scarta il nome del tag** e tiene solo `(host, url)`.
  Basta portare `tag_name` fino all'output (stessa idea implementata oggi
  per `eu_pa_scraper`, vedi §3) per sapere se un dominio sospetto è un
  semplice link, uno script eseguibile, un iframe embedded o un'immagine.
- **Subresource Integrity (SRI)**: se un `<script src>`/`<link href>` di
  terza parte ha l'attributo `integrity`, il browser verifica che la
  risorsa non sia stata alterata dal fornitore terzo — l'assenza di SRI su
  uno script eseguibile di terza parte è un segnale di rischio supply-chain
  concreto (proprio il tema "software supply chain management" citato dal
  bando). Si legge dallo stesso tag già estratto, zero costo aggiuntivo.
- **Mixed content**: pagina servita in HTTPS che carica risorse in HTTP
  semplice. Si verifica confrontando lo schema di `pagina_finale` (già
  disponibile) con lo schema di ogni `url_risorsa_trovata` (già estratto).
- **Header di sicurezza HTTP oltre Server/Via**: Content-Security-Policy,
  X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
  Permissions-Policy. La risposta HTTP è già scaricata per
  `http_headers_cdn`/dark stack; oggi si guardano solo due header.
- **Forzatura HTTPS**: se la richiesta a `http://hostname/` fa redirect a
  `https://` (già osservabile da `fetch_html()`, che prova prima https poi
  http).

### 2.3 Costo aggiuntivo reale (nuove richieste, da valutare rispetto al budget di tempo/rate-limit)

- **Concentrazione dei registrar di dominio** (via RDAP sul dominio, non
  sull'IP): richiede una query RDAP in più per hostname — stesso genere di
  rate limit già gestito per l'ASN, ma è un round-trip aggiuntivo.
- **DKIM**: a differenza di SPF/DMARC (un solo record DNS noto), DKIM vive
  su un selettore che va indovinato (`google._domainkey`, `selector1`,
  ecc.) — richiede N query DNS aggiuntive per hostname con esito incerto.
- **AS Hegemony su tutto il campione** (`as_hegemony.py` esiste già ma è
  sperimentale/disattivo di default): attivarlo su scala richiede rispettare
  il rate limit di IHR, quindi tempo aggiuntivo non trascurabile.

---

## 3. Piano di implementazione

### Fase 0 — fatta in questa sessione (eu_pa_scraper)

- `src/fetchers/generic.py`: nuova estrazione additiva
  `_extract_resource_refs()` (script/iframe/img/link/link_risorsa con tipo
  di tag), accumulata in un nuovo campo del checkpoint `resource_index`
  (dominio → tutte le occorrenze, non solo la prima). Zero costo di rete:
  riusa l'HTML già scaricato per `_extract_links()`. Retrocompatibile con
  `--resume` su checkpoint di versioni precedenti (che semplicemente non
  hanno ancora `resource_index`).
- `tools/postprocess_checkpoints.py` + `config_postprocess.yaml`: digerisce
  i checkpoint di tutti i Paesi e produce `report_generale.md/json`,
  `tutte_le_terze_parti.csv` e `link_sospetti.json/csv/md` nel formato
  richiesto, con **persistenza garantita** di `minaccia_confermata_da_umano`
  fra un run e l'altro (merge, mai reset). Vedi il messaggio precedente per
  il dettaglio e i test effettuati.

### Fase 1 — stessa patch, applicata a dst_rev3 (stimata: piccola)

1. In `05_scrape_dark_stack.py`, `extract_third_party_resources()`: portare
   `tag_name` nell'output (oggi scartato), aggiungere colonna
   `tipo_riferimento` a `FIELDS_DETTAGLIO` e al Markdown di
   `terze_parti_da_verificare`.
2. Stessa funzione: aggiungere il controllo SRI (`tag.get("integrity")`) e
   mixed-content (confronto schema pagina/risorsa), colonne aggiuntive nello
   stesso CSV di dettaglio — nessuna nuova richiesta HTTP.
3. Aggiungere `minaccia_confermata_da_umano` persistente a
   `scrivi_terze_parti_da_verificare()`, riusando la stessa logica di merge
   già scritta in `postprocess_checkpoints.py` (leggere il CSV/MD
   precedente, riportare avanti il flag per dominio già noto).
4. In `03_analyze.py`: aggiungere `summarize_tracker_hhi()` applicato a
   `dark_stack.csv` per organizzazione madre (la funzione HHI esiste già,
   serve solo un secondo richiamo su un'altra colonna/dataset).

### Fase 2 — segnali "quasi gratis" restanti (stimata: piccola-media)

- Header di sicurezza HTTP aggiuntivi, forzatura HTTPS, copertura IPv6,
  concentrazione emittenti TLS: tutti calcolabili da dati già raccolti o
  con un'estrazione aggiuntiva sulla stessa risposta HTTP/TLS già ottenuta.

### Fase 3 — segnali a costo aggiuntivo reale (da valutare per budget/tempi)

- Registrar concentration (RDAP dominio), DKIM, attivazione su scala di
  `as_hegemony.py`. Da inserire nella proposta Restack come "estensioni
  pianificate" più che come lavoro già pronto: coerente con il fatto che il
  bando premia la roadmap tecnica (30% "Technical excellence/feasibility"),
  non solo lo stato attuale.

---

## Nota per il README.md della proposta

Il linguaggio del bando ("dark stack", "cascading dependencies and hidden
liabilities", "software supply chain management", "reduce the risk of
future compromise") combacia quasi parola per parola con la sezione 05 di
`dst_rev3` — vale la pena citarlo esplicitamente nella proposta, insieme
alla metrica HHI (§1) come prova che il progetto già trasforma "la PA è
fragile" in numeri verificabili e ripetibili, il tipo di evidenza che la
fase di revisione di Restack esplicitamente richiede ("can you back up or
validate claim Y").
