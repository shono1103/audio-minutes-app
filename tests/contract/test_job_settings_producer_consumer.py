"""ジョブ設定の producer (minutes-api) と consumer (各 worker) が同じ契約を使うことを確認する。

R05 / R09 の受け入れ条件。API が実際に組み立てた payload を worker 側の解釈へ渡し、
外部送信の許可・接続 owner・タイトルが欠けたり読み替えられたりしないことを機械検証する。
実装パッケージを import せず、共有契約 (audio_minutes_contracts) と fixture だけで閉じる。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from audio_minutes_contracts import schemas
from audio_minutes_contracts.models import (
    FormatProfile,
    InputKind,
    Job,
    LanguageMode,
    MinutesJobSettings,
    TranscriptionJobSettings,
)
from pydantic import ValidationError

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"
OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_OWNER = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _standard_profile() -> FormatProfile:
    return FormatProfile.model_validate_json((FIXTURES / "format-profile.standard.json").read_text(encoding="utf-8"))


def _api_minutes_settings(**overrides) -> dict:
    """minutes-api の enqueue_minutes と同じ組み立て方で payload を作る。"""
    fields = {
        "kind": "claude_generated",
        "allow_external_send": True,
        "connected_owner_id": OWNER,
        "title": "週次定例 (利用者が編集)",
        "title_edited_by_user": True,
        "input_kind": InputKind.RECORDED_DUAL_TRACK,
        "format_snapshot": _standard_profile(),
        "instructions": None,
        "started_at": datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
        "duration_ms": 1_800_000,
        "transcript_revision": 1,
        "expected_current_version_id": None,
    }
    fields.update(overrides)
    return json.loads(MinutesJobSettings(**fields).model_dump_json())


def _preflight(settings_payload: dict, *, job_owner_id: uuid.UUID) -> MinutesJobSettings:
    """minutes-worker の _preflight と同じ判定を契約だけで再現する。"""
    settings = MinutesJobSettings.model_validate(settings_payload)
    if not settings.allow_external_send:
        raise PermissionError("external_send_forbidden")
    if settings.connected_owner_id is None or settings.connected_owner_id != job_owner_id:
        raise PermissionError("not_connected_owner")
    return settings


def test_fixtures_match_both_representations() -> None:
    """fixture が JSON Schema と pydantic 表現の両方で解釈できる。"""
    for name in ("job.transcription.json", "job.minutes.json"):
        document = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        assert schemas.errors("job", document) == [], name
        job = Job.model_validate(document)
        if job.kind.value == "transcription":
            TranscriptionJobSettings.model_validate(job.settings)
        else:
            MinutesJobSettings.model_validate(job.settings)


def test_api_payload_passes_worker_preflight() -> None:
    """API が作った payload を worker がそのまま受け取れる (本人・許可あり)。"""
    payload = _api_minutes_settings()
    settings = _preflight(payload, job_owner_id=OWNER)
    assert settings.allow_external_send is True
    assert settings.connected_owner_id == OWNER


@pytest.mark.parametrize(
    "overrides, job_owner_id, expected",
    [
        ({"allow_external_send": False}, OWNER, "external_send_forbidden"),
        ({"connected_owner_id": OTHER_OWNER}, OWNER, "not_connected_owner"),
        ({"connected_owner_id": None}, OWNER, "not_connected_owner"),
    ],
)
def test_forbidden_and_other_owner_are_rejected(overrides, job_owner_id, expected) -> None:
    with pytest.raises(PermissionError) as raised:
        _preflight(_api_minutes_settings(**overrides), job_owner_id=job_owner_id)
    assert str(raised.value) == expected


def test_missing_consent_fields_are_rejected_by_the_contract() -> None:
    """送信許可・接続 owner が欠けた payload は契約の時点で通らない。"""
    payload = _api_minutes_settings()
    for key in ("allow_external_send", "connected_owner_id"):
        broken = {name: value for name, value in payload.items() if name != key}
        with pytest.raises(ValidationError):
            MinutesJobSettings.model_validate(broken)


def test_title_key_is_shared_between_producer_and_consumer() -> None:
    """タイトルのキーが producer/consumer で一致する (R09)。

    別名 (session_title 等) を使うと、利用者が編集したタイトルが worker 側で
    「無題の会議」に落ちる。契約に無いキーは拒否されることまで確認する。
    """
    payload = _api_minutes_settings(title="編集済みタイトル", title_edited_by_user=True)
    assert payload["title"] == "編集済みタイトル"
    settings = MinutesJobSettings.model_validate(payload)
    assert settings.title == "編集済みタイトル" and settings.title_edited_by_user is True

    with pytest.raises(ValidationError):
        MinutesJobSettings.model_validate({**payload, "session_title": "別名"})


def test_transcription_settings_separate_revision_from_idempotency() -> None:
    """transcript の版と投入の冪等キーを混同しない (R07)。"""
    settings = TranscriptionJobSettings(
        input_kind=InputKind.RECORDED_DUAL_TRACK,
        language_mode=LanguageMode.AUTO,
        transcript_revision=2,
        max_audio_ms=14_400_000,
    )
    payload = json.loads(settings.model_dump_json())
    assert payload["transcript_revision"] == 2
    # 冪等キーは settings ではなく jobs.idempotency_key が持つ
    assert "idempotency_key" not in payload
