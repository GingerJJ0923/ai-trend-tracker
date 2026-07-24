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
from trend_tracker.models import MatchResult, SourceItem, Track
from trend_tracker.pipeline import (
    _configured_delivery_channels,
    _deliver_report,
    _run_with_retries,
    deduplicate_observations,
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
        match = MatchResult("t", "1", 88, 0.7, "high", "Directly relevant", item, "Try it on one task.")
        report = build_report(datetime.now(timezone.utc), 10, [track], {"t": [match]}, {"t": "Agent tools are becoming more actionable."})
        self.assertIn("Directly relevant", report)
        self.assertIn("https://example.com/tool", report)
        self.assertIn("进一步判断", report)
        self.assertIn("30 秒结论", report)
        self.assertIn("今日重点情报", report)
        self.assertIn("趋势雷达", report)
        self.assertIn("其他相关信号", report)
        self.assertNotIn("88 分", report)
        self.assertNotIn("Why it matches", report)

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
        rendered = markdown_email_html("# AI 趋势日报\n\n## 30 秒结论\n\n- **建议动作：** 立即测试")
        self.assertIn("<h1>AI 趋势日报</h1>", rendered)
        self.assertIn("<h2>30 秒结论</h2>", rendered)
        self.assertIn("<strong>建议动作：</strong>", rendered)
        self.assertNotIn("<pre", rendered)

    def test_email_html_renders_navigation_and_section_anchors(self):
        rendered = markdown_email_html(
            "> [今日重点 3 条](#highlights) · [趋势雷达](#trends)\n\n"
            "## 今日重点情报\n\n## 趋势雷达"
        )
        self.assertIn('href="#highlights"', rendered)
        self.assertIn('id="highlights"', rendered)
        self.assertIn('id="trends"', rendered)

    def test_splits_multiple_email_recipients(self):
        self.assertEqual(
            email_recipients("personal@qq.com; work@example.com,third@example.com"),
            ["personal@qq.com", "work@example.com", "third@example.com"],
        )

    def test_sends_one_email_to_multiple_recipients(self):
        http = DeliveryHttp()
        send_email(http, "re_key", "Digest <digest@example.com>", "personal@qq.com,work@example.com", "Daily", "Report")
        self.assertEqual(http.json_calls[0][1]["to"], ["personal@qq.com", "work@example.com"])

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
