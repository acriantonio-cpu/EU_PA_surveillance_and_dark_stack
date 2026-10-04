#!/usr/bin/env python3
# EN: Run the whole pipeline in sequence: 01 -> 02 -> 07 -> 05 -> 06 -> 08 -> 03 -> 04; which
# measurements run is decided by config.yaml.
"""
run_pipeline.py

Esegue in sequenza i passi della pipeline, comodo per un giro completo senza
lanciare i comandi a mano: 01 (estrazione IPA) -> 02 (DNS/hosting/TLS/HTTP)
-> 07 (ridondanza NS/endpoint, se attivo) -> 05 (dark stack + cookie, se
attivo) -> 06 (software/licenze, se attivo) -> 08 (tipo di ente dal testo
homepage, se attivo) -> 03 (analisi) -> 04 (export per Minitab).

QUALI MISURAZIONI VENGONO FATTE è deciso da config.yaml nella project root
(chiavi sotto 'misurazioni'), non da flag di questo script: i passi 05, 06 e
07 vengono saltati del tutto se il/i loro flag in config.yaml sono a False,
altrimenti vengono lanciati (ed è 02_probe_infra.py/05_scrape_dark_stack.py/
06_fingerprint_cms.py/07_resilience.py stessi a rispettare i flag più
granulari al loro interno). Vedi config.yaml per i dettagli e il costo
relativo di ciascuna misurazione. 07 è SPENTO di default (resilienza_dinamica:
false): a differenza di 05/06, che aggiungono una misurazione nuova, 07
aggiunge CONNESSIONI IN PIÙ verso endpoint già sondati da 02.

Esempio (da terminale VSCode, ambiente virtuale attivo, dalla project root):

    python src\\run_pipeline.py --regione "Lombardia" --limit 20

Il flag --limit è pensato per un primo giro di prova veloce: limita il
numero di enti effettivamente sondati (fasi 2, 5, 6), non quelli estratti da
IPA (fase 1). Toglilo per il campione completo (tutti gli enti d'Italia:
decine di migliaia di host, va lanciato con --resume in caso di
interruzioni). Con --skip-dark-stack, --skip-cms-fingerprint o --skip-http
puoi tagliare drasticamente i tempi se non ti serve tutto subito, oltre a
(o al posto di) editare config.yaml — vedi 02_probe_infra.py,
05_scrape_dark_stack.py e 06_fingerprint_cms.py --help per i dettagli di
ciascun passo.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import config as cfgmod

SRC_DIR = Path(__file__).resolve().parent


def run(cmd: list[str]):
    print(f"\n>>> {' '.join(cmd)}\n", file=sys.stderr)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\nERRORE: il comando ha restituito codice {result.returncode}. Mi fermo qui.", file=sys.stderr)
        sys.exit(result.returncode)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paese", default=None,
        help="Codice paese ISO a due lettere: seleziona le cartelle data/.../<paese> per "
             "tutti i passi (default: valore 'paese' in config.yaml, altrimenti IT se il "
             "file manca o non lo specifica). Con --paese IT viene anche eseguito lo step 01 "
             "(estrazione/mappatura da IndicePA, specifica italiana). Con qualsiasi altro "
             "paese lo step 01 viene SALTATO: data/processed/<paese>/siti.csv deve esistere "
             "già (preparato tu altrove) e non viene toccato dalla pipeline.",
    )
    ap.add_argument("--regione", required=False, help="Nome regione, es. 'Lombardia'")
    ap.add_argument(
        "--categoria", required=False, default=None,
        help="Codice_Categoria IPA, uno o più separati da virgola (es. 'L6,L7'). Default: tutti gli enti.",
    )
    ap.add_argument("--limit", type=int, default=None, help="Limita la sonda (02, 05, 06) a N righe (test rapido)")
    ap.add_argument("--config", default=None, help="Percorso di config.yaml (default: config.yaml nella project root)")
    ap.add_argument("--delay", type=float, default=None, help="Pausa (s) tra un host e il successivo, in 02/05/06 (default: da config.yaml)")
    ap.add_argument("--nameserver", action="append", default=None, help="Vedi 02_probe_infra.py --help")
    ap.add_argument("--concorrenza", type=int, default=None, help="Quanti hostname sondare in parallelo in 02 (default: da config.yaml). Vedi 02_probe_infra.py --help")
    ap.add_argument("--concorrenza-per-provider", type=int, default=None, help="Tetto di connessioni simultanee per blocco IP in 02 (default: da config.yaml). Vedi 02_probe_infra.py --help")
    ap.add_argument("--chunk-size", type=int, default=None, help="Righe per chunk in 02 (default: 200, vedi 02_probe_infra.py --help)")
    ap.add_argument("--asn-metodo", choices=["cymru_bulk", "rdap"], default=None, help="Come risolvere ASN/operatore in 02/05 (default: da config.yaml). Vedi 02_probe_infra.py --help")
    ap.add_argument("--resume", action="store_true", help="Riprende 02, 05 e 06 da dove interrotti (vedi --help dei singoli script)")
    ap.add_argument("--skip-http", action="store_true", help="In 02: salta TLS/HTTP, solo DNS/RDAP (molto più veloce)")
    ap.add_argument("--skip-dark-stack", action="store_true", help="Salta del tutto il passo 05 (terze parti + cookie), anche se attivo in config.yaml")
    ap.add_argument("--skip-cms-fingerprint", action="store_true", help="Salta del tutto il passo 06 (software/licenze), anche se attivo in config.yaml")
    ap.add_argument("--skip-resilienza", action="store_true", help="Salta del tutto il passo 07 (ridondanza NS/endpoint), anche se attivo in config.yaml")
    ap.add_argument("--skip-tipo-ente", action="store_true", help="Salta del tutto il passo 08 (tipo di ente dal testo homepage), anche se attivo in config.yaml")
    ap.add_argument(
        "--run-id", default=None,
        help="OTTIMIZZAZIONE (seconda passata, settembre 2026): identificatore del run, propagato "
             "IDENTICO a tutti i passi (02, 05, 06, 03, 04) così scrivono/leggono tutti dalla stessa "
             "cartella data/results/<paese>/<run-id>/ invece di rischiare che ciascun passo scelga un "
             "default diverso (es. se il run scavalla la mezzanotte UTC). Default: data UTC di quando "
             "PARTE questo comando (non ricalcolata passo per passo).",
    )
    args = ap.parse_args()

    # BUGFIX: il config.yaml va caricato SUBITO dopo il parsing degli
    # argomenti, non più avanti nella funzione — perché --paese deve poter
    # ereditare il suo default dalla chiave 'paese' di config.yaml (non da
    # un "IT" hardcoded in argparse). Precedenza: --paese esplicito da CLI >
    # 'paese' in config.yaml > "IT" come ultima istanza se manca entrambi.
    # Prima di questo fix, args.paese valeva sempre "IT" a meno di passare
    # --paese esplicitamente: il valore in config.yaml veniva letto (poco
    # più sotto) troppo tardi per contare come default di questo argomento.
    cfg = cfgmod.load_config(args.config)
    if args.paese is None:
        args.paese = str(cfg.get("paese", "IT"))

    # OTTIMIZZAZIONE (seconda passata): un solo run_id per l'intera pipeline,
    # calcolato QUI una volta sola e passato esplicitamente a ogni passo —
    # vedi config.py, sezione 'esecuzioni con timestamp', per il perché
    # (in breve: senza questo, ogni script che gira per conto proprio
    # sceglierebbe il proprio default e un run_pipeline.py lanciato appena
    # prima della mezzanotte UTC potrebbe far scrivere 02 in un giorno e 03
    # nel giorno successivo, spezzando il run in due cartelle).
    #
    # BUGFIX (seconda passata): con --resume e senza --run-id esplicito, il
    # default NON deve essere 'oggi' ma l'ultimo run noto dal puntatore. Un
    # campione nazionale (decine di migliaia di host, vedi 'stima tempi.txt'
    # nella project root) può impiegare più di un giorno: un --resume che
    # scavalla la mezzanotte UTC senza questo fix creerebbe silenziosamente
    # una NUOVA cartella <run-id> vuota invece di riprendere quella
    # interrotta, vanificando lo scopo di --resume.
    run_id = args.run_id or (cfgmod.leggi_puntatore_latest(args.paese) if args.resume else None) or cfgmod.run_id_default()
    print(f">>> run_id di questa pipeline: {run_id}\n", file=sys.stderr)

    # cfg già caricato subito dopo il parsing degli argomenti (vedi sopra),
    # per poter risolvere il default di --paese da config.yaml.
    # 05 e 06 vengono lanciati solo se ALMENO uno dei loro flag in config.yaml
    # è attivo (altrimenti lo script si limiterebbe a uscire subito senza
    # scrivere nulla, vedi il messaggio che stampano loro stessi): evitiamo
    # di lanciare un processo che sappiamo già non farà nulla.
    esegui_dark_stack = (
        not args.skip_dark_stack
        and (cfgmod.flag(cfg, "dark_stack_terze_parti") or cfgmod.flag(cfg, "cookie_tracker"))
    )
    esegui_cms_fingerprint = not args.skip_cms_fingerprint and cfgmod.flag(cfg, "cms_fingerprint")
    esegui_resilienza = not args.skip_resilienza and cfgmod.flag(cfg, "resilienza_dinamica")
    esegui_tipo_ente = not args.skip_tipo_ente and cfgmod.flag(cfg, "tipo_ente_classificazione")
    delay = args.delay if args.delay is not None else cfgmod.delay_di(cfg, "probe_infra", 0.5)

    py = sys.executable  # usa lo stesso interprete/venv da cui è lanciato questo script

    paese_upper = args.paese.strip().upper()

    # BUGFIX: lo step 01 (01_fetch_ipa_comuni.py) implementa ESCLUSIVAMENTE
    # l'estrazione italiana da IndicePA (vedi commento in cima a quello
    # script). Prima di questo fix veniva lanciato per qualsiasi --paese: per
    # un paese diverso da IT, non trovando data/raw/<paese>/enti.xlsx, finiva
    # comunque per scaricare il dataset ITALIANO di IndicePA (l'URL è
    # hardcoded, non dipende da --paese) e sovrascriveva
    # data/processed/<paese>/siti.csv con la mappatura italiana convertita —
    # cancellando il siti.csv preparato a mano per quel paese.
    #
    # Per --paese IT: step 01 gira come prima (genera/aggiorna siti.csv da
    # IndicePA). Per qualsiasi altro paese: step 01 viene saltato del tutto e
    # ci si aspetta che data/processed/<paese>/siti.csv esista già (prodotto
    # altrove e incollato lì a mano) — se manca, ci si ferma con un errore
    # invece di generare/sovrascrivere qualcosa di sbagliato.
    if paese_upper == "IT":
        fetch_cmd = [py, str(SRC_DIR / "01_fetch_ipa_comuni.py"), "--paese", args.paese]
        if args.regione:
            fetch_cmd += ["--regione", args.regione]
        if args.categoria:
            fetch_cmd += ["--categoria", args.categoria]
        run(fetch_cmd)
    else:
        siti_csv = SRC_DIR.parent / "data" / "processed" / paese_upper / "siti.csv"
        if args.regione or args.categoria:
            print(
                f"\nATTENZIONE: --regione/--categoria sono ignorati per --paese {paese_upper}: "
                "si applicano solo allo step 01 (estrazione IPA), che è specifico italiano e "
                "qui viene saltato.\n",
                file=sys.stderr,
            )
        print(
            f"\n>>> Step 01 (estrazione IPA) saltato: è specifico per l'Italia. Per --paese "
            f"{paese_upper} mi aspetto che {siti_csv} esista già (preparato altrove e "
            "incollato qui a mano).\n",
            file=sys.stderr,
        )
        if not siti_csv.exists():
            print(
                f"\nERRORE: {siti_csv} non trovato. Per paesi diversi da IT devi fornire tu "
                "il file siti.csv in quella cartella prima di lanciare la pipeline "
                "(non viene generato automaticamente).\n",
                file=sys.stderr,
            )
            sys.exit(1)

    probe_cmd = [py, str(SRC_DIR / "02_probe_infra.py"), "--paese", args.paese, "--delay", str(delay), "--run-id", run_id]
    if args.config:
        probe_cmd += ["--config", args.config]
    if args.limit:
        probe_cmd += ["--limit", str(args.limit)]
    if args.resume:
        probe_cmd += ["--resume"]
    if args.skip_http:
        probe_cmd += ["--skip-http"]
    if args.concorrenza is not None:
        probe_cmd += ["--concorrenza", str(args.concorrenza)]
    if args.concorrenza_per_provider is not None:
        probe_cmd += ["--concorrenza-per-provider", str(args.concorrenza_per_provider)]
    if args.chunk_size is not None:
        probe_cmd += ["--chunk-size", str(args.chunk_size)]
    if args.asn_metodo is not None:
        probe_cmd += ["--asn-metodo", args.asn_metodo]
    for ns in (args.nameserver or []):
        probe_cmd += ["--nameserver", ns]
    run(probe_cmd)

    if esegui_resilienza:
        # Va DOPO 02 (riusa risultati.csv come elenco hostname) e prima di
        # 05/06: nessuna dipendenza dagli altri due, l'ordine qui è solo
        # "appena i dati di 02 sono pronti".
        resilienza_cmd = [py, str(SRC_DIR / "07_resilience.py"), "--paese", args.paese, "--run-id", run_id]
        if args.config:
            resilienza_cmd += ["--config", args.config]
        if args.limit:
            resilienza_cmd += ["--limit", str(args.limit)]
        if args.resume:
            resilienza_cmd += ["--resume"]
        if args.concorrenza is not None:
            resilienza_cmd += ["--concorrenza", str(args.concorrenza)]
        if args.concorrenza_per_provider is not None:
            resilienza_cmd += ["--concorrenza-per-provider", str(args.concorrenza_per_provider)]
        run(resilienza_cmd)
    else:
        print("\n>>> Passo 07 (ridondanza NS/endpoint) saltato: --skip-resilienza, oppure "
              "resilienza_dinamica è False in config.yaml (default).\n", file=sys.stderr)

    if esegui_dark_stack:
        dark_cmd = [py, str(SRC_DIR / "05_scrape_dark_stack.py"), "--paese", args.paese, "--delay", str(delay), "--run-id", run_id]
        if args.config:
            dark_cmd += ["--config", args.config]
        if args.limit:
            dark_cmd += ["--limit", str(args.limit)]
        if args.resume:
            dark_cmd += ["--resume"]
        if args.asn_metodo is not None:
            dark_cmd += ["--asn-metodo", args.asn_metodo]
        for ns in (args.nameserver or []):
            dark_cmd += ["--nameserver", ns]
        run(dark_cmd)
    else:
        print("\n>>> Passo 05 (dark stack/cookie) saltato: --skip-dark-stack, oppure entrambi i flag "
              "dark_stack_terze_parti e cookie_tracker sono False in config.yaml.\n", file=sys.stderr)

    if esegui_cms_fingerprint:
        cms_cmd = [py, str(SRC_DIR / "06_fingerprint_cms.py"), "--paese", args.paese, "--delay", str(delay), "--run-id", run_id]
        if args.config:
            cms_cmd += ["--config", args.config]
        if args.limit:
            cms_cmd += ["--limit", str(args.limit)]
        if args.resume:
            cms_cmd += ["--resume"]
        run(cms_cmd)
    else:
        print("\n>>> Passo 06 (software/licenze) saltato: --skip-cms-fingerprint, oppure "
              "cms_fingerprint è False in config.yaml.\n", file=sys.stderr)

    if esegui_tipo_ente:
        tipo_ente_cmd = [py, str(SRC_DIR / "08_classifica_tipo_ente.py"), "--paese", args.paese, "--delay", str(delay), "--run-id", run_id]
        if args.config:
            tipo_ente_cmd += ["--config", args.config]
        if args.limit:
            tipo_ente_cmd += ["--limit", str(args.limit)]
        if args.resume:
            tipo_ente_cmd += ["--resume"]
        run(tipo_ente_cmd)
    else:
        print("\n>>> Passo 08 (tipo di ente dal testo homepage) saltato: --skip-tipo-ente, oppure "
              "tipo_ente_classificazione è False in config.yaml.\n", file=sys.stderr)

    run([py, str(SRC_DIR / "03_analyze.py"), "--paese", args.paese, "--run-id", run_id])

    # BUGFIX: 04_export_enti_esteso.py parte da data/raw/<paese>/enti.xlsx,
    # il file COMPLETO di IndicePA prodotto solo dallo step 01
    # (01_fetch_ipa_comuni.py), che a sua volta è specifico italiano e viene
    # eseguito solo per --paese IT (vedi il blocco dello step 01 più sopra).
    # Per qualsiasi altro paese quel file non esiste e non può esistere,
    # quindi non ha senso lanciare questo step: prima di questo fix veniva
    # comunque eseguito, falliva con enti.xlsx non trovato e run() (vedi
    # sopra) interrompeva l'intera pipeline con "codice 1", anche se tutti
    # gli step precedenti (02/05/06/07/08/03) erano andati a buon fine e i
    # loro risultati erano già scritti su disco.
    if paese_upper == "IT":
        run([py, str(SRC_DIR / "04_export_enti_esteso.py"), "--paese", args.paese, "--run-id", run_id])
    else:
        print(
            f"\n>>> Step 04 (export enti_esteso.xlsx) saltato: si basa su data/raw/{paese_upper}/enti.xlsx, "
            "che esiste solo per l'Italia (prodotto dallo step 01, specifico IndicePA). Per "
            f"{paese_upper} usa direttamente i file in data/results/{args.paese}/{run_id}/ "
            "(risultati.csv, tipo_ente.csv, dark_stack.csv, cms_fingerprint.csv, report.json...).\n",
            file=sys.stderr,
        )

    if paese_upper == "IT":
        print(
            f"\nFatto. Guarda data/results/{args.paese}/{run_id}/report.json per il riepilogo e "
            f"data/results/{args.paese}/{run_id}/enti_esteso.xlsx per l'estensione di enti.xlsx da usare in Minitab.",
            file=sys.stderr,
        )
    else:
        print(
            f"\nFatto. Guarda data/results/{args.paese}/{run_id}/report.json per il riepilogo "
            f"(niente enti_esteso.xlsx per {paese_upper}, vedi messaggio dello step 04 sopra).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
