"""PDF rendering (reportlab: pure Python, no browser or system libraries, so it behaves the same
on a laptop and in the Render container).

Text from the account is shown as text: every string goes through `text()`, which escapes the
markup reportlab would otherwise interpret and swaps characters the built-in PDF fonts can't draw
(anything outside Windows-1252, such as CJK) for "?", rather than printing black boxes.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from notifications.messages import OUTCOMES

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d1d5db")
HEADER_BG = colors.HexColor("#f3f4f6")
ACCENT = colors.HexColor("#5b4bd1")
STATUS_COLOURS = {
    "operational": colors.HexColor("#15803d"),
    "major_outage": colors.HexColor("#b91c1c"),
    "partial_outage": colors.HexColor("#b45309"),
    "maintenance": colors.HexColor("#475569"),
    "paused": colors.HexColor("#475569"),
    "unknown": colors.HexColor("#475569"),
}
STATUS_WORDS = {
    "operational": "Operational",
    "major_outage": "Outage",
    "partial_outage": "Partial outage",
    "maintenance": "Maintenance",
    "paused": "Not monitored",
    "unknown": "Unknown",
}
OVERALL_WORDS = {
    "operational": "All systems operational",
    "partial_outage": "Partial outage",
    "major_outage": "Major outage",
    "maintenance": "Under maintenance",
    "unknown": "Status unavailable",
}

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def text(value: Any) -> str:
    """Plain text made safe for a Paragraph and for the built-in font."""
    raw = _CONTROL.sub("", "" if value is None else str(value))
    drawable = raw.encode("cp1252", "replace").decode("cp1252")
    return escape(drawable)


def duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {secs} s" if minutes < 10 and secs else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def percent(value: float | None) -> str:
    if value is None:
        return "No data"
    return f"{value:g}%" if float(value).is_integer() else f"{value:.3f}".rstrip("0") + "%"


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def when(moment: datetime | None, tz_name: str) -> str:
    if moment is None:
        return "-"
    return moment.astimezone(_zone(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


def reason(code: str) -> str:
    return OUTCOMES.get(code) or code.replace("_", " ").capitalize()


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            alignment=0,
            textColor=INK,
            spaceAfter=2,
        ),
        "sub": ParagraphStyle(
            "sub", parent=base["Normal"], fontSize=10, textColor=MUTED, leading=14
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            textColor=INK,
            spaceBefore=14,
            spaceAfter=6,
        ),
        "cell": ParagraphStyle(
            "cell", parent=base["Normal"], fontSize=8.5, leading=11, textColor=INK
        ),
        "head": ParagraphStyle(
            "head",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=11,
            textColor=INK,
        ),
        "note": ParagraphStyle(
            "note", parent=base["Normal"], fontSize=8.5, leading=12, textColor=MUTED
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontSize=9.5, leading=13, textColor=INK
        ),
        "foot": ParagraphStyle(
            "foot", parent=base["Normal"], fontSize=8, textColor=MUTED, alignment=TA_CENTER
        ),
    }


def _table(
    rows: list[list[Any]], widths: list[float], styles, *, align_right: tuple[int, ...] = ()
):
    head, *body = rows
    data = [[Paragraph(text(c), styles["head"]) for c in head]]
    for row in body:
        data.append(
            [c if not isinstance(c, str) else Paragraph(text(c), styles["cell"]) for c in row]
        )
    table = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    table.setStyle(TableStyle(style))
    return table


def _document(buffer, title: str, footer: str, styles):
    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, footer)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(ACCENT)
        canvas.setLineWidth(2)
        canvas.line(18 * mm, A4[1] - 12 * mm, A4[0] - 18 * mm, A4[1] - 12 * mm)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="RootPulse",
        creator="RootPulse",
    )
    return doc, decorate


WIDTH = A4[0] - 36 * mm


def uptime_report_pdf(report: dict[str, Any], tz_name: str) -> bytes:
    styles = _styles()
    period, summary = report["period"], report["summary"]
    title = f"Uptime report {period['start']} to {period['end']}"
    buffer = io.BytesIO()
    doc, decorate = _document(buffer, title, "RootPulse uptime report", styles)

    story: list[Any] = [
        Paragraph("Uptime report", styles["title"]),
        Paragraph(
            text(f"{period['start']} to {period['end']} ({period['days']} days, UTC days)"),
            styles["sub"],
        ),
        Paragraph(text(f"Generated {when(report['generated_at'], tz_name)}"), styles["sub"]),
        Paragraph("Summary", styles["h2"]),
    ]
    facts = [
        ["Measure", "Value"],
        ["Monitors with data", str(summary["monitors"])],
        ["Uptime", percent(summary["uptime_percent"])],
        ["Checks", f"{summary['checks']:,}"],
        [
            "Average response",
            f"{summary['avg_response_ms']} ms" if summary["avg_response_ms"] is not None else "-",
        ],
        ["Incidents", str(summary["incidents"])],
        ["Total downtime", duration(summary["downtime_seconds"])],
        ["Longest incident", duration(summary["longest_incident_seconds"])],
        ["Average time to recover", duration(summary["mttr_seconds"])],
    ]
    story.append(_table(facts, [WIDTH * 0.45, WIDTH * 0.55], styles))
    if summary["excluded_incidents"]:
        story.append(Spacer(1, 4))
        story.append(
            Paragraph(
                text(
                    f"{summary['excluded_incidents']} incident(s) you marked as excluded from "
                    "reports are left out of every figure here."
                ),
                styles["note"],
            )
        )

    story.append(Paragraph("Monitors", styles["h2"]))
    if report["monitors"]:
        rows = [["Monitor", "Uptime", "Checks", "Avg response", "Incidents", "Downtime"]]
        for m in report["monitors"]:
            rows.append(
                [
                    m["name"],
                    percent(m["uptime_percent"]),
                    f"{m['checks']:,}",
                    f"{m['avg_response_ms']} ms" if m["avg_response_ms"] is not None else "-",
                    str(m["incidents"]),
                    duration(m["downtime_seconds"]),
                ]
            )
        w = WIDTH
        story.append(
            _table(rows, [w * 0.34, w * 0.13, w * 0.12, w * 0.15, w * 0.11, w * 0.15], styles)
        )
    else:
        story.append(
            Paragraph("No monitor had checks or incidents in this period.", styles["body"])
        )

    story.append(Paragraph("Incidents", styles["h2"]))
    if report["incidents"]:
        rows = [["Monitor", "Started", "Duration", "Cause", "State"]]
        for i in report["incidents"]:
            rows.append(
                [
                    i["monitor"],
                    when(i["started_at"], tz_name),
                    duration(i["duration_seconds"]),
                    reason(i["reason"]) + (f" ({i['status_code']})" if i["status_code"] else ""),
                    "Ongoing" if i["ongoing"] else "Resolved",
                ]
            )
        w = WIDTH
        story.append(_table(rows, [w * 0.24, w * 0.24, w * 0.14, w * 0.26, w * 0.12], styles))
        if report["incidents_truncated"]:
            story.append(Spacer(1, 4))
            story.append(Paragraph("The list shows the most recent 500 incidents.", styles["note"]))
    else:
        story.append(Paragraph("No incidents in this period.", styles["body"]))

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()


def status_page_pdf(page: dict[str, Any], tz_name: str) -> bytes:
    """A snapshot of a status page, from the same data the public page is built from."""
    styles = _styles()
    buffer = io.BytesIO()
    doc, decorate = _document(
        buffer, f"{page['name']} status", "RootPulse status page report", styles
    )
    overall = page["overall"]["status"]
    banner = ParagraphStyle(
        "banner", parent=styles["h2"], textColor=STATUS_COLOURS[overall], spaceBefore=10
    )

    story: list[Any] = [
        Paragraph(text(page["name"]), styles["title"]),
        Paragraph(text(f"Status as of {when(page['generated_at'], tz_name)}"), styles["sub"]),
    ]
    if page["branding"].get("description"):
        story.append(Paragraph(text(page["branding"]["description"]), styles["sub"]))
    story.append(Paragraph(text(OVERALL_WORDS[overall]), banner))

    story.append(Paragraph("Services", styles["h2"]))
    if page["components"]:
        rows = [["Service", "Group", "Status", f"Uptime, {page['uptime_days']} days"]]
        for c in page["components"]:
            word = Paragraph(
                f'<font color="{STATUS_COLOURS[c["status"]].hexval().replace("0x", "#")}">'
                f"{text(STATUS_WORDS[c['status']])}</font>",
                styles["cell"],
            )
            rows.append([c["name"], c["group"] or "-", word, percent(c["uptime_percent"])])
        w = WIDTH
        story.append(_table(rows, [w * 0.38, w * 0.22, w * 0.2, w * 0.2], styles))
    else:
        story.append(Paragraph("Nothing is listed on this page yet.", styles["body"]))

    announcements = page["announcements"]
    updates = announcements["active"] + announcements["past"]
    if updates:
        story.append(Paragraph("Announcements", styles["h2"]))
        for a in updates:
            kind = "Planned maintenance" if a["kind"] == "maintenance" else "Incident"
            state = a["state"].replace("_", " ")
            story.append(
                Paragraph(
                    f"<b>{text(a['title'])}</b> ({text(kind)}, {text(state)})", styles["body"]
                )
            )
            if a["kind"] == "maintenance" and a["starts_at"] and a["ends_at"]:
                story.append(
                    Paragraph(
                        text(f"{when(a['starts_at'], tz_name)} to {when(a['ends_at'], tz_name)}"),
                        styles["note"],
                    )
                )
            if a["body"]:
                story.append(Paragraph(text(a["body"]).replace("\n", "<br/>"), styles["note"]))
            story.append(Spacer(1, 6))

    story.append(Paragraph("Outages, last 14 days", styles["h2"]))
    if page["incidents"]:
        rows = [["Service", "Started", "Ended", "Notes"]]
        for i in page["incidents"]:
            notes = "\n".join(u["body"] for u in i["updates"])
            rows.append(
                [
                    i["component"],
                    when(i["started_at"], tz_name),
                    "Ongoing" if i["ongoing"] else when(i["ended_at"], tz_name),
                    Paragraph(text(notes or "-").replace("\n", "<br/>"), styles["cell"]),
                ]
            )
        w = WIDTH
        story.append(_table(rows, [w * 0.2, w * 0.22, w * 0.22, w * 0.36], styles))
    else:
        story.append(Paragraph("No outages reported.", styles["body"]))

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()
