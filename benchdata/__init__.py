"""bench-data: build benchmark inputs and candidate answer keys from free, first-hand public records
(SEC EDGAR, public job-board APIs, Wikidata, UK Companies House). Standard library only.

Every row it writes carries its source URL and retrieval time. Its output is *candidates*: a person
cross-checks disagreements and runs `verify-sources` before anything becomes an answer key
(ANSWER-KEYS.md in each bench repo).
"""
__version__ = "0.1.4"
