# Summary: public-infrastructure observatory (FR)

> English translation of `report_sintesi.md`, which `03_analyze.py` generates automatically. The figures are unchanged.

Generated on 2026-09-16 at 22:21 UTC from 2,000 measured entities.

**Web hosting:** 119 distinct operators. The HHI concentration index is 1356.0 (not concentrated), and data coverage is 99.0% (high).
The largest operator by number of entities is **OVH SAS**, with 33.1%.

**Email:** the HHI is 1462.7 (not concentrated), with 29.9% coverage.

**DMARC:** present on 7.4% of entities. Only 2.7% have a policy that actually blocks spoofing (reject or quarantine). The other 4.7% run DMARC in observation mode only (policy `none`).

**DNSSEC:** active on 5.1% of the sample.

**DNS redundancy:** 95.4% of entities rely on a single nameserver operator, with no backup.

**Sample quality:** 8.0% of rows have at least one measurement error. Keep this in mind when reading the percentages above.

**TLS certificates:** 4 have already expired and 253 expire within 30 days. These two groups should be checked first.

**Hosting jurisdiction:** the most common macro-area is **France**, with 32.8% of the sample.

**Tracker/third-party concentration:** the HHI is 614.8 (not concentrated), across 1,001 distinct parent organisations. 1,267 entities have at least one tracker detected.
The largest organisation by number of entities is **Google**, with 64.5%.

**Software/licences:** software was recognised on 69.8% of entities. Of those, 69.4% run entirely open-source software.

---
*Full detail is in `report.json`. The entities to check first are listed in `enti_a_rischio.csv` (entities at risk).*
