# Osservatorio concentrazione/resilienza infrastruttura pubblica italiana

> **English overview.** This is the infrastructure-measurement pipeline of the EU-PA DarkStack Observatory. The internal working name is `dst` ("dark stack toolkit"). It was first built for Italian public bodies, using the IndicePA registry, and is now parametrised per country (`--paese IT|FR|...`). For each public entity it measures DNS, mail, hosting ASN and jurisdiction, TLS, HTTP/CDN, third-party "dark stack" dependencies and cookies, CMS/licence, and real nameserver redundancy. It then computes HHI concentration and a composite risk score.
>
> What the sections below cover:
> - *Setup su Windows 11 + VSCode*: installation.
> - *Configurazione (config.yaml)*: switching each measurement on or off.
> - *Quickstart da terminale*: always start with a 20-host test, then a medium run, then the national run.
> - *Tempi di esecuzione*: runtimes.
> - *Dove trovare i risultati*: where the outputs are written.
> - *Struttura del progetto*: project layout.
> - *Ottimizzazioni*: changelog of the optimisation passes.
> - *Limiti noti*: **known limitations**, worth reading.
>
> `README_aggiornato.md` is the **more recent and complete** version of this file. It adds the entity-type classification, the cross-country comparison, manual verification of suspicious links, and checkpoint protection.
>
> Main command: `python src/run_pipeline.py --paese IT --limit 20`. Results go to `data/results/<COUNTRY>/<RUN_DATE>/`. The [glossary](../GLOSSARY.md) translates file and column names; [`data/results/README.md`](data/results/README.md) describes the published runs.

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

**OTTIMIZZAZIONE (seconda passata, settembre 2026)**: ogni run scrive ora in
`data/results/<PAESE>/<RUN-ID>/`, non più direttamente in
`data/results/<PAESE>/` — un run non sovrascrive più il precedente. `<RUN-ID>`
di default è la data UTC del giorno (`AAAA-MM-GG`); `data/results/<PAESE>/latest_run.json`
punta sempre all'ultimo run completato con successo. I percorsi qui sotto
vanno quindi letti come `data/results/IT/<RUN-ID>/risultati.csv` ecc. — se
lanci gli script senza `--run-id` esplicito, quello di oggi (o l'ultimo noto,
per gli script che leggono dati già prodotti come `03`/`04`) è scelto in
automatico, vedi la sezione "Ottimizzazioni" più sotto per il dettaglio.

- `data/processed/IT/siti.csv` — elenco degli enti selezionati con i loro siti
  normalizzati, settore (`sector`) e paese (`country_code`) (output del passo 1)
- `data/results/IT/<RUN-ID>/risultati.csv` — un rigo per ente: DNS/mail/nameserver
  (incluso il loro ASN), TLS (base + avanzato: protocolli deboli, cipher,
  HSTS, `tls_grade`), header HTTP/CDN, tempo di risposta, giurisdizione
  euristica dell'hosting, dipendenza dall'AS di transito principale se
  `as_hegemony` è attivo (output del passo 2)
- `data/results/IT/<RUN-ID>/dark_stack.csv` — un rigo per ente: dipendenze di terze
  parti rilevate nell'HTML (CDN/font, analytics, captcha...) con relativa
  organizzazione madre quando nota, quelle fuori
  dallo Spazio Economico Europeo, i cookie impostati dalla homepage
  classificati tecnico/profilazione, e `pagina_probabilmente_bloccata`
  (output del passo 5, idea "dark stack")
- `data/results/IT/<RUN-ID>/cms_fingerprint.csv` — un rigo per ente: software
  riconosciuto (CMS/framework/server), versione se esposta, licenza, se è
  open source, e `pagina_probabilmente_bloccata` (output del passo 6,
  censimento software/licenze)
- `data/results/IT/<RUN-ID>/*_manifest.json` — timestamp, `run_id`, versioni,
  User-Agent usato e quali misurazioni erano attive per ciascuna run, per
  riproducibilità (passi 2, 5, 6)
- `data/results/IT/<RUN-ID>/report.json` — il numero di sintesi: HHI (per ASN, con
  categoria di concentrazione ed etichetta di affidabilità) su DNS/mail/
  nameserver — sia per-ente sia deduplicato per hostname unico —, copertura
  DNSSEC/SPF/distribuzione completa policy DMARC, riepilogo errori di
  misurazione (per campo E per causa canonica: rate limit, timeout, ...),
  certificati TLS (scaduti/in scadenza), distribuzione HTTP
  status, CDN rilevate (incluso l'incrocio esplicito con IP extra-SEE),
  giurisdizioni (elenco dettagliato + aggregazione per macro-area), tempi di
  risposta (media/mediana/p95), robustezza TLS, cookie di profilazione,
  concentrazione (HHI) dei tracker/terze parti per organizzazione madre,
  censimento software/licenze, breakdown per settore e per regione (output
  del passo 3) — ogni sezione compare solo se la misurazione corrispondente
  era attiva quando hai generato i CSV di input, e gli HHI su meno di 10
  dati validi vengono esclusi dai breakdown per non essere fuorvianti
- `data/results/IT/<RUN-ID>/enti_a_rischio.csv` — un rigo per ente con un punteggio
  di rischio 0-5 (niente ridondanza NS, DMARC non attivo, niente DNSSEC,
  certificato scaduto/in scadenza, TLS grade basso) e i motivi, ordinato dal
  più a rischio: per capire da dove iniziare un intervento (output del passo 3)
- `data/results/IT/<RUN-ID>/report_sintesi.md` — le stesse informazioni di
  `report.json` in frasi italiane già scritte, per chi non vuole leggere un
  JSON (output del passo 3)
- `data/results/IT/<RUN-ID>/storico/report_*.json` — copie datate di `report.json`,
  solo se lanci `03_analyze.py --salva-storico`: da quando ogni run ha già la
  propria cartella `<RUN-ID>`, serve solo per confrontare più analisi fatte
  nello STESSO run, non più per lo storico fra run diversi (quello ora è
  semplicemente "guarda cartelle `<RUN-ID>` diverse")
- `data/results/IT/<RUN-ID>/enti_esteso.xlsx` — l'intera tabella `enti.xlsx` (tutte le
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
    └── results/<PAESE>/            ← output dei passi 2, 3, 4, 5, 6, ORGANIZZATI PER RUN (vedi sotto)
        ├── latest_run.json          ← puntatore all'ultimo run completato con successo
        └── <RUN-ID>/                ← es. 2026-09-11/ — un run = una cartella, mai sovrascritta da un run successivo
            ├── risultati.csv
            ├── dark_stack.csv
            ├── cms_fingerprint.csv
            ├── report.json
            ├── report_sintesi.md
            └── enti_a_rischio.csv
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

### Terza passata (seconda passata "PICO", settembre 2026): copertura, storico, e due addizioni sperimentali

A valle della bozza di proposta di finanziamento del progetto (vedi
`docs/AUDIT_IMPLEMENTAZIONE.md` per il contesto), una lista di ottimizzazioni
concordata in anticipo è stata implementata e testata (senza accesso alla
rete pubblica in fase di sviluppo — vedi `docs/AUDIT_IMPLEMENTAZIONE.md` per
il dettaglio di cosa è stato verificato e come):

- **Fallback RIPEstat** (`src/asn_bulk.py`, `resolve_asn_ripestat()`) —
  livello intermedio fra il bulk Cymru e l'RDAP per-IP finale, per la
  minoranza di IP che il bulk non risolve. Punta a ridurre proprio la
  copertura di attribuzione operatore (45,4%/56,7% nel pilota italiano),
  additivo per costruzione: se irraggiungibile, il comportamento ricade
  esattamente su quello di prima.
- **Retry con backoff** (`src/errors.py`, `ritenta_con_backoff()`) sul
  fallback RDAP finale, che prima non ne aveva nessuno.
- **Tassonomia di errore canonica** (`src/errors.py`,
  `classifica_errore()`): oltre al riepilogo per CAMPO già esistente
  (`top10_tipi_errore` in `03_analyze.py`), un secondo riepilogo per CAUSA
  (`top10_categorie_errore`: rate_limited, timeout, ...) — dice cosa
  aggiustare, non solo dove.
- **Rate limiter condiviso** (`src/rate_limiter.py`, token bucket) davanti a
  RDAP/RIPEstat: senza, con molti IP non risolti dal bulk, fino a
  `--concorrenza` thread potevano interrogare quei servizi tutti insieme.
- **Esecuzioni con timestamp**: ogni run scrive ora in
  `data/results/<paese>/<run-id>/`, non più sovrascrivendo il run
  precedente — condizione necessaria per l'osservazione ripetuta ogni 3-6
  mesi promessa dal progetto. `data/results/<paese>/latest_run.json` punta
  all'ultimo run completato con successo. **Bugfix incluso**: `--resume`
  che scavalla la mezzanotte UTC riprende correttamente lo stesso run
  (via il puntatore), non ne apre uno nuovo vuoto — importante perché un
  campione nazionale può impiegare più di un giorno (vedi `stima tempi.txt`).
- **Concentrazione dei tracker per organizzazione madre** (`03_analyze.py`,
  `summarize_tracker_hhi()`): stesso principio di Singh et al. (2026),
  calcolato sui dati già raccolti da `05_scrape_dark_stack.py` (nuova
  colonna `organizzazioni_madri_terzi`, da `organizzazione_madre` opzionale
  in `data/rules/dark_stack_rules.yaml`) — nessuna richiesta di rete in
  più. Attivo di default (`misurazioni.tracker_hhi`).
- **SPERIMENTALE, spento di default — dipendenza dall'AS di transito**
  (`src/as_hegemony.py`, `misurazioni.as_hegemony`): usa l'API di IHR
  (Internet Health Report, IIJ Research Lab — **non** RIPEstat: una prima
  bozza di questo modulo aveva ipotizzato erroneamente un endpoint
  RIPEstat inesistente, corretto dopo verifica della documentazione reale,
  vedi commento in cima al file). Non verificato contro il servizio reale
  in questo ambiente di sviluppo (nessun accesso alla rete pubblica): la
  logica di parsing è testata solo contro un mock locale. Attivalo con
  `--limit 20` la prima volta e controlla `errori` per `as_hegemony_failed`
  prima di un run massivo.

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
