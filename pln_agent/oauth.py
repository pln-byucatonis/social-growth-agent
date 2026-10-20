"""Connect real accounts: per-platform OAuth (authorization-code flow) run from the dashboard.

Flow
  1. An admin saves the platform developer app's client id + secret (Accounts → Platform apps). They go
     straight to AWS Secrets Manager (`pln/growth-agent/oauth/<provider>`) and are never shown again.
  2. "Connect" on an account → /oauth/start/<provider>?account=<id> → the platform's consent screen
     (top-level window; platforms refuse to render inside iframes).
  3. The platform redirects to /oauth/callback/<provider>. We exchange the code, ask the platform which
     identities the login controls (Pages + Instagram business accounts, YouTube channel, TikTok/X user,
     LinkedIn org admin roles), and bind each one to the agent account with the matching handle.
  4. Tokens go to Secrets Manager (`pln/growth-agent/tokens/<account_id>`); the database stores only the
     reference `vault:<name>` and the stable platform id, and the account becomes `oauth_connected`.

Connecting never authorizes publishing. That stays a separate, audited step per account, behind the
global dry-run / publishing switches.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass

from .accounts import connect_credentials
from .adapters.base import HttpRequest, urllib_transport
from .db import DB, now_iso

APP_SECRET = "pln/growth-agent/oauth/{provider}"
TOKEN_SECRET = "pln/growth-agent/tokens/{name}"
# The long-lived Meta *user* token, shared with the ops portal's Instagram/Facebook analytics ingest
# (ops reads it via META_TOKEN_SECRET), so one Meta sign-in reconnects publishing and analytics.
META_USER_SECRET = "pln/growth-agent/tokens/meta-user"
STATE_TTL_S = 900
LINKEDIN_ORG_ID = os.environ.get("PLN_LINKEDIN_ORG_ID", "91515598")
META_VERSION = os.environ.get("META_GRAPH_VERSION", "v26.0")


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    platforms: tuple[str, ...]
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    scope_sep: str = " "
    pkce: bool = False
    client_id_param: str = "client_id"
    console: str = ""
    review_note: str = ""


PROVIDERS: dict[str, Provider] = {p.key: p for p in [
    Provider("meta", "Meta (Facebook Pages + Instagram)", ("facebook", "instagram"),
             f"https://www.facebook.com/{META_VERSION}/dialog/oauth",
             f"https://graph.facebook.com/{META_VERSION}/oauth/access_token",
             ("pages_show_list", "pages_read_engagement", "pages_manage_posts", "read_insights", "business_management",
              "instagram_basic", "instagram_content_publish", "instagram_manage_insights"), scope_sep=",",
             console="https://developers.facebook.com/apps",
             review_note="Admins/testers of the app can connect right away; Meta App Review (Advanced Access) "
                         "is needed before the app works for anyone else."),
    Provider("google", "Google (YouTube)", ("youtube",),
             "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token",
             ("https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly",
              "https://www.googleapis.com/auth/yt-analytics.readonly"), pkce=True,
             console="https://console.cloud.google.com/apis/credentials",
             review_note="Enable YouTube Data API v3 + YouTube Analytics API. Uploads stay private until the API "
                         "project passes Google's audit. Pick the brand channel on Google's account chooser."),
    Provider("tiktok", "TikTok", ("tiktok",),
             "https://www.tiktok.com/v2/auth/authorize/", "https://open.tiktokapis.com/v2/oauth/token/",
             ("user.info.basic", "user.info.profile", "video.publish", "video.upload", "video.list"), scope_sep=",",
             client_id_param="client_key", console="https://developers.tiktok.com/apps",
             review_note="Add Login Kit + Content Posting API. Until TikTok audits the app, posts can only be "
                         "SELF_ONLY (private). Verify media.proleaguenetwork.com for pull-from-URL."),
    Provider("x", "X", ("x",), "https://x.com/i/oauth2/authorize", "https://api.x.com/2/oauth2/token",
             ("tweet.read", "tweet.write", "users.read", "media.write", "offline.access"), pkce=True,
             console="https://developer.x.com/en/portal/dashboard",
             review_note="OAuth 2.0, type 'Web App' (confidential client). Posting volume needs a paid API tier."),
    Provider("linkedin", "LinkedIn (company Page)", ("linkedin",),
             "https://www.linkedin.com/oauth/v2/authorization", "https://www.linkedin.com/oauth/v2/accessToken",
             ("w_organization_social", "r_organization_social", "rw_organization_admin"),
             console="https://www.linkedin.com/developers/apps",
             review_note=f"Request the Community Management API product. The person connecting must be an "
                         f"ADMINISTRATOR or CONTENT_ADMINISTRATOR of company {LINKEDIN_ORG_ID}."),
]}
PLATFORM_PROVIDER = {pl: p.key for p in PROVIDERS.values() for pl in p.platforms}


# ---- secret store -------------------------------------------------------------------------------

class AwsSecretStore:
    def __init__(self):
        import boto3
        self.c = boto3.client("secretsmanager", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    def get(self, name: str) -> dict | None:
        try:
            return json.loads(self.c.get_secret_value(SecretId=name)["SecretString"])
        except self.c.exceptions.ResourceNotFoundException:
            return None

    def put(self, name: str, value: dict) -> None:
        body = json.dumps(value)
        try:
            self.c.put_secret_value(SecretId=name, SecretString=body)
        except self.c.exceptions.ResourceNotFoundException:
            self.c.create_secret(Name=name, SecretString=body, Tags=[{"Key": "app", "Value": "pln-social-agent"}])


class MemorySecretStore:
    def __init__(self):
        self.data: dict[str, dict] = {}

    def get(self, name):
        v = self.data.get(name)
        return json.loads(json.dumps(v)) if v is not None else None

    def put(self, name, value):
        self.data[name] = json.loads(json.dumps(value))


_store = None
_transport = None


def store():
    global _store
    if _store is None:
        _store = MemorySecretStore() if os.environ.get("PLN_SECRET_STORE") == "memory" else AwsSecretStore()
    return _store


def set_store(s) -> None:
    global _store
    _store = s


def set_transport(t) -> None:
    """Tests inject a fake platform here."""
    global _transport
    _transport = t


def _http(req: HttpRequest):
    return (_transport or urllib_transport)(req)


# ---- app credentials ----------------------------------------------------------------------------

def save_app(provider: str, client_id: str, client_secret: str, db: DB, actor: str) -> None:
    if provider not in PROVIDERS:
        raise KeyError(provider)
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id or not client_secret:
        raise ValueError("client id and client secret are both required")
    store().put(APP_SECRET.format(provider=provider), {"client_id": client_id, "client_secret": client_secret})
    db.audit(actor, "oauth_app_saved", "oauth", provider, {"client_id_suffix": client_id[-4:]})  # never the secret


def save_meta_config(config_id: str, db: DB, actor: str) -> None:
    """Facebook Login for Business: the app's login *configuration* (assets + permissions chosen in the
    Meta dashboard). When set, the consent URL sends config_id instead of a scope list."""
    config_id = config_id.strip()
    if config_id and not re.fullmatch(r"\d{5,25}", config_id):
        raise ValueError("a Meta configuration ID is a number (Facebook Login for Business → Configurations)")
    app = store().get(APP_SECRET.format(provider="meta"))
    if not app:
        raise ValueError("save the Meta app id and secret first")
    app["config_id"] = config_id
    store().put(APP_SECRET.format(provider="meta"), app)
    db.audit(actor, "oauth_meta_config_saved", "oauth", "meta", {"config_id": config_id})


def app_status(provider: str) -> dict:
    app = store().get(APP_SECRET.format(provider=provider)) or {}
    cid = app.get("client_id", "")
    return {"configured": bool(cid and app.get("client_secret")), "client_id_hint": ("…" + cid[-4:]) if cid else "",
            "config_id": app.get("config_id", "")}


def _app(provider: str) -> dict:
    app = store().get(APP_SECRET.format(provider=provider))
    if not app:
        raise LookupError(f"{PROVIDERS[provider].label}: save the developer app's client id and secret first")
    return app


def _share_meta_login(me: dict, body: dict) -> None:
    """Different people manage different Pages, so the shared secret keeps one long-lived token per
    Facebook login (keyed by user id); the ops ingest unions the Pages every login can see. A repeat
    sign-in by the same person replaces only their entry. `access_token` mirrors the newest login for
    older readers."""
    shared = store().get(META_USER_SECRET) or {}
    logins = dict(shared.get("tokens") or {})
    rec = _tok_record("meta", body, user_id=me.get("id", ""), user_name=me.get("name", ""))
    logins[me.get("id") or f"login-{len(logins) + 1}"] = rec
    store().put(META_USER_SECRET, {"tokens": logins, "access_token": rec["access_token"],
                                   "expires_at": rec["expires_at"], "shared_with": "ops social ingest"})


def meta_user_token_status() -> dict:
    """For the Accounts page: which Meta logins are shared with analytics, and when each expires."""
    shared = store().get(META_USER_SECRET) or {}
    logins = shared.get("tokens") or ({"legacy": shared} if shared.get("access_token") else {})
    if not logins:
        return {"connected": False, "logins": []}
    out = []
    for uid, rec in logins.items():
        exp = rec.get("expires_at") or 0
        days = round((exp - time.time()) / 86400, 1) if exp else None
        out.append({"user_id": uid, "name": rec.get("user_name") or uid, "obtained_at": rec.get("obtained_at", ""),
                    "days_left": days, "expiring": days is not None and days < 10, "expired": days is not None and days <= 0})
    soonest = min((x["days_left"] for x in out if x["days_left"] is not None), default=None)
    return {"connected": True, "logins": out, "days_left": soonest,
            "expiring": any(x["expiring"] for x in out)}


def redirect_uri(provider: str) -> str:
    base = os.environ.get("PLN_PUBLIC_URL", "https://ops.proleaguenetwork.com/api/social-agent").rstrip("/")
    return f"{base}/oauth/callback/{provider}"


# ---- start / callback ---------------------------------------------------------------------------

def start(db: DB, provider: str, account_id: str, actor: str) -> str:
    """Returns the platform consent URL. The state nonce (and PKCE verifier) lives server-side, unlogged."""
    p = PROVIDERS[provider]
    acc = db.get("accounts", "account_id", account_id) if account_id else None
    if account_id and (not acc or PLATFORM_PROVIDER.get(acc["platform"]) != provider):
        raise ValueError(f"{account_id} is not a {p.label} account")
    app = _app(provider)
    nonce = secrets.token_urlsafe(24)
    st = {"provider": provider, "account_id": account_id, "actor": actor, "created": time.time()}
    q = {p.client_id_param: app["client_id"], "redirect_uri": redirect_uri(provider), "response_type": "code",
         "state": nonce, "scope": p.scope_sep.join(p.scopes)}
    if p.pkce:
        verifier = secrets.token_urlsafe(48)
        st["verifier"] = verifier
        q["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        q["code_challenge_method"] = "S256"
    if provider == "meta" and app.get("config_id"):  # Facebook Login for Business
        q.pop("scope")
        q.update({"config_id": app["config_id"], "override_default_response_type": "true"})
    if provider == "google":
        q.update({"access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "true"})
    db.insert("settings_kv", {"key": f"oauth_state:{nonce}", "value": json.dumps(st), "updated_at": now_iso(),
                              "updated_by": actor}, upsert_key="key")
    db.audit(actor, "oauth_started", "accounts", account_id or provider, {"provider": provider})
    return p.authorize_url + "?" + urllib.parse.urlencode(q)


def _take_state(db: DB, provider: str, nonce: str) -> dict:
    key = f"oauth_state:{nonce}"
    raw = db.kv_get(key) if nonce and re.match(r"^[A-Za-z0-9_-]{20,64}$", nonce) else None
    if not raw:
        raise PermissionError("unknown or already-used sign-in attempt; start again from Accounts")
    db.execute("DELETE FROM settings_kv WHERE key=?", [key])  # single use
    st = json.loads(raw)
    if st["provider"] != provider or time.time() - st["created"] > STATE_TTL_S:
        raise PermissionError("sign-in attempt expired or mismatched; start again from Accounts")
    return st


def _token_request(p: Provider, app: dict, form: dict) -> dict:
    headers = {"Accept": "application/json"}
    form = dict(form)
    if p.key == "x":  # confidential client: HTTP Basic
        headers["Authorization"] = "Basic " + base64.b64encode(f"{app['client_id']}:{app['client_secret']}".encode()).decode()
        form["client_id"] = app["client_id"]
    else:
        form[p.client_id_param] = app["client_id"]
        form["client_secret"] = app["client_secret"]
    if p.key == "meta":  # Graph takes GET with query params
        resp = _http(HttpRequest("GET", p.token_url + "?" + urllib.parse.urlencode(form), headers, purpose="token"))
    else:
        resp = _http(HttpRequest("POST", p.token_url, headers, form_body=form, purpose="token"))
    body = resp.body or {}
    if resp.status >= 300 or "access_token" not in body:
        err = body.get("error_description") or body.get("error") or body.get("message") or resp.status
        raise PermissionError(f"{p.label}: token exchange failed ({err})")
    return body


def _tok_record(provider: str, body: dict, **extra) -> dict:
    exp = body.get("expires_in")
    return {"provider": provider, "access_token": body["access_token"], "refresh_token": body.get("refresh_token", ""),
            "expires_at": int(time.time() + int(exp)) if exp else 0, "scope": body.get("scope", ""),
            "obtained_at": now_iso(), **extra}


def _get(url: str, token: str, headers: dict | None = None) -> dict:
    resp = _http(HttpRequest("GET", url, {"Authorization": f"Bearer {token}", **(headers or {})}, purpose="identity"))
    if resp.status >= 300:
        raise PermissionError(f"identity lookup failed: HTTP {resp.status} {json.dumps(resp.body)[:200]}")
    return resp.body or {}


def discover(provider: str, body: dict, app: dict) -> list[dict]:
    """Identities this login controls: [{platform, platform_id, handle, name, token_record}]."""
    tok = body["access_token"]
    out = []
    if provider == "meta":
        long = _token_request(PROVIDERS["meta"], app, {"grant_type": "fb_exchange_token", "fb_exchange_token": tok})
        user_tok = long["access_token"]
        me = _get(f"https://graph.facebook.com/{META_VERSION}/me?fields=id,name", user_tok)
        _share_meta_login(me, long)
        pages = _get(f"https://graph.facebook.com/{META_VERSION}/me/accounts?limit=100&fields="
                     "id,name,username,access_token,instagram_business_account{id,username}", user_tok)
        for pg in pages.get("data", []):
            rec = _tok_record("meta", {"access_token": pg["access_token"]}, page_id=pg["id"])  # page tokens don't expire
            out.append({"platform": "facebook", "platform_id": pg["id"], "handle": pg.get("username") or "",
                        "name": pg.get("name", ""), "token": rec})
            ig = pg.get("instagram_business_account")
            if ig:
                out.append({"platform": "instagram", "platform_id": ig["id"], "handle": ig.get("username", ""),
                            "name": pg.get("name", ""), "token": {**rec, "ig_user_id": ig["id"]}})
    elif provider == "google":
        rec = _tok_record("google", body)
        ch = _get("https://www.googleapis.com/youtube/v3/channels?part=id,snippet&mine=true", tok)
        for c in ch.get("items", []):
            out.append({"platform": "youtube", "platform_id": c["id"], "handle": (c["snippet"].get("customUrl") or ""),
                        "name": c["snippet"].get("title", ""), "token": {**rec, "channel_id": c["id"]}})
    elif provider == "tiktok":
        rec = _tok_record("tiktok", body, open_id=body.get("open_id", ""))
        u = _get("https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name,username", tok)
        u = (u.get("data") or {}).get("user") or {}
        out.append({"platform": "tiktok", "platform_id": u.get("open_id") or body.get("open_id", ""),
                    "handle": u.get("username", ""), "name": u.get("display_name", ""), "token": rec})
    elif provider == "x":
        rec = _tok_record("x", body)
        u = _get("https://api.x.com/2/users/me", tok).get("data") or {}
        out.append({"platform": "x", "platform_id": u.get("id", ""), "handle": u.get("username", ""),
                    "name": u.get("name", ""), "token": rec})
    elif provider == "linkedin":
        rec = _tok_record("linkedin", body)
        acl = _get("https://api.linkedin.com/rest/organizationAcls?q=roleAssignee&state=APPROVED", tok,
                   {"Linkedin-Version": os.environ.get("LINKEDIN_VERSION", "202601"), "X-Restli-Protocol-Version": "2.0.0"})
        for e in acl.get("elements", []):
            org = str(e.get("organization", "")).rsplit(":", 1)[-1]
            if e.get("role") in ("ADMINISTRATOR", "CONTENT_ADMINISTRATOR"):
                out.append({"platform": "linkedin", "platform_id": org, "handle": org, "name": f"organization {org}",
                            "token": {**rec, "org_id": org}})
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def match_account(ident: dict, accounts: list[dict]) -> dict | None:
    same = [a for a in accounts if a["platform"] == ident["platform"]]
    for a in same:
        if a.get("stable_platform_id") and a["stable_platform_id"] == ident["platform_id"]:
            return a
    if ident["platform"] == "linkedin":
        return next((a for a in same if ident["platform_id"] == LINKEDIN_ORG_ID), None)
    keys = {_norm(ident["handle"]), _norm(ident["name"])} - {""}
    for a in same:
        if {_norm(a.get("username_original", "")), _norm(a.get("username_normalized", ""))} & keys:
            return a
    return None


def _bind(db: DB, acc: dict, ident: dict, actor: str) -> None:
    name = TOKEN_SECRET.format(name=acc["account_id"])
    store().put(name, {**ident["token"], "platform": ident["platform"], "platform_id": ident["platform_id"],
                       "handle": ident["handle"]})
    if acc["verification_status"] == "unverified":
        db.update("accounts", "account_id", acc["account_id"], {"verification_status": "ownership_confirmed",
                                                                "ownership_owner": f"OAuth login by {actor}"})
    connect_credentials(db, acc["account_id"], f"vault:{name}", ident["platform_id"], actor)


def callback(db: DB, provider: str, params: dict, actor: str) -> dict:
    if params.get("error"):
        raise PermissionError(f"{PROVIDERS[provider].label} sign-in was cancelled or refused: "
                              f"{params.get('error_description') or params['error']}")
    st = _take_state(db, provider, params.get("state", ""))
    p, app = PROVIDERS[provider], _app(provider)
    form = {"grant_type": "authorization_code", "code": params.get("code", ""), "redirect_uri": redirect_uri(provider)}
    if p.pkce:
        form["code_verifier"] = st["verifier"]
    body = _token_request(p, app, form)
    idents = discover(provider, body, app)
    accounts = db.all("accounts")
    connected, unmatched = [], []
    for ident in idents:
        acc = match_account(ident, accounts)
        if acc is None and st.get("account_id") and len(idents) == 1:
            acc = db.get("accounts", "account_id", st["account_id"])  # single identity: the account you clicked
        if acc is None:
            unmatched.append({k: ident[k] for k in ("platform", "platform_id", "handle", "name")})
            continue
        _bind(db, acc, ident, actor)
        connected.append({"account_id": acc["account_id"], "platform": ident["platform"], "handle": ident["handle"],
                          "platform_id": ident["platform_id"]})
    db.audit(actor, "oauth_completed", "accounts", st.get("account_id") or provider,
             {"provider": provider, "connected": connected, "unmatched": unmatched})
    return {"provider": provider, "connected": connected, "unmatched": unmatched,
            "requested": st.get("account_id", "")}


# ---- token use ----------------------------------------------------------------------------------

def access_token(secret_name: str) -> str:
    """Current access token for a connected account, refreshing it when it expires within 5 minutes."""
    rec = store().get(secret_name)
    if not rec:
        raise LookupError(f"no token stored at {secret_name}: reconnect the account")
    if rec.get("expires_at") and rec["expires_at"] - time.time() < 300:
        if not rec.get("refresh_token"):
            raise LookupError(f"{rec.get('provider')} token expired and has no refresh token: reconnect the account")
        p = PROVIDERS[rec["provider"]]
        body = _token_request(p, _app(p.key), {"grant_type": "refresh_token", "refresh_token": rec["refresh_token"]})
        rec.update({"access_token": body["access_token"], "refresh_token": body.get("refresh_token") or rec["refresh_token"],
                    "expires_at": int(time.time() + int(body.get("expires_in") or 3600)), "refreshed_at": now_iso()})
        store().put(secret_name, rec)
    return rec["access_token"]
