# Results of the preliminary runs

These are real measurement runs. They produced the figures quoted in the Restack application.

| Country | Run | Entities | English summary |
|---|---|---|---|
| Italy | `IT/2026-09-11` | 500 | [`report_summary_EN.md`](IT/2026-09-11/report_summary_EN.md) |
| France | `FR/2026-09-16` | 2,000 | [`report_summary_EN.md`](FR/2026-09-16/report_summary_EN.md) |
| Italy | `IT/2026-09-15` | 20 | 20-host test of step 02 only (the output file is empty). It was not used in the application. |

File names and column headers are in Italian. They are translated in the [glossary](../../../GLOSSARY.md#output-files-eu-pa-darkstack-observatorydataresultscountryrun_date).

Notes for reviewers:
- Every automated flag means **"to be verified"**, never a verdict. This applies especially to `terze_parti_da_verificare.*` (third parties to verify).
- In the France run, `regione` holds the département number and `codice_ipa` holds the UUID from the French *Annuaire de l'administration*. The same national site (for example `www.impots.gouv.fr`) can appear for several local offices.
- Public Google API keys that PA pages embed in third-party URLs are replaced with `AIza_REDACTED`.
- Backups, discarded rows (`*_scartate_hostname_invalido.csv`) and large regenerable inputs are not published.
