# Audit implementazione — config.yaml + 5 nuove misurazioni

> **English summary.** This is the implementation audit log, written in three rounds (9–11 September 2026). For each round it records:
> - how the changes were verified, and what could not be verified offline;
> - what was tested;
> - which bugs were found and fixed before delivery;
> - what was deliberately not implemented;
> - what to check during the next real run.
>
> It covers `config.yaml` and the five new measurements, the bulk ASN resolution and concurrency work, and the "PICO" round: the error taxonomy, the rate limiter, the RIPEstat fallback, timestamped runs, tracker HHI and the experimental AS Hegemony.

Data: 2026-09-09
Ambito: risposta alla richiesta di (1) un config.yaml unico con flag
True/False per attivare/disattivare ogni misurazione, tutti impostati a
True, e (2) l'implementazione di tempi di risposta, robustezza TLS, cookie
di profilazione, censimento software/licenze open source, CDN extra-UE
esplicita.

## Come è stato verificato (e come no)

L'ambiente in cui è stato scritto questo codice non ha accesso alla rete
pubblica (solo a repository di pacchetti: PyPI, npm, GitHub — non a siti
`.it` reali). Di conseguenza:

- **Verificato con test reali** (server HTTP/HTTPS locali, avviati apposta,
  con certificato self-signed generato al momento): tutta la logica di
  parsing, estrazione ed elaborazione — è la parte che può nascondere bug
  di programmazione (indici sbagliati, eccezioni non gestite, encoding).
- **Verificato per lettura/ragionamento, non con una run reale su
  data/processed/IT/siti.csv**: il comportamento su un campione vero di
  22.000 host italiani — varietà reale di configurazioni server, latenze di
  rete, comportamenti imprevisti di singoli siti. Prima di un run massivo,
  fai un giro con `--limit 20` come già facevi, guardando in particolare
  la colonna `errori` dei nuovi campi.

## Cosa è stato testato, nel dettaglio

| Componente | Test eseguito | Esito |
|---|---|---|
| `config.py` (load_config, flag, merge con DEFAULTS) | Caricamento config.yaml reale + fallback senza file | OK |
| `02_probe_infra.py` — `scan_tls_protocols` | Contro server HTTPS locale (TLS1.2/1.3 attivi, 1.0/1.1/SSLv3 rifiutati dal sistema): rilevati correttamente i protocolli supportati, nessun crash sui non supportati | OK |
| `02_probe_infra.py` — `get_tls_cipher_info` | Cipher/bit letti correttamente (`TLS_AES_256_GCM_SHA384`, 256 bit) | OK |
| `02_probe_infra.py` — `calcola_tls_grade` | 4 combinazioni (A/B/C/D) testate esplicitamente con input sintetici | OK |
| `02_probe_infra.py` — `fetch_http_headers` (tempo di risposta + HSTS) | Contro server locale: tempo misurato (35.6 ms), HSTS rilevato da header reale | OK |
| `02_probe_infra.py` — flag granulari (`dns_base`, `tls_avanzato`, `tempi_risposta`, ecc.) | Run completo con `config_off.yaml` (metà flag a false): i campi disattivati restano vuoti, quelli attivi restano popolati | OK |
| `05_scrape_dark_stack.py` — estrazione/classificazione cookie | Contro server locale con 2 Set-Cookie reali (`session_id`, `_ga`): classificati correttamente tecnico/profilazione | OK |
| `05_scrape_dark_stack.py` — uscita pulita con flag disattivati | `dark_stack_terze_parti=false` → script esce (`exit 0`) senza scrivere output | OK |
| `06_fingerprint_cms.py` — fingerprint completo | Contro server locale con WordPress 6.2 (meta generator + wp-content/wp-includes), header `Server: Apache/2.4.41`, `X-Powered-By: PHP/8.1.2`: tutti e 3 riconosciuti con versione e licenza corrette | OK |
| `06_fingerprint_cms.py` — bug apici singoli vs doppi | Trovato e corretto durante il testing: alcuni CMS generano `<meta content='...'>` con apici singoli, i pattern erano scritti solo per apici doppi → aggiunta normalizzazione apici prima del match | OK (dopo fix) |
| `03_analyze.py` — tutte le nuove sezioni del report | Dataset sintetico di 6 enti con varietà di casi (incluso un ente con probe totalmente fallito): `tempi_risposta_http`, `tls_robustezza`, `cdn_extra_ue`, `cookie` (dentro dark_stack), `software_licenze` — tutte le sezioni prodotte senza errori, valori numerici verificati a mano | OK |
| `04_export_enti_esteso.py` — merge di `cms_fingerprint.csv` | Run end-to-end con enti.xlsx (7 righe, 1 senza sito) + risultati.csv + dark_stack.csv + cms_fingerprint.csv (6 righe ciascuno): tutte le 63 colonne finali presenti, `stato_probe` corretto | OK |
| `run_pipeline.py` | Solo verifica di sintassi/argparse (`--help`): non eseguito end-to-end perché richiederebbe un giro completo con rete pubblica | Non eseguito end-to-end |
| Tutti gli script | `python -m py_compile` su ciascun file | OK, nessun errore di sintassi |
| Tutti gli script | `--help` su ciascuno script | OK, nessun crash all'avvio |

## Bug trovati e corretti durante il testing

1. **`06_fingerprint_cms.py`, apici singoli**: la prima versione dei pattern
   HTML (es. `content="WordPress`) non matchava markup con apici singoli
   (`content='WordPress'`), che alcuni CMS generano. Corretto normalizzando
   gli apici singoli in doppi prima del match (vedi commento nel codice).
2. **`03_analyze.py`, prima versione di `cdn_extra_ue`**: la prima bozza
   usava `giurisdizione_holding` (euristica sul nome della holding) come
   criterio di "extra-UE", il che classificava come extra-UE anche
   holding europee non riconosciute dalla lista (falsi positivi). Corretto
   usando `asn_country_a` (il Paese ASN reale via RDAP) confrontato con
   l'elenco SEE — criterio geografico più verificabile, coerente con quello
   già usato in `05_scrape_dark_stack.py` per i domini terzi.

Nessun altro bug funzionale è stato trovato nei test eseguiti, ma questo
non è una garanzia di assenza di bug: sono stati testati gli scenari
principali (successo, fallimento di rete/DNS, flag disattivati), non ogni
possibile risposta anomala di un server reale (redirect infiniti, HTML
malformato in modi esotici, certificati con encoding non standard, ecc.):
gli script hanno comunque `try/except` ampi attorno a ogni chiamata di rete
proprio per questo, seguendo lo stile già presente nel resto del progetto.

## Cosa NON è stato possibile testare qui

- **Volume/performance reali**: quanto tempo aggiunge davvero
  `tls_avanzato` su un campione di migliaia di siti italiani reali (la
  stima nel README, "la misurazione più lenta", è basata sul numero di
  connessioni TCP+TLS extra per host, non su un cronometraggio reale).
- **RDAP rate limiting** con il nuovo carico (nessuna nuova chiamata RDAP è
  stata aggiunta rispetto a prima, quindi il comportamento dovrebbe essere
  identico a quello già noto, ma non è stato ri-verificato).
- **Varietà reale di generator/header** dei CMS italiani della PA (Bootstrap
  Italia, Liferay, Joomla, ecc.): le firme in `cms_signatures.yaml` sono
  scritte da conoscenza generale di questi prodotti, non estratte da
  campioni reali di siti PA italiani — è probabile che vadano affinate
  dopo aver visto i primi risultati veri (falsi negativi soprattutto: siti
  che usano varianti di markup non ancora coperte).
- **`config.yaml` con `run_pipeline.py` end-to-end**: la logica di quali
  step saltare (05/06) è stata verificata leggendo il codice, non con
  un'esecuzione reale della pipeline completa (richiederebbe accesso a
  internet pubblico, non disponibile in questo ambiente).

## Consiglio pratico per il primo giro

```powershell
python src\run_pipeline.py --regione "Lombardia" --limit 20
```

Poi guarda in ordine:

1. `data/results/IT/risultati.csv` — colonna `errori`: se `tls_scan_protocolli_failed`
   o `tls_cipher_failed` compaiono su molte righe, potrebbe essere la rete
   locale (firewall aziendale che blocca connessioni multiple ravvicinate)
   più che un problema del codice.
2. `data/results/IT/cms_fingerprint.csv` — guarda a occhio 5-6 righe con
   `n_software_rilevati > 0`: i nomi/versioni hanno senso? Se un sito noto
   per essere WordPress risulta 0, è un segnale che le firme vanno estese.
3. `data/results/IT/report.json` — sezione `tls_robustezza.distribuzione_grade`
   e `software_licenze.top15_software`: primo sanity check sull'insieme.

Se qualcosa non torna su questi tre punti, è più utile mandarmi quelle
righe specifiche (anche solo 2-3) che rilanciare tutto da capo.

---

# Audit — correzioni e ottimizzazioni (round 2)

Data: 2026-09-09 (stesso giorno, giro successivo)

Ambito: le 5 correzioni e le 3 ottimizzazioni chieste esplicitamente, più
tutte le "aggiunte" proposte nelle due analisi precedenti (interpretazione
HHI, sezione certificati, HTTP status, DMARC completo, aggregazione
geografica, breakdown per regione, enti a rischio, sintesi in linguaggio
naturale).

## Cosa è stato verificato con test reali in questo giro

| Componente | Test eseguito | Esito |
|---|---|---|
| Bug ASN (`.0` fasullo) | Letto risultati.csv reale caricato dall'utente byte per byte (`csv.DictReader`, non pandas): confermato che il CSV grezzo è **corretto**, il bug era solo nella lettura pandas senza `dtype=str` in `03_analyze.py`/`04_export_enti_esteso.py`. Corretto e ri-verificato: output senza `.0` | OK |
| Cache RDAP per prefisso (`resolve_asn_cached`) | Unit test con `resolve_asn` mockato: 2 IP nello stesso /24 → 1 sola chiamata reale, 1 IP in /24 diverso → chiamata separata | OK |
| HHI + soglia di affidabilità | Su file reale a 3 righe: `hhi_affidabile: False` (corretto, sotto soglia). Su dataset sintetico a 30 righe con dati random: `hhi_affidabile: True` (30 ≥ soglia 10). Verificato che `per_settore`/`per_regione` escludono correttamente i gruppi sotto soglia (tutte le regioni del dataset da 30 righe sono state escluse perché nessuna raggiunge 10 dati validi — comportamento atteso, non un bug) | OK |
| Vista deduplicata per hostname | Dataset sintetico con 30 righe/25 hostname distinti: `hosting_dns_per_hostname_unico` produce dimensione_campione=25 e un HHI leggermente diverso da quello per-ente (30 righe) — la differenza esiste ed è quella attesa | OK |
| Riepilogo errori di misurazione | Verificato sia su file reale (3 righe, 1 errore) sia sintetico (30 righe, ~15% errori): conteggio e tipi corretti | OK |
| `summarize_dmarc`, `summarize_certificati`, `summarize_http_status`, `summarize_giurisdizione_aggregata` | Tutti testati sul dataset sintetico da 30 righe con casi apposta diversificati (policy DMARC miste, certificati scaduti/in scadenza/validi, status 200/403/404/503, giurisdizioni riconosciute e non): valori verificati a mano contro il CSV di input | OK |
| `enti_a_rischio.csv` | Verificato che il punteggio (0-5) e i motivi corrispondano riga per riga alle condizioni nel CSV di input, su un campione con punteggio massimo osservato 5/5 | OK |
| `report_sintesi.md` | Letto il markdown generato: frasi coerenti con i numeri del JSON corrispondente | OK |
| `04_export_enti_esteso.py` con fix ASN | Run end-to-end (31 enti, 30 con dati, 1 senza sito): colonne `asn_a`/`asn_mx`/`asn_ns` verificate senza `.0` nell'output finale (sia .xlsx che .csv) | OK |
| Ordinamento `a`/`aaaa`/`caa` | Verifica di codice (`sorted()` aggiunto, stesso pattern già usato per `ns`/`mx`); non ri-testato con una query DNS reale in questo giro (il comportamento di `sorted()` su una lista di stringhe è deterministico, non serve testarlo contro un server reale) | OK (per costruzione) |
| User-Agent configurabile | Verifica di codice: `config.py` espone `user_agent_di()`, tutti e tre gli script (02/05/06) lo usano; non testato un giro con UA personalizzato contro un sito reale (nessun accesso a internet pubblico in questo ambiente) | Non testato end-to-end su rete reale |
| `pagina_probabilmente_bloccata` | Testato contro un server locale che simula una pagina di blocco Cloudflare realistica (status 403 + "Attention Required" + "Ray ID"): rilevata correttamente. Testati anche 3 casi di non-blocco (200 lungo, 403 lungo senza indizi, 404 corto) per escludere falsi positivi ovvi | OK |

## Bug trovati e corretti in questo giro

Nessun nuovo bug di programmazione trovato durante il testing di queste
modifiche (a differenza del giro precedente, dove il bug degli apici singoli
in `06_fingerprint_cms.py` era emerso testando). Le correzioni qui erano
tutte già identificate correttamente nell'analisi precedente; il lavoro di
questo giro è stato implementarle e verificarle, non scoprirne di nuove.

## Cosa NON è stato implementato (scelta esplicita, non dimenticanza)

- **Script di confronto fra run diverse nel tempo**: implementato solo il
  meccanismo di base (`--salva-storico` in `03_analyze.py`, che copia
  `report.json` con timestamp in `data/results/<paese>/storico/`), non uno
  script che calcola automaticamente la differenza fra due date. Motivo:
  richiede decisioni di design non banali (quali campi confrontare, come
  gestire un campione che cambia dimensione fra due run) che vale la pena
  affrontare quando ci sarà almeno una seconda run reale da confrontare con
  la prima, non in astratto ora.
- **Vista deduplicata per hostname anche in `per_settore`/`per_regione`**:
  implementata solo per le tre sezioni principali (`hosting_dns`,
  `hosting_mail`, `hosting_nameserver`), non per i breakdown per settore/
  regione. Estendibile in un secondo giro se risulta utile in pratica.
- **Confidence interval statistici sulle percentuali** (proposto ma non
  esplicitamente richiesto in questo giro): con un campione di migliaia di
  enti l'incertezza campionaria è marginale rispetto agli errori di
  copertura già segnalati (`affidabilita_copertura`), quindi non è stata la
  priorità di questo giro — resta un'aggiunta ragionevole per un futuro
  giro se il progetto viene usato per pubblicazioni che richiedono rigore
  statistico esplicito.

## Cosa verificare al prossimo giro reale

Con il prossimo `risultati.csv` su un campione più ampio (idealmente
qualche centinaio di righe), guarda in ordine:

1. **Messaggio finale di `02_probe_infra.py`** ("Cache RDAP: N blocchi IP
   distinti interrogati..."): un N molto più basso del numero di righe
   processate conferma che la cache sta funzionando bene su dati reali,
   non solo nel test sintetico.
2. **`report.json` → `errori_misurazione.pct_enti_con_almeno_un_errore`**:
   confrontalo con il valore del giro precedente (senza cache RDAP) sullo
   stesso tipo di campione — dovrebbe essere sensibilmente più basso.
3. **`dark_stack.csv`/`cms_fingerprint.csv` → quante righe hanno
   `pagina_probabilmente_bloccata=True`**: se sono molte, vale la pena
   valutare di cambiare `http_user_agent` in `config.yaml`.
4. **`enti_a_rischio.csv`**: le prime 10-20 righe hanno senso guardandole a
   occhio? Sono il primo posto dove guardare per un'azione concreta.

---

# Audit — ottimizzazioni "PICO" (round 3)

Data: 2026-09-11

Ambito: le 5 voci della lista "Suggested implementation order" concordata
nella bozza di proposta di finanziamento (§1 tassonomia errori/retry/
fallback RDAP, §4 esecuzioni con timestamp, §5 concorrenza/rate limiting,
§3 addizioni di novità — AS-hegemony e concentrazione tracker), applicate a
`dst_ottimizzato`. La voce §2 della stessa lista (resilienza dei fetcher)
riguarda `eu_pa_scraper`, un repository separato non incluso in questo zip,
e non è stata toccata qui.

## Come è stato verificato (e come no)

Stesso limite ambientale dei round precedenti: nessun accesso alla rete
pubblica durante lo sviluppo (solo PyPI/npm/GitHub). Di conseguenza:

- **Verificato con test reali** (server HTTP locali avviati apposta, che
  riproducono la forma di risposta DOCUMENTATA dai servizi reali — RIPEstat,
  IHR): tutta la logica nuova — classificazione errori, retry/backoff, token
  bucket, fallback RIPEstat, `run_id`/puntatore `latest_run.json`
  (compreso il bugfix `--resume` a cavallo di mezzanotte UTC, simulato
  scrivendo un puntatore con una data passata e verificando che venga
  effettivamente ripreso), tracker-HHI (dataset sintetico), e il modulo
  AS-hegemony end-to-end dentro `completa_asn()`.
- **Verificato per lettura/ragionamento, non con una run reale**: il
  comportamento dei servizi esterni REALI (RIPEstat, IHR) quando interrogati
  con traffico e IP/ASN reali del campione italiano — i mock locali
  riproducono la forma di risposta documentata, non garantiscono che il
  servizio si comporti sempre così in produzione (rate limit imprevisti,
  cambi di formato non ancora documentati, ecc.). Prima di un run massivo
  con `asn_ripestat_abilitato: true` (default) o `as_hegemony: true`
  (opt-in), fai un giro con `--limit 20` e controlla la colonna `errori`
  per `ripestat_*`/`as_hegemony_failed` imprevisti.

## Cosa è stato testato, nel dettaglio

| Componente | Test eseguito | Esito |
|---|---|---|
| `errors.classifica_errore` | ~10 nomi di eccezione noti mappati al codice canonico atteso (rate_limited, timeout, dns_non_risolto, tls_fallito, ip_non_pubblico, ...); input vuoto/sconosciuto → `sconosciuto` senza eccezioni | OK |
| `errors.ritenta_con_backoff` | Funzione che fallisce 2 volte con `Timeout` poi riesce → 3 chiamate totali, risultato finale corretto; funzione che fallisce sempre con `IPDefinedError` (non transitorio) → 1 sola chiamata, nessun retry sprecato | OK |
| `rate_limiter.LimitatoreVelocita` | 6 richieste con limite 5/s → le prime 5 quasi istantanee, la 6ª attende ~0,2s (misurato: 0,200s); `richieste_al_secondo=0` → 1000 richieste senza rallentamento misurabile | OK |
| `asn_bulk.resolve_asn_ripestat` | Contro mock HTTP locale che riproduce `network-info`/`as-overview` (verificati sulla documentazione reale di RIPEstat prima di scrivere il codice, non assunti a memoria): caso pieno (ASN+holder+country estratti correttamente), caso senza ASN annunciante, caso rete irraggiungibile — tutti senza eccezioni propagate | OK |
| `asn_bulk.BulkASNResolver` + RIPEstat | Test di integrazione: RIPEstat che risponde → il fallback RDAP finale non viene MAI chiamato (asserzione esplicita); RIPEstat che fallisce → ricade sul fallback RDAP, che a sua volta viene ritentato correttamente dopo un timeout simulato | OK |
| `config.run_id_default`, `aggiorna_puntatore_latest`, `leggi_puntatore_latest` | Formato data corretto; scrittura/lettura del puntatore in una cartella temporanea isolata; aggiornamenti successivi sovrascrivono correttamente | OK |
| `02_probe_infra.py` — run con timestamp end-to-end | Run reale (rete disattivata via flag, non mockata) su 1 hostname sintetico: output in `data/results/ZZ/2026-09-11/risultati.csv` (non più `data/results/ZZ/risultati.csv`), manifest con `run_id`, `latest_run.json` scritto correttamente | OK |
| `03_analyze.py` — run end-to-end su dataset sintetico a 3 enti | Dataset con: un ente pulito, uno con errore ASN + DMARC `none`, uno con più errori (`ns_query_failed`, `a_query_failed`) e tracker Google/Meta/Tawk.to condivisi in modo diseguale fra i 3 enti: `report.json` prodotto senza eccezioni, `errori_misurazione.top10_categorie_errore` presente e corretto (`lookup_http_fallito` per `HTTPLookupError`), `tracker_concentrazione_hhi` presente con HHI/top10/note coerenti con i dati di input verificati a mano, `report_sintesi.md` con il nuovo paragrafo tracker | OK |
| `run_pipeline.py` — coordinamento `run_id` | `subprocess.run` mockato per registrare i comandi senza eseguirli (01 richiede rete pubblica verso IndicePA, non disponibile qui): (1) run normale → tutti i passi condividono lo stesso `run_id`; (2) `--resume` con un puntatore preesistente su una data passata → tutti i passi riprendono quella data, non "oggi" — **questo è il bugfix**, la prima versione scritta in questa stessa sessione non lo gestiva correttamente, corretto prima di finalizzare; (3) `--resume` senza nessun puntatore preesistente (primo run in assoluto) → ricade su "oggi" senza crash | OK (bugfix incluso, vedi sotto) |
| `05_scrape_dark_stack.py` — `organizzazione_madre` | `load_rules()`/`classify()` con regole sintetiche (incluso il caso "nessuna regola trovata"): la colonna `organizzazioni_madri_terzi` risulta parallela a `domini_terzi`, stesso ordine, stesso separatore | OK |
| `data/rules/dark_stack_rules.yaml` con `organizzazione_madre` | Caricato con `yaml.safe_load` dopo la modifica: 31 regole totali, 15 con `organizzazione_madre` esplicita (Google, Meta, Microsoft, Cloudflare — solo dove la proprietà societaria è pubblica e non ambigua), nessuna regola rotta | OK |
| `as_hegemony.hegemony_per_asn` | Contro mock HTTP locale che riproduce la forma REALE di `ihr.iijlab.net/ihr/api/hegemony/` (verificata sulla documentazione del progetto IHR, non su RIPEstat — vedi "Bug trovati" sotto): caso con dipendenza dominante, caso senza dipendenza sopra soglia, caso senza dati, input vuoto/non numerico (nessuna chiamata di rete), rete irraggiungibile — tutti gestiti senza eccezioni | OK |
| `as_hegemony.HegemonyResolver` (cache per ASN) | 3 richieste, 2 ASN distinti → esattamente 2 chiamate reali (verificato contando le chiamate con un monkeypatch) | OK |
| `as_hegemony.colonne_extra_sicure` | Con un resolver che lancia `RuntimeError` deliberatamente → eccezione contenuta, colonne vuote, errore riportato in `as_hegemony_errore` | OK |
| `completa_asn()` + AS-hegemony, integrazione end-to-end | Con un `resolver_asn` finto (ASN fisso 12345) e `HegemonyResolver` puntato al mock IHR: (1) successo → `as_hegemony_dipendenza_principale`/`as_hegemony_quota` scritti correttamente in `row`; (2) `hegemony_resolver=None` (flag disattivato, comportamento di default) → colonne non toccate, nessuna chiamata; (3) ASN senza dati IHR → errore finisce in `row["errori"]` come `as_hegemony_failed:...`, non un'eccezione | OK |
| `config.yaml` (progetto) dopo le modifiche | Ricaricato con `config.load_config()` reale: tutte le nuove chiavi (`asn_ripestat_abilitato`, `asn_rate_limit_per_secondo`, `misurazioni.tracker_hhi`, `misurazioni.as_hegemony`) lette con i valori attesi | OK |
| Tutti gli script (incluso `as_hegemony.py`, `errors.py`, `rate_limiter.py`) | `python -m py_compile` su ciascun file | OK, nessun errore di sintassi |
| Tutti gli script con CLI | `--help` su ciascuno | OK, nessun crash all'avvio |
| Smoke test finale, pipeline completa a rete disattivata | Rilanciato dopo TUTTE le modifiche (incluso AS-hegemony e le nuove colonne in FIELDS) per escludere regressioni: stesso comportamento, header CSV con le 2 nuove colonne `as_hegemony_*` presenti e vuote (flag disattivato di default) | OK |

## Bug trovati e corretti durante il testing (in questa sessione)

1. **Endpoint RIPEstat inesistente per AS-hegemony**: la primissima bozza di
   `src/as_hegemony.py` ipotizzava (da conoscenza generale, non verificata)
   un endpoint `stat.ripe.net/data/as-hegemony/`. Una verifica esplicita
   della documentazione RIPEstat prima di testare il codice ha mostrato che
   quell'endpoint non esiste: la metrica AS Hegemony è calcolata e
   pubblicata da un progetto diverso, IHR (Internet Health Report, IIJ
   Research Lab, `ihr.iijlab.net`), con un'API diversa (per ASN di origine,
   non per IP, e con parametri di intervallo temporale). Il modulo è stato
   riscritto da zero contro la documentazione reale di IHR prima di
   scrivere qualunque test — l'errore è stato individuato e corretto PRIMA
   di essere testato con successo contro un mock, non dopo (il mock
   iniziale, se scritto per l'endpoint sbagliato, avrebbe potuto "passare"
   pur validando un'integrazione che non avrebbe mai funzionato contro il
   servizio reale). Vedi il commento in cima a `src/as_hegemony.py` per il
   dettaglio, lasciato visibile deliberatamente invece di essere rimosso
   silenziosamente.
2. **`--resume` a cavallo di mezzanotte UTC (run_id sbagliato)**: la prima
   versione della propagazione `run_id` (in `run_pipeline.py`,
   `02_probe_infra.py`, `05_scrape_dark_stack.py`, `06_fingerprint_cms.py`)
   calcolava sempre "la data di oggi" come default, anche con `--resume`.
   Per un campione nazionale che può impiegare più di un giorno (vedi
   `stima tempi.txt`: nel pilota, ~46 host in oltre un'ora — su 22.860 host
   IT l'estrapolazione supera abbondantemente le 24 ore), questo avrebbe
   fatto sì che un `--resume` lanciato il giorno dopo aprisse silenziosamente
   una cartella `<run-id>` NUOVA e vuota invece di riprendere quella
   interrotta, vanificando lo scopo stesso di `--resume`. Trovato per
   ragionamento (rileggendo la propria implementazione, non da un test
   fallito) prima di scrivere i test di integrazione per `run_pipeline.py`;
   corretto per tutti e 4 gli script coinvolti con la stessa regola: con
   `--resume` e senza `--run-id` esplicito, il default è l'ultimo run noto
   dal puntatore, non "oggi". Il caso è ora nel test suite di
   `run_pipeline.py` (vedi tabella sopra, riga "coordinamento run_id",
   caso 2) proprio per evitare che regredisca in un giro futuro.
3. **`def summarize_cms_fingerprint(...)` perso in una `str_replace`**: durante
   l'inserimento di `summarize_tracker_hhi()` in `03_analyze.py`, una
   sostituzione di testo mal delimitata ha rimosso la riga `def
   summarize_cms_fingerprint(cf: pd.DataFrame):` lasciando il corpo della
   funzione orfano. Rilevato immediatamente dal successivo `python -m
   py_compile` (che ha fallito), corretto ri-aggiungendo la riga mancante,
   ri-verificato con compilazione pulita prima di proseguire.

## Cosa NON è stato possibile testare qui

- **I servizi esterni reali** (RIPEstat, IHR) sotto le condizioni reali del
  progetto: volume di richieste, tempi di risposta reali, eventuali rate
  limit non documentati, cambi di formato non ancora pubblicati. I mock
  locali riproducono la forma DOCUMENTATA, che è la miglior verifica
  possibile senza accesso alla rete pubblica, ma non equivale a un run
  reale.
- **Il beneficio effettivo in punti percentuali di copertura**: l'obiettivo
  dichiarato nella proposta ("alzare il 45,4%/56,7% del pilota italiano")
  non è verificabile in questo ambiente — richiede un run reale su un
  campione reale per misurare quanto RIPEstat aggiunge sopra il bulk Cymru
  da solo.
- **`05_scrape_dark_stack.py` con RIPEstat+rate limiter su un carico reale
  di domini di terze parti**: la logica è la stessa già testata per
  `02_probe_infra.py` (stesso modulo `asn_bulk.py`), ma non è stata
  ri-testata end-to-end in questo script specifico per limiti di tempo di
  questa sessione — rischio basso (stesso codice condiviso, già verificato
  altrove), ma va comunque incluso nel primo giro con `--limit 20` reale.

## Cosa NON è stato implementato (scelta esplicita, non dimenticanza)

- **§2 della lista concordata (resilienza dei fetcher `eu_pa_scraper`)**:
  riguarda un repository diverso, non incluso in questo zip. Da applicare
  separatamente quando si lavora su quel repository.
- **Riscrittura asyncio completa di DNS/TLS/HTTP** (parte di §5 nella lista
  originale): il progetto usa già `ThreadPoolExecutor` con parallelismo
  configurabile e un limitatore per blocco IP (`asn_bulk.LimitatorePerBlocco`,
  round precedente) — cioè la parte "far tradurre i molti core della CPU in
  throughput reale" della richiesta originale era già sostanzialmente
  soddisfatta prima di questa sessione. Riscrivere da zero su `asyncio` (che
  richiederebbe sostituire `dnspython` sincrono con `dns.asyncresolver`+
  `aiodns`, e il modulo `ssl` sincrono con connessioni asyncio) avrebbe
  un rischio di regressione alto su una pipeline già funzionante, per un
  guadagno marginale rispetto al modello a thread già in uso — non
  implementato deliberatamente, non per mancanza di tempo. Il vero pezzo
  mancante di §5 (rate limiting condiviso verso i servizi RDAP/RIPEstat) è
  stato implementato (`rate_limiter.py`).
- **Sensitivity analysis (HHI con/senza CDN-fronted)**: proposta come voce
  P3 nella lista originale, non nella "suggested implementation order"
  concordata per questo giro — non implementata qui, resta un'estensione
  ragionevole di `summarize()`/`summarize_tracker_hhi()` per un giro futuro.
- **AS-hegemony integrato nel calcolo di concentrazione/report.json**:
  implementato SOLO come due colonne extra in `risultati.csv`
  (`as_hegemony_dipendenza_principale`, `as_hegemony_quota`), non come una
  nuova sezione aggregata in `report.json` — scelta deliberata: è
  sperimentale e spento di default, aggiungere un'intera sezione di report
  per un dato che potrebbe rivelarsi poco affidabile contro il servizio
  reale (mai testato dal vivo) avrebbe anticipato un impegno di design
  (quale soglia? quale aggregazione?) meglio rimandato a quando ci sono
  dati reali da guardare.

## Cosa verificare al prossimo giro reale (in aggiunta ai punti già elencati sopra)

1. **`risultati.csv` → colonna `errori`**: cerca `ripestat_*` (nuovo
   fallback, dovrebbe ridurre gli `asn_*_lookup_failed`, non aggiungerne)
   e, solo se hai attivato `as_hegemony: true`, `as_hegemony_failed`.
2. **Messaggio finale di `02_probe_infra.py`**: ora riporta anche
   `stat_ripestat_ok` insieme a bulk/RDAP — un numero via via crescente
   rispetto al solo RDAP finale è il segnale che il nuovo fallback sta
   aiutando davvero.
3. **`data/results/<paese>/latest_run.json`**: dopo un run completo,
   controlla che `run_id` corrisponda alla cartella che ti aspetti — è il
   file che userai per capire "qual è l'ultimo dato buono" senza dover
   ricordare la data a memoria.
4. **`report.json` → `tracker_concentrazione_hhi.top10_organizzazioni`**:
   con dati reali, quanto è alta la quota "Google"/"Meta"? È il numero più
   direttamente confrontabile con Singh et al. (2026) nella proposta.


