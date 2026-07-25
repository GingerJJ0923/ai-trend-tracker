import html
import re
import smtplib
import ssl
import urllib.parse
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List
from zoneinfo import ZoneInfo

from .http import HttpClient
from .models import MatchResult, Track


def build_report(
    generated_at: datetime,
    scanned_count: int,
    tracks: List[Track],
    matches_by_track: Dict[str, List[MatchResult]],
    trends_by_track: Dict[str, str],
    highlight_items: int = 3,
    quick_items: int = 12,
    relevance_threshold: int = 50,
    show_scores: bool = False,
    timezone_name: str = "Asia/Shanghai",
) -> str:
    try:
        local_time = generated_at.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError):
        local_time = generated_at
    weekdays = "一二三四五六日"
    date_label = "{0}年{1}月{2}日 星期{3}".format(
        local_time.year,
        local_time.month,
        local_time.day,
        weekdays[local_time.weekday()],
    )
    relevant_by_track = {
        track.id: [
            match
            for match in matches_by_track.get(track.id, [])
            if match.score >= relevance_threshold
        ]
        for track in tracks
    }
    all_relevant = [match for track in tracks for match in relevant_by_track[track.id]]
    analysis_count = sum(1 for match in all_relevant if match.analysis)
    featured = max(all_relevant, key=lambda match: match.score) if all_relevant else None
    highlight_count = sum(
        min(len(relevant_by_track[track.id]), highlight_items) for track in tracks
    )
    quick_count = sum(
        min(max(0, len(relevant_by_track[track.id]) - highlight_items), quick_items)
        for track in tracks
    )

    lines = [
        "# AI 趋势日报｜{0}".format(date_label),
        "",
        "> 今日扫描 **{0}** 条信息 · 发现相关 **{1}** 条 · 重点解读 **{2}** 条 · 深度分析 **{3}** 条".format(
            scanned_count,
            len(all_relevant),
            highlight_count,
            analysis_count,
        ),
        "",
        "> [今日重点 {0} 条](#highlights) · [趋势雷达](#trends) · [其他相关 {1} 条](#related)".format(
            highlight_count,
            quick_count,
        ),
        "",
        "## 30 秒结论",
        "",
    ]
    if featured:
        title = featured.display_title or featured.item.title
        tier = "高度相关" if featured.score >= 80 else "值得关注"
        score = " · {0:.0f} 分".format(featured.score) if show_scores else ""
        lines.extend(
            [
                "- **今天最值得关注：** {0}（{1}{2}）".format(title, tier, score),
                "- **与你的关系：** {0}".format(featured.reason or "与当前关注目标具有较高相关性。"),
                "- **建议动作：** {0}".format(featured.next_action or "先阅读原始资料，再决定是否投入进一步测试。"),
                "",
            ]
        )
    else:
        lines.extend(["本次采集没有信息达到相关性门槛，建议暂不行动，等待下一轮信号。", ""])

    lines.extend(["## 今日重点情报", ""])
    for track in tracks:
        relevant = relevant_by_track[track.id]
        high = [match for match in relevant if match.score >= 80]
        lines.extend(
            [
                "### {0}".format(track.name),
                "",
                "发现 **{0}** 条相关信息，其中 **{1}** 条高度相关；以下只展开最值得阅读的 {2} 条。".format(
                    len(relevant), len(high), min(len(relevant), highlight_items)
                ),
                "",
            ]
        )
        if not relevant:
            lines.extend(["暂时没有信息达到相关性门槛。", ""])
            continue
        for index, match in enumerate(relevant[:highlight_items], 1):
            target = match.item.product_url or match.item.url
            title = match.display_title or match.item.title
            tier = "高度相关" if match.score >= 80 else "值得关注"
            score = " · {0:.0f} 分".format(match.score) if show_scores else ""
            published = match.item.published_at.astimezone(local_time.tzinfo)
            lines.extend(
                [
                    "#### {0}. [{1}]({2})｜{3}{4}".format(index, title, target, tier, score),
                    "",
                    "- **是什么：** {0}".format(match.concise_summary or "请查看原始页面了解产品或技术详情。"),
                    "- **为什么值得看：** {0}".format(match.reason or "与当前关注目标具有直接关联。"),
                    "- **建议动作：** {0}".format(match.next_action or "先用一个真实的小任务验证效果。"),
                    "- **来源与时间：** `{0}` · {1}".format(match.item.source_key, published.strftime("%m月%d日 %H:%M")),
                    "- **原始证据：** [查看来源]({0})".format(match.item.url),
                ]
            )
            if match.analysis:
                lines.extend(["", "**进一步判断**", "", match.analysis])
            if match.feedback_links:
                lines.extend(
                    [
                        "",
                        "> 这条推荐对你如何？[有用]({0}) · [不相关]({1}) · [继续深挖]({2})".format(
                            match.feedback_links.get("helpful", ""),
                            match.feedback_links.get("irrelevant", ""),
                            match.feedback_links.get("deep_dive", ""),
                        ),
                    ]
                )
            lines.append("")

    lines.extend(["## 趋势雷达", ""])
    for track in tracks:
        lines.extend(
            [
                "### {0}".format(track.name),
                "",
                trends_by_track.get(track.id, "暂未生成趋势研判。"),
                "",
            ]
        )

    lines.extend(["## 其他相关信号", ""])
    for track in tracks:
        remaining = relevant_by_track[track.id][highlight_items:]
        displayed = remaining[:quick_items]
        lines.extend(["### {0}".format(track.name), ""])
        if not remaining:
            lines.extend(["除今日重点外，暂时没有更多相关信号。", ""])
            continue
        for match in displayed:
            target = match.item.product_url or match.item.url
            title = match.display_title or match.item.title
            summary = match.concise_summary or "与当前关注目标相关，点击查看原始资料。"
            tier = "高度相关" if match.score >= 80 else "值得关注"
            score = " · {0:.0f} 分".format(match.score) if show_scores else ""
            lines.append(
                "- **[{0}]({1})** — {2} · {3}{4}".format(
                    title,
                    target,
                    summary,
                    tier,
                    score,
                )
            )
        hidden_count = len(remaining) - len(displayed)
        if hidden_count > 0:
            lines.extend(
                [
                    "",
                    "另有 **{0}** 条相关信号已完成筛选并保留在数据库中，本期邮件不展开。".format(
                        hidden_count
                    ),
                ]
            )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_report(content: str, directory: str = "reports", suffix: str = "") -> Path:
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    safe_suffix = re.sub(r"[^A-Za-z0-9_-]+", "-", suffix).strip("-")
    filename = datetime.now().strftime("%Y-%m-%d")
    if safe_suffix:
        filename += "-" + safe_suffix
    report_path = path / (filename + ".md")
    report_path.write_text(content, encoding="utf-8")
    return report_path


def email_recipients(value: str) -> List[str]:
    return [row.strip() for row in re.split(r"[,;\n]+", value or "") if row.strip()]


def _inline_markdown(value: str) -> str:
    parts = []
    position = 0
    pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|#[A-Za-z0-9_-]+)\)")
    for match in pattern.finditer(value):
        parts.append(html.escape(value[position : match.start()]))
        label = html.escape(match.group(1))
        url = html.escape(match.group(2), quote=True)
        parts.append('<a href="{0}">{1}</a>'.format(url, label))
        position = match.end()
    parts.append(html.escape(value[position:]))
    rendered = "".join(parts)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)


def markdown_email_html(report: str) -> str:
    """Render the digest's small Markdown subset as a dark, email-safe product UI."""
    blocks = []
    list_open = False
    section_open = False
    card_open = False

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            blocks.append("</ul>")
            list_open = False

    def close_card() -> None:
        nonlocal card_open
        close_list()
        if card_open:
            blocks.append("</div>")
            card_open = False

    def close_section() -> None:
        nonlocal section_open
        close_card()
        if section_open:
            blocks.append("</section>")
            section_open = False

    for raw_line in report.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            if not list_open:
                blocks.append("<ul>")
                list_open = True
            blocks.append("<li>{0}</li>".format(_inline_markdown(line[2:])))
            continue
        close_list()
        if not line:
            continue
        if line.startswith("# "):
            close_section()
            blocks.append("<h1>{0}</h1>".format(_inline_markdown(line[2:])))
        elif line.startswith("## "):
            close_section()
            title = line[3:]
            section_ids = {
                "今日重点情报": "highlights",
                "趋势雷达": "trends",
                "其他相关信号": "related",
            }
            section_classes = {
                "30 秒结论": "section-conclusion",
                "今日重点情报": "section-highlights",
                "趋势雷达": "section-trends",
                "其他相关信号": "section-related",
            }
            blocks.append(
                '<section class="content-section {0}">'.format(
                    section_classes.get(title, "")
                )
            )
            section_open = True
            section_id = section_ids.get(title, "")
            id_attribute = ' id="{0}"'.format(section_id) if section_id else ""
            blocks.append("<h2{0}>{1}</h2>".format(id_attribute, _inline_markdown(title)))
        elif line.startswith("### "):
            close_card()
            blocks.append("<h3>{0}</h3>".format(_inline_markdown(line[4:])))
        elif line.startswith("#### "):
            close_card()
            blocks.append('<div class="signal-card">')
            card_open = True
            blocks.append("<h4>{0}</h4>".format(_inline_markdown(line[5:])))
        elif line.startswith("> "):
            blocks.append("<div class=\"meta\">{0}</div>".format(_inline_markdown(line[2:])))
        else:
            blocks.append("<p>{0}</p>".format(_inline_markdown(line)))
    close_section()
    content = "\n".join(blocks)
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>AI 趋势日报</title>
<style>
html,body{margin:0!important;padding:0!important;width:100%!important;background:#050b13!important;color:#dce9f3!important}
body,table,td,p,a,li,h1,h2,h3,h4{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif}
table{border-collapse:separate;border-spacing:0}a{color:#5ddcff;text-decoration:none;font-weight:650}strong{color:#f4f9fd}
.email-shell{width:100%;background:#050b13}.email-outer{padding:28px 14px 42px}.email-container{width:100%;max-width:720px;background:#081321;border:1px solid #193047;border-radius:18px;overflow:hidden;box-shadow:0 24px 70px rgba(0,0,0,.34)}
.brand-bar{padding:18px 28px;border-bottom:1px solid #193047;background:#07111e}.brand-mark{display:inline-block;width:8px;height:8px;margin-right:9px;border-radius:50%;background:#4ed9ff;box-shadow:0 0 12px #4ed9ff;vertical-align:1px}.brand-name{color:#8ceaff;font-size:12px;font-weight:800;letter-spacing:.16em}.brand-note{float:right;color:#526d82;font-size:10px;font-weight:500;letter-spacing:.06em}
.email-content{padding:30px 30px 22px;color:#9fb3c3;font-size:14px;line-height:1.75;mso-line-height-rule:exactly}
h1{margin:0 0 16px;color:#f5f9fc;font-size:28px;line-height:1.32;letter-spacing:-.035em}h2{margin:0 0 17px;color:#eef7fc;font-size:20px;line-height:1.4;letter-spacing:-.02em}h3{margin:24px 0 11px;color:#91e7ff;font-size:16px;line-height:1.45}h4{margin:0 0 13px;color:#eef8fd;font-size:16px;line-height:1.5}
p{margin:8px 0;color:#96aabd}.content-section{margin:30px 0 0;padding-top:26px;border-top:1px solid #193047}.section-conclusion{margin:20px 0 0;padding:24px 24px 22px;border:1px solid #20506a;border-radius:14px;background:#0b1c2c}.section-conclusion h2{color:#73e2ff}.section-conclusion ul{margin-bottom:0}.section-conclusion li{padding:7px 0;border-bottom:1px solid #163247}.section-conclusion li:last-child{border-bottom:0}
.meta{margin:11px 0 16px;padding:12px 14px;border:1px solid #193b54;border-radius:10px;background:#0b1b2a;color:#7695aa;font-size:12px}.meta a{display:inline-block;margin:3px 3px 3px 0;padding:5px 9px;border:1px solid #23516b;border-radius:999px;background:#0d2436;color:#70defa;font-size:11px;white-space:nowrap}
.signal-card{margin:12px 0 16px;padding:20px 20px 17px;border:1px solid #19364e;border-radius:13px;background:#0a1827}.signal-card:hover{border-color:#2d627e}.signal-card .meta{margin:14px 0 0;background:#0c2132}.signal-card .meta a{border-color:#2c6079;background:#0e293c}.signal-card ul{margin-top:10px}.signal-card li{padding:4px 0}
ul{margin:8px 0 15px;padding:0 0 0 20px}li{margin:5px 0;color:#9fb3c3}.section-related ul{margin-top:5px}.section-related li{margin:0;padding:10px 0;border-bottom:1px solid #142b3e}.section-related li:last-child{border-bottom:0}
.section-trends p{padding:15px 17px;border:1px solid #173247;border-radius:10px;background:#091725;color:#9eb3c3}.section-trends strong{color:#71dfff}
.email-footer{height:16px;border-top:1px solid #13293c;background:#07111d}
@media only screen and (max-width:600px){
.email-outer{padding:0!important}.email-container{border-right:0!important;border-left:0!important;border-radius:0!important}.brand-bar{padding:15px 18px!important}.brand-note{display:none!important}.email-content{padding:24px 18px 18px!important;font-size:14px!important}.section-conclusion{padding:20px 17px!important}.signal-card{padding:18px 16px 15px!important}h1{font-size:24px!important}h2{font-size:19px!important}h4{font-size:15px!important}.meta a{padding:6px 9px!important}
}
</style>
</head>
<body style="margin:0;padding:0;background:#050b13;color:#dce9f3">
<table role="presentation" class="email-shell" width="100%" bgcolor="#050b13">
<tr><td class="email-outer" align="center" style="padding:28px 14px 42px">
<table role="presentation" class="email-container" width="100%" bgcolor="#081321" style="width:100%;max-width:720px;background:#081321;border:1px solid #193047;border-radius:18px">
<tr><td class="brand-bar" style="padding:18px 28px;border-bottom:1px solid #193047;background:#07111e">
<span class="brand-mark" style="display:inline-block;width:8px;height:8px;margin-right:9px;border-radius:50%;background:#4ed9ff"></span><span class="brand-name" style="color:#8ceaff;font-size:12px;font-weight:800;letter-spacing:.16em">SIGNAL RADAR</span><span class="brand-note">PERSONAL AI INTELLIGENCE</span>
</td></tr>
<tr><td class="email-content" style="padding:30px 30px 22px;color:#9fb3c3;font-size:14px;line-height:1.75">__DIGEST_CONTENT__</td></tr>
<tr><td class="email-footer" height="16" style="height:16px;border-top:1px solid #13293c;background:#07111d">&nbsp;</td></tr>
</table>
</td></tr>
</table>
</body>
</html>""".replace("__DIGEST_CONTENT__", content)


def send_email(http: HttpClient, api_key: str, sender: str, recipient: str, subject: str, report: str) -> None:
    recipients = email_recipients(recipient)
    if not (api_key and sender and recipients):
        return
    body = markdown_email_html(report)
    response = http.post_json(
        "https://api.resend.com/emails",
        {"from": sender, "to": recipients, "subject": subject, "html": body},
        {"Authorization": "Bearer {0}".format(api_key), "Content-Type": "application/json"},
    )
    message_id = response.get("id", "unknown") if isinstance(response, dict) else "unknown"
    print("Digest email accepted by Resend for {0} recipients: {1}".format(len(recipients), message_id))


def send_smtp_email(
    host: str,
    port: int,
    username: str,
    password: str,
    recipient: str,
    subject: str,
    report: str,
) -> None:
    recipients = email_recipients(recipient)
    if not (host and port and username and password and recipients):
        return
    message = EmailMessage()
    message["From"] = username
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(report)
    message.add_alternative(
        markdown_email_html(report),
        subtype="html",
    )
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as server:
        server.login(username, password)
        server.send_message(message, from_addr=username, to_addrs=recipients)
    print("Digest sent through SMTP to {0} recipients.".format(len(recipients)))


def _serverchan_endpoint(sendkey: str) -> str:
    quoted_key = urllib.parse.quote(sendkey, safe="")
    if sendkey.startswith("sctp"):
        marker = sendkey.find("t", 4)
        uid = sendkey[4:marker] if marker > 4 else ""
        if not uid.isdigit():
            raise ValueError("Invalid ServerChan 3 SendKey; use a Turbo SCT key or a valid sctp key")
        return "https://{0}.push.ft07.com/send/{1}.send".format(uid, quoted_key)
    return "https://sctapi.ftqq.com/{0}.send".format(quoted_key)


def send_wechat(http: HttpClient, sendkey: str, subject: str, report: str) -> None:
    if not sendkey:
        return
    response = http.post_form(
        _serverchan_endpoint(sendkey),
        {"title": subject, "desp": report},
    )
    code = response.get("code") if isinstance(response, dict) else None
    if code not in (0, "0"):
        message = response.get("message", "unknown response") if isinstance(response, dict) else "empty response"
        raise RuntimeError("ServerChan rejected the digest: {0}".format(message))
    print("Digest accepted by ServerChan for WeChat delivery.")
