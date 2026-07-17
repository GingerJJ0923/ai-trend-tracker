import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class HttpError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, user_agent: str = "AI-Trend-Tracker/0.1") -> None:
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        payload: Optional[Any] = None,
        timeout: int = 30,
        retries: int = 2,
    ) -> bytes:
        merged_headers = {"User-Agent": self.user_agent, "Accept": "application/json, application/xml, text/xml, */*"}
        if headers:
            merged_headers.update(headers)
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            merged_headers.setdefault("Content-Type", "application/json")

        for attempt in range(retries + 1):
            request = urllib.request.Request(url, data=body, headers=merged_headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")[:1000]
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    retry_after = exc.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                    time.sleep(min(delay, 10))
                    continue
                raise HttpError("HTTP {0} for {1}: {2}".format(exc.code, url, details)) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise HttpError("Request failed for {0}: {1}".format(url, exc)) from exc
        raise HttpError("Request failed for {0}".format(url))

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> Any:
        return json.loads(self.request("GET", url, headers=headers).decode("utf-8"))

    def get_text(self, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        return self.request("GET", url, headers=headers).decode("utf-8", errors="replace")

    def post_json(self, url: str, payload: Any, headers: Optional[Dict[str, str]] = None) -> Any:
        raw = self.request("POST", url, headers=headers, payload=payload, timeout=60)
        return json.loads(raw.decode("utf-8")) if raw else None

