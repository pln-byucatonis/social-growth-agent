"""Optional LLM refinement layer. The system boots and runs fully on deterministic templates.

If PLN_LLM_PROVIDER=anthropic and the `anthropic` SDK + credentials are available, Claude proposes
alternative hooks/captions under a strict JSON schema. Every suggestion is re-run through the same
platform validator (grounding, banned phrases, numbers, limits); anything failing is discarded and the
template copy is kept. The LLM never sees credentials and never publishes anything.
"""
from __future__ import annotations

import json

from .strategists.base import CreativeContext, caption_hash

SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string"},
        "caption": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["hook", "caption", "rationale"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are Pro League Network's platform-native social editor. Voice: irreverent, high-energy sports "
    "entertainment that stays sponsor-safe. Rewrite the hook and caption for the named platform. HARD RULES: use only "
    "facts present in SOURCE; never add names, numbers, scores, results, sponsors, trends or claims that are not in "
    "SOURCE; no clickbait ('you won't believe', 'insane', 'brutal'), no rage bait, no wagering calls to action, no "
    "engagement bait; keep the existing CTA intent. Return JSON only."
)


class TemplateProvider:
    name = "template"

    def refine(self, variant: dict, ctx: CreativeContext, validator) -> dict:
        return variant


class AnthropicProvider:
    name = "anthropic"
    model = "claude-opus-5-5"

    def __init__(self):
        import anthropic  # optional dependency; ImportError → caller falls back to templates
        self.client = anthropic.Anthropic()

    def refine(self, variant: dict, ctx: CreativeContext, validator) -> dict:
        source = {"platform": ctx.platform, "brand": ctx.display_brand, "route_role": ctx.route_role,
                  "asset_title": ctx.asset.get("title"), "asset_description": ctx.asset.get("description"),
                  "verified_facts": ctx.verified_facts, "verified_people": [p["name"] for p in ctx.verified_people],
                  "moments": [{"label": m.label, "description": m.description, "kind": m.kind} for m in ctx.moments],
                  "current_hook": variant["hook"], "current_caption": variant["caption"], "cta": variant["cta"],
                  "spec": validator.spec}
        resp = self.client.beta.messages.create(
            model=self.model, max_tokens=4000, system=SYSTEM,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": "SOURCE:\n" + json.dumps(source, indent=2)}],
        )
        if resp.stop_reason != "end_turn":  # refusal / max_tokens → keep template copy
            return variant
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
        cand = {**variant, "hook": data["hook"], "caption": data["caption"]}
        errs = validator.validate(cand, ctx)
        if errs:
            variant.setdefault("llm_rejected", []).append({"errors": errs, "suggestion": data})
            return variant
        cand["caption_hash"] = caption_hash(cand["caption"])
        cand["llm_rationale"] = data["rationale"]
        cand["hook_candidates"] = [{"text": data["hook"], "style": "llm", "score": None}] + variant["hook_candidates"]
        return cand


def get_provider(name: str):
    if name == "anthropic":
        try:
            return AnthropicProvider()
        except Exception:  # missing SDK or credentials → deterministic path
            return TemplateProvider()
    return TemplateProvider()
