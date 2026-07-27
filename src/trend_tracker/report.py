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


SOURCE_DISPLAY_NAMES = {
    "producthunt": "Product Hunt",
    "hackernews": "Hacker News",
    "github": "GitHub",
    "huggingface-models": "Hugging Face Models",
    "huggingface-spaces": "Hugging Face Spaces",
    "arxiv-ai": "arXiv",
}


def _source_display_name(source_key: str) -> str:
    return SOURCE_DISPLAY_NAMES.get(
        source_key,
        source_key.replace("-", " ").strip().title(),
    )


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
    daily_brief: Dict[str, str] = None,
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
    brief = daily_brief or {}
    if all(brief.get(key) for key in ("today_change", "why_it_matters", "next_action")):
        lines.extend(
            [
                "- **新信号：** {0}".format(brief["today_change"]),
                "- **与你有关：** {0}".format(brief["why_it_matters"]),
                "- **今天可做：** {0}".format(brief["next_action"]),
                "",
            ]
        )
    elif featured:
        title = featured.display_title or featured.item.title
        score = " · {0:.0f} 分".format(featured.score) if show_scores else ""
        lines.extend(
            [
                "- **新信号：** {0}{1}".format(title, score),
                "- **与你有关：** {0}".format(featured.reason or "它与你当前关注的目标直接相关。"),
                "- **今天可做：** {0}".format(featured.next_action or "阅读原始资料，再用一个真实任务验证。"),
                "",
            ]
        )
    else:
        lines.extend(["本次采集没有信息达到相关性门槛，建议暂不行动，等待下一轮信号。", ""])

    lines.extend(["## 今日重点情报", ""])
    for track in tracks:
        relevant = relevant_by_track[track.id]
        lines.extend(
            [
                "### {0}".format(track.name),
                "",
                "发现 **{0}** 条相关信息，已按与你目标的匹配度排序；以下展开最值得阅读的 {1} 条。".format(
                    len(relevant), min(len(relevant), highlight_items)
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
            score = " · {0:.0f} 分".format(match.score) if show_scores else ""
            published = match.item.published_at.astimezone(local_time.tzinfo)
            source_meta = "**来源** {0} · **发布于** {1} · [查看原始资料 ↗]({2})".format(
                _source_display_name(match.item.source_key),
                published.strftime("%m月%d日 %H:%M"),
                match.item.url,
            )
            lines.extend(
                [
                    "#### {0}. [{1}]({2}){3}".format(index, title, target, score),
                    "",
                    "- **一句话看懂：** {0}".format(
                        match.concise_summary or "请查看原始页面了解产品或技术详情。"
                    ),
                    "- **与你有关：** {0}".format(
                        match.reason or "它与你当前关注的目标直接相关。"
                    ),
                ]
            )
            if match.analysis:
                lines.extend(["", "**深度判断**", "", match.analysis])
            else:
                lines.extend(
                    [
                        "- **先验证：** {0}".format(
                            match.next_action or "用一个真实任务验证它是否适合当前场景。"
                        )
                    ]
                )
            lines.extend(["", "> {0}".format(source_meta)])
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
            score = " · {0:.0f} 分".format(match.score) if show_scores else ""
            lines.append(
                "- **[{0}]({1})** — {2}{3}".format(
                    title,
                    target,
                    summary,
                    score,
                )
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
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)


def _signal_pipeline(value: str) -> str:
    match = re.fullmatch(
        r"今日扫描 \*\*(\d+)\*\* 条信息 · 发现相关 \*\*(\d+)\*\* 条 · "
        r"重点解读 \*\*(\d+)\*\* 条 · 深度分析 \*\*(\d+)\*\* 条",
        value,
    )
    if not match:
        return '<div class="meta">{0}</div>'.format(_inline_markdown(value))
    stages = (
        ("扫描", match.group(1), "stage-raw"),
        ("相关", match.group(2), "stage-related"),
        ("重点", match.group(3), "stage-priority"),
        ("深度", match.group(4), "stage-depth"),
    )
    cells = []
    for index, (label, number, class_name) in enumerate(stages):
        if index:
            cells.append(
                '<td class="pipeline-connector" aria-hidden="true">›</td>'
            )
        cells.append(
            '<td class="pipeline-stage {0}">'
            '<span class="pipeline-label">{1}</span>'
            '<strong class="pipeline-value">{2}</strong>'
            '<span class="pipeline-unit">条</span>'
            "</td>".format(class_name, label, number)
        )
    return (
        '<table role="presentation" class="signal-pipeline" width="100%">'
        '<tr><td class="pipeline-title" colspan="7">'
        '<span>今日信号压缩</span>'
        '<span class="pipeline-status"><i></i> 已完成</span>'
        "</td></tr>"
        "<tr>{0}</tr>"
        '<tr><td class="pipeline-track-cell" colspan="7">'
        '<span class="pipeline-track"><i></i></span>'
        "</td></tr>"
        '<tr><td class="pipeline-note" colspan="7">'
        "<i></i>多源广域扫描，只取最新信号"
        "</td></tr>"
        "</table>".format("".join(cells))
    )


def _signal_heading(value: str) -> str:
    ordinal = ""
    heading = value
    ordinal_match = re.match(r"^(\d+)\.\s+(.+)$", value)
    if ordinal_match:
        ordinal = ordinal_match.group(1).zfill(2)
        heading = ordinal_match.group(2)
    index = (
        '<span class="signal-index">{0}</span>'.format(ordinal)
        if ordinal
        else ""
    )
    return (
        '<div class="signal-heading">{0}<h4>{1}</h4></div>'.format(
            index,
            _inline_markdown(heading),
        )
    )


def _brief_item(value: str) -> str:
    variants = (
        ("**新信号：**", "brief-signal", "新信号"),
        ("**与你有关：**", "brief-relevance", "与你有关"),
        ("**今天可做：**", "brief-action", "今天可做"),
    )
    for prefix, class_name, label in variants:
        if not value.startswith(prefix):
            continue
        body = value[len(prefix) :].strip()
        return (
            '<div class="brief-row {0}">'
            '<span class="brief-glyph-cell" aria-hidden="true">'
            '<span class="brief-glyph"><i></i><b></b><em></em></span>'
            "</span>"
            '<span class="brief-copy">'
            '<strong class="brief-label">{1}</strong>'
            '<span class="brief-text">{2}</span>'
            "</span>"
            "</div>".format(class_name, label, _inline_markdown(body))
        )
    return ""


def _trend_orbit_map() -> str:
    return (
        '<div class="trend-orbit-map">'
        '<div class="trend-map-visual" aria-hidden="true">'
        '<span class="map-axis axis-x"></span><span class="map-axis axis-y"></span>'
        '<span class="map-orbit orbit-a"></span><span class="map-orbit orbit-b"></span>'
        '<span class="map-core"><i></i></span>'
        '<span class="map-node node-confirmed"><i></i><em>确认</em></span>'
        '<span class="map-node node-uncertain"><i></i><em>待证</em></span>'
        '<span class="map-node node-watch"><i></i><em>关注</em></span>'
        '<span class="probe-arm"><i></i></span>'
        "</div>"
        '<div class="trend-map-caption"><span>趋势坐标已建立</span>'
        '<span class="map-live"><i></i> 动态观测</span></div>'
        "</div>"
    )


def _radar_horizon() -> str:
    return (
        '<div class="radar-horizon">'
        '<div class="horizon-visual" aria-hidden="true">'
        '<span class="horizon-arc arc-outer"></span>'
        '<span class="horizon-arc arc-middle"></span>'
        '<span class="horizon-arc arc-inner"></span>'
        '<span class="horizon-axis"></span>'
        '<span class="horizon-sweep"></span>'
        '<span class="horizon-beacon beacon-a"></span>'
        '<span class="horizon-beacon beacon-b"></span>'
        '<span class="horizon-center"></span>'
        "</div>"
        '<div class="horizon-promise">世界持续加速，'
        "<strong>您始终先看一步</strong></div>"
        '<div class="horizon-status"><i></i>持续扫描中 · 等待下一次发现</div>'
        "</div>"
    )


def markdown_email_html(report: str) -> str:
    """Render the digest as an immersive, email-safe night observatory UI."""
    blocks = []
    list_open = False
    section_open = False
    card_open = False
    card_analysis = False
    hero_open = False
    meta_count = 0
    current_section = ""
    related_signal_index = 0

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            blocks.append("</ul>")
            list_open = False

    def close_card() -> None:
        nonlocal card_open, card_analysis
        close_list()
        if card_open:
            blocks.append("</div>")
            card_open = False
            card_analysis = False

    def close_section() -> None:
        nonlocal section_open
        close_card()
        if section_open:
            if current_section == "30 秒结论":
                blocks.append(
                    '<div class="conclusion-whisper">'
                    "把时间留给判断，而不是寻找"
                    "</div>"
                )
            blocks.append("</section>")
            section_open = False

    def close_hero() -> None:
        nonlocal hero_open
        close_list()
        if hero_open:
            blocks.append("</header>")
            hero_open = False

    for raw_line in report.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            item = line[2:]
            if current_section == "30 秒结论":
                brief_item = _brief_item(item)
                if brief_item:
                    close_list()
                    blocks.append(brief_item)
                    continue
            if not list_open:
                if current_section == "其他相关信号":
                    list_class = ' class="signal-log"'
                elif card_open and card_analysis:
                    list_class = ' class="analysis-grid"'
                elif card_open:
                    list_class = ' class="signal-facts"'
                else:
                    list_class = ""
                blocks.append("<ul{0}>".format(list_class))
                list_open = True
            if current_section == "其他相关信号":
                related_signal_index += 1
                wave_class = ("wave-short", "wave-medium", "wave-long")[
                    (related_signal_index - 1) % 3
                ]
                blocks.append(
                    '<li class="related-signal-row {0}">'
                    '<span class="log-index">{1:02d}</span>'
                    '<span class="log-wave" aria-hidden="true"><i></i><b></b></span>'
                    '<span class="log-copy">{2}</span></li>'.format(
                        wave_class,
                        related_signal_index,
                        _inline_markdown(item),
                    )
                )
            else:
                blocks.append(
                    "<li>{0}</li>".format(_inline_markdown(item))
                )
            continue
        close_list()
        if not line:
            continue
        if line.startswith("# "):
            close_section()
            close_hero()
            current_section = ""
            blocks.append('<header class="digest-hero">')
            hero_open = True
            blocks.append("<h1>{0}</h1>".format(_inline_markdown(line[2:])))
        elif line.startswith("## "):
            close_hero()
            close_section()
            title = line[3:]
            current_section = title
            if title == "其他相关信号":
                related_signal_index = 0
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
            if title == "趋势雷达":
                blocks.append(_trend_orbit_map())
            elif title == "其他相关信号":
                blocks.append(
                    '<p class="related-whisper">'
                    "不追逐共识，而是发现价值"
                    "</p>"
                )
                blocks.append('<div class="log-scanner" aria-hidden="true"></div>')
        elif line.startswith("### "):
            close_card()
            blocks.append("<h3>{0}</h3>".format(_inline_markdown(line[4:])))
        elif line.startswith("#### "):
            close_card()
            blocks.append('<div class="signal-card">')
            card_open = True
            card_analysis = False
            blocks.append(_signal_heading(line[5:]))
        elif line.startswith("> "):
            meta_value = line[2:]
            if card_open:
                if "这条推荐对你如何？" in meta_value:
                    blocks.append(
                        '<div class="meta feedback-bar">{0}</div>'.format(
                            _inline_markdown(meta_value)
                        )
                    )
                else:
                    blocks.append(
                        '<div class="signal-source-meta">{0}</div>'.format(
                            _inline_markdown(meta_value)
                        )
                    )
            elif meta_count == 0:
                blocks.append(_signal_pipeline(meta_value))
            elif "#highlights" in meta_value:
                blocks.append(
                    '<nav class="meta digest-nav">{0}</nav>'.format(
                        _inline_markdown(meta_value)
                    )
                )
            else:
                blocks.append(
                    '<div class="meta">{0}</div>'.format(
                        _inline_markdown(meta_value)
                    )
                )
            meta_count += 1
        elif line == "**深度判断**":
            card_analysis = True
            blocks.append(
                '<p class="analysis-kicker">'
                '<span>深度判断</span>'
                '<em class="premium-analysis-badge">'
                '<i></i> PREMIUM MODEL · 深度推理'
                "</em>"
                "</p>"
            )
        else:
            blocks.append("<p>{0}</p>".format(_inline_markdown(line)))
    close_hero()
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
html,body{margin:0!important;padding:0!important;width:100%!important;background:#040912!important;color:#e7f1f6!important}
body,table,td,p,a,li,h1,h2,h3,h4{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Segoe UI",Arial,sans-serif}
table{border-collapse:separate;border-spacing:0}a{color:#67d8f3;text-decoration:none;font-weight:650}a:hover{color:#a8ecfa}strong{color:#f3f8fb}code{padding:2px 5px;border:1px solid #1b3a4e;border-radius:4px;background:#071622;color:#86dff3;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:11px}
.email-shell{width:100%;background:#040912}.email-outer{padding:34px 14px 48px}.observatory-frame{width:100%;max-width:804px}.email-stage{text-align:center}.email-container{width:100%;max-width:700px;text-align:left;background:#07131f;border:1px solid #173348;border-radius:18px;overflow:hidden;box-shadow:0 28px 90px rgba(0,0,0,.4),0 0 42px rgba(56,133,165,.08)}
.telemetry-rail{display:none;width:52px;vertical-align:top}.rail-scene{position:relative;height:620px;overflow:hidden}.rail-spine{position:absolute;top:22px;width:1px;height:560px;background:linear-gradient(180deg,rgba(103,216,243,0),rgba(53,119,146,.62) 14%,rgba(53,119,146,.28) 72%,rgba(103,216,243,0))}.rail-left .rail-spine{right:16px}.rail-right .rail-spine{left:16px;background:linear-gradient(180deg,rgba(113,136,255,0),rgba(103,216,243,.72) 18%,rgba(113,136,255,.32) 70%,rgba(103,216,243,0))}
.rail-tick{position:absolute;height:1px;background:#2d6a83;box-shadow:0 0 7px rgba(103,216,243,.18)}.rail-left .tick-a{top:68px;right:16px;width:24px}.rail-left .tick-b{top:151px;right:16px;width:13px}.rail-left .tick-c{top:248px;right:16px;width:30px}.rail-left .tick-d{top:374px;right:16px;width:17px}.rail-left .tick-e{top:510px;right:16px;width:26px}.rail-dot{position:absolute;width:4px;height:4px;border-radius:50%;background:#397c97;box-shadow:0 0 9px rgba(103,216,243,.34)}.rail-left .dot-a{top:66px;right:14px}.rail-left .dot-b{top:246px;right:14px}.rail-left .dot-c{top:508px;right:14px}
.rail-particle{position:absolute;right:14px;width:3px;height:22px;border-radius:3px;background:linear-gradient(180deg,rgba(103,216,243,0),#67d8f3);box-shadow:0 6px 12px rgba(103,216,243,.56);opacity:.42}.particle-a{top:52px;animation:ingest-stream-a 10.5s linear infinite}.particle-b{top:205px;animation:ingest-stream-b 13s linear infinite}.particle-c{top:365px;background:linear-gradient(180deg,rgba(113,136,255,0),#7188ff);box-shadow:0 6px 12px rgba(113,136,255,.5);animation:ingest-stream-c 11.5s linear infinite}
.output-ring{position:absolute;left:11px;width:9px;height:9px;border:1px solid #477d94;border-radius:50%;box-shadow:0 0 12px rgba(103,216,243,.2)}.ring-a{top:112px}.ring-b{top:306px;border-color:#6577c8;box-shadow:0 0 14px rgba(113,136,255,.28);animation:beacon-breathe 4.8s ease-in-out infinite}.ring-c{top:486px}.signal-packet{position:absolute;top:50px;left:15px;width:3px;height:46px;border-radius:3px;background:linear-gradient(180deg,rgba(103,216,243,0),#d8f8ff 72%,#7188ff);box-shadow:0 9px 18px rgba(103,216,243,.72);opacity:.74;animation:output-packet 12s cubic-bezier(.4,0,.2,1) infinite}.signal-flare{position:absolute;top:310px;left:12px;width:8px;height:8px;border-radius:50%;background:#8be5f6;box-shadow:0 0 7px #67d8f3,0 0 18px rgba(113,136,255,.7);animation:signal-flare 4.8s ease-in-out infinite}
@keyframes ingest-stream-a{0%{transform:translateY(-70px);opacity:0}12%{opacity:.68}76%{opacity:.38}100%{transform:translateY(520px);opacity:0}}@keyframes ingest-stream-b{0%,18%{transform:translateY(-150px);opacity:0}29%{opacity:.56}100%{transform:translateY(360px);opacity:0}}@keyframes ingest-stream-c{0%,9%{transform:translateY(-190px);opacity:0}20%{opacity:.64}100%{transform:translateY(245px);opacity:0}}@keyframes output-packet{0%,23%{transform:translateY(-90px);opacity:0}32%{opacity:.9}78%{opacity:.7}100%{transform:translateY(500px);opacity:0}}@keyframes beacon-breathe{0%,100%{transform:scale(.82);opacity:.45}50%{transform:scale(1.32);opacity:1}}@keyframes signal-flare{0%,100%{transform:scale(.72);opacity:.48}50%{transform:scale(1.25);opacity:1}}
.brand-bar{padding:20px 28px 15px;border-bottom:1px solid #173348;background:#050e18}.brand-row{width:100%}.brand-identity{white-space:nowrap}.brand-orbit{display:inline-block;width:22px;height:22px;margin-right:10px;border:1px solid #2c7892;border-radius:50%;box-shadow:inset 0 0 10px rgba(103,216,243,.1);vertical-align:middle;text-align:center}.brand-mark{display:inline-block;width:6px;height:6px;margin-top:7px;border-radius:50%;background:#67d8f3;box-shadow:0 0 12px #67d8f3}.brand-name{color:#a5eafa;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;font-weight:800;letter-spacing:.17em;vertical-align:middle}.brand-note{color:#607a8d;font-size:10px;font-weight:500;letter-spacing:.04em}
.signal-spectrum{position:relative;height:18px;margin-top:13px;overflow:hidden;white-space:nowrap}.signal-spectrum span{display:inline-block;height:1px;margin-right:4px;background:#17384d;vertical-align:middle}.signal-spectrum .quiet{width:12%;}.signal-spectrum .short{width:5%;background:#25627c}.signal-spectrum .pulse{width:2px;height:11px;background:#67d8f3;box-shadow:0 0 8px rgba(103,216,243,.65)}.signal-spectrum .medium{width:18%;background:#20546d}.signal-spectrum .long{width:31%}.signal-spectrum .beacon{width:4px;height:4px;border-radius:50%;background:#8be5f6;box-shadow:0 0 8px rgba(103,216,243,.45)}.signal-spectrum .spectrum-scan{position:absolute;right:0;top:8px;width:18%!important;height:2px!important;margin:0!important;background:linear-gradient(90deg,rgba(103,216,243,0),#67d8f3 62%,#d9f8ff)!important;box-shadow:0 0 10px rgba(103,216,243,.72);opacity:.55;animation:signal-scan 9s cubic-bezier(.4,0,.2,1) infinite}
@keyframes signal-scan{0%,14%{transform:translateX(-455%);opacity:0}20%{opacity:.32}63%{transform:translateX(0);opacity:.88}72%,100%{transform:translateX(0);opacity:.34}}
.email-content{padding:0 32px 28px;text-align:left;color:#93a9b8;font-size:14px;line-height:1.78;mso-line-height-rule:exactly}.digest-hero{margin:0 -32px;padding:30px 32px 22px;text-align:left;border-bottom:1px solid #142f42;background:#081724}
h1{margin:0 0 19px;color:#f4f8fa;font-family:"Avenir Next","SF Pro Display","PingFang SC","Microsoft YaHei",sans-serif;font-size:29px;font-weight:650;line-height:1.32;letter-spacing:-.035em}h2{margin:2px 0 18px;color:#edf5f8;font-family:"Avenir Next","SF Pro Display","PingFang SC","Microsoft YaHei",sans-serif;font-size:21px;font-weight:650;line-height:1.4;letter-spacing:-.025em}h3{margin:25px 0 12px;color:#9ddfeb;font-size:15px;font-weight:650;line-height:1.45}h4{display:inline;margin:0;color:#eff6f9;font-size:16px;font-weight:650;line-height:1.52}
p{margin:9px 0;color:#91a7b6}.content-section{margin:34px 0 0;padding-top:29px;border-top:1px solid #173348}
.signal-pipeline{margin:0 0 15px;border:1px solid #19384d;border-radius:10px;background:#07131f}.pipeline-title{padding:10px 12px 7px;color:#58768a;font-size:9px;font-weight:650;letter-spacing:.08em}.pipeline-status{float:right;color:#5b817d;font-weight:500;letter-spacing:0}.pipeline-status i{display:inline-block;width:5px;height:5px;margin-right:5px;border-radius:50%;background:#72cdb9;box-shadow:0 0 7px rgba(114,205,185,.45)}.pipeline-stage{width:19%;padding:5px 5px 8px;text-align:center;white-space:nowrap}.pipeline-label{display:block;color:#5f7d90;font-size:9px}.pipeline-value{display:inline-block;margin-top:1px;color:#9db1be;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:18px;font-weight:700}.pipeline-unit{margin-left:2px;color:#4f6c7e;font-size:8px}.pipeline-connector{width:8%;padding:4px 0 0;color:#29536a;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:16px;text-align:center}.stage-related .pipeline-value{color:#b9cbd4}.stage-priority .pipeline-value{color:#dff6fb}.stage-depth .pipeline-value{color:#67d8f3;text-shadow:0 0 12px rgba(103,216,243,.18)}.pipeline-track-cell{padding:0 12px 6px}.pipeline-track{display:block;position:relative;height:2px;overflow:hidden;border-radius:2px;background:#142f40}.pipeline-track i{display:block;width:100%;height:2px;background:linear-gradient(90deg,#24485c 0%,#326f86 42%,#67d8f3 100%);opacity:.6}.pipeline-note{padding:0 12px 10px;color:#627f90;font-size:10px;line-height:1.45;letter-spacing:.025em}.pipeline-note i{display:inline-block;width:4px;height:4px;margin:0 6px 2px 0;border-radius:50%;background:#4c8197;box-shadow:0 0 7px rgba(103,216,243,.24)}
.meta{margin:12px 0 16px;padding:12px 14px;border:1px solid #19384d;border-radius:9px;background:#091a28;color:#718b9d;font-size:11px}.digest-nav{margin:0;padding:0;border:0;background:transparent;text-align:left}.digest-nav a{display:inline-block;margin:3px 6px 3px 0;padding:6px 10px;border:1px solid #1c4157;border-radius:7px;background:#0a1c2b;color:#79d9ee;font-size:10px;white-space:nowrap}
.section-conclusion{position:relative;margin:28px 0 0;padding:23px 23px 16px;border:1px solid #244354;border-left:3px solid #67d8f3;border-radius:11px;background:#0a1b29}.section-conclusion h2{margin-bottom:11px;color:#dff6fb}.brief-row{display:table;width:100%;box-sizing:border-box;padding:10px 0;border-bottom:1px solid #173447}.brief-row:last-child{border-bottom:0}.brief-glyph-cell{display:table-cell;width:47px;vertical-align:middle}.brief-glyph{display:block;position:relative;width:34px;height:34px;overflow:hidden;border:1px solid #21485c;border-radius:9px;background:#071723;box-shadow:inset 0 0 15px rgba(64,143,173,.07)}.brief-copy{display:table-cell;vertical-align:middle}.brief-label{display:block;color:#a5eafa;font-size:12px;font-weight:750;line-height:1.35;letter-spacing:.04em}.brief-text{display:block;margin-top:3px;color:#a7bac5;font-size:13px;line-height:1.62}.conclusion-whisper{margin:7px 0 0;padding-top:10px;border-top:1px solid #173447;color:#6d8998;font-size:11px;line-height:1.5;letter-spacing:.025em;text-align:right}.brief-glyph i,.brief-glyph b,.brief-glyph em{display:block;position:absolute;font-style:normal}.brief-signal .brief-glyph i{top:16px;left:5px;width:24px;height:1px;background:linear-gradient(90deg,#214a60,#67d8f3);box-shadow:0 0 6px rgba(103,216,243,.36)}.brief-signal .brief-glyph b{top:13px;left:18px;width:6px;height:6px;border-radius:50%;background:#c8f6ff;box-shadow:0 0 8px #67d8f3}.brief-signal .brief-glyph em{top:9px;left:14px;width:12px;height:12px;border:1px solid #67d8f3;border-radius:50%;opacity:.38;animation:brief-pulse 4.2s ease-out infinite}.brief-relevance .brief-glyph i{top:14px;left:14px;width:6px;height:6px;border:1px solid #a7eaf7;border-radius:50%;background:#163e50;box-shadow:0 0 10px rgba(103,216,243,.65)}.brief-relevance .brief-glyph b,.brief-relevance .brief-glyph em{top:8px;width:8px;height:16px;border:1px solid #527ca6}.brief-relevance .brief-glyph b{left:6px;border-right:0;border-radius:8px 0 0 8px}.brief-relevance .brief-glyph em{right:6px;border-left:0;border-radius:0 8px 8px 0}.brief-relevance .brief-glyph{animation:brief-lock 5.4s ease-in-out infinite}.brief-action .brief-glyph i{top:17px;left:6px;width:22px;height:1px;background:linear-gradient(90deg,#295268,#7188ff 62%,#9eeaf7);box-shadow:0 0 7px rgba(103,216,243,.3)}.brief-action .brief-glyph b{top:14px;left:10px;width:6px;height:6px;border-radius:50%;background:#8fe5f5;box-shadow:0 0 8px rgba(103,216,243,.75);animation:brief-launch 4.8s cubic-bezier(.4,0,.2,1) infinite}.brief-action .brief-glyph em{top:13px;right:6px;width:7px;height:7px;border-top:1px solid #9eeaf7;border-right:1px solid #9eeaf7;transform:rotate(45deg)}@keyframes brief-pulse{0%,45%{transform:scale(.55);opacity:0}58%{opacity:.72}100%{transform:scale(1.65);opacity:0}}@keyframes brief-lock{0%,100%{box-shadow:inset 0 0 15px rgba(64,143,173,.07)}50%{box-shadow:inset 0 0 19px rgba(103,216,243,.13),0 0 10px rgba(103,216,243,.05)}}@keyframes brief-launch{0%,25%{transform:translateX(-5px);opacity:.25}45%{opacity:1}85%,100%{transform:translateX(11px);opacity:.15}}
.signal-card{margin:12px 0 18px;padding:20px 20px 18px;border:1px solid #17384d;border-radius:11px;background:#091824}.signal-heading{display:flex;align-items:flex-start;gap:10px;margin-bottom:13px}.signal-index{flex:0 0 auto;padding-top:2px;color:#4f8ba3;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:11px}.signal-heading h4{flex:1}.signal-heading h4 a{color:#f0f6f8}.signal-facts{margin:7px 0 4px!important;padding:0!important;list-style:none}.signal-facts li{margin:0!important;padding:9px 0!important;border-bottom:1px solid #153143;color:#9bb0bd}.signal-facts li:last-child{border-bottom:0}.signal-facts strong{display:block;margin-bottom:3px;color:#9de5f1;font-size:13px;line-height:1.45;letter-spacing:.02em}.analysis-kicker{margin:16px 0 9px!important;color:#d5f4f8!important;font-size:14px!important;font-weight:750;line-height:1.5;letter-spacing:.03em}.analysis-kicker:before{display:inline-block;width:16px;height:1px;margin:0 8px 4px 0;background:#599bb2;content:""}.premium-analysis-badge{display:inline-block;margin-left:8px;padding:3px 7px;border:1px solid #345d79;border-radius:999px;background:#0b2030;color:#8fb8d8;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:8px;font-weight:700;font-style:normal;letter-spacing:.06em;vertical-align:2px;white-space:nowrap}.premium-analysis-badge i{display:inline-block;width:5px;height:5px;margin-right:5px;border-radius:50%;background:#7188ff;box-shadow:0 0 8px rgba(113,136,255,.78);animation:premium-model-pulse 4.6s ease-in-out infinite}.analysis-grid{margin:0 0 10px!important;padding:4px 14px!important;border:1px solid #17394d;border-left:2px solid #456f9d;border-radius:8px;background:#071621;list-style:none}.analysis-grid li{margin:0!important;padding:9px 0!important;border-bottom:1px solid #142f40;color:#91a8b7}.analysis-grid li:last-child{border-bottom:0}.analysis-grid strong{display:block;margin-bottom:3px;color:#9de2ee;font-size:13px;line-height:1.45;letter-spacing:.02em}.signal-source-meta{margin:14px 0 0;padding:11px 12px;border:1px solid #1a4054;border-left:2px solid #4d8ca3;border-radius:8px;background:#071723;color:#8da7b6;font-size:12px;line-height:1.75}.signal-source-meta strong{color:#c1dae1;font-size:12px;letter-spacing:.01em}.signal-source-meta a{display:inline-block;margin:3px 0 1px 4px;padding:4px 9px;border:1px solid #2a6075;border-radius:6px;background:#0a2230;color:#91e2f1;font-size:11px;font-weight:700}.signal-card .feedback-bar{margin:12px 0 0;border-color:#253f4c;background:#071723;color:#607c8e}.feedback-bar a{display:inline-block;margin:4px 4px 2px 0;padding:5px 9px;border:1px solid #254c5d;border-radius:6px;background:#0a202d;color:#79d9ed;font-size:10px}.feedback-bar a:last-child{border-color:#5b472f;background:#191714;color:#efb36b}@keyframes premium-model-pulse{0%,100%{opacity:.45;transform:scale(.78)}50%{opacity:1;transform:scale(1.16)}}
ul{margin:8px 0 15px;padding:0 0 0 19px}li{margin:5px 0;color:#94aab9}
.section-trends{position:relative}.section-trends h3{color:#8fd9e8}.section-trends p{margin:8px 0 14px;padding:15px 16px;border-left:2px solid #376f86;background:#081824;color:#9bb0bd}.section-trends strong{color:#87dded}.section-trends ul{margin:15px 0 20px;padding:0;list-style:none}.section-trends li{margin:7px 0;padding:11px 14px;border:1px solid #17384d;border-left:2px solid #376f86;border-radius:7px;background:#081824}.section-trends li:nth-child(2){border-left-color:#67d8f3}.section-trends li:nth-child(3){border-left-color:#6577c8}.section-trends li:nth-child(4){border-left-color:#477d94}
.trend-orbit-map{margin:3px 0 23px;overflow:hidden;border:1px solid #193e54;border-radius:12px;background:radial-gradient(circle at 50% 48%,rgba(64,143,173,.16),rgba(7,19,31,.4) 42%,#06111c 78%);box-shadow:inset 0 0 38px rgba(48,113,142,.08)}.trend-map-visual{position:relative;height:190px;overflow:hidden}.map-axis{position:absolute;background:linear-gradient(90deg,rgba(48,109,137,0),rgba(48,109,137,.34),rgba(48,109,137,0))}.axis-x{top:95px;left:8%;width:84%;height:1px}.axis-y{top:12%;left:50%;width:1px;height:76%;background:linear-gradient(180deg,rgba(48,109,137,0),rgba(48,109,137,.28),rgba(48,109,137,0))}.map-orbit{position:absolute;left:50%;border:1px solid rgba(65,125,151,.42);border-radius:50%}.orbit-a{top:50px;width:260px;height:88px;margin-left:-130px;transform:rotate(-9deg)}.orbit-b{top:28px;width:180px;height:134px;margin-left:-90px;border-color:rgba(101,119,200,.33);transform:rotate(23deg)}.map-core{position:absolute;top:85px;left:50%;width:14px;height:14px;margin-left:-7px;border:1px solid #67d8f3;border-radius:50%;box-shadow:0 0 18px rgba(103,216,243,.46)}.map-core i{display:block;width:6px;height:6px;margin:3px;border-radius:50%;background:#d8f8ff;box-shadow:0 0 7px #67d8f3,0 0 17px rgba(103,216,243,.72);animation:map-core-pulse 4.4s ease-in-out infinite}.map-node{position:absolute;color:#55788c;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:8px;letter-spacing:.08em}.map-node i{display:inline-block;width:6px;height:6px;margin-right:6px;border:1px solid #67d8f3;border-radius:50%;box-shadow:0 0 9px rgba(103,216,243,.4)}.map-node em{font-style:normal}.node-confirmed{top:49px;left:69%;color:#79c9d9}.node-confirmed i{background:#67d8f3}.node-uncertain{top:132px;left:22%;color:#7887c7;animation:uncertain-drift 6.8s ease-in-out infinite}.node-uncertain i{border-color:#7188ff;box-shadow:0 0 10px rgba(113,136,255,.55)}.node-watch{top:137px;left:73%;color:#79b8c8}.node-watch i{background:#8be5f6;box-shadow:0 0 12px rgba(103,216,243,.72)}.probe-arm{position:absolute;top:92px;left:50%;width:102px;height:1px;transform:rotate(18deg);transform-origin:left center;animation:probe-orbit 14s linear infinite}.probe-arm i{position:absolute;right:-3px;top:-3px;width:6px;height:6px;border-radius:50%;background:#7188ff;box-shadow:0 0 8px #7188ff,0 0 16px rgba(103,216,243,.42)}.trend-map-caption{padding:8px 12px 10px;border-top:1px solid #163447;color:#547487;font-size:8px;font-weight:650;letter-spacing:.08em}.map-live{float:right;color:#668a99;font-weight:500;letter-spacing:0}.map-live i{display:inline-block;width:5px;height:5px;margin-right:4px;border-radius:50%;background:#67d8f3;box-shadow:0 0 8px rgba(103,216,243,.62);animation:map-core-pulse 3.6s ease-in-out infinite}
.section-related{position:relative;overflow:hidden}.section-related h3{position:relative;z-index:2}.related-whisper{position:relative;z-index:2;margin:-10px 0 19px;padding-left:10px;border-left:1px solid #315d72;color:#698696;font-size:11px;line-height:1.55;letter-spacing:.025em}.log-scanner{position:absolute;top:91px;left:0;z-index:1;width:100%;height:1px;background:linear-gradient(90deg,rgba(103,216,243,0),rgba(103,216,243,.62),rgba(113,136,255,.25),rgba(103,216,243,0));box-shadow:0 0 12px rgba(103,216,243,.3);opacity:.36;animation:log-scan 15s cubic-bezier(.4,0,.2,1) infinite}.signal-log{position:relative;z-index:2;margin:5px 0 18px!important;padding:0!important;list-style:none}.related-signal-row{display:block;margin:0!important;padding:12px 4px!important;border-bottom:1px solid #142d3e!important;color:#8fa7b6}.related-signal-row:last-child{border-bottom:0!important}.log-index{display:inline-block;width:25px;color:#41687d;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:9px;vertical-align:middle}.log-wave{display:inline-block;position:relative;width:46px;height:10px;margin-right:8px;vertical-align:middle}.log-wave i{display:block;position:absolute;top:5px;left:0;height:1px;background:linear-gradient(90deg,#24566d,#67d8f3);box-shadow:0 0 6px rgba(103,216,243,.28)}.log-wave b{display:block;position:absolute;top:3px;width:5px;height:5px;border:1px solid #67d8f3;border-radius:50%;box-shadow:0 0 7px rgba(103,216,243,.35)}.wave-short .log-wave i{width:18px}.wave-short .log-wave b{left:18px}.wave-medium .log-wave i{width:29px}.wave-medium .log-wave b{left:29px}.wave-long .log-wave i{width:40px;background:linear-gradient(90deg,#315f71,#7188ff)}.wave-long .log-wave b{left:40px;border-color:#7188ff;box-shadow:0 0 8px rgba(113,136,255,.48)}.log-copy{vertical-align:middle}.log-copy a{color:#c9e5eb}
.email-footer{overflow:hidden;padding:0 28px 17px;border-top:1px solid #132d3e;background:#050e18}.radar-horizon{overflow:hidden}.horizon-visual{position:relative;height:122px;overflow:hidden}.horizon-arc{position:absolute;left:50%;border:1px solid rgba(57,115,140,.44);border-radius:50%;transform:translateX(-50%)}.arc-outer{bottom:-105px;width:420px;height:210px}.arc-middle{bottom:-75px;width:300px;height:150px;border-color:rgba(74,137,162,.4)}.arc-inner{bottom:-45px;width:180px;height:90px;border-color:rgba(101,119,200,.42)}.horizon-axis{position:absolute;bottom:0;left:8%;width:84%;height:1px;background:linear-gradient(90deg,rgba(103,216,243,0),rgba(103,216,243,.36),rgba(103,216,243,0))}.horizon-sweep{position:absolute;bottom:0;left:50%;width:170px;height:1px;transform:rotate(-35deg);transform-origin:left center;background:linear-gradient(90deg,#d8f8ff,rgba(103,216,243,.12),rgba(103,216,243,0));box-shadow:0 0 9px rgba(103,216,243,.45);animation:horizon-sweep 18s cubic-bezier(.4,0,.2,1) infinite}.horizon-beacon{position:absolute;width:6px;height:6px;border-radius:50%;background:#67d8f3;box-shadow:0 0 7px #67d8f3,0 0 16px rgba(103,216,243,.64)}.beacon-a{top:42px;left:31%;animation:horizon-beacon 7s ease-in-out infinite}.beacon-b{top:72px;left:70%;background:#7188ff;box-shadow:0 0 8px #7188ff,0 0 18px rgba(113,136,255,.58);animation:horizon-beacon 7s ease-in-out 2.4s infinite}.horizon-center{position:absolute;bottom:-4px;left:50%;width:9px;height:9px;margin-left:-5px;border:1px solid #8be5f6;border-radius:50%;background:#0a2130;box-shadow:0 0 14px rgba(103,216,243,.7)}.horizon-promise{text-align:center;color:#7895a3;font-size:12px;line-height:1.55;letter-spacing:.025em}.horizon-promise strong{color:#b3d5dd;font-weight:650}.horizon-status{margin-top:5px;text-align:center;color:#557889;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:8px;letter-spacing:.08em}.horizon-status i{display:inline-block;width:5px;height:5px;margin-right:6px;border-radius:50%;background:#67d8f3;box-shadow:0 0 8px rgba(103,216,243,.62);animation:map-core-pulse 3.8s ease-in-out infinite}
@keyframes map-core-pulse{0%,100%{opacity:.48;transform:scale(.82)}50%{opacity:1;transform:scale(1.22)}}@keyframes probe-orbit{from{transform:rotate(18deg)}to{transform:rotate(378deg)}}@keyframes uncertain-drift{0%,100%{transform:translate(-3px,1px);opacity:.58}50%{transform:translate(5px,-4px);opacity:1}}@keyframes log-scan{0%,18%{transform:translateY(-55px);opacity:0}28%{opacity:.48}82%{opacity:.3}100%{transform:translateY(520px);opacity:0}}@keyframes horizon-sweep{0%,18%{transform:rotate(-165deg);opacity:0}28%{opacity:.68}82%{opacity:.5}100%{transform:rotate(-15deg);opacity:0}}@keyframes horizon-beacon{0%,72%,100%{opacity:.18;transform:scale(.7)}78%{opacity:1;transform:scale(1.42)}86%{opacity:.38;transform:scale(.9)}}
@media only screen and (max-width:600px){
.email-outer{padding:0!important}.email-container{border-right:0!important;border-left:0!important;border-radius:0!important}.brand-bar{padding:16px 18px 13px!important}.brand-note{display:none!important}.email-content{padding:0 18px 20px!important;font-size:14px!important}.digest-hero{margin:0 -18px!important;padding:25px 18px 20px!important}.pipeline-stage{padding-right:2px!important;padding-left:2px!important}.pipeline-value{font-size:16px!important}.pipeline-connector{width:6%!important;font-size:13px!important}.section-conclusion{padding:20px 16px 14px!important}.brief-glyph-cell{width:43px!important}.brief-text{font-size:12px!important}.signal-card{padding:17px 15px 15px!important}.signal-heading{display:block!important}.signal-index{display:inline-block!important;margin-right:8px!important}.trend-map-visual{height:168px!important}.orbit-a{width:220px!important;margin-left:-110px!important}.orbit-b{width:150px!important;margin-left:-75px!important}.node-confirmed{left:65%!important}.node-uncertain{left:14%!important}.node-watch{left:68%!important}.probe-arm{width:82px!important}.log-wave{width:37px!important;margin-right:5px!important}.wave-long .log-wave i{width:31px!important}.wave-long .log-wave b{left:31px!important}.email-footer{padding-right:14px!important;padding-left:14px!important}.arc-outer{width:330px!important;height:166px!important;bottom:-83px!important}.arc-middle{width:240px!important;height:120px!important;bottom:-60px!important}.arc-inner{width:150px!important;height:76px!important;bottom:-38px!important}h1{font-size:24px!important}h2{font-size:19px!important}h4{font-size:15px!important}.digest-nav a,.feedback-bar a{padding:6px 9px!important}
}
@media only screen and (min-width:821px){.telemetry-rail{display:table-cell!important}}
@media (prefers-reduced-motion:reduce){.spectrum-scan,.rail-particle,.signal-packet,.output-ring,.signal-flare,.brief-glyph,.brief-glyph *,.premium-analysis-badge i,.map-core i,.probe-arm,.node-uncertain,.map-live i,.log-scanner,.horizon-sweep,.horizon-beacon,.horizon-status i{animation:none!important}.spectrum-scan{opacity:.45!important}.rail-particle{opacity:.34!important}.signal-packet{opacity:.58!important}.brief-signal .brief-glyph em{opacity:.42!important}.brief-action .brief-glyph b{opacity:.85!important}.premium-analysis-badge i{opacity:.8!important}.log-scanner{opacity:.22!important}.horizon-sweep{opacity:.42!important}}
</style>
</head>
<body style="margin:0;padding:0;background:#040912;color:#e7f1f6">
<table role="presentation" class="email-shell" width="100%" bgcolor="#040912">
<tr><td class="email-outer" align="center" style="padding:34px 14px 48px">
<table role="presentation" class="observatory-frame" width="100%"><tr>
<td class="telemetry-rail rail-left" width="52" valign="top" aria-hidden="true">
<div class="rail-scene"><span class="rail-spine"></span><span class="rail-tick tick-a"></span><span class="rail-tick tick-b"></span><span class="rail-tick tick-c"></span><span class="rail-tick tick-d"></span><span class="rail-tick tick-e"></span><span class="rail-dot dot-a"></span><span class="rail-dot dot-b"></span><span class="rail-dot dot-c"></span><span class="rail-particle particle-a"></span><span class="rail-particle particle-b"></span><span class="rail-particle particle-c"></span></div>
</td>
<td class="email-stage" align="center">
<table role="presentation" class="email-container" width="100%" bgcolor="#07131f" style="width:100%;max-width:700px;background:#07131f;border:1px solid #173348;border-radius:18px">
<tr><td class="brand-bar" style="padding:20px 28px 15px;border-bottom:1px solid #173348;background:#050e18">
<table role="presentation" class="brand-row" width="100%"><tr>
<td class="brand-identity"><span class="brand-orbit"><span class="brand-mark"></span></span><span class="brand-name">SIGNAL RADAR</span></td>
<td align="right"><span class="brand-note">每日个性化 AI 情报</span></td>
</tr></table>
<div class="signal-spectrum" aria-hidden="true"><span class="quiet"></span><span class="short"></span><span class="pulse"></span><span class="medium"></span><span class="beacon"></span><span class="short"></span><span class="pulse"></span><span class="long"></span><span class="spectrum-scan"></span></div>
</td></tr>
<tr><td class="email-content" align="left" style="padding:0 32px 28px;text-align:left;color:#93a9b8;font-size:14px;line-height:1.78">__DIGEST_CONTENT__</td></tr>
<tr><td class="email-footer" style="border-top:1px solid #132d3e;background:#050e18">__RADAR_HORIZON__</td></tr>
</table>
</td>
<td class="telemetry-rail rail-right" width="52" valign="top" aria-hidden="true">
<div class="rail-scene"><span class="rail-spine"></span><span class="output-ring ring-a"></span><span class="output-ring ring-b"></span><span class="output-ring ring-c"></span><span class="signal-packet"></span><span class="signal-flare"></span></div>
</td>
</tr></table>
</td></tr>
</table>
</body>
</html>""".replace("__DIGEST_CONTENT__", content).replace("__RADAR_HORIZON__", _radar_horizon())


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
