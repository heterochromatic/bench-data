"""Other free, first-hand sources: public job boards (Greenhouse, Lever, Ashby), Wikidata and
UK Companies House (needs COMPANIES_HOUSE_KEY, a free key)."""
import base64
import html as htmllib
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import http
from .sec import html_to_text

# --- Job boards ----------------------------------------------------------------------------------

BOARDS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def slugs(domain, name=""):
    stem = domain.lower().removeprefix("www.").split(".")[0]
    base = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return list(dict.fromkeys(s for s in (stem, base, stem.replace("-", "")) if s))


def normalize_jobs(board, data):
    """(title, url, description text, posted) for each job, whatever the board's shape."""
    if board == "greenhouse":
        return [(j.get("title", ""), j.get("absolute_url", ""), html_to_text(j.get("content", "")), j.get("updated_at", ""))
                for j in (data or {}).get("jobs", [])]
    if board == "lever":
        return [(j.get("text", ""), j.get("hostedUrl", ""), j.get("descriptionPlain", "") or "", str(j.get("createdAt", "")))
                for j in (data or [])]
    return [(j.get("title", ""), j.get("jobUrl", ""), j.get("descriptionPlain", "") or "", j.get("publishedAt", ""))
            for j in (data or {}).get("jobs", [])]


def find_board(domain, name=""):
    """First public board found for the company: (board, slug, jobs), or (None, None, [])."""
    for slug in slugs(domain, name):
        for board, tmpl in BOARDS.items():
            data = http.get_json(tmpl.format(slug=urllib.parse.quote(slug)))
            jobs = normalize_jobs(board, data) if data else []
            if jobs:
                return board, slug, jobs
    return None, None, []


STRONG_SF = re.compile(r"salesforce\s+(admin|administrator|administration|crm|sales cloud|reports?|dashboards?)|\bsfdc\b", re.I)
ANY_SF = re.compile(r"\bsalesforce\b", re.I)
SDR = re.compile(r"\b(sdr|bdr|sales development|business development rep)", re.I)


def salesforce_evidence(jobs):
    """Best job ad showing the company uses Salesforce as its CRM: (strength, url, snippet)."""
    best = (None, "", "")
    for title, url, text, _ in jobs:
        m = STRONG_SF.search(text) or STRONG_SF.search(title)
        if m:
            return "strong", url, text[max(0, m.start() - 60): m.end() + 60]
        m = ANY_SF.search(text)
        if m and not best[0]:
            best = ("mention", url, text[max(0, m.start() - 60): m.end() + 60])
    return best


def sdr_openings(jobs):
    return [(title, url, posted) for title, url, _, posted in jobs if SDR.search(title)]


# --- Wikidata ------------------------------------------------------------------------------------

WD_API = "https://www.wikidata.org/w/api.php"
# Wikimedia asks for a descriptive agent with a contact URL.
WD_HEADERS = {"User-Agent": "bench-data/0.1 (The Prompting Company benchmark research; https://promptingcompany.com)"}


def wikidata_websites(name, limit=3):
    """Official websites (P856) of the top entity matches for a company name: [(qid, label, url)]."""
    hits = http.get_json(WD_API, params={"action": "wbsearchentities", "search": name, "language": "en",
                                         "type": "item", "limit": limit, "format": "json"}, headers=WD_HEADERS)
    ids = [h["id"] for h in (hits or {}).get("search", [])]
    if not ids:
        return []
    ents = http.get_json(WD_API, params={"action": "wbgetentities", "ids": "|".join(ids), "props": "claims|labels",
                                         "languages": "en", "format": "json"}, headers=WD_HEADERS)
    out = []
    for qid in ids:
        e = (ents or {}).get("entities", {}).get(qid, {})
        for c in e.get("claims", {}).get("P856", []):
            url = c.get("mainsnak", {}).get("datavalue", {}).get("value")
            if url:
                out.append((qid, e.get("labels", {}).get("en", {}).get("value", ""), url))
    return out


def domain_of(url):
    host = urllib.parse.urlparse(url if "//" in url else f"https://{url}").hostname or ""
    return host.lower().removeprefix("www.")


# --- UK Companies House ----------------------------------------------------------------------------

CH = "https://api.company-information.service.gov.uk"


def ch_headers():
    key = os.environ.get("COMPANIES_HOUSE_KEY", "").strip()
    if not key:
        raise http.FetchError("Set COMPANIES_HOUSE_KEY (free key from developer.company-information.service.gov.uk)")
    return {"Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode()}


def ch_company(number):
    """Profile: name, status, SIC codes, registered country."""
    p = http.get_json(f"{CH}/company/{number}", headers=ch_headers())
    if not p:
        return None
    addr = p.get("registered_office_address") or {}
    return {"company_number": number, "name": p.get("company_name", ""), "status": p.get("company_status", ""),
            "sic_codes": ";".join(p.get("sic_codes", [])), "country": addr.get("country", "")}


def ch_officers(number):
    """Officers with appointment and resignation dates (resignations are job changes)."""
    data = http.get_json(f"{CH}/company/{number}/officers", params={"items_per_page": 100}, headers=ch_headers())
    return [{"company_number": number, "name": o.get("name", ""), "role": o.get("officer_role", ""),
             "occupation": o.get("occupation", ""), "appointed_on": o.get("appointed_on", ""),
             "resigned_on": o.get("resigned_on", "")} for o in (data or {}).get("items", [])]


# --- Domain finder (the company's own website) -------------------------------------------------------

LEGAL = {"inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd", "limited", "lp", "llp", "plc",
         "pbc", "holdings", "holding", "the", "and", "of"}
TLDS = (".com", ".ai", ".io", ".co")
PARKED = re.compile(r"domain (is )?for sale|buy this domain|parked|hugedomains|godaddy|sedo|dan\.com|this domain|coming soon", re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
SITE_NAME = re.compile(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', re.I)
SITE_HEADERS = {"User-Agent": "bench-data/0.1 (+https://promptingcompany.com)"}


def name_tokens(company):
    words = re.sub(r"[^a-z0-9 ]", " ", company.lower().replace("&", " and ")).split()
    return [w for w in words if w not in LEGAL]


def domain_guesses(company, max_guesses=8):
    t = name_tokens(company)
    if not t:
        return []
    stems = ["".join(t), "-".join(t)]
    if len(t) > 1 and len(t[0]) >= 5:
        stems.append(t[0])
    if len(t) > 2:
        stems.append("".join(t[:2]))
    stems = list(dict.fromkeys(s for s in stems if len(s) >= 3))
    return [s + tld for tld in TLDS for s in stems][:max_guesses]


def page_names(html_text):
    """The homepage's title and og:site_name, unescaped."""
    out = [m.group(1) for m in (TITLE.search(html_text), SITE_NAME.search(html_text)) if m]
    return [htmllib.unescape(re.sub(r"\s+", " ", s)).strip() for s in out]


def match_score(company, names):
    """Share of the company's name tokens found in the page's title or site name (spaces ignored)."""
    tokens = name_tokens(company)
    hay = " ".join(names).lower()
    flat = re.sub(r"[^a-z0-9]", "", hay)
    if not tokens or PARKED.search(hay):
        return 0.0
    if "".join(tokens) in flat:
        return 1.0
    return sum(t in flat for t in tokens) / len(tokens)


def _check(company, domain):
    """(score, first page name) for one guessed domain; (0, "") if it doesn't answer. A site that
    answers with a rate limit, block or server error gets one more try a second later; a domain that
    doesn't exist doesn't, so misses stay fast."""
    for attempt in range(2):
        try:
            body = http.get(f"https://{domain}/", headers=SITE_HEADERS, timeout=6, tries=1)
            break
        except http.FetchError as e:
            if e.status is None or attempt:
                return 0.0, ""
            time.sleep(1)
    if not body:
        return 0.0, ""
    names = page_names(body[:200_000].decode("utf-8", "replace"))
    return match_score(company, names), (names or [""])[0]


def find_domain(company, batch=4):
    """Try name-based domains, `batch` at a time in priority order, and stop at the first batch with a
    full match (the earliest guess in that batch wins). Returns (domain, status, page title, tried);
    status is match, review (partial match) or unknown."""
    guesses, tried, best = domain_guesses(company), [], (None, 0.0, "")
    with ThreadPoolExecutor(max_workers=batch) as pool:
        for start in range(0, len(guesses), batch):
            chunk = guesses[start:start + batch]
            tried += chunk
            for d, (score, title) in zip(chunk, pool.map(lambda d: _check(company, d), chunk)):
                if score == 1.0:
                    return d, "match", title, tried
                if score > best[1]:
                    best = (d, score, title)
    if best[0] and best[1] >= 0.5:
        return best[0], "review", best[2], tried
    return "", "unknown", "", tried
