import base64
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from trend_tracker.ai import AIService, candidate_scores
from trend_tracker.connectors import collect_rss
from trend_tracker.config import Settings, supabase_key_role
from trend_tracker.feedback import feedback_url, sign_feedback_token, verify_feedback_token
from trend_tracker.http import HttpClient
from trend_tracker.models import BetaUser, MatchResult, SourceItem, Track
from trend_tracker.pipeline import (
    _configured_delivery_channels,
    _deliver_report,
    _run_with_retries,
    deduplicate_observations,
    digest,
    import_beta_users,
)
from trend_tracker.report import build_report, email_recipients, markdown_email_html, send_email, send_smtp_email, send_wechat
from trend_tracker.repository import SupabaseRepository
from trend_tracker.utils import canonical_url, clean_text, cosine_similarity, item_fingerprint, parse_datetime


class UtilityTests(unittest.TestCase):
    def test_clean_text_strips_markup(self):
        self.assertEqual(clean_text("<p>Hello&nbsp; world</p>"), "Hello world")

    def test_canonical_url_removes_tracking(self):
        value = canonical_url("HTTPS://Example.com/product/?utm_source=x&b=2#section")
        self.assertEqual(value, "https://example.com/product?b=2")

    def test_datetime_normalizes_utc(self):
        value = parse_datetime("2026-07-16T12:00:00+08:00")
        self.assertEqual(value.isoformat(), "2026-07-16T04:00:00+00:00")

    def test_cosine_similarity(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)


class PipelineTests(unittest.TestCase):
    def make_item(self, source, external_id, url, title="AI tool"):
        item = SourceItem(
            id=external_id,
            source_key=source,
            external_id=external_id,
            title=title,
            url=url,
            product_url=url,
            summary="Useful agent workflow",
            published_at=datetime.now(timezone.utc),
        )
        item.fingerprint = item_fingerprint(item.product_url, item.url, item.title)
        return item

    def test_deduplicates_cross_source_url(self):
        one = self.make_item("ph", "1", "https://example.com/tool?utm_source=ph")
        two = self.make_item("hn", "2", "https://example.com/tool")
        result = deduplicate_observations([one, two])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata["related_sources"], ["hn", "ph"])

    def test_embedding_candidate_order(self):
        track = Track(id="t", name="test", goal="agent", embedding=[1.0, 0.0])
        first = self.make_item("a", "1", "https://a.test")
        second = self.make_item("b", "2", "https://b.test")
        first.embedding = [0.9, 0.1]
        second.embedding = [0.0, 1.0]
        ranked = candidate_scores(track, [second, first])
        self.assertEqual(ranked[0][0].id, "1")

    def test_report_contains_evidence(self):
        item = self.make_item("ph", "1", "https://example.com/tool")
        track = Track(id="t", name="Agents", goal="Track useful agents")
        match = MatchResult(
            "t",
            "1",
            88,
            0.7,
            "high",
            "Directly relevant",
            item,
            (
                "- **新在哪里：** 新能力。\n"
                "- **判断依据：** 原始资料。\n"
                "- **适用边界：** 尚待验证。\n"
                "- **先验证：** 用一个任务测试。"
            ),
        )
        report = build_report(datetime.now(timezone.utc), 10, [track], {"t": [match]}, {"t": "Agent tools are becoming more actionable."})
        self.assertIn("Directly relevant", report)
        self.assertIn("https://example.com/tool", report)
        self.assertIn("深度判断", report)
        self.assertNotIn("编辑判断", report)
        self.assertIn("一句话看懂", report)
        self.assertIn("与你有关", report)
        self.assertIn("适用边界", report)
        self.assertIn("查看原始资料", report)
        self.assertIn("**来源** Ph", report)
        self.assertIn("**发布于**", report)
        self.assertNotIn("为什么值得看", report)
        self.assertNotIn("来源与时间", report)
        self.assertIn("30 秒结论", report)
        self.assertIn("今日重点情报", report)
        self.assertIn("趋势雷达", report)
        self.assertIn("其他相关信号", report)
        self.assertNotIn("88 分", report)
        self.assertNotIn("｜高度相关", report)
        self.assertNotIn(" · 值得关注", report)
        self.assertNotIn("Why it matches", report)

    def test_report_uses_synthesized_three_line_daily_brief(self):
        item = self.make_item("ph", "1", "https://example.com/tool", "最高分产品")
        track = Track(id="t", name="智能体", goal="关注能落地的智能体产品")
        match = MatchResult(
            "t",
            "1",
            95,
            0.8,
            "high",
            "与目标直接相关",
            item,
            display_title="最高分产品",
            concise_summary="改善工作流",
            next_action="测试产品",
        )
        report = build_report(
            datetime.now(timezone.utc),
            10,
            [track],
            {"t": [match]},
            {"t": "信号呈现两条不同路线。"},
            daily_brief={
                "today_change": "相关产品正在从功能展示转向可复现的真实任务验证。",
                "why_it_matters": "选型标准需要从功能数量转向任务完成质量。",
                "next_action": "选一个真实任务，对两个候选产品做同输入测试。",
            },
        )
        conclusion = report.split("## 今日重点情报", 1)[0]
        self.assertIn("**新信号：** 相关产品正在从功能展示转向可复现的真实任务验证。", conclusion)
        self.assertIn("**与你有关：** 选型标准需要从功能数量转向任务完成质量。", conclusion)
        self.assertIn("**今天可做：** 选一个真实任务，对两个候选产品做同输入测试。", conclusion)
        self.assertNotIn("今天最值得关注", conclusion)
        self.assertNotIn("最高分产品", conclusion)

    def test_report_splits_highlights_and_quick_signals(self):
        track = Track(id="t", name="智能体", goal="关注可落地的智能体产品")
        matches = []
        for index in range(7):
            item = self.make_item("ph", str(index), "https://example.com/{0}".format(index), "产品 {0}".format(index))
            matches.append(
                MatchResult(
                    "t", str(index), 90 - index, 0.7, "high", "与目标直接相关", item,
                    display_title="产品 {0}".format(index),
                    concise_summary="用于改善工作流",
                    next_action="用一个任务进行测试",
                )
            )
        report = build_report(
            datetime.now(timezone.utc), 20, [track], {"t": matches},
            {"t": "**趋势判断：** 智能体正在走向落地。"},
            highlight_items=3,
            quick_items=2,
        )
        highlights, related = report.split("## 其他相关信号")
        self.assertIn("#### 3. [产品 2]", highlights)
        self.assertNotIn("产品 3", highlights)
        self.assertIn("产品 3", related)
        self.assertIn("产品 4", report)
        self.assertNotIn("产品 5", report)
        self.assertNotIn("另有", report)

    def test_report_places_trends_before_related_signals(self):
        track = Track(id="t", name="智能体", goal="关注可落地的智能体产品")
        matches = []
        for index in range(4):
            item = self.make_item("ph", str(index), "https://example.com/{0}".format(index), "产品 {0}".format(index))
            matches.append(
                MatchResult(
                    "t", str(index), 90 - index, 0.7, "high", "与目标直接相关", item,
                    display_title="产品 {0}".format(index),
                    concise_summary="改善智能体工作流",
                    next_action="用一个任务测试",
                )
            )
        report = build_report(
            datetime.now(timezone.utc), 20, [track], {"t": matches},
            {"t": "**趋势判断：** 智能体正在走向落地。"},
        )
        self.assertLess(report.index("## 趋势雷达"), report.index("## 其他相关信号"))
        self.assertIn("[今日重点 3 条](#highlights)", report)

    def test_report_uses_three_beta_feedback_actions_without_known(self):
        item = self.make_item("ph", "1", "https://example.com/tool")
        track = Track(id="t", name="智能体", goal="关注智能体")
        match = MatchResult(
            "t",
            "1",
            90,
            0.8,
            "high",
            "直接相关",
            item,
            feedback_links={
                "helpful": "https://feedback.test/#helpful",
                "irrelevant": "https://feedback.test/#irrelevant",
                "deep_dive": "https://feedback.test/#deep",
            },
        )
        report = build_report(
            datetime.now(timezone.utc),
            1,
            [track],
            {"t": [match]},
            {"t": "趋势"},
        )
        self.assertIn("[有用]", report)
        self.assertIn("[不相关]", report)
        self.assertIn("[继续深挖]", report)
        self.assertNotIn("已经知道", report)
        rendered = markdown_email_html(report)
        self.assertIn('href="https://feedback.test/#helpful"', rendered)

    @patch("trend_tracker.pipeline.time.sleep")
    def test_delivery_retry_succeeds_after_transient_failure(self, sleep):
        calls = []

        def flaky_action():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("temporary failure")

        success, error, attempts = _run_with_retries(flaky_action)
        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(attempts, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("trend_tracker.pipeline.time.sleep")
    @patch("trend_tracker.pipeline.send_wechat")
    @patch("trend_tracker.pipeline.send_smtp_email")
    def test_email_failure_does_not_block_wechat(self, smtp_send, wechat_send, sleep):
        smtp_send.side_effect = RuntimeError("smtp unavailable")
        settings = SimpleNamespace(
            digest_to="one@example.com,two@example.com",
            smtp_username="sender@example.com",
            smtp_password="secret",
            smtp_host="smtp.example.com",
            smtp_port=465,
            resend_key="",
            digest_from="",
            serverchan_sendkey="SCTexample",
        )

        class RecordingRepository:
            def __init__(self):
                self.metadata = []

            def update_digest_metadata(self, digest_id, metadata):
                self.metadata.append(json.loads(json.dumps(metadata)))

        repository = RecordingRepository()
        metadata = {"delivery": {"email": {"status": "pending"}, "wechat": {"status": "pending"}}}
        with self.assertRaisesRegex(RuntimeError, "email"):
            _deliver_report(settings, DeliveryHttp(), repository, 1, metadata, "日报", "内容")
        self.assertEqual(smtp_send.call_count, 3)
        wechat_send.assert_called_once()
        self.assertEqual(metadata["delivery"]["email"]["status"], "failed")
        self.assertEqual(metadata["delivery"]["wechat"]["status"], "success")

    def test_beta_recipient_gets_private_email_without_personal_wechat(self):
        settings = SimpleNamespace(
            digest_to="",
            smtp_username="sender@example.com",
            smtp_password="secret",
            resend_key="",
            digest_from="",
            serverchan_sendkey="personal-wechat-key",
        )
        channels = _configured_delivery_channels(
            settings, "invitee@example.com", allow_wechat=False
        )
        self.assertEqual(channels, ["email"])

    @patch("trend_tracker.pipeline._process_digest", return_value="reports/user.md")
    @patch("trend_tracker.pipeline.make_services")
    def test_digest_reads_beta_users_from_supabase_without_legacy_json(
        self, make_services, process_digest
    ):
        beta_user = BetaUser(
            id="user-id",
            email="friend@example.com",
            display_name="朋友",
            timezone="Asia/Shanghai",
        )
        track = Track(
            id="track-id",
            beta_user_id=beta_user.id,
            name="AI 产品",
            goal="关注新的 AI 产品",
        )

        class Repository:
            def get_beta_users(self):
                return [beta_user]

            def has_beta_users(self):
                return True

            def get_recent_items(self, since):
                return []

            def get_tracks(self, beta_user_id=None):
                self.beta_user_id = beta_user_id
                return [track]

            def cleanup(self):
                return {}

            def storage_status(self):
                return {}

            def seed_beta_users(self, users):
                raise AssertionError("Daily digest must never import the legacy Secret")

        repository = Repository()
        make_services.return_value = (object(), repository, object())
        settings = SimpleNamespace(
            beta_max_users=20,
            beta_max_tracks_per_user=3,
            lookback_hours=30,
        )
        settings.beta_users = lambda: (_ for _ in ()).throw(
            AssertionError("Daily digest must never parse BETA_USERS_JSON")
        )

        self.assertEqual(digest(settings), "reports/user.md")
        self.assertEqual(repository.beta_user_id, beta_user.id)
        process_digest.assert_called_once()

    @patch("trend_tracker.pipeline._process_digest")
    @patch("trend_tracker.pipeline.make_services")
    def test_all_paused_beta_users_do_not_fall_back_to_personal_delivery(
        self, make_services, process_digest
    ):
        class Repository:
            def get_beta_users(self):
                return []

            def has_beta_users(self):
                return True

            def get_recent_items(self, since):
                return []

            def cleanup(self):
                return {}

            def storage_status(self):
                return {}

            def seed_tracks(self, tracks):
                raise AssertionError("Paused beta must not fall back to personal mode")

        make_services.return_value = (object(), Repository(), object())
        settings = SimpleNamespace(
            beta_max_users=20,
            beta_max_tracks_per_user=3,
            lookback_hours=30,
        )

        self.assertEqual(digest(settings), "")
        process_digest.assert_not_called()

    @patch("trend_tracker.pipeline.SupabaseRepository")
    @patch("trend_tracker.pipeline.HttpClient")
    def test_legacy_beta_import_is_explicit_and_reports_supabase_counts(
        self, http_client, repository_class
    ):
        seeds = [
            {
                "email": "friend@example.com",
                "tracks": [{"name": "AI 产品", "goal": "关注新产品"}],
            }
        ]
        user = BetaUser(id="user-id", email="friend@example.com")
        repository = repository_class.return_value
        repository.seed_beta_users.return_value = 1
        repository.get_beta_users.return_value = [user]
        repository.get_tracks.return_value = [
            Track(id="track-id", name="AI 产品", goal="关注新产品")
        ]
        settings = SimpleNamespace(
            supabase_url="https://project.supabase.co",
            supabase_key="sb_secret_example",
            beta_users=lambda: seeds,
            require_supabase=lambda: None,
        )

        self.assertEqual(import_beta_users(settings), 1)
        repository.seed_beta_users.assert_called_once_with(seeds)
        repository.get_tracks.assert_called_once_with(user.id)


class FakeHttp(HttpClient):
    def __init__(self, text):
        self.text = text

    def get_text(self, url, headers=None):
        return self.text


class ConnectorTests(unittest.TestCase):
    def test_atom_feed_parsing(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>item-1</id><title>New AI Agent</title>
            <link href="https://example.com/agent?utm_source=feed" rel="alternate"/>
            <summary><![CDATA[<p>A practical workflow agent.</p>]]></summary>
            <published>2026-07-16T10:00:00Z</published>
          </entry>
        </feed>"""
        config = {"key": "feed", "url": "https://example.com/feed", "max_items": 10}
        since = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
        items = collect_rss(config, FakeHttp(feed), since)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].external_id, "item-1")
        self.assertEqual(items[0].summary, "A practical workflow agent.")


class ConfigTests(unittest.TestCase):
    def test_detects_new_supabase_key_roles(self):
        self.assertEqual(supabase_key_role("sb_secret_example"), "service_role")
        self.assertEqual(supabase_key_role("sb_publishable_example"), "anon")

    def test_detects_legacy_anon_jwt(self):
        payload = base64.urlsafe_b64encode(json.dumps({"role": "anon"}).encode("utf-8")).decode("ascii").rstrip("=")
        self.assertEqual(supabase_key_role("e30.{0}.signature".format(payload)), "anon")

    def test_rejects_publishable_key_as_server_secret(self):
        old = os.environ.copy()
        try:
            os.environ["SUPABASE_URL"] = "https://project.supabase.co"
            os.environ["SUPABASE_SECRET_KEY"] = "sb_publishable_example"
            settings = Settings.from_env()
            with self.assertRaisesRegex(RuntimeError, "anon/publishable"):
                settings.require_supabase()
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_source_config_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text(json.dumps([{"type": "rss", "key": "x", "url": "https://example.com/feed"}]), encoding="utf-8")
            old = os.environ.copy()
            try:
                os.environ["SOURCE_CONFIG"] = str(path)
                settings = Settings.from_env()
                self.assertEqual(settings.sources()[0]["key"], "x")
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_provider_neutral_configuration(self):
        old = os.environ.copy()
        try:
            for name in (
                "OPENAI_API_KEY",
                "CHAT_API_KEY",
                "CHAT_BASE_URL",
                "EMBEDDING_API_KEY",
                "EMBEDDING_BASE_URL",
                "EMBEDDING_MODEL",
                "RANKING_MODEL",
                "ANALYSIS_MODEL",
            ):
                os.environ.pop(name, None)
            os.environ.update(
                {
                    "CHAT_API_KEY": "deepseek-key",
                    "CHAT_BASE_URL": "https://api.deepseek.com/",
                    "RANKING_MODEL": "deepseek-v4-flash",
                    "EMBEDDING_API_KEY": "glm-key",
                    "EMBEDDING_BASE_URL": "https://open.bigmodel.cn/api/paas/v4/",
                    "EMBEDDING_MODEL": "embedding-3",
                    "EMBEDDING_DIMENSIONS": "512",
                }
            )
            settings = Settings.from_env()
            self.assertEqual(settings.chat_base_url, "https://api.deepseek.com")
            self.assertEqual(settings.embedding_base_url, "https://open.bigmodel.cn/api/paas/v4")
            self.assertEqual(settings.analysis_model, "deepseek-v4-flash")
            self.assertEqual(settings.embedding_dimensions, 512)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_digest_presentation_configuration(self):
        old = os.environ.copy()
        try:
            os.environ.update(
                {
                    "REPORT_HIGHLIGHT_ITEMS": "3",
                    "REPORT_QUICK_ITEMS": "12",
                    "REPORT_RELEVANCE_THRESHOLD": "60",
                    "REPORT_SHOW_SCORES": "true",
                }
            )
            settings = Settings.from_env()
            self.assertEqual(settings.report_highlight_items, 3)
            self.assertEqual(settings.report_quick_items, 12)
            self.assertEqual(settings.report_relevance_threshold, 60)
            self.assertTrue(settings.report_show_scores)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_derives_immutable_email_asset_url_in_github_actions(self):
        old = os.environ.copy()
        try:
            os.environ.pop("DIGEST_ASSET_BASE_URL", None)
            os.environ["GITHUB_REPOSITORY"] = "example/ai-trend-tracker"
            os.environ["GITHUB_SHA"] = "abc123"
            settings = Settings.from_env()
            self.assertEqual(
                settings.digest_asset_base_url,
                "https://raw.githubusercontent.com/example/ai-trend-tracker/abc123/assets/email",
            )
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_explicit_email_asset_url_overrides_github_default(self):
        old = os.environ.copy()
        try:
            os.environ["DIGEST_ASSET_BASE_URL"] = "https://cdn.example.com/radar/"
            os.environ["GITHUB_REPOSITORY"] = "example/ai-trend-tracker"
            os.environ["GITHUB_SHA"] = "abc123"
            settings = Settings.from_env()
            self.assertEqual(
                settings.digest_asset_base_url,
                "https://cdn.example.com/radar",
            )
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_parses_invite_only_beta_users(self):
        old = os.environ.copy()
        try:
            os.environ["BETA_USERS_JSON"] = json.dumps(
                [
                    {
                        "email": "Friend@Example.com",
                        "display_name": "朋友",
                        "tracks": [{"name": "AI Coding", "goal": "关注新工具"}],
                    }
                ]
            )
            settings = Settings.from_env()
            users = settings.beta_users()
            self.assertEqual(users[0]["email"], "friend@example.com")
            self.assertEqual(users[0]["tracks"][0]["goal"], "关注新工具")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_repairs_common_beta_users_json_copy_paste_errors(self):
        old = os.environ.copy()
        try:
            os.environ["BETA_USERS_JSON"] = """
            [
              {
                “email”: “friend@example.com”，
                “display_name”: “朋友”，
                “tracks”: [
                  {
                    “name”: “AI Coding”
                    “goal”: “关注新工具”，
                  }，
                ]，
              }
              {
                “email”: “second@example.com”，
                “tracks”: [
                  {
                    “name”: “AI 产品”，
                    “goal”: “关注产品信号”
                  }
                ]
              }
            ]
            """
            settings = Settings.from_env()
            users = settings.beta_users()
            self.assertEqual(
                [user["email"] for user in users],
                ["friend@example.com", "second@example.com"],
            )
            self.assertEqual(users[0]["tracks"][0]["goal"], "关注新工具")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_invalid_beta_users_json_reports_location_without_content(self):
        old = os.environ.copy()
        try:
            os.environ["BETA_USERS_JSON"] = """
            [
              {
                "email": "private@example.com",
                "tracks": @
              }
            ]
            """
            settings = Settings.from_env()
            with self.assertRaises(ValueError) as context:
                settings.beta_users()
            message = str(context.exception)
            self.assertIn("near line", message)
            self.assertIn("column", message)
            self.assertNotIn("private@example.com", message)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_legacy_openai_configuration_remains_supported(self):
        old = os.environ.copy()
        try:
            for name in (
                "CHAT_API_KEY",
                "CHAT_BASE_URL",
                "EMBEDDING_API_KEY",
                "EMBEDDING_BASE_URL",
                "EMBEDDING_MODEL",
                "RANKING_MODEL",
                "ANALYSIS_MODEL",
            ):
                os.environ.pop(name, None)
            os.environ["OPENAI_API_KEY"] = "legacy-key"
            settings = Settings.from_env()
            self.assertEqual(settings.chat_api_key, "legacy-key")
            self.assertEqual(settings.chat_base_url, "https://api.openai.com/v1")
            self.assertEqual(settings.embedding_model, "text-embedding-3-small")
        finally:
            os.environ.clear()
            os.environ.update(old)


class RepositoryTests(unittest.TestCase):
    def test_new_supabase_secret_key_is_not_sent_as_bearer_jwt(self):
        repository = SupabaseRepository("https://project.supabase.co", "sb_secret_example")
        self.assertEqual(repository.headers["apikey"], "sb_secret_example")
        self.assertNotIn("Authorization", repository.headers)

    def test_legacy_service_role_jwt_is_sent_as_bearer(self):
        repository = SupabaseRepository("https://project.supabase.co", "legacy.jwt.value")
        self.assertEqual(repository.headers["Authorization"], "Bearer legacy.jwt.value")


class RecordingHttp:
    def __init__(self):
        self.calls = []

    def post_json(self, url, payload, headers=None):
        self.calls.append((url, payload, headers))
        if url.endswith("/embeddings"):
            return {"data": [{"index": 0, "embedding": [1.0] + [0.0] * 511}]}
        return {"choices": [{"message": {"content": "{\"results\": []}"}}]}


class AIServiceTests(unittest.TestCase):
    def make_service(self, http=None):
        return AIService(
            chat_api_key="chat-key",
            chat_base_url="https://api.deepseek.com",
            embedding_api_key="embedding-key",
            embedding_base_url="https://open.bigmodel.cn/api/paas/v4",
            embedding_model="embedding-3",
            embedding_dimensions=512,
            ranking_model="deepseek-v4-flash",
            analysis_model="deepseek-v4-pro",
            http=http or RecordingHttp(),
        )

    def test_uses_separate_provider_endpoints(self):
        http = RecordingHttp()
        service = self.make_service(http)
        vectors = service.embeddings(["hello"])
        self.assertEqual(len(vectors[0]), 512)
        self.assertEqual(vectors[0][0], 1.0)
        self.assertEqual(http.calls[0][0], "https://open.bigmodel.cn/api/paas/v4/embeddings")

    def test_parses_fenced_json_from_compatible_provider(self):
        value = AIService._parse_json_content("```json\n{\"analysis\":\"ok\"}\n```")
        self.assertEqual(value["analysis"], "ok")

    def test_daily_brief_synthesizes_all_signals_and_avoids_empty_conclusion(self):
        class BriefHttp(RecordingHttp):
            def post_json(self, url, payload, headers=None):
                self.calls.append((url, payload, headers))
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "today_change": "同类产品正在分化为低代码编排和可观测性两条路线。",
                                        "why_it_matters": "需要先确定当前瓶颈是搭建效率还是运行稳定性。",
                                        "next_action": "用一个现有流程分别验证两类产品。",
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }

        http = BriefHttp()
        service = self.make_service(http)
        track = Track(id="t", name="智能体工作流", goal="寻找可落地的智能体工具")
        item_one = SourceItem(
            id="one",
            source_key="github",
            external_id="one",
            title="Builder",
            url="https://example.com/builder",
            summary="低代码编排智能体",
        )
        item_two = SourceItem(
            id="two",
            source_key="producthunt",
            external_id="two",
            title="Observer",
            url="https://example.com/observer",
            summary="智能体可观测性",
        )
        matches = [
            MatchResult("t", "one", 92, 0.8, "high", "降低搭建成本", item_one),
            MatchResult("t", "two", 88, 0.7, "high", "提高运行稳定性", item_two),
        ]
        brief = service.daily_brief(
            [track],
            {"t": matches},
            {"t": "本期信号呈现不同产品路线。"},
        )
        self.assertIn("分化", brief["today_change"])
        request = http.calls[0][1]
        self.assertIn("never merely rewrite the highest-scoring item", request["messages"][0]["content"])
        self.assertIn("do not say that there is no consensus", request["messages"][0]["content"])
        self.assertIn("within 45 Chinese characters", request["messages"][0]["content"])
        self.assertIn("within 32 Chinese characters", request["messages"][0]["content"])
        self.assertIn("must start with a verb", request["messages"][0]["content"])
        payload = json.loads(request["messages"][1]["content"])
        self.assertEqual(len(payload["signals"]), 2)

    def test_daily_brief_replaces_model_no_consensus_with_useful_fallback(self):
        class NoConsensusHttp(RecordingHttp):
            def post_json(self, url, payload, headers=None):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "today_change": "今天没有趋势，暂未形成共识。",
                                        "why_it_matters": "继续观察。",
                                        "next_action": "等待。",
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }

        service = self.make_service(NoConsensusHttp())
        track = Track(id="t", name="AI 产品", goal="寻找新产品")
        item = SourceItem(
            id="one",
            source_key="github",
            external_id="one",
            title="New Tool",
            url="https://example.com/tool",
        )
        match = MatchResult("t", "one", 90, 0.8, "high", "真实任务评测开始产品化", item)
        brief = service.daily_brief([track], {"t": [match]}, {"t": ""})
        self.assertNotIn("共识", brief["today_change"])
        self.assertNotIn("没有趋势", brief["today_change"])
        self.assertIn("出现新变化", brief["today_change"])

    def test_deep_analysis_uses_decision_structure_without_repetition(self):
        class AnalysisHttp(RecordingHttp):
            def post_json(self, url, payload, headers=None):
                self.calls.append((url, payload, headers))
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "analysis": (
                                            "- **新在哪里：** 支持真实任务回放。\n"
                                            "- **判断依据：** README 展示轨迹导入能力。\n"
                                            "- **适用边界：** 尚缺团队采用证据。\n"
                                            "- **先验证：** 导入一条现有任务。"
                                        )
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }

        http = AnalysisHttp()
        service = self.make_service(http)
        track = Track(id="t", name="Agent 评测", goal="寻找真实任务评测工具")
        item = SourceItem(
            id="one",
            source_key="github",
            external_id="one",
            title="TraceBench",
            url="https://example.com/tracebench",
            summary="支持真实任务轨迹回放",
        )
        analysis = service.analyze(
            track,
            MatchResult("t", "one", 90, 0.8, "high", "支持任务评测", item),
        )
        self.assertIn("新在哪里", analysis)
        self.assertIn("适用边界", analysis)
        prompt = http.calls[0][1]["messages"][0]["content"]
        self.assertNotIn("核心价值", prompt)
        self.assertIn("exactly four bullet lines", prompt)
        self.assertIn("exactly one action", prompt)

    def test_trend_summary_uses_evidence_chain_labels(self):
        class TrendHttp(RecordingHttp):
            def post_json(self, url, payload, headers=None):
                self.calls.append((url, payload, headers))
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "summary": (
                                            "- **趋势判断：** 评测正转向真实任务回放。\n"
                                            "- **判断依据：** 两个独立项目提供轨迹对照能力。\n"
                                            "- **尚待验证：** 尚缺规模化采用证据。\n"
                                            "- **接下来观察：** 是否出现公开客户案例。"
                                        )
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }

        http = TrendHttp()
        service = self.make_service(http)
        track = Track(id="t", name="Agent 评测", goal="寻找真实任务评测工具")
        item = SourceItem(
            id="one",
            source_key="github",
            external_id="one",
            title="TraceBench",
            url="https://example.com/tracebench",
            summary="支持真实任务轨迹回放",
        )
        summary = service.trend_summary(
            track,
            [MatchResult("t", "one", 90, 0.8, "high", "支持任务评测", item)],
        )
        self.assertIn("判断依据", summary)
        self.assertIn("尚待验证", summary)
        self.assertIn("接下来观察", summary)
        prompt = http.calls[0][1]["messages"][0]["content"]
        self.assertNotIn("已确认信号", prompt)
        self.assertIn("names concrete independent signals", prompt)
        self.assertIn("observable event or evidence trigger", prompt)


class FeedbackTokenTests(unittest.TestCase):
    def test_signed_feedback_round_trip_and_fragment_url(self):
        token = sign_feedback_token(
            "secret", "user-id", "track-id", "item-id", "helpful", expires_at=200
        )
        payload = verify_feedback_token("secret", token, now=100)
        self.assertEqual(payload["a"], "helpful")
        url = feedback_url(
            "https://owner.github.io/repo/feedback.html",
            "https://project.supabase.co/functions/v1/feedback",
            token,
        )
        self.assertIn("?api=", url)
        self.assertIn("#", url)
        self.assertNotIn("token=", url)

    def test_rejects_tampered_or_expired_feedback(self):
        token = sign_feedback_token(
            "secret", "user-id", "track-id", "item-id", "irrelevant", expires_at=100
        )
        with self.assertRaisesRegex(ValueError, "signature"):
            verify_feedback_token("wrong-secret", token, now=50)
        with self.assertRaisesRegex(ValueError, "expired"):
            verify_feedback_token("secret", token, now=101)


class DeliveryHttp:
    def __init__(self):
        self.json_calls = []
        self.form_calls = []

    def post_json(self, url, payload, headers=None):
        self.json_calls.append((url, payload, headers))
        return {"id": "email-id"}

    def post_form(self, url, payload, headers=None):
        self.form_calls.append((url, payload, headers))
        return {"code": 0, "data": {"pushid": "wechat-id"}}


class DeliveryTests(unittest.TestCase):
    def test_markdown_links_are_clickable_in_email_html(self):
        rendered = markdown_email_html("Read [Product](https://example.com/tool?a=1&b=2)")
        self.assertIn('<a href="https://example.com/tool?a=1&amp;b=2"', rendered)
        self.assertIn(">Product</a>", rendered)

    def test_email_html_renders_digest_structure(self):
        rendered = markdown_email_html(
            "# AI 趋势日报\n\n"
            "## 30 秒结论\n\n"
            "- **新信号：** 评测正在转向真实任务\n"
            "- **与你有关：** 选型标准需要改变\n"
            "- **今天可做：** 立即测试"
        )
        self.assertIn("<h1>AI 趋势日报</h1>", rendered)
        self.assertIn("<h2>30 秒结论</h2>", rendered)
        self.assertIn('class="brief-row brief-signal"', rendered)
        self.assertIn('class="brief-row brief-relevance"', rendered)
        self.assertIn('class="brief-row brief-action"', rendered)
        self.assertIn('class="brief-glyph"', rendered)
        self.assertIn(">今天可做</strong>", rendered)
        self.assertIn('class="conclusion-whisper"', rendered)
        self.assertIn("把时间留给判断，而不是寻找", rendered)
        self.assertNotIn("<pre", rendered)

    def test_email_html_renders_navigation_and_section_anchors(self):
        rendered = markdown_email_html(
            "> [今日重点 3 条](#highlights) · [趋势雷达](#trends)\n\n"
            "## 今日重点情报\n\n## 趋势雷达"
        )
        self.assertIn('href="#highlights"', rendered)
        self.assertIn('id="highlights"', rendered)
        self.assertIn('id="trends"', rendered)
        self.assertIn('class="trend-orbit-map"', rendered)
        self.assertIn('class="probe-arm"', rendered)
        self.assertIn("@keyframes probe-orbit", rendered)
        self.assertIn('class="radar-horizon"', rendered)
        self.assertIn('class="horizon-sweep"', rendered)
        self.assertIn('class="horizon-promise"', rendered)
        self.assertIn("世界持续加速", rendered)
        self.assertIn("您始终先看一步", rendered)
        self.assertIn("@keyframes horizon-sweep", rendered)

    def test_email_html_uses_remote_motion_with_static_fallbacks(self):
        rendered = markdown_email_html(
            "# AI 趋势日报\n\n## 趋势雷达\n\n- **趋势判断：** Agent 正在走向落地",
            "https://cdn.example.com/email?a=1&b=2",
        )
        self.assertIn(
            'src="https://cdn.example.com/email?a=1&amp;b=2/signal-spectrum.gif"',
            rendered,
        )
        self.assertIn("trend-radar.gif", rendered)
        self.assertIn("radar-horizon.gif", rendered)
        self.assertIn("signal-spectrum-static.png", rendered)
        self.assertIn("trend-radar-static.png", rendered)
        self.assertIn("radar-horizon-static.png", rendered)
        self.assertIn("<!--[if mso]>", rendered)
        self.assertIn('class="motion-gif"', rendered)
        self.assertIn('class="motion-static"', rendered)
        self.assertIn("prefers-reduced-motion:reduce", rendered)
        self.assertIn("Agent 正在走向落地", rendered)
        self.assertLess(len(rendered.encode("utf-8")), 80 * 1024)

    def test_email_html_renders_related_signals_as_observation_log(self):
        rendered = markdown_email_html(
            "## 其他相关信号\n\n"
            "### Agent 工具\n\n"
            "- **[产品 A](https://example.com/a)** — 编排工作流\n"
            "- **[产品 B](https://example.com/b)** — 评测工作流\n"
            "- **[产品 C](https://example.com/c)** — 观测工作流"
        )
        self.assertIn('class="log-scanner"', rendered)
        self.assertIn('class="related-whisper"', rendered)
        self.assertIn("不追逐共识，而是发现价值", rendered)
        self.assertIn('class="signal-log"', rendered)
        self.assertIn('class="related-signal-row wave-short"', rendered)
        self.assertIn('class="related-signal-row wave-medium"', rendered)
        self.assertIn('class="related-signal-row wave-long"', rendered)
        self.assertIn('class="log-index">01</span>', rendered)
        self.assertIn('href="https://example.com/a"', rendered)

    def test_email_html_renders_highlight_as_decision_card(self):
        rendered = markdown_email_html(
            "## 今日重点情报\n\n"
            "### Agent 工具\n\n"
            "#### 1. [TraceBench](https://example.com/product)\n\n"
            "- **一句话看懂：** 真实任务轨迹回放工具\n"
            "- **与你有关：** 帮助验证 Agent 工作流\n\n"
            "**深度判断**\n\n"
            "- **新在哪里：** 从榜单转向真实任务\n"
            "- **判断依据：** README 展示轨迹导入\n"
            "- **适用边界：** 尚缺采用数据\n"
            "- **先验证：** 导入一条任务\n\n"
            "> **来源** GitHub · **发布于** 07月27日 07:42 · "
            "[查看原始资料 ↗](https://example.com/source)"
        )
        self.assertIn('class="signal-facts"', rendered)
        self.assertIn('class="analysis-kicker"', rendered)
        self.assertIn('class="premium-analysis-badge"', rendered)
        self.assertIn("PREMIUM MODEL · 深度推理", rendered)
        self.assertIn('class="analysis-grid"', rendered)
        self.assertIn('class="signal-source-meta"', rendered)
        self.assertIn(">来源</strong> GitHub", rendered)
        self.assertIn(">发布于</strong> 07月27日 07:42", rendered)
        self.assertIn(">查看原始资料 ↗</a>", rendered)
        self.assertNotIn('class="meta feedback-bar"', rendered)

    def test_email_html_uses_dark_signal_radar_product_shell(self):
        rendered = markdown_email_html(
            "# AI 趋势日报\n\n"
            "> 今日扫描 **120** 条信息 · 发现相关 **8** 条 · 重点解读 **3** 条 · 深度分析 **1** 条\n\n"
            "## 30 秒结论\n\n"
            "- **今天可做：** 立即测试\n\n"
            "## 今日重点情报\n\n"
            "### 企业 Agent\n\n"
            "#### 1. [评测工具](https://example.com/tool)\n\n"
            "- **是什么：** 一个工具"
        )
        self.assertIn('bgcolor="#040912"', rendered)
        self.assertIn('class="brand-name"', rendered)
        self.assertIn(">SIGNAL RADAR</span>", rendered)
        self.assertIn(".email-container{width:100%;max-width:700px;text-align:left", rendered)
        self.assertIn('class="email-content" align="left"', rendered)
        self.assertIn("padding:0 32px 28px;text-align:left", rendered)
        self.assertIn('class="signal-spectrum"', rendered)
        self.assertIn('class="spectrum-scan"', rendered)
        self.assertIn("@keyframes signal-scan", rendered)
        self.assertIn('class="observatory-frame"', rendered)
        self.assertIn('class="telemetry-rail rail-left"', rendered)
        self.assertIn('class="telemetry-rail rail-right"', rendered)
        self.assertEqual(rendered.count('class="rail-particle '), 3)
        self.assertIn('class="signal-packet"', rendered)
        self.assertIn('class="signal-flare"', rendered)
        self.assertIn("@keyframes output-packet", rendered)
        self.assertIn("min-width:821px", rendered)
        self.assertIn("prefers-reduced-motion:reduce", rendered)
        self.assertIn('class="signal-pipeline"', rendered)
        self.assertIn('class="pipeline-stage stage-raw"', rendered)
        self.assertIn('class="pipeline-stage stage-depth"', rendered)
        self.assertIn('class="pipeline-note"', rendered)
        self.assertIn("多源广域扫描", rendered)
        self.assertIn("只取最新信号", rendered)
        self.assertIn('class="conclusion-whisper"', rendered)
        self.assertIn("把时间留给判断，而不是寻找", rendered)
        self.assertIn('class="horizon-promise"', rendered)
        self.assertIn("世界持续加速", rendered)
        self.assertIn("您始终先看一步", rendered)
        self.assertNotIn('class="brand-promise"', rendered)
        self.assertNotIn('class="brand-value-strip"', rendered)
        self.assertIn(">120</strong>", rendered)
        self.assertIn(">8</strong>", rendered)
        self.assertIn('class="signal-index">01</span>', rendered)
        self.assertNotIn('class="signal-tier"', rendered)
        self.assertIn("今天可做", rendered)
        self.assertIn('class="content-section section-conclusion"', rendered)
        self.assertIn('class="signal-card"', rendered)
        self.assertNotIn("background:#f4f6f8", rendered)
        self.assertNotIn('class="action-row"', rendered)
        self.assertNotIn(".section-conclusion .action-row", rendered)
        self.assertNotIn(".signal-card .action-row", rendered)
        self.assertNotIn("#f2bf7d", rendered)
        self.assertNotIn("#e8b875", rendered)
        self.assertIn("一个工具", rendered)

    def test_splits_multiple_email_recipients(self):
        self.assertEqual(
            email_recipients("personal@qq.com; work@example.com,third@example.com"),
            ["personal@qq.com", "work@example.com", "third@example.com"],
        )

    def test_sends_one_email_to_multiple_recipients(self):
        http = DeliveryHttp()
        send_email(
            http,
            "re_key",
            "Digest <digest@example.com>",
            "personal@qq.com,work@example.com",
            "Daily",
            "Report",
            "https://cdn.example.com/email",
        )
        self.assertEqual(http.json_calls[0][1]["to"], ["personal@qq.com", "work@example.com"])
        self.assertIn("signal-spectrum.gif", http.json_calls[0][1]["html"])

    def test_sends_markdown_report_to_serverchan(self):
        http = DeliveryHttp()
        send_wechat(http, "SCTexample", "Daily", "# Report")
        self.assertEqual(http.form_calls[0][0], "https://sctapi.ftqq.com/SCTexample.send")
        self.assertEqual(http.form_calls[0][1]["desp"], "# Report")

    @patch("trend_tracker.report.smtplib.SMTP_SSL")
    def test_sends_smtp_email_to_multiple_recipients(self, smtp_ssl):
        server = smtp_ssl.return_value.__enter__.return_value
        send_smtp_email(
            "smtp.qq.com",
            465,
            "sender@qq.com",
            "authorization-code",
            "personal@qq.com,work@example.com",
            "Daily",
            "# Report",
        )
        server.login.assert_called_once_with("sender@qq.com", "authorization-code")
        self.assertEqual(server.send_message.call_args.kwargs["to_addrs"], ["personal@qq.com", "work@example.com"])


if __name__ == "__main__":
    unittest.main()
