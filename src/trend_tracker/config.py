import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def supabase_key_role(key: str) -> str:
    """Return the privilege type encoded by a Supabase API key."""
    if key.startswith("sb_secret_"):
        return "service_role"
    if key.startswith("sb_publishable_"):
        return "anon"
    parts = key.split(".")
    if len(parts) != 3:
        return "unknown"
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return "unknown"
    return str(data.get("role") or "unknown")


@dataclass
class Settings:
    supabase_url: str
    supabase_key: str
    chat_api_key: str
    chat_base_url: str
    embedding_api_key: str
    embedding_base_url: str
    github_token: str
    resend_key: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    serverchan_sendkey: str
    digest_from: str
    digest_to: str
    source_config: str
    lookback_hours: int
    hn_item_limit: int
    match_candidates: int
    deep_analysis_limit: int
    embedding_model: str
    embedding_dimensions: int
    ranking_model: str
    analysis_model: str
    tracks_json: str
    output_language: str
    report_timezone: str
    report_top_items: int
    force_digest: bool

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        legacy_openai_key = os.environ.get("OPENAI_API_KEY", "")
        chat_api_key = os.environ.get("CHAT_API_KEY") or legacy_openai_key
        embedding_api_key = os.environ.get("EMBEDDING_API_KEY") or legacy_openai_key
        legacy_base_url = "https://api.openai.com/v1" if legacy_openai_key else ""
        embedding_model = os.environ.get("EMBEDDING_MODEL") or (
            "text-embedding-3-small" if legacy_openai_key else ""
        )
        ranking_model = os.environ.get("RANKING_MODEL") or (
            "gpt-5-nano" if legacy_openai_key else ""
        )
        analysis_model = os.environ.get("ANALYSIS_MODEL") or (
            "gpt-5-mini" if legacy_openai_key else ranking_model
        )
        return cls(
            supabase_url=os.environ.get("SUPABASE_URL", "").rstrip("/"),
            supabase_key=(
                os.environ.get("SUPABASE_SECRET_KEY")
                or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
            ),
            chat_api_key=chat_api_key,
            chat_base_url=(os.environ.get("CHAT_BASE_URL") or legacy_base_url).rstrip("/"),
            embedding_api_key=embedding_api_key,
            embedding_base_url=(os.environ.get("EMBEDDING_BASE_URL") or legacy_base_url).rstrip("/"),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            resend_key=os.environ.get("RESEND_API_KEY", ""),
            smtp_host=os.environ.get("SMTP_HOST") or "smtp.qq.com",
            smtp_port=env_int("SMTP_PORT", 465),
            smtp_username=os.environ.get("SMTP_USERNAME", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            serverchan_sendkey=os.environ.get("SERVERCHAN_SENDKEY", ""),
            digest_from=os.environ.get("DIGEST_FROM", ""),
            digest_to=os.environ.get("DIGEST_TO", ""),
            source_config=os.environ.get("SOURCE_CONFIG", "config/sources.json"),
            lookback_hours=env_int("LOOKBACK_HOURS", 30),
            hn_item_limit=env_int("HN_ITEM_LIMIT", 120),
            match_candidates=env_int("MATCH_CANDIDATES", 30),
            deep_analysis_limit=env_int("DEEP_ANALYSIS_LIMIT", 3),
            embedding_model=embedding_model,
            embedding_dimensions=env_int("EMBEDDING_DIMENSIONS", 512),
            ranking_model=ranking_model,
            analysis_model=analysis_model,
            tracks_json=os.environ.get("TRACKS_JSON", "[]"),
            output_language=os.environ.get("OUTPUT_LANGUAGE", "zh-CN"),
            report_timezone=os.environ.get("REPORT_TIMEZONE", "Asia/Shanghai"),
            report_top_items=max(1, env_int("REPORT_TOP_ITEMS", 5)),
            force_digest=env_bool("FORCE_DIGEST"),
        )

    def require_supabase(self) -> None:
        missing = []
        if not self.supabase_url:
            missing.append("SUPABASE_URL")
        if not self.supabase_key:
            missing.append("SUPABASE_SECRET_KEY (or legacy SUPABASE_SERVICE_ROLE_KEY)")
        if missing:
            raise RuntimeError("Missing required settings: " + ", ".join(missing))
        role = supabase_key_role(self.supabase_key)
        if role != "service_role":
            if role == "anon":
                raise RuntimeError(
                    "SUPABASE_SECRET_KEY contains an anon/publishable key. "
                    "Use a server-side sb_secret_* key or the legacy service_role key."
                )
            raise RuntimeError(
                "SUPABASE_SECRET_KEY is not a recognized server-side key. "
                "Use a server-side sb_secret_* key or the legacy service_role key."
            )

    def sources(self) -> List[Dict[str, Any]]:
        path = Path(self.source_config)
        if not path.exists():
            raise FileNotFoundError("Source config not found: {0}".format(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Source config must contain a JSON array")
        return data

    def seed_tracks(self) -> List[Dict[str, str]]:
        try:
            data = json.loads(self.tracks_json or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("TRACKS_JSON is invalid JSON") from exc
        if not isinstance(data, list):
            raise ValueError("TRACKS_JSON must be a JSON array")
        result = []
        for row in data:
            if isinstance(row, dict) and row.get("name") and row.get("goal"):
                result.append({"name": str(row["name"]), "goal": str(row["goal"])})
        return result
