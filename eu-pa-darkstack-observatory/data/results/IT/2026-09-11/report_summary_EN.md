# Summary: public-infrastructure observatory (IT)

> English translation of `report_sintesi.md`, which `03_analyze.py` generates automatically. The figures are unchanged.

Generated on 2026-09-11 at 20:51 UTC from 500 measured entities.

**Web hosting:** 71 distinct operators. The HHI concentration index is 1049.6 (not concentrated), and data coverage is 93.0% (high).
The largest operator by number of entities is **ARUBA-ASN Aruba S.p.A.**, with 27.1%.

**Email:** the HHI is 1939.1 (moderately concentrated), with 93.2% coverage.

**DMARC:** present on 60.6% of entities. Only 11.8% have a policy that actually blocks spoofing (reject or quarantine). The other 48.6% run DMARC in observation mode only (policy `none`).

**DNSSEC:** active on 2.0% of the sample.

**DNS redundancy:** 54.0% of entities rely on a single nameserver operator, with no backup.

**Sample quality:** 11.2% of rows have at least one measurement error. Keep this in mind when reading the percentages above.

**TLS certificates:** 7 have already expired and 33 expire within 30 days. These two groups should be checked first.

**Hosting jurisdiction:** the most common macro-area is **Italy**, with 36.4% of the sample.

**Tracker/third-party concentration:** the HHI is 678.8 (not concentrated), across 319 distinct parent organisations. 376 entities have at least one tracker detected.
The largest organisation by number of entities is **Google**, with 60.6%.

**Software/licences:** software was recognised on 82.2% of entities. Of those, 78.2% run entirely open-source software.

---
*Full detail is in `report.json`. The entities to check first are listed in `enti_a_rischio.csv` (entities at risk).*
