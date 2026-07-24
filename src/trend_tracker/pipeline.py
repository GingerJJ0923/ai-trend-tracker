import json
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .ai import AIService, candidate_scores
from .config import Settings
from .connectors import collect_source
from .feedback import feedback_url, sign_feedback_token
from .http import HttpClient
from .models import BetaUser, MatchResult, SourceItem, Track
from .report import build_report, send_email, send_smtp_email, send_wechat, write_report
from .repository import SupabaseRepository


def make_services(settings: Settings):
    settings.require_supabase()
    http = HttpClient()
    repository = SupabaseRepository(settings.supabase_url, settings.supabase_key, http)
    ai = AIService(
        chat_api_key=settings.chat_api_key,
        chat_base_url=settings.chat_base_url,
        embedding_api_key=settings.embedding_api_key,
        embedding_base_url=settings.embedding_base_url,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        ranking_model=settings.ranking_model,
        analysis_model=settings.analysis_model,
        http=http,
        output_language=settings.output_language,
    )
    return http, repository, ai


def deduplicate_observations(items: List[SourceItem]) -> List[SourceItem]:
    grouped: Dict[str, SourceItem] = {}
    sources: Dict[str, set] = {}
    for item in sorted(items, key=lambda row: row.published_at, reverse=True):
        key = item.fingerprint or "{0}:{1}".format(item.source_key, item.external_id)
        sources.setdefault(key, set()).add(item.source_key)
        if key not in grouped:
            grouped[key] = item
    for key, item in grouped.items():
        item.metadata = dict(item.metadata)
        item.metadata["related_sources"] = sorted(sources[key])
    return list(grouped.values())


def collect(settings: Settings, dry_run: bool = False) -> int:
    http = HttpClient()
    repository = None
    if not dry_run:
        settings.require_supabase()
        repository = SupabaseRepository(settings.supabase_url, settings.supabase_key, http)
    total = 0
    successful_sources = 0
    preview = []
    for source in settings.sources():
        key = source.get("key", source.get("type", "unknown"))
        try:
            items = collect_source(source, http, settings.lookback_hours, settings.github_token, settings.hn_item_limit)
            unique = {}
            for item in items:
                unique[(item.source_key, item.external_id)] = item
            items = list(unique.values())
            total += len(items)
            if repository:
                repository.upsert_items(items)
                repository.log_fetch(key, "success", len(items))
            successful_sources += 1
            preview.extend(items[:3])
            print("{0}: collected {1} items".format(key, len(items)))
        except Exception as exc:
            print("{0}: FAILED: {1}".format(key, exc))
            if repository:
                try:
                    repository.log_fetch(key, "failed", 0, str(exc))
                except Exception as log_exc:
                    print("{0}: could not write failure log to Supabase: {1}".format(key, log_exc))
    if dry_run:
        print(json.dumps([{"source": item.source_key, "title": item.title, "url": item.url} for item in preview], ensure_ascii=False, indent=2))
    if successful_sources == 0:
        raise RuntimeError("All configured sources failed; inspect the source errors above")
    print("Total collected observations: {0}".format(total))
    return total


def seed_tracks(settings: Settings) -> int:
    _, repository, _ = make_services(settings)
    created = repository.seed_tracks(settings.seed_tracks())
    print("Created {0} new Tracks".format(created))
    return created


def _ensure_embeddings(repository: SupabaseRepository, ai: AIService, tracks: List[Track], items: List[SourceItem]) -> None:
    if not ai.embeddings_enabled:
        return
    missing_tracks = [track for track in tracks if not track.embedding]
    if missing_tracks:
        vectors = ai.embeddings([track.matching_goal() for track in missing_tracks])
        for track, vector in zip(missing_tracks, vectors):
            track.embedding = vector
            repository.update_track_embedding(track.id, vector)

    missing_items = [item for item in items if not item.embedding]
    if missing_items:
        vectors = ai.embeddings([item.text_for_matching() for item in missing_items])
        for item, vector in zip(missing_items, vectors):
            item.embedding = vector
            repository.update_item_embedding(str(item.id), vector)


def _configured_delivery_channels(
    settings: Settings,
    recipient: Optional[str] = None,
    allow_wechat: bool = True,
) -> List[str]:
    channels = []
    email_ready = bool(
        (recipient or settings.digest_to)
        and (
            (settings.smtp_username and settings.smtp_password)
            or (settings.resend_key and settings.digest_from)
        )
    )
    if email_ready:
        channels.append("email")
    if allow_wechat and settings.serverchan_sendkey:
        channels.append("wechat")
    return channels


def _run_with_retries(action: Callable[[], None], attempts: int = 3) -> Tuple[bool, str, int]:
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            action()
            return True, "", attempt
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    return False, last_error, attempts


def _deliver_report(
    settings: Settings,
    http: HttpClient,
    repository: SupabaseRepository,
    digest_id: int,
    metadata: Dict,
    subject: str,
    report: str,
    recipient: Optional[str] = None,
    allow_wechat: bool = True,
) -> None:
    delivery = dict(metadata.get("delivery") or {})
    failures = []

    def record(channel: str, success: bool, error: str, attempts: int) -> None:
        delivery[channel] = {
            "status": "success" if success else "failed",
            "attempts": attempts,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
            "error": error[:1000] if error else None,
        }
        metadata["delivery"] = delivery
        repository.update_digest_metadata(digest_id, metadata)
        if not success:
            failures.append("{0}: {1}".format(channel, error))

    delivery_recipient = recipient or settings.digest_to
    channels = _configured_delivery_channels(settings, delivery_recipient, allow_wechat)
    if "email" in channels and delivery.get("email", {}).get("status") != "success":
        if settings.smtp_username and settings.smtp_password:
            email_action = lambda: send_smtp_email(
                settings.smtp_host,
                settings.smtp_port,
                settings.smtp_username,
                settings.smtp_password,
                delivery_recipient,
                subject,
                report,
            )
        else:
            email_action = lambda: send_email(
                http,
                settings.resend_key,
                settings.digest_from,
                delivery_recipient,
                subject,
                report,
            )
        record("email", *_run_with_retries(email_action))

    if "wechat" in channels and delivery.get("wechat", {}).get("status") != "success":
        record(
            "wechat",
            *_run_with_retries(lambda: send_wechat(http, settings.serverchan_sendkey, subject, report)),
        )

    if failures:
        raise RuntimeError("Digest delivery failed after retries: " + "; ".join(failures))


def _compile_tracks(
    repository: SupabaseRepository, ai: AIService, tracks: List[Track]
) -> None:
    for track in tracks:
        if track.compiled_goal and track.goal_spec.get("source") != "raw_fallback":
            continue
        compiled_goal, goal_spec = ai.compile_goal(track)
        track.compiled_goal = compiled_goal
        track.goal_spec = goal_spec
        track.embedding = None
        repository.update_compiled_goal(track.id, compiled_goal, goal_spec)


def _attach_feedback_links(
    settings: Settings, beta_user: BetaUser, matches: List[MatchResult]
) -> None:
    if not settings.feedback_enabled:
        return
    for match in matches:
        match.feedback_links = {}
        for action in ("helpful", "irrelevant", "deep_dive"):
            token = sign_feedback_token(
                settings.feedback_signing_secret,
                beta_user.id,
                match.track_id,
                match.item_id,
                action,
            )
            match.feedback_links[action] = feedback_url(
                settings.feedback_page_url,
                settings.feedback_api_url,
                token,
            )


def _local_day(timezone_name: str) -> Tuple[datetime, datetime, str]:
    try:
        local_timezone = ZoneInfo(timezone_name)
    except (KeyError, ValueError):
        local_timezone = timezone.utc
    local_now = datetime.now(local_timezone)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return local_now, day_start, "AI 趋势日报｜{0}".format(local_now.strftime("%Y-%m-%d"))


def _process_digest(
    settings: Settings,
    http: HttpClient,
    repository: SupabaseRepository,
    ai: AIService,
    items: List[SourceItem],
    tracks: List[Track],
    beta_user: Optional[BetaUser] = None,
) -> str:
    timezone_name = beta_user.timezone if beta_user else settings.report_timezone
    local_now, day_start, subject = _local_day(timezone_name)
    recipient = beta_user.email if beta_user else settings.digest_to
    allow_wechat = beta_user is None or beta_user.wechat_enabled
    required_channels = _configured_delivery_channels(
        settings, recipient, allow_wechat
    )
    if beta_user and "email" not in required_channels:
        raise RuntimeError(
            "A beta user cannot receive email: configure SMTP or Resend credentials"
        )

    beta_user_id = beta_user.id if beta_user else None
    existing_digest = (
        None
        if settings.force_digest
        else repository.get_latest_digest_since(day_start, beta_user_id)
    )
    if existing_digest:
        report = str(existing_digest.get("content") or "")
        metadata = dict(existing_digest.get("metadata") or {})
        delivery = metadata.get("delivery")
        suffix = beta_user.id[:8] if beta_user else ""
        path = write_report(report, suffix=suffix)
        if not delivery:
            print("Today's legacy digest already exists; assuming it was delivered and skipping duplicates.")
            return str(path)
        if all(delivery.get(channel, {}).get("status") == "success" for channel in required_channels):
            print("Today's digest has already reached every configured channel; skipping duplicate delivery.")
            return str(path)
        _deliver_report(
            settings,
            http,
            repository,
            int(existing_digest["id"]),
            metadata,
            subject,
            report,
            recipient,
            allow_wechat,
        )
        print("Retried only the delivery channels that had not succeeded.")
        return str(path)

    if not tracks:
        raise RuntimeError("No active Tracks for this digest recipient.")

    _compile_tracks(repository, ai, tracks)
    _ensure_embeddings(repository, ai, tracks, items)

    matches_by_track: Dict[str, List[MatchResult]] = {}
    trends_by_track: Dict[str, str] = {}
    db_match_records = []
    for track in tracks:
        feedback_examples = (
            repository.get_feedback_examples(beta_user.id, track.id)
            if beta_user
            else []
        )
        candidates = candidate_scores(track, items)[: settings.match_candidates]
        matches = ai.rerank(track, candidates, feedback_examples)
        relevant = [match for match in matches if match.score >= 50]
        if beta_user:
            _attach_feedback_links(settings, beta_user, relevant)
        for match in matches:
            db_match_records.append(
                {
                    "track_id": match.track_id,
                    "item_id": match.item_id,
                    "score": round(match.score, 2),
                    "semantic_score": round(match.semantic_score, 6),
                    "tier": match.tier,
                    "reason": match.reason,
                }
            )
        for match in [row for row in relevant if row.score >= 80][: settings.deep_analysis_limit]:
            match.analysis = ai.analyze(track, match)
            repository.upsert_analysis(track.id, match.item_id, match.analysis, settings.analysis_model)
        matches_by_track[track.id] = matches
        trends_by_track[track.id] = ai.trend_summary(track, matches)

    repository.upsert_matches(db_match_records)
    generated_at = datetime.now(timezone.utc)
    report = build_report(
        generated_at,
        len(items),
        tracks,
        matches_by_track,
        trends_by_track,
        highlight_items=settings.report_highlight_items,
        quick_items=settings.report_quick_items,
        relevance_threshold=settings.report_relevance_threshold,
        show_scores=settings.report_show_scores,
        timezone_name=timezone_name,
    )
    suffix = beta_user.id[:8] if beta_user else ""
    path = write_report(report, suffix=suffix)
    metadata = {
        "scanned_count": len(items),
        "track_count": len(tracks),
        "mode": "design_partner_beta" if beta_user else "legacy_personal",
        "recipient": beta_user.email if beta_user else None,
        "delivery": {channel: {"status": "pending"} for channel in required_channels},
    }
    digest_id = repository.save_digest(
        generated_at,
        report,
        metadata,
        beta_user_id,
    )
    exposed_matches = [
        match
        for track in tracks
        for match in matches_by_track.get(track.id, [])
        if match.score >= settings.report_relevance_threshold
    ]
    repository.record_digest_items(digest_id, exposed_matches)
    _deliver_report(
        settings,
        http,
        repository,
        digest_id,
        metadata,
        subject,
        report,
        recipient,
        allow_wechat,
    )
    print("Report written to {0}".format(path))
    return str(path)


def digest(settings: Settings) -> str:
    http, repository, ai = make_services(settings)
    beta_seeds = settings.beta_users()
    if beta_seeds:
        created = repository.seed_beta_users(beta_seeds)
        print("Created {0} new beta users".format(created))
        beta_users = repository.get_beta_users()
    else:
        beta_users = []
        repository.seed_tracks(settings.seed_tracks())

    since = datetime.now(timezone.utc) - timedelta(hours=settings.lookback_hours)
    observations = repository.get_recent_items(since)
    items = deduplicate_observations(observations)
    paths: List[str] = []
    failures: List[str] = []

    if beta_users:
        for beta_user in beta_users:
            try:
                tracks = repository.get_tracks(beta_user.id)
                paths.append(
                    _process_digest(
                        settings,
                        http,
                        repository,
                        ai,
                        items,
                        tracks,
                        beta_user,
                    )
                )
            except Exception as exc:
                anonymous_id = beta_user.id[:8]
                failures.append("{0}: {1}".format(anonymous_id, exc))
                print("Beta digest FAILED for user {0}: {1}".format(anonymous_id, exc))
    else:
        tracks = repository.get_tracks()
        if not tracks:
            raise RuntimeError(
                "No active Tracks. Configure TRACKS_JSON or BETA_USERS_JSON."
            )
        paths.append(
            _process_digest(settings, http, repository, ai, items, tracks)
        )

    cleanup_result = repository.cleanup()
    storage_status = repository.storage_status()
    print("Cleanup result: {0}".format(cleanup_result))
    print("Database storage: {0}".format(storage_status))
    if failures:
        raise RuntimeError(
            "Some beta digests failed after other users were processed: "
            + "; ".join(failures)
        )
    return paths[-1] if paths else ""
