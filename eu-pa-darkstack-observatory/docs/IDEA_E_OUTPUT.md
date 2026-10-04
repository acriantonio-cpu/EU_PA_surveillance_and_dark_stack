# L'idea, spiegata semplice

> **English summary: the idea in plain words.** Every municipality, hospital or school website depends on third-party technical providers: hosting, DNS and mail. Nobody has mapped these providers at national scale, or what would happen if one of them failed. The project builds that map from public technical data only. It does not log in, does not touch personal data, and measures the site, not the people. It reports:
> - who runs DNS and mail, and how many operators each entity has (one operator means no backup);
> - where servers are hosted, by company and country;
> - whether basic protections are active (DNSSEC, SPF, DMARC).
>
> The output is per-entity data (`risultati.csv`) plus headline indicators (`report.json`): the HHI concentration index (0–10,000, above 2,500 counts as highly concentrated), the top-10 operators (the "blast radius" if one fails), adoption rates of the protections, and the share of entities without redundancy. The last section says what the tool does **not** do. *(Written at an early stage: third-party tracker detection, mentioned there as future work, has since been implemented in `05_scrape_dark_stack.py`.)*

## Il problema

Ogni sito web di un comune, ospedale o scuola pubblica italiana si appoggia
a servizi tecnici gestiti da aziende terze: chi tiene online il sito (l'hosting),
chi gestisce l'indirizzo internet del sito (il DNS), chi fa arrivare la posta
elettronica dell'ente (il server mail). Queste scelte le fa ogni ente per
conto proprio, spesso senza un quadro d'insieme — e nessuno oggi ha mai
mappato, su scala nazionale, chi sono davvero questi fornitori e cosa
succederebbe se uno di loro avesse un problema.

Il progetto costruisce quella mappa, partendo dai comuni italiani e
misurando i dati tecnici pubblici (non serve autenticarsi, non si toccano
dati personali di nessun cittadino: si misura il sito, non le persone).

## Cosa misura, in pratica

Per ogni comune, il programma controlla (senza mai "entrare" nel sito, solo
interrogando il sistema dei nomi a dominio di internet, un po' come chiedere
"in che quartiere abita questo indirizzo"):

- **Chi gestisce il DNS** (il "centralino" che traduce il nome del sito nel
  suo indirizzo tecnico) e quanti operatori diversi lo fanno — un solo
  operatore vuol dire: se quell'azienda ha un problema, il sito sparisce da
  internet.
- **Chi gestisce la posta elettronica** dell'ente, stesso ragionamento.
- **Dove sono ospitati i server** (in termini di azienda proprietaria
  dell'indirizzo internet, e in che paese ha sede quell'azienda).
- **Se il sito ha attivato protezioni di base**: DNSSEC (protezione contro
  la falsificazione del DNS), SPF e DMARC (protezioni contro chi manda mail
  false spacciandosi per l'ente — un vettore comune di phishing verso i
  cittadini).

## Cosa restituisce alla fine

Tre file, uno via via più "leggibile":

1. **`data/processed/IT/siti.csv`** — l'elenco pulito dei siti dei comuni di
   una regione, con URL normalizzati. È il dato di partenza, poco
   interessante da solo.

2. **`data/results/IT/risultati.csv`** — un rigo per comune, con tutte le
   misure tecniche elencate sopra. È il dato grezzo: utile per chi vuole
   fare le proprie analisi, poco leggibile per chi vuole solo capire "come
   siamo messi".

3. **`data/results/IT/report.json`** — il numero di sintesi, quello che va
   in un titolo di giornale o in un grafico di una proposta di progetto:

   - **Indice di concentrazione (HHI)**: un numero da 0 a 10.000 che dice
     quanto pochi operatori si spartiscono il mercato dell'hosting/mail dei
     comuni misurati. Più il numero è alto, più la dipendenza è concentrata
     su poche aziende — un valore sopra 2.500 è considerato "concentrazione
     alta" negli standard usati per valutare i mercati.
   - **Top 10 operatori**: quali aziende ospitano più comuni, e quanti.
     Questo è il dato che risponde alla domanda "se cade *quest'azienda*,
     quanti comuni smettono di funzionare online?" (il "raggio d'impatto"
     di cui parla il piano di progetto).
   - **Percentuali di adozione delle protezioni di base**: quanti comuni su
     100 hanno DNSSEC attivo, quanti hanno SPF/DMARC contro il phishing via
     mail.
   - **Quota di comuni senza ridondanza**: quanti comuni dipendono da un
     solo operatore per il DNS, senza alternative in caso di guasto.

## Perché è utile a qualcuno oltre a te

- **A un cittadino/giornalista**: perché trasforma "il digitale della PA è
  fragile" — un'affermazione generica che si sente spesso — in un numero
  verificabile: "il 40% dei comuni della Lombardia dipende da un unico
  fornitore DNS, senza alternative".
- **A un ente pubblico**: perché il report gli dice, in modo oggettivo,
  se ha un problema di concentrazione o di protezioni mancanti — cosa che
  oggi nessun singolo comune ha gli strumenti per scoprire da solo.
- **A un decisore pubblico (a livello regionale o nazionale)**: perché
  ripetendo la misura nel tempo (ogni 3-6 mesi) si vede se la situazione
  migliora o peggiora, e dove concentrare eventuali interventi.

## Cosa NON fa (per essere chiari fin da subito)

- Non entra nei siti, non raccoglie dati di chi li visita, non tocca dati
  personali di nessun cittadino.
- Non dice se un sito è "bello" o "brutto" da usare: misura solo
  l'infrastruttura tecnica sotto al sito, non l'esperienza utente.
- Non è (ancora, in questa prima versione) uno strumento che rileva i
  tracker di terze parti caricati dal sito (Google Analytics, font esterni,
  ecc.) — quella è una fase successiva del progetto più ampio, che richiede
  un approccio diverso (un browser automatico, non solo query DNS).
