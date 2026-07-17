import hashlib
import html
import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+.-]{1,}|[\u4e00-\u9fff]")


def clean_text(value: Any, limit: int = 12000) -> str:
    if value is None:
        return ""
    text = html.unescape(TAG_RE.sub(" ", str(value)))
    return SPACE_RE.sub(" ", text).strip()[:limit]


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif not value:
        return datetime.now(timezone.utc)
    else:
        text = str(value).strip()
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            result = parsedate_to_datetime(text)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def canonical_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source"}]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))
    except ValueError:
        return url.strip()


def item_fingerprint(product_url: str, url: str, title: str) -> str:
    canonical = canonical_url(product_url) or canonical_url(url)
    basis = canonical or clean_text(title).lower()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    a = list(left)
    b = list(right)
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def lexical_similarity(goal: str, content: str) -> float:
    goal_tokens = {token.lower() for token in TOKEN_RE.findall(goal)}
    content_tokens = {token.lower() for token in TOKEN_RE.findall(content)}
    if not goal_tokens or not content_tokens:
        return 0.0
    overlap = len(goal_tokens & content_tokens)
    return overlap / math.sqrt(len(goal_tokens) * len(content_tokens))


def parse_vector(value: Any) -> Optional[List[float]]:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return [float(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [float(item) for item in parsed]
        except json.JSONDecodeError:
            return None
    return None

