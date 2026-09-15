"""FormatProfile のテンプレートから minutes.md を作る。HTML・外部埋め込みによる漏えいを防ぐ。"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from audio_minutes_contracts.models import FormatProfile, Segment

from minutes_worker.prompting import format_ms

_VARIABLE = re.compile(r"\{\{\s*([a-z_]+)(?::([a-z_]+))?\s*\}\}")
_IMAGE = re.compile(r"!\[")
_AUTOLINK = re.compile(r"<(https?://[^>]+)>")

KNOWN_VARIABLES = {"title", "started_at", "duration"}


def sanitize_markdown(text: str) -> str:
    """モデル出力の Markdown から HTML タグ・自動リンク・画像埋め込みを無害化する。"""
    text = _AUTOLINK.sub(r"\1", text)
    text = html.escape(text, quote=False)
    return _IMAGE.sub("!\\\\[", text)


def unknown_template_variables(template: str) -> list[str]:
    unknown = []
    for match in _VARIABLE.finditer(template):
        name, argument = match.group(1), match.group(2)
        if name == "section":
            if argument is None:
                unknown.append(match.group(0))
            continue
        if name not in KNOWN_VARIABLES or argument is not None:
            unknown.append(match.group(0))
    return unknown


def render_references(segments: list[Segment], referenced_ids: list[str]) -> str:
    by_id = {segment.id: segment for segment in segments}
    lines = []
    for segment_id in referenced_ids:
        segment = by_id.get(segment_id)
        if segment is None:
            continue
        lines.append(f"- `{segment.id}` {format_ms(segment.start_ms)}–{format_ms(segment.end_ms)} ({segment.source})")
    return "\n".join(lines) if lines else "- (参照なし)"


def render_minutes(
    profile: FormatProfile,
    output: dict[str, Any],
    *,
    title: str,
    started_at: datetime | None,
    duration_ms: int | None,
    segments: list[Segment],
) -> str:
    sections_by_key = {section["key"]: section for section in output.get("sections", []) if isinstance(section, dict)}
    referenced: list[str] = []
    for section in output.get("sections", []):
        for reference in section.get("references", []):
            if reference not in referenced:
                referenced.append(reference)

    def section_markdown(key: str) -> str:
        definition = next((item for item in profile.sections if item.key == key), None)
        if definition is None or not definition.enabled:
            return ""
        heading = f"## {sanitize_markdown(definition.title)}"
        if key == "transcript_references":
            return f"{heading}\n\n{render_references(segments, referenced)}"
        section = sections_by_key.get(key)
        body = sanitize_markdown(section["markdown"]) if section else "_(該当する内容は文字起こしにありませんでした)_"
        refs = ""
        if section and section.get("references"):
            refs = "\n\n_根拠: " + ", ".join(f"`{reference}`" for reference in section["references"]) + "_"
        return f"{heading}\n\n{body.strip()}{refs}"

    def replace(match: re.Match[str]) -> str:
        name, argument = match.group(1), match.group(2)
        if name == "title":
            return sanitize_markdown(title)
        if name == "started_at":
            return started_at.isoformat() if started_at else "不明"
        if name == "duration":
            return format_ms(duration_ms) if duration_ms is not None else "不明"
        if name == "section" and argument:
            return section_markdown(argument)
        return match.group(0)

    rendered = _VARIABLE.sub(replace, profile.template_markdown)
    insufficient = output.get("insufficient_information") or []
    if insufficient:
        rendered = rendered.rstrip() + "\n\n## 情報不足\n\n" + "\n".join(
            f"- {sanitize_markdown(item)}" for item in insufficient
        )
    return re.sub(r"\n{3,}", "\n\n", rendered).strip() + "\n"


__all__ = ["KNOWN_VARIABLES", "render_minutes", "render_references", "sanitize_markdown", "unknown_template_variables"]
