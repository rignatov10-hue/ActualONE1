"""Data sources without any intermediary: Google Drive via its own API, plain
https URLs, or local files.

Drive access modes:
- public files: downloaded immediately with anonymous requests;
- private files: set up your own Google Cloud OAuth client once with
  ``python -m standalone.oauth_setup``; the token then lives in
  ``google_token.json`` and refreshes itself.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

from .tools import Dataset, ToolError, extract_drive_id, parse_tabular, utc_now

OAUTH_TOKEN_PATH = Path(os.environ.get("GOOGLE_OAUTH_TOKEN", "google_token.json"))
_MAX_BYTES = 150 * 1024 * 1024
_TIMEOUT = 120

_GOOGLE_SCOPES = "https://www.googleapis.com/auth/drive.readonly"


def _http_get(url: str, headers: dict[str, str] | None = None, max_bytes: int = _MAX_BYTES) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            disposition = response.headers.get("Content-Disposition", "") or ""
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(500).decode("utf-8", "replace")
        except Exception:
            pass
        raise ToolError(
            f"The server answered {exc.code} for the download ({body[:200]})."
        ) from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"Could not reach {url}: {exc.reason}.") from exc
    if len(raw) > max_bytes:
        raise ToolError(
            f"The file is larger than the {max_bytes // (1024 * 1024)} MB this analyst supports."
        )
    return raw, {"content_type": content_type, "disposition": disposition}


def _title_from_disposition(disposition: str, fallback: str) -> str:
    for part in disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename") and "=" in part:
            name = part.split("=", 1)[1].strip().strip('"')
            if name:
                return name
    return fallback


def _oauth_token() -> str:
    if not OAUTH_TOKEN_PATH.exists():
        raise ToolError(
            "This file is private to the user and no Google OAuth token is configured. "
            "Either share the file as 'anyone with the link', or run "
            "`python -m standalone.oauth_setup` once to link your own Google account. "
            "No other AI service or cloud is involved either way."
        )
    try:
        data = json.loads(OAUTH_TOKEN_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"Could not read {OAUTH_TOKEN_PATH}: {exc}.") from exc
    expires_at = float(data.get("expires_at", 0))
    if expires_at and expires_at - 60 < time.time() and data.get("refresh_token"):
        data = _refresh_google_token(data, OAUTH_TOKEN_PATH)
    token = data.get("access_token")
    if not token:
        raise ToolError(
            f"{OAUTH_TOKEN_PATH} holds no access_token. Run `python -m standalone.oauth_setup` again."
        )
    return str(token)


def _refresh_google_token(data: dict, path: Path) -> dict:
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or data.get("client_id")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or data.get("client_secret")
    refresh = data.get("refresh_token")
    if not (client_id and client_secret and refresh):
        raise ToolError("The Google token cannot be refreshed; run `python -m standalone.oauth_setup` again.")
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
    ).encode()
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            fresh = json.loads(response.read())
    except urllib.error.URLError as exc:
        raise ToolError(f"Google token refresh failed: {exc}.") from exc
    if "access_token" not in fresh:
        raise ToolError(f"Google token refresh failed: {fresh.get('error', fresh)}.")
    data["access_token"] = fresh["access_token"]
    data["expires_at"] = time.time() + float(fresh.get("expires_in", 3600))
    try:
        path.write_text(json.dumps(data))
    except OSError:
        pass
    return data


def _metadata(file_id: str) -> dict:
    url = (
        "https://www.googleapis.com/drive/v3/files/"
        + urllib.parse.quote(file_id)
        + "?fields=name,mimeType,size&supportsAllDrives=true"
    )
    raw, _ = _http_get(url, headers={"Authorization": f"Bearer {_oauth_token()}"})
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError("Google Drive answered with unreadable metadata.") from exc


def _drive_public_url(file_id: str, export_csv: bool) -> str:
    if export_csv:
        return f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=csv"
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def _sheet_export_url(file_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=csv"


def _download_drive_file(file_id: str) -> tuple[bytes, str, str]:
    """Download a Drive file. Public files work anonymously; private ones need OAuth."""
    try:
        return _download_drive_file_oauth(file_id)
    except ToolError as exc:
        if "is private" not in str(exc) and "token" not in str(exc).lower():
            raise
    raw, headers = _http_get(_drive_public_url(file_id, export_csv=False))
    if "text/html" not in headers["content_type"]:
        title = _title_from_disposition(headers["disposition"], file_id)
        return raw, title, "text/csv"
    csv_raw, csv_headers = _http_get(_drive_public_url(file_id, export_csv=True))
    if "text/html" not in csv_headers["content_type"]:
        title = _title_from_disposition(csv_headers["disposition"], file_id + ".csv")
        return csv_raw, title, "text/csv"
    raise ToolError(
        "The Drive file is not publicly downloadable and no Google OAuth token is set up. "
        "Either share the file as 'anyone with the link', or run "
        "`python -m standalone.oauth_setup` to link your own Google account once."
    )


def _download_drive_file_oauth(file_id: str) -> tuple[bytes, str, str]:
    token = _oauth_token()
    auth = {"Authorization": f"Bearer {token}"}
    meta = _metadata(file_id)
    title = str(meta.get("name") or file_id)
    mime = str(meta.get("mimeType") or "")
    if mime == "application/vnd.google-apps.spreadsheet":
        raw, _ = _http_get(_sheet_export_url(file_id), headers=auth)
        return raw, title, "text/csv"
    if mime in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
    }:
        raw, _ = _http_get(
            "https://www.googleapis.com/drive/v3/files/" + urllib.parse.quote(file_id) + "?alt=media&supportsAllDrives=true",
            headers=auth,
        )
        return raw, title, "xlsx"
    if mime in {"text/csv", "application/vnd.ms-excel", "text/plain", "application/octet-stream", ""}:
        raw, _ = _http_get(
            "https://www.googleapis.com/drive/v3/files/" + urllib.parse.quote(file_id) + "?alt=media&supportsAllDrives=true",
            headers=auth,
        )
        return raw, title, "text/csv"
    raise ToolError(
        f"'{title}' has MIME type '{mime}'. This analyst reads CSV files, Excel/ODS workbooks "
        "and Google Sheets."
    )


def fetch_dataset(source: str) -> tuple[Dataset, Any]:
    source = str(source).strip()
    lowered = source.lower()
    title = source
    source_mime = "text/csv"

    try:
        if "://" not in lowered:
            path = Path(source).expanduser()
            if path.exists():
                raw = path.read_bytes()
                if len(raw) > _MAX_BYTES:
                    raise ToolError(
                        f"The file is larger than the {_MAX_BYTES // (1024 * 1024)} MB this analyst supports."
                    )
                title = path.name
                source_mime = "xlsx" if path.suffix.lower() in {".xlsx", ".ods"} else "text/csv"
            else:
                raise ToolError(f"Local file '{source}' does not exist.")
        elif "drive.google.com" in lowered or "docs.google.com" in lowered:
            file_id = extract_drive_id(source)
            raw, title, source_mime = _download_drive_file(file_id)
        elif lowered.startswith(("http://", "https://")):
            raw, headers = _http_get(source)
            title = _title_from_disposition(headers["disposition"], source.rsplit("/", 1)[-1] or source)
            source_mime = "xlsx" if "spreadsheet" in headers["content_type"] or title.lower().endswith((".xlsx", ".ods")) else "text/csv"
        else:
            raise ToolError("Pass a Google Drive link, an https URL, or a local file path.")
    except ToolError:
        raise

    if source_mime not in {"xlsx", "text/csv"}:
        source_mime = "text/csv"
    df = parse_tabular(raw, title, source_mime)
    if df.shape[1] == 0 or df.shape[0] == 0:
        raise ToolError(f"'{title}' appears to be empty; there is nothing to analyze.")
    dataset = Dataset(source=source, title=title, load_time=utc_now())
    return dataset, df
