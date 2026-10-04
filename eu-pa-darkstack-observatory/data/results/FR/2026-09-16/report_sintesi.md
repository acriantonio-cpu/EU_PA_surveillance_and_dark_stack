# Sintesi — osservatorio infrastruttura pubblica (FR)

Generato il 2026-09-16 22:21 UTC su 2000 enti misurati.

**Hosting web**: 119 operatori distinti, indice di concentrazione HHI 1356.0 (non concentrato). Copertura del dato: 99.0% (alta).
Il primo operatore per numero di enti è **OVH OVH SAS** con il 33.1%.

**Posta elettronica**: HHI 1462.7 (non concentrato), copertura 29.9%.

**DMARC**: presente sul 7.4% degli enti, ma con policy che blocca davvero lo spoofing (reject/quarantine) solo su 2.7% — il 4.7% ha DMARC solo in modalità osservazione (policy 'none').

**DNSSEC** attivo sul 5.1% del campione.

**Ridondanza DNS**: 95.4% degli enti ha un solo operatore di nameserver (nessun backup).

**Qualità del campione**: 8.0% delle righe ha almeno un errore di misurazione — da tenere presente leggendo le percentuali sopra.

**Certificati TLS**: 4 già scaduti, 253 in scadenza entro 30 giorni — questi due gruppi meritano un controllo prioritario.

**Giurisdizione hosting**: la macro-area più comune è **Francia** con il 32.8% del campione.

**Concentrazione tracker/terze parti**: HHI 614.8 (non concentrato) su 1001 organizzazioni madri distinte (1267 enti con almeno un tracker rilevato).
La prima organizzazione per numero di enti è **Google** con il 64.5%.

**Software/licenze**: software riconosciuto su 69.8% degli enti, di cui 69.4% interamente open source (fra quelli con software riconosciuto).

---
*Generato automaticamente da 03_analyze.py. Per il dettaglio completo vedi report.json; per gli enti da controllare per primi vedi enti_a_rischio.csv.*