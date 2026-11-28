"""TikTok post settings: a person chooses how each TikTok post goes out, as TikTok's Direct Post rules require.

TikTok's Content Sharing Guidelines (developers.tiktok.com/doc/content-sharing-guidelines) ask that, before a video is
posted through the API, the person posting sees and chooses:

* which account it goes to (name and avatar, read live from TikTok's creator info);
* the title (the caption), which they can edit;
* who can watch it, picked from the options TikTok allows that account, with nothing picked in advance;
* whether people can comment, duet and stitch: off until ticked, and greyed out where the account has them turned off;
* whether it's commercial content (their own brand and/or a paid partnership), with the label TikTok will add;
* TikTok's Music Usage Confirmation (and the Branded Content Policy for a paid partnership) before they post;
* a preview of the video, and that it can take a few minutes to appear after posting.

The agent never fills these in: until a person saves them (treatment.tiktok), a TikTok post can't be scheduled and
the publisher refuses to send it. Editing the caption afterwards voids the choice.
"""
from __future__ import annotations

import os

from .db import DB, now_iso

PRIVACY_LABELS = {"PUBLIC_TO_EVERYONE": "Everyone", "MUTUAL_FOLLOW_FRIENDS": "Friends",
                  "FOLLOWER_OF_CREATOR": "Followers", "SELF_ONLY": "Only me"}
MUSIC_URL = "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en"
BRANDED_URL = "https://www.tiktok.com/legal/page/global/bc-policy/en"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
# TikTok's error codes from creator info that mean "this account can't post right now"
CANT_POST = {"spam_risk_too_many_posts": "this account has posted too much today; try again later",
             "spam_risk_user_banned_from_posting": "TikTok has stopped this account from posting",
             "reached_active_user_cap": "the app has reached TikTok's daily limit of posting accounts"}


class TikTokPostError(Exception):
    pass


def audited() -> bool:
    """Until TikTok audits the app, every post it sends is private (Only me)."""
    return os.environ.get("TIKTOK_CLIENT_AUDITED", "false").lower() == "true"


def creator_info(account: dict, transport=None) -> dict:
    """The account as TikTok sees it right now: name, avatar, who-can-watch options, interactions turned off,
    longest video allowed."""
    from .adapters import get_adapter
    from .adapters.base import AdapterError, HttpRequest, resolve_token
    ad = get_adapter("tiktok", transport=transport)
    try:
        token = resolve_token(account.get("credential_ref") or "")
        body = ad._send(HttpRequest("POST", CREATOR_INFO_URL, {"Authorization": f"Bearer {token}",
                                                              "Content-Type": "application/json; charset=UTF-8"}, {},
                                    "creator info"), "tiktok creator_info").body
    except AdapterError as e:
        code = ((e.response or {}).get("error") or {}).get("code", "")
        raise TikTokPostError(CANT_POST.get(code) or ("connect this TikTok account first" if e.kind in ("blocked", "auth")
                                                      else f"TikTok didn't answer: {str(e)[:160]}"))
    code = (body.get("error") or {}).get("code", "ok")
    if code != "ok":
        raise TikTokPostError(CANT_POST.get(code, f"TikTok said: {code}"))
    d = body.get("data") or {}
    return {"username": d.get("creator_username", ""), "nickname": d.get("creator_nickname", ""),
            "avatar": d.get("creator_avatar_url", ""), "privacy_options": list(d.get("privacy_level_options") or []),
            "comment_disabled": bool(d.get("comment_disabled")), "duet_disabled": bool(d.get("duet_disabled")),
            "stitch_disabled": bool(d.get("stitch_disabled")),
            "max_duration_s": d.get("max_video_post_duration_sec")}


def duration(v: dict) -> float | None:
    qc = v.get("render_qc") or {}
    return qc.get("duration_s") or ((v.get("treatment") or {}).get("editor") or {}).get("length_s")


def choose(db: DB, vid: str, form: dict, info: dict, actor: str) -> dict:
    """Check and save what the person chose (treatment.tiktok). Raises TikTokPostError with what to fix."""
    from .workflow import edit_variant
    v = db.get("variants", "variant_id", vid)
    if not v or v.get("platform") != "tiktok":
        raise TikTokPostError("not a TikTok post")
    privacy = form.get("privacy_level") or ""
    if privacy not in info["privacy_options"]:
        raise TikTokPostError("choose who can watch this video")
    commercial = bool(form.get("commercial"))
    your_brand, branded = commercial and bool(form.get("your_brand")), commercial and bool(form.get("branded_content"))
    if commercial and not (your_brand or branded):
        raise TikTokPostError("commercial content is on: say whether it promotes your own brand, a paid partnership, or both")
    if branded and privacy == "SELF_ONLY":
        raise TikTokPostError("a paid partnership can't be private: choose who can watch it")
    if not audited() and privacy != "SELF_ONLY":
        raise TikTokPostError("until TikTok approves the app, posts can only be private (Only me)")
    d, cap = duration(v), info.get("max_duration_s")
    if d and cap and d > cap:
        raise TikTokPostError(f"this video is {d:.0f} s; this account can post up to {cap} s")
    title = (form.get("title") or "").strip()
    if not title:
        raise TikTokPostError("the title can't be empty")
    if len(title) > 2200:
        raise TikTokPostError("the title is over TikTok's 2,200 characters")
    if title != (v.get("caption") or "").strip():
        edit_variant(db, vid, {"caption": title}, actor)  # the person edited it: this is the text they approve
        v = db.get("variants", "variant_id", vid)
    allow = {k: bool(form.get(f"allow_{k}")) and not info[f"{k}_disabled"] for k in ("comment", "duet", "stitch")}
    chosen = {"privacy_level": privacy, "disable_comment": not allow["comment"], "disable_duet": not allow["duet"],
              "disable_stitch": not allow["stitch"], "brand_organic_toggle": your_brand, "brand_content_toggle": branded,
              "creator": {"username": info["username"], "nickname": info["nickname"]},
              "caption_hash": v["caption_hash"], "by": actor, "at": now_iso()}
    t = dict(v.get("treatment") or {})
    t["tiktok"] = chosen
    db.update("variants", "variant_id", vid, {"treatment": t, "updated_at": now_iso()})
    db.audit(actor, "tiktok_settings_chosen", "variants", vid, {k: x for k, x in chosen.items() if k != "caption_hash"})
    return chosen


def chosen(v: dict) -> dict | None:
    """The person's settings, if they still apply to this exact caption."""
    s = (v.get("treatment") or {}).get("tiktok")
    return s if s and s.get("caption_hash") == v.get("caption_hash") else None


def label(s: dict) -> str:
    """What TikTok will show on the post, for the person to see before posting."""
    if s.get("brand_content_toggle"):
        return "Paid partnership"
    return "Promotional content" if s.get("brand_organic_toggle") else ""
