import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo


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


def _normalize_json_punctuation(value: str) -> str:
    """Normalize common Chinese JSON punctuation without changing string content."""
    result: List[str] = []
    in_string = False
    quote_end = ""
    escaped = False
    for char in value.lstrip("\ufeff"):
        if in_string:
            if escaped:
                result.append(char)
                escaped = False
            elif char == "\\":
                result.append(char)
                escaped = True
            elif char == quote_end:
                result.append('"')
                in_string = False
                quote_end = ""
            elif quote_end == "”" and char == '"':
                result.append('\\"')
            else:
                result.append(char)
            continue

        if char == '"':
            result.append(char)
            in_string = True
            quote_end = '"'
        elif char == "“":
            result.append('"')
            in_string = True
            quote_end = "”"
        elif char == "，":
            result.append(",")
        elif char == "：":
            result.append(":")
        else:
            result.append(char)
    return "".join(result)


def _remove_trailing_json_commas(value: str) -> str:
    """Remove commas immediately before a closing object or array token."""
    result: List[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == ",":
            next_index = index + 1
            while next_index < len(value) and value[next_index].isspace():
                next_index += 1
            if next_index < len(value) and value[next_index] in "}]":
                index += 1
                continue
        result.append(char)
        index += 1
    return "".join(result)


def parse_json_setting(value: str, name: str) -> Any:
    """Parse a JSON setting and repair only common, unambiguous copy/paste errors."""
    raw_value = value or "[]"
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        repaired = _remove_trailing_json_commas(
            _normalize_json_punctuation(raw_value)
        )

    # A missing comma is unambiguous when the decoder stops at the start of
    # another key/object after a complete JSON value. Repair several omissions
    # so a hand-edited beta list does not take the whole digest offline.
    last_error: json.JSONDecodeError | None = None
    for _ in range(20):
        try:
            parsed = json.loads(repaired)
            print(
                "WARNING: {0} contained common JSON formatting issues; "
                "safe syntax repairs were applied.".format(name)
            )
            return parsed
        except json.JSONDecodeError as exc:
            last_error = exc
            if exc.msg != "Expecting ',' delimiter":
                break
            current_index = exc.pos
            previous_index = current_index - 1
            while previous_index >= 0 and repaired[previous_index].isspace():
                previous_index -= 1
            if previous_index < 0 or current_index >= len(repaired):
                break
            previous_char = repaired[previous_index]
            current_char = repaired[current_index]
            completed_value = (
                previous_char in {'"', "}", "]"}
                or previous_char.isdigit()
            )
            starts_next_value = current_char in {'"', "{", "["}
            if not (completed_value and starts_next_value):
                break
            repaired = repaired[:current_index] + "," + repaired[current_index:]

    assert last_error is not None
    raise ValueError(
        "{0} is invalid JSON near line {1}, column {2}. "
        "Check the comma or punctuation immediately before that position.".format(
            name,
            last_error.lineno,
            last_error.colno,
        )
    ) from last_error


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
    digest_asset_base_url: str
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
    beta_users_json: str
    beta_max_users: int
    beta_max_tracks_per_user: int
    feedback_signing_secret: str
    feedback_page_url: str
    feedback_api_url: str
    output_language: str
    report_timezone: str
    report_highlight_items: int
    report_quick_items: int
    report_relevance_threshold: int
    report_show_scores: bool
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
        digest_asset_base_url = os.environ.get("DIGEST_ASSET_BASE_URL", "").rstrip("/")
        if not digest_asset_base_url:
            github_repository = os.environ.get("GITHUB_REPOSITORY", "").strip("/")
            github_sha = os.environ.get("GITHUB_SHA", "").strip()
            if github_repository and github_sha:
                digest_asset_base_url = (
                    "https://raw.githubusercontent.com/{0}/{1}/assets/email".format(
                        github_repository,
                        github_sha,
                    )
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
            digest_asset_base_url=digest_asset_base_url,
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
            beta_users_json=os.environ.get("BETA_USERS_JSON", "[]"),
            beta_max_users=max(1, env_int("BETA_MAX_USERS", 20)),
            beta_max_tracks_per_user=max(
                1, env_int("BETA_MAX_TRACKS_PER_USER", 3)
            ),
            feedback_signing_secret=os.environ.get("FEEDBACK_SIGNING_SECRET", ""),
            feedback_page_url=os.environ.get("FEEDBACK_PAGE_URL", "").rstrip("/"),
            feedback_api_url=os.environ.get("FEEDBACK_API_URL", "").rstrip("/"),
            output_language=os.environ.get("OUTPUT_LANGUAGE", "zh-CN"),
            report_timezone=os.environ.get("REPORT_TIMEZONE", "Asia/Shanghai"),
            report_highlight_items=max(
                1,
                env_int("REPORT_HIGHLIGHT_ITEMS", env_int("REPORT_TOP_ITEMS", 3)),
            ),
            report_quick_items=max(0, env_int("REPORT_QUICK_ITEMS", 12)),
            report_relevance_threshold=max(
                0,
                min(100, env_int("REPORT_RELEVANCE_THRESHOLD", 50)),
            ),
            report_show_scores=env_bool("REPORT_SHOW_SCORES"),
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
        data = parse_json_setting(self.tracks_json, "TRACKS_JSON")
        if not isinstance(data, list):
            raise ValueError("TRACKS_JSON must be a JSON array")
        result = []
        for row in data:
            if isinstance(row, dict) and row.get("name") and row.get("goal"):
                result.append({"name": str(row["name"]), "goal": str(row["goal"])})
        return result

    def beta_users(self) -> List[Dict[str, Any]]:
        """Parse the owner-managed invite list for the small design-partner beta."""
        data = parse_json_setting(self.beta_users_json, "BETA_USERS_JSON")
        if not isinstance(data, list):
            raise ValueError("BETA_USERS_JSON must be a JSON array")
        if len(data) > self.beta_max_users:
            raise ValueError(
                "BETA_USERS_JSON has {0} users, above BETA_MAX_USERS={1}".format(
                    len(data), self.beta_max_users
                )
            )
        result: List[Dict[str, Any]] = []
        seen_emails = set()
        wechat_users = 0
        for index, row in enumerate(data):
            if not isinstance(row, dict):
                raise ValueError("BETA_USERS_JSON row {0} must be an object".format(index + 1))
            email = str(row.get("email") or "").strip().lower()
            if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+", email):
                raise ValueError("BETA_USERS_JSON row {0} has an invalid email".format(index + 1))
            if email in seen_emails:
                raise ValueError(
                    "BETA_USERS_JSON row {0} duplicates an earlier email".format(
                        index + 1
                    )
                )
            seen_emails.add(email)
            tracks = []
            seen_track_names = set()
            for track in row.get("tracks") or []:
                if isinstance(track, dict) and track.get("name") and track.get("goal"):
                    track_name = str(track["name"]).strip()
                    if track_name in seen_track_names:
                        raise ValueError(
                            "BETA_USERS_JSON row {0} has duplicate Track names".format(
                                index + 1
                            )
                        )
                    seen_track_names.add(track_name)
                    tracks.append(
                        {
                            "name": track_name,
                            "goal": str(track["goal"]).strip(),
                        }
                    )
            if not tracks:
                raise ValueError(
                    "BETA_USERS_JSON row {0} must have at least one Track".format(
                        index + 1
                    )
                )
            if len(tracks) > self.beta_max_tracks_per_user:
                raise ValueError(
                    "BETA_USERS_JSON row {0} has {1} Tracks, above "
                    "BETA_MAX_TRACKS_PER_USER={2}".format(
                        index + 1, len(tracks), self.beta_max_tracks_per_user
                    )
                )
            timezone_name = str(
                row.get("timezone") or self.report_timezone
            ).strip()
            try:
                ZoneInfo(timezone_name)
            except (KeyError, ValueError) as exc:
                raise ValueError(
                    "BETA_USERS_JSON row {0} has an invalid timezone".format(
                        index + 1
                    )
                ) from exc
            result.append(
                {
                    "email": email,
                    "display_name": str(row.get("display_name") or "").strip(),
                    "timezone": timezone_name,
                    "wechat_enabled": row.get("wechat_enabled") is True,
                    "tracks": tracks,
                }
            )
            if row.get("wechat_enabled") is True:
                wechat_users += 1
        if wechat_users > 1:
            raise ValueError(
                "BETA_USERS_JSON may enable WeChat for at most one owner account"
            )
        return result

    @property
    def feedback_enabled(self) -> bool:
        values = (
            self.feedback_signing_secret,
            self.feedback_page_url,
            self.feedback_api_url,
        )
        if any(values) and not all(values):
            raise ValueError(
                "FEEDBACK_SIGNING_SECRET, FEEDBACK_PAGE_URL and FEEDBACK_API_URL "
                "must be configured together"
            )
        return all(values)
