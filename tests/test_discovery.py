"""Scheme discovery (myScheme, National Portal, catalog), news extraction and the 'reported' rules."""
import json

import pytest

from scraper import build, catalog, myscheme, portal, run, store, validate
from scraper.extract import (DISBURSEMENT, NEW_SCHEME, REVISED_ALLOCATION, Candidate, classify, extract_candidates,
                             find_amounts, find_beneficiaries, find_benefit, find_department, fy_of)
from scraper.fetch import Fetcher
from scraper.news import canonical_url
from scraper.verify import GovtDoc, find_allocation

FY = "2025-26"
FUNDING = {"centrally_sponsored": [r"Integrated Child Development|\bICDS\b", r"Pradhan Mantri Awas|\bPMAY"],
           "overrides": {"KA-gruha-lakshmi": {"funding": "state", "evidence": "by hand"}}}


# ---------------------------------------------------------------- extraction
def test_regional_amounts_need_a_rupee_sign_or_word():
    assert [a for _, a in find_amounts("₹700 करोड़ का बजट")] == [700]
    assert [a for _, a in find_amounts("36 हजार करोड़ रुपये का प्रावधान")] == [36000]
    assert [a for _, a in find_amounts("ரூ.13,807 கோடி ஒதுக்கீடு")] == [13807]
    assert [a for _, a in find_amounts("₹1.2 लाख करोड़")] == [120000]
    assert find_amounts("2 करोड़ महिलाओं को लाभ") == []          # a head count, not money


def test_benefit_beneficiaries_department():
    s = ("Under the Mahila Samman Yojana 2.5 crore women will get Rs 2,100 per month, "
         "the Women and Child Development Department said.")
    assert find_benefit(s) == {"amount_rs": 2100, "per": "month"}
    assert find_beneficiaries(s) == 25_000_000
    assert find_department(s) == "Women and Child Development Department"
    assert find_department("issued by the Department of Social Justice and Empowerment.") == \
        "Department of Social Justice and Empowerment"
    assert find_benefit("₹1,500 प्रति माह दिए जाएंगे") == {"amount_rs": 1500, "per": "month"}
    assert find_benefit("Rs 500 crore a year") is None           # an allocation, not a benefit


def test_article_types():
    assert classify("The state released Rs 3,690 crore as the 15th instalment") == DISBURSEMENT
    assert classify("Allocation for the scheme was hiked to Rs 5,000 crore") == REVISED_ALLOCATION
    assert classify("CM launches new scheme for farmers") == NEW_SCHEME
    assert classify("किसानों के खाते में किस्त जारी की गई") == DISBURSEMENT
    assert classify("The scheme continues this year") == ""


def test_hindi_article_gives_state_scheme_and_amount():
    text = "उत्तर प्रदेश सरकार ने मुख्यमंत्री कन्या सुमंगला योजना के लिए ₹700 करोड़ का बजट रखा।"
    (c,) = extract_candidates(text, published="2025-02-20", lang="hi")
    assert (c.state, c.name, c.amount, c.lang) == ("UP", "मुख्यमंत्री कन्या सुमंगला योजना", 700, "hi")
    assert c.fy == "2025-26"   # a February budget story is about the coming FY


def test_disbursement_and_launch_without_allocation_are_candidates():
    rel = extract_candidates("The state released Rs 3,690 crore under Majhi Ladki Bahin Yojana to 2.4 crore "
                             "beneficiaries.", title="Maharashtra", published="2026-10-10")
    assert rel[0].kind == DISBURSEMENT and rel[0].beneficiaries == 24_000_000 and rel[0].fy == "2026-27"
    new = extract_candidates("Delhi CM launched the Mahila Samman Yojana on Monday.", published="2026-10-12")
    assert new[0].kind == NEW_SCHEME and new[0].amount is None and new[0].state == "DL"


def test_fy_of():
    assert fy_of("2026-03-31") == "2025-26" and fy_of("2026-04-01") == "2026-27"
    assert fy_of("2026-02-01", budget=True) == "2026-27"


def test_canonical_url_merges_amp_and_tracking():
    a = canonical_url("https://www.example.com/news/story-1/amp?utm_source=x&id=4#top")
    b = canonical_url("https://example.com/news/story-1?id=4".replace("example", "www.example"))
    assert a == b == "https://www.example.com/news/story-1?id=4"


# ---------------------------------------------------------------- names and catalog
def test_name_score_keeps_pm_and_cm_schemes_apart():
    assert store.name_score("Pradhan Mantri Awas Yojana", "Mukhyamantri Awas Yojana") == 0
    assert store.name_score("Majhi Ladki Bahin Yojana", "Mukhyamantri Majhi Ladki Bahin Yojana") == 100
    assert store.name_score("Pension Scheme", "Destitute Women Pension Scheme") < 88


def test_catalog_dedupes_names_and_prefers_myscheme():
    cat = []
    catalog.seed_aliases(cat, {"MH": {"Mukhyamantri Majhi Ladki Bahin Yojana": ["Ladki Behen Yojana"]}}, FUNDING)
    rec, created = catalog.merge(cat, {"state": "MH", "name": "Ladki Behen Yojana", "via": "news",
                                       "department": "guess"}, FUNDING)
    assert not created and len(cat) == 1
    rec, _ = catalog.merge(cat, {"state": "MH", "name": "Mukhyamantri Majhi Ladki Bahin Yojana", "via": "myscheme",
                                 "department": "Women and Child Development Department", "level": "state",
                                 "description": "Monthly aid for women.",
                                 "source": {"title": "myScheme", "url": "https://www.myscheme.gov.in/schemes/mmlby"}},
                           FUNDING)
    assert rec["department"] == "Women and Child Development Department"
    assert rec["official_url"].startswith("https://www.myscheme.gov.in/")
    assert rec["funding"] == "state" and "myScheme" in rec["funding_evidence"]
    assert set(rec["discovered_via"]) == {"config", "news", "myscheme"}


def test_funding_classification():
    assert catalog.classify_funding(["Integrated Child Development Scheme"], "state", FUNDING)[0] == "centrally_sponsored"
    assert catalog.classify_funding(["PMAY-Gramin"], "central", FUNDING)[0] == "centrally_sponsored"
    assert catalog.classify_funding(["Mukhyamantri Kanya Sumangala Yojana"], None, FUNDING)[0] == "state"
    assert catalog.classify_funding(["PM-KISAN Samman Nidhi"], "central", FUNDING)[0] == "central"
    assert catalog.classify_funding(["Farmer Welfare Fund"], None, FUNDING) == ("unknown", "")
    rec, _ = catalog.merge([], {"state": "KA", "name": "Gruha Lakshmi", "via": "news"}, FUNDING)
    assert rec["funding"] == "state" and rec["funding_evidence"] == "by hand"


MYSCHEME_PAGE = {"statusCode": 200, "data": {"hits": {"page": {"total": 2}, "items": [
    {"id": "a", "fields": {"schemeName": "Mukhyamantri Majhi Ladki Bahin Yojana", "slug": "mmlby", "level": "State",
                           "nodalMinistryName": "Women and Child Development Department",
                           "briefDescription": "Monthly aid for women.", "beneficiaryState": ["Maharashtra"]}},
    {"id": "b", "fields": {"schemeName": "Pradhan Mantri Kisan Samman Nidhi", "slug": "pm-kisan", "level": "Central",
                           "nodalMinistryName": "Ministry of Agriculture and Farmers Welfare",
                           "beneficiaryState": ["All"]}},
    {"id": "c", "fields": {"schemeName": "", "slug": "broken"}}]}}}


def test_myscheme_parse_and_discover(monkeypatch):
    items, total = myscheme.parse_search(MYSCHEME_PAGE)
    assert total == 2 and [i["level"] for i in items] == ["state", "central"]
    assert items[0]["url"] == "https://www.myscheme.gov.in/schemes/mmlby"

    class FakeFetcher:
        def get(self, url, **kw):
            assert kw["headers"] == {"x-api-key": "k"}
            return json.dumps(MYSCHEME_PAGE).encode(), "application/json"

    cfg = {"search_api": "https://api.example/search", "page_size": 50}
    monkeypatch.delenv("MYSCHEME_API_KEY", raising=False)
    assert myscheme.discover(FakeFetcher(), cfg, {"MH": "Maharashtra"})[1].startswith("skipped")
    monkeypatch.setenv("MYSCHEME_API_KEY", "k")
    entries, skipped = myscheme.discover(FakeFetcher(), cfg, {"MH": "Maharashtra"})
    assert skipped is None and [(e["state"], e["level"]) for e in entries] == [("MH", "state"), ("CENTRE", "central")]


def test_national_portal_links():
    html = (b'<a href="/schemes/kisan">Pradhan Mantri Kisan Samman Nidhi</a><a href="/about">About us</a>'
            b'<a href="https://x.gov.in/y">Read more</a>')
    assert portal.scheme_links(html, "https://www.india.gov.in/list") == [
        ("Pradhan Mantri Kisan Samman Nidhi", "https://www.india.gov.in/schemes/kisan")]


# ---------------------------------------------------------------- pipeline rules
CFG = {"match": {"verified_min_score": 90, "review_min_score": 72, "amount_tolerance": 0.005, "window_chars": 400},
       "funding": FUNDING}


def cand(name="Majhi Ladki Bahin Yojana", amount=36000.0, url="https://news.example/a", kind="existing_scheme",
         fy=FY, **kw):
    return Candidate("MH", name, amount, "s", 0.9, url, "headline", "2025-03-10", {"feed": "Paper"}, kind=kind,
                     fy=fy, **kw)


def test_reported_scheme_keeps_news_figure_out_of_announced():
    schemes, queue = [], []
    assert run.process_candidate(cand(), schemes, queue, [], CFG, FY, set(), []) == "not_found"
    (rec,) = schemes
    assert rec["status"] == "reported" and rec["announced"] is None and rec["reported_amount"] == 36000
    assert rec["reports"][0]["kind"] == "existing_scheme" and rec["reports"][0]["outlet"] == "Paper"


def test_disbursement_update_is_never_an_allocation():
    schemes = []
    out = run.process_candidate(cand(amount=3690, kind=DISBURSEMENT), schemes, [], [], CFG, FY, set(), [])
    assert out == "reported-disbursement"
    assert schemes[0]["reported_amount"] is None and schemes[0]["reports"][0]["amount_cr"] == 3690


def test_several_articles_one_scheme():
    schemes, cat = [], []
    catalog.seed_aliases(cat, {"MH": {"Mukhyamantri Majhi Ladki Bahin Yojana": ["Ladki Behen Yojana"]}}, FUNDING)
    run.process_candidate(cand("Ladki Behen Yojana", url="https://a.example/1"), schemes, [], [], CFG, FY, set(), cat)
    run.process_candidate(cand("Majhi Ladki Bahin Yojana", url="https://b.example/2"), schemes, [], [], CFG, FY, set(), cat)
    run.process_candidate(cand("Majhi Ladki Bahin Yojana", url="https://b.example/2"), schemes, [], [], CFG, FY, set(), cat)
    assert len(schemes) == 1 and schemes[0]["name"] == "Mukhyamantri Majhi Ladki Bahin Yojana"
    assert len(schemes[0]["reports"]) == 2 and schemes[0]["catalog_id"] == cat[0]["id"]


def test_other_fy_story_kept_for_its_own_year():
    schemes = []
    run.process_candidate(cand(fy="2026-27"), schemes, [], [GovtDoc("https://x.gov.in", "Majhi Ladki Bahin Yojana "
                                                                    "Rs 36,000 crore")], CFG, FY, set(), [])
    assert schemes[0]["fy"] == "2026-27" and schemes[0]["status"] == "reported"


def test_news_figure_confirmed_in_govt_document():
    schemes = []
    doc = GovtDoc("https://finance.maharashtra.gov.in/b.pdf", "Majhi Ladki Bahin Yojana provision Rs 36,000 crore")
    assert run.process_candidate(cand(), schemes, [], [doc], CFG, FY, set(), []) == "verified"
    assert schemes[0]["announced"] == 36000 and schemes[0]["status"] == "verified"


def test_catalog_allocation_goes_to_review_then_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "REVIEW", tmp_path / "review.json")
    monkeypatch.setattr(run, "REJECTED", tmp_path / "rejected.json")
    cat = [catalog.merge([], {"state": "UP", "name": "Kanya Sumangala Yojana", "via": "myscheme",
                              "source": {"title": "t", "url": "https://www.myscheme.gov.in/schemes/mksy"}}, FUNDING)[0]]
    docs = [GovtDoc("https://budget.up.nic.in/a.pdf", "Mukhya ... Kanya  Sumangala Yojana – Rs 700 crore provided.")]
    schemes, queue = [], []
    assert run.match_catalog("UP", cat, schemes, queue, docs, FY, set(), 10) == 1
    assert schemes == [] and queue[0]["kind"] == "allocation" and queue[0]["govt_amount"] == 700
    assert run.match_catalog("UP", cat, schemes, queue, docs, FY, set(), 10) == 0   # not queued twice
    queue[0]["decision"] = "approve"
    run.REVIEW.write_text(json.dumps(queue))
    assert run.apply_review_decisions(schemes, FY) == []
    assert schemes[0]["status"] == "verified" and schemes[0]["announced"] == 700


def test_find_allocation_needs_one_amount_near_an_exact_name():
    docs = [GovtDoc("https://x.gov.in/a.pdf", "Gruha Lakshmi Rs 28,608 crore and Gruha Jyothi Rs 10,100 crore")]
    assert find_allocation(["Anna Bhagya"], docs).outcome == "not_found"
    assert find_allocation(["Gruha Lakshmi"], docs, window=40).govt_amount == 28608
    assert find_allocation(["Gruha Lakshmi"], docs, window=400).outcome == "not_found"   # two amounts: ambiguous


def test_validation_and_build_hide_unconfirmed_amounts():
    bad = [{"state": "MH", "fy": FY, "name": "A", "announced": 10, "status": "reported", "sources": []},
           {"state": "MH", "fy": FY, "name": "B", "announced": 10, "status": "verified",
            "sources": [{"kind": "news", "url": "u"}]},
           {"state": "MH", "fy": FY, "name": "C", "announced": None, "status": "reported", "sources": [],
            "funding": "mixed", "reports": [{"kind": "rumour"}]}]
    issues = []
    validate.check_schemes(bad, FY, issues)
    assert [i["field"] for i in issues] == ["scheme A", "scheme B", "scheme C", "scheme C"]
    assert build.public_scheme({**bad[0], "reported_amount": 10})["announced"] is None


# ---------------------------------------------------------------- polite fetching
class _Resp:
    def __init__(self, code):
        self.status_code, self.content, self.headers = code, b"x", {"content-type": "text/html"}

    def raise_for_status(self):
        pass


def test_fetcher_caps_requests_and_backs_off(tmp_path):
    f = Fetcher("bot", delay=0, cache_dir=tmp_path, max_per_host=2)
    f._robots["https://a.example"] = None
    f._robots["https://b.example"] = None
    calls = []
    f.s.get = lambda url, **kw: calls.append(url) or _Resp(429 if "b.example" in url else 200)
    assert f.get("https://a.example/1") and f.get("https://a.example/2")
    assert f.get("https://a.example/3") is None                     # cap reached
    assert f.get("https://b.example/1") is None and f.get("https://b.example/2") is None
    assert calls == ["https://a.example/1", "https://a.example/2", "https://b.example/1"]   # backed off after 429


def test_fetcher_respects_robots(tmp_path):
    import urllib.robotparser
    f = Fetcher("YojanaTrackerBot", delay=0, cache_dir=tmp_path)
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(["User-agent: *", "Disallow: /rss"])
    f._robots["https://news.example"] = rp
    f.s.get = lambda url, **kw: pytest.fail("must not fetch")
    assert f.get("https://news.example/rss/search?q=yojana") is None
