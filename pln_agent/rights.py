"""Rights, identity and sensitivity gates.

`blockers` stop approval AND publishing. `review_flags` require a human (two-person signoff for the
SENSITIVE set) and correct platform labeling (paid-partnership toggle, AI-generated label).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

def _policy() -> dict:
    """config/rights_policy.json: management's standing rights decisions (versioned with the code)."""
    import json

    from .config import CONFIG_DIR
    try:
        return json.loads((CONFIG_DIR / "rights_policy.json").read_text())
    except FileNotFoundError:
        return {}


# flags that need two different approvers (rights_policy.json); an empty list means one approver for everything.
# Flags still label the post and add their checklist either way.
_DEFAULT_TWO_PERSON = {"gambling", "paid_partnership", "combat", "injury", "minors", "ai_generated_person", "likeness_unreleased"}
SENSITIVE = set(_policy().get("two_person_flags", _DEFAULT_TWO_PERSON))
# flags whose posts are never bulk-approved from the queue: one person still approves them, on the post's own page
OWN_PAGE = _DEFAULT_TWO_PERSON - {"combat"}


def likeness_releases_on_file() -> bool:
    return bool(_policy().get("likeness_releases_on_file"))


def identity_policy() -> str:
    """block (default): unverified identities block the asset.
    withhold_names: the asset may post, but no athlete is named until identities are verified."""
    import os
    return os.environ.get("PLN_IDENTITY_POLICY", "block")


LABEL_REQUIREMENTS = {
    "names_withheld": "Athlete identities are unverified: do not name or tag anyone in copy, captions or on-screen text.",
    "paid_partnership": "Turn on the platform's branded-content / paid-partnership disclosure and name the partner.",
    "ai_generated_person": "Apply the platform's AI-generated content label (TikTok AIGC toggle, Meta 'AI info', YouTube altered/synthetic disclosure).",
    "gambling": "No wagering call-to-action; check geo/age restrictions and each platform's gambling policy.",
    "combat": "Combat sports: no gore, slow-mo injury replays, or 'brutal KO' bait; check advertiser suitability.",
    "injury": "Remove or justify any injury footage; never use as the hook.",
    "minors": "Confirm guardian consent and platform minor-safety rules.",
    "likeness_unreleased": "Get a likeness/appearance release for identifiable people before use.",
}


@dataclass
class RightsResult:
    blockers: list[str] = field(default_factory=list)
    review_flags: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers

    @property
    def needs_two_person(self) -> bool:
        return any(f in SENSITIVE for f in self.review_flags)

    @property
    def needs_own_page(self) -> bool:
        return self.needs_two_person or any(f in OWN_PAGE for f in self.review_flags)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "blockers": self.blockers, "review_flags": self.review_flags,
                "labels": self.labels, "two_person_signoff": self.needs_two_person}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def evaluate(asset: dict, at: datetime | None = None, territory: str = "US", require_media: bool = False) -> RightsResult:
    at = at or datetime.now(timezone.utc)
    r = RightsResult()
    if not asset.get("rights_verified"):
        r.blockers.append("media rights not verified")
    if not (asset.get("license") or "").strip():
        r.blockers.append("no license/ownership record")
    exp = _parse(asset.get("rights_expires_at"))
    if exp and exp <= at:
        r.blockers.append(f"rights expired {exp.date().isoformat()}")
    terr = [t.upper() for t in (asset.get("territory") or [])]
    if terr and territory.upper() not in terr and "WORLDWIDE" not in terr:
        r.blockers.append(f"territory {territory} not licensed (licensed: {', '.join(terr)})")
    if not terr:
        r.blockers.append("territory not specified")
    if asset.get("has_audio") and not asset.get("music_cleared"):
        r.blockers.append("audio present but music/sound not cleared")
    if not asset.get("people_verified"):
        if identity_policy() == "withhold_names":
            # Post without naming anyone; copy validation rejects any unverified name that slips in.
            r.review_flags.append("names_withheld")
        else:
            r.blockers.append("people/athlete identities not verified")
    if require_media and not asset.get("media_present"):
        r.blockers.append("source media file missing")

    flags = asset.get("flags") or {}
    for f in ("gambling", "paid_partnership", "combat", "injury", "minors", "ai_generated_person"):
        if flags.get(f):
            r.review_flags.append(f)
    for p in asset.get("people") or []:
        if not p.get("likeness_release", False) and not likeness_releases_on_file():
            if "likeness_unreleased" not in r.review_flags:
                r.review_flags.append("likeness_unreleased")
    sponsors = asset.get("sponsors") or []
    if sponsors:
        if not asset.get("sponsor_approved"):
            r.blockers.append("sponsor appears but sponsor approval missing")
        if any(s.get("paid_partnership") for s in sponsors) and "paid_partnership" not in r.review_flags:
            r.review_flags.append("paid_partnership")
    r.labels = [LABEL_REQUIREMENTS[f] for f in r.review_flags if f in LABEL_REQUIREMENTS]
    return r
