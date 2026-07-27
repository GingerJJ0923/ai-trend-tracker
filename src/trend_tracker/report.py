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
        "</table>".format("".join(cells))
    )


def _signal_heading(value: str) -> str:
    ordinal = ""
    heading = value
    ordinal_match = re.match(r"^(\d+)\.\s+(.+)$", value)
    if ordinal_match:
        ordinal = ordinal_match.group(1).zfill(2)
        heading = ordinal_match.group(2)
    tier = ""
    if "｜" in heading:
        heading, tier = heading.rsplit("｜", 1)
    index = (
        '<span class="signal-index">{0}</span>'.format(ordinal)
        if ordinal
        else ""
    )
    badge = (
        '<span class="signal-tier">{0}</span>'.format(_inline_markdown(tier))
        if tier
        else ""
    )
    return (
        '<div class="signal-heading">{0}<h4>{1}</h4>{2}</div>'.format(
            index,
            _inline_markdown(heading),
            badge,
        )
    )


def markdown_email_html(report: str) -> str:
    """Render the digest as a restrained, email-safe night observatory UI."""
    blocks = []
    list_open = False
    section_open = False
    card_open = False
    hero_open = False
    meta_count = 0

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

    def close_hero() -> None:
        nonlocal hero_open
        close_list()
        if hero_open:
            blocks.append("</header>")
            hero_open = False

    for raw_line in report.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            if not list_open:
                blocks.append("<ul>")
                list_open = True
            item = line[2:]
            item_class = (
                ' class="action-row"'
                if item.startswith("**建议动作：**")
                else ""
            )
            blocks.append(
                "<li{0}>{1}</li>".format(item_class, _inline_markdown(item))
            )
            continue
        close_list()
        if not line:
            continue
        if line.startswith("# "):
            close_section()
            close_hero()
            blocks.append('<header class="digest-hero">')
            hero_open = True
            blocks.append("<h1>{0}</h1>".format(_inline_markdown(line[2:])))
        elif line.startswith("## "):
            close_hero()
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
            blocks.append(_signal_heading(line[5:]))
        elif line.startswith("> "):
            meta_value = line[2:]
            if meta_count == 0:
                blocks.append(_signal_pipeline(meta_value))
            elif card_open:
                blocks.append(
                    '<div class="meta feedback-bar">{0}</div>'.format(
                        _inline_markdown(meta_value)
                    )
                )
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
.email-shell{width:100%;background:#040912}.email-outer{padding:34px 14px 48px}.email-container{width:100%;max-width:700px;background:#07131f;border:1px solid #173348;border-radius:18px;overflow:hidden;box-shadow:0 28px 90px rgba(0,0,0,.4)}
.brand-bar{padding:20px 28px 15px;border-bottom:1px solid #173348;background:#050e18}.brand-row{width:100%}.brand-identity{white-space:nowrap}.brand-orbit{display:inline-block;width:22px;height:22px;margin-right:10px;border:1px solid #2c7892;border-radius:50%;box-shadow:inset 0 0 10px rgba(103,216,243,.1);vertical-align:middle;text-align:center}.brand-mark{display:inline-block;width:6px;height:6px;margin-top:7px;border-radius:50%;background:#67d8f3;box-shadow:0 0 12px #67d8f3}.brand-name{color:#a5eafa;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;font-weight:800;letter-spacing:.17em;vertical-align:middle}.brand-note{color:#607a8d;font-size:10px;font-weight:500;letter-spacing:.04em}
.signal-spectrum{position:relative;height:18px;margin-top:13px;overflow:hidden;white-space:nowrap}.signal-spectrum span{display:inline-block;height:1px;margin-right:4px;background:#17384d;vertical-align:middle}.signal-spectrum .quiet{width:12%;}.signal-spectrum .short{width:5%;background:#25627c}.signal-spectrum .pulse{width:2px;height:11px;background:#67d8f3;box-shadow:0 0 8px rgba(103,216,243,.65)}.signal-spectrum .medium{width:18%;background:#20546d}.signal-spectrum .long{width:31%}.signal-spectrum .beacon{width:4px;height:4px;border-radius:50%;background:#8be5f6;box-shadow:0 0 8px rgba(103,216,243,.45)}.signal-spectrum .spectrum-scan{position:absolute;right:0;top:8px;width:18%!important;height:2px!important;margin:0!important;background:linear-gradient(90deg,rgba(103,216,243,0),#67d8f3 62%,#d9f8ff)!important;box-shadow:0 0 10px rgba(103,216,243,.72);opacity:.55;animation:signal-scan 9s cubic-bezier(.4,0,.2,1) infinite}
@keyframes signal-scan{0%,14%{transform:translateX(-455%);opacity:0}20%{opacity:.32}63%{transform:translateX(0);opacity:.88}72%,100%{transform:translateX(0);opacity:.34}}
.email-content{padding:0 32px 28px;color:#93a9b8;font-size:14px;line-height:1.78;mso-line-height-rule:exactly}.digest-hero{margin:0 -32px;padding:30px 32px 22px;border-bottom:1px solid #142f42;background:#081724}
h1{margin:0 0 19px;color:#f4f8fa;font-family:"Avenir Next","SF Pro Display","PingFang SC","Microsoft YaHei",sans-serif;font-size:29px;font-weight:650;line-height:1.32;letter-spacing:-.035em}h2{margin:2px 0 18px;color:#edf5f8;font-family:"Avenir Next","SF Pro Display","PingFang SC","Microsoft YaHei",sans-serif;font-size:21px;font-weight:650;line-height:1.4;letter-spacing:-.025em}h3{margin:25px 0 12px;color:#9ddfeb;font-size:15px;font-weight:650;line-height:1.45}h4{display:inline;margin:0;color:#eff6f9;font-size:16px;font-weight:650;line-height:1.52}
p{margin:9px 0;color:#91a7b6}.content-section{margin:34px 0 0;padding-top:29px;border-top:1px solid #173348}
.signal-pipeline{margin:0 0 15px;border:1px solid #19384d;border-radius:10px;background:#07131f}.pipeline-title{padding:10px 12px 7px;color:#58768a;font-size:9px;font-weight:650;letter-spacing:.08em}.pipeline-status{float:right;color:#5b817d;font-weight:500;letter-spacing:0}.pipeline-status i{display:inline-block;width:5px;height:5px;margin-right:5px;border-radius:50%;background:#72cdb9;box-shadow:0 0 7px rgba(114,205,185,.45)}.pipeline-stage{width:19%;padding:5px 5px 8px;text-align:center;white-space:nowrap}.pipeline-label{display:block;color:#5f7d90;font-size:9px}.pipeline-value{display:inline-block;margin-top:1px;color:#9db1be;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:18px;font-weight:700}.pipeline-unit{margin-left:2px;color:#4f6c7e;font-size:8px}.pipeline-connector{width:8%;padding:4px 0 0;color:#29536a;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:16px;text-align:center}.stage-related .pipeline-value{color:#b9cbd4}.stage-priority .pipeline-value{color:#dff6fb}.stage-depth .pipeline-value{color:#67d8f3;text-shadow:0 0 12px rgba(103,216,243,.18)}.pipeline-track-cell{padding:0 12px 11px}.pipeline-track{display:block;position:relative;height:2px;overflow:hidden;border-radius:2px;background:#142f40}.pipeline-track i{display:block;width:100%;height:2px;background:linear-gradient(90deg,#24485c 0%,#326f86 42%,#67d8f3 100%);opacity:.6}
.meta{margin:12px 0 16px;padding:12px 14px;border:1px solid #19384d;border-radius:9px;background:#091a28;color:#718b9d;font-size:11px}.digest-nav{margin:0;padding:0;border:0;background:transparent;text-align:left}.digest-nav a{display:inline-block;margin:3px 6px 3px 0;padding:6px 10px;border:1px solid #1c4157;border-radius:7px;background:#0a1c2b;color:#79d9ee;font-size:10px;white-space:nowrap}
.section-conclusion{position:relative;margin:28px 0 0;padding:23px 23px 19px;border:1px solid #244354;border-left:3px solid #67d8f3;border-radius:11px;background:#0a1b29}.section-conclusion h2{margin-bottom:13px;color:#dff6fb}.section-conclusion ul{margin-bottom:0}.section-conclusion li{padding:8px 0;border-bottom:1px solid #173447}.section-conclusion li:last-child{border-bottom:0}.section-conclusion .action-row{margin-top:3px;padding:10px 12px;border:1px solid #614a2f;border-radius:7px;background:#1a1817;color:#d8c3a8}.section-conclusion .action-row strong{color:#f2bf7d}
.signal-card{margin:12px 0 18px;padding:20px 20px 18px;border:1px solid #17384d;border-radius:11px;background:#091824}.signal-heading{display:flex;align-items:flex-start;gap:10px;margin-bottom:13px}.signal-index{flex:0 0 auto;padding-top:2px;color:#4f8ba3;font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:11px}.signal-heading h4{flex:1}.signal-heading h4 a{color:#f0f6f8}.signal-tier{flex:0 0 auto;margin-top:2px;padding:3px 7px;border:1px solid #29576b;border-radius:999px;background:#0b2230;color:#7fd9ec;font-size:9px;white-space:nowrap}.signal-card ul{margin-top:9px}.signal-card li{padding:4px 0}.signal-card .action-row{margin:3px 0;padding:6px 9px;border-left:2px solid #b78350;color:#c4b49f}.signal-card .action-row strong{color:#e8b875}.signal-card .feedback-bar{margin:15px 0 0;border-color:#253f4c;background:#071723;color:#607c8e}.feedback-bar a{display:inline-block;margin:4px 4px 2px 0;padding:5px 9px;border:1px solid #254c5d;border-radius:6px;background:#0a202d;color:#79d9ed;font-size:10px}.feedback-bar a:last-child{border-color:#5b472f;background:#191714;color:#efb36b}
ul{margin:8px 0 15px;padding:0 0 0 19px}li{margin:5px 0;color:#94aab9}.section-trends h3{color:#8fd9e8}.section-trends p{margin:8px 0 14px;padding:15px 16px;border-left:2px solid #376f86;background:#081824;color:#9bb0bd}.section-trends strong{color:#87dded}.section-related ul{margin-top:5px;padding-left:0;list-style:none}.section-related li{margin:0;padding:11px 0;border-bottom:1px solid #142d3e}.section-related li:last-child{border-bottom:0}.section-related li a{color:#c9e5eb}
.email-footer{height:12px;border-top:1px solid #132d3e;background:#050e18}
@media only screen and (max-width:600px){
.email-outer{padding:0!important}.email-container{border-right:0!important;border-left:0!important;border-radius:0!important}.brand-bar{padding:16px 18px 13px!important}.brand-note{display:none!important}.email-content{padding:0 18px 20px!important;font-size:14px!important}.digest-hero{margin:0 -18px!important;padding:25px 18px 20px!important}.pipeline-stage{padding-right:2px!important;padding-left:2px!important}.pipeline-value{font-size:16px!important}.pipeline-connector{width:6%!important;font-size:13px!important}.section-conclusion{padding:20px 16px 17px!important}.signal-card{padding:17px 15px 15px!important}.signal-heading{display:block!important}.signal-index{display:inline-block!important;margin-right:8px!important}.signal-tier{display:inline-block!important;margin:9px 0 0!important}h1{font-size:24px!important}h2{font-size:19px!important}h4{font-size:15px!important}.digest-nav a,.feedback-bar a{padding:6px 9px!important}
}
@media (prefers-reduced-motion:reduce){.spectrum-scan{animation:none!important;transform:none!important;opacity:.45!important}}
</style>
</head>
<body style="margin:0;padding:0;background:#040912;color:#e7f1f6">
<table role="presentation" class="email-shell" width="100%" bgcolor="#040912">
<tr><td class="email-outer" align="center" style="padding:34px 14px 48px">
<table role="presentation" class="email-container" width="100%" bgcolor="#07131f" style="width:100%;max-width:700px;background:#07131f;border:1px solid #173348;border-radius:18px">
<tr><td class="brand-bar" style="padding:20px 28px 15px;border-bottom:1px solid #173348;background:#050e18">
<table role="presentation" class="brand-row" width="100%"><tr>
<td class="brand-identity"><span class="brand-orbit"><span class="brand-mark"></span></span><span class="brand-name">SIGNAL RADAR</span></td>
<td align="right"><span class="brand-note">每日个性化 AI 情报</span></td>
</tr></table>
<div class="signal-spectrum" aria-hidden="true"><span class="quiet"></span><span class="short"></span><span class="pulse"></span><span class="medium"></span><span class="beacon"></span><span class="short"></span><span class="pulse"></span><span class="long"></span><span class="spectrum-scan"></span></div>
</td></tr>
<tr><td class="email-content" style="padding:0 32px 28px;color:#93a9b8;font-size:14px;line-height:1.78">__DIGEST_CONTENT__</td></tr>
<tr><td class="email-footer" height="12" style="height:12px;border-top:1px solid #132d3e;background:#050e18">&nbsp;</td></tr>
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
