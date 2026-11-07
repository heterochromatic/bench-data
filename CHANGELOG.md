# Changelog

## v0.1.0 (2026-11-04, not yet run against live sources)

- Commands: `formd`, `officers`, `officers-truth`, `firmographics`, `jobs`, `press-contacts`, `events`, `uk`.
- Sources: SEC EDGAR (full index, submissions, Forms 3/4, Form D, 8-K Item 5.02, 10-K, 8-K exhibit 99), Greenhouse/Lever/Ashby job boards, Wikidata, UK Companies House.
- One HTTP client: per-host pacing (SEC at most ~7 requests a second), retries on 429/5xx, optional disk cache (`BENCHDATA_CACHE`).
- Offline tests on recorded fixtures for every command.

## v0.1.1 (2026-11-04)

- First live runs. Form D skips investment funds by default; Wikidata paced and failure-tolerant; `firmographics` reads `formd-filings.csv`.
- New `domains` command: company domains from the company's own homepage (title must name the company), plus `contacts.candidates.csv` ready to enrich.
- Domain finder in parallel (4 guesses per company, 8 companies at once; `--workers`): 20 companies in ~7 s instead of ~2.5 min, same results. Thread-safe per-host pacing; one retry when a site answers 403/429/5xx; no wasted wait after a final failed attempt; `FetchError.status`.

## v0.1.2 (2026-11-05)

- `public-contacts`: officers at SEC filers with domain, size and region.
- 10-K reading: company website (`website_from_10k`) and a looser, guarded headcount reader (`headcount_from_10k`). On the 555-company pull: domains 323 → 546 of 600 officers, headcount 338 → 459.
- SEC paced at ~4/s with long 429 backoff; failed Form D filings skipped; more parked-page patterns.
- Headcount fixes (2026-11-05): `headcount_from_10k` now prefers the company's own statement ("we had 2,100 employees"), takes the full-time count from "723 full-time and 32 part-time", and skips department, union and growth figures; it had misread 70 of 787 10-K headcounts (part-time counts, acquired companies). `verify-sources` now needs the number as a whole number near a word for staff, not anywhere in the filing, and reads the tagged headcount in UK accounts. T5: "IT services" industry codes (SIC 7373/7374) count as unknown, not as proof a company isn't software.
- `t3-crm-seed` (2026-11-06): the T3 HubSpot seed with first and last names, plus seeded duplicate pairs (a middle initial, or the company suffix dropped) built only from rows outside the answer key.
