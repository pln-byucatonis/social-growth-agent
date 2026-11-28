"""YouTube Shorts upload: announce the video, then send the file to the address YouTube gives back."""
import os
import tempfile
import unittest

from pln_agent.adapters import get_adapter
from pln_agent.adapters.base import AdapterError, HttpResponse


class YouTubeUploadTests(unittest.TestCase):
    def setUp(self):
        os.environ["YT_TEST_TOKEN"] = "tok"
        self.addCleanup(os.environ.pop, "YT_TEST_TOKEN")
        f = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        f.write(b"\x00" * 1234)
        f.close()
        self.addCleanup(os.unlink, f.name)
        self.v = {"variant_id": "a:yt", "hook": "Flipped from the bottom", "title": "", "caption": "He was stuck.",
                  "hashtags": ["#CarJitsu"], "render_path": f.name, "risk_flags": {}}
        self.acc = {"credential_ref": "env:YT_TEST_TOKEN", "username_normalized": "carjitsu"}

    def test_two_step_upload_private_until_audited(self):
        calls = []

        def transport(req):
            calls.append(req)
            if req.method == "POST":
                return HttpResponse(200, {}, {"Location": "https://upload.example/session1"})
            return HttpResponse(200, {"id": "abc123", "status": {"privacyStatus": "private"}})

        r = get_adapter("youtube", transport=transport).publish(self.v, self.acc, "k")
        self.assertEqual((r.platform_post_id, r.permalink), ("abc123", "https://www.youtube.com/shorts/abc123"))
        init, put = calls
        self.assertEqual(init.json_body["status"]["privacyStatus"], "private")  # unaudited project: private
        self.assertEqual(init.headers["X-Upload-Content-Length"], "1234")
        self.assertEqual(init.json_body["snippet"]["title"], "Flipped from the bottom")
        self.assertEqual((put.method, put.url, len(put.data)), ("PUT", "https://upload.example/session1", 1234))
        self.assertEqual(put.redacted()["bytes"], 1234)

    def test_public_once_audited(self):
        os.environ["YOUTUBE_API_AUDITED"] = "true"
        self.addCleanup(os.environ.pop, "YOUTUBE_API_AUDITED")
        req = get_adapter("youtube").plan_requests(self.v, self.acc, "", "t")[0]
        self.assertEqual(req.json_body["status"]["privacyStatus"], "public")

    def test_no_upload_address_or_render(self):
        ad = get_adapter("youtube", transport=lambda req: HttpResponse(200, {}, {}))
        with self.assertRaises(AdapterError):
            ad.publish(self.v, self.acc, "k")
        with self.assertRaises(AdapterError):
            ad.publish({**self.v, "render_path": ""}, self.acc, "k")


if __name__ == "__main__":
    unittest.main()
