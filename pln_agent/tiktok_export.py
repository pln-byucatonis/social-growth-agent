"""TikTok Studio export upload: the numbers TikTok shows in Studio, until the TikTok API is approved.

TikTok Studio → Analytics → Download data gives one file per tab (CSV or XLSX): Overview (a row per day: video views,
profile views, likes, comments, shares), Followers (a row per day: followers and the change) and Content (a row per
video: link, post time, views, likes, comments, shares). Headers differ a little between exports and languages, so
columns are matched by what they contain, not their exact name; the upload page shows what was read before anything is
saved. Rows land in `metrics` (source `tiktok_studio_export`, idempotent `metric_key`, so re-uploading the same file
changes nothing) and videos become `posts` rows, the same tables the Instagram insights fill.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, datetime
from xml.etree import ElementTree

from .db import DB, now_iso
from .metrics import family

SOURCE = "tiktok_studio_export"
# column -> (native metric, words that identify the header); first match wins, so the specific ones come first
DAILY = [("new_followers", ("difference", "net", "change", "new follower")), ("follower_count", ("follower",)),
         ("profile_views", ("profile",)), ("video_views", ("video view", "views")), ("likes", ("like",)),
         ("comments", ("comment",)), ("shares", ("share",))]
VIDEO = [("video_views", ("view", "play")), ("likes", ("like",)), ("comments", ("comment",)), ("shares", ("share",)),
         ("saves", ("save", "favorite", "favourite"))]


class ExportError(Exception):
    pass


def _rows_xlsx(data: bytes) -> list[list[str]]:
    """First sheet of an .xlsx, as text (no openpyxl needed)."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
        out = []
        for r in ElementTree.fromstring(z.read(sheet)).iter(f"{{{ns['m']}}}row"):
            row: dict[int, str] = {}
            for c in r.findall("m:c", ns):
                col = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(re.sub(r"\d", "", c.get("r", "A")))))
                v = c.find("m:v", ns)
                txt = c.find("m:is/m:t", ns)
                val = (txt.text if txt is not None else (v.text if v is not None else "")) or ""
                if c.get("t") == "s" and val:
                    val = shared[int(val)]
                row[col] = val
            out.append([row.get(i, "") for i in range(1, max(row, default=0) + 1)])
        return out


def read_rows(name: str, data: bytes) -> list[list[str]]:
    if name.lower().endswith(".xlsx") or data[:2] == b"PK":
        return _rows_xlsx(data)
    text = data.decode("utf-8-sig", errors="replace")
    return [r for r in csv.reader(io.StringIO(text))]


def _num(s: str) -> float | None:
    s = (s or "").strip().replace(",", "")
    if not s or s.lower() in {"-", "--", "n/a", "na"}:
        return None
    m = re.fullmatch(r"([\d.]+)\s*([km]?)", s.lower())
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2)]


def _day(s: str, today: date) -> str | None:
    """TikTok writes dates several ways, sometimes without a year ("October 21"): that means the latest such day."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{5}(\.\d+)?", s):  # an Excel serial date
        return date.fromordinal(date(1899, 12, 30).toordinal() + int(float(s))).isoformat()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in fmt or "%H" in fmt else s, fmt).date().isoformat()
        except ValueError:
            pass
    for fmt in ("%B %d", "%b %d", "%d %B", "%d %b"):
        for year in (today.year, today.year - 1):  # the year in the string too, so Feb 29 parses
            try:
                d = datetime.strptime(f"{s} {year}", fmt + " %Y").date()
            except ValueError:
                continue
            if d <= today:
                return d.isoformat()
    return None


def _match(headers: list[str], spec) -> dict[int, str]:
    out: dict[int, str] = {}
    for i, h in enumerate(headers):
        hl = h.strip().lower()
        for metric, words in spec:
            if metric not in out.values() and any(w in hl for w in words):
                out[i] = metric
                break
    return out


def parse(name: str, data: bytes, today: date | None = None) -> dict:
    """{kind: daily|videos, rows: [...], columns: {header: metric}, skipped: [...]} — nothing is saved."""
    today = today or date.today()
    rows = [r for r in read_rows(name, data) if any((c or "").strip() for c in r)]
    if not rows:
        raise ExportError("the file is empty")
    hi = next((i for i, r in enumerate(rows[:10]) if sum(bool(_match([c], DAILY + VIDEO)) for c in r) >= 1
               and any(re.search(r"date|time|link|title|day", (c or "").lower()) for c in r)), 0)
    headers = [c.strip() for c in rows[hi]]
    low = [h.lower() for h in headers]
    link_col = next((i for i, h in enumerate(low) if "link" in h or "url" in h), None)
    video_id_col = next((i for i, h in enumerate(low) if "video id" in h), None)
    is_videos = link_col is not None or video_id_col is not None or any("title" in h for h in low)
    date_col = next((i for i, h in enumerate(low) if ("post" in h and "time" in h) or "posted" in h), None) if is_videos else None
    if date_col is None:
        date_col = next((i for i, h in enumerate(low) if "date" in h or h in ("time", "day")), None)
    title_col = next((i for i, h in enumerate(low) if "title" in h or "description" in h or "caption" in h), None)
    cols = _match([h if i not in (link_col, title_col, date_col, video_id_col) else "" for i, h in enumerate(headers)],
                  VIDEO if is_videos else DAILY)
    if not cols:
        raise ExportError(f"no TikTok metrics recognised in the columns: {', '.join(h for h in headers if h)}")
    out, skipped = [], []
    for n, r in enumerate(rows[hi + 1:], start=hi + 2):
        cell = lambda i: (r[i] if i is not None and i < len(r) else "")  # noqa: E731
        vals = {m: _num(cell(i)) for i, m in cols.items()}
        vals = {m: x for m, x in vals.items() if x is not None}
        if is_videos:
            vid_id = (re.search(r"/video/(\d+)", cell(link_col)) or re.search(r"^(\d{8,})$", cell(video_id_col).strip()) or [None, None])[1]
            if not vid_id:
                skipped.append(f"row {n}: no video link / id")
                continue
            out.append({"video_id": vid_id, "link": cell(link_col), "title": cell(title_col)[:300],
                        "posted": _day(cell(date_col), today), "metrics": vals})
        else:
            d = _day(cell(date_col), today)
            if not d:
                skipped.append(f"row {n}: unreadable date {cell(date_col)!r}")
                continue
            if vals:
                out.append({"day": d, "metrics": vals})
    return {"kind": "videos" if is_videos else "daily", "rows": out,
            "columns": {headers[i]: m for i, m in cols.items()}, "skipped": skipped}


def save(db: DB, account_id: str, parsed: dict, actor: str, today: date | None = None) -> dict:
    acc = db.get("accounts", "account_id", account_id)
    if not acc or acc["platform"] != "tiktok":
        raise ExportError("pick a TikTok account")
    day = (today or date.today()).isoformat()
    rows, posts = [], 0
    for r in parsed["rows"]:
        for m, val in r["metrics"].items():
            if parsed["kind"] == "videos":  # a video's totals as of the export day
                key = f"tiktok:studio:{account_id}:{r['video_id']}:{m}:{day}"
                extra = {"platform_post_id": r["video_id"], "checkpoint": "snapshot", "period_start": r.get("posted"), "period_end": day}
            else:
                key = f"tiktok:studio:{account_id}:{r['day']}:{m}"
                extra = {"platform_post_id": None, "checkpoint": "snapshot", "period_start": r["day"], "period_end": r["day"]}
            rows.append({"account_id": account_id, "variant_id": None, "platform": "tiktok", "metric": m,
                         "canonical_metric": family("tiktok", m), "value": val, "source": SOURCE, "captured_at": now_iso(),
                         "metric_key": key, **extra})
        if parsed["kind"] == "videos":
            pid = f"tiktok:{r['video_id']}"
            old = db.get("posts", "post_id", pid)
            db.insert("posts", {"post_id": pid, "account_id": account_id, "brand_id": acc.get("brand_id"), "platform": "tiktok",
                                "platform_post_id": r["video_id"], "published_at": (old or {}).get("published_at") or r.get("posted"),
                                "permalink": r["link"] or (old or {}).get("permalink"), "post_type": "video",
                                "origin": (old or {}).get("origin") or "tiktok_studio_export",
                                "variant_id": (old or {}).get("variant_id"), "caption": (old or {}).get("caption") or r["title"],
                                "first_seen_at": (old or {}).get("first_seen_at") or now_iso(), "updated_at": now_iso()},
                      upsert_key="post_id")
            posts += 1
    n = db.insert_many("metrics", rows, upsert_key="metric_key") if rows else 0
    db.audit(actor, "tiktok_export_imported", "accounts", account_id,
             {"kind": parsed["kind"], "metrics": n, "posts": posts, "skipped": parsed["skipped"][:20]})
    return {"metrics": n, "posts": posts, "kind": parsed["kind"]}
