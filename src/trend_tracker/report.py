import html
import re
import smtplib
import ssl
import urllib.parse
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List

from .http import HttpClient
from .models import MatchResult, Track


def build_report(
    generated_at: datetime,
    scanned_count: int,
    tracks: List[Track],
    matches_by_track: Dict[str, List[MatchResult]],
    trends_by_track: Dict[str, str],
) -> str:
    lines = [
        "# AI Trend Tracker — {0}".format(generated_at.strftime("%Y-%m-%d")),
        "",
        "Generated at {0}. Scanned **{1}** deduplicated observations.".format(generated_at.isoformat(), scanned_count),
        "",
    ]
    for track in tracks:
        matches = matches_by_track.get(track.id, [])
        relevant = [match for match in matches if match.score >= 50]
        high = [match for match in relevant if match.score >= 80]
        lines.extend(
            [
                "## Track: {0}".format(track.name),
                "",
                "> {0}".format(track.goal.replace("\n", " ")),
                "",
                "Found **{0}** possible matches, including **{1}** high-relevance signals.".format(len(relevant), len(high)),
                "",
                "### Trend synthesis",
                "",
                trends_by_track.get(track.id, "No trend synthesis generated."),
                "",
                "### Ranked signals",
                "",
            ]
        )
        if not relevant:
            lines.extend(["No item crossed the relevance threshold.", ""])
            continue
        for index, match in enumerate(relevant[:15], 1):
            target = match.item.product_url or match.item.url
            lines.extend(
                [
                    "#### {0}. [{1}]({2}) — {3:.0f}/100".format(index, match.item.title, target, match.score),
                    "",
                    "- Source: `{0}` · Published: {1}".format(match.item.source_key, match.item.published_at.strftime("%Y-%m-%d %H:%M UTC")),
                    "- Why it matches: {0}".format(match.reason or "No reason supplied."),
                    "- Original evidence: [{0}]({1})".format(match.item.url, match.item.url),
                ]
            )
            if match.item.summary:
                lines.append("- Summary: {0}".format(match.item.summary[:600]))
            if match.analysis:
                lines.extend(["", "**Deep analysis**", "", match.analysis])
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_report(content: str, directory: str = "reports") -> Path:
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    report_path = path / (datetime.now().strftime("%Y-%m-%d") + ".md")
    report_path.write_text(content, encoding="utf-8")
    return report_path


def email_recipients(value: str) -> List[str]:
    return [row.strip() for row in re.split(r"[,;\n]+", value or "") if row.strip()]


def send_email(http: HttpClient, api_key: str, sender: str, recipient: str, subject: str, report: str) -> None:
    recipients = email_recipients(recipient)
    if not (api_key and sender and recipients):
        return
    escaped = html.escape(report)
    body = "<html><body><pre style=\"white-space:pre-wrap;font-family:system-ui,sans-serif\">{0}</pre></body></html>".format(escaped)
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
    escaped = html.escape(report)
    message.add_alternative(
        "<html><body><pre style=\"white-space:pre-wrap;font-family:system-ui,sans-serif\">{0}</pre></body></html>".format(escaped),
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
