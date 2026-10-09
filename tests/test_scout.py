import base64

import httpx

from app.scout import GitHubScout, MAX_SNIPPET_CHARS


def test_scout_skips_nonpermissive_repos_and_bounds_file(monkeypatch):
    import app.scout as module

    calls = []
    class Client:
        def __init__(self, **_):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/search/repositories"):
                data = {"items": [
                    {"full_name": "example/copyleft", "private": False,
                     "license": {"spdx_id": "GPL-3.0"}, "default_branch": "main"},
                    {"full_name": "example/permissive", "private": False,
                     "license": {"spdx_id": "MIT"}, "default_branch": "main"},
                ]}
            elif url.endswith("/contents"):
                data = [{"type": "file", "name": "index.html", "path": "index.html", "size": 4000},
                        {"type": "file", "name": "huge.js", "path": "huge.js", "size": 50000}]
            else:
                data = {"encoding": "base64", "content": base64.b64encode(b"a" * 4000).decode()}
            return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr(module.httpx, "Client", Client)
    results = GitHubScout().search("javascript calculator")
    assert len(results) == 1
    assert results[0]["license"] == "MIT"
    assert len(results[0]["files"][0]["content"]) == MAX_SNIPPET_CHARS
    assert all("copyleft" not in url and "huge.js" not in url for url, _ in calls)
    assert len(calls) == 3


def test_scout_rejects_conflicting_file_license(monkeypatch):
    import app.scout as module

    class Client:
        def __init__(self, **_):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def get(self, url, **kwargs):
            if url.endswith("/search/repositories"):
                data = {"items": [{"full_name": "example/repo", "private": False,
                                    "license": {"spdx_id": "MIT"}, "default_branch": "main"}]}
            elif url.endswith("/contents"):
                data = [{"type": "file", "name": "app.py", "path": "app.py", "size": 60}]
            else:
                content = b"# SPDX-License-Identifier: GPL-3.0\nprint('example')\n"
                data = {"encoding": "base64", "content": base64.b64encode(content).decode()}
            return httpx.Response(200, json=data, request=httpx.Request("GET", url))
    monkeypatch.setattr(module.httpx, "Client", Client)
    assert GitHubScout().search("python example") == []
