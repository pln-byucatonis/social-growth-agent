"""Typed provider adapter interface.

Each adapter builds the exact official-API requests it would send (`plan_requests`). In dry-run those
requests are only previewed (tokens redacted) and a SIMULATED receipt is stored. Live calls go through an
injectable `transport` so tests can exercise failures without network or tokens. There is intentionally
no method for changing usernames, bios or deleting content.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass
class HttpRequest:
    method: str
    url: str
    headers: dict = field(default_factory=dict)
    json_body: dict | None = None
    purpose: str = ""
    form_body: dict | None = None  # application/x-www-form-urlencoded (OAuth token endpoints)

    def redacted(self) -> dict:
        h = {k: ("***REDACTED***" if k.lower() in ("authorization", "access-token") else v) for k, v in self.headers.items()}
        body = dict(self.json_body or {})
        for k in list(body):
            if "token" in k.lower() or "secret" in k.lower():
                body[k] = "***REDACTED***"
        return {"method": self.method, "url": self.url, "headers": h, "json": body, "purpose": self.purpose}


@dataclass
class HttpResponse:
    status: int
    body: dict
    headers: dict = field(default_factory=dict)


class Transport(Protocol):
    def __call__(self, req: HttpRequest) -> HttpResponse: ...


def urllib_transport(req: HttpRequest, timeout: float = 30) -> HttpResponse:
    if req.form_body is not None:
        data = urllib.parse.urlencode(req.form_body).encode()
        ctype = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        data = json.dumps(req.json_body).encode() if req.json_body is not None else None
        ctype = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(req.url, data=data, method=req.method, headers={**req.headers, **ctype})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
            return HttpResponse(resp.status, json.loads(raw), dict(resp.headers))
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw[:500]}
        return HttpResponse(e.code, body, dict(e.headers or {}))


class AdapterError(Exception):
    """kind: retryable | rate_limit | auth | permission | validation | blocked"""

    def __init__(self, kind: str, message: str, http_status: int | None = None, response: dict | None = None):
        super().__init__(message)
        self.kind, self.http_status, self.response = kind, http_status, response or {}

    @property
    def retryable(self) -> bool:
        return self.kind in ("retryable", "rate_limit")


@dataclass
class PublishResult:
    status: str  # published | processing
    platform_post_id: str
    permalink: str = ""
    raw: dict = field(default_factory=dict)


def classify_http(resp: HttpResponse, context: str) -> None:
    if 200 <= resp.status < 300:
        return
    kind = ("rate_limit" if resp.status == 429 else "retryable" if resp.status >= 500 else
            "auth" if resp.status == 401 else "permission" if resp.status == 403 else "validation")
    raise AdapterError(kind, f"{context}: HTTP {resp.status} {json.dumps(resp.body)[:300]}", resp.status, resp.body)


def resolve_token(credential_ref: str) -> str:
    """env:NAME → that env var. vault:<secret name> → the OAuth token record in AWS Secrets Manager written by
    the Connect flow (pln_agent.oauth), refreshed first when it is about to expire."""
    if not credential_ref:
        raise AdapterError("blocked", "no credential reference configured for account")
    scheme, _, name = credential_ref.partition(":")
    if scheme == "env":
        tok = os.environ.get(name)
        if not tok:
            raise AdapterError("auth", f"credential env var {name} is not set")
        return tok
    if scheme == "vault":
        from ..oauth import access_token
        try:
            return access_token(name)
        except LookupError as e:
            raise AdapterError("auth", str(e))
    raise AdapterError("blocked", f"credential scheme {scheme!r} is not supported")


def media_public_url(variant: dict) -> str:
    """The approved render's public HTTPS URL (pull-from-URL APIs: Instagram, Facebook, TikTok). Set by
    pln_agent.public_media.publish_render once the post is approved."""
    url = variant.get("public_media_url") or ""
    if not url:
        raise AdapterError("blocked", "no public link for this render yet: render it, approve it, then create the "
                                      "public link (done automatically when the post is scheduled)")
    return url


class BaseAdapter:
    platform = ""
    official_docs: list[str] = []
    requirements: list[str] = []  # scopes / app review / account type
    app_review_blockers: list[str] = []
    supports_video_publish = True

    def __init__(self, transport: Callable[[HttpRequest], HttpResponse] | None = None, settings=None):
        self.transport = transport or urllib_transport
        self.settings = settings

    def capabilities(self) -> dict:
        return {"platform": self.platform, "video_publish": self.supports_video_publish, "requirements": self.requirements,
                "app_review_blockers": self.app_review_blockers, "docs": self.official_docs,
                "profile_edits": False, "delete": False}

    def preflight(self, variant: dict, account: dict) -> list[str]:
        probs = []
        if not account.get("stable_platform_id"):
            probs.append("no stable platform id (page/org/user id) — complete OAuth first")
        return probs

    def plan_requests(self, variant: dict, account: dict, media_url: str, token: str = "{TOKEN}") -> list[HttpRequest]:
        raise NotImplementedError

    def simulate(self, variant: dict, account: dict, idempotency_key: str) -> PublishResult:
        media_url = "https://media.example.invalid/" + idempotency_key + ".mp4"
        reqs = [r.redacted() for r in self.plan_requests(variant, account, media_url)]
        return PublishResult("simulated", f"SIMULATED-{idempotency_key[:12]}", f"dryrun://{self.platform}/{idempotency_key[:12]}",
                             {"simulated": True, "would_send": reqs, "blocking_notes": self.app_review_blockers})

    def publish(self, variant: dict, account: dict, idempotency_key: str) -> PublishResult:
        raise NotImplementedError

    def status(self, account: dict, platform_post_id: str) -> PublishResult:
        return PublishResult("published", platform_post_id)

    def find_existing(self, account: dict, idempotency_key: str) -> PublishResult | None:
        """Used on crash recovery before any retry. Default: unknown → None (operator confirms manually)."""
        return None

    def rollback_guidance(self, permalink: str) -> str:
        return (f"Manual rollback only: open {permalink or 'the post'} in the native {self.platform} app as an admin, "
                "archive/hide or delete it, then mark the job 'rolled_back' with a note. The agent never deletes content.")

    def _send(self, req: HttpRequest, context: str) -> HttpResponse:
        try:
            resp = self.transport(req)
        except (TimeoutError, ConnectionError, OSError, urllib.error.URLError) as e:
            raise AdapterError("retryable", f"{context}: network error {e}")
        classify_http(resp, context)
        return resp
