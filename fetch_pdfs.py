"""Download PDFs attached directly to the Arena parts listed in pn.json."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


class ArenaError(Exception):
    """An actionable error without credentials or server response bodies."""


class SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https":
            raise ArenaError("Arena redirected to an insecure URL.")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlparse(req.full_url).netloc != urlparse(newurl).netloc:
            redirected.remove_header("Arena_session_id")
        return redirected


class ArenaClient:
    def __init__(self, base_url, email, password, workspace_id=None):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ArenaError("ARENA_BASE_URL must be an HTTPS API URL without credentials or query parameters.")
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.workspace_id = workspace_id
        self.token = None
        self.opener = build_opener(SafeRedirects())

    @contextmanager
    def response(self, method, path, *, params=None, body=None):
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        headers = {"Accept": "application/json", "User-Agent": "arena-data-fetcher/1.0"}
        if self.token:
            headers["arena_session_id"] = self.token
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = Request(url, data=data, headers=headers, method=method)
        for attempt in range(4):
            try:
                response = self.opener.open(request, timeout=60)
                break
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After", "")
                exc.close()
                if method == "GET" and status in (429, 500, 502, 503, 504) and attempt < 3:
                    if retry_after.isdigit() and int(retry_after) > 60:
                        raise ArenaError("Arena rate limit reached; retry later.") from None
                    time.sleep(int(retry_after) if retry_after.isdigit() else 2 ** attempt)
                    continue
                hint = " Check credentials and API permissions." if status in (400, 401, 403) else ""
                raise ArenaError(f"Arena {method} request failed (HTTP {status}).{hint}") from None
            except (URLError, TimeoutError, OSError):
                raise ArenaError("Could not connect to Arena; check network access and ARENA_BASE_URL.") from None
        try:
            yield response
        except (HTTPException, TimeoutError):
            raise ArenaError("Arena response was interrupted; retry this download.") from None
        finally:
            response.close()

    def json(self, method, path, **kwargs):
        with self.response(method, path, **kwargs) as response:
            try:
                return json.load(response)
            except (ValueError, UnicodeError):
                raise ArenaError("Arena returned an invalid JSON response.") from None

    def login(self):
        body = {"email": self.email, "password": self.password}
        if self.workspace_id:
            try:
                body["workspaceId"] = int(self.workspace_id)
            except ValueError:
                raise ArenaError("ARENA_WORKSPACE_ID must be an integer.") from None
        result = self.json("POST", "/login", body=body)
        self.token = result.get("arena_session_id") or result.get("arenaSessionId")
        if not self.token:
            raise ArenaError("Arena login did not return a session token.")

    def logout(self):
        if self.token:
            try:
                with self.response("PUT", "/logout"):
                    pass
            finally:
                self.token = None

    def pages(self, path, **params):
        offset, limit = 0, 400
        while True:
            page = self.json("GET", path, params={**params, "offset": offset, "limit": limit})
            results = page.get("results")
            if not isinstance(results, list):
                raise ArenaError("Arena returned an unexpected collection response.")
            yield from results
            if len(results) < limit:
                break
            offset += len(results)

    def find_part(self, number):
        # Arena searches may interpret wildcards; only accept literal full matches.
        matches = {item["guid"]: item for item in self.pages("/items", number=number)
                   if item.get("number") == number}
        if len(matches) > 1:
            raise ArenaError("Multiple Arena items have this exact number; refusing an ambiguous match.")
        return next(iter(matches.values()), None)

    def attachments(self, item_guid):
        return self.pages(f"/items/{quote(item_guid, safe='')}/files")

    def download(self, item_guid, association_guid, destination):
        path = f"/items/{quote(item_guid, safe='')}/files/{quote(association_guid, safe='')}/content"
        temporary = None
        try:
            with self.response("GET", path) as response:
                first = response.read(1024)
                if b"%PDF-" not in first:
                    raise ArenaError("Downloaded content is not a PDF; file was not saved.")
                with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(first)
                    received = len(first)
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        received += len(chunk)
                    expected = getattr(response, "headers", {}).get("Content-Length")
                    if expected is not None and received != int(expected):
                        raise ArenaError("Incomplete PDF download; file was not saved.")
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def load_parts(path):
    with path.open(encoding="utf-8-sig") as source:
        parts = json.load(source)
    if not isinstance(parts, list) or not parts or any(not isinstance(p, str) or not p.strip() for p in parts):
        raise ArenaError("Part-number JSON must be a nonempty array of nonempty strings.")
    # Preserve exact numbers, including spaces, and first appearance order.
    return list(dict.fromkeys(parts)), len(parts)


def safe_component(value, limit=100):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(". ") or "unnamed"
    if len(cleaned) > limit:
        cleaned = cleaned[:limit - 9].rstrip(". ") + "_" + hashlib.sha256(value.encode()).hexdigest()[:8]
    return cleaned


def pdf_metadata(association):
    file = association.get("file") or {}
    name = file.get("title") or file.get("name") or ""
    format_ = str(file.get("format", "")).lower().lstrip(".")
    is_pdf = format_ in ("pdf", "application/pdf") or any(
        str(file.get(key, "")).lower().endswith(".pdf") for key in ("title", "name", "location")
    )
    if not is_pdf:
        return None
    if not name:
        name = str(file.get("location", "")).replace("\\", "/").rsplit("/", 1)[-1] or "Document"
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name


def prepare_output(path):
    if path.is_file():
        if path.stat().st_size:
            raise ArenaError(f"Output path {path} is a nonempty file; choose a folder with --output.")
        backup = path.with_name(path.name + ".empty-file")
        if backup.exists():
            raise ArenaError(f"Cannot preserve the empty output file: {backup} already exists.")
        path.rename(backup)
    path.mkdir(parents=True, exist_ok=True)


def fetch_parts(client, parts, output, *, dry_run=False):
    report = []
    used_names = set()
    for index, number in enumerate(parts, 1):
        entry = {"part_number": number, "status": "ok", "files": []}
        report.append(entry)
        print(f"[{index}/{len(parts)}] {number}", flush=True)
        try:
            item = client.find_part(number)
            if item is None:
                entry["status"] = "not_found"
                print("  Part not found.")
                continue
            entry["item_guid"] = item["guid"]
            pdfs = {}
            for association in client.attachments(item["guid"]):
                name = pdf_metadata(association)
                if name is not None:
                    pdfs[association["guid"]] = (name, association)
            # Stable sequence when the server changes its result ordering.
            ordered = sorted(pdfs.values(), key=lambda pair: (pair[0].casefold(), pair[1]["guid"]))
            if not ordered:
                entry["status"] = "no_pdfs"
                print("  No attached PDFs.")
            for sequence, (name, association) in enumerate(ordered, 1):
                filename = f"{safe_component(number, 70)}_{sequence:02d}_{safe_component(name, 95)}.pdf"
                # Protect parts that differ only by case or illegal filesystem characters.
                if filename.casefold() in used_names:
                    digest = hashlib.sha256(number.encode()).hexdigest()[:8]
                    filename = f"{safe_component(number, 60)}_{digest}_{sequence:02d}_{safe_component(name, 95)}.pdf"
                used_names.add(filename.casefold())
                file_entry = {"association_guid": association["guid"], "document_name": name,
                              "filename": filename, "status": "planned"}
                entry["files"].append(file_entry)
                if dry_run:
                    print(f"  Would download {filename}")
                    continue
                try:
                    client.download(item["guid"], association["guid"], output / filename)
                    file_entry["status"] = "downloaded"
                    print(f"  Saved {filename}")
                except (ArenaError, OSError) as exc:
                    entry["status"] = file_entry["status"] = "error"
                    file_entry["error"] = str(exc)
                    print(f"  Failed {filename}: {exc}", file=sys.stderr)
        except (ArenaError, OSError) as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
            print(f"  Failed: {exc}", file=sys.stderr)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=Path, default=ROOT / "pn.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--report", type=Path, default=ROOT / "download-report.json")
    parser.add_argument("--dry-run", action="store_true", help="List PDFs via Arena without downloading them.")
    parser.add_argument("--limit", type=int, help="Process only the first N unique parts.")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    try:
        parts, count = load_parts(args.parts)
        if args.limit:
            parts = parts[:args.limit]
        load_dotenv(args.env_file)
        email = os.getenv("ARENA_EMAIL")
        password = os.getenv("ARENA_PASSWORD")
        if not email or not password:
            raise ArenaError("Set ARENA_EMAIL and ARENA_PASSWORD in .env (see .env.example).")
        client = ArenaClient(os.getenv("ARENA_API_BASE_URL") or os.getenv("ARENA_BASE_URL")
                             or "https://api.arenasolutions.com/v1",
                             email, password, os.getenv("ARENA_WORKSPACE_ID"))
        if not args.dry_run:
            prepare_output(args.output)
        print(f"Read {count} entries; processing {len(parts)} unique parts.")
        client.login()
        try:
            results = fetch_parts(client, parts, args.output, dry_run=args.dry_run)
        finally:
            try:
                client.logout()
            except (ArenaError, OSError) as exc:
                print(f"Warning: logout failed: {exc}", file=sys.stderr)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(),
                                          "dry_run": args.dry_run, "parts": results}, indent=2) + "\n", encoding="utf-8")
        downloaded = sum(f["status"] == "downloaded" for p in results for f in p["files"])
        issues = sum(p["status"] in ("error", "not_found") for p in results)
        print(f"Downloaded {downloaded} PDFs. Parts missing or with errors: {issues}. Report: {args.report}")
        return 1 if issues else 0
    except (ArenaError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
