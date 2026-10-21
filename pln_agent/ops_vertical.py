"""Video Production × the ops vertical pipeline.

The ops portal already runs AWS Elemental MediaConvert Smart Crop (subject tracking) through
`PlnFastStack-VerticalizeFn…`, which accepts an arbitrary window of a master:
    {"source_bucket", "source_key", "start_sec", "end_sec", "label"}
and writes  s3://<assets bucket>/verticals/<label>/…_9x16.mp4  (plus a 16:9) asynchronously.

So for every Creative Director cut (A ≈12s / B ≈25s / C ≈40s) of a master-based asset the agent asks ops for a
subject-tracked 9:16 of exactly that window, polls S3 for the result, then renders its own on-screen text +
captions on top (no further cropping) and runs QC. Anything ops can't do falls back to the agent's motion-centred
crop. MediaConvert is the only vertical source: the older VerticalFn renditions are retired and never used.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .db import DB, now_iso

VERTICALIZE_FN = os.environ.get("PLN_OPS_VERTICALIZE_FN", "PlnFastStack-VerticalizeFn13D4A47C-imDAmk4raRWX")
_lambda = _s3 = None


def _clients():
    global _lambda, _s3
    if _lambda is None:
        import boto3
        _lambda, _s3 = boto3.client("lambda", region_name="us-east-1"), boto3.client("s3", region_name="us-east-1")
    return _lambda, _s3


def label_for(variant_id: str, cut: str) -> str:
    return f"agent-{hashlib.sha1(variant_id.encode()).hexdigest()[:12]}-{cut}"


def _master(asset: dict) -> tuple[str, str] | None:
    """(bucket, key) when the asset's media is a full master (cut times are absolute in it)."""
    path = asset.get("media_path") or ""
    if not path.startswith("s3://") or "/verticals/" in path:
        return None
    b, _, k = path[5:].partition("/")
    return b, k


def request_cuts(db: DB, variant_id: str, actor: str, invoke=None, only: list[str] | None = None) -> dict:
    """Ask ops for a subject-tracked 9:16 of every available cut (or just `only`) that doesn't have one yet."""
    v = db.get("variants", "variant_id", variant_id)
    asset = db.get("assets", "asset_id", v["asset_id"])
    t = dict(v.get("treatment") or {})
    m = _master(asset)
    if not m:
        return {"requested": [], "skipped": "asset isn't a master on S3 — rendered with the agent's own crop"}
    invoke = invoke or (lambda payload: _clients()[0].invoke(FunctionName=VERTICALIZE_FN, InvocationType="Event",
                                                              Payload=json.dumps(payload).encode()))
    requested, cuts = [], []
    for c in t.get("duration_variants") or []:
        c = dict(c)
        ov = c.get("ops_vertical") or {}
        if c.get("available") and c.get("segments") and ov.get("status") not in ("requested", "ready") \
                and (only is None or c["label"] in only):
            seg = c["segments"][0]
            label = label_for(variant_id, c["label"])
            invoke({"source_bucket": m[0], "source_key": m[1], "start_sec": seg["source_in"], "end_sec": seg["source_out"],
                    "label": label})
            c["ops_vertical"] = {"label": label, "status": "requested", "requested_at": now_iso(),
                                 "window": [seg["source_in"], seg["source_out"]]}
            requested.append(c["label"])
        cuts.append(c)
    if requested:
        t["duration_variants"] = cuts
        db.update("variants", "variant_id", variant_id, {"treatment": t, "updated_at": now_iso()})
        db.audit(actor, "ops_vertical_requested", "variants", variant_id, {"cuts": requested, "fn": VERTICALIZE_FN})
    return {"requested": requested}


def poll(db: DB, assets_bucket: str, settings=None, lister=None, render=True, actor: str = "video-production") -> dict:
    """Pick up finished ops verticals; render text + QC on each newly ready cut."""
    lister = lister or (lambda prefix: [o["Key"] for o in _clients()[1].list_objects_v2(
        Bucket=assets_bucket, Prefix=prefix).get("Contents", [])])
    ready = []
    for v in db.query("SELECT variant_id FROM variants WHERE treatment::text LIKE ?" if db.dialect != "sqlite" else
                      "SELECT variant_id FROM variants WHERE treatment LIKE ?", ['%"status": "requested"%']):
        row = db.get("variants", "variant_id", v["variant_id"])
        t = dict(row.get("treatment") or {})
        changed = []
        for c in t.get("duration_variants") or []:
            ov = c.get("ops_vertical") or {}
            if ov.get("status") != "requested":
                continue
            keys = [k for k in lister(f"verticals/{ov['label']}/") if re.search(r"9x16\.mp4$", k)]
            if keys:
                ov.update({"status": "ready", "key": keys[0], "ready_at": now_iso()})
                c["ops_vertical"] = ov
                changed.append(c)
        if changed:
            db.update("variants", "variant_id", row["variant_id"], {"treatment": t, "updated_at": now_iso()})
            ready += [(row["variant_id"], c["label"]) for c in changed]
            if render and settings is not None:
                from .media import render_cuts
                render_cuts(db, row["variant_id"], settings, actor, only=[c["label"] for c in changed])
    return {"ready": ready}
