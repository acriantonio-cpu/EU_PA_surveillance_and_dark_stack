# Come scaricare i siti della Pubblica Amministrazione italiana

La fonte è **IndicePA**, il registro ufficiale gestito dall'Agenzia per
l'Italia Digitale (AgID), licenza Creative Commons CC-BY 4.0. Contiene
oltre 20.000 enti (comuni, ASL, scuole, università...) con il loro sito
istituzionale.

## Opzione A — lascia fare allo script (più comodo, prova questa per prima)

`src/01_fetch_ipa_comuni.py` prova a scaricare da solo il file da IndicePA
la prima volta che lo lanci. Se il download va a buon fine, non devi fare
nulla: lo script scrive automaticamente `data/raw/IT/enti.xlsx` e lo riusa nei
run successivi senza riscaricarlo.

```powershell
python src\01_fetch_ipa_comuni.py --regione "Lombardia"
```

Se vedi un errore di rete o di download, passa all'Opzione B.

## Opzione B — download manuale (più affidabile, consigliata se A fallisce)

I portali open data a volte bloccano richieste automatiche, o cambiano
struttura senza preavviso: il download manuale è più robusto.

1. Apri nel browser: **https://indicepa.gov.it/ipa-dati/dataset/enti**
2. Cerca la risorsa chiamata **"Enti"**, formato **XLSX** (dimensione
   circa 4 MB), e clicca sul pulsante di download.
3. Rinomina il file scaricato in `enti.xlsx` (se non si chiama già così).
4. **Spostalo nella cartella `data\raw\` del progetto**, così:

   ```
   dark_stack_toolkit\
   └── data\
       └── raw\
           └── enti.xlsx   ← qui
   ```

   In VSCode: trascina il file dal Esplora file di Windows direttamente
   nella cartella `data/raw/IT` visibile nel pannello a sinistra di VSCode.

5. Rilancia lo script — a questo punto lo trova da solo, senza bisogno di
   parametri aggiuntivi:

   ```powershell
   python src\01_fetch_ipa_comuni.py --regione "Lombardia"
   ```

   Se preferisci indicare esplicitamente il percorso (utile se lo hai
   salvato altrove, es. nei Download):

   ```powershell
   python src\01_fetch_ipa_comuni.py --regione "Lombardia" --enti-file "C:\Users\TUONOME\Downloads\enti.xlsx"
   ```

## Come leggere il file una volta scaricato

`enti.xlsx` ha una riga per ogni ente pubblico italiano registrato in IPA.
Le colonne che lo script usa:

| Colonna              | Cosa contiene                                             |
| --------------------- | ---------------------------------------------------------- |
| `Codice_IPA`          | Codice identificativo univoco dell'ente                    |
| `Denominazione_ente`  | Nome dell'ente, es. "Comune di Milano"                      |
| `Tipologia`           | Categoria dell'ente — i comuni hanno "Comuni e loro Consorzi e Associazioni" |
| `Sito_istituzionale`  | L'indirizzo del sito web dell'ente — è il dato che ci interessa |

Lo script filtra automaticamente solo le righe con `Tipologia` che contiene
"comun" (case-insensitive), quindi non devi selezionare nulla a mano.

## Un secondo file scaricato automaticamente: comuni.json

Per sapere in quale **regione** ricade ogni comune (informazione che il
dataset IPA non contiene direttamente), lo script scarica anche
un'anagrafica comuni→regione da GitHub (progetto open
`matteocontrini/comuni-json`, dati basati su ISTAT). Anche questo file
viene salvato automaticamente in `data/raw/IT/comuni.json` al primo utilizzo
e riusato nei run successivi — non devi scaricarlo a mano, a meno che tu
non abbia problemi di rete anche con GitHub, nel qual caso puoi scaricarlo
da https://github.com/matteocontrini/comuni-json (file `comuni.json` nella
root del repository) e metterlo in `data/raw/IT/comuni.json`.

## Domande frequenti

**"Non trovo la mia regione, lo script dice errore."**
Controlla di aver scritto il nome della regione come compare
ufficialmente (es. "Emilia-Romagna" con il trattino, "Trentino-Alto Adige"
per esteso). Lo script, se non trova corrispondenze, stampa l'elenco
completo delle regioni disponibili nell'anagrafica: copia il nome esatto
da lì.

**"Il file XLSX scaricato è vecchio/aggiornato, come faccio a sapere
quando è stato aggiornato l'ultima volta?"**
La pagina IndicePA riporta la data di aggiornamento del dataset. Per la
riproducibilità della misurazione, conviene annotare quella data insieme
ai tuoi risultati (lo trovi anche nel manifesto che genera
`02_probe_infra.py`, anche se lì viene registrato il timestamp della
*misurazione*, non quello del dataset IPA — se vuoi tracciare anche
quest'ultimo, aggiungilo a mano in una nota insieme ai risultati).
