# Yojana Tracker

A lightweight static site showing central and state beneficiary schemes (yojanas), budgets and tax collections for **FY 2025-26**, with every figure traced to its source.

- **Home:** central schemes total and gross tax collected, two charts (where the centre spends, top 5 central schemes), and a states table (top 5 by beneficiary amount, "show all", search).
- **State page:** state budget, beneficiary amount announced, and the state's top 5 yojanas with a status badge each.

Pilot coverage: the centre + Maharashtra, Uttar Pradesh, Tamil Nadu, Karnataka, Rajasthan. The other 26 states/UTs appear as "Coming soon".

## How data flows

```
PIB + national news (RSS)  →  extract (state, scheme, ₹ amount)
                                   ↓
              government documents (.gov.in / .nic.in) for that state
                                   ↓
   verified ── news figure matches the govt document
   mismatch ── govt document shows another figure → govt figure is published
   review   ── fuzzy name match or several numbers nearby → pull request for you
   not found ─ published as "Unverified · not in totals"
                                   ↓
                data/*.json  →  site/data/site.json  →  GitHub Pages
```

Status badges on the site:

| Badge | Meaning |
|---|---|
| Verified | Matched in a government document (or a PIB release) |
| Budget docs (via PRS) | Seed figure from PRS Legislative Research's analysis of the official budget; upgraded to Verified once matched in the govt document itself |
| Unverified · not in totals | Seen in the news only; shown but never added to any total |

## Repository layout

```
site/                 static site (index.html, styles.css, app.js, data/site.json)
scraper/              Python pipeline
  config.yaml         news feeds, govt source pages, thresholds, pilot states
  news.py             RSS discovery
  extract.py          finds state / scheme / amount in text
  fetch.py            polite fetching (robots.txt, delays, cache), PDF + HTML text
  verify.py           matches a candidate against govt documents
  store.py, build.py  data files → site/data/site.json
  run.py              the daily job
data/
  seed.json           headline figures (budgets, tax, sector totals) with sources
  schemes.json        every scheme found, with status and sources
  review_queue.json   items waiting for you
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
cd site && python -m http.server    # open http://localhost:8000
```

## Known gaps before the pilot is complete

- **Government source pages** in `scraper/config.yaml` are each state finance department's home page. Check once that they lead to the 2025-26 budget documents; if a state hosts them elsewhere, add the exact page or PDF under `direct_docs`.
- **Scanned PDFs**: text is read from the PDF text layer. Scanned budget documents yield little text, so those schemes stay "Unverified" until an OCR step (e.g. `ocrmypdf`) is added.
- **Beneficiary despatched** is "Pending" everywhere. The next step is a reader for DBT Bharat (dbtbharat.gov.in) state-wise transfers and CAG monthly accounts.
- **Beneficiary announced** for states currently uses each state's *Social Welfare & Nutrition* sector spend (RE 2025-26) as an interim total, until scheme-level totals are verified.
- News outlets' RSS feeds only carry recent articles. Because FY 2025-26 budgets were presented in early 2025, the hand-collected seed in `data/schemes.json` covers the pilot states; the daily run mainly keeps verifying those and picks up new announcements.
