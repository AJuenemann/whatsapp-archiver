import os
import tempfile
import unittest

_app_data = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _app_data.name
os.environ["DATABASE_URL"] = f"sqlite:///{_app_data.name}/test.db"
os.environ["MEDIA_DIR"] = f"{_app_data.name}/media"
os.environ["WHATSAPP_BRIDGE_URL"] = "http://127.0.0.1:1"

from whatsapp_archive.app import _media_response_options, app


class MediaSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_unavailable_keeps_status_route_usable(self):
        import json

        from whatsapp_archive.app import whatsapp_connection_status

        response = whatsapp_connection_status()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["state"], "bridge_unavailable")

    async def test_untrusted_host_is_rejected(self):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [(b"host", b"untrusted.example")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
            "root_path": "",
        }

        await app(scope, receive, send)

        self.assertEqual(sent[0]["status"], 400)

    def test_qr_html_response_is_not_cached(self):
        from whatsapp_archive.app import index

        response = index("", None)

        self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_active_media_types_are_never_served_inline(self):
        svg_type, svg_disposition, _svg_headers = _media_response_options("payload.svg")
        html_type, html_disposition, _html_headers = _media_response_options("payload.html")
        jpeg_type, jpeg_disposition, jpeg_headers = _media_response_options("photo.jpg")

        self.assertEqual((svg_type, svg_disposition), ("image/svg+xml", "attachment"))
        self.assertEqual((html_type, html_disposition), ("text/html", "attachment"))
        self.assertEqual((jpeg_type, jpeg_disposition), ("image/jpeg", "inline"))
        self.assertEqual(jpeg_headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", jpeg_headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()