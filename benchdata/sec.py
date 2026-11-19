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


def quarter_closed(y, q, today=None):
    """True once the quarter is over (its full index no longer changes)."""
    return (date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)) <= (today or date.today())


def index(forms, since: date, until: date):
    """Filings of the given form types filed between two dates (quarterly full index)."""
    out = []
    for y, q in _quarters(since, until):
        # A quarter's index keeps growing until the quarter ends, so only closed quarters are cached.
        text = http.get_text(f"{WWW}/Archives/edgar/full-index/{y}/QTR{q}/form.idx", headers=headers(),
                             cache=quarter_closed(y, q))
        for r in parse_form_index(text or ""):
            if r["form"] in forms and since.isoformat() <= r["date"] <= until.isoformat():
                out.append(r)
    return out


def submissions(cik):
    # A company's submissions file changes with every new filing, so it is never cached.
    return http.get_json(f"{DATA}/submissions/CIK{int(cik):010d}.json", headers=headers(), cache=False)


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


def officer_changes(sub, since=""):
    """8-K filings reporting Item 5.02 (departure or appointment of officers) since a date."""
    return [f for f in recent_filings(sub) if f["form"] == "8-K" and "5.02" in (f["items"] or "") and f["date"] >= since]


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))


COUNT = re.compile(r"(\d(?:[\d,]*\d)?(?:\.\d+)?)(\s+million)?\s+((?:[A-Za-z-]+\s+){0,5}?)"
                   r"(employees|associates|colleagues|team members|persons|people|full-time equivalents)\b", re.I)
# Only these words may sit between the number and the noun ("1,240 full-time employees"), so
# "18,261 shares were awarded to associates" or "page 26 We believe our people" don't count.
BETWEEN_OK = {"full-time", "part-time", "full", "part", "time", "and", "temporary", "active", "equivalent", "employee",
              "regular", "permanent", "total", "global", "seasonal", "hourly", "salaried", "worldwide", "other", "our"}
CONTEXT = re.compile(r"employ|workforce|headcount|\bhad\b|total of|as of|approximately|network of|human capital", re.I)
OWN = re.compile(r"\bwe\s+(?:currently\s+)?(?:had|have|employ|employed)\b|\bour\s+(?:total\s+)?(?:workforce|headcount)\b", re.I)
# "723 full-time and 32 part-time employees": take the full-time count.
FULL_PART = re.compile(r"(\d(?:[\d,]*\d)?)\s+full[- ]time\s+(?:employees\s+|individuals\s+)?and\s+\d(?:[\d,]*\d)?\s+(?:part[- ]time|temporary)", re.I)
PART_BEFORE = re.compile(r"\b(?:from|covering|including|comprised of|consisting of|and|of whom|of which)\s+(?:approximately\s+|about\s+|over\s+|more than\s+)?$", re.I)
PART_AFTER = re.compile(r"\s+(?:(?:were\s+|are\s+)?(?:in|within)\s+(?:our\s+|the\s+)?(?:global\s+)?(?:research|r&d|sales|marketing|engineering|product|development|customer|support|manufacturing|technology)"
                        r"|in\s+(?:our|the|its)\s+[\w&\- ]{0,40}?(?:organization|business|unit|team|division|segment|department|function)"
                        r"|(?:who\s+(?:are|were)\s+)?(?:dedicated|engaged|working (?:in|on))|supporting (?:research|r&d|sales))", re.I)
HUMAN_CAPITAL = re.compile(r"human capital|\bemployees\b\s+(?:and|as of)|number of employees", re.I)


def headcount_from_10k(text):
    """Total headcount stated in a 10-K ("we had approximately 1,240 full-time employees"), searched
    from the Human Capital section first. Returns (count, snippet) or (None, "")."""
    hc = HUMAN_CAPITAL.search(text)
    # First the company speaking for itself ("we had 2,100 employees"), so a customer's or an acquired company's
    # headcount ("has more than 200 employees") doesn't win by appearing first; then the other patterns in order.
    return (_count_pass(text, 0, OWN) or _full_time_part(text)
            or (hc and _count_pass(text, hc.start(), CONTEXT)) or _count_pass(text, 0, CONTEXT) or (None, ""))


def _full_time_part(text):
    """ "723 full-time and 32 part-time employees": the full-time count."""
    for m in FULL_PART.finditer(text):
        if CONTEXT.search(text[max(0, m.start() - 70): m.start()]):
            return int(m.group(1).replace(",", "")), text[max(0, m.start() - 80): m.end() + 40].strip()
    return None


def _count_pass(text, start, context):
    """The first "N employees" from start whose preceding words match context and that isn't a part of the total."""
    for m in COUNT.finditer(text, start):
        words = m.group(3).lower().split()
        if any(w not in BETWEEN_OK for w in words) or not context.search(text[max(0, m.start() - 70): m.start()]):
            continue
        if PART_BEFORE.search(text[max(0, m.start() - 25): m.start()]) or PART_AFTER.match(text, m.end()):
            continue   # a department, a country, a union or an earlier year, not the total
        if m.group(4).lower() == "people" and not re.search(r"employ", text[max(0, m.start() - 80): m.end()], re.I):
            continue
        raw = m.group(1).replace(",", "")
        n = float(raw) * (1_000_000 if m.group(2) else 1)
        if not 1 <= n <= 3_000_000 or ("," not in m.group(1) and not m.group(2) and 1900 <= n <= 2100):
            continue
        return int(n), text[max(0, m.start() - 80): m.end() + 40].strip()
    return None


WEBSITE = re.compile(r"(?:website|web site|internet address)[^.]{0,100}?\b((?:https?://)?([a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}))", re.I)
NOT_COMPANY_SITES = ("sec.gov", "nasdaq.com", "nyse.com", "q4cdn.com", "q4inc.com", "gcs-web.com", "investorroom.com",
                     "linkedin.com", "twitter.com", "x.com", "facebook.com", "youtube.com", "instagram.com")


def registered_domain(host):
    """Drop the usual website prefixes: www.acme.com, investors.acme.com and ir.acme.com are acme.com."""
    host = host.lower().strip(".")
    while re.match(r"(www\d?|investors?|ir|corporate|about)\.[^.]+\.", host):
        host = host.split(".", 1)[1]
    return host


def website_from_10k(text):
    """The company's own website as the 10-K states it ("our website is www.acme.com"): (domain, snippet)."""
    for m in WEBSITE.finditer(text):
        host = re.sub(r"^https?://", "", m.group(1)).split("/")[0]
        dom = registered_domain(host)
        if "." in dom and not any(dom == x or dom.endswith("." + x) for x in NOT_COMPANY_SITES):
            return dom, text[max(0, m.start() - 20): m.end() + 20].strip()
    return "", ""


def latest_10k(cik, sub):
    """(text, url) of the company's latest 10-K, or ("", "") if it has none."""
    k = next((f for f in recent_filings(sub) if f["form"] == "10-K"), None)
    if not k:
        return "", ""
    url = archive_url(cik, k["accession"], k["doc"])
    return html_to_text(http.get_text(url, headers=headers()) or ""), url


def latest_10k_headcount(cik, sub):
    text, url = latest_10k(cik, sub)
    n, snippet = headcount_from_10k(text) if text else (None, "")
    return n, snippet, url


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
GENERIC = re.compile(r"^(info|ir|investor|investors|press|media|pr|news|contact|hello|sales|support|marketing)@", re.I)
NAME_BEFORE = re.compile(r"([A-Z][a-z]+(?: [A-Z]\.)? [A-Z][A-Za-z'-]+)[,\s]+(?:[A-Z][^@]{0,60}?)?$")


# Words that mean the captured "name" is a team, a place or a company, not a person.
NOT_A_PERSON = re.compile(r"\b(investor|investors|relations|media|press|pr|communications|corporate|department|team|group|office|strategic|"
                          r"avenue|street|road|drive|boulevard|suite|floor|plaza|inc|corp|llc|ltd|company|holdings|partners)\b", re.I)


def name_from_email(email):
    """'maria.chen@acme.com' -> 'Maria Chen' when the local part is two alphabetic words; else ''."""
    parts = re.split(r"[._-]", email.split("@")[0])
    return " ".join(w.title() for w in parts) if len(parts) == 2 and all(w.isalpha() and len(w) > 1 for w in parts) else ""


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
        name = nm.group(1) if nm and not NOT_A_PERSON.search(nm.group(1)) else ""
        # The address itself is the stronger evidence: prefer first.last@ when the text's name disagrees.
        from_email = name_from_email(email)
        from_email = "" if NOT_A_PERSON.search(from_email) else from_email
        if from_email and (not name or set(name.lower().split()) != set(from_email.lower().split())):
            name = from_email
        out.append((name, email, (before[-100:] + email).strip()))
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
