"""SEC EDGAR: filing indexes, company submissions, Forms 3/4 (officers), Form D (private rounds and
their executives), 8-K Item 5.02 (officer changes), 10-K headcount, press-release contacts.

The SEC requires a contact in every request's User-Agent: set SEC_CONTACT (e.g. "Name email@example.com").
"""
import html
import os
import re
import xml.etree.ElementTree as ET
from datetime import date

from . import http

WWW = "https://www.sec.gov"
DATA = "https://data.sec.gov"


def headers():
    contact = os.environ.get("SEC_CONTACT", "").strip()
    if not contact:
        raise http.FetchError("Set SEC_CONTACT to a name and email: the SEC requires it in every request")
    return {"User-Agent": f"TPC benchmark research {contact}", "Accept-Encoding": "identity"}


def archive_url(cik, accession, filename=""):
    return f"{WWW}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{filename}"


# --- Indexes -------------------------------------------------------------------------------------

def _quarters(since, until):
    y, q = since.year, (since.month - 1) // 3 + 1
    while (y, q) <= (until.year, (until.month - 1) // 3 + 1):
        yield y, q
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def parse_form_index(text):
    """Rows of a form.idx file: form, company, cik, date, accession."""
    rows, started = [], False
    for line in text.splitlines():
        if line.startswith("-----"):
            started = True
            continue
        if not started or not line.strip():
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        form, company, cik, filed, path = parts[0], " ".join(parts[1:-3]), parts[-3], parts[-2], parts[-1]
        accession = path.rsplit("/", 1)[-1].removesuffix(".txt")
        rows.append({"form": form, "company": company, "cik": cik, "date": filed, "accession": accession})
    return rows


def index(forms, since: date, until: date):
    """Filings of the given form types filed between two dates (quarterly full index)."""
    out = []
    for y, q in _quarters(since, until):
        text = http.get_text(f"{WWW}/Archives/edgar/full-index/{y}/QTR{q}/form.idx", headers=headers())
        for r in parse_form_index(text or ""):
            if r["form"] in forms and since.isoformat() <= r["date"] <= until.isoformat():
                out.append(r)
    return out


# --- Companies -----------------------------------------------------------------------------------

def submissions(cik):
    return http.get_json(f"{DATA}/submissions/CIK{int(cik):010d}.json", headers=headers())


def recent_filings(sub):
    """The submissions file's recent filings as dicts (form, date, accession, doc, items)."""
    r = (sub or {}).get("filings", {}).get("recent", {})
    keys = [("form", "form"), ("date", "filingDate"), ("accession", "accessionNumber"),
            ("doc", "primaryDocument"), ("items", "items")]
    n = len(r.get("form", []))
    return [{k: (r.get(src) or [""] * n)[i] for k, src in keys} for i in range(n)]


US_STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
                "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split())
# EDGAR's own codes for a few common non-US locations (full list: EDGAR state and country codes).
EDGAR_COUNTRY = {"X0": "GB", "A6": "AU", "A1": "CA", "Z4": "CA", "2M": "DE", "I0": "FR", "M5": "JP", "L2": "IL",
                 "K3": "HK", "U0": "SG", "F4": "CN", "L3": "IE", "P7": "NL", "V8": "SE", "W8": "CH", "M8": "KR"}


def company_facts(sub):
    """Name, website, SIC industry and HQ country from a submissions file."""
    biz = (sub.get("addresses") or {}).get("business") or {}
    code = (biz.get("stateOrCountry") or "").upper()
    country = "US" if code in US_STATES else EDGAR_COUNTRY.get(code, "")
    return {"name": sub.get("name", ""), "website": (sub.get("website") or "").strip(),
            "sic": sub.get("sic", ""), "sic_description": sub.get("sicDescription", ""),
            "hq_country": country, "hq_code": code}


# --- Forms 3/4: officers --------------------------------------------------------------------------

def raw_xml_name(primary_doc):
    """Form 3/4 primaryDocument points at the XSL-rendered view; the raw XML has the same file name."""
    return re.sub(r"^xsl[^/]+/", "", primary_doc)


def _t(el, path):
    found = el.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_ownership(xml_text):
    """Issuer and reporting officers from a Form 3/4 XML document."""
    root = ET.fromstring(xml_text)
    issuer = {"issuer_cik": _t(root, "issuer/issuerCik"), "issuer_name": _t(root, "issuer/issuerName"),
              "period": _t(root, "periodOfReport")}
    people = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        is_officer = _t(rel, "isOfficer").lower() in ("1", "true") if rel is not None else False
        people.append({**issuer, "owner_cik": _t(ro, "reportingOwnerId/rptOwnerCik"),
                       "owner_name": _t(ro, "reportingOwnerId/rptOwnerName"),
                       "officer_title": _t(rel, "officerTitle") if rel is not None else "",
                       "is_officer": is_officer})
    return people


def ownership(cik, accession, primary_doc=""):
    """Parsed Form 3/4. Without a primary document name, the XML file is found from the filing index."""
    name = raw_xml_name(primary_doc) if primary_doc else ""
    if not name:
        idx = http.get_json(archive_url(cik, accession, "index.json"), headers=headers())
        name = next((i["name"] for i in (idx or {}).get("directory", {}).get("item", []) if i["name"].endswith(".xml")), "")
    if not name:
        return [], archive_url(cik, accession)
    url = archive_url(cik, accession, name)
    text = http.get_text(url, headers=headers())
    return (parse_ownership(text) if text else []), url


# --- Form D: private funding rounds ---------------------------------------------------------------

def parse_form_d(xml_text):
    root = ET.fromstring(xml_text)
    iss = root.find("primaryIssuer")
    off = root.find("offeringData")
    filing = {
        "company": _t(iss, "entityName") if iss is not None else "",
        "issuer_cik": _t(iss, "cik") if iss is not None else "",
        "state_or_country": _t(iss, "issuerAddress/stateOrCountry") if iss is not None else "",
        "industry_group": _t(off, "industryGroup/industryGroupType") if off is not None else "",
        "total_offering": _t(off, "offeringSalesAmounts/totalOfferingAmount") if off is not None else "",
        "total_sold": _t(off, "offeringSalesAmounts/totalAmountSold") if off is not None else "",
        "first_sale": _t(off, "typeOfFiling/dateOfFirstSale/value") if off is not None else "",
    }
    people = []
    for rp in root.findall("relatedPersonsList/relatedPersonInfo"):
        rels = [(r.text or "").strip() for r in rp.findall("relatedPersonRelationshipList/relationship")]
        people.append({**filing, "first_name": _t(rp, "relatedPersonName/firstName"),
                       "last_name": _t(rp, "relatedPersonName/lastName"), "relationship": "; ".join(rels)})
    return filing, people


def form_d(cik, accession):
    url = archive_url(cik, accession, "primary_doc.xml")
    text = http.get_text(url, headers=headers())
    return (None, [], url) if not text else (*parse_form_d(text), url)


# --- 8-K Item 5.02 and 10-K headcount ---------------------------------------------------------------

def officer_changes(sub, since=""):
    """8-K filings reporting Item 5.02 (departure or appointment of officers) since a date."""
    return [f for f in recent_filings(sub) if f["form"] == "8-K" and "5.02" in (f["items"] or "") and f["date"] >= since]


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))


EMPLOYEES = re.compile(r"(?:approximately|about|had|employ(?:ed)?)\s+([\d,]{2,9})\s+(?:full[- ]time\s+|total\s+)?(?:employees|people|team members)", re.I)


def headcount_from_10k(text):
    """First 'approximately N employees' statement, with a snippet for the reviewer."""
    m = EMPLOYEES.search(text)
    if not m:
        return None, ""
    return int(m.group(1).replace(",", "")), text[max(0, m.start() - 80): m.end() + 40].strip()


def latest_10k_headcount(cik, sub):
    k = next((f for f in recent_filings(sub) if f["form"] == "10-K"), None)
    if not k:
        return None, "", ""
    url = archive_url(cik, k["accession"], k["doc"])
    text = http.get_text(url, headers=headers())
    n, snippet = headcount_from_10k(html_to_text(text or ""))
    return n, snippet, url


# --- Press-release contacts (8-K exhibit 99) ---------------------------------------------------------

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
GENERIC = re.compile(r"^(info|ir|investor|investors|press|media|pr|news|contact|hello|sales|support|marketing)@", re.I)
NAME_BEFORE = re.compile(r"([A-Z][a-z]+(?: [A-Z]\.)? [A-Z][A-Za-z'-]+)[,\s]+(?:[A-Z][^@]{0,60}?)?$")


def contacts_from_text(text):
    """Named personal emails near a 'Contact' heading: (name, email, snippet). Generic inboxes are dropped."""
    out = []
    for m in EMAIL.finditer(text):
        email = m.group(0).rstrip(".")
        if GENERIC.match(email):
            continue
        before = text[max(0, m.start() - 160): m.start()]
        if "contact" not in before.lower():
            continue
        nm = NAME_BEFORE.search(before.strip())
        out.append((nm.group(1) if nm else "", email, (before[-100:] + email).strip()))
    return out


def press_contacts(cik, accession):
    idx = http.get_json(archive_url(cik, accession, "index.json"), headers=headers())
    files = [i["name"] for i in (idx or {}).get("directory", {}).get("item", []) if re.search(r"ex-?99", i["name"], re.I)]
    out = []
    for name in files:
        url = archive_url(cik, accession, name)
        for person, email, snippet in contacts_from_text(html_to_text(http.get_text(url, headers=headers()) or "")):
            out.append({"name": person, "email": email, "snippet": snippet, "source_url": url})
    return out
