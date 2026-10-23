#!/usr/bin/env python3
"""deed.page REST API CLI for the Muse deed-page connector.

Auth: loads the per-user `custom.deed-page` credential (a deed.page owner
token) as a surrogate through Muse's bundled dynamic_credentials helper. The
real token never touches this script: the runtime swaps the surrogate on
approved egress, only to deed.page.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

HELPER_PATH = "/opt/hatch/skills/skill-creator/bin"
CREDENTIAL_NAME = "custom.deed-page"
ALLOWED_HOSTS = ("deed.page",)
API_BASE = "https://deed.page/v1"
MAX_BODY = 4 * 1024 * 1024  # deed.page per-request body cap
CONNECT_GUIDANCE = (
    "not connected: collect the user's deed.page owner token through the "
    "secure credential flow (credentials.request_api_access) as "
    "`custom.deed-page`, then retry. See https://deed.page/auth.md"
)
SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
VERSION_RE = re.compile(r"v[0-9]{1,9}")

try:
    sys.path.insert(0, HELPER_PATH)
    from dynamic_credentials import (
        DynamicCredentialError,
        add_surrogate_to_request,
        read_json_response,
    )
except ImportError:
    sys.exit(
        "error: this CLI needs the Muse dynamic_credentials helper at "
        f"{HELPER_PATH} (install this skill on a Muse runtime to use it)"
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so credentials never travel to another destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def slug(value: str) -> str:
    if not SLUG_RE.fullmatch(value):
        raise ValueError("invalid slug: use lowercase letters, digits and hyphens (1-63 chars)")
    return value


def version(value: str) -> str:
    if not VERSION_RE.fullmatch(value):
        raise ValueError("invalid version: use the form v3")
    return value


def call(
    method: str,
    path: str,
    body: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict | None = None,
) -> dict:
    if body is not None and len(body) > MAX_BODY:
        raise ValueError(
            "request exceeds deed.page's 4 MB per-request cap; publish fewer or smaller files"
        )
    headers = {"Accept": "application/json", "X-Client": "Muse"}
    if content_type:
        headers["Content-Type"] = content_type
    headers.update(extra_headers or {})
    req = urllib.request.Request(API_BASE + path, data=body, method=method, headers=headers)
    try:
        add_surrogate_to_request(req, CREDENTIAL_NAME, allowed_hosts=ALLOWED_HOSTS)
    except DynamicCredentialError as exc:
        detail = str(exc).lower()
        if "missing" in detail or "surrogate" in detail:
            sys.exit(CONNECT_GUIDANCE)
        sys.exit(f"error: credential problem: {exc}")
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as response:
            return read_json_response(response)
    except urllib.error.HTTPError as exc:
        messages = {
            400: "request rejected; check the slug and input",
            401: "token missing, revoked or invalid; reconnect deed.page through Muse's secure credential flow",
            403: "this token does not own that site, or the account is suspended",
            404: "site or version not found; check the identifier",
            409: "version conflict or slug taken; re-read the site before retrying",
            413: "upload too large for the plan or the 4 MB request cap",
            422: "content rejected by deed.page upload screening",
            429: "rate limited; wait and check the write outcome before retrying",
        }
        message = messages.get(exc.code, "request failed; check the current state before retrying a write")
        result: dict = {"error": message, "status": exc.code}
        try:
            code = json.loads(exc.read().decode("utf-8")).get("code")
            if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code):
                result["code"] = code
        except (ValueError, AttributeError, OSError):
            pass
        retry_after = exc.headers.get("Retry-After", "")
        if retry_after.isdigit():
            result["retry_after_seconds"] = int(retry_after)
        sys.exit(json.dumps(result))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        sys.exit(json.dumps({"error": "no valid response received; check the current state before retrying a write"}))


def require_confirmation(args: argparse.Namespace) -> None:
    if not args.confirm:
        raise ValueError("write not confirmed; show the exact change to the user and rerun with --confirm")


def html_body(filename: str) -> bytes:
    data = Path(filename).read_bytes()
    if not data.strip():
        raise ValueError("input file is empty")
    return data


def folder_tarball(dirname: str) -> bytes:
    root = Path(dirname)
    if not root.is_dir():
        raise ValueError("--dir must be an existing folder")
    if not (root / "index.html").is_file():
        raise ValueError("the folder needs an index.html at its root")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"symlinks are not published: {path.relative_to(root)}")
            if path.is_file():
                tar.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    return buffer.getvalue()


def redact_sensitive(value):
    if isinstance(value, dict):
        return {k: redact_sensitive(v) for k, v in value.items() if k not in {"token", "secret"}}
    if isinstance(value, list):
        return [redact_sensitive(v) for v in value]
    return value


def execute(args: argparse.Namespace):
    if args.command == "auth":
        result = call("GET", "/whoami")
        if not result.get("authenticated"):
            sys.exit(CONNECT_GUIDANCE)
        plan = result.get("plan") or {}
        return {"ok": True, "plan": plan.get("id") if isinstance(plan, dict) else plan}
    if args.command == "list":
        return call("GET", "/sites")
    path = "/sites/" + slug(args.slug)
    if args.command == "get":
        return call("GET", path)
    if args.command == "versions":
        return call("GET", path + "/versions")
    if args.command == "analytics":
        query = {k: v for k, v in (("from", args.from_date), ("to", args.to_date)) if v}
        return call("GET", path + "/analytics" + ("?" + urllib.parse.urlencode(query) if query else ""))

    require_confirmation(args)
    headers = {}
    if getattr(args, "expected_version", None):
        headers["If-Match"] = '"' + version(args.expected_version) + '"'
    if args.command == "publish":
        if bool(args.file) == bool(args.dir):
            raise ValueError("pass exactly one of --file or --dir")
        headers["Idempotency-Key"] = str(uuid.uuid4())
        if args.file:
            return call("PUT", path, html_body(args.file), "text/html; charset=utf-8", headers)
        return call("PUT", path, folder_tarball(args.dir), "application/gzip", headers)
    if args.command == "restore":
        return call("POST", path + "/versions/" + version(args.version) + "/restore", b"", None, headers)
    return call("DELETE", path)


def build_parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("auth", help="verify the credential and show the plan")
    commands.add_parser("list", help="list sites owned by the token")
    for name in ("get", "versions"):
        commands.add_parser(name).add_argument("slug")
    analytics = commands.add_parser("analytics", help="cookieless view counts for a site you own")
    analytics.add_argument("slug")
    analytics.add_argument("--from", dest="from_date")
    analytics.add_argument("--to", dest="to_date")

    publish = commands.add_parser("publish", help="create or replace a site")
    publish.add_argument("slug")
    publish.add_argument("--file", help="one HTML file, stored as index.html")
    publish.add_argument("--dir", help="a folder with index.html at its root")
    publish.add_argument("--expected-version", help="fail with 409 unless the live version matches, e.g. v3")
    publish.add_argument("--confirm", action="store_true")

    restore = commands.add_parser("restore", help="restore a retained version as a new version")
    restore.add_argument("slug")
    restore.add_argument("version")
    restore.add_argument("--expected-version")
    restore.add_argument("--confirm", action="store_true")

    delete = commands.add_parser("delete", help="delete a site you own")
    delete.add_argument("slug")
    delete.add_argument("--confirm", action="store_true")
    return result


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(json.dumps(redact_sensitive(execute(args)), ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
    except OSError:
        print(json.dumps({"error": "could not read the input file or folder"}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
