"""Schemes from PRS state budget analyses: discovery, extraction, categories, saving and what the site lists."""
from scraper import build, prs, run, store
from scraper.extract import INFRASTRUCTURE, POLICY, WELFARE, category

FY = "2025-26"
SLUGS = {"HR": "haryana", "GA": "goa"}

LISTING = b"""<html><body>
<a href="/budgets/states/haryana-budget-analysis-2025-26">Haryana</a>
<a href="/budgets/states/goa-budget-analysis-2025-26">Goa</a>
<a href="/budgets/states/goa-budget-analysis-2024-25">Goa (last year)</a>
<a href="/budgets/states/ladakh-budget-analysis-2025-26">Ladakh</a>
<a href="/budgets/states/policy/state-of-state-finances-2025">Report</a>
</body></html>"""

# as printed on prsindia.org (2025-26 analyses), trimmed
HR_PAGE = b"""<html><body><h1>Haryana Budget Analysis 2025-26</h1><div class="field-item even">
<p><strong>Policy Highlights</strong></p>
<ul><li><p><strong>Cash transfer to women:&nbsp; </strong>The Lado Laxmi Yojana will be launched to provide financial
assistance of Rs 2,100 per month to women.&nbsp; Rs 5,000 crore has been allocated to the scheme in 2025-26.</p></li>
<li><p><strong>Skill development:</strong> Mukhyamantri Yuva Kaushal Samman Yojana will be launched to provide 2,000
final year students with internship opportunities.</p></li>
<li><p><strong>Haryana AI Mission:</strong> The Haryana AI Mission will be launched.  One AI hub will be built in
Gurugram and Panchkula.</p></li>
<li><p><strong>Paddy cultivation:</strong> Under the Mera Pani, Meri Viraasat Yojana, subsidy for farmers giving up
paddy cultivation will be increased to Rs 8,000 per acre.</p></li>
<li><p><strong>Upgradation of district hospitals:</strong> District hospitals will be upgraded.</p></li></ul>
<table><tr><td>Haryana's Economy</td></tr></table>
<p><a href="https://prsindia.org/files/budget/budget_state/haryana/2025/Haryana_Budget_Analysis_2025-26.pdf">PDF</a></p>
</div></body></html>"""

GA_PAGE = b"""<html><body><h1>Goa Budget Analysis 2025-26</h1>
<p><strong>Policy Highlights</strong></p>
<ul><li><p><strong>Mukhyamantri Kaushalya Sahay Yojana:</strong> Under this scheme, students who complete certified
courses in areas such as apparel, automobiles, and beauty and wellness will be provided accessory kit grants of
Rs 20,000.</p></li>
<li><p><strong>Pension:</strong> The Unified Pension Scheme will be implemented from 2025-26 onwards.  Assured pension
will be provided to government employees.</p></li>
<li><p><strong>Tax holiday for new hotels:</strong> New hotels will get a tax holiday.</p></li></ul>
</body></html>"""

# the PDF's 'Budget Provisions' column, as provisions() returns it
HR_PROVISIONS = [("Social Welfare and Nutrition", "Rs 5,000 crore has been allocated towards Lado Lakshmi Yojana."),
                 ("Education, Sports, Arts, and Culture",
                  "Rs 8,258 crore has been allocated towards Government primary schools.")]


def hr_items():
    return prs.schemes_on_page("HR", FY, "https://prsindia.org/budgets/states/haryana-budget-analysis-2025-26",
                               "PRS – Haryana Budget Analysis 2025-26", prs.highlights(HR_PAGE), HR_PROVISIONS,
                               "https://prsindia.org/files/x.pdf")


def ga_items():
    return prs.schemes_on_page("GA", FY, "https://prsindia.org/budgets/states/goa-budget-analysis-2025-26",
                               "PRS – Goa Budget Analysis 2025-26", prs.highlights(GA_PAGE), [], None)


def test_listing_finds_this_years_state_analyses_only():
    found, unknown = prs.listing(LISTING, "https://prsindia.org/budgets/states?budget_year=2025-26", SLUGS, FY)
    assert found == {"HR": "https://prsindia.org/budgets/states/haryana-budget-analysis-2025-26",
                     "GA": "https://prsindia.org/budgets/states/goa-budget-analysis-2025-26"}
    assert unknown == ["ladakh"]


def test_highlights_and_pdf_link():
    hl = prs.highlights(HR_PAGE)
    assert hl[0][0] == "Cash transfer to women" and hl[0][1].startswith("The Lado Laxmi Yojana will be launched")
    assert len(hl) == 5
    assert prs.pdf_link(HR_PAGE, "https://prsindia.org/").endswith("Haryana_Budget_Analysis_2025-26.pdf")


def test_haryana_lado_laxmi_with_allocation_one_record_for_both_spellings():
    items = {i["name"]: i for i in hr_items()}
    lado = items["Lado Laxmi Yojana"]
    assert lado["allocation"] == 5000 and lado["category"] == WELFARE and lado["kind"] == "scheme"
    assert "Lado Lakshmi Yojana" not in items                      # PDF spelling merged into the same scheme
    assert items["Mukhyamantri Yuva Kaushal Samman Yojana"]["allocation"] is None   # kept without an amount
    assert items["Haryana AI Mission"]["category"] == POLICY
    assert "Mera Pani, Meri Viraasat Yojana" in items              # a name with a comma
    assert "Upgradation of district hospitals" not in items        # infrastructure, unnamed: not a scheme


def test_goa_kaushalya_sahay_without_allocation():
    items = {i["name"]: i for i in ga_items()}
    ks = items["Mukhyamantri Kaushalya Sahay Yojana"]
    assert ks["allocation"] is None and ks["category"] == WELFARE
    assert "accessory kit grants of Rs 20,000" in ks["benefit"]
    assert ks["source"]["url"].endswith("goa-budget-analysis-2025-26")
    assert items["Unified Pension Scheme"]["category"] == POLICY  # government employees' pension


def test_an_amount_belongs_only_to_the_scheme_its_sentence_names():
    text = "Rs 945 crore has been allocated towards Old Age Pension Yojana and the Widow Pension Yojana."
    assert prs._allocation("Widow Pension Yojana", text, False, ["Old Age Pension Yojana", "Widow Pension Yojana"]) is None
    assert prs._allocation("X Yojana", "The X Yojana costs Rs 50 crore, including Y Yojana.", True) is None


def test_categories():
    assert category("Ladli Behna Yojana. Rs 18,669 crore has been allocated towards it.") == WELFARE
    assert category("Mukhya Mantri Gram Sadak Yojana. Roads will be built.") == INFRASTRUCTURE
    assert category("Tax holiday for new hotels.") == POLICY


def test_spelling_variants_are_one_scheme():
    assert store.name_score("Lado Laxmi Yojana", "Lado Lakshmi Yojana") == 100


def test_saved_without_allocation_and_listed_on_the_site_only_if_welfare():
    schemes, catalog = [], []
    out = run.save_prs_schemes(hr_items() + ga_items(), schemes, catalog, {})
    assert out["new"] == len(schemes)
    by = {s["name"]: s for s in schemes}
    assert by["Lado Laxmi Yojana"]["announced"] == 5000 and by["Lado Laxmi Yojana"]["status"] == "secondary"
    ks = by["Mukhyamantri Kaushalya Sahay Yojana"]
    assert ks["announced"] is None and ks["status"] == "secondary" and ks["sources"][0]["kind"] == "prs"
    # a second run updates, never duplicates, and never clears an amount
    again = run.save_prs_schemes([dict(i, allocation=None, allocation_source=None) for i in hr_items()],
                                 schemes, catalog, {})
    assert again["new"] == 0 and by["Lado Laxmi Yojana"]["announced"] == 5000
    listed = [s for s in schemes if s.get("category") == WELFARE and s.get("kind") == "scheme"]
    assert {"Lado Laxmi Yojana", "Mukhyamantri Kaushalya Sahay Yojana"} <= {s["name"] for s in listed}
    assert "Haryana AI Mission" not in {s["name"] for s in listed}
    assert build.public_scheme(by["Lado Laxmi Yojana"])["announced"] == 5000
