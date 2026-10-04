# Sintesi — osservatorio infrastruttura pubblica (IT)

Generato il 2026-09-11 20:51 UTC su 500 enti misurati.

**Hosting web**: 71 operatori distinti, indice di concentrazione HHI 1049.6 (non concentrato). Copertura del dato: 93.0% (alta).
Il primo operatore per numero di enti è **ARUBA-ASN Aruba S.p.A.** con il 27.1%.

**Posta elettronica**: HHI 1939.1 (moderatamente concentrato), copertura 93.2%.

**DMARC**: presente sul 60.6% degli enti, ma con policy che blocca davvero lo spoofing (reject/quarantine) solo su 11.8% — il 48.6% ha DMARC solo in modalità osservazione (policy 'none').

**DNSSEC** attivo sul 2.0% del campione.

**Ridondanza DNS**: 54.0% degli enti ha un solo operatore di nameserver (nessun backup).

**Qualità del campione**: 11.2% delle righe ha almeno un errore di misurazione — da tenere presente leggendo le percentuali sopra.

**Certificati TLS**: 7 già scaduti, 33 in scadenza entro 30 giorni — questi due gruppi meritano un controllo prioritario.

**Giurisdizione hosting**: la macro-area più comune è **Italia** con il 36.4% del campione.

**Concentrazione tracker/terze parti**: HHI 678.8 (non concentrato) su 319 organizzazioni madri distinte (376 enti con almeno un tracker rilevato).
La prima organizzazione per numero di enti è **Google** con il 60.6%.

**Software/licenze**: software riconosciuto su 82.2% degli enti, di cui 78.2% interamente open source (fra quelli con software riconosciuto).

---
*Generato automaticamente da 03_analyze.py. Per il dettaglio completo vedi report.json; per gli enti da controllare per primi vedi enti_a_rischio.csv.*