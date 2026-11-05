"""Offline tests: every source answers from recorded fixtures through a fake transport. Run:
    python3 -I -m unittest discover -s tests
"""
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
        self.assertEqual((c["segment_size"], c["segment_region"]), ("ent", "us"))
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
