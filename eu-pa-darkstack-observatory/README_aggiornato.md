# Osservatorio concentrazione/resilienza infrastruttura pubblica italiana

> **English overview.** This is the updated, most complete README of the measurement pipeline. It is a superset of `README.md`. The extra sections are:
> - *Terza passata*: integration of externally proposed extensions. It adds `07_resilience.py` (observable redundancy across nameservers and endpoints) and the technology-dependency index in `03_analyze.py`. A proposed vulnerability-lookup script was reviewed and deliberately **not** integrated.
> - *Quarta passata*: entity-type classification in 24 EU languages (`08_classifica_tipo_ente.py`) and the cross-country comparison (`09_confronto_paesi.py`).
> - *Verifica manuale di link sospetti*: manual verification of suspicious third-party links (`terze_parti_da_verificare.*`).
> - *Protezione dei checkpoint*: checkpoint protection, with automatic timestamped backups before any overwrite.
> - *Limiti noti*: the **known limitations** section, quoted in the Restack application.
>
> The [glossary](../GLOSSARY.md) translates file and column names.

Kit di partenza per misurare da chi dipende, tecnicamente, il sito di un
ente pubblico italiano (comuni, ASL, scuole, università, ecc. — tutti gli
enti di IndicePA, non solo i comuni) — quali aziende gestiscono il suo DNS,
la sua posta, dove sono ospitati i suoi server — e calcolare quanto quella
dipendenza sia concentrata su pochi operatori.

**Prima di leggere il codice**, leggi `docs/IDEA_E_OUTPUT.md`: spiega, in
linguaggio semplice, cosa fa il progetto e cosa produce alla fine.

Oltre a posizione/backup dell'infrastruttura, il progetto misura anche:
tempi di risposta HTTP, robustezza TLS (protocolli deboli ancora attivi,
cipher, HSTS), cookie di profilazione, uso di CDN extra-UE, e un censimento
del software/licenze in uso. **Ogni misurazione è attivabile/disattivabile
da `config.yaml`** (vedi sezione dedicata sotto) — utile perché alcune sono
sensibilmente più lente di altre su un campione grande.

## Setup su Windows 11 + VSCode

1. **Installa Python** (3.11 o 3.12) da [python.org](https://www.python.org/downloads/) se non ce l'hai già.
   Durante l'installazione spunta "Add python.exe to PATH".

2. **Apri questa cartella in VSCode** (File → Apri cartella...).

3. **Apri il terminale integrato** (Terminale → Nuovo terminale, oppure `` Ctrl+` ``)
   e crea l'ambiente virtuale:

   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

   Se PowerShell rifiuta di eseguire lo script di attivazione con un errore
   "esecuzione script disabilitata", esegui una volta questo comando
   (permette script locali firmati, è la configurazione consigliata da
   Microsoft per lo sviluppo):

   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   ```

4. **Installa le dipendenze** (con il venv attivo, vedi `(venv)` all'inizio
   della riga del terminale):

   ```powershell
   pip install -r requirements.txt
   ```

5. **In VSCode**, apri la palette comandi (`Ctrl+Shift+P`) → "Python: Select
   Interpreter" → scegli quello dentro `venv\Scripts\python.exe`. Le
   configurazioni di debug in `.vscode/launch.json` sono già pronte:
   apri il pannello "Esegui e debug" (icona play con l'insetto, `Ctrl+Shift+D`)
   e lancia "Pipeline completa (test rapido, Lombardia, 20 host)" per un
   primo giro senza toccare il terminale.

## Configurazione (config.yaml)

Tutte le misurazioni si attivano/disattivano da **un unico file**,
`config.yaml` nella cartella principale del progetto — non serve toccare il
codice. Aprilo con un editor di testo qualunque (anche il Blocco Note, ma
VSCode evidenzia la sintassi YAML): ogni riga sotto `misurazioni:` è un
flag `true`/`false`.

```yaml
misurazioni:
  dns_base: true
  asn_hosting: true
  tls_base: true
  tls_avanzato: true
  http_headers_cdn: true
  tempi_risposta: true
  dark_stack_terze_parti: true
  cookie_tracker: true
  cms_fingerprint: true
```

Partono tutte a `true`. Guida pratica per decidere cosa conviene tenere
acceso, dal più economico al più lento (il costo è per hostname, si
moltiplica per la dimensione del campione):

| Flag | Cosa misura | Costo | Dipende da |
|---|---|---|---|
| `dns_base` | NS, MX, A/AAAA, DNSSEC, CAA, SPF, DMARC | Basso (query DNS locali) | — |
| `http_headers_cdn` | Header Server/Via, CDN rilevata | Basso (1 richiesta HTTP) | — |
| `tempi_risposta` | Tempo di risposta HTTP in ms | **Gratis** | `http_headers_cdn=true` |
| `asn_hosting` | Operatore/ASN via RDAP (DNS/mail/nameserver) | Medio (RDAP ha rate limit propri) | `dns_base=true` |
| `tls_base` | Emittente, versione, scadenza certificato | Basso (1 connessione TLS) | — |
| `dark_stack_terze_parti` | Domini terzi da HTML statico + Paese | Alto (1 richiesta HTTP + N lookup RDAP) | — |
| `cookie_tracker` | Cookie tecnici/profilazione | **Gratis** | `dark_stack_terze_parti=true` |
| `cms_fingerprint` | CMS/framework/server + licenza | Medio (1 richiesta HTTP per hostname) | — |
| `tls_avanzato` | Protocolli deboli attivi, cipher, HSTS, voto A-D | **Il più lento** (fino a 4 connessioni TLS extra) | — |

Se un giro su un campione grande è troppo lento, la prima cosa da spegnere è
`tls_avanzato`; la seconda `dark_stack_terze_parti` (che spegne anche
`cookie_tracker`, dato che ne dipende).

Il file ha anche una sezione `timeout_secondi` e `delay_secondi` per
regolare pause/timeout di default di tutti gli script insieme, invece di
passarli uno per uno da riga di comando.

**Come si usa in pratica**, passo per passo:

1. Apri `config.yaml` nella cartella principale del progetto.
2. Metti a `false` le voci che non ti servono per questo giro (es.
   `tls_avanzato: false` per un giro più veloce).
3. Salva il file.
4. Lancia la pipeline normalmente (`python src\run_pipeline.py ...`): non
   serve nessun argomento in più, gli script leggono `config.yaml` da soli.
5. I campi delle misurazioni disattivate restano vuoti nei CSV di output
   (non è un errore: è la misurazione che hai scelto di saltare) e le
   relative sezioni non compaiono in `report.json`.
6. Vuoi tornare a misurare tutto? Rimetti i flag a `true` e rilancia — puoi
   anche tenere `--resume` per completare solo gli enti mancanti, ma **nota
   bene**: `--resume` riusa le righe già scritte così come sono, quindi se
   avevi disattivato una misurazione durante quel run, le righe già scritte
   restano con quei campi vuoti anche dopo aver riattivato il flag; per
   ricalcolarle serve un run pulito (senza `--resume`, o un `--out` diverso).

`run_pipeline.py` salta del tutto lo step 05 (dark stack + cookie) se
entrambi `dark_stack_terze_parti` e `cookie_tracker` sono `false`, e lo step
06 (software/licenze) se `cms_fingerprint` è `false`: niente processi
inutili lanciati a vuoto.

Ogni script accetta comunque `--config percorso\a\un\altro\config.yaml` se
vuoi tenere configurazioni diverse per campioni diversi (es. un giro veloce
"solo DNS" e uno completo "tutto attivo") senza modificare sempre lo stesso
file.

**User-Agent** (`http_user_agent`, stessa `config.yaml`): usato da `02`, `05`
e `06` in ogni richiesta HTTP. Lasciato vuoto di default, usa uno User-Agent
che si dichiara onestamente come bot di un osservatorio di interesse
pubblico — coerente con lo scopo del progetto, ma alcuni siti dietro
CDN/WAF (Cloudflare soprattutto) rispondono 403 a uno User-Agent così. Se
preferisci un tasso di blocchi più basso, imposta un valore tipo browser
in `config.yaml`: è una scelta esplicita che fai tu, non un default
nascosto. In alternativa, lascia lo User-Agent com'è e guarda la colonna
`pagina_probabilmente_bloccata` in `dark_stack.csv`/`cms_fingerprint.csv`
per sapere quali righe potrebbero essere state bloccate invece di lette
davvero.

## Quickstart da terminale

Tutti i comandi vanno lanciati dalla cartella principale del progetto
(quella con `requirements.txt`), con il venv attivo (vedi "Setup" sopra —
prompt che inizia con `(venv)`).

### Passo 1 — un giro di prova piccolo, SEMPRE per primo

Non lanciare mai il campione nazionale come primo tentativo, nemmeno se hai
fretta: un giro piccolo conferma in 1-2 minuti che rete, config.yaml e
dipendenze funzionano, invece di scoprire un problema dopo ore.

```powershell
python src\run_pipeline.py --regione "Lombardia" --limit 20
```

Guarda l'output a schermo mentre gira:
- Righe `[N/20] chunk completato (... host/s ...)` → sta procedendo.
- Un `ATTENZIONE: il bulk Team Cymru ha risolto solo il N%...` → la tua rete
  probabilmente blocca la porta 43/TCP in uscita (capita su reti
  aziendali/scolastiche). Non è un errore grave: il run continua comunque
  via RDAP, ma più lento. Vedi "Se il bulk Cymru non funziona" sotto.
- Alla fine, la riga `Risoluzione ASN (cymru_bulk): N IP risolti via bulk...`
  ti dice quanti IP sono stati risolti in blocco vs uno alla volta: se `N
  via fallback RDAP per-IP` è quasi tutto il totale, il bulk non sta
  funzionando (stessa causa del punto sopra).
- Una riga `Risposte HTTP 403/429/503...` → alcuni siti ti hanno bloccato
  (probabile WAF che non gradisce lo User-Agent di default, vedi sotto "Se
  un sito risponde spesso 403/406/429/503").

Se questo giro va a buon fine, apri `data/results/IT/risultati.csv` e dai
un'occhiata alle righe: hanno senso i valori (ASN, TLS, HTTP)? Se sì, sei
pronto per un campione più grande.

### Passo 2 — un giro medio, per tarare i parametri

```powershell
python src\run_pipeline.py --categoria "L6,L7" --limit 500
```

(`L6` = comuni, `L7` = ASL, `L17` = università, `L33` = scuole — vedi
`Codice_Categoria` in `enti.xlsx` per l'elenco completo). Usa questo giro per decidere, guardando l'output:
- Se il bulk Cymru funziona bene (quasi 0 fallback RDAP) e non vedi molti
  403/429/503 → puoi alzare `--concorrenza` per il giro finale.
- Se vedi molti 403/429/503 concentrati su pochi operatori → abbassa
  `--concorrenza-per-provider` (default 3) prima di alzare `--concorrenza`.
- Se `tls_avanzato` (la misurazione più lenta) non ti serve subito, mettilo
  a `false` in `config.yaml` per un giro più rapido.

### Passo 3 — il campione nazionale completo

Solo dopo aver validato i passi 1 e 2:

```powershell
python src\run_pipeline.py --concorrenza 25
```

Su ~22.000 host aspettati comunque un run lungo (vedi "Tempi di esecuzione"
sotto per un ordine di grandezza) — lascialo girare in background e
controlla ogni tanto l'output. **Se si interrompe per qualunque motivo**
(chiusura terminale, sospensione del PC, errore di rete), non serve
ripartire da capo:

```powershell
python src\run_pipeline.py --concorrenza 25 --resume
```

`--resume` salta gli enti già presenti nei file di output e continua sui
restanti — usa GLI STESSI flag (`--concorrenza`, `--categoria`, ecc.) del
run originale, altrimenti alcuni comportamenti potrebbero non essere quelli
che ti aspetti (vedi nota su `--resume` in "Configurazione" sopra).

### Solo alcuni passi, o passo per passo

`run_pipeline.py` esegue 01→02→05→06→03→04 in sequenza. Se preferisci
lanciare i singoli script (per debug, o per rifare solo un passo):

```powershell
python src\01_fetch_ipa_comuni.py --regione "Lombardia"
python src\02_probe_infra.py --limit 20
python src\05_scrape_dark_stack.py --limit 20
python src\06_fingerprint_cms.py --limit 20
python src\03_analyze.py
python src\04_export_enti_esteso.py
```

Ognuno dei tre passi di rete (02, 05, 06) supporta `--resume` allo stesso
modo, indipendentemente dagli altri:

```powershell
python src\02_probe_infra.py --resume
python src\05_scrape_dark_stack.py --resume
python src\06_fingerprint_cms.py --resume
```

Tutti e tre processano ogni hostname UNA sola volta, anche se più enti
condividono lo stesso sito.

### Parametri utili per velocità/prudenza (02, e via run_pipeline.py)

| Flag | Cosa fa | Default | Quando cambiarlo |
|---|---|---|---|
| `--concorrenza N` | hostname sondati in parallelo | 15 | Alza su un campione grande e una connessione buona; abbassa se vedi errori di rete o troppi 403 |
| `--concorrenza-per-provider N` | connessioni simultanee max verso lo stesso blocco IP | 3 | Abbassa se un singolo operatore (es. un consorzio hosting comuni) mostra molti 403/429/503 |
| `--chunk-size N` | righe scritte su disco per volta | 200 | Abbassa se vuoi perdere meno lavoro a un'interruzione; alza per lookup ASN bulk più efficienti |
| `--asn-metodo {cymru_bulk,rdap}` | come risolvere ASN/operatore | cymru_bulk | Passa a `rdap` se la porta 43/TCP è bloccata dalla tua rete (vedi sotto) |
| `--skip-http` | salta TLS/HTTP, solo DNS/ASN | (attivo) | Per un giro velocissimo solo su DNS/hosting |

Questi valori si possono anche fissare una volta per tutte in `config.yaml`
(chiavi `concorrenza`, `concorrenza_per_provider`, `asn_metodo`) invece di
ripeterli ogni volta da riga di comando.

### Se il bulk Cymru non funziona

Se vedi l'avviso "il bulk Team Cymru ha risolto solo il N%..." o, a fine
run, "N via fallback RDAP per-IP" molto alto:

1. Verifica che la tua rete non blocchi la porta 43/TCP in uscita (alcune
   reti aziendali/scolastiche/VPN lo fanno; prova `Test-NetConnection
   whois.cymru.com -Port 43` in PowerShell — se fallisce, è quello).
2. Se non puoi sbloccarla, usa `--asn-metodo rdap` (o `asn_metodo: rdap` in
   config.yaml): torna al comportamento pre-ottimizzazione (più lento, un
   lookup RDAP per IP, ma funziona su qualunque rete).
3. Il run non si blocca da solo in nessuno dei due casi: il fallback è
   automatico, l'avviso è solo per farti risparmiare tempo capendo prima il
   perché di un run più lento del previsto.

## Tempi di esecuzione

Ordine di grandezza per `02_probe_infra.py` con le impostazioni di default
(`concorrenza: 15`, bulk Cymru funzionante): su un campione dove il bulk
funziona bene, l'operazione più lenta resta `tls_avanzato` (fino a 6
connessioni TLS extra per hostname — vedi tabella in "Configurazione"
sopra), non più la risoluzione ASN. Con `tls_avanzato` disattivato e
`--concorrenza` alzata su una buona connessione, un campione di qualche
migliaio di host può completarsi in **minuti, non ore**; con tutto attivo
(incluso `tls_avanzato`) e concorrenza di default, aspettati più tempo — fai
un giro con `--limit 200` cronometrato per farti un'idea concreta sulla tua
rete prima di stimare il tempo del campione nazionale.

`05_scrape_dark_stack.py` aggiunge un download HTML + N lookup DNS/ASN per
i domini terzi trovati (mitigato da una cache **globale** sui domini terzi:
la maggior parte dei siti pubblici usa gli stessi 5-10 tracker/CDN, quindi
dopo i primi run il lookup si riusa quasi sempre — e ora, come 02, prova
prima il bulk Cymru). `cookie_tracker` non aggiunge tempo, riusa la stessa
richiesta. `06_fingerprint_cms.py` aggiunge un'altra richiesta HTTP per
hostname (indipendente da 02 e 05, spesso ancora in cache DNS del sistema
operativo). Regola i flag in `config.yaml` (o `--skip-http`/
`--skip-dark-stack`/`--skip-cms-fingerprint` in `run_pipeline.py`) se per
ora ti serve solo una parte del quadro.

Anche con tutte le ottimizzazioni, un residuo di `asn_*_lookup_failed` resta
possibile (gli IP che nemmeno il fallback RDAP riesce a risolvere): non è
più la norma come nella versione originale, ma su un campione di decine di
migliaia di host qualche riga con quell'errore è normale, non un segnale di
malfunzionamento.

**Se un sito risponde spesso 403/406/429/503**, prima di pensare a un
problema di rete controlla `report.json` → `http_status` → nota: potrebbe
essere un WAF/anti-bot (Cloudflare in particolare) che blocca lo User-Agent
di default, che si dichiara esplicitamente come bot — oppure, se è
concentrato su un singolo operatore, `--concorrenza-per-provider` troppo
alto per quel server (vedi sopra). `05`/`06` marcano queste righe con
`pagina_probabilmente_bloccata=True` quando la risposta sembra una pagina
di blocco invece che il sito vero: in quel caso `n_terze_parti`/
`n_software_rilevati` a 0 non significa "sito pulito", ma "non l'abbiamo
visto". Puoi cambiare lo User-Agent in `config.yaml` (`http_user_agent`) se
preferisci un tasso di blocchi più basso.

## Dove trovare i risultati

- `data/processed/IT/siti.csv` — elenco degli enti selezionati con i loro siti
  normalizzati, settore (`sector`) e paese (`country_code`) (output del passo 1)
- `data/results/IT/risultati.csv` — un rigo per ente: DNS/mail/nameserver
  (incluso il loro ASN), TLS (base + avanzato: protocolli deboli, cipher,
  HSTS, `tls_grade`), header HTTP/CDN, tempo di risposta, giurisdizione
  euristica dell'hosting (output del passo 2)
- `data/results/IT/dark_stack.csv` — un rigo per ente: dipendenze di terze
  parti rilevate nell'HTML (CDN/font, analytics, captcha...), quelle fuori
  dallo Spazio Economico Europeo, i cookie impostati dalla homepage
  classificati tecnico/profilazione, e `pagina_probabilmente_bloccata`
  (output del passo 5, idea "dark stack")
- `data/results/IT/cms_fingerprint.csv` — un rigo per ente: software
  riconosciuto (CMS/framework/server), versione se esposta, licenza, se è
  open source, e `pagina_probabilmente_bloccata` (output del passo 6,
  censimento software/licenze)
- `data/results/IT/*_manifest.json` — timestamp, versioni, User-Agent usato
  e quali misurazioni erano attive per ciascuna run, per riproducibilità
  (passi 2, 5, 6)
- `data/results/IT/report.json` — il numero di sintesi: HHI (per ASN, con
  categoria di concentrazione ed etichetta di affidabilità) su DNS/mail/
  nameserver — sia per-ente sia deduplicato per hostname unico —, copertura
  DNSSEC/SPF/distribuzione completa policy DMARC, riepilogo errori di
  misurazione, certificati TLS (scaduti/in scadenza), distribuzione HTTP
  status, CDN rilevate (incluso l'incrocio esplicito con IP extra-SEE),
  giurisdizioni (elenco dettagliato + aggregazione per macro-area), tempi di
  risposta (media/mediana/p95), robustezza TLS, cookie di profilazione,
  censimento software/licenze, breakdown per settore e per regione (output
  del passo 3) — ogni sezione compare solo se la misurazione corrispondente
  era attiva quando hai generato i CSV di input, e gli HHI su meno di 10
  dati validi vengono esclusi dai breakdown per non essere fuorvianti
- `data/results/IT/enti_a_rischio.csv` — un rigo per ente con un punteggio
  di rischio 0-5 (niente ridondanza NS, DMARC non attivo, niente DNSSEC,
  certificato scaduto/in scadenza, TLS grade basso) e i motivi, ordinato dal
  più a rischio: per capire da dove iniziare un intervento (output del passo 3)
- `data/results/IT/report_sintesi.md` — le stesse informazioni di
  `report.json` in frasi italiane già scritte, per chi non vuole leggere un
  JSON (output del passo 3)
- `data/results/IT/storico/report_*.json` — copie datate di `report.json`,
  solo se lanci `03_analyze.py --salva-storico`: utile per confrontare run
  diverse nel tempo (nessuno script di confronto automatico incluso ancora,
  vedi `docs/AUDIT_IMPLEMENTAZIONE.md`)
- `data/results/IT/enti_esteso.xlsx` — l'intera tabella `enti.xlsx` (tutte le
  righe, comprese quelle senza sito o non ancora sondate: vedi colonna
  `stato_probe`) con affiancate tutte le colonne misurate ai passi 2, 5 e 6,
  pronta per Minitab (output del passo 4)

## Dove scaricare i dati e dove metterli

Vedi `docs/GUIDA_DOWNLOAD_IPA.md` — passo per passo, incluso cosa fare se
il download automatico dentro lo script non funziona.

## Struttura del progetto

```
.
├── README.md                      ← questo file
├── LICENSE                        ← EUPL-1.2 (compila il tuo nome prima di pubblicare, vedi il file)
├── config.yaml                    ← configurazione unica: flag misurazioni, timeout, delay
├── requirements.txt
├── .vscode/                       ← configurazione VSCode (interprete, debug)
├── docs/
│   ├── IDEA_E_OUTPUT.md           ← spiegazione in linguaggio semplice
│   ├── GUIDA_DOWNLOAD_IPA.md      ← come scaricare i dati IPA
│   ├── AUDIT_IMPLEMENTAZIONE.md   ← cosa è stato testato e come, per le nuove misurazioni
│   └── AI_USAGE.md                ← log di trasparenza sull'uso di IA generativa
├── src/
│   ├── config.py                  ← loader di config.yaml, condiviso da tutti gli script
│   ├── 01_fetch_ipa_comuni.py     ← passo 1: estrazione siti, settore/paese
│   ├── 02_probe_infra.py          ← passo 2: DNS/mail/nameserver/ASN/TLS/HTTP/tempi risposta
│   ├── 05_scrape_dark_stack.py    ← passo 5: dipendenze terze parti + cookie (idea 1)
│   ├── 06_fingerprint_cms.py      ← passo 6: censimento software/licenze (idea 3)
│   ├── 03_analyze.py              ← passo 3: analisi/HHI/riepiloghi
│   ├── 04_export_enti_esteso.py   ← passo 4: export unico per Minitab
│   └── run_pipeline.py            ← esegue i passi in sequenza, rispettando config.yaml
└── data/
    ├── raw/<PAESE>/                ← dati grezzi scaricati, uno per paese (es. IT/)
    ├── rules/
    │   ├── dark_stack_rules.yaml   ← classificazione domini terzi, condivisa fra tutti i paesi
    │   ├── cookie_rules.yaml       ← classificazione cookie tecnico/profilazione
    │   └── cms_signatures.yaml     ← firme CMS/framework/server e relativa licenza
    ├── processed/<PAESE>/          ← output del passo 1, uno per paese
    └── results/<PAESE>/            ← output dei passi 2, 3, 4, 5, 6, uno per paese
```

Ogni script accetta `--paese CODICE` (default `IT`) per scegliere la
sottocartella: `python src\01_fetch_ipa_comuni.py --paese IT`. Per ora
`01_fetch_ipa_comuni.py` implementa solo la logica di estrazione italiana
(IndicePA); aggiungere un altro paese in futuro significa scrivere un nuovo
script di fetch (es. `01_fetch_fr.py`) che scriva in `data/processed/FR/siti.csv`
con lo stesso schema di colonne — da lì in poi `02`, `03`, `04`, `05` funzionano
già per qualunque paese passando `--paese FR`, senza modifiche.

## Ottimizzazioni (settembre 2026)

A valle di un primo run reale su 699 enti (su un campione IPA di 22.860),
due limiti erano diventati il collo di bottiglia principale per uno run
su scala nazionale: rate limit RDAP sui lookup ASN (fino al 55-75% delle
righe con errore su alcune colonne) e velocità (ordine di 0,7-1,6
host/minuto in modalità sequenziale). Modifiche:

- **`src/asn_bulk.py` (nuovo)** — risoluzione ASN/operatore in blocco via
  Team Cymru whois bulk (`whois.cymru.com:43`, una sola connessione TCP per
  centinaia di IP) invece di un lookup RDAP per IP. Fallback automatico su
  RDAP (stessa `resolve_asn()` di prima) solo per gli IP che il bulk non
  risolve. Controllabile da `config.yaml` (`asn_metodo: cymru_bulk` di
  default, `rdap` per tornare al comportamento originale) o da riga di
  comando (`--asn-metodo`).
- **`src/02_probe_infra.py` — pipeline a tre fasi invece di un ciclo
  sequenziale**: (1) DNS di un chunk di hostname IN PARALLELO
  (`ThreadPoolExecutor`, grado di parallelismo = `concorrenza` in
  config.yaml o `--concorrenza`, default 15); (2) UNA sola chiamata bulk
  per risolvere gli ASN di tutti gli IP raccolti nel chunk; (3) TLS/HTTP
  degli stessi hostname IN PARALLELO. I risultati vengono scritti e messi
  in flush su disco un chunk alla volta (`--chunk-size`, default 200): un
  'interruzione perde al massimo un chunk, `--resume` continua a
  funzionare come prima.
- **`src/05_scrape_dark_stack.py`** — i lookup Paese dei domini di terze
  parti usano lo stesso resolver bulk/fallback di `asn_bulk.py`.
- Log di avanzamento per chunk con velocità (host/s) e ETA, invece
  dell'annotazione manuale usata finora (vedi `stima tempi.txt`).

Guida pratica: `--concorrenza` è la leva principale per la velocità
complessiva; `--chunk-size` è un compromesso fra efficienza dei lookup ASN
bulk (chunk più grandi = meno query bulk) e resilienza di `--resume` (chunk
più piccoli = meno lavoro perso se il run si interrompe). Su una rete che
blocca la porta 43/TCP in uscita, passare a `asn_metodo: rdap` torna al
comportamento originale (più lento, ma non richiede quella porta).

### Seconda passata (stessa data): robustezza e "buon vicinato" di rete

A valle di una revisione critica della prima passata, sono stati aggiunti:

- **`asn_bulk.LimitatorePerBlocco`** — tetto massimo di connessioni
  TLS/HTTP simultanee verso lo STESSO blocco IP (`concorrenza_per_provider`
  in config.yaml, default 3), indipendente da `concorrenza`. Motivo: un
  singolo operatore può ospitare una quota rilevante del campione (vedi
  `giurisdizione_holding`/HHI nei risultati — un consorzio di hosting per
  piccoli comuni è il caso tipico), e senza questo limite `--concorrenza`
  connessioni potevano finire tutte insieme sullo stesso server, un
  comportamento aggressivo non coerente con un progetto che si dichiara
  onestamente (vedi `http_user_agent` sopra).
- **Retry con backoff breve** per una singola query bulk Cymru fallita
  (`asn_bulk.CYMRU_TENTATIVI_DEFAULT`), prima di far ricadere fino a 500 IP
  sul fallback RDAP per un blip di rete transitorio.
- **`try/except` attorno alla risoluzione ASN in blocco** in
  `02_probe_infra.py`: un errore imprevisto in quella fase non interrompe
  più l'intero run, solo lascia vuoti gli ASN del chunk corrente (con un
  avviso a schermo); il lavoro DNS già fatto per quel chunk non richiede
  comunque un `--resume` perché non era ancora stato scritto su disco.
- **"Canary check"**: appena si hanno abbastanza campioni (default: 10 IP
  tentati), se il bulk Cymru ha risolto meno del 50%, un avviso esplicito a
  schermo suggerisce di controllare la porta 43/TCP invece di scoprirlo
  solo a fine run.
- **Resolver DNS per thread** (non più uno nuovo per ogni hostname): il
  `ThreadPoolExecutor` ora è unico e persistente per l'intero run (non
  ricreato a ogni chunk), così i thread — e i resolver DNS associati —
  vengono davvero riusati.
- **Contatore risposte 403/429/503** per chunk e a fine run: un segnale
  diretto per capire se la parallelizzazione sta aumentando i blocchi
  WAF/rate-limit rispetto alla versione sequenziale, utile per tarare
  `--concorrenza`/`--concorrenza-per-provider`.
- **Etichetta organizzazione normalizzata** per gli IP risolti via Cymru
  (evita di duplicare il Paese, es. "GOOGLE, US" invece di
  "GOOGLE, US (US)"). Resta comunque un formato diverso da quello RDAP
  ("GOOGLE - Google LLC, US"): non è un problema per l'HHI/concentrazione
  in `03_analyze.py` (raggruppa per ASN numerico, uguale da entrambe le
  fonti), solo per l'etichetta mostrata nei report.

## Terza passata (integrazione estensioni esterne, settembre 2026)

Sono stati proposti tre script esterni indipendenti (`07_resilience.py`,
`08_vulnerability_lookup.py`, `09_technology_index.py`). Dopo audit, ne è
stato integrato uno per intero, uno in forma diversa da quella proposta, e
uno **non** è stato integrato:

- **`src/07_resilience.py`** (integrato, adattato) — misura la ridondanza
  DNS/HTTP effettivamente osservabile: ogni server NS dichiarato viene
  interrogato separatamente (non solo il resolver di sistema, come fa 02),
  e ogni indirizzo A/AAAA pubblicato viene testato individualmente
  (TCP→TLS→HTTP), non solo il primo raggiungibile. Aggiunge un dato che 02
  non fornisce: "se un nameserver o un endpoint cade, gli altri rispondono
  davvero?", invece di "il sito principale funziona?". Adattato per usare
  `config.yaml`/`config.py`, il `LimitatorePerBlocco` già esistente (stesso
  principio "buon vicinato" di 02), manifesto, `--resume`, colonne in
  italiano coerenti con lo schema hostname/comune/regione/codice_ipa.
  **Disattivato di default** (`resilienza_dinamica: false`): aggiunge
  connessioni TCP/TLS in più oltre a quelle già fatte da 02 per lo stesso
  hostname, va attivato consapevolmente.
  Un bug di design è stato corretto durante il test reale (su github.com):
  la versione originale confrontava le risposte A/AAAA/MX/SOA fra NS per
  uguaglianza esatta e contava ogni differenza come "incoerenza" — ma molti
  siti usano DNS round-robin/geo-DNS, per cui NS diversi restituiscono
  legittimamente A diversi. La metrica di incoerenza ora guarda solo il MX
  (dato stabile per definizione), la diversità di A/AAAA resta visibile ma
  solo come informazione (`n_ip_distinti_tra_ns`), non penalizzata.

- **Indice sintetico di dipendenza tecnologica** (in `03_analyze.py`,
  **non** un nono script separato) — l'estensione proposta
  (`09_technology_index.py`) ricalcolava HHI di hosting/tracker/software da
  zero leggendo i CSV grezzi, duplicando logica già presente in
  `03_analyze.py` (con il rischio concreto di due numeri diversi per la
  stessa cosa) e ignorando `hhi_affidabile`/`SOGLIA_MINIMA_HHI` (trattava
  un campione troppo piccolo come "concentrazione zero" invece di
  escluderlo). È stata invece aggiunta una funzione
  (`summarize_indice_dipendenza_tecnologica`) che combina SOLO numeri già
  scritti altrove nello stesso report.json (hosting_dns,
  tracker_concentrazione_hhi, software_licenze, resilienza_dinamica),
  rispettando le stesse soglie di affidabilità: una dimensione non
  affidabile viene esclusa dalla media pesata (ripesata sul resto), non
  trattata come zero. Attivo di default (`indice_dipendenza_tecnologica:
  true`), gratis se le misurazioni sottostanti sono già attive.

- **`08_vulnerability_lookup.py` (NON integrato)** — l'idea (correlare
  software+versione rilevati con vulnerabilità note via OSV) è valida in
  astratto, ma OSV copre ecosistemi di package manager (npm, PyPI,
  Packagist, crates.io, Debian, ...), non CMS/framework/web server generici.
  Verificato contro `data/rules/cms_signatures.yaml`: **nessuno** dei
  software attualmente riconosciuti da `06_fingerprint_cms.py` (WordPress,
  Joomla, Drupal, TYPO3, Plone, Liferay, DNN, Umbraco, Sitecore, Apache,
  nginx, IIS, PHP, ASP.NET, Bootstrap Italia) ha un ecosistema OSV
  corrispondente. Integrarlo così com'è avrebbe prodotto una colonna
  "nessuna vulnerabilità nota" per praticamente ogni riga — non perché il
  software sia sicuro, ma perché la fonte dati non copre questo dominio:
  un falso senso di sicurezza, più dannoso che utile in un report su
  infrastruttura pubblica. Per una versione utile servirebbe un lookup su
  NVD/CVE via CPE (identificatori vendor:prodotto:versione), che richiede
  di estendere `cms_signatures.yaml` con un campo CPE per firma e non è
  stato implementato in questa passata.

## Quarta passata (classificazione tipo ente + confronto fra Paesi, settembre 2026)

Richiesta: capire dal testo della homepage se un sito è un comune, una
provincia, una scuola, un ospedale, ecc., in tutte le lingue UE, e vedere se
la stessa raccolta dati permette una misura aggiuntiva.

- **`src/08_classifica_tipo_ente.py`** (nuovo) — scarica la homepage e la
  confronta con `data/rules/tipo_ente_rules.yaml`: 463 parole chiave nelle
  24 lingue ufficiali UE, per 11 categorie (comune, provincia, regione,
  scuola, università, sanità, polizia, vigili del fuoco, camera di
  commercio, ministero, giustizia). Non serve sapere la lingua del sito:
  confronta il testo con TUTTE le lingue insieme (un sito bilingue può
  autodichiararsi in più di una). Titolo/meta description pesano il triplo
  del corpo pagina nel punteggio. Attivo di default
  (`tipo_ente_classificazione: true`), stesso ordine di costo di 05/06 (un
  fetch completo per hostname).

- **Misura aggiuntiva "gratis"** (risposta a "questa raccolta dati può
  farci fare un test aggiuntivo?"): il corpo della homepage serve comunque
  per classificare, quindi cronometrarne il download COMPLETO non costa un
  giro di rete in più. A differenza di `http_response_time_ms` (02, si
  ferma agli header, corpo non scaricato — vedi commento in
  `fetch_http_headers()`), qui si misura `tempo_download_pagina_ms` e
  `peso_pagina_kb`: un proxy di UX/accessibilità digitale ("quanto ci mette
  un utente a scaricare la homepage"), non di salute del server. In
  `03_analyze.py`: sezione `tempo_download_pagina_completa`/`peso_pagina`
  (nel report generale) e per categoria/Paese (vedi sotto).

- **Segnale di possibile ente NON pubblico** — motivato da un caso REALE
  trovato in `data/processed/IT/siti.csv`: "Giochi24 S.r.l." è presente con
  `sector=camera_commercio` (le camere di commercio elencano anche aziende
  private iscritte, non solo uffici pubblici). Calcolato sulla stessa
  pagina già scaricata (nessun fetch in più): cerca forme societarie
  private ("S.r.l.", "GmbH", "Ltd"...) e terminologia e-commerce, in tutte
  le lingue coperte. Scritto **sia** come colonna (`probabile_non_pubblico`)
  nel file principale **sia** in un CSV separato
  (`tipo_ente_possibili_non_pubblici.csv`) per revisione indipendente senza
  toccare né il file principale né la lista di partenza — fatto INSIEME al
  fetch (nessun costo di rete aggiuntivo) ma esposto SEPARATAMENTE
  nell'output. È un CANDIDATO da rivedere a mano, non un'esclusione
  automatica: una società partecipata pubblica (es. "Farmacie Comunali
  S.p.A.") ha legittimamente una forma societaria privata pur essendo a
  controllo pubblico.

- **Arricchimento di `03_analyze.py`** — sezione `tipo_ente` (distribuzione
  categorie, % classificato, tempo/peso pagina, quota possibili enti non
  pubblici) e, dove `sector` è disponibile (oggi solo IT, da IndicePA),
  `accordo_con_sector`: confronta le due fonti SOLO sulle categorie con
  vocabolario condiviso (comune/provincia/regione/scuola/università/sanità)
  — un controllo di qualità sul classificatore stesso, oltre che
  sull'ente. Nuovo breakdown `per_tipo_ente` (stesso schema di
  `per_settore`, ma per categoria testuale, disponibile per QUALUNQUE
  Paese, non solo l'Italia).

- **`src/09_confronto_paesi.py`** (nuovo) — risponde a "vedendo i dati per
  ogni classe ed ogni Paese EU": l'UNICO script della pipeline che lavora
  su più Paesi insieme (01-08 sono tutti "per Paese"). Va eseguito DOPO
  `03_analyze.py` per ciascun Paese di interesse: scopre automaticamente i
  Paesi con un run completato (`data/results/<PAESE>/latest_run.json`),
  legge la sezione `tipo_ente` del loro `report.json` e produce una matrice
  categoria x Paese (`confronto_tipo_ente_per_paese.csv`) più una tabella
  di sintesi per Paese (`confronto_sintesi_per_paese.csv`, con tempo/peso
  pagina e quota di possibili enti non pubblici). Un Paese senza sezione
  `tipo_ente` viene SALTATO con un avviso esplicito, non riempito a zero:
  "nessun dato raccolto" e "zero comuni rilevati" sono informazioni
  diverse, confonderle userebbe l'assenza di dato come se fosse un
  risultato.

## Verifica manuale di link sospetti (aggiunta su richiesta, settembre 2026)

Richiesta: per ogni dominio di terza parte trovato, avere la prima URL
esatta in cui è stato trovato, per poter verificare/certificare a mano
pagine sospette (es. link a piattaforme come Wix comparsi su un sito
istituzionale, possibile indizio di compromissione).

- **`05_scrape_dark_stack.py`** scrive ora, oltre a `dark_stack.csv`
  (aggregato per hostname), anche `dark_stack_link_dettaglio.csv`: una riga
  per ogni (hostname scansionato, dominio di terza parte), con la URL
  ESATTA della pagina letta (`pagina_scansionata`, dopo eventuali redirect —
  `fetch_html()` ora ritorna anche `resp.url`, non solo lo status/HTML) e la
  URL esatta della risorsa che referenzia quel dominio. Scritto GRATIS sulla
  stessa richiesta HTTP già fatta per `dark_stack.csv`, nessun fetch
  aggiuntivo.
- Alla fine del run, lo script rilegge l'INTERO file di dettaglio (non solo
  le righe di questa esecuzione) e produce **due file separati**:
  `terze_parti_da_verificare.csv` e `terze_parti_da_verificare.md`. Per
  ciascun dominio di terza parte: la PRIMA volta in cui è comparso (per
  timestamp) con la sua URL esatta, e l'elenco di TUTTE le pagine in cui
  compare — nel formato "dominio.com, trovato nei link: sito1.it/...,
  sito2.it/..., ...". Ordinato dal dominio più raro (visto su meno pagine,
  di solito il più interessante da controllare) al più diffuso. Una nota in
  testa al file .md ricorda che comparire nell'elenco non è di per sé prova
  di malevolenza (fornitori di font/mappe/analytics compaiono
  legittimamente su moltissimi siti): è un aiuto alla revisione manuale, non
  un verdetto automatico.
- Attivabile/disattivabile da `config.yaml` (`terze_parti_dettaglio_link`,
  default true, gratis se `dark_stack_terze_parti` è già attivo).
- `03_analyze.py` include ora un rimando leggero a questi file dentro
  `report.json` (conteggio + i 5 domini più rari), senza duplicarne il
  contenuto — il dettaglio pesante resta nei file separati, come richiesto.

## Protezione dei checkpoint (aggiunta su richiesta, settembre 2026)

Ogni script (01-09) scrive i propri output per Paese/run in modalità
sovrascrittura quando gira SENZA `--resume`. Corretto per un run nuovo, ma
rilanciare lo STESSO `--run-id` senza `--resume` (es. lo stesso giorno,
dato che il run_id di default è la data odierna) sovrascriverebbe
silenziosamente il checkpoint del livello precedente per quel Paese.

Per evitarlo, `config.py` espone `backup_se_esiste(path)`: chiamata da
TUTTI gli script prima di aprire un output in scrittura, se il file esiste
già lo rinomina con un suffisso di timestamp (es.
`tipo_ente_backup-20260915T100925Z.csv`) invece di lasciarlo sovrascrivere.
Nessun checkpoint va più perso rilanciando uno script per errore: il file
precedente resta sempre recuperabile su disco, accanto a quello nuovo.
`--resume` (che fa append, non sovrascrittura) non genera backup — solo la
combinazione "stesso run-id, file già esistente, nessun --resume" lo fa.
Verificato con un test diretto: primo run scrive `tipo_ente.csv`, un
secondo run sullo stesso `--run-id` senza `--resume` sposta il file
precedente in `tipo_ente_backup-...csv` prima di riscrivere, un run con
`--resume` subito dopo non genera backup aggiuntivi.

## Limiti noti (da correggere prima di un dataset pubblicabile)

- Il join ente→comune/provincia/regione (in `01_fetch_ipa_comuni.py`) usa il
  codice catastale ufficiale (`Codice_catastale_comune` in `enti.xlsx`)
  incrociato con l'anagrafica `matteocontrini/comuni-json` (non ufficiale,
  ma chiave = codice catastale, non nome): affidabile, ma per un dataset
  finale conviene comunque incrociare anche con il file ISTAT ufficiale
  `Elenco-comuni-italiani.csv` come controllo indipendente.
- La verifica DNSSEC è "esiste un DS nel genitore", non una validazione
  crittografica completa della catena di fiducia.
- La diversità NS/MX (`ns_diversi_secondlevel` in `02_probe_infra.py`) e il
  riconoscimento "stesso sito vs terza parte" in `05_scrape_dark_stack.py`
  usano lo stesso dominio di secondo livello grezzo, senza Public Suffix
  List — approssimazione accettabile sui domini `.it`, da correggere prima
  di estendere ad altri paesi.
- ~~RDAP (per il lookup ASN) ha rate limit propri lato registri regionali:
  su campioni di migliaia di host conviene passare a un lookup bulk (es.
  IP-to-ASN via DNS di Team Cymru) invece di RDAP per singolo IP.~~
  **Risolto (settembre 2026)**: vedi `src/asn_bulk.py` e la sezione
  "Ottimizzazioni (settembre 2026)" più sotto. Il fallback RDAP-per-IP
  resta disponibile (`asn_metodo: rdap` in `config.yaml`) e per la
  minoranza di IP che il bulk non risolve, quindi il limite originale non è
  sparito del tutto, solo ridotto a un caso marginale invece che alla
  norma.
- `giurisdizione_holding` (02) è un'euristica su pattern testuali del nome
  operatore RDAP, NON un dato giuridico verificato: copre gli operatori più
  comuni, va estesa quando se ne incontrano di non riconosciuti (il report
  li segnala come "non riconosciuto").
- `05_scrape_dark_stack.py` analizza l'HTML statico restituito dal server,
  non il traffico reale del browser: risorse iniettate via JavaScript dopo
  il caricamento (es. un chat widget richiamato da un altro script) non
  vengono viste. Per una mappatura completa servirebbe un browser headless
  (Playwright/Selenium).
- La classificazione dei domini terzi (`data/rules/dark_stack_rules.yaml`)
  copre i vendor più comuni ma non è esaustiva: i domini non riconosciuti
  finiscono in `altro_terzo_parte` e vanno rivisti a occhio se il conteggio
  è alto.
- `tls_grade` (02, con `tls_avanzato`) è un voto sintetico A-D con
  un'euristica dichiarata e semplice (protocolli deboli attivi → cipher
  debole → HSTS assente), **non** il metodo di SSL Labs, che pesa molte più
  variabili: va letto come colpo d'occhio su un campione grande, non come
  audit di sicurezza puntuale su un singolo sito.
- La classificazione tecnico/profilazione dei cookie
  (`data/rules/cookie_rules.yaml`, 05 con `cookie_tracker`) è un'euristica
  sul NOME del cookie, non una valutazione giuridica GDPR/ePrivacy: il nome
  è un forte indizio, non una prova. Cattura solo i cookie della prima
  risposta HTTP — cookie impostati via JavaScript, o solo dopo il consenso
  su un banner, non vengono visti (stesso limite strutturale del dark
  stack: servirebbe un browser headless).
- Il censimento software/licenze (`06_fingerprint_cms.py`,
  `data/rules/cms_signatures.yaml`) è un fingerprint da TRACCE ESTERNE
  (markup HTML, header HTTP), non un'ispezione del codice installato:
  misura "che software risulta in uso e se quel software, in generale, è
  open source con quale licenza", **non** se l'ente rispetta gli obblighi
  di quella licenza (attribuzione, ridistribuzione delle modifiche per
  licenze copyleft, ecc.) — un audit di conformità legale richiede accesso
  al codice sorgente installato e non è automatizzabile da fuori. Un
  mancato riconoscimento non significa "nessun software": molti siti
  rimuovono deliberatamente le tracce di fingerprint per motivi di
  sicurezza. Le firme coprono i CMS/framework più comuni nella PA italiana
  ma non sono esaustive.
- `cdn_extra_ue` (03, sezione del report) usa il Paese ASN dell'IP del sito
  (`asn_country_a`, dato RDAP) come criterio geografico: è diverso da
  `giurisdizione_holding_hosting`, che invece guarda la nazionalità della
  società che gestisce l'hosting. Un server fisicamente in UE gestito da
  una controllata di una società extra-UE compare come "non extra-UE" in
  `cdn_extra_ue` ma può comparire come tale in `giurisdizione_holding_hosting`:
  sono due letture complementari dello stesso fenomeno, non la stessa cosa.
- L'HHI (03) viene calcolato solo se ci sono almeno `SOGLIA_MINIMA_HHI`
  (10) dati validi nel gruppo: sotto quella soglia il valore compare
  comunque (con `hhi_affidabile: false` e una nota) nei blocchi principali
  (`hosting_dns`/`hosting_mail`/`hosting_nameserver`), ma viene ESCLUSO del
  tutto dai breakdown `per_settore`/`per_regione` per non riempirli di
  numeri fuorvianti calcolati su 1-2 enti. 10 è una soglia pragmatica, non
  uno standard statistico: su un campione complessivo grande va bene, su un
  sottogruppo già piccolo di suo potrebbe essere ancora generosa.
- Le viste `*_per_hostname_unico` (03) rispondono a una domanda diversa
  dalle viste principali: "quante infrastrutture fisiche distinte esistono"
  invece di "da quanti enti dipende ciascun operatore". Nessuna delle due è
  "quella giusta": scegli in base a cosa stai comunicando (numero di enti
  esposti a un fornitore vs numero di infrastrutture diverse in campo).
- `pagina_probabilmente_bloccata` (05, 06) è un'euristica su status HTTP +
  lunghezza/contenuto del corpo della risposta, non una certezza: pensata
  per essere conservativa (preferisce non segnalare un blocco reale
  piuttosto che segnalarne uno inesistente), quindi è più probabile un
  falso negativo (blocco non rilevato) che un falso positivo.
- La cache per prefisso IP (`asn_bulk.prefisso_cache()`, blocchi /24 IPv4 o
  /48 IPv6 — usata sia dal bulk Cymru che dal fallback RDAP e dal
  `LimitatorePerBlocco`) è un compromesso fra hit-rate della cache e
  rischio di confondere due operatori diversi su blocchi adiacenti. Su
  allocazioni più frammentate del solito (rare, ma esistono) potrebbe
  attribuire lo stesso ASN a IP che in realtà appartengono a operatori
  diversi vicini nello spazio di indirizzamento.
- `enti_a_rischio.csv` (03) è un punteggio COMPOSITO ad-hoc (un punto per
  condizione), non una metodologia di risk-scoring validata: pensato per
  dare una priorità pratica su quali enti guardare per primi, non come
  giudizio definitivo sulla sicurezza di un singolo ente.
- `07_resilience.py` fa probe DNS/TCP/TLS ONE-SHOT (una query/connessione
  per NS/endpoint, non ripetuta nel tempo): un endpoint che risulta giù in
  quell'istante potrebbe essere solo un blip transitorio, non un guasto
  strutturale — per distinguere i due casi servirebbero misurazioni ripetute
  a distanza di tempo, non ancora implementate. `n_ip_distinti_tra_ns` è
  puramente informativo (diversità di indirizzi fra NS): un valore alto può
  indicare tanto DNS round-robin/anycast normale quanto una zona
  disallineata fra provider, i dati raccolti da questo script non bastano a
  distinguere i due casi con certezza.
- `indice_dipendenza_tecnologica` (03) è, come `enti_a_rischio.csv`, un
  indice COMPOSITO ad-hoc con pesi scelti a mano (non stimati/validati
  statisticamente): utile per un colpo d'occhio comparativo fra run/paesi
  diversi con la STESSA formula, non come singolo numero da citare senza
  guardare quali dimensioni sono incluse (`dimensioni_incluse`/
  `dimensioni_escluse` nel report.json).
- `08_classifica_tipo_ente.py` è un classificatore a PAROLE CHIAVE, non un
  modello linguistico: un ente che non si autodichiara nella homepage (sito
  minimale, testo generato via JavaScript non eseguito da questo script)
  resta `non_classificato` — non equivale a "non è un ente pubblico". La
  colonna `probabile_non_pubblico` è un CANDIDATO da rivedere a mano
  (vedi sezione sopra), MAI un filtro da applicare automaticamente per
  escludere righe. La profondità di copertura delle 24 lingue UE non è
  uniforme (vedi commento in testa a `data/rules/tipo_ente_rules.yaml`):
  un Paese con poche parole chiave dedicate avrà più `non_classificato`
  degli altri per questo motivo, non necessariamente perché i suoi enti
  si autodichiarano meno.
- `09_confronto_paesi.py` confronta Paesi la cui lista di partenza
  (`siti.csv`) è stata raccolta con metodologie ANCHE MOLTO diverse fra
  loro (per l'Italia da un indice ufficiale IndicePA, per altri Paesi
  spesso a mano — vedi `data/processed/AT/siti.csv`): una differenza nella
  distribuzione tipo_ente fra due Paesi può riflettere una differenza reale
  oppure solo una lista di partenza più o meno completa/rappresentativa.
  Non è un confronto "a campione controllato" nel senso statistico del
  termine.
