# bench-data

Builds benchmark inputs and **candidate** answer keys from free, first-hand public records. Standard library only. Every row it writes carries its `source_url` and `retrieved_at`.

Its output is never an answer key on its own. Per each bench repo's `ANSWER-KEYS.md`: review the rows the two sources disagree on, spot-check 20 random rows by hand, then copy the reviewed rows to the key file.

**Status:** tested offline against recorded responses only. Nothing has been fetched from a live source yet.

## Setup

```bash
export SEC_CONTACT="Your Name you@example.com"     # the SEC requires a contact in every request
export COMPANIES_HOUSE_KEY=...                     # free key, only for the `uk` command
export BENCHDATA_CACHE="$PWD/cache"                # optional: never fetch the same URL twice
python3 -I -m unittest discover -s tests
```

Write outputs into the bench repo's gitignored `private/` folder, never into a committed folder:

```bash
OUT=../clay-bench-enrichment-waterfall/private/datasets/candidates
```

## Commands

| Command | Source | Writes | Feeds |
|---|---|---|---|
| `formd --since --until [--limit 300]` | SEC Form D | `formd-filings.csv`, `formd-executives.csv` (executive officers only) | T1 and W1 contacts at private US companies; T2 funding reference |
| `officers --since --until [--limit 500]` | SEC Forms 3/4 in a past window | `officers-seed.csv`, `t3-seed-export.candidates.csv` | T3 seed (officers as a stale CRM held them) |
| `officers-truth --seed officers-seed.csv` | Each officer's latest Form 3/4; the seed company's 8-K Item 5.02s | `t3-ground-truth.candidates.csv` | T3 key: current title and company, changed Y/N/unknown |
| `firmographics --ciks file [--headcount] [--wikidata]` | SEC submissions, latest 10-K, Wikidata | `firmographics.candidates.csv` | W1 firmographic key, T5 headcount, domains |
| `jobs --companies file` | Greenhouse, Lever and Ashby public boards | `job-evidence.csv` | T5 Salesforce evidence; T2 SDR openings |
| `press-contacts --ciks file --since` | Named emails in 8-K press-release exhibits | `press-contacts.candidates.csv` | Known-email (gold) candidates; on-domain flag |
| `events --ciks watchlist --since --until` | SEC 8-K Item 5.02 and Form D | `t4-events.candidates.csv` | T4 event log, run daily during the window |
| `domains --companies file [--executives file]` | The company's own homepage | `domains.candidates.csv` (match / review / unknown), `contacts.candidates.csv` | Domains for Form D companies (they have none on file); contacts ready to enrich: first name, last name, company domain |
| `uk --numbers file` | UK Companies House | `uk-companies.csv`, `uk-officers.csv` | EMEA contacts; officer resignations as job changes |

Input files are plain CSVs with a `cik`, `domain` (and optional `name`) or `company_number` column.

## Example: the T3 seed and key

```bash
python3 -m benchdata officers --since 2025-05-01 --until 2025-10-31 --limit 500 --out $OUT
python3 -m benchdata officers-truth --seed $OUT/officers-seed.csv --out $OUT
```

Then review every row marked `unknown`, and every row where the seed company filed an officer-change 8-K but `changed` is N.

## How the candidates are built

- **Random, not alphabetical:** index files are sorted by company name, so commands sample filings in a fixed random order (`--seed`).
- **Unknown, not guessed:** a value without first-hand evidence is written as `unknown`. For example, an officer with no Form 3/4 in the last 270 days is unknown, not unchanged.
- **Job boards:** "none" means no public board was found or no ad mentions Salesforce. That's unknown, not "doesn't use Salesforce".
- **Domains:** guessed from the company name (`.com`, `.ai`, `.io`, `.co`), and accepted only when the homepage's title or site name contains every word of the company name. Parked "for sale" pages are rejected. A partial match is marked `review`, nothing found is `unknown`. Guesses are checked 4 at a time per company and 8 companies at a time (`--workers`), with per-host pacing kept; a site that answers with a rate limit or error gets one retry. First live run: 12 of 20 matched, 3 to review, 5 unknown, in about 7 seconds (2.5 minutes before parallelizing).
- **Press contacts:** generic inboxes (`ir@`, `press@`, ...) are dropped, and each row says whether the email is on the company's own domain. Agency contacts drop out under the benchmarks' domain check.

## Limits

- SEC filers lean towards public and regulated companies, and towards executives, whom vendors find more easily. Form D (private companies) and Companies House (UK) help balance it. Disclose the mix.
- APAC coverage (Korea DART, Japan EDINET, NZ Companies Office, ASX notices) isn't built yet.
- `EDGAR_COUNTRY` maps a few common non-US EDGAR location codes; others come out blank for review.

## Licence

MIT.
