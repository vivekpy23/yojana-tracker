"""Web search, official newsrooms, typed financial claims, AI-extraction guards, cross-script scheme identity and
disbursement summaries – with Bihar's Mukhyamantri Mahila Rojgar Yojana (CM Bihar release of 16 Feb 2026:
₹18,100 Cr to 1.81 crore women so far, ₹2,500 Cr of it transferred that day) as the end-to-end case. Offline:
a fake fetcher serves the saved release and made-up search results."""
import copy
import datetime as dt
import json
import time
from pathlib import Path

import pytest
import yaml

from scraper import build, claims, llm, newsroom, run, search, store
from scraper.extract import category, find_amounts, find_beneficiaries
from scraper.registry import Registry

SAMPLES = Path(__file__).parent / "samples"
RELEASE_URL = "https://cm.bihar.gov.in/users/viewnews.aspx?id=4107"
RELEASE = (SAMPLES / "cm_bihar_viewnews_4107.html").read_bytes()
CFG = yaml.safe_load((Path(run.__file__).with_name("config.yaml")).read_text(encoding="utf-8"))


def release_text():
    return newsroom.main_text(RELEASE, "text/html", RELEASE_URL)


class FakeFetcher:
    def __init__(self, pages: dict):
        self.pages, self.requested, self.last_from_cache = pages, [], False

    def get(self, url, *, max_age_h=0, headers=None):
        self.requested.append(url)
        for prefix, page in self.pages.items():
            if url == prefix or (prefix.endswith("*") and url.startswith(prefix[:-1])):
                return page
        return None

    def get_api(self, url, *, params=None, headers=None):
        return None


# ---------------------------------------------------------------- parsing
def test_hindi_compound_amounts_and_counts():
    assert [a for _, a in find_amounts("18 हजार 100 करोड़ रुपये, 15 हजार 600 करोड़ रूपये, 36 हजार करोड़ रुपये")] == \
        [18100, 15600, 36000]
    assert find_amounts("2 करोड़ महिलाएं") == []                       # a count, not money
    assert find_beneficiaries("1 करोड़ 81 लाख लाभुक महिलाओं के खाते में") == 18_100_000
    assert find_beneficiaries("25 लाख लाभुक महिलाओं") == 2_500_000


def test_release_claims_are_typed_and_dated():
    cs = claims.extract_claims(release_text(), state="BR")
    by = {(c["type"], c["amount_cr"], c["as_of"]) for c in cs}
    assert ("cumulative_disbursement", 18100, "2026-02-15") in by     # 'अब तक … 18 हजार 100 करोड़'
    assert ("disbursement_event", 2500, "2026-02-15") in by           # 'आज … 2,500 करोड़'
    assert ("cumulative_disbursement", 15600, None) in by             # 'इसके पूर्व … की जा चुकी है': earlier, undated
    total = next(c for c in cs if c["amount_cr"] == 18100)
    assert total["scheme"] == "मुख्यमंत्री महिला रोजगार योजना" and total["beneficiaries"] == 18_100_000
    assert total["published"] == "2026-02-16" and total["fy"] == "2025-26" and total["period_start"] == "2025-09-26"


def test_english_claim_types():
    text = ("Patna, Feb 15, 2026: Chief Minister Nitish Kumar released Rs 2,500 crore under the Mukhyamantri Mahila "
            "Rojgar Yojana today. So far Rs 18,100 crore has been transferred to 1.81 crore women under the scheme. "
            "The Bihar budget had allocated Rs 9,052 crore for the Mahila Sashaktikaran head.")
    cs = {c["amount_cr"]: c for c in claims.extract_claims(text, state="BR")}
    assert cs[2500]["type"] == "disbursement_event" and cs[2500]["as_of"] == "2026-02-15"
    assert cs[18100]["type"] == "cumulative_disbursement" and cs[18100]["beneficiaries"] == 18_100_000
    assert cs[9052]["type"] == "budget_allocation"


def test_scheme_names_match_across_scripts():
    en = "Mukhyamantri Mahila Rojgar Yojana"
    assert store.name_score("मुख्यमंत्री महिला रोजगार योजना", en) == 100
    assert store.name_score("महिला रोजगार योजना", "Mukhyamantri Mahila Rozgar Yojana") == 100   # short form, z/j
    assert store.name_score("मुख्यमंत्री महिला उद्यमी योजना", en) < 88                           # a different scheme
    assert store.name_score("प्रधानमंत्री आवास योजना", "Mukhyamantri Awas Yojana") == 0          # PM vs CM never merge
    assert category("मुख्यमंत्री महिला रोजगार योजना") == "welfare"


def test_romanised_name_becomes_the_name():
    schemes = []
    src = {"kind": "govt", "title": "CM Bihar", "url": RELEASE_URL}
    rec = store.upsert(schemes, state="BR", name="मुख्यमंत्री महिला रोजगार योजना", fy="2025-26", amount=None,
                       status="reported", source=src)
    store.upsert(schemes, state="BR", name="Mukhyamantri Mahila Rojgar Yojana", fy="2025-26", amount=None,
                 status="reported", source=src)
    assert len(schemes) == 1 and rec["name"] == "Mukhyamantri Mahila Rojgar Yojana"
    assert "मुख्यमंत्री महिला रोजगार योजना" in rec["aliases"]


# ---------------------------------------------------------------- financials: dedup and no double counting
def _claim(typ, amount, as_of, url, **kw):
    return {"type": typ, "amount_cr": amount, "as_of": as_of, "source": {"kind": "news", "url": url}, **kw}


def test_same_figure_from_two_sources_is_one_entry():
    rec = {}
    assert store.add_financial(rec, _claim("cumulative_disbursement", 18100, "2026-02-15", "https://a"))
    assert not store.add_financial(rec, _claim("cumulative_disbursement", 18100, "2026-02-15", "https://b"))
    assert store.add_financial(rec, _claim("disbursement_event", 2500, "2026-02-15", "https://a"))
    assert not store.add_financial(rec, _claim("disbursement_event", 2500, "2026-02-16", "https://c"))  # a day later
    assert len(rec["financials"]) == 2 and len(rec["financials"][0]["sources"]) == 2


def test_disbursed_is_the_latest_total_never_total_plus_transfers():
    rec = {}
    for c in (_claim("cumulative_disbursement", 15600, "2025-11-28", "https://a", period_start="2025-09-26"),
              _claim("cumulative_disbursement", 18100, "2026-02-15", "https://b", period_start="2025-09-26",
                     beneficiaries=18_100_000),
              _claim("disbursement_event", 2500, "2026-02-15", "https://b")):
        store.add_financial(rec, c)
    for f in rec["financials"]:
        f["verified"], f["verified_against"] = True, f["sources"][0]["url"]
    s = store.disbursement_summary(rec, "2025-26")
    assert s["amount"] == 18100 and s["kind"] == "cumulative" and s["as_of"] == "2026-02-15"
    assert s["beneficiaries"] == 18_100_000 and s["url"] == "https://b"


def test_transfers_are_summed_once_and_unverified_figures_ignored():
    rec = {}
    store.add_financial(rec, _claim("disbursement_event", 2500, "2026-02-15", "https://a"))
    store.add_financial(rec, _claim("disbursement_event", 2500, "2026-02-16", "https://b"))   # same transfer
    store.add_financial(rec, _claim("disbursement_event", 1000, "2025-12-01", "https://c"))
    store.add_financial(rec, _claim("disbursement_event", 900, "2026-01-01", "https://d"))     # stays unverified
    for f in rec["financials"][:2]:
        f["verified"] = True
    assert store.disbursement_summary(rec, "2025-26")["amount"] == 3500


def test_a_total_that_started_before_the_fy_is_not_this_years():
    rec = {}
    store.add_financial(rec, _claim("cumulative_disbursement", 50000, "2026-02-15", "https://a",
                                    period_start="2023-08-01"))
    rec["financials"][0]["verified"] = True
    assert store.disbursement_summary(rec, "2025-26") is None


# ---------------------------------------------------------------- AI extraction guards
def test_ai_claims_must_be_grounded_in_the_text():
    text = release_text()
    quote = "अब कुल मिलाकर 1 करोड़ 81 लाख महिलाओं के खाते में 18 हजार 100 करोड़ रूपये की राशि अंतरित कर दी गई है।"
    assert " ".join(quote.split()) in " ".join(text.split())
    good = {"type": "cumulative_disbursement", "amount_crore": 18100, "beneficiaries": 18100000,
            "as_of": "2026-02-15", "period_start": "2025-09-26", "quote": quote}
    data = {"published": "2026-02-16", "schemes": [{
        "name_as_written": "मुख्यमंत्री महिला रोजगार योजना", "name_english": "Mukhyamantri Mahila Rojgar Yojana",
        "state": "BR", "launch_date": "2025-09-26",
        "claims": [good,
                   {**good, "amount_crore": 20000},                                    # not what the quote says
                   {**good, "quote": "18,100 crore were transferred to women."}]}]}    # not in the text
    schemes, cs, rejected = llm.validate(data, text)
    assert schemes[0]["name"] == "Mukhyamantri Mahila Rojgar Yojana" and schemes[0]["launched"] == "2025-09-26"
    assert len(cs) == 1 and rejected == 2 and cs[0]["fy"] == "2025-26" and cs[0]["extractor"] == "llm"


def test_ai_extractor_uses_structured_output_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path)
    calls = []

    class Block:
        type, text = "text", json.dumps({"published": None, "schemes": []})

    class Resp:
        stop_reason, content = "end_turn", [Block()]

    class Client:
        class beta:
            class messages:
                @staticmethod
                def create(**kw):
                    calls.append(kw)
                    return Resp()

    ex = llm.Extractor({"enabled": True}, client=Client())
    ex.extract("some article text")
    ex.extract("some article text")
    assert len(calls) == 1 and ex.stats["cached"] == 1
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]


def test_ai_extractor_off_without_config():
    assert llm.Extractor({"enabled": False}).disabled


# ---------------------------------------------------------------- search providers, daily queries, budgets
BING_RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Bihar: CM launches second phase of Mahila Rojgar Yojana</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;url=https%3a%2f%2fnews.example.in%2fbihar-mahila-rojgar&amp;c=1</link>
<description>Rs 10,000 each to women</description><pubDate>Mon, 16 Feb 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_bing_news_returns_the_article_url_not_the_click_through():
    f = FakeFetcher({"https://www.bing.com/news/search*": (BING_RSS, "application/rss+xml")})
    (hit,) = search.BingNews({}).search(f, "बिहार नई योजना शुरू लाभार्थी", "hi", 10)
    assert hit["url"] == "https://news.example.in/bihar-mahila-rojgar" and hit["published"] == "2026-02-16"
    assert "setlang=hi" in f.requested[0]


def test_one_broad_daily_query_per_jurisdiction_rotating_theme_and_language():
    scfg = CFG["search"]
    days = [dt.date(2026, 10, 1) + dt.timedelta(d) for d in range(6)]
    seen = {search.daily_query(scfg, "BR", d)[1:] for d in days}
    assert seen == {(l, t) for t in ("new_schemes", "announcements", "disbursements") for l in ("hi", "en")}
    q, lang, _ = search.daily_query(scfg, "CENTRE", days[0])
    assert ("Union government" in q) if lang == "en" else ("केंद्र सरकार" in q)
    assert len({search.daily_query(scfg, c, days[0])[2] for c in ("BR", "UP", "MH", "TN", "KA", "RJ")}) > 1
    assert "Rojgar" not in yaml.safe_dump(scfg, allow_unicode=True)   # no scheme is named in the config


def test_brave_first_and_keyed_providers_skipped_without_keys(monkeypatch):
    for v in ("BRAVE_API_KEY", "GOOGLE_CSE_KEY", "GOOGLE_CSE_CX"):
        monkeypatch.delenv(v, raising=False)
    ready, skipped = search.providers(CFG["search"])
    assert [p.name for p in ready] == ["bing_news"] and set(skipped) == {"brave", "google_cse"}
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    assert [p.name for p in search.providers(CFG["search"])[0]] == ["brave", "bing_news"]


def test_budgets_hold_across_runs_on_the_same_day(tmp_path):
    from scraper.checkpoint import Budget, Checkpoint
    path = tmp_path / "state.json"
    brave = next(p for p in CFG["search"]["providers"] if p["name"] == "brave")
    cp = Checkpoint(today="2026-10-22", path=path)
    b = Budget(brave, cp)
    assert b.allow_discovery("BR")
    b.spend("discovery", "BR")
    assert not b.allow_discovery("BR") and b.allow_discovery("UP")
    for _ in range(brave["verification_per_day"]):
        assert b.allow_verification()
        b.spend("verification")
    assert not b.allow_verification()
    cp.save()
    again = Budget(brave, Checkpoint.load(path, today="2026-10-22"))      # a second run the same day
    assert not again.allow_discovery("BR")
    assert Budget(brave, Checkpoint.load(path, today="2026-10-23")).allow_discovery("BR")   # the next day
    capped = Budget({**brave, "monthly_cap": 4}, Checkpoint.load(path, today="2026-10-23"))
    assert not capped.allow_discovery("UP")                                   # 1 + 3 used this month


def test_registry_maps_domains_to_jurisdictions():
    reg = Registry.load()
    assert reg.jurisdiction_of("https://cm.bihar.gov.in/users/viewnews.aspx?id=4107") == "BR"
    assert reg.jurisdiction_of("https://pib.gov.in/PressReleasePage.aspx?PRID=1") == "CENTRE"
    assert reg.is_credible("https://www.livemint.com/x") and not reg.is_credible("https://spam.example.com/x")
    assert set(reg.jurisdictions) >= {s["code"] for s in json.loads((Path(run.__file__).parent.parent / "data" /
                                                                       "seed.json").read_text(encoding="utf-8"))["states"]}


def test_verification_query_uses_the_regional_name_on_the_official_domain():
    q = search.verification_query(CFG["search"], ["Mukhyamantri Mahila Rojgar Yojana", "मुख्यमंत्री महिला रोजगार योजना"],
                                  "BR", "bihar.gov.in")
    assert q == '"मुख्यमंत्री महिला रोजगार योजना" site:bihar.gov.in'


# ---------------------------------------------------------------- generic newsrooms
def _page(date_line: str, body: str) -> tuple[bytes, str]:
    html = (f'<div class="nav-menu">Home latest news</div><div class="post">Read News on {date_line} {body}</div>'
            f'<div class="sidebar-outer">Recent News 5 करोड़ रुपये अंतरित</div>')
    return html.encode(), "text/html"


LISTING = "https://cm.bihar.gov.in/users/news.aspx"
BASE = "https://cm.bihar.gov.in/users/viewnews.aspx?id="


def newsroom_pages(listed=range(4109, 4114)):
    pages = {LISTING: ("".join(f'<a href="viewnews.aspx?id={i}">x</a>' for i in listed).encode(), "text/html"),
             BASE + "4108": (b"<div>Read News</div>", "text/html"),       # no item at this id
             BASE + "4107": (RELEASE, "text/html"),
             BASE + "4106": _page("30-03-25", "पटना, 30 मार्च 2025 पिछला वर्ष।")}
    for i in listed:
        pages[BASE + str(i)] = _page("17-04-26", f"पटना, 17 अप्रैल 2026 समाचार {i}।")
    return pages


def test_generic_main_text_and_item_pattern_need_no_selector():
    text = newsroom.main_text(RELEASE, "text/html", RELEASE_URL)
    assert "Recent News" not in text and "18 हजार 100 करोड़" in text
    tpl, ids = newsroom.infer_template(newsroom_pages()[LISTING][0], LISTING)
    assert tpl == BASE + "{id}" and ids == [4109, 4110, 4111, 4112, 4113]


def test_newsroom_is_incremental_and_backfills_to_the_fy_start_once():
    room = {"name": "Bihar CM – news", "listing": LISTING, "lang": "hi", "max_misses": 3}
    state, audit = {}, {}
    items = newsroom.crawl(FakeFetcher(newsroom_pages()), room, "2025-26", state, audit)
    assert sorted(i.url[-4:] for i in items) == ["4107", "4109", "4110", "4111", "4112", "4113"]
    assert state["backfill_done"] and state["max_id"] == 4113 and audit["backfill_stop"].startswith("id 4106")
    rel = next(i for i in items if i.url.endswith("4107"))
    assert rel.published == "2026-02-16" and rel.as_of == "2026-02-15" and "Recent News" not in rel.text
    later = newsroom.crawl(FakeFetcher(newsroom_pages(range(4110, 4115))), room, "2025-26", state, {})
    assert [i.url[-4:] for i in later] == ["4114"]                  # next day: only the new item


# ---------------------------------------------------------------- end to end: discovered, verified, stored, shown
NEWS_URL = "https://news.example.in/bihar-mahila-rojgar"
NEWS = (b"<html><body><h1>Bihar: Rs 2,500 crore released under Mukhyamantri Mahila Rojgar Yojana</h1><p>Patna, "
        b"Feb 15, 2026: Chief Minister Nitish Kumar on Sunday released Rs 2,500 crore to 25 lakh women under the "
        b"Mukhyamantri Mahila Rojgar Yojana. So far Rs 18,100 crore has been transferred to 1.81 crore women under the "
        b"scheme, the government said.</p></body></html>", "text/html")
BRAVE_JSON = {"web": {"results": [
    {"url": NEWS_URL, "title": "Mahila Rojgar Yojana: Rs 18,100 crore so far", "description": "Bihar scheme crore",
     "page_age": "2026-02-16T09:00:00"},
    {"url": "https://spam.example.com/yojana-list", "title": "All yojana list 2026", "description": "yojana crore"}]}}


class BraveFetcher(FakeFetcher):
    def __init__(self, pages):
        super().__init__(pages)
        self.api_calls = []

    def get_api(self, url, *, params=None, headers=None):
        self.api_calls.append(params["q"])
        return BRAVE_JSON if "site:" not in params["q"] else {"web": {"results": []}}


def _ctx(schemes, states=("BR",)):
    return {"cfg": CFG, "fy": "2025-26", "states": set(states), "schemes": schemes, "catalog": [], "queue": [],
            "rejected": set(), "docs": {}, "tally": {}, "extractor": llm.Extractor({"enabled": False})}


REG = Registry({"credible_news": ["news.example.in"], "jurisdictions": {
    "BR": {"domains": ["bihar.gov.in"], "newsrooms": [{"name": "Bihar CM – news", "listing": LISTING, "lang": "hi",
                                                       "max_misses": 3}]},
    "CENTRE": {"domains": ["pib.gov.in"], "newsrooms": []}}})


def test_bihar_mahila_rojgar_end_to_end(monkeypatch, tmp_path):
    from scraper.checkpoint import Checkpoint
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    for v in ("GOOGLE_CSE_KEY", "GOOGLE_CSE_CX"):
        monkeypatch.delenv(v, raising=False)
    fetcher = BraveFetcher({**newsroom_pages(), "https://www.bing.com/news/search*": (BING_RSS, "application/rss+xml"),
                            NEWS_URL: NEWS})
    # what Bihar had before: PRS records, none of them this scheme
    schemes = [{"id": "BR-vriddhhajan-pension", "state": "BR", "fy": "2025-26",
                "name": "Mukhyamantri Vriddhhajan Pension Yojana", "announced": 828.0, "reported_amount": None,
                "despatched": None, "status": "secondary", "sources": [], "category": "welfare", "kind": "scheme"}]
    ctx = _ctx(schemes, ("BR", "CENTRE"))
    cp = Checkpoint(today="2026-10-22", path=tmp_path / "state.json")
    cov = run.discover_web(fetcher, ctx, cp, REG, deadline=time.time() + 600)
    run.verify_financials(schemes, ctx["docs"], CFG)
    cov = run.coverage_report(cov, schemes, cp, "2025-26")

    br = cov["jurisdictions"]["BR"]
    # search budget: exactly one discovery query per provider per jurisdiction (Union included); no verification
    # search needed, because the newsroom already verified the figure
    assert [q["provider"] for q in br["queries"]] == ["brave", "bing_news"]
    assert [q["provider"] for q in cov["jurisdictions"]["CENTRE"]["queries"]] == ["brave", "bing_news"]
    assert len(fetcher.api_calls) == 2 and br["verification_searches"] == []
    assert br["untrusted_skipped"] == 1                              # spam.example.com is not read
    # stored: one record, romanised name, Hindi alias, no allocation, typed and verified figures
    recs = [s for s in schemes if "Rojgar" in s["name"] or "रोजगार" in s["name"]]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["name"] == "Mukhyamantri Mahila Rojgar Yojana" and "मुख्यमंत्री महिला रोजगार योजना" in rec["aliases"]
    assert rec["announced"] is None and rec["category"] == "welfare" and rec["fy"] == "2025-26"
    total = next(f for f in rec["financials"] if f["type"] == "cumulative_disbursement" and f["amount_cr"] == 18100)
    assert total["verified"] and total["verified_against"] == RELEASE_URL and total["as_of"] == "2026-02-15"
    assert {s["url"] for s in total["sources"]} == {RELEASE_URL, NEWS_URL}           # one figure, two sources
    assert len([f for f in rec["financials"] if f["type"] == "disbursement_event"]) == 1
    # surfaced by generic search (not only the newsroom) – so it isn't in "missed by search"
    assert rec["id"] in cp.surfaced("BR") and rec["id"] not in [m["id"] for m in br["missed_by_search"]["examples"]]
    # displayed: ₹18,100 Cr disbursed (not 18,100 + 2,500), as of 15 Feb 2026, from the official release
    shown = build.public_scheme(rec)
    assert shown["despatched"] == 18100 and shown["despatched_source"]["url"] == RELEASE_URL
    assert shown["despatched_source"]["as_of"] == "2026-02-15" and shown["despatched_source"]["beneficiaries"] == 18_100_000
    assert shown["status"] == "verified" and shown["announced"] is None
    # incremental: a second run the same day makes no new searches and reads nothing again
    cp.save()
    fetcher.requested.clear()
    again = run.discover_web(fetcher, _ctx(schemes, ("BR", "CENTRE")), Checkpoint.load(tmp_path / "state.json",
                                                                                         today="2026-10-22"),
                             REG, deadline=time.time() + 600)
    assert len(fetcher.api_calls) == 2
    assert all("skipped" in q for c in again["jurisdictions"].values() for q in c["queries"])
    assert again["jurisdictions"]["BR"]["pages_read"] == 0


def test_figures_unverified_after_search_get_a_targeted_verification_search(monkeypatch, tmp_path):
    from scraper.checkpoint import Checkpoint
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    reg = Registry({"credible_news": ["news.example.in"],
                    "jurisdictions": {"BR": {"domains": ["bihar.gov.in"], "newsrooms": []}}})   # no newsroom
    fetcher = BraveFetcher({NEWS_URL: NEWS, "https://www.bing.com/news/search*": (BING_RSS, "application/rss+xml")})
    schemes = []
    cp = Checkpoint(today="2026-10-22", path=tmp_path / "s.json")
    cov = run.discover_web(fetcher, _ctx(schemes), cp, reg, deadline=time.time() + 600)
    v = cov["jurisdictions"]["BR"]["verification_searches"]
    assert v and all(x["query"].endswith("site:bihar.gov.in") for x in v)
    assert len(v) <= next(p for p in CFG["search"]["providers"] if p["name"] == "brave")["verification_per_day"]
    rec = next(s for s in schemes if "Rojgar" in s["name"])
    assert rec["despatched"] is None                                  # news alone is never shown


def test_the_deadline_stops_cleanly_and_logs_what_was_not_reached(tmp_path):
    from scraper.checkpoint import Checkpoint
    cov = run.discover_web(FakeFetcher({}), _ctx([], ("BR", "UP")), Checkpoint(path=tmp_path / "s.json"), REG,
                           deadline=time.time() - 1)
    assert all(c["not_reached"] for c in cov["jurisdictions"].values())


def test_an_article_about_another_state_is_not_filed_under_the_searched_one():
    text = "Bhopal: Madhya Pradesh CM released Rs 1,834 crore under the Ladli Behna Yojana to 1.27 crore women today."
    assert run.article_state(text, "Ladli Behna Yojana: Madhya Pradesh CM transfers funds", "BR") == "MP"
    assert run.article_state("A scheme story with no state.", "", "BR") is None


# ---------------------------------------------------------------- names: page furniture, headlines, canonical name
def test_page_furniture_and_headline_words_are_not_scheme_names():
    from scraper.extract import find_schemes
    assert [n for _, n in find_schemes("| Mint View Market Dashboard Mukhyamantri Mahila Rojgar Yojana: बिहार")] == \
        ["Mukhyamantri Mahila Rojgar Yojana"]
    assert [n for _, n in find_schemes("Prefer Livemint on Google Mukhyamantri Mahila Rojgar Yojana: x")] == \
        ["Mukhyamantri Mahila Rojgar Yojana"]
    assert find_schemes("Rs 10,000 Credited In Scheme") == []
    assert [n for _, n in find_schemes("The Haryana AI Mission will be launched")] == ["Haryana AI Mission"]
    assert [n for _, n in find_schemes("राज्य सरकार ने करते हुए कृषि इनपुट अनुदान योजना")] == ["कृषि इनपुट अनुदान योजना"]


def test_cross_script_single_word_names_need_the_same_prefix():
    assert store.name_score("मुख्यमंत्री उद्यमी योजना", "Chief Minister Udyami Yojana") == 100
    assert store.name_score("उद्यमी योजना", "Udyami Yojana") == 0
    assert store.name_score("मुख्यमंत्री महिला रोजगार योजना", "Bihar Mahila Rojgar Yojana") >= 88


def test_the_cleaner_name_wins():
    assert store.better_name("Bihar Mahila Rojgar Yojana", "Mukhyamantri Mahila Rojgar Yojana")
    assert store.better_name("मुख्यमंत्री महिला रोजगार योजना", "Mukhyamantri Mahila Rojgar Yojana")
    assert not store.better_name("Mukhyamantri Mahila Rojgar Yojana", "Mahila Rojgar Yojana")
    assert not store.better_name("Mukhyamantri Mahila Rojgar Yojana", "मुख्यमंत्री महिला रोजगार योजना")


def test_an_undated_earlier_total_never_creates_a_record():
    ctx = {"cfg": CFG, "fy": "2025-26", "states": {"BR"}, "schemes": [], "catalog": [], "queue": [],
           "rejected": set(), "docs": {}, "tally": {}, "extractor": llm.Extractor({"enabled": False})}
    text = ("पटना, 20 अगस्त 2026 बिहार सरकार। इसके पूर्व मुख्यमंत्री महिला रोजगार योजना के तहत 1 करोड़ 56 लाख "
            "महिलाओं के खाते में 15 हजार 600 करोड़ रुपये की राशि अंतरित की जा चुकी है।")
    run.ingest(text, url="https://x.bihar.gov.in/1", title="", published="2026-08-20", state_hint="BR", lang="hi",
               official=True, outlet="x", ctx=ctx)
    assert all(s["fy"] == "2026-27" for s in ctx["schemes"])   # the mention is kept in its own FY; no 2025-26 record
    run.verify_financials(ctx["schemes"], ctx["docs"], CFG)
    assert all(f["as_of"] is None and not f["verified"] for s in ctx["schemes"] for f in s.get("financials", []))
    assert all(s.get("despatched") is None for s in ctx["schemes"])  # never shown


def test_conflicting_figures_in_one_source_are_left_out_not_summed():
    rec = {}
    url = "https://cm.bihar.gov.in/users/viewnews.aspx?id=4001"
    for amount in (124734, 1247.34):   # headline typo vs text, same release, same day
        store.add_financial(rec, _claim("disbursement_event", amount, "2025-08-10", url))
    for f in rec["financials"]:
        f["verified"], f["verified_against"] = True, url
    assert store.disbursement_summary(rec, "2025-26") is None
    assert rec["financial_conflicts"][0]["amounts_cr"] == [1247.34, 124734]


def test_a_transfer_on_an_official_page_is_never_an_allocation():
    ctx = {"cfg": CFG, "fy": "2025-26", "states": {"BR"}, "schemes": [], "catalog": [], "queue": [],
           "rejected": set(), "docs": {}, "tally": {}, "extractor": llm.Extractor({"enabled": False})}
    text = ("पटना, 28 नवम्बर 2025 बिहार के मुख्यमंत्री ने आज 'मुख्यमंत्री महिला रोजगार योजना' की 10 लाख लाभुक महिलाओं के खाते में "
            "1000 करोड़ रुपये की राशि का अंतरण किया। मुख्यमंत्री ने 769.63 करोड़ रुपये की मुख्यमंत्री समग्र शहरी विकास "
            "योजना का शिलान्यास किया।")
    run.ingest(text, url="https://cm.bihar.gov.in/users/viewnews.aspx?id=4050", title="", published="2025-11-28",
               state_hint="BR", lang="hi", official=True, outlet="CM", ctx=ctx)
    assert all(s["announced"] is None for s in ctx["schemes"])
    urban = next(s for s in ctx["schemes"] if "शहरी" in s["name"])
    assert urban["category"] == "infrastructure"
