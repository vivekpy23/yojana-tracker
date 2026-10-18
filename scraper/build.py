"""Turn data/*.json into the single file the static site reads: site/data/site.json."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .store import DATA, ROOT, SCHEMES, SEED, load

OUT = ROOT / "site" / "data" / "site.json"


def build(out: Path = OUT) -> dict:
    seed = load(SEED, {})
    schemes = load(SCHEMES, [])
    fy = seed["fy"]
    fy_schemes = [s for s in schemes if s["fy"] == fy]

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
            "budget": st.get("budget"), "beneficiary_announced": st.get("beneficiary_announced"),
            "beneficiary_despatched": despatched, "tax": st.get("tax"), "source": st.get("source"),
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
    out.write_text(json.dumps(site, ensure_ascii=False, separators=(",", ":")))
    return site


if __name__ == "__main__":
    s = build()
    print(f"wrote {OUT.relative_to(ROOT)}: {len(s['states'])} states, counts={s['counts']}")
