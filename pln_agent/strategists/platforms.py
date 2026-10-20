"""Independent platform strategists. Each one owns its audience job, hook styles, edit decision list,
copy structure, hashtag policy, CTA, test hypothesis and validation schema.

All copy is assembled ONLY from: annotated source moments (with timecodes), verified facts, verified
people, brand voice descriptors and the content calendar. Anything not grounded is either omitted or
fails validation — the strategists never invent scores, names, sponsors, trends or results.
"""
from __future__ import annotations

from .base import CreativeContext, Hook, Moment, caption_hash, common_violations, tc

# Brand descriptors are creative direction (editable), not factual claims about events.
LEX = {
    "carjitsu": {"noun": "CarJitsu", "arena": "inside a car", "what": "grappling inside a car",
                 "tags": ["#CarJitsu", "#Grappling"], "debate": "Tap or keep fighting?",
                 "b2b": ("Constraint is a format strategy.",
                         "Put a grappling match inside a car and the rules explain themselves: tight space, visible stakes, "
                         "and a single decisive moment a new viewer understands without commentary.")},
    "putt_tour": {"noun": "Putt Tour", "arena": "on the green", "what": "pressure putting",
                  "tags": ["#PuttTour", "#Putting"], "debate": "Make or miss — what's your call?",
                  "b2b": ("The most legible stakes in sports might be one ball and one hole.",
                          "Putting compresses a whole competition into one readable shot. No rules primer needed, "
                          "and the reaction is the payoff.")},
    "pln": {"noun": "Pro League Network", "arena": "on PLN", "what": "new-format sports",
            "tags": ["#ProLeagueNetwork"], "debate": "Which sport should we break down next?",
            "b2b": ("New sports formats are a distribution advantage.",
                    "Novel formats earn attention because viewers have to stop and work out what they're seeing.")},
    "slapfight": {"noun": "SlapFIGHT", "arena": "at SlapFIGHT", "what": "sanctioned striking competition",
                  "tags": ["#SlapFIGHT"], "debate": "Who had the better strategy?",
                  "b2b": ("Rules-forward combat formats can be sponsor-safe.", "Clear rules and officiating are the product.")},
    "str33t": {"noun": "STR33T", "arena": "on the street court", "what": "street sports culture",
               "tags": ["#STR33T"], "debate": "Which crew takes the rematch?",
               "b2b": ("Local identity scales.", "Team rivalries and creator athletes travel well on social.")},
    "pro_carrom_tour": {"noun": "Pro Carrom Tour", "arena": "on the board", "what": "precision carrom",
                        "tags": ["#Carrom"], "debate": "Could you make that shot?",
                        "b2b": ("Precision sports have their own kind of drama.", "Small surface, huge skill ceiling.")},
}
LEX["world_putting_league"] = LEX["putt_tour"]


def lex(ctx: CreativeContext) -> dict:
    return LEX.get(ctx.asset["brand_id"], LEX["pln"])


def _sentence(s: str) -> str:
    s = (s or "").strip()
    return s if not s or s[-1] in ".!?…" else s + "."


def _first_fact(ctx: CreativeContext) -> str:
    return ctx.verified_facts[0] if ctx.verified_facts else ""


def _segments(*parts: tuple[Moment | None, str, float, float]) -> list[dict]:
    """parts: (moment, purpose, pre_roll, post_roll) → EDL segments clamped to clip bounds."""
    segs = []
    for m, purpose, pre, post in parts:
        if not m:
            continue
        segs.append({"source_in": round(max(0.0, m.start - pre), 2), "source_out": round(m.end + post, 2),
                     "purpose": purpose, "moment": m.label, "source_timecode": f"{tc(m.start)}–{tc(m.end)}"})
    # merge overlaps to avoid stutter
    segs.sort(key=lambda s: s["source_in"])
    merged: list[dict] = []
    for s in segs:
        if merged and s["source_in"] <= merged[-1]["source_out"]:
            merged[-1]["source_out"] = max(merged[-1]["source_out"], s["source_out"])
            merged[-1]["purpose"] += "+" + s["purpose"]
        else:
            merged.append(s)
    return merged


def _clamp(segs: list[dict], duration: float | None) -> list[dict]:
    if duration:
        for s in segs:
            s["source_out"] = round(min(s["source_out"], duration), 2)
    return [s for s in segs if s["source_out"] > s["source_in"]]


def _edl_len(segs: list[dict]) -> float:
    return round(sum(s["source_out"] - s["source_in"] for s in segs), 2)


class Strategist:
    platform = ""
    spec: dict = {}

    def hooks(self, ctx: CreativeContext) -> list[Hook]:
        raise NotImplementedError

    def build(self, ctx: CreativeContext) -> dict:
        raise NotImplementedError

    def validate(self, v: dict, ctx: CreativeContext) -> list[str]:
        errs = []
        s = self.spec
        text = " ".join([v.get("hook", ""), v.get("caption", ""), v.get("title", "") or "",
                         " ".join(v.get("onscreen_text", []) and [o["text"] for o in v["onscreen_text"]])])
        errs += common_violations(text, ctx)
        if len(v.get("caption", "")) > s["caption_max"]:
            errs.append(f"caption {len(v['caption'])} chars > {s['caption_max']}")
        if len(v.get("hashtags", [])) > s["hashtags_max"]:
            errs.append(f"{len(v['hashtags'])} hashtags > policy max {s['hashtags_max']}")
        if s.get("title_max") and len(v.get("title") or "") > s["title_max"]:
            errs.append(f"title > {s['title_max']} chars")
        if not v.get("cta"):
            errs.append("missing CTA")
        if not v.get("test_hypothesis"):
            errs.append("missing test hypothesis")
        edl = v.get("edl") or {}
        segs = edl.get("segments", [])
        if s.get("needs_video", True):
            if not segs:
                errs.append("no EDL segments grounded in annotated source moments")
            dur = ctx.asset.get("duration_s")
            for seg in segs:
                if dur and seg["source_out"] > dur + 0.05:
                    errs.append(f"EDL segment exceeds source duration ({seg['source_out']} > {dur})")
            total = _edl_len(segs)
            lo = s.get("bridge_min_s", s["min_s"]) if ctx.route_role == "legacy_bridge" else s["min_s"]
            if segs and not (lo <= total <= s["max_s"]):
                errs.append(f"cut length {total}s outside {lo}–{s['max_s']}s policy window")
        return errs

    # helpers
    def _hashtags(self, ctx: CreativeContext, extra: list[tuple[str, str]] | None = None, limit: int | None = None):
        L = lex(ctx)
        tags = [(L["tags"][0], "owned brand tag: lets fans find the property's archive")]
        if len(L["tags"]) > 1:
            tags.append((L["tags"][1], "sport/category discovery tag"))
        if ctx.route_role == "pln_amplification":
            tags.append(("#ProLeagueNetwork", "network tag ties amplification back to PLN"))
        tags += extra or []
        lim = self.spec["hashtags_max"] if limit is None else limit
        tags = tags[:lim]
        why = "; ".join(f"{t} = {r}" for t, r in tags) or "no hashtags: platform rewards conversation over tags"
        return [t for t, _ in tags], why + ". No trend tags: we have no verified trend data."

    def _finish(self, ctx: CreativeContext, v: dict) -> dict:
        v.setdefault("platform", self.platform)
        v["caption_hash"] = caption_hash(v["caption"])
        v["validation"] = {"errors": self.validate(v, ctx), "spec": self.spec}
        return v


class TikTokStrategist(Strategist):
    platform = "tiktok"
    spec = {"caption_max": 2200, "hashtags_max": 4, "min_s": 7, "max_s": 35, "aspect": "9:16",
            "north_star": "watch time + follows per 1k views", "audience": "younger discovery audiences"}

    def hooks(self, ctx):
        L, p = lex(ctx), ctx.peak
        if not p:
            return []
        hs = [Hook(f"Wait for it… the {p.label.lower()} {L['arena']}", p, "wait_for_it", 0.8, 0.9, 1.0),
              Hook(f"How does this even work {L['arena']}?", p, "question", 0.7, 0.85, 0.8)]
        if p.stakes:
            hs.append(Hook(f"{_sentence(p.stakes)} Then this.", p, "stakes", 0.9, 0.85, 1.0))
        if p.kind in ("reversal", "finish", "clutch"):
            hs.append(Hook(f"POV: you thought you had this {L['arena']}", p, "pov", 0.75, 0.9, 0.85))
        return sorted(hs, key=lambda h: -h.score)

    def build(self, ctx):
        L, p = lex(ctx), ctx.peak
        hooks = self.hooks(ctx)
        if ctx.route_role == "pln_amplification":  # PLN = discovery curator: different hook style than the property
            hooks = [x for x in hooks if x.style == "question"] + [x for x in hooks if x.style != "question"]
        h = hooks[0] if hooks else Hook(ctx.asset["title"], None, "title", 0.5, 0.4, 1.0)
        setup = ctx.before(p) if p else None
        react = ctx.after(p) if p else None
        explain = ctx.by_kind("explain")
        if ctx.route_role == "pln_amplification" and explain:
            setup, react = None, explain  # payoff first, then the rules context new viewers need
        segs = _clamp(_segments((setup, "tension (trimmed)", -max(0.0, (setup.dur - 2.5)) if setup else 0, 0),
                                (p, "payoff", 0.3, 0.2), (react, "reaction" if react is not explain else "context", 0, 0)), ctx.asset.get("duration_s"))
        unused = [m for m in ctx.moments if m not in (setup, p, react)]
        cta = "Follow for part 2" if unused else L["debate"]
        caption = f"{h.text} {_sentence(p.description) if p else ''} {L['debate']}".strip()
        if ctx.route_role == "pln_amplification":
            caption = f"This is {L['noun']}: {L['what']}. {_sentence(p.description) if p else ''} 🎥 {ctx.canonical_handle}".strip()
        if ctx.route_role == "legacy_inplace":
            caption += " (World Putting League is now Putt Tour — same league, new name.)"
        tags, why = self._hashtags(ctx)
        overlays = [{"start": 0.0, "end": 2.5, "text": h.text, "position": "upper_third"}]
        if _first_fact(ctx):
            overlays.append({"start": 3.0, "end": 6.0, "text": _first_fact(ctx), "position": "lower_third"})
        v = {"series": ctx.series or "Wait For It", "format": "9:16 native vertical, captions burned, no end card",
             "hook": h.text, "hook_candidates": [x.as_dict() for x in hooks], "caption": caption,
             "hashtags": tags, "hashtag_rationale": why, "title": None,
             "voiceover": "None. Native source audio only (cleared). Optional creator-style VO recorded by talent, not synthetic.",
             "onscreen_text": overlays,
             "edl": {"output": {"w": 1080, "h": 1920, "reframe": "subject_center_crop"}, "segments": segs,
                     "notes": "Cold-open 2–3s before payoff; first frame must show motion, not a title card. "
                              "No external watermarks, no app-injected promo branding."},
             "thumbnail": {"source_time": round((p.start + p.end) / 2, 2) if p else 0, "text": None,
                           "note": "TikTok cover chosen in-app from payoff frame"},
             "cta": cta, "angle": "discovery: immediate stakes, zero context required",
             "test_hypothesis": f"Cold-opening {tc(segs[0]['source_in']) if segs else '?'} (pre-payoff) vs chronological cut "
                                f"lifts avg watch time and follows/1k views for '{h.style}' hook."}
        return self._finish(ctx, v)


class InstagramStrategist(Strategist):
    platform = "instagram"
    spec = {"caption_max": 2200, "hashtags_max": 5, "min_s": 8, "bridge_min_s": 4, "max_s": 45, "aspect": "9:16",
            "north_star": "non-follower reach → follows; shares/saves", "audience": "property fandom"}

    def hooks(self, ctx):
        L, p = lex(ctx), ctx.peak
        if not p:
            return []
        hs = [Hook(f"{ctx.display_brand}: {p.label}", p, "title_card", 0.9, 0.6, 1.0),
              Hook(f"Send this to someone who thinks {L['what']} isn't a sport", p, "share_prompt", 0.75, 0.8, 0.85)]
        explain = ctx.by_kind("explain")
        if explain:
            hs.append(Hook(f"Rules in 20 Seconds: {L['noun']}", explain, "explainer", 0.9, 0.7, 0.8))
        return sorted(hs, key=lambda h: -h.score)

    def build(self, ctx):
        L, p = lex(ctx), ctx.peak
        hooks = self.hooks(ctx)
        h = hooks[0] if hooks else Hook(ctx.asset["title"], None, "title", 0.5, 0.4, 1.0)
        setup = ctx.before(p) if p else None
        react = ctx.after(p) if p else None
        segs = _clamp(_segments((setup, "setup", 0.5, 0), (p, "payoff", 0.2, 0.4), (react, "reaction", 0, 0.5)),
                      ctx.asset.get("duration_s"))
        lines = [h.text, "", _sentence(p.description) if p else ctx.asset.get("description", "")]
        if _first_fact(ctx):
            lines.append(_sentence(_first_fact(ctx)))
        cta = "Send this to the friend who needs to see it, and save it for the rematch."
        lines += ["", cta]
        if ctx.route_role == "pln_amplification":
            h = Hook(f"Sports you didn't know you needed: {L['noun']}", p, "discovery", 0.85, 0.8, 0.95)
            lines = [f"Sports you didn't know you needed: {L['noun']}.", "", _sentence(p.description) if p else "",
                     f"Full series on {ctx.canonical_handle}.", "", "Follow PLN for the weirdest real sports on earth."]
            cta = f"Follow {ctx.canonical_handle} + PLN"
        if ctx.route_role == "legacy_bridge":
            h = Hook("World Putting League is now Putt Tour", p, "rebrand_bridge", 0.95, 0.6, 1.0)
            lines = ["World Putting League is now Putt Tour.", "", _sentence(p.description) if p else "",
                     f"The full moment and every new drop lives at {ctx.canonical_handle}. Follow there so you don't miss the next one."]
            cta = f"Follow {ctx.canonical_handle}"
        if ctx.verified_people and ctx.route_role != "legacy_bridge":
            lines.append(f"Featuring {', '.join(x['name'] for x in ctx.verified_people)}.")
        tags, why = self._hashtags(ctx, limit=3 if ctx.route_role == "legacy_bridge" else None)
        if ctx.route_role == "legacy_bridge":  # teaser-only: shorter cut that points to the canonical account
            segs = _clamp(_segments((p, "teaser payoff", 1.5, 0.0)), ctx.asset.get("duration_s"))
        v = {"series": ctx.series or ("Rules in 20 Seconds" if h.style == "explainer" else "The Moment"),
             "format": "Reel 9:16 + carousel alt (setup / payoff / reaction stills)",
             "hook": h.text, "hook_candidates": [x.as_dict() for x in hooks], "caption": "\n".join(lines).strip(),
             "hashtags": tags, "hashtag_rationale": why, "title": None,
             "voiceover": "Optional host VO line over setup: '" + (_sentence(setup.description) if setup else "") + "'",
             "onscreen_text": [{"start": 0.0, "end": 2.0, "text": h.text, "position": "center"},
                               {"start": 2.0, "end": _edl_len(segs), "text": ctx.display_brand, "position": "lower_left_small"}],
             "edl": {"output": {"w": 1080, "h": 1920, "reframe": "subject_center_crop"}, "segments": segs,
                     "notes": "Chronological setup→payoff→reaction; branded lower-left text only (owned brand, no platform logos)."},
             "thumbnail": {"source_time": round((p.start + p.end) / 2, 2) if p else 0, "text": h.text[:40],
                           "carousel_frames": [m.start for m in (setup, p, react) if m]},
             "cta": cta, "angle": "brand fandom: identity + shareability",
             "collab_suggestion": (f"Invite {ctx.canonical_handle} as Collab (both accounts must accept in-app)"
                                   if ctx.route_role == "pln_amplification" else None),
             "test_hypothesis": f"'{h.style}' cover text vs. no-text cover changes non-follower reach→follow rate."}
        return self._finish(ctx, v)


class FacebookStrategist(Strategist):
    platform = "facebook"
    spec = {"caption_max": 5000, "hashtags_max": 1, "min_s": 10, "max_s": 120, "aspect": "4:5 or 16:9",
            "north_star": "returning engaged viewers, quality comments", "audience": "existing fans, community, older viewers"}

    def hooks(self, ctx):
        L, p = lex(ctx), ctx.peak
        if not p:
            return []
        return sorted([Hook(f"Did you catch this {L['noun']} moment?", p, "recall", 0.85, 0.6, 1.0),
                       Hook(f"Be honest: {L['debate']}", p, "debate", 0.8, 0.75, 0.9)], key=lambda h: -h.score)

    def build(self, ctx):
        L, p = lex(ctx), ctx.peak
        hooks = self.hooks(ctx)
        h = hooks[0] if hooks else Hook(ctx.asset["title"], None, "title", 0.5, 0.4, 1.0)
        ms = sorted(ctx.moments, key=lambda m: m.start)
        segs = _clamp([{"source_in": round(max(0, ms[0].start - 1.0), 2), "source_out": round(ms[-1].end + 1.0, 2),
                        "purpose": "full sequence", "moment": "+".join(m.label for m in ms),
                        "source_timecode": f"{tc(ms[0].start)}–{tc(ms[-1].end)}"}] if ms else [], ctx.asset.get("duration_s"))
        lines = [h.text, _sentence(ctx.asset.get("description") or (p.description if p else ""))]
        if _first_fact(ctx):
            lines.append(_sentence(_first_fact(ctx)))
        lines.append(f"Drop your answer below: {L['debate']}")
        if ctx.upcoming_event:
            lines.append(f"Next up: {ctx.upcoming_event['event_name']} ({ctx.upcoming_event['date']}).")
        if ctx.utm_url:
            lines.append(ctx.utm_url)
        tags, why = self._hashtags(ctx, limit=1)
        v = {"series": ctx.series or "Full Finish Friday", "format": "native video 4:5 (feed) with burned captions; full sequence",
             "hook": h.text, "hook_candidates": [x.as_dict() for x in hooks], "caption": "\n\n".join(lines),
             "hashtags": tags, "hashtag_rationale": why, "title": ctx.asset.get("title"),
             "voiceover": "None; source audio + burned captions for sound-off viewing.",
             "onscreen_text": [{"start": 0.0, "end": 3.0, "text": h.text, "position": "top"}],
             "edl": {"output": {"w": 1080, "h": 1350, "reframe": "pad_or_crop_4x5"}, "segments": segs,
                     "notes": "Let the whole sequence breathe; FB viewers stay for context. No quick-cut TikTok edit."},
             "thumbnail": {"source_time": round((p.start + p.end) / 2, 2) if p else 0, "text": ctx.asset.get("title")},
             "cta": "Comment your call and share with the fan who'd argue with you", "angle": "community conversation + return viewing",
             "test_hypothesis": "Specific debate question vs. generic 'thoughts?' raises meaningful comments per 1k reached."}
        return self._finish(ctx, v)


class LinkedInStrategist(Strategist):
    platform = "linkedin"
    spec = {"caption_max": 3000, "hashtags_max": 2, "min_s": 8, "max_s": 90, "aspect": "1:1",
            "north_star": "qualified inbound conversations", "audience": "sports media execs, sponsors, distributors, investors"}

    def hooks(self, ctx):
        L, p = lex(ctx), ctx.peak
        insight, _ = L["b2b"]
        return [Hook(insight, p, "insight", 0.9, 0.7, 0.95),
                Hook(f"What {L['noun']} teaches about building sports audiences", p, "lesson", 0.85, 0.6, 1.0)]

    def build(self, ctx):
        L, p = lex(ctx), ctx.peak
        hooks = self.hooks(ctx)
        h = hooks[0]
        insight, why_it_works = L["b2b"]
        ms = sorted(ctx.moments, key=lambda m: m.start)
        segs = _clamp(_segments(*[(m, m.kind, 0.3, 0.3) for m in ms[:3]]), ctx.asset.get("duration_s"))
        body = [h.text, "",
                f"In this clip: {(p.description if p else ctx.asset.get('description', '')).rstrip('.')}.", "",
                why_it_works, ""]
        facts = ctx.verified_facts
        if facts:
            body += [f"Verified detail: {_sentence(facts[0])}", ""]
        body += ["For partners, that legibility is the asset: a moment a fan can explain to a friend in one sentence "
                 "is a moment a brand can stand next to.", "",
                 "What would you measure first: completion, shares, or the follow that comes after?", "",
                 "If you're building fan-first formats or looking for distinctive sports inventory, PLN is open to the conversation."]
        tags, why = self._hashtags(ctx, extra=[], limit=0)
        tags, why = ["#SportsBusiness"], "#SportsBusiness = single professional discovery tag; LinkedIn reach is driven by dwell + comments, not tags."
        v = {"series": "Why This Sport Works", "format": "native 1:1 clip with captions + text post (document-carousel alt)",
             "hook": h.text, "hook_candidates": [x.as_dict() for x in hooks], "caption": "\n".join(body),
             "hashtags": tags, "hashtag_rationale": why, "title": None,
             "voiceover": "Optional exec POV voice-note recorded by a named PLN leader (requires their approval).",
             "onscreen_text": [{"start": 0.0, "end": 3.0, "text": h.text, "position": "top"}],
             "edl": {"output": {"w": 1080, "h": 1080, "reframe": "center_crop_1x1"}, "segments": segs,
                     "notes": "Sound-off friendly; captions mandatory; no hype graphics."},
             "thumbnail": {"source_time": round((p.start + p.end) / 2, 2) if p else 0, "text": None},
             "cta": "Invite qualified partnership / distribution conversations", "angle": "B2B: format legibility as commercial value",
             "test_hypothesis": "Insight-led first line vs. clip-description first line increases right-person comments and inbound DMs."}
        return self._finish(ctx, v)


class YouTubeStrategist(Strategist):
    platform = "youtube"
    spec = {"caption_max": 5000, "hashtags_max": 3, "title_max": 100, "min_s": 8, "max_s": 60, "aspect": "9:16",
            "north_star": "watch time, subs gained, Shorts→long-form", "audience": "search + Shorts feed"}

    def hooks(self, ctx):
        L, p = lex(ctx), ctx.peak
        if not p:
            return []
        return [Hook(f"{p.label} | {ctx.display_brand}", p, "searchable", 0.95, 0.6, 1.0),
                Hook(f"What is {L['noun']}? {L['what'].capitalize()}, explained by one moment", p, "explainer", 0.85, 0.7, 0.95)]

    def build(self, ctx):
        L, p = lex(ctx), ctx.peak
        hooks = self.hooks(ctx)
        h = hooks[0] if hooks else Hook(ctx.asset["title"], None, "title", 0.5, 0.4, 1.0)
        explain = ctx.by_kind("explain")
        segs = _clamp(_segments((explain, "context card", 0, 0), (p, "payoff", 1.0, 1.0), (ctx.after(p) if p else None, "reaction", 0, 0)),
                      ctx.asset.get("duration_s"))
        desc = [_sentence(ctx.asset.get("description", "")), f"{L['noun']} is {L['what']}."]
        if ctx.asset.get("event_name"):
            desc.append(f"From {ctx.asset['event_name']}.")
        if ctx.utm_url:
            desc.append(f"More: {ctx.utm_url}")
        title = h.text
        if ctx.route_role == "pln_amplification":
            title = f"{L['noun']} in one moment | PLN Network Picks"
            desc = [f"From the Pro League Network family: {L['noun']}.", f"The full channel is {ctx.canonical_handle}.",
                    "PLN Network Picks collects the best moments across every PLN sport."]
        tags, why = self._hashtags(ctx, limit=2)
        v = {"series": "PLN Network Picks" if ctx.route_role == "pln_amplification" else "Shorts → full match playlist", "format": "YouTube Short 9:16; link related long-form video in-app",
             "hook": title, "hook_candidates": [x.as_dict() for x in hooks], "caption": "\n".join(desc),
             "hashtags": tags, "hashtag_rationale": why, "title": title[:100],
             "voiceover": f"Optional explainer VO: '{L['noun']} is {L['what']}. Watch the {p.label.lower() if p else 'moment'}.'",
             "onscreen_text": [{"start": 0.0, "end": 3.0, "text": f"What is {L['noun']}?", "position": "upper_third"}],
             "edl": {"output": {"w": 1080, "h": 1920, "reframe": "subject_center_crop"}, "segments": segs,
                     "notes": "Context first (search viewers are new), then payoff."},
             "thumbnail": {"source_time": round((p.start + p.end) / 2, 2) if p else 0, "text": None},
             "cta": "Subscribe + watch the full match (related video link)", "angle": "search/evergreen explainer",
             "test_hypothesis": "Searchable descriptive title vs. curiosity title changes Shorts→long-form click-through."}
        return self._finish(ctx, v)


class XStrategist(Strategist):
    platform = "x"
    spec = {"caption_max": 280, "hashtags_max": 1, "min_s": 3, "max_s": 30, "aspect": "16:9",
            "north_star": "replies + link clicks in live windows", "audience": "live/real-time fans, media"}

    def hooks(self, ctx):
        p = ctx.peak
        if not p:
            return [Hook(ctx.asset["title"], None, "title", 0.6, 0.5, 1.0)]
        lead = f"{p.stakes.rstrip('.')}… then the {p.label.lower()}." if p.stakes else f"{p.label}."
        return [Hook(lead, p, "moment_line", 0.9, 0.6, 1.0)]

    def build(self, ctx):
        L, p = lex(ctx), ctx.peak
        h = self.hooks(ctx)[0]
        parts = [h.text]
        if ctx.asset.get("event_name"):
            parts.append(f"{ctx.asset['event_name']}.")
        if ctx.route_role == "legacy_inplace":
            parts.append("(We're Putt Tour now.)")
        if ctx.utm_url:
            parts.append(ctx.utm_url)
        text = " ".join(parts)
        if len(text) > 280:
            text = text[:277] + "…"
        segs = _clamp(_segments((p, "payoff", 0.5, 0.5)), ctx.asset.get("duration_s"))
        v = {"series": "Live Moments", "format": "post + short native clip (16:9)", "hook": h.text,
             "hook_candidates": [h.as_dict()], "caption": text, "hashtags": [], "title": None,
             "hashtag_rationale": "No hashtag unless an official event tag exists; X copy is context, not tags.",
             "voiceover": None, "onscreen_text": [],
             "edl": {"output": {"w": 1280, "h": 720, "reframe": "none"}, "segments": segs, "notes": "Payoff only."},
             "thumbnail": {"source_time": round((p.start + p.end) / 2, 2) if p else 0, "text": None},
             "cta": "Reply with your take", "angle": "real-time context and results",
             "test_hypothesis": "Posting within the live window vs. next-day changes replies + link clicks per 1k impressions."}
        return self._finish(ctx, v)


class TwitchStrategist(Strategist):
    platform = "twitch"
    spec = {"caption_max": 140, "hashtags_max": 0, "min_s": 0, "max_s": 0, "aspect": "n/a", "needs_video": False,
            "north_star": "average concurrent viewers", "audience": "live community"}

    def hooks(self, ctx):
        return [Hook(f"{lex(ctx)['noun']} live watch-along", None, "live_title", 0.8, 0.6, 1.0)]

    def build(self, ctx):
        L = lex(ctx)
        title = f"{L['noun']} watch-along + Q&A"
        if ctx.upcoming_event:
            title = f"{ctx.upcoming_event['event_name']}: live watch-along"
        v = {"series": "PLN Live", "format": "stream segment proposal (Twitch has no clip-upload API; schedule only)",
             "hook": title, "hook_candidates": [h.as_dict() for h in self.hooks(ctx)], "caption": title[:140],
             "hashtags": [], "hashtag_rationale": "Twitch uses category + tags in stream settings, set manually.",
             "title": title[:140], "voiceover": "Live host required; no unattended loops.", "onscreen_text": [],
             "edl": {"segments": [], "notes": "Live programming. Requires streaming rights for any match footage shown."},
             "thumbnail": {}, "cta": "Follow PLN on Twitch for the next live session", "angle": "live community",
             "test_hypothesis": "Event-adjacent scheduling vs. standalone sessions changes average concurrent viewers."}
        return self._finish(ctx, v)


STRATEGISTS: dict[str, Strategist] = {s.platform: s for s in (
    TikTokStrategist(), InstagramStrategist(), FacebookStrategist(), LinkedInStrategist(),
    YouTubeStrategist(), XStrategist(), TwitchStrategist())}
