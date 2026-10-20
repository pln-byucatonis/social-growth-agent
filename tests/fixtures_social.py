"""Offline fixture shaped exactly like the ops portal's social tables (social_account_metric_daily, social_post,
social_post_metric_daily). Synthetic numbers with KNOWN effects so tests can check the analysis finds them:

* CarJitsu IG: each extra post in a week adds ~40 net followers; submission clips earn ~3x the shares of
  announcements; captions ending in a question earn more comments.
* PLN IG: extra posts add no followers, and engagement per 1k views drops in busy weeks.
* Impressions are 0 on every row (Instagram stopped reporting them) — must be treated as "not reported".
"""
import json
import random
from datetime import date, datetime, timedelta, timezone

DDL = """
CREATE TABLE social_account (id integer PRIMARY KEY, brand text, platform text, platform_account_id text,
  handle text, display_name text, status text);
CREATE TABLE social_account_metric_daily (account_id integer, metric_date date, followers bigint, reach bigint,
  profile_views bigint, impressions bigint, engagements bigint, platform_extra text, source text, collected_at timestamp);
CREATE TABLE social_post (id integer PRIMARY KEY, account_id integer, platform_post_id text, published_at timestamp,
  text text, permalink text, post_type text, media text, origin text);
CREATE TABLE social_post_metric_daily (post_id integer, metric_date date, impressions bigint, engagements bigint,
  reactions bigint, comments bigint, shares bigint, link_clicks bigint, views bigint, platform_extra text,
  collected_at timestamp)
"""
START = date(2026, 6, 1)
WEEKS = 10
CJ_POSTS = [3, 8, 4, 9, 5, 10, 3, 7, 6, 8]   # posts per week (varied on purpose)
PLN_POSTS = [4, 9, 5, 10, 4, 8, 5, 9, 6, 10]
ACCOUNTS = {6: ("carjitsu", "@carjitsu_championship", "carjitsu.instagram.carjitsu_championship", 320000),
            15: ("pro-league-network", "@proleaguenetwork", "pln.instagram.proleaguenetwork", 64000)}


def build(ops, agent_db):
    for stmt in [s for s in DDL.split(";") if s.strip()]:
        ops.execute(stmt)
    rng = random.Random(42)
    for oid, (brand, handle, agent_id, base) in ACCOUNTS.items():
        ops.execute("INSERT INTO social_account VALUES (?,?,?,?,?,?,?)",
                    [oid, brand, "instagram", f"1784{oid:011d}", handle, handle, "active"])
        agent_db.update("accounts", "account_id", agent_id, {"ops_account_id": oid})
    post_id = 1
    for oid, (brand, handle, agent_id, base) in ACCOUNTS.items():
        weekly = CJ_POSTS if oid == 6 else PLN_POSTS
        followers = base
        for w in range(WEEKS):
            n = weekly[w]
            gain = (40 * n + rng.randint(-25, 25)) if oid == 6 else (60 + rng.randint(-25, 25))
            for d in range(7):
                day = START + timedelta(days=7 * w + d)
                followers += gain / 7
                ops.execute("INSERT INTO social_account_metric_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
                            [oid, day.isoformat(), int(followers), 20000 + rng.randint(0, 5000), 300 + rng.randint(0, 80),
                             0, 900, json.dumps({"username": handle.lstrip("@")}), "platform",
                             f"{day.isoformat()} 10:00:00"])
            for i in range(n):
                day = START + timedelta(days=7 * w + (i % 7))
                hour = 23 if i % 2 else 16  # UTC → 7pm / 12pm New York
                pub = datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc)
                kind = ["submission", "announcement"][i % 2] if oid == 6 else ["highlight", "announcement"][i % 2]
                question = (i % 3 == 0)
                text = {"submission": "Back-seat reversal into a submission. Tap or nap",
                        "announcement": "Tickets on sale now for the fall card",
                        "highlight": "Wild moment from the weekend"}[kind] + ("? Who wins this?" if question else ".")
                ops.execute("INSERT INTO social_post VALUES (?,?,?,?,?,?,?,?,?)",
                            [post_id, oid, f"18{post_id:06d}", pub.strftime("%Y-%m-%d %H:%M:%S"), text,
                             f"https://www.instagram.com/reel/X{post_id}/", "reels", "{}", "ingested"])
                views = 20000 + rng.randint(-3000, 3000)
                shares = int(views * (0.009 if kind == "submission" else 0.003) * rng.uniform(0.85, 1.15))
                comments = int(views * (0.004 if question else 0.0015) * rng.uniform(0.85, 1.15))
                eng_rate = 0.04 if oid == 6 else (0.05 if n <= 6 else 0.03)
                for age in (4, 8):
                    cap = pub + timedelta(days=age)
                    f = 0.8 if age == 4 else 1.0
                    ops.execute("INSERT INTO social_post_metric_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                [post_id, cap.date().isoformat(), 0, int(views * eng_rate * f), int(views * 0.03 * f),
                                 int(comments * f), int(shares * f), 0, int(views * f),
                                 json.dumps({"reach": int(views * 0.8 * f), "saved": int(views * 0.002 * f), "lifetime": 1}),
                                 cap.strftime("%Y-%m-%d %H:%M:%S")])
                post_id += 1
    return post_id - 1
