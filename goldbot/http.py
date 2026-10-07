"""Tiny stdlib HTTP helper (no extra dependencies), injectable for tests."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body.decode() or "null")


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        # never include query strings (they may carry API keys) in error messages
        super().__init__(f"HTTP {status} from {url.split('?')[0]}: {body[:300]}")
        self.status = status


def request(method: str, url: str, *, params: dict | None = None, json_body=None,
            headers: dict | None = None, timeout: float = 20, retries: int = 2) -> Response:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    hdrs = {"User-Agent": "goldbot/0.1", **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Response(r.status, {k.upper(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code < 500 and e.code != 429:
                raise HttpError(e.code, body, url) from None
            last = HttpError(e.code, body, url)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = RuntimeError(f"network error for {url.split('?')[0]}: {e}")
        if attempt < retries:
            time.sleep(2 ** (attempt + 1))
    raise last  # type: ignore[misc]
