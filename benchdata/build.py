"""`python3 -m benchdata <command>`: write candidate inputs and answer keys as CSV.

Every row has source_url and retrieved_at. Files ending in `.candidates.csv` are not answer keys yet:
review disagreements, spot-check 20 rows, then copy the reviewed rows to the key file named in the
bench's ANSWER-KEYS.md.
"""
import argparse
import collections
import csv
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from . import http, sec, sources


def write(out_dir, name, rows, columns):
    p = Path(out_dir) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"{p}: {len(rows)} rows")
    return p


def read(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def country(code):
    code = (code or "").upper()
    return "US" if code in sec.US_STATES else sec.EDGAR_COUNTRY.get(code, "")


def sample(rows, seed):
    """One row per filing, in a fixed random order: index files are sorted by company name, so taking
    the first N would favour names starting with A."""
    seen, out = set(), []
    for r in rows:
        if r["accession"] not in seen:
            seen.add(r["accession"])
            out.append(r)
    random.Random(seed).shuffle(out)
    return out


def progress(i, n, every=25):
    if i % every == 0 or i == n:
        print(f"  {i}/{n}", file=sys.stderr, flush=True)


# --- Commands ------------------------------------------------------------------------------------

# Form D industry groups that are investment vehicles, not operating companies anyone sells to.
FUND_GROUPS = {"Pooled Investment Fund", "Investing", "REITS and Finance", "Other Real Estate", "Commercial", "Residential",
               "Other Banking and Financial Services"}


def cmd_formd(a):
    """Private US funding rounds and their executives: T1/W1 contacts, T2 funding reference.
    Investment funds and real-estate vehicles are skipped unless --include-funds."""
    filings, people, skipped, failed = [], [], 0, []
    idx = sample(sec.index({"D"}, a.since, a.until), a.seed)
    for i, r in enumerate(idx, 1):
        if len(filings) >= a.limit:
            break
        try:
            filing, persons, url = sec.form_d(r["cik"], r["accession"])
        except http.FetchError as e:
            failed.append(f"{r['accession']}: {e}")
            continue
        progress(len(filings), a.limit)
        if not filing:
            continue
        if not a.include_funds and filing["industry_group"] in FUND_GROUPS:
            skipped += 1
            continue
        base = {**filing, "issuer_cik": r["cik"], "filed": r["date"], "hq_country": country(filing["state_or_country"]),
                "source_url": url, "retrieved_at": http.now()}
        filings.append(base)
        people += [{**base, **p} for p in persons if "Executive Officer" in p["relationship"]]
    cols = ["company", "issuer_cik", "hq_country", "industry_group", "total_offering", "total_sold", "first_sale", "filed",
            "source_url", "retrieved_at"]
    write(a.out, "formd-filings.csv", filings, cols)
    write(a.out, "formd-executives.csv", people, ["first_name", "last_name", "relationship", *cols])
    if skipped:
        print(f"skipped {skipped} investment-fund or real-estate filings")
    if failed:
        print(f"{len(failed)} filings couldn't be fetched (first: {failed[0]})")


def cmd_officers(a):
    """Officers as of a past window (Forms 3/4): the T3 seed, as stored in a stale CRM."""
    seen, rows = set(), []
    idx = sample(sec.index({"3", "4"}, a.since, a.until), a.seed)
    for i, r in enumerate(idx, 1):
        if len(rows) >= a.limit:
            break
        people, url = sec.ownership(r["cik"], r["accession"])
        for p in people:
            key = (p["owner_cik"], p["issuer_cik"])
            if p["is_officer"] and p["officer_title"] and key not in seen:
                seen.add(key)
                rows.append({"record_id": f"S{len(rows) + 1:04d}", "owner_cik": p["owner_cik"], "name": p["owner_name"],
                             "jobtitle": p["officer_title"], "company": p["issuer_name"], "issuer_cik": p["issuer_cik"],
                             "as_of": r["date"], "source_url": url, "retrieved_at": http.now()})
        progress(i, len(idx), every=100)
    write(a.out, "officers-seed.csv", rows, ["record_id", "owner_cik", "name", "jobtitle", "company", "issuer_cik", "as_of",
                                            "source_url", "retrieved_at"])
    write(a.out, "t3-seed-export.candidates.csv", [{**r, "bm_job_changed": ""} for r in rows],
          ["record_id", "jobtitle", "company", "bm_job_changed"])


def cmd_officers_truth(a):
    """Current role of each seed officer from their latest Form 3/4, plus the issuer's 8-K Item 5.02s."""
    cutoff = (date.today() - timedelta(days=a.recent_days)).isoformat()
    out, seed = [], read(a.seed)
    for i, s in enumerate(seed, 1):
        filings = [f for f in sec.recent_filings(sec.submissions(s["owner_cik"]) or {}) if f["form"] in ("3", "4")]
        latest = next(iter(filings), None)
        row = {"record_id": s["record_id"], "true_title": "unknown", "true_company": "unknown", "changed": "unknown",
               "latest_filing": "", "evidence_url": "", "officer_changes_at_seed_company": "", "retrieved_at": http.now()}
        if latest:
            people, url = sec.ownership(s["owner_cik"], latest["accession"], latest["doc"])
            me = next((p for p in people if p["owner_cik"].lstrip("0") == s["owner_cik"].lstrip("0")), None)
            row.update(latest_filing=latest["date"], evidence_url=url)
            if me and latest["date"] >= cutoff:
                row.update(true_title=me["officer_title"] or "unknown", true_company=me["issuer_name"],
                           changed="Y" if me["issuer_cik"].lstrip("0") != s["issuer_cik"].lstrip("0") else "N")
        changes = sec.officer_changes(sec.submissions(s["issuer_cik"]) or {}, since=s["as_of"])
        row["officer_changes_at_seed_company"] = ";".join(f"{c['date']} {sec.archive_url(s['issuer_cik'], c['accession'], c['doc'])}" for c in changes)
        out.append(row)
        progress(i, len(seed))
    write(a.out, "t3-ground-truth.candidates.csv", out, ["record_id", "true_title", "true_company", "changed", "latest_filing",
                                                       "evidence_url", "officer_changes_at_seed_company", "retrieved_at"])
    print("Review: rows marked unknown, and any row with officer changes at the seed company but changed = N.")


def cmd_firmographics(a):
    """Website, industry, HQ country and 10-K headcount for SEC filers: W1 and T5 keys."""
    out, ciks = [], [r.get("cik") or r["issuer_cik"] for r in read(a.ciks)]   # also reads formd-filings.csv
    for i, cik in enumerate(ciks, 1):
        sub = sec.submissions(cik)
        if not sub:
            continue
        f = sec.company_facts(sub)
        site_src = "sec" if f["website"] else ""
        if not f["website"] and a.wikidata:
            try:
                hits = sources.wikidata_websites(f["name"])
            except http.FetchError as e:
                hits, site_src = [], f"wikidata failed: {e}"[:120]
            if hits:
                f["website"], site_src = hits[0][2], f"wikidata {hits[0][0]} (check it's the same company)"
        n, snippet, url = sec.latest_10k_headcount(cik, sub) if a.headcount else (None, "", "")
        out.append({"cik": cik, **f, "domain": sources.domain_of(f["website"]) if f["website"] else "",
                    "website_source": site_src, "employees": n if n is not None else "unknown",
                    "employees_snippet": snippet, "employees_source_url": url,
                    "source_url": f"{sec.DATA}/submissions/CIK{int(cik):010d}.json", "retrieved_at": http.now()})
        progress(i, len(ciks))
    write(a.out, "firmographics.candidates.csv", out, ["cik", "name", "domain", "website", "website_source", "sic",
                                                      "sic_description", "hq_country", "hq_code", "employees",
                                                      "employees_snippet", "employees_source_url", "source_url", "retrieved_at"])


def cmd_jobs(a):
    """Public job boards: Salesforce evidence (T5) and open SDR/BDR roles (T2 review reference)."""
    out, companies = [], read(a.companies)
    for i, c in enumerate(companies, 1):
        board, slug, jobs = sources.find_board(c["domain"], c.get("name", ""))
        strength, url, snippet = sources.salesforce_evidence(jobs)
        sdrs = sources.sdr_openings(jobs)
        out.append({"domain": c["domain"], "board": board or "", "slug": slug or "", "jobs": len(jobs),
                    "salesforce_evidence": strength or "none", "salesforce_evidence_url": url, "salesforce_snippet": snippet,
                    "sdr_openings": len(sdrs), "sdr_urls": ";".join(u for _, u, _ in sdrs[:5]), "retrieved_at": http.now()})
        progress(i, len(companies))
    write(a.out, "job-evidence.csv", out, ["domain", "board", "slug", "jobs", "salesforce_evidence", "salesforce_evidence_url",
                                          "salesforce_snippet", "sdr_openings", "sdr_urls", "retrieved_at"])
    print("'none' means no public board was found or no ad mentions Salesforce: that's unknown, not N.")


def cmd_press_contacts(a):
    """Named personal emails in 8-K press-release exhibits: known-email (gold) candidates."""
    # The input may carry each company's domain (e.g. from public-contacts): SEC records rarely have one.
    out, inputs = [], read(a.ciks)
    ciks = [r.get("cik") or r["issuer_cik"] for r in inputs]   # also reads formd-filings.csv
    known_domain = {(r.get("cik") or r.get("issuer_cik", "")).lstrip("0"): (r.get("company_domain") or "").lower() for r in inputs}
    for i, cik in enumerate(ciks, 1):
        sub = sec.submissions(cik) or {}
        site = known_domain.get(cik.lstrip("0")) or (sources.domain_of(sec.company_facts(sub)["website"]) if sub else "")
        company = sub.get("name", "")
        for f in [f for f in sec.recent_filings(sub) if f["form"] == "8-K" and f["date"] >= a.since.isoformat()][: a.per_company]:
            for c in sec.press_contacts(cik, f["accession"]):
                if c["name"] and set(sources.name_tokens(c["name"])) <= set(sources.name_tokens(company)):
                    c["name"] = ""   # the "name" was the company's own name, not a person
                email_dom = c["email"].lower().rpartition("@")[2]
                c.update(cik=cik, company=company, company_domain=site, filed=f["date"],
                         on_domain=bool(site) and (email_dom == site or email_dom.endswith("." + site)), retrieved_at=http.now())
                out.append(c)
        progress(i, len(ciks))
    write(a.out, "press-contacts.candidates.csv", out, ["cik", "company", "company_domain", "name", "email", "on_domain",
                                                       "filed", "snippet", "source_url", "retrieved_at"])
    print("Keep on-domain rows with a clear name; agency contacts (off-domain) drop out.")


def cmd_events(a):
    """T4 event feed: 8-K Item 5.02 and Form D filings for watched companies in a window."""
    watch = {r["cik"].lstrip("0") for r in read(a.ciks)}
    out = []
    for r in sec.index({"8-K", "D"}, a.since, a.until):
        if r["cik"].lstrip("0") not in watch:
            continue
        if r["form"] == "8-K":
            f = next((f for f in sec.recent_filings(sec.submissions(r["cik"]) or {}) if f["accession"] == r["accession"]), None)
            if not f or "5.02" not in (f["items"] or ""):
                continue
            kind, url = "job change (8-K 5.02)", sec.archive_url(r["cik"], r["accession"], f["doc"])
        else:
            kind, url = "funding (Form D)", sec.archive_url(r["cik"], r["accession"], "primary_doc.xml")
        out.append({"date_logged": http.now()[:10], "account_cik": r["cik"], "company": r["company"], "event_type": kind,
                    "event_date": r["date"], "public_source_url": url, "logged_by": "bench-data"})
    write(a.out, "t4-events.candidates.csv", out, ["date_logged", "account_cik", "company", "event_type", "event_date",
                                                  "public_source_url", "logged_by"])


EMEA = set("GB IE FR DE NL BE LU CH AT IT ES PT SE NO DK FI IS PL CZ IL AE ZA".split())
APAC = set("AU NZ JP KR SG HK CN TW IN ID MY PH TH VN".split())


NA = {"US", "CA", "MX"}


def region(country):
    """W1 regions: na (US, Canada, Mexico), emea, apac."""
    return "na" if country in NA else "emea" if country in EMEA else "apac" if country in APAC else ""


# Shared verticals (the W1 industry list). Rules over SEC/Form D industry text and UK SIC codes;
# first match wins. shortcut: keyword rules, not a full SIC table; refine if many rows come out unknown.
VERTICAL_RULES = [
    ("Marketing and media", r"advertis|marketing|public relations|media|publishing|broadcast|motion picture|entertainment"),
    ("Healthcare", r"health|pharma|biolog|biotech|medical|surgical|drug|hospital|diagnostic|dental"),
    ("Software", r"software|prepackaged|internet|data processing|computer programming|saas"),
    ("IT services", r"computer|information technology|semiconductor|electronic comp|technology"),
    ("Financial services", r"bank|financ|insur|invest|credit|savings|reit and finance|securit|lending|capital"),
    ("Energy and utilities", r"oil|gas|petroleum|electric|energy|utilit|mining|coal|solar|power"),
    ("Real estate and construction", r"real estate|reit|construction|building|contractor|lodging"),
    ("Retail and ecommerce", r"retail|stores|e-commerce|apparel|consumer|food|beverage|restaurant"),
    ("Manufacturing", r"manufactur|industrial|machinery|chemical|metal|plastic|equipment|motor vehicle|aerospace"),
    ("Telecommunications", r"telecom|telephone|communications|wireless|cable"),
    ("Transportation and logistics", r"transport|trucking|airline|air courier|railroad|shipping|logistics|freight"),
    ("Professional services", r"services-management|consulting|staffing|legal|engineering services|business services"),
    ("Education", r"education|school|training"),
    ("Hospitality and travel", r"hotel|travel|leisure|eating"),
]
UK_SIC_VERTICALS = [("731", "Marketing and media"), ("702", "Marketing and media"), ("58", "Software"), ("62", "Software"),
                    ("63", "Software"), ("64", "Financial services"), ("65", "Financial services"), ("66", "Financial services"),
                    ("86", "Healthcare"), ("47", "Retail and ecommerce"), ("70", "Professional services"),
                    ("69", "Professional services"), ("41", "Real estate and construction"), ("68", "Real estate and construction")]
AGENCY_UK_SIC = {"73110", "73120", "70210"}


SENIORITY_RULES = [("executive", r"^executive officer$|^company director$"),
                   ("c_suite", r"\bchief\b|\bc[efotmrpi]o\b|\bpresident\b|\bfounder\b|\bowner\b|managing director|\bceo\b"),
                   ("vp", r"\bvice president\b|\bvp\b|\bsvp\b|\bevp\b|\bhead of\b"),
                   ("director", r"\bdirector\b"),
                   ("other", r"\w")]
FUNCTION_RULES = [("finance", r"financ|cfo|accounting|controller|treasur|investor"),
                  ("sales", r"sales|revenue|cro\b|business development|commercial"),
                  ("marketing", r"marketing|cmo\b|brand|communications|growth"),
                  ("engineering_it", r"technolog|cto\b|engineer|information|cio\b|product|data|digital|software"),
                  ("legal", r"legal|counsel|secretary|compliance"),
                  ("hr", r"people|human|hr\b|talent"),
                  ("operations", r"operat|coo\b|supply|manufactur"),
                  ("executive", r"chief executive|ceo\b|president|founder|managing director|owner")]


def seniority(title):
    t = (title or "").lower()
    return next((v for v, pat in SENIORITY_RULES if re.search(pat, t)), "unknown") if t else "unknown"


def job_function(title):
    t = (title or "").lower()
    if t in ("executive officer", "company director"):
        return "unknown"   # generic officer titles from Form D and Companies House say nothing about function
    return next((v for v, pat in FUNCTION_RULES if re.search(pat, t)), "other") if t else "unknown"


def funding_band(amount):
    try:
        n = float(amount)
    except (TypeError, ValueError):
        return ""
    return "under_1m" if n < 1e6 else "1m_10m" if n < 1e7 else "10m_plus"


def vertical(industry_text="", uk_sic=""):
    for code in (uk_sic or "").split(";"):
        for prefix, v in UK_SIC_VERTICALS:
            if code.startswith(prefix):
                return v
    t = (industry_text or "").lower()
    return next((v for v, pat in VERTICAL_RULES if re.search(pat, t)), "")


def is_agency(industry_text="", uk_sic=""):
    return any(c in AGENCY_UK_SIC for c in (uk_sic or "").split(";")) or "advertising agenc" in (industry_text or "").lower()


def size_segment(employees):
    """W1 size cells: smb 1-50, mid 51-1,000, ent 1,001+ employees."""
    if not isinstance(employees, int):
        return ""
    return "smb" if employees <= 50 else "mid" if employees <= 1000 else "ent"


def split_sec_name(name):
    """Forms 3/4 list owners as 'LAST FIRST MIDDLE': return (first, last) in title case."""
    parts = [p for p in name.replace(",", " ").split() if p.lower().rstrip(".") not in ("jr", "sr", "ii", "iii", "iv", "md", "phd")]
    if len(parts) < 2:
        return "", name.title()
    return parts[1].title(), parts[0].title()


def cmd_public_contacts(a):
    """Current officers at SEC-filing companies (recent Forms 3/4), with the company's domain (from
    SEC records, then the website its 10-K states, then its homepage), size from the 10-K's headcount
    and region from its address: W1 mid-market and enterprise rows."""
    officers, per_issuer = [], {}
    idx = sample(sec.index({"3", "4"}, a.since, a.until), a.seed)
    for r in idx:
        if len(officers) >= a.limit:
            break
        try:
            people, url = sec.ownership(r["cik"], r["accession"])
        except http.FetchError:
            continue
        for p in people:
            ic = p["issuer_cik"].lstrip("0")
            if not (p["is_officer"] and p["officer_title"]) or per_issuer.get(ic, 0) >= a.per_company:
                continue
            if any(o["owner_cik"] == p["owner_cik"] and o["issuer_cik"] == ic for o in officers):
                continue
            per_issuer[ic] = per_issuer.get(ic, 0) + 1
            officers.append({**p, "issuer_cik": ic, "filed": r["date"], "person_source_url": url})
        progress(len(officers), a.limit, every=50)
    companies = {}
    for i, ic in enumerate(per_issuer, 1):
        sub = sec.submissions(ic) or {}
        f = sec.company_facts(sub) if sub else {"name": "", "website": "", "hq_country": "", "sic_description": ""}
        domain, how = (sources.domain_of(f["website"]), "sec") if f["website"] else ("", "")
        text, kurl = sec.latest_10k(ic, sub) if sub else ("", "")
        if not domain and text:
            domain, _ = sec.website_from_10k(text)
            how = "10-K" if domain else ""
        if not domain and f["name"]:
            d, status, _, _ = sources.find_domain(f["name"])
            if status == "match":
                domain, how = d, "homepage"
        n, _ = sec.headcount_from_10k(text) if text else (None, "")
        companies[ic] = {"company": f["name"], "company_domain": domain, "domain_source": how,
                         "hq_country": f["hq_country"], "industry": f["sic_description"], "employees": n if n is not None else "",
                         "employees_source_url": kurl}
        progress(i, len(per_issuer), every=25)
    rows = []
    for o in officers:
        c = companies[o["issuer_cik"]]
        first, last = split_sec_name(o["owner_name"])
        rows.append({"first_name": first, "last_name": last, "title": o["officer_title"], **c, "cik": o["issuer_cik"],
                     "segment_size": size_segment(c["employees"] if isinstance(c["employees"], int) else None),
                     "segment_region": region(c["hq_country"]), "filed": o["filed"],
                     "person_source_url": o["person_source_url"], "retrieved_at": http.now()})
    cols = ["first_name", "last_name", "title", "company", "company_domain", "domain_source", "cik", "hq_country", "industry",
            "employees", "segment_size", "segment_region", "filed", "person_source_url", "employees_source_url", "retrieved_at"]
    write(a.out, "public-contacts.candidates.csv", rows, cols)
    ready = [r for r in rows if r["company_domain"]]
    print(f"{len(ready)} of {len(rows)} officers have a company domain; "
          f"size cells: {dict(sorted(collections.Counter(r['segment_size'] or '?' for r in ready).items()))}")


def cmd_uk_contacts(a):
    """Current directors at active UK companies (Companies House), with domain from the company's
    homepage (.co.uk first), size from the average employees in its filed accounts: W1 EMEA rows."""
    companies, start = [], 0
    while len(companies) < a.companies:
        page = sources.ch_search(a.sic.split(","), a.company_type, size=100, start=start)
        if not page:
            break
        companies += page
        start += 100
    random.Random(a.seed).shuffle(companies)
    rows, failed = [], 0
    for i, c in enumerate(companies[: a.companies], 1):
        try:
            directors = [o for o in sources.ch_officers(c["company_number"]) if o["role"] == "director" and not o["resigned_on"]]
        except http.FetchError:
            failed += 1
            continue
        if not directors:
            continue
        domain, status, _, _ = sources.find_domain(c["name"], tlds=sources.UK_TLDS)
        n, acc_url = sources.ch_headcount(c["company_number"]) if domain and status == "match" else (None, "")
        for o in directors[: a.per_company]:
            first, last = sources.split_ch_name(o["name"])
            rows.append({"first_name": first, "last_name": last, "title": o["occupation"], "company": c["name"],
                         "company_domain": domain if status == "match" else "", "domain_source": "homepage" if status == "match" else "",
                         "company_number": c["company_number"], "hq_country": "GB", "industry": c["sic_codes"],
                         "employees": n if n is not None else "", "segment_size": size_segment(n), "segment_region": "emea",
                         "person_source_url": f"{sources.CH}/company/{c['company_number']}/officers",
                         "employees_source_url": acc_url, "retrieved_at": http.now()})
        progress(i, a.companies, every=25)
    cols = ["first_name", "last_name", "title", "company", "company_domain", "domain_source", "company_number", "hq_country",
            "industry", "employees", "segment_size", "segment_region", "person_source_url", "employees_source_url", "retrieved_at"]
    write(a.out, "uk-contacts.candidates.csv", rows, cols)
    if failed:
        print(f"{failed} companies couldn't be fetched")
    ready = [r for r in rows if r["company_domain"]]
    print(f"{len(ready)} of {len(rows)} directors have a company domain; "
          f"size cells: {dict(sorted(collections.Counter(r['segment_size'] or '?' for r in ready).items()))}")


def cmd_w1_assemble(a):
    """Merge candidate pools into the W1 input: one row per person at a matched domain, capped per
    size x region cell, with a private provenance file and a firmographic-key candidate sample."""
    d = Path(a.candidates)
    pools = []
    if (d / "contacts.candidates.csv").exists():   # Form D executives: privately funded companies (startups)
        filing = {f["company"]: f for f in read(d / "formd-filings.csv")} if (d / "formd-filings.csv").exists() else {}
        for r in read(d / "contacts.candidates.csv"):
            f = filing.get(r["company"], {})
            pools.append({**r, "segment_size": "smb", "size_basis": "assumed (Form D private company)", "employees": "",
                          "hq_country": f.get("hq_country", "US"), "industry": f.get("industry_group", ""),
                          "startup": "Y", "source": "form-d", "title": "executive officer",
                          "segment_funding": funding_band(f.get("total_sold") or f.get("total_offering"))})
    for name, src in (("public-contacts.candidates.csv", "sec-filer"), ("uk-contacts.candidates.csv", "companies-house"),
                      ("agency-contacts.candidates.csv", "companies-house (agency)")):
        if (d / name).exists():
            pools += [{**r, "size_basis": "headcount" if r.get("employees") else "", "source": src,
                       # UK filings rarely give an occupation; a statutory director of a small company is its executive
                       "title": r.get("title") or ("company director" if src.startswith("companies-house") else "")}
                      for r in read(d / name)]
    for r in pools:
        sic = r.get("industry", "") if r["source"].startswith("companies-house") else ""
        text = "" if sic else r.get("industry", "")
        r["segment_region"] = region(r.get("hq_country", "")) or {"us": "na"}.get(r.get("segment_region", ""), r.get("segment_region", ""))
        r["segment_vertical"] = vertical(text, sic)
        r["segment_startup"] = r.get("startup", "N")
        r["segment_agency"] = "Y" if is_agency(text, sic) else "N"
        r["segment_seniority"] = seniority(r.get("title"))
        r["segment_function"] = job_function(r.get("title"))
        r["segment_ownership"] = "public" if r["source"] == "sec-filer" else "private"
        r.setdefault("segment_funding", "")
    # Gold people (named, on-domain press contacts) go first so the cap never drops them; their cell is
    # their company's, from the SEC-filer pool.
    gold = []
    if (d / "press-contacts.candidates.csv").exists() and (d / "public-contacts.candidates.csv").exists():
        cell_of = {}
        for r in read(d / "public-contacts.candidates.csv"):
            cell_of.setdefault(r["cik"].lstrip("0"), r)
        for r in read(d / "press-contacts.candidates.csv"):
            co = cell_of.get(r["cik"].lstrip("0"))
            if r["on_domain"] == "True" and r["name"] and co and len(r["name"].split()) >= 2:
                first, last = r["name"].split()[0], r["name"].split()[-1]
                gold.append({"first_name": first, "last_name": last, "company": co["company"], "company_domain": co["company_domain"],
                             "segment_size": co["segment_size"], "segment_region": region(co["hq_country"]), "employees": co["employees"],
                             "segment_vertical": vertical(co.get("industry", "")), "segment_startup": "N",
                             "segment_agency": "Y" if is_agency(co.get("industry", "")) else "N",
                             "segment_seniority": "unknown", "segment_function": "unknown", "segment_ownership": "public",
                             "segment_funding": "",
                             "hq_country": co["hq_country"], "size_basis": "headcount" if co["employees"] else "", "source": "press-release (gold)",
                             "known_email": r["email"], "person_source_url": r["source_url"], "employees_source_url": co["employees_source_url"]})
    seen, rows = set(), []
    for r in gold + pools:
        key = (r["first_name"].lower(), r["last_name"].lower(), (r["company_domain"] or "").lower())
        cell = f"{r.get('segment_size')}/{r.get('segment_region')}"
        if not all(key) or key in seen or cell not in a.cells.split(","):
            continue
        seen.add(key)
        rows.append({**r, "cell": cell})
    rng = random.Random(a.seed)
    head = [r for r in rows if r.get("known_email")]
    rest = [r for r in rows if not r.get("known_email")]
    rng.shuffle(rest)
    rows = head + rest
    counts, picked = collections.Counter(), []
    for r in rows:
        if counts[r["cell"]] < a.cap:
            counts[r["cell"]] += 1
            picked.append(r)
    for i, r in enumerate(picked, 1):
        r["input_id"] = f"W{i:04d}"
    write(a.out, "w1-input.csv", picked, ["input_id", "first_name", "last_name", "company_domain", "segment_size", "segment_region",
                                          "segment_vertical", "segment_startup", "segment_agency", "segment_seniority",
                                          "segment_function", "segment_ownership", "segment_funding"])
    write(a.out, "w1-input-sources.csv", picked, ["input_id", "company", "company_domain", "cell", "size_basis", "employees",
                                                  "hq_country", "source", "person_source_url", "domain_source_url",
                                                  "domain_source", "employees_source_url"])
    write(a.out, "w1-email-gold.candidates.csv",
          [{"input_id": r["input_id"], "known_email": r["known_email"], "known_phone": "", "source_url": r["person_source_url"]}
           for r in picked if r.get("known_email")], ["input_id", "known_email", "known_phone", "source_url"])
    sized = [r for r in picked if r.get("employees")]
    sample = rng.sample(sized, min(a.firmographic_sample, len(sized)))
    write(a.out, "w1-firmographics-truth.candidates.csv",
          [{"input_id": r["input_id"], "true_employee_count": r["employees"], "true_industry": r.get("segment_vertical") or "unknown",
            "true_hq_country": r.get("hq_country", ""), "source_url": r.get("employees_source_url", "")} for r in sample],
          ["input_id", "true_employee_count", "true_industry", "true_hq_country", "source_url"])
    print("cell counts:", dict(sorted(counts.items())))
    print("verticals:", dict(collections.Counter(r["segment_vertical"] or "unknown" for r in picked).most_common()))
    print("startups:", sum(r["segment_startup"] == "Y" for r in picked), "| agencies:", sum(r["segment_agency"] == "Y" for r in picked))
    for dim in ("seniority", "function", "ownership", "funding"):
        print(f"{dim}:", dict(collections.Counter(r[f"segment_{dim}"] or "n/a" for r in picked).most_common()))
    print("Record the cell counts in the waterfall config (disclosures.cells.counts) before freezing.")


def _contact_pool(d):
    """People at matched domains from a candidates folder: Form D contacts and SEC-filer officers."""
    pool = []
    if (d / "contacts.candidates.csv").exists():
        filing = {f["company"]: f for f in read(d / "formd-filings.csv")} if (d / "formd-filings.csv").exists() else {}
        pool += [{**r, "hq_country": filing.get(r["company"], {}).get("hq_country", "US"), "source": "form-d"}
                 for r in read(d / "contacts.candidates.csv")]
    if (d / "public-contacts.candidates.csv").exists():
        pool += [{**r, "source": "sec-filer"} for r in read(d / "public-contacts.candidates.csv") if r["company_domain"]]
    return pool


def cmd_t1_assemble(a):
    """T1 input: people at matched domains, none at a company already in the W1 input (no overlap)."""
    d = Path(a.candidates)
    exclude = {r["company_domain"].lower() for r in read(a.exclude)} if a.exclude else set()
    seen, rows = set(), []
    for r in _contact_pool(d):
        dom = (r["company_domain"] or "").lower()
        key = (r["first_name"].lower(), r["last_name"].lower(), dom)
        if all(key) and key not in seen and dom not in exclude and region(r.get("hq_country", "")):
            seen.add(key)
            rows.append(r)
    random.Random(a.seed).shuffle(rows)
    rows = rows[: a.rows]
    for i, r in enumerate(rows, 1):
        r["input_id"] = str(i)
    write(a.out, "t1-input.csv", rows, ["input_id", "first_name", "last_name", "company_domain"])
    write(a.out, "t1-input-sources.csv", rows, ["input_id", "company", "company_domain", "hq_country", "source",
                                                "person_source_url", "domain_source_url"])
    print(f"{len(rows)} rows; regions: {dict(collections.Counter(region(r.get('hq_country', '')) for r in rows))}; "
          f"excluded {len(exclude)} W1 company domains")


def cmd_t5_assemble(a):
    """T5 input: company domains (one per company) from every candidates folder given."""
    doms, rows = set(), []
    for folder in a.candidates.split(","):
        for r in _contact_pool(Path(folder)):
            dom = (r["company_domain"] or "").lower()
            if dom and dom not in doms:
                doms.add(dom)
                rows.append({"domain": dom, "name": r.get("company", ""), "employees": r.get("employees", ""),
                             "employees_source_url": r.get("employees_source_url", "")})
    random.Random(a.seed).shuffle(rows)
    rows = rows[: a.rows]
    write(a.out, "t5-domains.csv", rows, ["domain"])
    write(a.out, "t5-companies.csv", rows, ["domain", "name", "employees", "employees_source_url"])
    print(f"{len(rows)} domains; with headcount: {sum(bool(r['employees']) for r in rows)}")


def _source_text(url):
    """Text of a cited source: SEC documents with the SEC contact header, Companies House documents
    with the API key (accounts need /content), anything else as a plain page."""
    if not url:
        return ""
    try:
        if "sec.gov" in url:
            raw = http.get_text(url, headers=sec.headers())
        elif "company-information.service.gov.uk/document" in url:
            raw = http.get_text(f"{url}/content", headers={**sources.ch_headers(), "Accept": "application/xhtml+xml"})
        else:
            raw = http.get_text(url, headers=sources.SITE_HEADERS)
    except http.FetchError:
        return ""
    return sec.html_to_text(raw or "")


def _number_in(text, n):
    n = int(n)
    return any(f in text for f in (f"{n:,}", str(n))) if n >= 10 else bool(re.search(rf"\b{n}\b", text))


CHECKS = {
    # key file: (what must appear in the cited source, source column)
    "email-gold": (lambda r, t: r["known_email"].lower() in t.lower(), "source_url"),
    "t3-ground-truth": (lambda r, t: r["changed"] in ("unknown", "") or
                        (r["true_company"].lower() in t.lower() and all(w in t.lower() for w in r["true_title"].lower().split()[:3])),
                        "evidence_url"),
    "firmographics-truth": (lambda r, t: not r["true_employee_count"] or _number_in(t, r["true_employee_count"]), "source_url"),
}


def cmd_verify_sources(a):
    """Re-fetch every answer-key candidate's cited source and keep only rows whose value appears in it.
    Replaces a hand spot-check: every row is checked, by machine, and failures are listed."""
    kind = next(k for k in CHECKS if k in Path(a.file).name)
    check, col = CHECKS[kind]
    rows = read(a.file)
    with ThreadPoolExecutor(max_workers=4) as pool:
        texts = list(pool.map(lambda r: _source_text(r.get(col, "")), rows))
    passed, failed = [], []
    for r, t in zip(rows, texts):
        (passed if t and check(r, t) else failed).append({**r, "verify_status": "pass" if t and check(r, t) else ("no source" if not t else "value not in source")})
    out = Path(a.file).with_name(Path(a.file).name.replace(".candidates", ".verified"))
    write(out.parent, out.name, passed, list(rows[0].keys()) + ["verify_status"] if rows else ["verify_status"])
    write(out.parent, out.name.replace(".verified", ".rejected"), failed, list(rows[0].keys()) + ["verify_status"] if rows else ["verify_status"])
    print(f"{kind}: {len(passed)} of {len(rows)} rows confirmed in their cited source; "
          f"rejected: {dict(collections.Counter(r['verify_status'] for r in failed))}")


def cmd_domains(a):
    """Company domains from the company's own homepage: name-based guesses, accepted only when the
    page title names the company. With --executives, also writes contacts ready to enrich."""
    companies = read(a.companies)
    names = [c.get("company") or c.get("name") or "" for c in companies]
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(sources.find_domain, names))
    out = []
    for i, (c, name, (domain, status, title, tried)) in enumerate(zip(companies, names, results), 1):
        out.append({"company": name, "cik": c.get("issuer_cik") or c.get("cik", ""), "domain": domain, "status": status,
                    "page_title": title, "tried": ";".join(tried), "source_url": f"https://{domain}/" if domain else "",
                    "retrieved_at": http.now()})
        progress(i, len(companies), every=10)
    write(a.out, "domains.candidates.csv", out, ["company", "cik", "domain", "status", "page_title", "tried", "source_url", "retrieved_at"])
    found = sum(r["status"] == "match" for r in out)
    print(f"{found} matched, {sum(r['status'] == 'review' for r in out)} to review, {sum(r['status'] == 'unknown' for r in out)} unknown")
    if a.executives:
        by_cik = {r["cik"].lstrip("0"): r for r in out if r["status"] == "match"}
        contacts = []
        for e in read(a.executives):
            d = by_cik.get((e.get("issuer_cik") or "").lstrip("0"))
            if d:
                contacts.append({"first_name": e["first_name"], "last_name": e["last_name"], "company": e["company"],
                                 "company_domain": d["domain"], "relationship": e.get("relationship", ""),
                                 "person_source_url": e.get("source_url", ""), "domain_source_url": d["source_url"]})
        write(a.out, "contacts.candidates.csv", contacts, ["first_name", "last_name", "company", "company_domain", "relationship",
                                                           "person_source_url", "domain_source_url"])


def cmd_uk(a):
    """Companies House: profiles and officers (appointments and resignations) for UK companies."""
    profiles, officers, numbers = [], [], [r["company_number"] for r in read(a.numbers)]
    for i, n in enumerate(numbers, 1):
        p = sources.ch_company(n)
        if p:
            profiles.append({**p, "source_url": f"{sources.CH}/company/{n}", "retrieved_at": http.now()})
            officers += [{**o, "source_url": f"{sources.CH}/company/{n}/officers", "retrieved_at": http.now()}
                         for o in sources.ch_officers(n)]
        progress(i, len(numbers))
    write(a.out, "uk-companies.csv", profiles, ["company_number", "name", "status", "sic_codes", "country", "source_url", "retrieved_at"])
    write(a.out, "uk-officers.csv", officers, ["company_number", "name", "role", "occupation", "appointed_on", "resigned_on",
                                               "source_url", "retrieved_at"])


def build_parser():
    p = argparse.ArgumentParser(prog="benchdata", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    day = date.fromisoformat

    def cmd(name, fn, help_, **args):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--out", required=True, help="output folder, e.g. ../clay-bench-ease-pricing/private/datasets/candidates")
        for flag, kw in args.items():
            s.add_argument(f"--{flag.replace('_', '-')}", **kw)
        s.set_defaults(fn=fn)

    cmd("formd", cmd_formd, "private funding rounds and their executives (Form D)",
        since=dict(type=day, required=True), until=dict(type=day, required=True), limit=dict(type=int, default=300),
        seed=dict(type=int, default=2026), include_funds=dict(action="store_true"))
    cmd("officers", cmd_officers, "officers as of a past window (Forms 3/4): the T3 seed",
        since=dict(type=day, required=True), until=dict(type=day, required=True), limit=dict(type=int, default=500),
        seed=dict(type=int, default=2026))
    cmd("officers-truth", cmd_officers_truth, "current role of each seed officer: T3 key candidates",
        seed=dict(required=True), recent_days=dict(type=int, default=270))
    cmd("firmographics", cmd_firmographics, "website, industry, HQ country, 10-K headcount for SEC filers",
        ciks=dict(required=True, help="CSV with a cik (or issuer_cik) column, e.g. formd-filings.csv"), wikidata=dict(action="store_true"),
        headcount=dict(action="store_true"))
    cmd("jobs", cmd_jobs, "public job boards: Salesforce evidence and SDR openings",
        companies=dict(required=True, help="CSV with domain (and optional name) columns"))
    cmd("press-contacts", cmd_press_contacts, "named emails in 8-K press releases: gold candidates",
        ciks=dict(required=True), since=dict(type=day, required=True), per_company=dict(type=int, default=5))
    cmd("events", cmd_events, "T4 event feed for watched companies",
        ciks=dict(required=True), since=dict(type=day, required=True), until=dict(type=day, required=True))
    cmd("public-contacts", cmd_public_contacts, "current officers at SEC filers, with domain, size and region: W1 mid/ent rows",
        since=dict(type=day, required=True), until=dict(type=day, required=True), limit=dict(type=int, default=600),
        per_company=dict(type=int, default=2), seed=dict(type=int, default=2026))
    cmd("uk-contacts", cmd_uk_contacts, "current directors at active UK companies, with domain and accounts headcount: W1 EMEA rows",
        sic=dict(default=",".join(sources.TECH_SIC), help="comma-separated SIC codes"),
        company_type=dict(default="ltd", choices=["ltd", "plc"], help="plc for larger companies"),
        companies=dict(type=int, default=300), per_company=dict(type=int, default=2), seed=dict(type=int, default=2026))
    cmd("w1-assemble", cmd_w1_assemble, "merge candidate pools into w1-input.csv, capped per size x region cell",
        candidates=dict(required=True, help="folder with contacts / public-contacts / uk-contacts candidate files"),
        cells=dict(default="smb/na,mid/na,ent/na,smb/emea"), cap=dict(type=int, default=400),
        firmographic_sample=dict(type=int, default=200), seed=dict(type=int, default=2026))
    cmd("verify-sources", cmd_verify_sources, "re-check every answer-key row against its cited source (replaces a hand spot-check)",
        file=dict(required=True, help="a *-email-gold, t3-ground-truth or *-firmographics-truth .candidates.csv"))
    cmd("t1-assemble", cmd_t1_assemble, "T1 input: people at matched domains, excluding W1's companies",
        candidates=dict(required=True), exclude=dict(help="W1 input CSV: its company domains are left out"),
        rows=dict(type=int, default=500), seed=dict(type=int, default=2026))
    cmd("t5-assemble", cmd_t5_assemble, "T5 input: one domain per company from the given candidate folders",
        candidates=dict(required=True, help="comma-separated candidate folders"), rows=dict(type=int, default=1000),
        seed=dict(type=int, default=2026))
    cmd("domains", cmd_domains, "company domains from the company's own homepage (title must name the company)",
        companies=dict(required=True, help="CSV with a company or name column, e.g. formd-filings.csv"),
        executives=dict(help="formd-executives.csv: also write contacts.candidates.csv for matched companies"),
        workers=dict(type=int, default=8, help="companies checked at the same time (each tries 4 guesses at once)"))
    cmd("uk", cmd_uk, "Companies House profiles and officers", numbers=dict(required=True, help="CSV with company_number"))
    return p


def load_env(path=Path(__file__).resolve().parents[1] / ".env"):
    """KEY=VALUE lines from bench-data/.env (gitignored); variables already set win."""
    import os
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k and not k.startswith("#") and v.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip().strip('"').strip("'")


def main(argv=None):
    load_env()
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except http.FetchError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
