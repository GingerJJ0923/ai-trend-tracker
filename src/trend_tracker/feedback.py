import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any, Dict


FEEDBACK_ACTIONS = {"helpful", "irrelevant", "deep_dive"}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_feedback_token(
    secret: str,
    beta_user_id: str,
    track_id: str,
    item_id: str,
    action: str,
    expires_at: int = 0,
) -> str:
    if not secret:
        raise ValueError("Feedback signing secret is required")
    if action not in FEEDBACK_ACTIONS:
        raise ValueError("Unsupported feedback action")
    payload: Dict[str, Any] = {
        "u": beta_user_id,
        "t": track_id,
        "i": item_id,
        "a": action,
        "exp": expires_at or int(time.time()) + 30 * 24 * 60 * 60,
    }
    encoded = _b64url(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _b64url(
        hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return "{0}.{1}".format(encoded, signature)


def verify_feedback_token(secret: str, token: str, now: int = 0) -> Dict[str, Any]:
    encoded, signature = token.split(".", 1)
    expected = _b64url(
        hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid feedback signature")
    payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    if payload.get("a") not in FEEDBACK_ACTIONS:
        raise ValueError("Unsupported feedback action")
    if int(payload.get("exp") or 0) < (now or int(time.time())):
        raise ValueError("Feedback token has expired")
    return payload


def feedback_url(page_url: str, api_url: str, token: str) -> str:
    """Keep the signed token in the fragment so mail scanners cannot submit it."""
    separator = "&" if "?" in page_url else "?"
    return "{0}{1}api={2}#{3}".format(
        page_url,
        separator,
        urllib.parse.quote(api_url, safe=""),
        urllib.parse.quote(token, safe=""),
    )
