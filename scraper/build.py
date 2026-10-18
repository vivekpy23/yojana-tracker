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


def build(out: Path = OUT, validation_out: Path | None = VALIDATION) -> dict:
    seed = load(SEED, {})
    schemes = load(SCHEMES, [])
    fy = seed["fy"]
    fy_schemes = [s for s in schemes if s["fy"] == fy]
    comparisons, issues = validate.run(seed["states"], fy)

    states = []
    for st in seed["states"]:
        mine = sorted((s for s in fy_schemes if s["state"] == st["code"]),
                      key=lambda s: s["announced"] or 0, reverse=True)
        # Unverified schemes are listed but never counted in totals.
        counted = [s for s in mine if s["status"] != "unverified"]
        despatched = st.get("beneficiary_despatched")
        if despatched is None and counted and all(s.get("despatched") is not None for s in counted):
            despatched = {"value": sum(s["despatched"] for s in counted), "basis": "provisional",
                          "status": "verified"}
        states.append({
            "code": st["code"], "name": st["name"], "ut": st.get("ut", False),
            "pilot": st.get("pilot", False),
            # Site 'budget' is total expenditure on the standard definition (excl. debt repayment);
            # the seed's original 'budget' (incl. debt repayment, as published) is kept in data/ only.
            "budget": public(st.get("expenditure")), "beneficiary_announced": st.get("beneficiary_announced"),
            "beneficiary_despatched": despatched, "tax": public(st.get("tax")), "source": st.get("source"),
            "budget_actual": public(st.get("budget_actual")), "tax_actual": public(st.get("tax_actual")),
            "vs_re": comparisons[st["code"]],
            "schemes": [{k: s.get(k) for k in ("name", "announced", "despatched", "status", "sources", "note")}
                        for s in mine],
        })

    site = {
        "fy": fy,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "central": seed["central"],
        "states": states,
        "counts": {
            "verified": sum(s["status"] == "verified" for s in fy_schemes),
            "secondary": sum(s["status"] == "secondary" for s in fy_schemes),
            "unverified": sum(s["status"] == "unverified" for s in fy_schemes),
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
