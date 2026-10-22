"""Public links for approved renders (https://media.proleaguenetwork.com/v/<unguessable>/<name>.mp4).

Instagram, Facebook and TikTok pull the video from a URL, so the finished file must be reachable without
signing. Only an APPROVED post's render is ever copied to the public bucket (a separate bucket behind
CloudFront; the masters and draft renders stay private). Keys are random, nothing lists the bucket, and
objects expire after 45 days (lifecycle rule) — long after the platform has ingested them.
"""
from __future__ import annotations

import os
import re
import secrets

from .db import DB, now_iso

PUBLIC_STATES = ("approved", "scheduled", "publishing", "published")


class PublicMediaError(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("PLN_MEDIA_PUBLIC_BUCKET") and os.environ.get("PLN_MEDIA_PUBLIC_BASE_URL"))


def _copy(src_bucket: str, src_key: str, bucket: str, key: str, content_type: str = "video/mp4") -> None:
    import boto3
    boto3.client("s3").copy_object(Bucket=bucket, Key=key, CopySource={"Bucket": src_bucket, "Key": src_key},
                                   ContentType=content_type, CacheControl="public, max-age=86400",
                                   MetadataDirective="REPLACE")


def publish_render(db: DB, variant_id: str, actor: str, copier=_copy) -> str:
    v = db.get("variants", "variant_id", variant_id)
    if not v:
        raise PublicMediaError(f"unknown post {variant_id}")
    if v["state"] not in PUBLIC_STATES:
        raise PublicMediaError("only approved posts get a public link (approve it first)")
    if v.get("public_media_url"):
        return v["public_media_url"]
    path = v.get("render_path") or ""
    if v.get("render_status") != "rendered" or not path.startswith("s3://"):
        raise PublicMediaError("render the finished post first")
    if not configured():
        raise PublicMediaError("public media hosting is not configured (PLN_MEDIA_PUBLIC_BUCKET / _BASE_URL)")
    from . import carousel
    if carousel.is_slides(v):  # every slide gets its own public link; the first one stands for the post
        try:
            slides = carousel.public_links(v, os.environ["PLN_MEDIA_PUBLIC_BUCKET"], os.environ["PLN_MEDIA_PUBLIC_BASE_URL"], copier)
        except carousel.CarouselError as e:
            raise PublicMediaError(str(e)) from e
        url = slides[0]["public_url"]
        db.update("variants", "variant_id", variant_id, {"public_media_url": url, "slides": slides, "updated_at": now_iso()})
        db.audit(actor, "public_link_created", "variants", variant_id, {"url": url, "items": len(slides)})
        return url
    src_bucket, _, src_key = path[5:].partition("/")
    slug = re.sub(r"[^a-z0-9]+", "-", f"{v['brand_id']}-{v['platform']}".lower()).strip("-")
    key = f"v/{secrets.token_urlsafe(18)}/{slug}.mp4"
    copier(src_bucket, src_key, os.environ["PLN_MEDIA_PUBLIC_BUCKET"], key)
    url = os.environ["PLN_MEDIA_PUBLIC_BASE_URL"].rstrip("/") + "/" + key
    db.update("variants", "variant_id", variant_id, {"public_media_url": url, "updated_at": now_iso()})
    db.audit(actor, "public_link_created", "variants", variant_id, {"url": url})
    return url
