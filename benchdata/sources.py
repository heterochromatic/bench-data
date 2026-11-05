"""Other free, first-hand sources: public job boards (Greenhouse, Lever, Ashby), Wikidata and
UK Companies House (needs COMPANIES_HOUSE_KEY, a free key)."""
import base64
import os
import re
import urllib.parse

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
