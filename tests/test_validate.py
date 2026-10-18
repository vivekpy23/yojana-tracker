"""PRS reference tables and the data-integrity rules. Figures are the real RJ / UP 2025-26 ones."""
import copy
from pathlib import Path

import pytest

from scraper import prs, validate

S = Path(__file__).parent / "samples"
FY = "2025-26"


# ---------------------------------------------------------------- PRS tables
def test_prs_rows_rajasthan():
    rows, problems = prs.key_rows((S / "prs_rj_tables.html").read_bytes(), FY)
    assert problems == []
    assert rows["Total Expenditure"] == {"BE": 537069, "RE": 558596}   # includes debt repayment
    assert rows["(-) Repayment of debt"] == {"BE": 157452, "RE": 199124}
    assert rows["Net Expenditure (E)"] == {"BE": 379617, "RE": 359472}
    assert rows["State's Own Tax"] == {"BE": 142743, "RE": 138539}


def test_prs_rows_need_the_right_fy_columns():
    rows, problems = prs.key_rows((S / "prs_up_tables.html").read_bytes(), "2024-25")
    assert rows == {} and len(problems) == len(prs.ROWS)


class FakeFetcher:
    def __init__(self, body):
        self.body = body

    def get(self, url, max_age_h=0):
        return self.body, "text/html"


def test_update_reference_keeps_originals_and_adds_standard_figure():
    st = {"code": "UP", "pilot": True,
          "budget": {"value": 716231, "basis": "RE", "status": "secondary"},
          "tax": {"value": 226181, "basis": "RE", "status": "secondary"},
          "source": {"title": "PRS", "url": "https://prsindia.org/budgets/states/uttar-pradesh-budget-analysis-2026-27"}}
    seed = {"states": [st]}
    assert prs.update_reference(FakeFetcher((S / "prs_up_tables.html").read_bytes()), seed, FY, "2026-10-18") == {}
    assert st["budget"]["value"] == 716231 and st["budget"]["definition"] == prs.EXP_INCL_DEBT  # untouched, labelled
    assert st["tax"]["definition"] == prs.OWN_TAX and st["tax"]["fy"] == FY
    assert st["expenditure"]["value"] == 674755 and st["expenditure"]["definition"] == prs.EXP_STD
    assert st["expenditure"]["source_row"] == "Net Expenditure (E)"
    assert st["prs_table"]["rows"]["Net Expenditure (E)"]["BE"] == 757333


# ---------------------------------------------------------------- comparisons
def rj():
    """Rajasthan as stored after a run: PRS reference + CAG March(Sup) 2026 derived actuals."""
    actual = {"basis": "provisional actuals", "fy": FY, "status": "verified",
              "source": {"url": "https://d7i5wg8xwe4hf.cloudfront.net/x.pdf"}}
    return {
        "code": "RJ", "pilot": True,
        "budget": {"value": 558596, "basis": "RE", "fy": FY, "definition": prs.EXP_INCL_DEBT},
        "expenditure": {"value": 359472, "basis": "RE", "fy": FY, "definition": prs.EXP_STD},
        "tax": {"value": 138539, "basis": "RE", "fy": FY, "definition": prs.OWN_TAX},
        "prs_table": {"rows": {"Total Expenditure": {"BE": 537069, "RE": 558596},
                               "Net Expenditure (E)": {"BE": 379617, "RE": 359472},
                               "State's Own Tax": {"BE": 142743, "RE": 138539}}},
        "budget_actual": {**actual, "value": 306614.26, "definition": prs.EXP_STD,
                          "derived": {"formula": "4 + 5 + loans", "inputs": {"x": {}}, "be": 394797.86,
                                      "source_url": "https://d7i5wg8xwe4hf.cloudfront.net/x.pdf"}},
        "tax_actual": {**actual, "value": 113539.89, "definition": prs.OWN_TAX,
                       "derived": {"formula": "tax − union share", "inputs": {"x": {}}, "be": 142743.39,
                                   "source_url": "https://d7i5wg8xwe4hf.cloudfront.net/x.pdf"}},
    }


def test_valid_comparison_when_definition_fy_basis_and_heads_match():
    issues = []
    assert validate.compare(rj(), "tax_actual", "tax", FY, issues) == 82.0
    assert issues == []


def test_heads_mismatch_blocks_rajasthan_expenditure():
    # CAG BE (rev + capital + loans) 3,94,798 vs PRS Net Expenditure BE 3,79,617: +4.0 %.
    issues = []
    assert validate.compare(rj(), "budget_actual", "expenditure", FY, issues) is None
    assert issues[0]["level"] == "blocked" and issues[0]["detail"].startswith("heads:") and "+4.0%" in issues[0]["detail"]


def test_definition_mismatch_blocks_total_incl_debt():
    issues = []
    assert validate.compare(rj(), "budget_actual", "budget", FY, issues) is None
    assert issues[0]["detail"].startswith("definition:")


@pytest.mark.parametrize("change, rule", [
    (lambda s: s["tax"].update(basis="BE"), "basis:"),
    (lambda s: s["tax_actual"].update(basis="RE"), "basis:"),
    (lambda s: s["tax_actual"].update(fy="2024-25"), "fy:"),
    (lambda s: s["tax"].pop("definition"), "definition:"),
    (lambda s: s["prs_table"]["rows"].pop("State's Own Tax"), "heads:"),
    (lambda s: s["tax_actual"].update(value=60000.0), "ratio:"),
])
def test_invalid_comparisons_show_na(change, rule):
    st, issues = rj(), []
    change(st)
    assert validate.compare(st, "tax_actual", "tax", FY, issues) is None
    assert issues[0]["detail"].startswith(rule)


def test_missing_figure_gives_none_without_blocking():
    st, issues = rj(), []
    del st["tax_actual"]
    assert validate.compare(st, "tax_actual", "tax", FY, issues) is None and issues == []


# ---------------------------------------------------------------- figure checks
def details(st):
    issues = []
    validate.check_figures(st, FY, issues)
    return [(i["field"], i["level"], i["detail"].split(":")[0]) for i in issues]


def test_clean_state_has_no_issues():
    assert details(rj()) == []


def test_figure_checks():
    st = rj()
    del st["tax_actual"]["derived"]["inputs"]
    st["expenditure"]["fy"] = "2026-27"
    del st["budget"]["basis"]
    st["tax"]["value"] = 140000                     # no longer equals the PRS row it came from
    got = details(st)
    assert ("tax_actual", "error", "derived") in got
    assert ("expenditure", "error", "fy") in got
    assert ("budget", "error", "basis") in got
    assert ("tax", "error", "source") in got


def test_missing_figures_for_pilot_states():
    st = rj()
    del st["budget_actual"]
    assert ("budget_actual", "missing", "no figure") in details(st)


def test_run_returns_comparisons_per_state():
    comparisons, issues = validate.run([rj(), {"code": "GA", "name": "Goa"}], FY)
    assert comparisons == {"RJ": {"budget": None, "tax": 82.0}, "GA": {"budget": None, "tax": None}}
    assert [i["field"] for i in issues] == ["budget_actual/expenditure"]
