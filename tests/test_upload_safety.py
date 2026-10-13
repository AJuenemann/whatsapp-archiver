import os
import tempfile
import unittest

_app_data = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _app_data.name
os.environ["DATABASE_URL"] = f"sqlite:///{_app_data.name}/test.db"
os.environ["INCOMING_DIR"] = f"{_app_data.name}/incoming"
os.environ["MEDIA_DIR"] = f"{_app_data.name}/media"
os.environ["WHATSAPP_BRIDGE_URL"] = "http://127.0.0.1:1"

from whatsapp_archive.app import UploadSizeLimitMiddleware, _media_response_options, app


class UploadSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_declared_oversized_upload_is_rejected_before_parsing(self):
        called_app = False
        sent = []

        async def app(_scope, _receive, _send):
            nonlocal called_app
            called_app = True

        async def receive():
            raise AssertionError("Oversized request body should not be read")

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/imports",
            "headers": [(b"content-length", b"101")],
        }
        await UploadSizeLimitMiddleware(app, limit=100)(scope, receive, send)

        self.assertFalse(called_app)
        self.assertEqual(sent[0]["status"], 413)

    async def test_chunked_oversized_upload_is_rejected(self):
        sent = []
        chunks = iter(
            [
                {"type": "http.request", "body": b"1234", "more_body": True},
                {"type": "http.request", "body": b"5678", "more_body": False},
            ]
        )

        async def receive():
            return next(chunks)

        async def send(message):
            sent.append(message)

        async def app(_scope, limited_receive, _send):
            while (await limited_receive()).get("more_body", False):
                pass

        scope = {"type": "http", "method": "POST", "path": "/imports", "headers": []}
        await UploadSizeLimitMiddleware(app, limit=6)(scope, receive, send)

        self.assertEqual(sent[0]["status"], 413)

    async def test_real_fastapi_stack_rejects_oversized_content_length(self):
        sent = []

        async def receive():
            raise AssertionError("Request should be rejected before reading its body")

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/imports",
            "raw_path": b"/imports",
            "query_string": b"",
            "headers": [(b"host", b"localhost"), (b"content-length", b"525336577")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
            "root_path": "",
        }
        await app(scope, receive, send)

        self.assertEqual(sent[0]["status"], 413)

    async def test_bridge_unavailable_keeps_status_route_usable(self):
        import json

        from whatsapp_archive.app import whatsapp_connection_status

        response = whatsapp_connection_status()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["state"], "bridge_unavailable")

    def test_qr_html_response_is_not_cached(self):
        from whatsapp_archive.app import index

        response = index("", None, "")

        self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_active_media_types_are_never_served_inline(self):
        svg_type, svg_disposition, svg_headers = _media_response_options("payload.svg")
        html_type, html_disposition, _html_headers = _media_response_options("payload.html")
        jpeg_type, jpeg_disposition, jpeg_headers = _media_response_options("photo.jpg")

        self.assertEqual((svg_type, svg_disposition), ("image/svg+xml", "attachment"))
        self.assertEqual((html_type, html_disposition), ("text/html", "attachment"))
        self.assertEqual((jpeg_type, jpeg_disposition), ("image/jpeg", "inline"))
        self.assertEqual(jpeg_headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", jpeg_headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()