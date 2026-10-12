"""Bounded, read-only discovery of permissively licensed GitHub examples."""

import base64
import re
from urllib.parse import quote

import httpx


PERMISSIVE_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause"}
CODE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css")
MAX_FILE_BYTES = 12_000
MAX_SNIPPET_CHARS = 3_000


class GitHubScout:
    def __init__(self, timeout_seconds: float = 8):
        self.timeout_seconds = timeout_seconds

    def search(self, query: str) -> list[dict]:
        # A model can suggest search terms, but never API qualifiers or URLs.
        terms = re.findall(r"[\w-]{2,}", query, flags=re.UNICODE)[:8]
        if not terms:
            return []
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "batcomputer-scout"}
        try:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=False,
                              headers=headers) as client:
                response = client.get("https://api.github.com/search/repositories",
                                      params={"q": " ".join(terms) + " in:name,description,readme archived:false",
                                              "per_page": 8})
                response.raise_for_status()
                payload = response.json()
                items = payload.get("items", []) if isinstance(payload, dict) else []
                if not isinstance(items, list):
                    return []
                checked = 0
                for repo in items:
                    if not isinstance(repo, dict) or repo.get("private"):
                        continue
                    license_info = repo.get("license") or {}
                    if not isinstance(license_info, dict):
                        continue
                    license_id = license_info.get("spdx_id")
                    if license_id not in PERMISSIVE_LICENSES:
                        continue
                    checked += 1
                    if checked > 3:
                        break
                    full_name = repo.get("full_name", "")
                    if not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name):
                        continue
                    owner, name = full_name.split("/", 1)
                    branch = str(repo.get("default_branch") or "")
                    if not branch or len(branch) > 120:
                        continue
                    base = f"https://api.github.com/repos/{owner}/{name}/contents"
                    listing = client.get(base, params={"ref": branch})
                    listing.raise_for_status()
                    files = listing.json()
                    if not isinstance(files, list):
                        continue
                    candidates = [item for item in files if isinstance(item, dict)
                                  and item.get("type") == "file"
                                  and str(item.get("name", "")).lower().endswith(CODE_SUFFIXES)
                                  and 0 < int(item.get("size") or 0) <= MAX_FILE_BYTES]
                    candidates.sort(key=lambda item: (str(item.get("name", "")).lower()
                                                      not in {"index.html", "main.py", "app.py", "main.js"},
                                                      int(item.get("size") or 0)))
                    examples = []
                    for item in candidates[:2]:
                        path = str(item.get("path", ""))
                        if not path or "/" in path or ".." in path:
                            continue
                        content_response = client.get(base + "/" + quote(path), params={"ref": branch})
                        content_response.raise_for_status()
                        data = content_response.json()
                        if not isinstance(data, dict):
                            continue
                        if data.get("encoding") != "base64":
                            continue
                        raw = base64.b64decode(data.get("content", ""), validate=False)
                        if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:
                            continue
                        content = raw.decode("utf-8", errors="replace")
                        header = content[:700].lower()
                        if ("spdx-license-identifier:" in header and
                                license_id.lower() not in header):
                            continue
                        if "gnu general public license" in header or "gpl-" in header:
                            continue
                        examples.append({"path": path, "content": content[:MAX_SNIPPET_CHARS]})
                    if examples:
                        return [{"repository": full_name, "url": f"https://github.com/{full_name}",
                                 "license": license_id, "files": examples}]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, base64.binascii.Error):
            return []
        return []
