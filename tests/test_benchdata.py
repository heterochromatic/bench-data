"""Offline tests: every source answers from recorded fixtures through a fake transport. Run:
    python3 -I -m unittest discover -s tests
"""
import collections
import csv
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO))

from benchdata import build, http, sec, sources  # noqa: E402

ROUTES = [  # (substring of URL, fixture file); first match wins
    ("full-index/2025/QTR2/form.idx", "form.idx"), ("full-index/2026/QTR2/form.idx", "form.idx"),
    ("/1000001/000100000125000004/index.json", "index-form4.json"),
    ("/1000001/000100000125000004/form4-2025.xml", "form4-2025.xml"),
    ("/2000002/000100000125000004/form4-2025.xml", "form4-2025.xml"),
    ("/2000002/000400000426000077/form4-2026.xml", "form4-2026.xml"),
    ("/3000003/000300000326000001/primary_doc.xml", "formd.xml"),
    ("/5000005/000500000526000001/primary_doc.xml", "formd-fund.xml"),
    ("CIK0001000001.json", "sub-acme.json"), ("CIK0002000002.json", "sub-jane.json"),
    ("/1000001/000100000126000210/acme-10k.htm", "acme-10k.htm"),
    ("/1000001/000100000126000805/index.json", "index-8k.json"),
    ("/1000001/000100000126000805/acme-ex99_1.htm", "acme-ex99.htm"),
    ("boards-api.greenhouse.io/v1/boards/acme/", "greenhouse.json"),
    ("https://betalabs.com/", "home-parked.html"), ("https://beta-labs.com/", "home-beta.html"),
    ("action=wbsearchentities", "wd-search.json"), ("action=wbgetentities", "wd-entities.json"),
    ("/company/09999999/officers", "ch-officers.json"), ("/company/09999999/filing-history", "ch-filings.json"),
    ("/advanced-search/companies", "ch-search.json"), ("/document/DOC123/content", "ch-accounts.xhtml"),
    ("https://deltadata.co.uk/", "home-delta.html"), ("/company/09999999", "ch-company.json"),
]


class FakeTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        for part, name in ROUTES:
            if part in url:
                return 200, (FIX / name).read_bytes()
        return 404, b""


def read(p):
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class BenchDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.fake = FakeTransport()
        self._orig = http.transport
        http.transport = self.fake
        http.MIN_INTERVAL.clear()
        http.DEFAULT_INTERVAL = 0
        os.environ["SEC_CONTACT"] = "Test Runner test@example.com"
        os.environ["COMPANIES_HOUSE_KEY"] = "test-key"
        os.environ.pop("BENCHDATA_CACHE", None)

    def tearDown(self):
        http.transport = self._orig
        self.tmp.cleanup()
        for k in ("SEC_CONTACT", "COMPANIES_HOUSE_KEY"):
            os.environ.pop(k, None)

    def csv_of(self, name, rows, cols):
        p = self.out / name
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        return str(p)

    def test_sec_contact_required_and_sent(self):
        os.environ.pop("SEC_CONTACT")
        with self.assertRaises(http.FetchError):
            sec.headers()
        os.environ["SEC_CONTACT"] = "Test Runner test@example.com"
        sec.submissions(1000001)
        self.assertIn("test@example.com", self.fake.calls[-1][1]["User-Agent"])

    def test_form_index_and_dedupe(self):
        rows = sec.parse_form_index((FIX / "form.idx").read_text())
        self.assertEqual(rows[0], {"form": "4", "company": "ACME SOFTWARE INC", "cik": "1000001", "date": "2025-06-10",
                                   "accession": "0001000001-25-000004"})
        self.assertEqual(len(build.sample(rows, 1)), 4)   # the two Form 4 lines are one filing

    def test_formd_executives_only(self):
        build.main(["formd", "--out", str(self.out), "--since", "2026-04-01", "--until", "2026-06-30"])
        [f] = read(self.out / "formd-filings.csv")
        self.assertEqual((f["company"], f["hq_country"], f["total_offering"], f["first_sale"]),
                         ("Beta Labs Inc", "US", "12000000", "2026-06-01"))
        people = read(self.out / "formd-executives.csv")
        self.assertEqual([(p["first_name"], p["last_name"]) for p in people], [("Ana", "Lopez")])
        self.assertTrue(people[0]["source_url"].endswith("primary_doc.xml"))
        build.main(["formd", "--out", str(self.out), "--since", "2026-04-01", "--until", "2026-06-30", "--include-funds"])
        self.assertEqual(len(read(self.out / "formd-filings.csv")), 2)

    def test_officers_seed_and_truth(self):
        build.main(["officers", "--out", str(self.out), "--since", "2025-06-01", "--until", "2025-06-30"])
        [s] = read(self.out / "officers-seed.csv")
        self.assertEqual((s["name"], s["jobtitle"], s["company"]), ("Smith Jane", "Chief Marketing Officer", "Acme Software Inc"))
        build.main(["officers-truth", "--out", str(self.out), "--seed", str(self.out / "officers-seed.csv"), "--recent-days", "100000"])
        [t] = read(self.out / "t3-ground-truth.candidates.csv")
        self.assertEqual((t["true_title"], t["true_company"], t["changed"]), ("CMO", "Gamma Cloud Corp", "Y"))
        self.assertIn("2026-06-15", t["officer_changes_at_seed_company"])

    def test_board_seat_elsewhere_is_not_a_job_change(self):
        build.main(["officers", "--out", str(self.out), "--since", "2025-06-01", "--until", "2025-06-30"])
        sub = (FIX / "sub-jane.json").read_text()
        orig = http.transport
        def t(url, h, to):
            if "CIK0002000002.json" in url:
                return 200, sub.replace("xslF345X05/form4-2026.xml", "xslF345X05/form4-director-elsewhere.xml").encode()
            if "form4-director-elsewhere.xml" in url:
                return 200, (FIX / "form4-director-elsewhere.xml").read_bytes()
            return orig(url, h, to)
        http.transport = t
        build.main(["officers-truth", "--out", str(self.out), "--seed", str(self.out / "officers-seed.csv"), "--recent-days", "100000"])
        [r] = read(self.out / "t3-ground-truth.candidates.csv")
        self.assertEqual((r["changed"], r["true_company"]), ("N", "Acme Software Inc"))   # the 2025 officer filing at Acme still counts

    def test_officers_truth_unknown_when_stale(self):
        build.main(["officers", "--out", str(self.out), "--since", "2025-06-01", "--until", "2025-06-30"])
        build.main(["officers-truth", "--out", str(self.out), "--seed", str(self.out / "officers-seed.csv"), "--recent-days", "1"])
        [t] = read(self.out / "t3-ground-truth.candidates.csv")
        self.assertEqual(t["changed"], "unknown")

    def test_firmographics_with_headcount_and_wikidata(self):
        ciks = self.csv_of("ciks.csv", [["1000001"]], ["cik"])
        build.main(["firmographics", "--out", str(self.out), "--ciks", ciks, "--headcount", "--wikidata"])
        [f] = read(self.out / "firmographics.candidates.csv")
        self.assertEqual((f["domain"], f["hq_country"], f["employees"], f["website_source"]),
                         ("acme.example.com", "US", "1240", "sec"))
        self.assertIn("1,240 full-time employees", f["employees_snippet"])
        self.assertEqual(sources.wikidata_websites("Beta Labs"), [("Q123", "Beta Labs", "https://betalabs.example.org/")])

    def test_press_contacts_keep_named_on_domain(self):
        ciks = self.csv_of("ciks.csv", [["1000001"]], ["cik"])
        build.main(["press-contacts", "--out", str(self.out), "--ciks", ciks, "--since", "2026-01-01"])
        rows = read(self.out / "press-contacts.candidates.csv")
        emails = {r["email"]: r for r in rows}
        self.assertNotIn("ir@acme.example.com", emails)
        self.assertEqual(emails["maria.chen@acme.example.com"]["name"], "Maria Chen")
        self.assertEqual(emails["maria.chen@acme.example.com"]["on_domain"], "True")
        self.assertEqual(emails["tom@prfirm.example.net"]["on_domain"], "False")
        self.assertEqual(sec.contacts_from_text("Media Contact: Amazon Investor Relations, jane@amazon.example.com")[0][0], "")
        self.assertEqual(sec.contacts_from_text("Contact: 100 Kinney Avenue, Suite 5, bob@acme.example.com")[0][0], "")
        self.assertEqual(sec.contacts_from_text("Contact: Berry Senior, kris.newton@acme.example.com")[0][0], "Kris Newton")
        self.assertEqual(sec.name_from_email("jsmith@acme.example.com"), "")
        self.assertEqual(sec.contacts_from_text("Contact: investor.relations@acme.example.com")[0][0], "")

    def test_jobs_salesforce_and_sdr(self):
        companies = self.csv_of("companies.csv", [["acme.example.com", "Acme"]], ["domain", "name"])
        build.main(["jobs", "--out", str(self.out), "--companies", companies])
        [j] = read(self.out / "job-evidence.csv")
        self.assertEqual((j["board"], j["salesforce_evidence"], j["sdr_openings"]), ("greenhouse", "strong", "1"))
        self.assertTrue(j["salesforce_evidence_url"].endswith("/jobs/2"))

    def test_events_watchlist(self):
        ciks = self.csv_of("watch.csv", [["1000001"], ["3000003"]], ["cik"])
        build.main(["events", "--out", str(self.out), "--ciks", ciks, "--since", "2026-06-01", "--until", "2026-06-30"])
        kinds = sorted(r["event_type"] for r in read(self.out / "t4-events.candidates.csv"))
        self.assertEqual(kinds, ["funding (Form D)", "job change (8-K 5.02)"])

    def test_domains_from_homepage_title(self):
        build.main(["formd", "--out", str(self.out), "--since", "2026-04-01", "--until", "2026-06-30"])
        build.main(["domains", "--out", str(self.out), "--companies", str(self.out / "formd-filings.csv"),
                    "--executives", str(self.out / "formd-executives.csv")])
        [d] = read(self.out / "domains.candidates.csv")
        self.assertEqual((d["domain"], d["status"]), ("beta-labs.com", "match"))   # betalabs.com is parked, skipped
        self.assertTrue(d["tried"].startswith("betalabs.com;beta-labs.com"))
        [c] = read(self.out / "contacts.candidates.csv")
        self.assertEqual((c["first_name"], c["company_domain"]), ("Ana", "beta-labs.com"))
        self.assertEqual(sources.find_domain("Nonexistent Widgets Inc")[1], "unknown")

    def test_public_contacts(self):
        build.main(["public-contacts", "--out", str(self.out), "--since", "2025-06-01", "--until", "2025-06-30"])
        [c] = read(self.out / "public-contacts.candidates.csv")
        self.assertEqual((c["first_name"], c["last_name"], c["title"]), ("Jane", "Smith", "Chief Marketing Officer"))
        self.assertEqual((c["company_domain"], c["domain_source"], c["employees"]), ("acme.example.com", "sec", "1240"))
        self.assertEqual((c["segment_size"], c["segment_region"]), ("ent", "na"))
        self.assertEqual(build.split_sec_name("DOE JOHN A JR"), ("John", "Doe"))
        self.assertEqual((build.size_segment(50), build.size_segment(51), build.size_segment(1001)), ("smb", "mid", "ent"))

    def test_10k_headcount_and_website(self):
        cases = {
            "As of June 30, 2026, the Bank had 158 full-time equivalent employees, consisting of": 158,
            "we employed approximately 1,576,000 full-time and part-time employees.": 1576000,
            "As of December 27, 2025, we had a total of 10,791 employees.": 10791,
            "combined with approximately 2.1 million associates as of January": 2100000,
            "As of December 31, 2025, we employed 569 persons worldwide, of whom 408": 569,
            "During fiscal 2026, a total of 18,261 shares were awarded under the plan to certain associates": None,
            "In 2024 there were approximately 38.5 million people with diabetes in the United States.": None,
            "employed at the parent company. 26 We believe that our people are our greatest asset": None,
        }
        for text, want in cases.items():
            self.assertEqual(sec.headcount_from_10k(text)[0], want, text)
        self.assertEqual(sec.website_from_10k("filings are available on our website at https://investors.acme.co.uk/sec")[0], "acme.co.uk")
        self.assertEqual(sec.website_from_10k("posted on the SEC's website at www.sec.gov and our website, www.beta.example.com.")[0], "beta.example.com")

    def test_uk_contacts(self):
        build.main(["uk-contacts", "--out", str(self.out), "--companies", "1"])
        [c] = read(self.out / "uk-contacts.candidates.csv")   # the resigned director is left out
        self.assertEqual((c["first_name"], c["last_name"], c["title"]), ("Lee", "Kim", "Chief Revenue Officer"))
        self.assertEqual((c["company_domain"], c["employees"], c["segment_size"], c["segment_region"]),
                         ("deltadata.co.uk", "84", "mid", "emea"))
        self.assertEqual(sources.split_ch_name("PATEL, Ravi Kumar"), ("Ravi", "Patel"))

    def test_env_file_loads_without_overriding(self):
        envf = self.out / ".env"
        envf.write_text("# x\nBD_TEST_A=from-file\nCOMPANIES_HOUSE_KEY=file-key\n")
        build.load_env(envf)
        self.assertEqual((os.environ["BD_TEST_A"], os.environ["COMPANIES_HOUSE_KEY"]), ("from-file", "test-key"))
        os.environ.pop("BD_TEST_A")

    def test_w1_assemble_caps_cells_and_dedupes(self):
        c = self.out / "cands"
        c.mkdir()
        cols = ["first_name", "last_name", "company", "company_domain", "person_source_url", "domain_source_url"]
        with open(c / "contacts.candidates.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(cols)
            w.writerows([[f"F{i}", "L", "Co", f"d{i}.example.com", "u", "u"] for i in range(5)] + [["F0", "L", "Co", "d0.example.com", "u", "u"]])
        pcols = ["first_name", "last_name", "company", "company_domain", "employees", "segment_size", "segment_region", "hq_country", "employees_source_url", "cik"]
        with open(c / "public-contacts.candidates.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(pcols)
            w.writerows([["A", "B", "Big", "big.example.com", "5000", "ent", "us", "US", "k", "1"], ["C", "D", "Nod", "", "", "", "", "", "", "2"],
                         ["E", "F", "Apac", "ap.example.com", "900", "mid", "apac", "JP", "k", "3"]])
        with open(c / "press-contacts.candidates.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["cik", "name", "email", "on_domain", "source_url"])
            w.writerow(["1", "Gina Gold", "gina.gold@big.example.com", "True", "https://sec.example/ex99"])
        build.main(["w1-assemble", "--out", str(self.out), "--candidates", str(c), "--cap", "3"])
        rows = read(self.out / "w1-input.csv")
        cells = collections.Counter(f"{r['segment_size']}/{r['segment_region']}" for r in rows)
        self.assertEqual(dict(cells), {"smb/na": 3, "ent/na": 2})   # capped, no APAC, no domainless row, F0 once, gold kept
        self.assertEqual({r["segment_startup"] for r in rows if r["segment_size"] == "smb"}, {"Y"})
        [g] = read(self.out / "w1-email-gold.candidates.csv")
        self.assertEqual(g["known_email"], "gina.gold@big.example.com")
        gold_row = next(r for r in rows if r["input_id"] == g["input_id"])
        self.assertEqual((gold_row["first_name"], gold_row["company_domain"], gold_row["segment_size"]), ("Gina", "big.example.com", "ent"))

    def test_t1_excludes_w1_companies_and_t5_dedupes(self):
        c = self.out / "pool"
        c.mkdir()
        with open(c / "contacts.candidates.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["first_name", "last_name", "company", "company_domain"])
            w.writerows([["A", "One", "Co1", "one.example.com"], ["B", "Two", "Co2", "two.example.com"], ["C", "Three", "Co2", "two.example.com"]])
        excl = self.csv_of("w1.csv", [["W1", "x", "y", "one.example.com"]], ["input_id", "first_name", "last_name", "company_domain"])
        build.main(["t1-assemble", "--out", str(self.out), "--candidates", str(c), "--exclude", excl])
        rows = read(self.out / "t1-input.csv")
        self.assertEqual(sorted(r["company_domain"] for r in rows), ["two.example.com", "two.example.com"])
        build.main(["t5-assemble", "--out", str(self.out), "--candidates", str(c)])
        self.assertEqual(sorted(r["domain"] for r in read(self.out / "t5-domains.csv")), ["one.example.com", "two.example.com"])

    def test_verify_sources_keeps_only_confirmed_rows(self):
        gold = self.csv_of("w1-email-gold.candidates.csv",
                           [["W1", "maria.chen@acme.example.com", "", "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000805/acme-ex99_1.htm"],
                            ["W2", "nobody@acme.example.com", "", "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000805/acme-ex99_1.htm"]],
                           ["input_id", "known_email", "known_phone", "source_url"])
        build.main(["verify-sources", "--out", str(self.out), "--file", gold])
        self.assertEqual([r["input_id"] for r in read(self.out / "w1-email-gold.verified.csv")], ["W1"])
        self.assertEqual(read(self.out / "w1-email-gold.rejected.csv")[0]["verify_status"], "value not in source")
        firm = self.csv_of("w1-firmographics-truth.candidates.csv",
                           [["W3", "1240", "Software", "US", "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000210/acme-10k.htm"]],
                           ["input_id", "true_employee_count", "true_industry", "true_hq_country", "source_url"])
        build.main(["verify-sources", "--out", str(self.out), "--file", firm])
        self.assertEqual(len(read(self.out / "w1-firmographics-truth.verified.csv")), 1)

    def test_sic_companies(self):
        idx = (FIX / "form.idx").read_text() + "10-K        ACME SOFTWARE INC                                             1000001     2026-02-20  edgar/data/1000001/0001000001-26-000210.txt\n"
        orig = http.transport
        http.transport = lambda u, h, t: (200, idx.encode()) if "form.idx" in u else orig(u, h, t)
        build.main(["sic-companies", "--out", str(self.out), "--sic", "7370-7374", "--since", "2026-01-01", "--until", "2026-03-31"])
        [c] = read(self.out / "sic-companies.candidates.csv")
        self.assertEqual((c["domain"], c["sic"], c["employees"], c["hq_country"]), ("acme.example.com", "7372", "1240", "US"))

    def test_t3_duplicate_variants(self):
        r = {"firstname": "Janet", "middle": "Anne", "company": "GIBRALTAR INDUSTRIES, INC."}
        self.assertEqual(build.dup_variant(r, 0)["firstname"], "Janet A.")
        self.assertEqual(build.dup_variant(r, 1)["company"], "GIBRALTAR INDUSTRIES")
        self.assertEqual(build.dup_variant({**r, "middle": "", "company": "Acme"}, 0)["company"], "Acme")

    def test_t5_rule(self):
        q = build.t5_qualifies
        self.assertEqual(q({"employees": "120", "industry": "Services-Prepackaged Software", "hq_country": "US"}), "Y")
        self.assertEqual(q({"employees": "900", "industry": "Services-Prepackaged Software", "hq_country": "US"}), "N")
        self.assertEqual(q({"employees": "120", "industry": "Biotechnology", "hq_country": "US"}), "N")
        self.assertEqual(q({"employees": "120", "industry": "Services-Prepackaged Software", "hq_country": "GB"}), "N")
        self.assertEqual(q({"employees": "", "industry": "Services-Prepackaged Software", "hq_country": "US"}), "unknown")
        self.assertEqual(q({"employees": "120", "industry": "Services-Computer Integrated Systems Design", "hq_country": "US"}), "unknown")
        self.assertEqual(q({"employees": "120", "industry": "Services-Prepackaged Software", "hq_country": "CA"}), "Y")

    def test_headcount_in_source(self):
        h = build._headcount_in
        self.assertTrue(h("As of June 30 we had approximately 2,100 employees.", "2100"))
        self.assertTrue(h("Employees. We had 150 full-time staff.", "150"))
        self.assertFalse(h("Revenue was $150 million. We have many employees.", "150"))
        self.assertFalse(h("We have 1,120 employees.", "120"))
        self.assertFalse(h("We sold 150 units this year.", "150"))

    def test_headcount_totals_not_parts(self):
        h = lambda t: sec.headcount_from_10k(t)[0]
        self.assertEqual(h("As of December 31, 2025, the Company employed 723 full-time and 32 part-time employees."), 723)
        self.assertIsNone(h("As of the filing, we have approximately 1,200 employees who are engaged in the development of products."))
        self.assertEqual(h("We had approximately 23,600 employees, comprised of approximately 15,600 full-time and 8,000 part-time employees."), 23600)
        self.assertEqual(h("As of June 30, 2026, we had over 3,500 employees in our research and development organization. "
                           "Human capital. We had approximately 9,000 employees in total."), 9000)

    def test_headcount_ignores_acquired_company_far_from_heading(self):
        text = ("Item 1. As of June 30, we have approximately 2,100 employees. " + "filler " * 600 +
                "to keep our employees and attract new ones. " + "filler " * 600 + "Acme has more than 200 employees.")
        self.assertEqual(sec.headcount_from_10k(text)[0], 2100)

    def test_verticals_and_agencies(self):
        self.assertEqual(build.vertical("Services-Prepackaged Software"), "Software")
        self.assertEqual(build.vertical("Biotechnology"), "Healthcare")
        self.assertEqual(build.vertical("", "73110"), "Marketing and media")
        self.assertEqual(build.vertical("", "62012"), "Software")
        self.assertEqual(build.vertical("Unclassifiable"), "")
        self.assertTrue(build.is_agency("", "62012;73110"))
        self.assertTrue(build.is_agency("Services-Advertising Agencies"))
        self.assertEqual((build.region("CA"), build.region("GB"), build.region("JP"), build.region("BR")), ("na", "emea", "apac", ""))
        self.assertEqual((build.seniority("Chief Financial Officer"), build.seniority("SVP, Sales"), build.seniority("Director of IT"),
                          build.seniority("Controller"), build.seniority("")), ("c_suite", "vp", "director", "other", "unknown"))
        self.assertEqual((build.seniority("executive officer"), build.seniority("company director")), ("executive", "executive"))
        self.assertEqual(build.job_function("company director"), "unknown")
        self.assertTrue(all(len(g.split(".")[0]) <= 63 for g in sources.domain_guesses("A" * 80 + " Holdings Inc")))
        self.assertEqual((build.job_function("Chief Financial Officer"), build.job_function("SVP, Sales"),
                          build.job_function("Chief Marketing Officer"), build.job_function("General Counsel")),
                         ("finance", "sales", "marketing", "legal"))
        self.assertEqual((build.funding_band("500000"), build.funding_band("9000000"), build.funding_band("Indefinite")),
                         ("under_1m", "1m_10m", ""))

    def test_companies_house(self):
        nums = self.csv_of("uk.csv", [["09999999"]], ["company_number"])
        build.main(["uk", "--out", str(self.out), "--numbers", nums])
        [c] = read(self.out / "uk-companies.csv")
        self.assertEqual((c["name"], c["sic_codes"]), ("DELTA DATA LTD", "62012"))
        officers = read(self.out / "uk-officers.csv")
        self.assertEqual([o["resigned_on"] for o in officers], ["", "2026-07-31"])
        self.assertTrue(self.fake.calls[-1][1]["Authorization"].startswith("Basic "))

    def test_pacing_holds_across_threads(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        http.MIN_INTERVAL["slow.example"] = 0.05
        t0 = time.monotonic()
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: http._pace("slow.example"), range(4)))
        self.assertGreaterEqual(time.monotonic() - t0, 0.14)   # 4 calls, 3 gaps of 0.05 s

    def test_domain_guess_retries_once_on_block(self):
        calls = []

        def flaky(url, h, t):
            calls.append(url)
            if url == "https://flakyco.com/" and calls.count(url) == 1:
                return 429, b""
            if url == "https://flakyco.com/":
                return 200, b"<title>FlakyCo | Home</title>"
            return None, b"nodename nor servname provided"
        http.transport = flaky
        orig_sleep = sources.time.sleep
        sources.time.sleep = lambda s: None
        try:
            self.assertEqual(sources.find_domain("FlakyCo Inc")[:2], ("flakyco.com", "match"))
        finally:
            sources.time.sleep = orig_sleep

    def test_sec_rate_limit_waits_minutes(self):
        self.assertEqual(http._backoff("www.sec.gov", 429, 0), 30)
        self.assertEqual(http._backoff("data.sec.gov", 429, 2), 120)
        self.assertEqual(http._backoff("example.org", 503, 1), 4)

    def test_client_errors_not_retried_and_auth_dropped_on_redirect(self):
        calls = []
        http.transport = lambda url, h, t: (calls.append(url), (400, b""))[1]
        with self.assertRaises(http.FetchError):
            http.get("https://example.org/bad")
        self.assertEqual(len(calls), 1)
        import urllib.request
        req = urllib.request.Request("https://api.example.org/doc", headers={"Authorization": "Basic x"})
        new = http._DropAuthOnRedirect().redirect_request(req, None, 302, "Found", {}, "https://storage.example.net/f")
        self.assertFalse(new.has_header("Authorization"))
        same = http._DropAuthOnRedirect().redirect_request(req, None, 302, "Found", {}, "https://api.example.org/f2")
        self.assertTrue(same.has_header("Authorization"))

    def test_job_board_403_means_no_board(self):
        http.transport = lambda url, h, t: (403, b"") if "ashbyhq" in url else (404, b"")
        self.assertEqual(sources.find_board("nothere.example.com"), (None, None, []))

    def test_retry_then_give_up(self):
        http.transport = lambda url, h, t: (503, b"")
        orig_sleep = http.time.sleep
        http.time.sleep = lambda s: None
        try:
            with self.assertRaises(http.FetchError):
                http.get("https://example.org/x", tries=2)
        finally:
            http.time.sleep = orig_sleep


if __name__ == "__main__":
    unittest.main()
