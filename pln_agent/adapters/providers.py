"""Official-API adapters. Request shapes follow each platform's public docs as of Oct 2026 and MUST be re-verified
against current docs + a sandbox/test account before the first live call. None has been exercised against a
live API from this repository (no credentials were provided).
"""
from __future__ import annotations

import os
import time

from .base import AdapterError, BaseAdapter, HttpRequest, PublishResult, media_public_url, resolve_token


def _caption(v: dict) -> str:
    tags = " ".join(v.get("hashtags") or [])
    return (v["caption"] + ("\n\n" + tags if tags and tags not in v["caption"] else "")).strip()


class LinkedInAdapter(BaseAdapter):
    platform = "linkedin"
    official_docs = ["https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api",
                     "https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api",
                     "https://learn.microsoft.com/linkedin/marketing/versioning"]
    requirements = ["LinkedIn developer app with Community Management API product approved",
                    "OAuth scope w_organization_social (+ r_organization_social for analytics)",
                    "Authorizing member holds ADMINISTRATOR or CONTENT_ADMIN role on the PLN company Page",
                    "Linkedin-Version header pinned (LINKEDIN_VERSION, YYYYMM) and re-tested before sunset"]
    app_review_blockers = ["No PLN company Page ID supplied yet", "Community Management API access requires LinkedIn approval"]

    def plan_requests(self, v, account, media_url, token="{TOKEN}"):
        ver = getattr(self.settings, "linkedin_version", "202601")
        org = f"urn:li:organization:{account.get('stable_platform_id') or '{ORG_ID}'}"
        h = {"Authorization": f"Bearer {token}", "Linkedin-Version": ver, "X-Restli-Protocol-Version": "2.0.0"}
        return [
            HttpRequest("POST", "https://api.linkedin.com/rest/videos?action=initializeUpload", h,
                        {"initializeUploadRequest": {"owner": org, "fileSizeBytes": "{FILE_SIZE}", "uploadCaptions": False,
                                                     "uploadThumbnail": False}}, "initialize video upload"),
            HttpRequest("PUT", "{uploadInstructions[].uploadUrl}", {}, None, "upload video parts (binary)"),
            HttpRequest("POST", "https://api.linkedin.com/rest/videos?action=finalizeUpload", h,
                        {"finalizeUploadRequest": {"video": "{VIDEO_URN}", "uploadToken": "", "uploadedPartIds": ["{ETAGS}"]}},
                        "finalize video upload"),
            HttpRequest("POST", "https://api.linkedin.com/rest/posts", h,
                        {"author": org, "commentary": _caption(v), "visibility": "PUBLIC",
                         "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
                         "content": {"media": {"title": (v.get("hook") or "")[:200], "id": "{VIDEO_URN}"}},
                         "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}, "create post"),
        ]

    def publish(self, v, account, idempotency_key):
        token = resolve_token(account["credential_ref"])
        reqs = self.plan_requests(v, account, "", token)
        # Binary multi-part upload of the rendered file is wired via the same transport in production;
        # here we require a pre-uploaded video URN to keep the live path explicit and reviewable.
        video_urn = v.get("platform_media_id")
        if not video_urn:
            raise AdapterError("blocked", "LinkedIn video must be uploaded via Videos API first (initialize→PUT parts→finalize); "
                                          "binary upload step requires the rendered file + approved app")
        post = reqs[-1]
        post.json_body["content"]["media"]["id"] = video_urn
        resp = self._send(post, "linkedin create post")
        pid = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id") or resp.body.get("id", "")
        return PublishResult("published", pid, f"https://www.linkedin.com/feed/update/{pid}", resp.body)


class FacebookPageAdapter(BaseAdapter):
    platform = "facebook"
    official_docs = ["https://developers.facebook.com/docs/video-api/guides/publishing",
                     "https://developers.facebook.com/docs/pages-api/posts"]
    requirements = ["Meta app in Live mode; Business verification", "Page access token for the PLN Page",
                    "Permissions pages_manage_posts, pages_read_engagement, pages_show_list (App Review: Advanced Access)",
                    "Confirm '@Pro League Network' resolves to the intended managed Page ID"]
    app_review_blockers = ["Advanced Access for pages_manage_posts requires Meta App Review"]

    def plan_requests(self, v, account, media_url, token="{PAGE_TOKEN}"):
        ver = getattr(self.settings, "meta_graph_version", "v26.0")
        page = account.get("stable_platform_id") or "{PAGE_ID}"
        return [HttpRequest("POST", f"https://graph-video.facebook.com/{ver}/{page}/videos", {},
                            {"file_url": media_url, "description": _caption(v), "title": v.get("title") or "",
                             "published": True, "access_token": token}, "publish page video")]

    def publish(self, v, account, idempotency_key):
        token = resolve_token(account["credential_ref"])
        req = self.plan_requests(v, account, media_public_url(v), token)[0]
        resp = self._send(req, "facebook page video")
        vid = resp.body.get("id", "")
        return PublishResult("processing", vid, f"https://www.facebook.com/{vid}", resp.body)


class InstagramAdapter(BaseAdapter):
    platform = "instagram"
    official_docs = ["https://developers.facebook.com/docs/instagram-platform/content-publishing",
                     "https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media"]
    requirements = ["Instagram Professional (Business/Creator) account", "Instagram API with Facebook Login (Page-linked) or Instagram Login",
                    "Permissions instagram_basic + instagram_content_publish (or instagram_business_content_publish)",
                    "Media at a public HTTPS URL; respect content_publishing_limit (rolling 24h)"]
    app_review_blockers = ["instagram_content_publish requires Meta App Review + Business verification"]
    poll_seconds = 5
    poll_max = 24

    def _host(self):
        return os.environ.get("IG_GRAPH_HOST", "https://graph.facebook.com")

    def plan_requests(self, v, account, media_url, token="{TOKEN}"):
        ver = getattr(self.settings, "meta_graph_version", "v26.0")
        ig = account.get("stable_platform_id") or "{IG_USER_ID}"
        return [
            HttpRequest("POST", f"{self._host()}/{ver}/{ig}/media", {},
                        {"media_type": "REELS", "video_url": media_url, "caption": _caption(v), "share_to_feed": True,
                         "access_token": token}, "create reel container"),
            HttpRequest("GET", f"{self._host()}/{ver}/{{CONTAINER_ID}}?fields=status_code", {}, None, "poll container"),
            HttpRequest("POST", f"{self._host()}/{ver}/{ig}/media_publish", {},
                        {"creation_id": "{CONTAINER_ID}", "access_token": token}, "publish container"),
        ]

    def publish(self, v, account, idempotency_key):
        token = resolve_token(account["credential_ref"])
        ver = getattr(self.settings, "meta_graph_version", "v26.0")
        create, _, pub = self.plan_requests(v, account, media_public_url(v), token)
        cid = self._send(create, "instagram create container").body["id"]
        for _ in range(self.poll_max):
            st = self._send(HttpRequest("GET", f"{self._host()}/{ver}/{cid}?fields=status_code&access_token={token}"),
                            "instagram poll").body.get("status_code")
            if st == "FINISHED":
                break
            if st == "ERROR":
                raise AdapterError("validation", "instagram container processing ERROR")
            time.sleep(self.poll_seconds)
        else:
            raise AdapterError("retryable", "instagram container not ready in time")
        pub.json_body["creation_id"] = cid
        mid = self._send(pub, "instagram media_publish").body["id"]
        perm = self._send(HttpRequest("GET", f"{self._host()}/{ver}/{mid}?fields=permalink&access_token={token}"),
                          "instagram permalink").body.get("permalink", "")
        return PublishResult("published", mid, perm, {"container": cid})


class TikTokAdapter(BaseAdapter):
    platform = "tiktok"
    official_docs = ["https://developers.tiktok.com/docs/en/content-posting-api-get-started",
                     "https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post",
                     "https://developers.tiktok.com/docs/en/content-sharing-guidelines"]
    requirements = ["TikTok developer app with Content Posting API + Direct Post enabled", "OAuth scope video.publish (user consent)",
                    "Query creator_info before every post; privacy_level must be one of returned options",
                    "PULL_FROM_URL requires verified URL-prefix/domain ownership (else FILE_UPLOAD)",
                    "Disclose branded content (brand_content_toggle) and AI-generated content (is_aigc)"]
    app_review_blockers = ["UNAUDITED clients can only post SELF_ONLY (private). Public posting requires TikTok's audit."]

    def plan_requests(self, v, account, media_url, token="{TOKEN}"):
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        flags = (v.get("risk_flags") or {}).get("review_flags", [])
        audited = os.environ.get("TIKTOK_CLIENT_AUDITED", "false").lower() == "true"
        return [
            HttpRequest("POST", "https://open.tiktokapis.com/v2/post/publish/creator_info/query/", h, {}, "creator info"),
            HttpRequest("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/", h, {
                "post_info": {"title": _caption(v)[:2200], "privacy_level": "PUBLIC_TO_EVERYONE" if audited else "SELF_ONLY",
                              "disable_duet": False, "disable_comment": False, "disable_stitch": False,
                              "video_cover_timestamp_ms": int(1000 * (v.get("thumbnail") or {}).get("source_time", 0) -
                                                              1000 * ((v.get("edl") or {}).get("segments") or [{"source_in": 0}])[0]["source_in"]),
                              "brand_content_toggle": "paid_partnership" in flags, "brand_organic_toggle": False,
                              "is_aigc": "ai_generated_person" in flags},
                "source_info": {"source": "PULL_FROM_URL", "video_url": media_url}}, "init direct post"),
            HttpRequest("POST", "https://open.tiktokapis.com/v2/post/publish/status/fetch/", h, {"publish_id": "{PUBLISH_ID}"}, "status"),
        ]

    def publish(self, v, account, idempotency_key):
        token = resolve_token(account["credential_ref"])
        info, init, _ = self.plan_requests(v, account, media_public_url(v), token)
        opts = self._send(info, "tiktok creator_info").body.get("data", {}).get("privacy_level_options", [])
        want = init.json_body["post_info"]["privacy_level"]
        if want not in opts:
            raise AdapterError("validation", f"privacy_level {want} not offered by creator_info ({opts})")
        resp = self._send(init, "tiktok init")
        pid = resp.body.get("data", {}).get("publish_id", "")
        return PublishResult("processing", pid, "", resp.body)

    def status(self, account, platform_post_id):
        token = resolve_token(account["credential_ref"])
        req = self.plan_requests({"caption": "", "hashtags": []}, account, "", token)[2]
        req.json_body = {"publish_id": platform_post_id}
        d = self._send(req, "tiktok status").body.get("data", {})
        st = d.get("status", "")
        return PublishResult("published" if st == "PUBLISH_COMPLETE" else "processing", platform_post_id, "", d)


class YouTubeAdapter(BaseAdapter):
    platform = "youtube"
    official_docs = ["https://developers.google.com/youtube/v3/docs/videos/insert",
                     "https://developers.google.com/youtube/v3/guides/uploading_a_video"]
    requirements = ["Google Cloud project with YouTube Data API v3", "OAuth scope https://www.googleapis.com/auth/youtube.upload",
                    "Quota: videos.insert is expensive; request quota increase", "Altered/synthetic content disclosure where applicable"]
    app_review_blockers = ["Uploads from unverified API projects are locked to private until the project passes Google's audit"]

    def plan_requests(self, v, account, media_url, token="{TOKEN}"):
        return [HttpRequest("POST", "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                            {"Authorization": f"Bearer {token}"},
                            {"snippet": {"title": (v.get("title") or v["hook"])[:100], "description": _caption(v),
                                         "categoryId": "17"},
                             "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                                        "containsSyntheticMedia": "ai_generated_person" in (v.get("risk_flags") or {}).get("review_flags", [])}},
                            "start resumable upload"),
                HttpRequest("PUT", "{Location header}", {}, None, "upload bytes")]

    def publish(self, v, account, idempotency_key):
        raise AdapterError("blocked", "YouTube resumable byte upload not enabled until OAuth project is verified; use dry-run preview")


class XAdapter(BaseAdapter):
    platform = "x"
    official_docs = ["https://docs.x.com/x-api/posts/create-post", "https://docs.x.com/x-api/media/upload-media"]
    requirements = ["X developer project on a paid tier that permits posting volume", "OAuth 2.0 scopes tweet.write users.read media.write offline.access"]
    app_review_blockers = ["API access tier/cost must be approved by PLN"]

    def plan_requests(self, v, account, media_url, token="{TOKEN}"):
        h = {"Authorization": f"Bearer {token}"}
        return [HttpRequest("POST", "https://api.x.com/2/media/upload", h, {"media_category": "tweet_video"}, "chunked media upload (INIT/APPEND/FINALIZE)"),
                HttpRequest("POST", "https://api.x.com/2/tweets", h, {"text": v["caption"][:280], "media": {"media_ids": ["{MEDIA_ID}"]}}, "create post")]

    def publish(self, v, account, idempotency_key):
        token = resolve_token(account["credential_ref"])
        req = self.plan_requests(v, account, "", token)[1]
        if v.get("platform_media_id"):
            req.json_body["media"]["media_ids"] = [v["platform_media_id"]]
        else:
            req.json_body.pop("media")
        d = self._send(req, "x create post").body.get("data", {})
        handle = account["username_normalized"]
        return PublishResult("published", d.get("id", ""), f"https://x.com/{handle}/status/{d.get('id', '')}", d)


class TwitchAdapter(BaseAdapter):
    platform = "twitch"
    supports_video_publish = False
    official_docs = ["https://dev.twitch.tv/docs/api/reference/#create-channel-stream-schedule-segment"]
    requirements = ["Twitch app; OAuth scope channel:manage:schedule", "Streaming rights for any match footage"]
    app_review_blockers = ["Twitch has no general VOD/clip upload API: schedule segments only; live requires a host"]

    def plan_requests(self, v, account, media_url, token="{TOKEN}"):
        return [HttpRequest("POST", f"https://api.twitch.tv/helix/schedule/segment?broadcaster_id={account.get('stable_platform_id') or '{ID}'}",
                            {"Authorization": f"Bearer {token}", "Client-Id": "{CLIENT_ID}"},
                            {"start_time": "{RFC3339}", "timezone": "America/New_York", "duration": "120", "title": v["caption"][:140]},
                            "create schedule segment")]

    def publish(self, v, account, idempotency_key):
        raise AdapterError("blocked", "Twitch: schedule segments only; enable after channel OAuth")


class SchedulingProviderAdapter(BaseAdapter):
    """Fallback for an authorized third-party scheduler (configure SCHEDULER_API_BASE). Disabled by default."""
    platform = "scheduler"
    requirements = ["Signed agreement + API key with an authorized scheduling partner", "Each PLN account connected in that provider by an admin"]
    app_review_blockers = ["No scheduling provider configured"]

    def plan_requests(self, v, account, media_url, token="{TOKEN}"):
        base = os.environ.get("SCHEDULER_API_BASE", "https://scheduler.example.invalid")
        return [HttpRequest("POST", f"{base}/posts", {"Authorization": f"Bearer {token}"},
                            {"profile": account["account_id"], "text": _caption(v), "media_url": media_url}, "create scheduled post")]

    def publish(self, v, account, idempotency_key):
        raise AdapterError("blocked", "scheduling provider not configured")


ADAPTERS = {a.platform: a for a in (LinkedInAdapter, FacebookPageAdapter, InstagramAdapter, TikTokAdapter,
                                    YouTubeAdapter, XAdapter, TwitchAdapter)}


def get_adapter(platform: str, transport=None, settings=None) -> BaseAdapter:
    return ADAPTERS[platform](transport=transport, settings=settings)
