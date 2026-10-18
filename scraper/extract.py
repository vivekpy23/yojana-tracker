"""Pull (state, scheme, amount) candidates out of free text such as news articles."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

STATES = {
    "MH": ["Maharashtra", "Mumbai"],
    "UP": ["Uttar Pradesh", "UP government", "Lucknow"],
    "TN": ["Tamil Nadu", "Chennai"],
    "KA": ["Karnataka", "Bengaluru", "Bangalore"],
    "RJ": ["Rajasthan", "Jaipur"],
    "AP": ["Andhra Pradesh"], "AR": ["Arunachal Pradesh"], "AS": ["Assam"], "BR": ["Bihar"],
    "CG": ["Chhattisgarh"], "GA": ["Goa"], "GJ": ["Gujarat"], "HR": ["Haryana"],
    "HP": ["Himachal Pradesh"], "JH": ["Jharkhand"], "KL": ["Kerala"], "MP": ["Madhya Pradesh"],
    "MN": ["Manipur"], "ML": ["Meghalaya"], "MZ": ["Mizoram"], "NL": ["Nagaland"],
    "OD": ["Odisha"], "PB": ["Punjab"], "SK": ["Sikkim"], "TS": ["Telangana"], "TR": ["Tripura"],
    "UK": ["Uttarakhand"], "WB": ["West Bengal"], "DL": ["Delhi"], "JK": ["Jammu and Kashmir", "Jammu & Kashmir"],
    "PY": ["Puducherry"],
}
CENTRE_CUES = ["Union Budget", "Centre", "Central government", "Union government", "Finance Minister Nirmala"]

# Words that end a scheme name in Indian budget writing.
SCHEME_SUFFIX = (
    r"Yojana|Yojna|Scheme|Mission|Abhiyan|Abhiyaan|Thittam|Thogai|Nidhi|Guarantees?|Pension|"
    r"Fund|Programme|Program|Bhagya|Lakshmi|Jyothi|Shakti|Shakthi|Samman|Sammaan|Kosh|Payanam"
)
SCHEME_RE = re.compile(
    r"((?:(?:[A-Z][\w'’.-]*|of|for|and|&|the|Dr\.)\s+){0,7}(?:" + SCHEME_SUFFIX + r")\b(?:\s*\([A-Za-z\s-]{2,40}\))?)"
)
# Rs 36,000 crore | ₹1.2 lakh crore | INR 500 cr | Rs. 3,500-crore
AMOUNT_RE = re.compile(
    r"(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d+)?)\s*-?\s*(lakh\s*crore|lakh\s*cr|crore|cr\b)",
    re.IGNORECASE,
)
ALLOC_CUES = re.compile(r"allocat|earmark|provision|set aside|outlay|budget|sanction|announc", re.I)
LEADING_NOISE = re.compile(r"^(?:The|A|An|For|Of|And|Under|Its|New|Under the|Rs)\s+", re.I)


def to_crore(number: str, unit: str) -> float:
    value = float(number.replace(",", ""))
    unit = unit.lower().replace(" ", "")
    if unit.startswith("lakh"):
        value *= 100_000
    return round(value, 2)


def find_amounts(text: str) -> list[tuple[int, float]]:
    """Return (char offset, amount in crore) for every rupee amount in text."""
    return [(m.start(), to_crore(m.group(1), m.group(2))) for m in AMOUNT_RE.finditer(text)]


def clean_scheme(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" ,.-")
    prev = None
    while prev != name:
        prev = name
        name = LEADING_NOISE.sub("", name)
    return name


def find_schemes(text: str) -> list[tuple[int, str]]:
    out = []
    for m in SCHEME_RE.finditer(text):
        name = clean_scheme(m.group(1))
        # Need at least one distinctive word besides the suffix (skip bare "Scheme", "the Mission").
        if len(name.split()) < 2:
            continue
        out.append((m.start(), name))
    return out


def detect_states(text: str) -> list[str]:
    found = []
    for code, names in STATES.items():
        if any(re.search(r"\b" + re.escape(n) + r"\b", text) for n in names):
            found.append(code)
    return found


def is_centre(text: str) -> bool:
    return any(c.lower() in text.lower() for c in CENTRE_CUES)


SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z₹\"'])")


@dataclass
class Candidate:
    state: str
    name: str
    amount: float
    sentence: str
    confidence: float
    source_url: str = ""
    source_title: str = ""
    published: str = ""
    extra: dict = field(default_factory=dict)


def extract_candidates(text: str, *, url: str = "", title: str = "", published: str = "",
                       fy: str = "2025-26") -> list[Candidate]:
    """Pair each scheme mention with the nearest amount in the same sentence.

    The article's state is the one named in the title, else the most-mentioned one in the body.
    Sentences that name a different single state override it.
    """
    article_states = detect_states(title) or _dominant_state(text)
    candidates: list[Candidate] = []
    for sentence in SENTENCE_SPLIT.split(text):
        amounts = find_amounts(sentence)
        if not amounts:
            continue
        schemes = find_schemes(sentence)
        if not schemes:
            continue
        local = detect_states(sentence)
        state = local[0] if len(local) == 1 else (article_states[0] if len(article_states) == 1 else None)
        if state is None:
            if is_centre(sentence) or is_centre(title):
                state = "CENTRE"
            else:
                continue
        # A sentence that talks about a different FY is not ours.
        other_fy = re.findall(r"20\d\d-\d\d", sentence)
        if other_fy and fy not in other_fy:
            continue
        used = set()
        for s_pos, s_name in schemes:
            # nearest amount (prefer the one after the name, as in "X Yojana gets Rs 500 crore")
            best = min(amounts, key=lambda a: (abs(a[0] - s_pos) + (0 if a[0] >= s_pos else 15)))
            if best in used:
                continue
            used.add(best)
            conf = 0.9 if ALLOC_CUES.search(sentence) else 0.6
            if len(schemes) > 1 and len(amounts) == 1:
                conf -= 0.2  # ambiguous: several schemes, one number
            between = sentence[min(best[0], s_pos):max(best[0], s_pos)]
            if re.search(r"\b(including|such as|like|among)\b", between, re.I):
                conf -= 0.4  # the number is a group total, not this scheme's
            candidates.append(Candidate(state, s_name, best[1], sentence.strip(), round(conf, 2),
                                        url, title, published))
    return candidates


def _dominant_state(text: str) -> list[str]:
    counts = {}
    for code, names in STATES.items():
        n = sum(len(re.findall(r"\b" + re.escape(x) + r"\b", text)) for x in names)
        if n:
            counts[code] = n
    if not counts:
        return []
    top = max(counts.values())
    return [c for c, n in counts.items() if n == top]


def slugify(text: str) -> str:
    text = re.sub(r"\(.*?\)", "", text.lower())
    text = re.sub(r"\b(mukhyamantri|mukhya mantri|chief minister'?s?|cm|pradhan mantri|pm|the|scheme|yojana|yojna)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "scheme"
