"""CAG Monthly Key Indicators parser. Samples in tests/samples/ are excerpts of the real reports
(cag.gov.in state pages, and text of the 'Accounts at a Glance' table extracted with pdfplumber)."""
import copy
import json
from pathlib import Path

import pytest

from scraper import cag

S = Path(__file__).parent / "samples"
HOSTS = ["d7i5wg8xwe4hf.cloudfront.net"]


def txt(name):
    return (S / name).read_text(encoding="utf-8")


def test_state_pages_from_index():
    pages = cag.state_pages((S / "cag_index.html").read_bytes(), "https://cag.gov.in/en/state-accounts-report")
    assert pages["Andhra Pradesh"] == "https://cag.gov.in/en/state-accounts-report?defuat_state_id=64"


def test_march_report_picks_mki_tab_only():
    label, url = cag.march_report((S / "cag_state_mh.html").read_bytes(), 2026, HOSTS)
    assert label == "March, 2026"
    assert "MKI-MAH-March-2026" in url  # not the decoy link in another tab


def test_march_report_prefers_supplementary():
    label, url = cag.march_report((S / "cag_state_ka.html").read_bytes(), 2026, HOSTS)
    assert label == "March(Sup), 2026"


def test_march_report_rejects_other_hosts_and_years():
    html = (S / "cag_state_mh.html").read_bytes()
    assert cag.march_report(html, 2026, ["example.org"]) is None
    assert cag.march_report(html, 2031, HOSTS) is None


def test_parse_maharashtra_matches_pdf():
    # Checked by eye against page 1 of the March 2026 PDF: rows (4), (5), (7), (8), (a), (vi).
    ind = cag.parse_mki(txt("cag_glance_mh.txt"), 2026)
    exp = ind.expenditure
    assert exp.value == round(573896.76 + 87733.33 + 11238.32, 2)          # standard definition
    assert exp.be == round(708910.83 + 114204.44 + 21696.36, 2)            # same formula, BE column
    assert exp.inputs["printed_total"] == {"row": "(7) Total Expenditure (4)+(5)", "be": 823115.27, "actual": 661630.09}
    assert exp.checks[0] == "printed Total Expenditure = 4+5"
    tax = ind.own_tax
    assert tax.value == 381196.91 and tax.be == round(477400.02 - 89855.80, 2)
    assert tax.inputs["tax_revenue"]["actual"] == 469190.88 and tax.inputs["union_share"]["actual"] == 87993.97
    assert "−" in tax.formula and ind.problems == []


def test_parse_karnataka_wrapped_rows():
    # '7 Total Expenditure (4+ 5)' has its figures on the next line; loans row has its two
    # percentages on the next line (both checked by eye on page 2).
    ind = cag.parse_mki(txt("cag_glance_ka.txt"), 2026)
    assert ind.expenditure.inputs["printed_total"]["actual"] == 370144.74
    assert ind.expenditure.inputs["loans_and_advances"]["actual"] == 3775.86
    assert ind.expenditure.value == round(302335.88 + 67808.86 + 3775.86, 2)


def test_parse_tamil_nadu_table_runs_onto_page_2():
    ind = cag.parse_mki(txt("cag_glance_tn.txt"), 2026)
    assert ind.expenditure.inputs["printed_total"]["actual"] == 419848.41
    assert ind.own_tax.value == round(249312.36 - 56819.23, 2)


def test_parse_odisha_spaced_percent_and_singular_loan_label():
    ind = cag.parse_mki(txt("cag_glance_od.txt"), 2026)
    assert ind.expenditure.inputs["loans_and_advances"]["row"].startswith("8. Loan and Advances Disbursed")
    assert ind.own_tax.value == round(120097.07 - 63073.7, 2)


def test_parse_tripura_split_digits_stay_pending():
    # Revenue expenditure prints as '2 4 5 2 0 . 1 8' – must not be guessed.
    ind = cag.parse_mki(txt("cag_glance_tr.txt"), 2026)
    assert ind.expenditure is None
    assert any(p.startswith("expenditure") for p in ind.problems)


def test_parse_assam_failed_cross_check_stays_pending():
    ind = cag.parse_mki(txt("cag_glance_as.txt"), 2026)
    assert ind.expenditure is None and ind.own_tax is None


def test_parse_rejects_wrong_year():
    assert cag.parse_mki(txt("cag_glance_mh.txt"), 2027).expenditure is None


def test_rows_need_exactly_four_figures():
    rows = cag.table_rows("(7) Total Expenditure (4)+(5) 1 8 18475.91 75.35 92.20\n"
                          "(4) Revenue Expenditure … … … …\n"
                          "(5) Capital Expenditure 10.00 9.00 90.00 80.00")
    assert [r.label for r in rows] == ["(5) Capital Expenditure"]


def test_loans_row_checked_by_its_percent_column_when_not_in_total():
    text = txt("cag_glance_mh.txt").replace("21696.36 11238.32 51.80", "21696.36 11238.32 75.00")
    ind = cag.parse_mki(text, 2026)
    assert ind.expenditure is None and any("% column" in p for p in ind.problems)


def test_glance_text_from_pdf_stops_at_monthly_trend():
    text = cag.glance_text((S / "cag_mki_mh_march2026_p1-2.pdf").read_bytes())
    assert "Total Expenditure (4)+(5) 823115.27 661630.09" in text
    assert "Monthly Trend" not in text and "April 32127.37" not in text


class FakeFetcher:
    """Serves the saved samples instead of the network."""
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, max_age_h=0):
        body = self.pages.get(url)
        return (body, "application/pdf" if body and body.startswith(b"%PDF") else "text/html") if body else None


@pytest.fixture
def cfg():
    return {"cag": {"index_pages": ["https://cag.gov.in/en/state-accounts-report"], "pdf_hosts": HOSTS,
                    "state_names": {"MH": "Maharashtra", "AP": "Andhra Pradesh", "GA": "Goa"}}}


def test_update_actuals_fills_seed_and_keeps_existing(cfg):
    mh_pdf_url = cag.march_report((S / "cag_state_mh.html").read_bytes(), 2026, HOSTS)[1]
    f = FakeFetcher({
        "https://cag.gov.in/en/state-accounts-report": (S / "cag_index.html").read_bytes(),
        "https://cag.gov.in/en/state-accounts-report?defuat_state_id=79": (S / "cag_state_mh.html").read_bytes(),
        mh_pdf_url: (S / "cag_mki_mh_march2026_p1-2.pdf").read_bytes(),
    })
    old = {"value": 1.0, "basis": "provisional actuals", "status": "verified"}
    seed = {"states": [{"code": "MH"}, {"code": "AP", "budget_actual": copy.deepcopy(old)}, {"code": "GA"}]}
    tally = cag.update_actuals(f, cfg, seed, "2025-26", "2026-10-18")

    mh = seed["states"][0]
    spent = mh["budget_actual"]
    assert spent["value"] == 672868.41 and spent["definition"] == "exp_std"
    assert spent["basis"] == "provisional actuals" and spent["fy"] == "2025-26" and spent["status"] == "verified"
    assert spent["period"] == "April 2025 – March 2026"
    assert spent["source"]["url"] == mh_pdf_url == spent["derived"]["source_url"]
    assert set(spent["derived"]["inputs"]) == {"revenue_expenditure", "capital_outlay", "loans_and_advances", "printed_total"}
    assert mh["tax_actual"]["value"] == 381196.91 and mh["tax_actual"]["definition"] == "own_tax"
    assert mh["tax_actual"]["derived"]["inputs"]["union_share"]["actual"] == 87993.97
    assert "tax" not in mh and "budget" not in mh  # revised estimates untouched
    # AP: state page not served -> no report; the figure already stored is never cleared.
    assert seed["states"][1]["budget_actual"] == old
    assert tally["filled"] == ["MH"] and set(tally["no_report"]) == {"AP", "GA"}


def test_build_passes_actuals_to_site(tmp_path, monkeypatch):
    from scraper import build, store
    seed = json.loads(store.SEED.read_text(encoding="utf-8"))
    seed["states"][0]["budget_actual"] = {"value": 5.0, "basis": "provisional actuals", "status": "verified"}
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(seed), encoding="utf-8")
    monkeypatch.setattr(build, "SEED", p)
    site = build.build(tmp_path / "site.json", tmp_path / "validation.json")
    assert site["states"][0]["budget_actual"]["value"] == 5.0
    assert "tax_actual" in site["states"][0]
