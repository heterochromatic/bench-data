# bench-data

Builds benchmark inputs and **candidate** answer keys from free, first-hand public records. Standard library only. Every row it writes carries its `source_url` and `retrieved_at`.

Its output is never an answer key on its own. Per each bench repo's `ANSWER-KEYS.md`: review the rows the two sources disagree on, run `verify-sources`, then copy the reviewed rows to the key file.

**Status:** used live to build the inputs and answer keys for [clay-waterfall-enrichment-benchmark](https://github.com/promptingcompany/clay-waterfall-enrichment-benchmark) and for a study that isn't published (November 2026); the unit tests run offline against recorded responses.

## Setup

```bash
export SEC_CONTACT="Your Name you@example.com"     # required: sent as the contact in every request (the SEC requires one)
export COMPANIES_HOUSE_KEY=...                     # free key, only for the `uk` command
export BENCHDATA_CACHE="$PWD/cache"                # optional: never fetch the same URL twice
python3 -I -m unittest discover -s tests
```

Write outputs into the bench repo's gitignored `private/` folder, never into a committed folder:

```bash
OUT=../clay-waterfall-enrichment-benchmark/private/datasets/candidates
```

## Commands

| Command | Source | Writes | What it builds |
|---|---|---|---|
| `formd --since --until [--limit 300]` | SEC Form D | `formd-filings.csv`, `formd-executives.csv` (executive officers only) | Contacts at private US companies for an enrichment benchmark; a record of recent funding rounds |
| `officers --since --until [--limit 500]` | SEC Forms 3/4 in a past window | `officers-seed.csv`, `t3-seed-export.candidates.csv` | Starting list for a CRM-refresh task: officers as a stale CRM held them |
| `officers-truth --seed officers-seed.csv` | Each officer's latest Form 3/4; the seed company's 8-K Item 5.02s | `t3-ground-truth.candidates.csv` | Key for the CRM-refresh task: current title and company, changed Y/N/unknown |
| `firmographics --ciks file [--headcount] [--wikidata]` | SEC submissions, latest 10-K, Wikidata | `firmographics.candidates.csv` | Firmographic key: website, industry, HQ country and headcount |
| `jobs --companies file` | Greenhouse, Lever and Ashby public boards | `job-evidence.csv` | Open SDR/BDR roles per company, as evidence for an account-list task |
| `press-contacts --ciks file --since` | Named emails in 8-K press-release exhibits | `press-contacts.candidates.csv` | Known-email (gold) candidates; on-domain flag |
| `events --ciks watchlist --since --until` | SEC 8-K Item 5.02 and Form D | `t4-events.candidates.csv` | Log of officer changes and funding rounds for an alerting task, run daily during the window |
| `public-contacts --since --until [--limit 600] [--per-company 2]` | SEC Forms 3/4, submissions and the latest 10-K | `public-contacts.candidates.csv` | Current officers at public companies with domain (SEC record → website stated in the 10-K → homepage), size cell from the 10-K headcount and region: the waterfall benchmark's mid-market and enterprise rows |
| `domains --companies file [--executives file]` | The company's own homepage | `domains.candidates.csv` (match / review / unknown), `contacts.candidates.csv` | Domains for Form D companies (they have none on file); contacts ready to enrich: first name, last name, company domain |
| `uk --numbers file` | UK Companies House | `uk-companies.csv`, `uk-officers.csv` | EMEA contacts; officer resignations as job changes |
| `uk-contacts [--sic codes] [--company-type ltd] [--companies 300] [--per-company 2]` | UK Companies House (search, officers, filed accounts) and the company's own homepage | `uk-contacts.candidates.csv` | Current directors at active UK companies, with domain and accounts headcount: the waterfall benchmark's EMEA rows |
| `sic-companies --sic 7370-7374 --since --until [--limit 400]` | SEC full index, submissions and the latest 10-K | `sic-companies.candidates.csv` | Companies in a SIC range that filed a 10-K, with website and headcount, e.g. a pool of software companies for an account list |
| `t3-crm-seed --seed officers-seed.csv --key t3-ground-truth.csv [--pairs 25]` | The seed and the reviewed key from `officers-truth` (no fetching) | `t3-crm-seed.csv`, `t3-seed-export.csv` | HubSpot import for the CRM-refresh task: names, plus seeded duplicate pairs outside the key |
| `w1-assemble --candidates folder [--cells ...] [--cap 400]` | Candidate files already built (no fetching) | `w1-input.csv`, `w1-input-sources.csv`, `w1-email-gold.candidates.csv`, `w1-firmographics-truth.candidates.csv` | Contact list for the waterfall benchmark (its task W1), capped per size × region cell |
| `t1-assemble --candidates folder [--exclude w1-input.csv] [--rows 500]` | Candidate files already built (no fetching) | `t1-input.csv`, `t1-input-sources.csv`, `t1-email-gold.candidates.csv` | Contact list for an enrichment benchmark: people at matched domains, none at a company in the `--exclude` file |
| `t5-assemble --candidates folder[,folder] [--rows 1000] [--software 400]` | Candidate files already built (no fetching) | `t5-domains.csv`, `t5-companies.csv`, `t5-ground-truth.candidates.csv` | Account list with ICP labels (50–500 employees, software, HQ in North America), software companies first |
| `verify-sources --file key.candidates.csv` | Each row's cited source, fetched again | `<key>.verified.csv`, `<key>.rejected.csv` | Every answer-key row re-checked against its cited source |

In command and file names, `w1-` is the waterfall benchmark's task and `t1-` to `t5-` are task numbers from the unpublished study.

Every command also takes `--out <folder>`. Run `python3 -m benchdata <command> -h` for all options.

Input files are plain CSVs with a `cik`, `domain` (and optional `name`) or `company_number` column.

## Example: the seed and key for a CRM-refresh task

```bash
python3 -m benchdata officers --since 2025-05-01 --until 2025-10-31 --limit 500 --out $OUT
python3 -m benchdata officers-truth --seed $OUT/officers-seed.csv --out $OUT
```

Then review every row marked `unknown`, and every row where the seed company filed an officer-change 8-K but `changed` is N.

## How the candidates are built

- **Random, not alphabetical:** index files are sorted by company name, so commands sample filings in a fixed random order (`--seed`).
- **Unknown, not guessed:** a value without first-hand evidence is written as `unknown`. For example, an officer with no Form 3/4 in the last 270 days is unknown, not unchanged.
- **Domains:** guessed from the company name (`.com`, `.ai`, `.io`, `.co`), and accepted only when the homepage's title or site name contains every word of the company name. Parked "for sale" pages are rejected. A partial match is marked `review`, nothing found is `unknown`. Guesses are checked 4 at a time per company and 8 companies at a time (`--workers`), with per-host pacing kept; a site that answers with a rate limit or error gets one retry.
- **10-K reading:** the website is the first company domain the 10-K states near "website" (investor-relations and `www.` prefixes dropped; SEC, exchanges and social sites ignored). Headcount is the first "N employees" statement, searched from the Human Capital section, with only words like "full-time" or "approximately" allowed between the number and the noun. On 555 companies it found domains for 546 of 600 officers and headcount for 459.
- **Press contacts:** generic inboxes (`ir@`, `press@`, ...) are dropped, and each row says whether the email is on the company's own domain. Agency contacts drop out under the benchmarks' domain check.

## Limits

- SEC filers lean towards public and regulated companies, and towards executives, whom vendors find more easily. Form D (private companies) and Companies House (UK) help balance it. Disclose the mix.
- APAC coverage (Korea DART, Japan EDINET, NZ Companies Office, ASX notices) isn't built yet.
- `EDGAR_COUNTRY` maps a few common non-US EDGAR location codes; others come out blank for review.

## Licence

MIT.
