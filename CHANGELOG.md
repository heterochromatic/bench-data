# Changelog

## v0.1.0 (2026-11-04, not yet run against live sources)

- Commands: `formd`, `officers`, `officers-truth`, `firmographics`, `jobs`, `press-contacts`, `events`, `uk`.
- Sources: SEC EDGAR (full index, submissions, Forms 3/4, Form D, 8-K Item 5.02, 10-K, 8-K exhibit 99), Greenhouse/Lever/Ashby job boards, Wikidata, UK Companies House.
- One HTTP client: per-host pacing (SEC at most ~7 requests a second), retries on 429/5xx, optional disk cache (`BENCHDATA_CACHE`).
- Offline tests on recorded fixtures for every command.

## v0.1.1 (2026-11-04)

- First live runs. Form D skips investment funds by default; Wikidata paced and failure-tolerant; `firmographics` reads `formd-filings.csv`.
- New `domains` command: company domains from the company's own homepage (title must name the company), plus `contacts.candidates.csv` ready to enrich.
