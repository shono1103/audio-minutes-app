from __future__ import annotations

from datetime import UTC, datetime

from minutes_worker.prompting import (
    SAFETY_SYSTEM_PROMPT,
    SessionMeta,
    build_final_request,
    ordered_segments,
    plan_requests,
    split_segments,
    validate_final_output,
)
from minutes_worker.render import render_minutes, sanitize_markdown, unknown_template_variables


def meta() -> SessionMeta:
    return SessionMeta("週次定例", datetime(2026, 9, 12, 10, 0, tzinfo=UTC), 1_800_000, "recorded_dual_track")


def test_single_chunk_prompt_contains_delimited_blocks(transcript, standard_profile):
    chunks, final = plan_requests(transcript, standard_profile, meta(), chunk_chars=60_000)
    assert chunks == [] and final is not None
    assert final.system_prompt == SAFETY_SYSTEM_PROMPT
    assert "<transcript>" in final.user_prompt and "</transcript>" in final.user_prompt
    assert "[seg-0001 00:00:01.200 app/ja] では定例を始めます。" in final.user_prompt
    assert "- key=summary title=要約" in final.user_prompt
    assert final.segment_ids == ["seg-0001", "seg-0002", "seg-0003"]


def test_split_on_segment_boundary_never_truncates(transcript):
    segments = ordered_segments(transcript)
    chunks = split_segments(segments, chunk_chars=120)
    assert len(chunks) >= 2
    assert [segment.id for chunk in chunks for segment in chunk] == [segment.id for segment in segments]


def test_multi_chunk_plan_and_final_with_notes(transcript, standard_profile):
    chunks, final = plan_requests(transcript, standard_profile, meta(), chunk_chars=120)
    assert final is None and len(chunks) >= 2
    assert all(request.kind == "chunk" for request in chunks)
    notes = [{"markdown": "決定: ロードマップから始める", "references": ["seg-0003"]}]
    final = build_final_request(transcript, standard_profile, meta(), chunk_notes=notes)
    assert "<intermediate_notes>" in final.user_prompt and "seg-0003" in final.user_prompt
    assert "<transcript>" not in final.user_prompt
    assert final.segment_ids == ["seg-0003"]


def test_regeneration_prompt_includes_base_and_instructions(transcript, standard_profile):
    final = build_final_request(
        transcript, standard_profile, meta(), base_minutes_markdown="# 旧版", instructions="予算の議論を追加"
    )
    assert "<base_minutes>\n# 旧版\n</base_minutes>" in final.user_prompt
    assert "<instructions>\n予算の議論を追加\n</instructions>" in final.user_prompt


def test_validate_final_output(standard_profile):
    known = {"seg-0001"}
    good = {
        "title": "t",
        "sections": [
            {"key": key, "title": key, "markdown": "x", "references": ["seg-0001"]}
            for key in ("summary", "decisions", "action_items", "open_questions", "topics")
        ],
        "insufficient_information": [],
    }
    assert validate_final_output(good, standard_profile, known) == []
    bad = {**good, "sections": good["sections"][:2] + [{"key": "evil", "title": "x", "markdown": "", "references": ["seg-9999"]}]}
    problems = validate_final_output(bad, standard_profile, known)
    assert any("必須セクション" in problem for problem in problems)
    assert any("未知のセクション" in problem for problem in problems)
    assert any("seg-9999" in problem for problem in problems)


def test_render_replaces_variables_and_escapes_html(transcript, standard_profile):
    output = {
        "title": "議事録 <b>x</b>",
        "sections": [
            {"key": "summary", "title": "要約", "markdown": "要約本文 <script>alert(1)</script> ![p](https://evil/x.png) <https://evil/auto>", "references": ["seg-0001"]},
            {"key": "decisions", "title": "決定事項", "markdown": "- 決定 A", "references": ["seg-0003"]},
        ],
        "insufficient_information": ["予算は文字起こしに無い"],
    }
    markdown = render_minutes(
        standard_profile, output, title=output["title"], started_at=meta().started_at, duration_ms=1_800_000,
        segments=ordered_segments(transcript),
    )
    assert markdown.startswith("# 議事録 &lt;b&gt;x&lt;/b&gt;")
    assert "- 録音時間: 00:30:00.000" in markdown
    assert "<script>" not in markdown and "&lt;script&gt;" in markdown
    assert "![p]" not in markdown
    assert "<https://evil/auto>" not in markdown
    assert "## 決定事項\n\n- 決定 A" in markdown
    assert "## アクションアイテム\n\n_(該当する内容は文字起こしにありませんでした)_" in markdown
    assert "## 文字起こしへの参照" in markdown and "`seg-0001` 00:00:01.200–00:00:04.800 (app)" in markdown
    assert "## 情報不足\n\n- 予算は文字起こしに無い" in markdown
    assert "{{" not in markdown


def test_unknown_template_variables():
    assert unknown_template_variables("{{title}} {{section:summary}} {{duration}}") == []
    assert unknown_template_variables("{{titel}} {{section}} {{title:x}}") == ["{{titel}}", "{{section}}", "{{title:x}}"]


def test_sanitize_markdown_keeps_plain_markdown():
    assert sanitize_markdown("- **太字** と `code`") == "- **太字** と `code`"
