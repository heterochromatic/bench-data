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
    ("CIK0001000001.json", "sub-acme.json"), ("CIK0002000002.json", "sub-jane.json"),
    ("/1000001/000100000126000210/acme-10k.htm", "acme-10k.htm"),
    ("/1000001/000100000126000805/index.json", "index-8k.json"),
    ("/1000001/000100000126000805/acme-ex99_1.htm", "acme-ex99.htm"),
    ("boards-api.greenhouse.io/v1/boards/acme/", "greenhouse.json"),
    ("action=wbsearchentities", "wd-search.json"), ("action=wbgetentities", "wd-entities.json"),
    ("/company/09999999/officers", "ch-officers.json"), ("/company/09999999", "ch-company.json"),
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
        self.assertEqual(len(build.sample(rows, 1)), 3)   # the two Form 4 lines are one filing

    def test_formd_executives_only(self):
        build.main(["formd", "--out", str(self.out), "--since", "2026-04-01", "--until", "2026-06-30"])
        [f] = read(self.out / "formd-filings.csv")
        self.assertEqual((f["company"], f["hq_country"], f["total_offering"], f["first_sale"]),
                         ("Beta Labs Inc", "US", "12000000", "2026-06-01"))
        people = read(self.out / "formd-executives.csv")
        self.assertEqual([(p["first_name"], p["last_name"]) for p in people], [("Ana", "Lopez")])
        self.assertTrue(people[0]["source_url"].endswith("primary_doc.xml"))

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

    def test_companies_house(self):
        nums = self.csv_of("uk.csv", [["09999999"]], ["company_number"])
        build.main(["uk", "--out", str(self.out), "--numbers", nums])
        [c] = read(self.out / "uk-companies.csv")
        self.assertEqual((c["name"], c["sic_codes"]), ("DELTA DATA LTD", "62012"))
        officers = read(self.out / "uk-officers.csv")
        self.assertEqual([o["resigned_on"] for o in officers], ["", "2026-07-31"])
        self.assertTrue(self.fake.calls[-1][1]["Authorization"].startswith("Basic "))

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
