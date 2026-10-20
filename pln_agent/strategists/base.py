"""Shared strategist machinery: creative context, fact-safety, hook scoring, validation primitives."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

BANNED_PATTERNS = [
    r"you won'?t believe", r"\binsane\b", r"gone wrong", r"\bdestroy(?:ed|s)\b", r"\bbrutal\b", r"\bKO'?d\b",
    r"\bshocking\b", r"\bexposed\b", r"\bdrama\b", r"\bbet (?:now|on)\b", r"\bodds\b", r"\bguaranteed\b",
    r"\bviral\b", r"\bbreak(?:ing)? the internet\b", r"\bfight (?:broke out|erupts)\b",
    # sensationalized injury language (machine-written clip captions often use it)
    r"\bdevastating\b", r"\bout cold\b", r"\bknocked (?:him |her )?out\b", r"\blights out\b", r"\bcarnage\b",
]
WATERMARK_WORDS = ["watermark", "instagram logo", "tiktok logo", "capcut", "reels logo"]


@dataclass
class Moment:
    start: float
    end: float
    label: str
    description: str
    kind: str = "moment"  # setup | payoff | reaction | explain | reversal | clutch | finish | skill | absurd
    intensity: float = 0.5
    stakes: str = ""
    out_of_range: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Moment":
        return cls(start=float(d["start"]), end=float(d["end"]), label=d.get("label", ""),
                   description=d.get("description", ""), kind=d.get("kind", "moment"),
                   intensity=float(d.get("intensity", 0.5)), stakes=d.get("stakes", ""),
                   out_of_range=bool(d.get("out_of_range")))

    @property
    def dur(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class CreativeContext:
    asset: dict
    account: dict
    brand: dict
    platform: str
    route_role: str  # canonical | pln_amplification | legacy_bridge | legacy_inplace
    display_brand: str
    canonical_handle: str = ""  # who to credit / redirect to
    utm_url: str = ""
    upcoming_event: dict | None = None
    series_override: str = ""
    moments: list[Moment] = field(default_factory=list)

    def __post_init__(self):
        if not self.moments:
            self.moments = [Moment.from_dict(m) for m in (self.asset.get("moments") or []) if not m.get("out_of_range")]

    # ---- grounded material -------------------------------------------------------------
    @property
    def verified_facts(self) -> list[str]:
        return [f["text"] for f in (self.asset.get("facts") or []) if f.get("verified")]

    @property
    def verified_people(self) -> list[dict]:
        if not self.asset.get("people_verified"):
            return []
        return [p for p in (self.asset.get("people") or []) if p.get("verified")]

    def person_ref(self, i: int = 0, fallback: str = "one competitor") -> str:
        ppl = self.verified_people
        return ppl[i]["name"] if len(ppl) > i else fallback

    def by_kind(self, *kinds: str) -> Moment | None:
        c = [m for m in self.moments if m.kind in kinds]
        return max(c, key=lambda m: m.intensity) if c else None

    @property
    def peak(self) -> Moment | None:
        return max(self.moments, key=lambda m: m.intensity) if self.moments else None

    def before(self, m: Moment) -> Moment | None:
        c = [x for x in self.moments if x.end <= m.start + 0.01]
        return max(c, key=lambda x: x.end) if c else None

    def after(self, m: Moment) -> Moment | None:
        c = [x for x in self.moments if x.start >= m.end - 0.01]
        return min(c, key=lambda x: x.start) if c else None

    @property
    def series(self) -> str:
        return self.series_override or self.asset.get("series") or ""

    @property
    def grounding_corpus(self) -> str:
        a = self.asset
        parts = [a.get("title", ""), a.get("description", ""), a.get("event_name", ""), a.get("event_date") or "",
                 *self.verified_facts, *[m.description + " " + m.label + " " + m.stakes for m in self.moments],
                 *[p["name"] for p in self.verified_people], self.display_brand, self.canonical_handle, self.utm_url]
        if self.upcoming_event:
            parts += [self.upcoming_event.get("event_name", ""), self.upcoming_event.get("date", "")]
        return " ".join(parts)


@dataclass
class Hook:
    text: str
    moment: Moment | None
    style: str
    clarity: float
    curiosity: float
    accuracy: float  # 1.0 = directly grounded in annotated footage

    @property
    def score(self) -> float:
        length_pen = 0.0 if len(self.text) <= 70 else min(0.3, (len(self.text) - 70) / 200)
        return round(0.4 * self.accuracy + 0.3 * self.clarity + 0.3 * self.curiosity - length_pen, 3)

    def as_dict(self) -> dict:
        return {"text": self.text, "style": self.style, "score": self.score,
                "source_timecode": [self.moment.start, self.moment.end] if self.moment else None}


def caption_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).lower().encode()).hexdigest()[:16]


def shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    w = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def tc(sec: float) -> str:
    m, s = divmod(sec, 60)
    return f"{int(m):02d}:{s:05.2f}"


def ungrounded_numbers(text: str, corpus: str) -> list[str]:
    """Numbers in copy that don't appear in the grounded source material (possible invented facts)."""
    allowed = set(re.findall(r"\d+(?:[.,:]\d+)*", corpus)) | {"1", "2", "20"}  # '1' for 'part 1', '20s' explainers
    found = re.findall(r"(?<![\w#@/=.-])\d+(?:[.,:]\d+)*", re.sub(r"https?://\S+", "", text))
    return [n for n in found if n not in allowed]


_JUNK_NAME = re.compile(r"^(person|face|unknown|cluster)\s*#?\s*\d+$", re.I)


def is_junk_name(name: str) -> bool:
    """Tagger output that isn't a person's name: face-cluster placeholders ("person #2775"),
    1–2 character fragments and short all-caps codes ("FPS")."""
    n = (name or "").strip()
    return (not n or len(n) < 3 or bool(_JUNK_NAME.match(n)) or (n.isupper() and len(n) <= 3 and n.isalpha()))


def house_vocabulary(ctx: "CreativeContext") -> set[str]:
    """Words our own copy legitimately uses: brand names/aliases, network name, platform handles."""
    from ..accounts import load_brands
    words = {"pro league network", "pln", "league", "network", ctx.display_brand.lower()}
    for b in load_brands():
        words.add(b["name"].lower())
        words.update(a.lower() for a in b.get("aliases", []))
        words.update(w.lower() for w in re.findall(r"[A-Za-z0-9]+", b["name"]))
    if ctx.canonical_handle:
        words.add(ctx.canonical_handle.lower().lstrip("@"))
    return words


def common_violations(text: str, ctx: CreativeContext) -> list[str]:
    errs = []
    low = text.lower()
    for pat in BANNED_PATTERNS:
        if re.search(pat, low):
            errs.append(f"banned clickbait/unsafe phrase matches /{pat}/")
    if "[verify" in low or "tbd" in low.split():
        errs.append("unresolved VERIFY/TBD placeholder")
    vocab = house_vocabulary(ctx)
    for p in ctx.asset.get("people") or []:
        name = (p.get("name") or "").strip()
        if (ctx.asset.get("people_verified") and p.get("verified")) or is_junk_name(name):
            continue
        n = name.lower()
        if n in vocab:  # e.g. a tagger "person" called "League" vs our own "Pro League Network"
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low):
            errs.append(f"names unverified person {name!r}")
    bad = ungrounded_numbers(text, ctx.grounding_corpus)
    if bad:
        errs.append(f"numbers not found in verified source material: {bad}")
    if any(w in low for w in WATERMARK_WORDS):
        errs.append("references third-party watermark/logo")
    return errs
