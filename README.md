# Yojana Tracker

A lightweight static site showing central and state beneficiary schemes (yojanas), budgets and tax collections for **FY 2025-26**, with every figure traced to its source.

- **Home:** central scheme allocations (revised estimates) and gross tax collected, two charts (where the centre spends, top 5 central schemes), and a states table (top 5 by schemes announced, "show all", search; "—" = not yet available). Methodology sits in a "How we calculate" panel in the footer.
- **State page:** state budget and own tax revenue (each with the CAG provisional actual and % of the revised estimate underneath), schemes announced, and the state's top 5 yojanas by allocation – each with a status badge, its funding type (state / centrally sponsored / central) and department, and a link to its official page. An allocation is shown only once confirmed in an official source.

Pilot coverage (schemes, news verification): the centre + Maharashtra, Uttar Pradesh, Tamil Nadu, Karnataka, Rajasthan. Budget, own tax and Schemes announced come from PRS for every state/UT PRS analyses (all but Puducherry); CAG actuals for every state that publishes a March report.

## How data flows

```
myScheme (per State/UT) ─┐
National Portal of India ┼→ data/catalog.json: one entry per scheme (all its names), department,
known aliases (config)  ─┘   description, official link, funding type – no amounts
                                   ↓
PIB, national + regional-language news, state press releases
   → extract (state, scheme, type, ₹ amount, benefit, beneficiaries, department)
   → one report per article on the scheme's record (deduplicated by URL and by scheme name)
                                   ↓
              government documents (.gov.in / .nic.in) for that state
                                   ↓
   verified ── news allocation matches the govt document
   mismatch ── govt document shows another figure → govt figure is published
   review   ── fuzzy name match, several numbers nearby, or a catalog scheme found next to a single
               amount in a govt document → pull request for you
   not found ─ kept as "Reported — awaiting official verification", no amount shown
                                   ↓
                data/*.json  →  site/data/site.json  →  GitHub Pages
```

Status badges on the site:

| Badge | Meaning |
|---|---|
| Verified | Matched in a government document (or a PIB release) |
| Budget docs (via PRS) | Seed figure from PRS Legislative Research's analysis of the official budget; upgraded to Verified once matched in the govt document itself |
| Reported — awaiting official verification | Seen in the news (or discovered) only. Listed without an amount; the reported figure stays in `data/schemes.json → reported_amount` and is never shown or added to any total |

## Data sources

| Figure | Source | Notes |
|---|---|---|
| Central schemes, gross tax, charts | PRS analysis of the Union Budget; confirmed against indiabudget.gov.in | `data/seed.json → central` |
| State budget, own tax revenue, Social Welfare & Nutrition (RE 2025-26) | PRS state budget analyses of 2026-27 – every state/UT PRS covers (30 of 31; not Puducherry) | Badge "Budget docs (via PRS)". `scraper/prs.py` reads PRS's key-figure, receipts and sector tables each run and stores the BE/RE rows as printed under `prs_table`. The five pilot states' hand-entered figures are kept and checked against these rows; for the other states the figures are created from them. A row PRS doesn't print stays empty (Arunachal Pradesh and J&K have no Social Welfare & Nutrition row) |
| **Actual spending and own tax collected (2025-26)** | **CAG, state accounts → Monthly Key Indicators**, March 2026 report ([cag.gov.in](https://cag.gov.in/en/state-accounts-report)) | `scraper/cag.py`; stored as `budget_actual` / `tax_actual`, basis "provisional actuals" |
| Scheme discovery (names, departments, descriptions, funding type) | myScheme (primary, per State/UT), National Portal of India (secondary) | `scraper/myscheme.py`, `scraper/portal.py`, `scraper/catalog.py` → `data/catalog.json`. No amounts |
| Scheme announcements | PIB, national and regional-language news RSS, state press-release pages; allocations verified against state budget documents, departmental reports and notifications | `scraper/news.py`, `scraper/verify.py` |

## Scheme discovery

**myScheme** ([myscheme.gov.in/search](https://www.myscheme.gov.in/search)) is searched once per State/UT (`beneficiaryState` filter) for every scheme it lists, with the nodal department, a short description and whether it is a State or a Central scheme. The search page gets its data from myScheme's search API, which needs an API key. The run uses the API only when `MYSCHEME_API_KEY` holds a key issued to this project (ask the myScheme team / API Setu; add it as a repository secret). The key bundled in the portal's own JavaScript is not used. Without a key the step is skipped and the run summary says so. Central schemes listed for a state are catalogued once, under the Centre.

**National Portal of India** pages in `config.yaml → national_portal.pages` are read for scheme links. A scheme found only there is still catalogued; where both list it, myScheme's details win. india.gov.in answered *Access Denied* to the crawler in October 2026, so this source contributes nothing until access works.

**One entry per scheme.** Names are compared after dropping generic words (scheme, yojana, abhiyan…) and brackets. A Prime Minister's scheme and a Chief Minister's scheme with the same name are never merged, because they are usually the centrally sponsored scheme and the state's own scheme. A one-word name only matches the same word. Known alternative names, spellings and regional-language names go in `config.yaml → scheme_aliases`. Every name a scheme was seen under is kept in `aliases`.

**Funding type** (`funding`, with the reason in `funding_evidence`):

| Value | When |
|---|---|
| `centrally_sponsored` | The name matches the Union Budget's list of Centrally Sponsored Schemes (`config.yaml → funding.centrally_sponsored`). This is checked first, so a state's implementation of PMAY or ICDS is not called a state scheme |
| `state` | myScheme lists it as a State scheme, or it is named as a Chief Minister's scheme |
| `central` | myScheme lists it as a Central scheme and it is not on the CSS list |
| `unknown` | No evidence. The type is never guessed, and the site shows nothing |

Hand-set values go in `funding.overrides` (currently Karnataka's guarantee schemes and three Tamil Nadu schemes).

**Amounts.** myScheme and the portal carry no budget figures. A catalog scheme reaches the site's list only through a news report or a confirmed allocation. For each jurisdiction with government documents configured, every catalog scheme without a confirmed allocation is searched for **verbatim** (any of its names) in those documents: budget documents plus any `notification_pages` (government orders, gazettes, departmental reports). When exactly one amount stands near the name, it goes to the review queue (`"kind": "allocation"`) and is never published directly. You approve it as that scheme's allocation for the year, or reject it.

## News discovery

Feeds are listed in `config.yaml → news_feeds`:

- PIB
- National papers, plus state-desk feeds with a `state` hint
- Regional-language papers: Hindi, Marathi, Tamil and Kannada

State press-release pages are listed under `announcement_pages`. Extraction works in English, Devanagari (Hindi/Marathi), Tamil and Kannada: rupee amounts in crore (`₹700 करोड़`, `36 हजार करोड़ रुपये`, `ரூ.13,807 கோடி`), per-beneficiary benefits (`₹1,500 per month`, `₹2,100 प्रति माह`), beneficiary counts (`2.5 crore women`), departments, and state names. A regional amount needs a rupee sign or word, so `2 करोड़ महिलाएं` (2 crore women) is not read as money.

**Article type** (`reports[].kind`):

| Type | Meaning |
|---|---|
| `disbursement_update` | Released, credited, instalment, किस्त… |
| `revised_allocation` | Hiked, revised, supplementary… |
| `new_scheme` | Launched, new scheme, शुरू… |
| `existing_scheme` | None of the above |

A disbursement amount is never treated as an allocation. It also doesn't count as a disbursement, which needs a government source with period and date (see below).

**FY.** A story belongs to the FY it names. Otherwise it belongs to the FY of its publication date, and a January–March budget story belongs to the coming FY. Only stories about the site's FY are verified for the site; the rest are kept as reports under their own FY, ready for the next one.

**Deduplication.** Article URLs are canonicalised (AMP, tracking parameters and fragments stripped), so one article is one report. Reports of the same scheme under different names land on one record via the catalog.

**What is stored** per article is the link, headline, outlet, date, language and the extracted facts, never the article text. A feed with `fetch_article: false` is read from its RSS headline and summary only, for outlets whose terms don't allow fetching pages.

**Politeness.**
- Every request checks robots.txt and honours its `Crawl-delay`, with at least 2 s between requests to a host.
- Each run caps requests per host (`http.max_requests_per_host`) and articles per feed (`max_articles_per_source`).
- A host answering 429 or 503 is left alone for the rest of the run.
- The job runs daily (GitHub Actions, 06:47 IST).
- **Google News RSS is not used:** news.google.com/robots.txt disallows `/rss` for all crawlers.
- pib.gov.in and india.gov.in answered *Access Denied* to the crawler's user agent in October 2026. Their steps log the failure and the run carries on.

**CAG Monthly Key Indicators.** The March report of a year is cumulative for the whole year (unaudited, provisional). Where a state publishes several March versions, the run uses *March(Sup)* (after closing entries), then *March*, then *March(Pre)*; the version used is stored in `report`. Figures come from the "Accounts at a Glance" table (pages 1–2 of the PDF):

- *Total expenditure* = revenue expenditure (4) + capital expenditure/outlay (5) + loans & advances disbursed, computed from the rows (states print their own "Total Expenditure" with or without loans; it is kept only as a check that the rows were read correctly). The loans row is also checked against its own % column.
- *Own tax revenue* = "(a) Tax Revenue" − "(vi) State's share of Union taxes" (CAG's tax row includes the central share). Accepted only if the tax items add up to the tax row.
- Both are **derived** figures: stored with `derived.formula`, every input row (label, BE, actual), the same formula applied to the BE column, and the PDF URL.
- Anything that fails a check, is scanned (no text layer), or has no March report stays `null` → "Pending". Each run logs the reason per state.
- PDFs are on CAG's CDN (`d7i5wg8xwe4hf.cloudfront.net`); only links found on the cag.gov.in state pages are used.

## Definitions and validation

**One definition of total expenditure everywhere:** revenue expenditure + capital outlay + loans & advances, excluding debt repayment. For PRS this is the published "Net Expenditure (E)" row (= Total Expenditure − Repayment of debt), stored as `expenditure`; the site's "budget" shows it. The figure originally entered from PRS ("Total Expenditure", which *includes* debt repayment) stays in `seed.json → budget`, unchanged, labelled `definition: exp_incl_debt`. Source figures are never overwritten with corrected values.

Every figure carries `basis` (BE / RE / provisional actuals), `fy` and `definition`. `scraper/validate.py` runs on every build and writes `data/validation.json`. A comparison (actual as % of revised estimate) is shown only if:

| Rule | Comparison shown as N/A when… |
|---|---|
| basis | it isn't provisional actuals vs RE |
| fy | either figure is for another FY than the site's |
| definition | the two figures use different definitions |
| heads | CAG's BE for the derived figure (same formula, same table) differs from PRS's BE for that definition by more than 0.1 % – the two sources then group accounting heads differently |
| ratio | actual / RE is outside 60–120 % (needs a human look) |

It also reports errors – a figure without basis / FY / definition, a derived figure without formula, inputs or source URL, a seed figure that no longer equals the PRS row it came from – and missing figures for pilot states. Errors make the daily job open a review pull request instead of publishing.

**Validation of the pilot states (Oct 2026).** PRS columns are "2025-26 Budgeted" / "2025-26 Revised" in the analysis of the 2026-27 budget; CAG figures are the March 2026 report (April 2025 – March 2026). All five PRS "Total Expenditure" figures included debt repayment (e.g. Rajasthan ₹5,58,596 Cr incl. ₹1,99,124 Cr repayment; Uttar Pradesh ₹7,16,231 Cr incl. ₹41,476 Cr). On the heads check, own tax revenue matches for Maharashtra, Tamil Nadu and Rajasthan (CAG BE = PRS BE); it does not for Uttar Pradesh (CAG BE ₹3,69,287 Cr vs PRS ₹2,95,000 Cr) or Karnataka – their CAG tables put part of the central tax share under other items. Expenditure BEs match for Karnataka only (CAG ₹3,83,074.77 Cr vs PRS ₹3,83,075 Cr); they differ for Rajasthan (+4.0 %), Uttar Pradesh (+3.2 %), Tamil Nadu (+11.0 %) and Maharashtra (+20.7 %), so those comparisons show N/A. Details per state are in `data/validation.json`.

**Checked but not usable yet (October 2026):**

- *RBI, State Finances: A Study of Budgets* – the latest edition (Jan 2026) covers 2023-24 Accounts, 2024-25 RE and 2025-26 **BE**; RE 2025-26 will first appear in the next edition. rbidocs.rbi.org.in also answers automated downloads with a CAPTCHA page, so its XLSX/PDF files can't be fetched by the daily job.
- *DBT Bharat* – state dashboards show only a cumulative total and the current year (2026-27) and ignore year parameters. The home page has year-wise data back to 2019-20 (2025-26: ₹7,51,902 Cr), but only nationally and by central scheme group, not by state. The state cumulative totals are clearly incomplete (UP and TN ₹0, Karnataka ₹2.7 Cr).

**Benefits disbursed – sources checked (18 Oct 2026), none usable for 2025-26:**

| Source | Result |
|---|---|
| MahaDBT (mahadbtmahait.gov.in) | TLS certificate not valid for the hostname – not fetched |
| Ladki Bahin portal (ladakibahin.maharashtra.gov.in) | 403 Forbidden |
| DBT UP (dbtup.upsdc.gov.in) | JavaScript-only shell, no data |
| Kanya Sumangala (mksy.up.gov.in) | "KPI" / "Quick Stats" pages return the home page; scheme rules only |
| UP pensions (sspy-up.gov.in) | TLS certificate expired – not fetched |
| DBT Tamil Nadu (dbt.tn.gov.in) | An internal payment-processing console (approval actions, beneficiary names and mobile numbers), not a public disclosure – deliberately not used |
| KMUT (kmut.tn.gov.in) | TLS certificate expired – not fetched |
| DBT Karnataka (dbtkarnataka.gov.in) | No response |
| Jan Aadhaar, Rajasthan | DBT dashboard's app script 404s; e-Transaction app disallowed by robots.txt; latest administrative report is Jan 2024 |
| Web search for official scheme reports | Only budget allocations, news and aggregator figures that contradict each other |

TLS errors are never bypassed: a figure fetched that way can't be shown to come from the government site. State budget documents report actuals two years later, so 2025-26 scheme actuals first appear with the 2027-28 budgets.

**How a disbursement figure gets on the site.** A scheme's `despatched` amount counts only with `despatched_source` = {`url`, `title`, `period`, `as_of`}; a state-level `beneficiary_despatched` needs `basis: "actuals"`, `source.url`, `period` and `as_of`. A state total covering only some of its counted schemes is labelled **Partial**; with no figure the cell shows "—". Budget allocations are never used. Figures missing provenance are reported in `data/validation.json` and not shown.

## Repository layout

```
site/                 static site (index.html, styles.css, app.js, data/site.json)
scraper/              Python pipeline
  config.yaml         news feeds, govt source pages, thresholds, pilot states
  myscheme.py         myScheme discovery per State/UT (needs MYSCHEME_API_KEY)
  portal.py           National Portal of India discovery (secondary)
  catalog.py          one entry per scheme across names; funding type
  news.py             news + press-release discovery (RSS, regional languages)
  extract.py          finds state / scheme / amount / type / benefit / beneficiaries / department in text
  fetch.py            polite fetching (robots.txt, delays, cache), PDF + HTML text
  verify.py           matches a candidate against govt documents
  cag.py              CAG Monthly Key Indicators → provisional actuals per state
  prs.py              PRS key-figure tables → reference BE/RE rows, standard expenditure figure
  validate.py         data-integrity rules → which comparisons are valid; data/validation.json
  store.py, build.py  data files → site/data/site.json
  run.py              the daily job
data/
  seed.json           headline figures (budgets, tax, sector totals) with sources
  catalog.json        every scheme discovered (myScheme, National Portal, news) – names, department, funding
  schemes.json        schemes per FY with status, confirmed allocation, sources and news reports
  review_queue.json   items waiting for you
  validation.json     data-integrity findings from the last build (blocked comparisons, errors)
  rejected.json       items you rejected (never re-queued)
tests/                unit tests (run in CI before every update)
.github/workflows/    daily update + GitHub Pages deploy
```

## Go live on GitHub Pages (free, ~10 minutes)

1. Create a **public** GitHub repository, e.g. `yojana-tracker`, and push this folder to `main`:
   ```bash
   git init && git add . && git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<you>/yojana-tracker.git
   git push -u origin main
   ```
2. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. Repo **Settings → Actions → General → Workflow permissions**: choose *Read and write* and tick *Allow GitHub Actions to create and approve pull requests*.
4. **Actions → Deploy site → Run workflow.** Your site appears at `https://<you>.github.io/yojana-tracker/`.
5. **Actions → Daily data update → Run workflow** once to test the scraper. After that it runs daily at 06:47 IST.
6. Put your repo URL in `scraper/config.yaml → http.user_agent` so site owners can see who is crawling.

## Reviewing uncertain figures

When the daily run can't decide, it opens a pull request titled *"Data update: figures waiting for your review"* instead of publishing. In that PR, edit `data/review_queue.json`, set `"decision": "approve"` or `"reject"` per item (optionally `"use_amount"`), commit and merge. The next run applies your decisions.

## Run locally

```bash
pip install -r scraper/requirements.txt
python -m pytest -q                 # tests
python -m scraper.run               # full run (needs internet)
python -m scraper.run --build-only  # rebuild site JSON from data/
python -m scraper.run --no-discovery  # skip myScheme / National Portal (news + verification only)
cd site && python -m http.server    # open http://localhost:8000
```

## Known gaps before the pilot is complete

- **Government source pages** in `scraper/config.yaml` are each state finance department's home page. Check once that they lead to the 2025-26 budget documents; if a state hosts them elsewhere, add the exact page or PDF under `direct_docs`.
- **Scanned PDFs**: text is read from the PDF text layer. Scanned budget documents yield little text, so those schemes stay "Reported" until an OCR step (e.g. `ocrmypdf`) is added.
- **Benefits disbursed** (`beneficiary_despatched` in the data) is empty everywhere: no checked source publishes verified 2025-26 transfers (see *Data sources*). Options: RTI requests to the scheme departments, re-checking the portals once their certificates are fixed, or the 2027-28 budget documents (2025-26 actuals).
- **CAG actuals** are Pending for Assam, Kerala, Meghalaya, Sikkim, Tripura (expenditure) and Uttarakhand because their reports fail the arithmetic check or are scanned; Goa, Mizoram, Delhi and Puducherry have no March 2026 report on cag.gov.in.
- **Schemes announced** (`beneficiary_announced` in the data) for states currently uses each state's *Social Welfare & Nutrition* sector spend (RE 2025-26) as an interim total, until scheme-level totals are verified. States classify welfare differently, so this isn't fully comparable across states: Andhra Pradesh, for example, books ₹41,705 Cr under "Welfare of SC, ST, OBC and Minorities" and only ₹7,290 Cr under Social Welfare & Nutrition.
- News outlets' RSS feeds only carry recent articles. Because FY 2025-26 budgets were presented in early 2025, the hand-collected seed in `data/schemes.json` covers the pilot states; the daily run mainly keeps verifying those and picks up new announcements (stored under their own FY).
- **No scheme amount is shown yet.** All 17 seeded schemes are news-reported (`reported_amount`) and none has been matched in a government document, so state pages list them as "Reported — awaiting official verification" with "—" for the allocation.
- **myScheme** discovery needs an authorised API key (`MYSCHEME_API_KEY`); until then the catalog holds only the aliases in `config.yaml` and news discoveries. The parser follows the search API's published response shape (`data.hits.items[].fields`) and is tested against a sample, not yet against a live response.
- **National Portal, PIB and state press-release pages** (`announcement_pages`) are unconfirmed: india.gov.in and pib.gov.in refused the crawler in October 2026, and the press-release URLs are each department's home page.
- **Regional-language names** are matched to English ones only through `scheme_aliases`; there is no transliteration. Regional scheme-name extraction takes the words before योजना / திட்டம் / ಯೋಜನೆ up to a function word, so it can include a stray word – such reports stay "Reported" and are visible in `data/schemes.json`.
- **Funding type** of non-pilot or news-only schemes is often `unknown` until myScheme access works.
