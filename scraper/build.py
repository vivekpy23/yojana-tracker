"""Turn data/*.json into the single file the static site reads: site/data/site.json."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import validate
from .store import DATA, ROOT, SCHEMES, SEED, load, save

OUT = ROOT / "site" / "data" / "site.json"
VALIDATION = DATA / "validation.json"


def public(fig: dict | None) -> dict | None:
    """What the site needs from a figure; inputs, BE anchors and checks stay in data/."""
    if not isinstance(fig, dict):
        return fig
    out = {k: fig[k] for k in ("value", "basis", "status", "label", "source") if k in fig}
    if fig.get("derived"):
        out["derived"] = True
    return out


def disbursed(st: dict, counted: list[dict]) -> dict | None:
    """Benefits disbursed for a state: a sourced state-level figure, else the sum of schemes with a
    sourced disbursement ('partial' when only some counted schemes have one), else None ("—").
    Never derived from budget allocations."""
    fig = st.get("beneficiary_despatched")
    if validate.disbursement_ok(fig):
        return {**public(fig), "coverage": fig.get("coverage", "complete"),
                "period": fig["period"], "as_of": fig["as_of"]}
    have = [s for s in counted if s.get("despatched") is not None and validate.disbursement_ok(s.get("despatched_source"))]
    if not have:
        return None
    return {"value": sum(s["despatched"] for s in have), "basis": "actuals", "status": "verified",
            "coverage": "complete" if len(have) == len(counted) else "partial",
            "schemes_covered": len(have), "schemes_counted": len(counted),
            "label": f"Sum of {len(have)} of {len(counted)} schemes with a published disbursement figure"}


OFFICIAL = ("verified", "secondary")


def public_scheme(s: dict) -> dict:
    """A scheme as the site sees it. The amount appears only once an official source confirms it; a
    news-reported figure (`reported_amount`) and the article reports stay in data/."""
    out = {k: s.get(k) for k in ("name", "status", "sources", "note", "funding", "department", "official_url")}
    out["announced"] = s.get("announced") if s.get("status") in OFFICIAL else None
    desc = s.get("description") or ""
    out["description"] = desc if len(desc) <= 280 else desc[:277].rsplit(" ", 1)[0] + "…"
    # a disbursement without source, period and date is not shown
    out["despatched"] = s.get("despatched") if validate.disbursement_ok(s.get("despatched_source")) else None
    out["despatched_source"] = s.get("despatched_source")
    return out


def build(out: Path = OUT, validation_out: Path | None = VALIDATION) -> dict:
    seed = load(SEED, {})
    schemes = load(SCHEMES, [])
    fy = seed["fy"]
    fy_schemes = [s for s in schemes if s["fy"] == fy]
    comparisons, issues = validate.run(seed["states"], fy, schemes)

    states = []
    for st in seed["states"]:
        # Officially confirmed amounts first (largest first), then reported schemes, which show no amount.
        mine = sorted((s for s in fy_schemes if s["state"] == st["code"]),
                      key=lambda s: (s["status"] in OFFICIAL, (s.get("announced") or 0) if s["status"] in OFFICIAL else 0),
                      reverse=True)
        # Reported schemes are listed but never counted in totals.
        counted = [s for s in mine if s["status"] in OFFICIAL]
        despatched = disbursed(st, counted)
        states.append({
            "code": st["code"], "name": st["name"], "ut": st.get("ut", False),
            "pilot": st.get("pilot", False),
            # Site 'budget' is total expenditure on the standard definition (excl. debt repayment);
            # the seed's original 'budget' (incl. debt repayment, as published) is kept in data/ only.
            "budget": public(st.get("expenditure")), "beneficiary_announced": st.get("beneficiary_announced"),
            "beneficiary_despatched": despatched, "tax": public(st.get("tax")),
            "revenue": public(st.get("revenue")), "source": st.get("source"),
            "budget_actual": public(st.get("budget_actual")), "tax_actual": public(st.get("tax_actual")),
            "vs_re": comparisons[st["code"]],
            "schemes": [public_scheme(s) for s in mine],
        })

    site = {
        "fy": fy,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "central": seed["central"],
        "states": states,
        "counts": {
            "verified": sum(s["status"] == "verified" for s in fy_schemes),
            "secondary": sum(s["status"] == "secondary" for s in fy_schemes),
            "reported": sum(s["status"] == "reported" for s in fy_schemes),
            "pending_review": len(load(DATA / "review_queue.json", [])),
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(site, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if validation_out:
        save(validation_out, {"fy": fy, "issues": issues})
    site["validation"] = {lvl: sum(i["level"] == lvl for i in issues) for lvl in ("error", "blocked", "missing")}
    return site


if __name__ == "__main__":
    s = build()
    print(f"wrote {OUT.relative_to(ROOT)}: {len(s['states'])} states, counts={s['counts']}")
