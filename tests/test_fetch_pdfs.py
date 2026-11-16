import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from fetch_pdfs import (
    ArenaClient, ArenaError, SafeRedirects, fetch_parts, load_parts,
    pdf_metadata, prepare_output,
)


class DownloaderTests(unittest.TestCase):
    def test_input_deduplicates_without_changing_part_numbers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "parts.json"
            path.write_text(json.dumps(["ABC", "NEWA CRT   28*28*21", "ABC"]), encoding="utf-8-sig")
            self.assertEqual(load_parts(path), (["ABC", "NEWA CRT   28*28*21"], 3))
            path.write_text('[123]', encoding="utf-8")
            with self.assertRaises(ArenaError):
                load_parts(path)

    def test_complete_run_handles_pagination_matching_and_downloads(self):
        client = ArenaClient("https://example.com/v1", "email", "secret", "123")
        files = [
            {"guid": "z", "file": {"title": "Z Drawing", "format": "pdf"}},
            {"guid": "a", "file": {"title": "A: Drawing.pdf", "format": "pdf"}},
            {"guid": "txt", "file": {"title": "Notes.txt", "format": "txt"}},
        ]

        def open_request(request, timeout):
            url = request.full_url
            if request.method == "POST":
                self.assertEqual(json.loads(request.data)["workspaceId"], 123)
                return io.BytesIO(b'{"arena_session_id":"token"}')
            self.assertEqual(request.get_header("Arena_session_id"), "token")
            if request.method == "PUT":
                self.assertTrue(url.endswith("/logout"))
                return io.BytesIO(b"")
            if "/content" in url:
                return io.BytesIO(b"%PDF-1.7\nexample")
            if "/files?" in url:
                return io.BytesIO(json.dumps({"results": files}).encode())
            if "number=MISSING" in url:
                return io.BytesIO(b'{"results":[]}')
            # A broad search result must not be accepted as an exact match.
            return io.BytesIO(b'{"results":[{"guid":"wrong","number":"PART-extra"},'
                              b'{"guid":"item","number":"PART"}]}')

        client.opener.open = Mock(side_effect=open_request)
        with tempfile.TemporaryDirectory() as folder:
            client.login()
            report = fetch_parts(client, ["PART", "MISSING"], Path(folder))
            client.logout()
            self.assertEqual(report[0]["status"], "ok")
            self.assertEqual(report[1]["status"], "not_found")
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()),
                             ["PART_01_A_ Drawing.pdf", "PART_02_Z Drawing.pdf"])
            self.assertTrue(all(p.read_bytes().startswith(b"%PDF-") for p in Path(folder).iterdir()))
            self.assertIsNone(client.token)

    def test_pagination_does_not_truncate_after_first_page(self):
        client = ArenaClient("https://example.com/v1", "email", "secret")
        first = [{"guid": str(i)} for i in range(400)]
        client.json = Mock(side_effect=[{"results": first}, {"results": [{"guid": "last"}]}])
        self.assertEqual(len(list(client.pages("/items"))), 401)
        self.assertEqual(client.json.call_args.kwargs["params"]["offset"], 400)

    def test_login_accepts_current_camel_case_session_field(self):
        client = ArenaClient("https://example.com/v1", "email", "secret")
        client.json = Mock(return_value={"arenaSessionId": "token"})
        client.login()
        self.assertEqual(client.token, "token")

    def test_bad_download_preserves_existing_file_and_cleans_temporary_files(self):
        client = ArenaClient("https://example.com/v1", "email", "secret")
        client.opener.open = Mock(return_value=io.BytesIO(b"<html>login page</html>"))
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "existing.pdf"
            destination.write_bytes(b"%PDF-old")
            with self.assertRaises(ArenaError):
                client.download("item", "file", destination)
            self.assertEqual(destination.read_bytes(), b"%PDF-old")
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_file_failure_does_not_stop_other_pdfs_or_parts(self):
        client = Mock()
        client.find_part.side_effect = [{"guid": "one"}, {"guid": "two"}]
        client.attachments.return_value = [
            {"guid": "a", "file": {"title": "a.pdf"}},
            {"guid": "b", "file": {"title": "b.pdf"}},
        ]
        client.download.side_effect = [ArenaError("unavailable"), None, None, None]
        result = fetch_parts(client, ["ONE", "TWO"], Path("unused"))
        self.assertEqual([r["status"] for r in result], ["error", "ok"])
        self.assertEqual(client.download.call_count, 4)

    def test_truncated_pdf_does_not_replace_existing_download(self):
        client = ArenaClient("https://example.com/v1", "email", "secret")
        response = io.BytesIO(b"%PDF-partial")
        response.headers = {"Content-Length": "5000"}
        client.opener.open = Mock(return_value=response)
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "existing.pdf"
            destination.write_bytes(b"%PDF-old")
            with self.assertRaises(ArenaError):
                client.download("item", "file", destination)
            self.assertEqual(destination.read_bytes(), b"%PDF-old")
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_dry_run_does_not_download_or_create_output(self):
        client = Mock()
        client.find_part.return_value = {"guid": "item"}
        client.attachments.return_value = [{"guid": "a", "file": {"title": "a.pdf"}}]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "absent"
            result = fetch_parts(client, ["PART"], output, dry_run=True)
            client.download.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(result[0]["files"][0]["status"], "planned")

    def test_filename_collisions_across_parts_are_disambiguated(self):
        client = Mock()
        client.find_part.return_value = {"guid": "item"}
        client.attachments.return_value = [{"guid": "a", "file": {"title": "doc.pdf"}}]
        result = fetch_parts(client, ["a*b", "a?b", "A*B"], Path("unused"), dry_run=True)
        names = [r["files"][0]["filename"].casefold() for r in result]
        self.assertEqual(len(set(names)), 3)

    def test_empty_placeholder_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "data"
            output.touch()
            prepare_output(output)
            self.assertTrue(output.is_dir())
            self.assertTrue(output.with_name("data.empty-file").is_file())

    def test_redirect_does_not_leak_session_to_other_hosts(self):
        request = Request("https://arena.example/content", headers={"arena_session_id": "secret"})
        redirected = SafeRedirects().redirect_request(request, None, 302, "Found", {}, "https://storage.example/file")
        self.assertIsNone(redirected.get_header("Arena_session_id"))
        with self.assertRaises(ArenaError):
            SafeRedirects().redirect_request(request, None, 302, "Found", {}, "http://storage.example/file")

    def test_pdf_identification_uses_format_or_filename(self):
        self.assertEqual(pdf_metadata({"file": {"title": "Drawing", "format": "PDF"}}), "Drawing")
        self.assertEqual(pdf_metadata({"file": {"name": "Drawing.PDF"}}), "Drawing")
        self.assertIsNone(pdf_metadata({"file": {"title": "PDF instructions.txt", "format": "txt"}}))


if __name__ == "__main__":
    unittest.main()
