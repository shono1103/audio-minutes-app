"""固定 revision の文字起こしモデルを事前取得し、通常処理前に完全性を検証する。

取得中は既存の ``manifest.json`` を置換しない。中断時は marker と Hugging Face の
部分 cache を残し、同じコマンドで再開できる。通常処理と smoke は必ず offline で行う。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from transcription_worker.config import ModelSetting, WorkerConfig

MANIFEST_SCHEMA = "model-manifest/1"
IN_PROGRESS_FILE = ".prefetch-in-progress.json"
SMOKE_FILE = "smoke.json"
DEFAULT_MIN_FREE_BYTES = 8 * 1024**3


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _file_entries(root: Path) -> list[dict[str, object]]:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            entries.append(
                {"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": _sha256(path)}
            )
    if not entries:
        raise FileNotFoundError(f"モデル snapshot が空です: {root}")
    return entries


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _relative_snapshot(path: Path, models_dir: Path) -> str:
    root = models_dir.resolve()
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(root))
    except ValueError as error:
        raise ValueError(f"snapshot が models volume の外を指しています: {resolved}") from error


def prefetch_faster_whisper(
    setting: ModelSetting, models_dir: Path, *, offline: bool, force_download: bool = False
) -> dict[str, object]:
    from huggingface_hub import snapshot_download

    path = Path(
        snapshot_download(
            repo_id=setting.model_id,
            revision=setting.revision,
            cache_dir=str(models_dir / "hub"),
            local_files_only=offline,
            force_download=force_download,
        )
    )
    return {
        "engine": "faster-whisper",
        "model_id": setting.model_id,
        "revision": setting.revision,
        "compute_type": setting.compute_type,
        "resolved_path": _relative_snapshot(path, models_dir),
        "files": _file_entries(path),
    }


def describe_ggml(setting: ModelSetting, ggml_dir: Path, models_dir: Path | None = None) -> dict[str, object]:
    if not setting.ggml_file:
        raise FileNotFoundError(f"ggml ファイル名が未設定: {setting.model_id}")
    path = ggml_dir / setting.ggml_file
    if not path.is_file():
        raise FileNotFoundError(f"ggml モデルがありません: {path}")
    root = models_dir or ggml_dir.parent
    return {
        "engine": "whisper.cpp",
        "model_id": setting.model_id,
        "revision": setting.revision,
        "quantization": setting.quantization,
        "resolved_path": _relative_snapshot(ggml_dir, root),
        "files": [{"path": setting.ggml_file, "bytes": path.stat().st_size, "sha256": _sha256(path)}],
        "note": "変換ツール commit・引数は benchmarks/ の manifest に記録する",
    }


def write_manifest(models_dir: Path, entries: list[dict[str, object]]) -> Path:
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "complete": True,
        "models": entries,
    }
    path = models_dir / "manifest.json"
    _atomic_json(path, manifest)
    return path


def expected_models(config: WorkerConfig) -> dict[tuple[str, str], ModelSetting]:
    return {
        (config.model_multilingual.model_id, config.model_multilingual.revision): config.model_multilingual,
        (config.model_ja.model_id, config.model_ja.revision): config.model_ja,
    }


def verify_manifest(
    models_dir: Path,
    config: WorkerConfig | None = None,
    *,
    check_hashes: bool = True,
    report_interrupted: bool = True,
) -> list[str]:
    """manifest・固定 revision・layout・size・hash を検査し、不一致を返す。"""
    problems: list[str] = []
    if report_interrupted and (models_dir / IN_PROGRESS_FILE).is_file():
        problems.append("前回のモデル取得が中断されています (prefetch を再実行してください)")
    manifest_path = models_dir / "manifest.json"
    if not manifest_path.is_file():
        return [*problems, "manifest.json がありません"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [*problems, f"manifest.json を読めません: {type(error).__name__}"]
    if manifest.get("schema_version") != MANIFEST_SCHEMA or manifest.get("complete") is not True:
        problems.append("manifest.json が完成済みの対応版ではありません")
    raw_models = manifest.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        return [*problems, "manifest.json にモデルがありません"]

    found: set[tuple[str, str]] = set()
    root = models_dir.resolve()
    for model in raw_models:
        if not isinstance(model, dict):
            problems.append("manifest.json のモデル項目が不正です")
            continue
        model_id = model.get("model_id")
        revision = model.get("revision")
        if not isinstance(model_id, str) or not isinstance(revision, str):
            problems.append("model_id または revision がありません")
            continue
        found.add((model_id, revision))
        relative = model.get("resolved_path")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            problems.append(f"不正な snapshot path: {model_id}")
            continue
        model_root = models_dir / relative
        try:
            model_root.resolve().relative_to(root)
        except ValueError:
            problems.append(f"snapshot path が models volume の外です: {model_id}")
            continue
        files = model.get("files")
        if not isinstance(files, list) or not files:
            problems.append(f"ファイル一覧が空です: {model_id}")
            continue
        for entry in files:
            if not isinstance(entry, dict):
                problems.append(f"ファイル項目が不正です: {model_id}")
                continue
            relative_file = entry.get("path")
            if not isinstance(relative_file, str) or Path(relative_file).is_absolute() or ".." in Path(relative_file).parts:
                problems.append(f"不正なファイル path: {model_id}")
                continue
            path = model_root / relative_file
            if not path.is_file():
                problems.append(f"欠落: {model_id} {relative_file}")
                continue
            expected_size = entry.get("bytes")
            if not isinstance(expected_size, int) or expected_size <= 0 or path.stat().st_size != expected_size:
                problems.append(f"size 不一致: {model_id} {relative_file}")
                continue
            if check_hashes and _sha256(path) != entry.get("sha256"):
                problems.append(f"hash 不一致: {model_id} {relative_file}")

    if config is not None:
        required = set(expected_models(config))
        for model_id, revision in sorted(required - found):
            problems.append(f"固定モデルがありません: {model_id}@{revision}")
        for model_id, revision in sorted(found - required):
            problems.append(f"設定外のモデルです: {model_id}@{revision}")
    return problems


def model_status(models_dir: Path, config: WorkerConfig, *, check_hashes: bool = False) -> dict[str, Any]:
    problems = verify_manifest(models_dir, config, check_hashes=check_hashes)
    smoke_path = models_dir / SMOKE_FILE
    try:
        smoke_document = json.loads(smoke_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        smoke_document = None
    expected = set(expected_models(config))
    smoked = {
        (item.get("model_id"), item.get("revision"))
        for item in (smoke_document or {}).get("models", [])
        if isinstance(item, dict)
    }
    if not isinstance(smoke_document, dict) or smoke_document.get("ready") is not True or smoked != expected:
        problems.append("現在の固定モデルに対する offline smoke が完了していません")
    return {
        "ready": not problems,
        "engine": config.engine,
        "backend": config.backend,
        "offline": config.hf_offline,
        "models": [
            {"profile": profile, "model_id": ref.model_id, "revision": ref.revision}
            for profile, ref in (("multilingual", config.model_multilingual), ("ja", config.model_ja))
        ],
        "errors": problems,
    }


def prefetch(config: WorkerConfig, models_dir: Path, *, online: bool, min_free_bytes: int) -> Path:
    # 完成済みなら network に触れず終了する。中断 marker があっても manifest が完全なら除去できる。
    existing_problems = verify_manifest(models_dir, config, report_interrupted=False)
    if not existing_problems:
        (models_dir / IN_PROGRESS_FILE).unlink(missing_ok=True)
        return models_dir / "manifest.json"
    free = shutil.disk_usage(models_dir).free
    if free < min_free_bytes:
        raise OSError(f"モデル取得用の空き容量が不足しています: free={free} required={min_free_bytes}")
    marker = {
        "schema_version": "model-prefetch/1",
        "started_at": datetime.now(UTC).isoformat(),
        "models": [
            {"model_id": model_id, "revision": revision}
            for model_id, revision in expected_models(config)
        ],
    }
    _atomic_json(models_dir / IN_PROGRESS_FILE, marker)
    entries: list[dict[str, object]] = []
    force_download = online and any("hash 不一致" in problem or "size 不一致" in problem for problem in existing_problems)
    for setting in (config.model_multilingual, config.model_ja):
        if config.engine == "faster-whisper":
            entries.append(
                prefetch_faster_whisper(
                    setting, models_dir, offline=not online, force_download=force_download
                )
            )
        else:
            entries.append(describe_ggml(setting, config.whisper_cpp_model_dir, models_dir))
    path = write_manifest(models_dir, entries)
    problems = verify_manifest(models_dir, config, report_interrupted=False)
    if problems:
        raise RuntimeError("取得後の検証に失敗しました: " + "; ".join(problems))
    (models_dir / IN_PROGRESS_FILE).unlink(missing_ok=True)
    return path


def smoke(config: WorkerConfig) -> dict[str, Any]:
    """offline で両モデルを load し、短い合成 PCM を各モデルへ一度だけ通す。"""
    if config.backend != "cpu" or config.engine != "faster-whisper":
        raise RuntimeError("MVP の model smoke は faster-whisper CPU profile 専用です")
    if not config.hf_offline:
        raise RuntimeError("model smoke は HF_HUB_OFFLINE=1 でだけ実行できます")
    problems = verify_manifest(config.models_dir, config)
    if problems:
        raise RuntimeError("モデル検証に失敗しました: " + "; ".join(problems))
    rate = 16_000
    times = np.arange(rate // 2, dtype=np.float32) / rate
    audio = (0.05 * np.sin(2 * np.pi * 440.0 * times)).astype(np.float32)
    from transcription_worker.engines.base import TranscribeOptions
    from transcription_worker.pipeline.transcribe import engine_factory, model_refs

    factory = engine_factory(config)
    loaded = []
    for profile, ref in model_refs(config).items():
        engine = factory(ref)
        language = "ja" if profile == "ja" else "en"
        try:
            engine.load()
            engine.transcribe(audio, language, TranscribeOptions(beam_size=1))
            loaded.append(
                {
                    "profile": profile,
                    "model_id": ref.model_id,
                    "revision": ref.revision,
                    "checks": {"loaded": True, "synthetic_transcription": True},
                    "language_argument": language,
                }
            )
        finally:
            engine.unload()
    document = {
        "ready": True,
        "completed_at": datetime.now(UTC).isoformat(),
        "engine": config.engine,
        "backend": "cpu",
        "offline": True,
        "manifest": {"layout": True, "size": True, "sha256": True},
        "synthetic_audio": {"sample_rate": rate, "duration_ms": 500},
        "models": loaded,
    }
    _atomic_json(config.models_dir / SMOKE_FILE, document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="モデルの事前取得・完全性検証・offline smoke")
    parser.add_argument("command", choices=("prefetch", "verify", "status", "smoke"))
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--online", action="store_true", help="prefetch 時だけ Hugging Face へ接続する")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--min-free-bytes",
        type=int,
        default=int(os.environ.get("AM_MODEL_MIN_FREE_BYTES", str(DEFAULT_MIN_FREE_BYTES))),
    )
    args = parser.parse_args(argv)
    config = WorkerConfig.from_env()
    models_dir = args.models_dir or config.models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    if models_dir != config.models_dir:
        from dataclasses import replace

        config = replace(config, models_dir=models_dir)

    try:
        if args.command == "prefetch":
            path = prefetch(config, models_dir, online=args.online, min_free_bytes=args.min_free_bytes)
            if not args.quiet:
                print(f"manifest: {path}")
            return 0
        if args.command == "smoke":
            document = smoke(config)
        elif args.command == "verify":
            problems = verify_manifest(models_dir, config, check_hashes=True)
            document = {"ready": not problems, "errors": problems}
        else:
            document = model_status(models_dir, config, check_hashes=False)
    except Exception as error:  # noqa: BLE001 - CLI 境界で秘密を含まない要約だけを返す
        if not args.quiet:
            print(f"モデル準備に失敗しました: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(document, ensure_ascii=False))
    elif not args.quiet:
        for problem in document.get("errors", []):
            print(problem, file=sys.stderr)
        print("ok" if document["ready"] else f"{len(document['errors'])} 件の問題")
    return 0 if document["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
