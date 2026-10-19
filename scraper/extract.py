"""Pull scheme candidates (state, scheme, ₹ amount, article type, benefit, beneficiaries, department) out of
free text such as news articles – English plus Hindi/Marathi (Devanagari), Tamil and Kannada."""
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
# Regional-language names (Devanagari for Hindi/Marathi, Tamil, Kannada), merged into STATES.
STATES_LOCAL = {
    "MH": ["महाराष्ट्र"], "UP": ["उत्तर प्रदेश", "यूपी"], "TN": ["तमिलनाडु", "தமிழ்நாடு", "தமிழக"],
    "KA": ["कर्नाटक", "ಕರ್ನಾಟಕ", "கர்நாடக"], "RJ": ["राजस्थान"], "BR": ["बिहार"], "MP": ["मध्य प्रदेश"],
    "GJ": ["गुजरात"], "HR": ["हरियाणा"], "CG": ["छत्तीसगढ़"], "JH": ["झारखंड"], "UK": ["उत्तराखंड"],
    "HP": ["हिमाचल प्रदेश"], "PB": ["पंजाब"], "DL": ["दिल्ली"], "OD": ["ओडिशा"], "WB": ["पश्चिम बंगाल"],
    "AP": ["आंध्र प्रदेश"], "TS": ["तेलंगाना"], "KL": ["केरल"], "AS": ["असम"],
}
for _code, _names in STATES_LOCAL.items():
    STATES[_code] = STATES[_code] + _names
CENTRE_CUES = ["Union Budget", "Centre", "Central government", "Union government", "Finance Minister Nirmala",
               "केंद्र सरकार", "केंद्रीय", "மத்திய அரசு", "ಕೇಂದ್ರ ಸರ್ಕಾರ"]

INDIC = r"[ऀ-ॿ஀-௿ಀ-೿]"

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
# ₹500 करोड़ | 36 हजार करोड़ रुपये | ரூ.13,807 கோடி | 51,034 ಕೋಟಿ ರೂ. – a rupee sign or word is required, so
# "2 करोड़ महिलाएं" (2 crore women) is not read as an amount.
_CRORE_LOCAL = r"(?:लाख\s*)?(?:करो[डड़]़?|कोटी)|(?:லட்சம்\s*)?கோடி|(?:ಲಕ್ಷ\s*)?ಕೋಟಿ"
_THOUSAND_LOCAL = r"हजार|हज़ार|ஆயிரம்|ಸಾವಿರ"
_RUPEE_PRE = r"(?:Rs\.?|₹|रु\.?|ரூ\.?|ರೂ\.?)"
_RUPEE_POST = r"(?:रुपये|रुपए|रुपयांच\S*|ரூபாய்|ರೂಪಾಯಿ|ರೂ\.?)"
INDIC_AMOUNT_RE = re.compile(
    r"(" + _RUPEE_PRE + r"\s*)?([\d,]+(?:\.\d+)?)\s*(" + _THOUSAND_LOCAL + r")?\s*(" + _CRORE_LOCAL + r")(\s*" + _RUPEE_POST + r")?"
)
ALLOC_CUES = re.compile(r"allocat|earmark|provision|set aside|outlay|budget|sanction|announc|"
                        r"प्रावधान|आवंटन|बजट|तरतूद|ஒதுக்கீடு|பட்ஜெட்|ಅನುದಾನ|ಬಜೆಟ್", re.I)
LEADING_NOISE = re.compile(r"^(?:The|A|An|For|Of|And|Under|Its|New|Under the|Rs)\s+", re.I)


def to_crore(number: str, unit: str) -> float:
    value = float(number.replace(",", ""))
    unit = unit.lower().replace(" ", "")
    if unit.startswith(("lakh", "लाख", "லட்சம்", "ಲಕ್ಷ")):
        value *= 100_000
    return round(value, 2)


def find_amounts(text: str) -> list[tuple[int, float]]:
    """Return (char offset, amount in crore) for every rupee amount in text."""
    out = [(m.start(), to_crore(m.group(1), m.group(2))) for m in AMOUNT_RE.finditer(text)]
    for m in INDIC_AMOUNT_RE.finditer(text):
        if not (m.group(1) or m.group(5)):
            continue  # no rupee sign or word: a count, not money
        value = to_crore(m.group(2), m.group(4)) * (1000 if m.group(3) else 1)
        out.append((m.start(), round(value, 2)))
    return sorted(out)


# Per-beneficiary benefit: "₹1,500 per month", "Rs 6,000 a year", "₹2,100 प्रति माह", "1500 रुपये महीना".
_PERIODS = {"month": "month", "monthly": "month", "year": "year", "annum": "year", "annually": "year",
            "yearly": "year", "instalment": "instalment", "installment": "instalment"}
BENEFIT_RE = re.compile(
    r"(?:Rs\.?|₹|INR)\s*([\d,]+)(?![\d,]|\s*-?\s*(?:lakh|crore|cr\b))\s*(?:(?:per|a|an|each|every)\s+|/\s*)?"
    r"(month|year|annum|instalment|installment|monthly|annually|yearly)\b", re.I)
_LOCAL_PERIOD = {"प्रति माह": "month", "प्रतिमाह": "month", "हर महीने": "month", "महीना": "month", "दरमहा": "month",
                 "महिना": "month", "सालाना": "year", "प्रति वर्ष": "year", "மாதம்": "month", "மாதந்தோறும்": "month",
                 "ಪ್ರತಿ ತಿಂಗಳು": "month", "ತಿಂಗಳಿಗೆ": "month"}
BENEFIT_LOCAL_RE = re.compile(
    r"(?:(?:₹|रु\.?|ரூ\.?|ರೂ\.?)\s*([\d,]+)|([\d,]+)\s*(?:रुपये|रुपए|ரூபாய்|ರೂಪಾಯಿ))\s*(?:की\s+)?("
    + "|".join(sorted(_LOCAL_PERIOD, key=len, reverse=True)) + ")")


def find_benefit(text: str) -> dict | None:
    """The first per-beneficiary benefit in rupees, e.g. {'amount_rs': 1500, 'per': 'month'}."""
    m = BENEFIT_RE.search(text)
    if m:
        return {"amount_rs": int(m.group(1).replace(",", "")), "per": _PERIODS[m.group(2).lower()]}
    m = BENEFIT_LOCAL_RE.search(text)
    if m:
        return {"amount_rs": int((m.group(1) or m.group(2)).replace(",", "")), "per": _LOCAL_PERIOD[m.group(3)]}
    return None


_COUNT_UNIT = {"lakh": 1e5, "crore": 1e7, "million": 1e6, "लाख": 1e5, "कोटी": 1e7, "லட்சம்": 1e5, "கோடி": 1e7,
               "ಲಕ್ಷ": 1e5, "ಕೋಟಿ": 1e7}
BENEFICIARIES_RE = re.compile(
    r"([\d,]*\d(?:\.\d+)?)\s*(lakh|crore|million)?\s+(?:eligible\s+|more\s+)?"
    r"(beneficiaries|women|farmers|families|households|students|girls|people|pensioners|workers)\b", re.I)
BENEFICIARIES_LOCAL_RE = re.compile(
    r"([\d,]*\d(?:\.\d+)?)\s*(लाख|करो[डड़]़?|कोटी|லட்சம்|கோடி|ಲಕ್ಷ|ಕೋಟಿ)?\s*"
    r"(?:लाभार्थ|महिला|किसान|परिवार|लाडक्या|பயனாளி|பெண்|ಫಲಾನುಭವಿ|ಮಹಿಳೆ)")


def find_beneficiaries(text: str) -> int | None:
    """An estimated beneficiary count ("2.5 crore women", "1.15 कोटी लाभार्थी") as a number of people."""
    for rx in (BENEFICIARIES_RE, BENEFICIARIES_LOCAL_RE):
        m = rx.search(text)
        if m:
            unit = (m.group(2) or "").lower()
            n = float(m.group(1).replace(",", "")) * (1e7 if unit.startswith("करो") else _COUNT_UNIT.get(unit, 1))
            if n >= 100:  # "5 women" in an anecdote is not a beneficiary estimate
                return int(n)
    return None


_DEPT_WORD = r"(?:[A-Z][\w'’&-]*|and|&|of|the|for)"
DEPT_RE = re.compile(
    r"\b((?:Department|Ministry) of " + _DEPT_WORD + r"(?:\s+" + _DEPT_WORD + r")*)"
    r"|\b((?:[A-Z][\w'’&-]*\s+(?:and\s+|&\s+)?){1,5}Department)\b")
DEPT_LOCAL_RE = re.compile(r"((?:" + INDIC + r"+\s+){1,4})(विभाग|துறை|ಇಲಾಖೆ)")


def find_department(text: str) -> str:
    m = DEPT_RE.search(text)
    if m:
        name = re.sub(r"(\s+(?:and|&|of|the|for))+$", "", (m.group(1) or m.group(2)).strip())
        return re.sub(r"^The\s+", "", name)
    m = DEPT_LOCAL_RE.search(text)
    if m:
        words = _trim_indic(m.group(1).split())
        if words:
            return " ".join(words + [m.group(2)])
    return ""


# What an article says about a scheme. Precedence: disbursement > revised allocation > new scheme.
NEW_SCHEME, EXISTING_SCHEME, REVISED_ALLOCATION, DISBURSEMENT = (
    "new_scheme", "existing_scheme", "revised_allocation", "disbursement_update")
_KIND_CUES = [
    (DISBURSEMENT, re.compile(
        r"\b(?:disburs\w*|released?|transferr?\w*|credited|deposited|instal+ments?|paid out|DBT transfer)\b|"
        r"जारी कि|जारी की|ट्रांसफर|हस्तांतरित|किस्त|खाते में|वितरित|हप्ता|जमा कर|வரவு வைக்க|விடுவிக்க|தவணை|"
        r"ಬಿಡುಗಡೆ|ಕಂತು|ಜಮಾ", re.I)),
    (REVISED_ALLOCATION, re.compile(
        r"\b(?:revised|hike[ds]?|raised|enhanced|increased|additional allocation|supplementary|slashed|"
        r"cut|reduced|top-?up)\b|बढ़ा|बढ़ोतरी|संशोधित|अतिरिक्त|अनुपूरक|कटौती|घटा|वाढ|உயர்த்த|கூடுதல்|ಹೆಚ್ಚಳ|ಹೆಚ್ಚುವರಿ",
        re.I)),
    (NEW_SCHEME, re.compile(
        r"\b(?:launch\w*|unveil\w*|introduc\w*|rolls? out|rolled out|new scheme|new yojana)\b|"
        r"शुरू|लॉन्च|नई योजना|शुभारंभ|सुरू|नवी योजना|தொடங்கி|புதிய திட்ட|அறிமுக|ಆರಂಭ|ಹೊಸ ಯೋಜನೆ", re.I)),
]


def classify(text: str) -> str:
    """new_scheme | revised_allocation | disbursement_update, or '' when the text gives no cue."""
    for kind, rx in _KIND_CUES:
        if rx.search(text or ""):
            return kind
    return ""


def clean_scheme(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" ,.-")
    prev = None
    while prev != name:
        prev = name
        name = LEADING_NOISE.sub("", name)
    return name


# Regional scheme names: the words before योजना / अभियान / திட்டம் / ಯೋಜನೆ, cut at the first function word.
# Inflected endings (Marathi योजनेत, Tamil திட்டத்தின்) are stored in their base form.
_INDIC_SUFFIX = {"योजना": "योजना", "योजनेत": "योजना", "योजनेचा": "योजना", "योजनेची": "योजना", "योजनेचे": "योजना",
                 "योजनांतर्गत": "योजना", "योजने": "योजना", "अभियान": "अभियान", "मिशन": "मिशन",
                 "திட்டம்": "திட்டம்", "திட்டத்தின்": "திட்டம்", "திட்டத்தை": "திட்டம்", "திட்டத்தில்": "திட்டம்",
                 "ಯೋಜನೆ": "ಯೋಜನೆ", "ಯೋಜನೆಯ": "ಯೋಜನೆ", "ಯೋಜನೆಯಡಿ": "ಯೋಜನೆ", "ಯೋಜನೆಗೆ": "ಯೋಜನೆ",
                 "योजनेसाठी": "योजना", "योजनेला": "योजना", "திட்டத்துக்கு": "திட்டம்", "திட்டத்திற்கு": "திட்டம்"}
INDIC_SCHEME_RE = re.compile(
    r"((?:" + INDIC + r"+\s+){1,5})(" + "|".join(sorted(_INDIC_SUFFIX, key=len, reverse=True)) + r")(?!" + INDIC + ")")
_INDIC_STOP = {"ने", "की", "के", "का", "में", "से", "को", "और", "पर", "लिए", "है", "हैं", "इस", "यह", "तहत", "नई", "नए",
               "सरकार", "राज्य", "केंद्र", "द्वारा", "एक", "व", "तथा", "अंतर्गत", "आणि", "या", "ही", "हे", "नवी", "शासनाने",
               "सरकारने", "அரசு", "மற்றும்", "புதிய", "ಸರ್ಕಾರ", "ಮತ್ತು", "ಹೊಸ",
               # determiners and verbs – योजना also means "plan" ("पूरी योजना", "उसकी योजना")
               "बल्कि", "इसके", "इसकी", "उसके", "उसकी", "उनकी", "उनके", "पूरी", "पूरा", "कोई", "किसी", "अपनी", "अपने",
               "सभी", "हर", "बड़ी", "बड़े", "नया", "नयी", "कुछ", "ऐसी", "ऐसे", "जो", "जिस", "भी", "तो", "था", "थी",
               "थे", "रही", "रहे", "गई", "गया", "किया", "करने", "लेकर", "बताया", "सारी", "हमारी", "आपकी"}


def _trim_indic(words: list[str]) -> list[str]:
    keep: list[str] = []
    for w in reversed(words):
        if w in _INDIC_STOP or not re.match(INDIC, w):
            break
        keep.insert(0, w)
    return keep


def find_schemes(text: str) -> list[tuple[int, str]]:
    out = []
    for m in SCHEME_RE.finditer(text):
        name = clean_scheme(m.group(1))
        # Need at least one distinctive word besides the suffix (skip bare "Scheme", "the Mission").
        if len(name.split()) < 2:
            continue
        out.append((m.start(), name))
    for m in INDIC_SCHEME_RE.finditer(text):
        words = _trim_indic(m.group(1).split())
        if words:
            out.append((m.start(2) - len(" ".join(words)) - 1, " ".join(words + [_INDIC_SUFFIX[m.group(2)]])))
    return sorted(out)


def _mentions(name: str, text: str) -> bool:
    # \b is unreliable around Indic vowel signs, so regional names are matched as plain substrings.
    return bool(re.search(r"\b" + re.escape(name) + r"\b", text)) if name.isascii() else name in text


def detect_states(text: str) -> list[str]:
    return [code for code, names in STATES.items() if any(_mentions(n, text) for n in names)]


def is_centre(text: str) -> bool:
    return any(c.lower() in text.lower() for c in CENTRE_CUES)


SENTENCE_SPLIT = re.compile(r"(?<=[.!?।])\s+(?=[A-Z₹\"'ऀ-ॿ஀-௿ಀ-೿])")
FY_RE = re.compile(r"20\d\d-\d\d")


def fy_of(date: str, budget: bool = False) -> str:
    """Indian financial year (April–March) of an ISO date: 2026-10-19 -> '2026-27'. With `budget`, a date in
    January–March gives the coming year, since budgets presented then are for the next FY."""
    y, m = int(date[:4]), int(date[5:7])
    start = y if m >= 4 or budget else y - 1
    return f"{start}-{str(start + 1)[-2:]}"


BUDGET_CUE = re.compile(r"budget|बजट|अर्थसंकल्प|பட்ஜெட்|ಬಜೆಟ್", re.I)


@dataclass
class Candidate:
    state: str
    name: str
    amount: float | None          # ₹ crore; None when the article gives no scheme-level amount
    sentence: str
    confidence: float
    source_url: str = ""
    source_title: str = ""
    published: str = ""
    extra: dict = field(default_factory=dict)
    kind: str = EXISTING_SCHEME   # new_scheme | existing_scheme | revised_allocation | disbursement_update
    benefit: dict | None = None   # per-beneficiary benefit in rupees, e.g. {"amount_rs": 1500, "per": "month"}
    beneficiaries: int | None = None
    department: str = ""
    fy: str = ""
    lang: str = "en"

    def report(self) -> dict:
        """What is kept from an article: extracted facts and the link, never the article text."""
        return {"url": self.source_url, "title": self.source_title, "outlet": self.extra.get("feed", ""),
                "published": self.published, "lang": self.lang, "kind": self.kind, "fy": self.fy or None,
                "amount_cr": self.amount, "benefit": self.benefit, "beneficiaries": self.beneficiaries,
                "department": self.department or None}


def extract_candidates(text: str, *, url: str = "", title: str = "", published: str = "",
                       fy: str = "2025-26", state_hint: str | None = None, lang: str = "en",
                       as_of: str = "") -> list[Candidate]:
    """Pair each scheme mention with the nearest amount in the same sentence.

    A sentence is used when it names a scheme and carries a ₹ crore amount, a per-beneficiary benefit, a
    beneficiary count, or a launch / disbursement cue. The article's state is the one named in the title,
    else the feed's state, else the most-mentioned one in the body; a sentence naming a different single
    state overrides it. The candidate's FY is the one the sentence names, else the FY of the publication
    date (or `as_of`, the day it was read, for undated pages) – the next FY for a budget story from
    January–March – else `fy`.
    """
    date = published or as_of
    article_states = detect_states(title) or ([state_hint] if state_hint else _dominant_state(text))
    title_kind = classify(title)
    candidates: list[Candidate] = []
    for sentence in SENTENCE_SPLIT.split(text):
        amounts = find_amounts(sentence)
        benefit = find_benefit(sentence)
        people = find_beneficiaries(sentence)
        kind = classify(sentence) or title_kind
        if not (amounts or benefit or people or kind in (NEW_SCHEME, DISBURSEMENT)):
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
        named_fy = set(FY_RE.findall(sentence))
        cand_fy = (fy if fy in named_fy else next(iter(named_fy)) if len(named_fy) == 1
                   else fy_of(date, budget=bool(BUDGET_CUE.search(title + " " + sentence))) if date else fy)
        dept = find_department(sentence)
        used = set()
        for s_pos, s_name in schemes:
            best = None
            if amounts:
                # nearest amount (prefer the one after the name, as in "X Yojana gets Rs 500 crore")
                best = min(amounts, key=lambda a: (abs(a[0] - s_pos) + (0 if a[0] >= s_pos else 15)))
                if best in used:
                    best = None
                else:
                    used.add(best)
            conf = 0.9 if (ALLOC_CUES.search(sentence) or kind) else 0.6
            if best is not None:
                if len(schemes) > 1 and len(amounts) == 1:
                    conf -= 0.2  # ambiguous: several schemes, one number
                between = sentence[min(best[0], s_pos):max(best[0], s_pos)]
                if re.search(r"\b(including|such as|like|among)\b", between, re.I):
                    conf -= 0.4  # the number is a group total, not this scheme's
            candidates.append(Candidate(state, s_name, best[1] if best else None, sentence.strip(), round(conf, 2),
                                        url, title, published, kind=kind or EXISTING_SCHEME, benefit=benefit,
                                        beneficiaries=people, department=dept, fy=cand_fy, lang=lang))
    return candidates


def _dominant_state(text: str) -> list[str]:
    counts = {}
    for code, names in STATES.items():
        n = sum(len(re.findall(r"\b" + re.escape(x) + r"\b", text)) if x.isascii() else text.count(x) for x in names)
        if n:
            counts[code] = n
    if not counts:
        return []
    top = max(counts.values())
    return [c for c, n in counts.items() if n == top]


_INDIC_GENERIC = {"योजना", "मुख्यमंत्री", "திட்டம்", "முதலமைச்சர்", "ಯೋಜನೆ", "ಮುಖ್ಯಮಂತ್ರಿ"}


def slugify(text: str) -> str:
    text = re.sub(r"\(.*?\)", "", text.lower())
    text = re.sub(r"\b(mukhyamantri|mukhya mantri|chief minister'?s?|cm|pradhan mantri|pm|the|scheme|yojana|yojna)\b", " ", text)
    text = " ".join(w for w in text.split() if w not in _INDIC_GENERIC)
    # Regional scripts are kept, so two different Hindi names never share a slug.
    return re.sub(r"[^a-z0-9ऀ-ॿ஀-௿ಀ-೿]+", "-", text).strip("-") or "scheme"
