import json

import pytest

from scraper import run, store
from scraper.extract import extract_candidates, find_amounts, slugify, to_crore
from scraper.verify import GovtDoc, verify


def test_amount_units():
    assert to_crore("36,000", "crore") == 36000
    assert to_crore("1.2", "lakh crore") == 120000
    assert [a for _, a in find_amounts("Rs. 3,500-crore and ₹ 51,034 cr and INR 2 lakh crore")] == [3500, 51034, 200000]


def test_extracts_state_scheme_amount():
    text = ("Deputy CM Ajit Pawar presented the Maharashtra budget for 2025-26. "
            "The government allocated Rs 36,000 crore for the Mukhyamantri Majhi Ladki Bahin Yojana. "
            "A new metro line was also announced.")
    c = extract_candidates(text, title="Maharashtra budget 2025-26 highlights")
    assert len(c) == 1
    assert c[0].state == "MH" and c[0].amount == 36000 and "Ladki Bahin Yojana" in c[0].name
    assert c[0].confidence == 0.9


def test_other_financial_year_is_tagged_not_dropped():
    text = "Maharashtra allocated Rs 26,500 crore for Ladki Bahin Yojana in 2026-27."
    (c,) = extract_candidates(text, title="Maharashtra budget", fy="2025-26")
    assert c.fy == "2026-27" and c.amount == 26500


def test_sentence_level_state_overrides_article():
    text = ("Southern states unveiled budgets. "
            "Karnataka set aside Rs 28,608 crore for the Gruha Lakshmi scheme. "
            "Tamil Nadu allocated Rs 13,807 crore for Kalaignar Magalir Urimai Thogai.")
    got = {(c.state, c.amount) for c in extract_candidates(text, title="State budgets")}
    assert got == {("KA", 28608), ("TN", 13807)}


DOC = ("DEMAND NO. 31 ... Mukhyamantri Majhi Ladki Bahin Yojana – provision for 2025-26 "
       "Rs. 36,000 crore towards monthly assistance ... other heads Rs. 120 crore")


def test_verify_exact():
    v = verify("Mukhyamantri Majhi Ladki Bahin Yojana", 36000, [GovtDoc("https://finance.maharashtra.gov.in/x.pdf", DOC)])
    assert v.outcome == "verified" and v.govt_amount == 36000


def test_verify_mismatch_uses_govt_figure():
    doc = "Kalaignar Magalir Urimai Thogai scheme: allocation Rs 13,722 crore for the year."
    v = verify("Kalaignar Magalir Urimai Thogai", 13807, [GovtDoc("https://tnbudget.tn.gov.in/a.pdf", doc)])
    assert v.outcome == "mismatch" and v.govt_amount == 13722


def test_verify_not_found():
    v = verify("Gruha Jyothi", 10000, [GovtDoc("https://finance.karnataka.gov.in/a.pdf", "Unrelated text Rs 5 crore")])
    assert v.outcome == "not_found"


def test_indian_number_format():
    assert run.indian_format(4077772)[0] == "40,77,772"
    assert run.indian_format(813476)[0] == "8,13,476"
    assert run.indian_format(950)[0] == "950"


def test_slug_and_fuzzy_merge():
    assert slugify("Mukhyamantri Majhi Ladki Bahin Yojana") == "majhi-ladki-bahin"
    recs = [{"id": "MH-x", "state": "MH", "fy": "2025-26", "name": "Mukhyamantri Majhi Ladki Bahin Yojana",
             "announced": None, "reported_amount": 36000, "status": "reported", "sources": []}]
    assert store.find_existing(recs, "MH", "Majhi Ladki Bahin Yojana", "2025-26") is recs[0]
    assert store.find_existing(recs, "MH", "Namo Shetkari Mahasanman Nidhi", "2025-26") is None


def test_upsert_never_downgrades_verified():
    recs = []
    src = {"kind": "news", "title": "t", "url": "u1"}
    store.upsert(recs, state="TN", name="Pudhumai Penn Thittam", fy="2025-26", amount=420, status="verified", source=src)
    store.upsert(recs, state="TN", name="Pudhumai Penn Thittam", fy="2025-26", amount=999, status="reported",
                 source={"kind": "news", "title": "t2", "url": "u2"})
    assert recs[0]["announced"] == 420 and recs[0]["status"] == "verified" and len(recs[0]["sources"]) == 2


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    for name in ("REVIEW", "REJECTED"):
        monkeypatch.setattr(run, name, tmp_path / f"{name.lower()}.json")
    return tmp_path


def test_review_decisions(tmp_data):
    queue = [
        {"state": "UP", "name": "Kanya Sumangala Yojana", "news_amount": 700, "govt_amount": 750,
         "govt_url": "https://budget.up.nic.in/a.pdf", "news_source": {"kind": "news", "title": "n", "url": "n1"},
         "decision": "approve", "use_amount": None},
        {"state": "UP", "name": "Fake Scheme", "news_amount": 5, "govt_amount": None, "govt_url": None,
         "news_source": {"kind": "news", "title": "n", "url": "n2"}, "decision": "reject"},
        {"state": "UP", "name": "Pending Scheme", "news_amount": 9, "govt_amount": None, "govt_url": None,
         "news_source": {"kind": "news", "title": "n", "url": "n3"}, "decision": None},
    ]
    run.REVIEW.write_text(json.dumps(queue))
    schemes = []
    remaining = run.apply_review_decisions(schemes, "2025-26")
    assert [r["name"] for r in remaining] == ["Pending Scheme"]
    assert schemes[0]["announced"] == 750 and schemes[0]["status"] == "verified"
    assert "UP|fake scheme|5" in json.loads(run.REJECTED.read_text())


def test_build_excludes_nothing_but_counts_status(tmp_path):
    from scraper import build
    site = build.build(tmp_path / "site.json", tmp_path / "validation.json")
    assert site["fy"] == "2025-26"
    mh = next(s for s in site["states"] if s["code"] == "MH")
    assert mh["schemes"][0]["status"] == "reported"
    assert mh["schemes"][0]["announced"] is None          # a news figure is never shown
    assert "reported_amount" not in mh["schemes"][0] and "reports" not in mh["schemes"][0]
    assert len(site["states"]) == 31
