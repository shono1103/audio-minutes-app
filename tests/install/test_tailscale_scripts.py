from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def fixture(tmp_path: Path, *, serve: dict | None = None, credentials: int = 0) -> tuple[dict[str, str], Path, Path]:
    bin_dir = tmp_path / "bin"
    state = tmp_path / "serve.json"
    state.write_text(json.dumps(serve or {}), encoding="utf-8")
    calls = tmp_path / "calls"
    executable(
        bin_dir / "tailscale",
        "#!/bin/bash\nset -eu\necho \"tailscale $*\" >>\"$CALLS\"\n"
        "if [[ \"$1 $2\" == 'status --json' ]]; then echo '{\"BackendState\":\"Running\",\"Self\":{\"DNSName\":\"mini.tail.example.ts.net.\"}}'; exit; fi\n"
        "if [[ \"$1 $2 $3\" == 'serve status --json' ]]; then cat \"$SERVE_STATE\"; exit; fi\n"
        "if [[ \"$1\" == serve && \"${*: -1}\" == off ]]; then echo '{}' >\"$SERVE_STATE\"; exit; fi\n"
        "if [[ \"$1\" == serve ]]; then cat >\"$SERVE_STATE\" <<'EOF'\n"
        '{"TCP":{"443":{"HTTPS":true}},"Web":{"mini.tail.example.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8787"}}}}}\n'
        "EOF\n[[ \"${FAIL_SERVE_APPLY:-0}\" != 1 ]] || exit 9\nfi\n",
    )
    executable(
        bin_dir / "docker",
        "#!/bin/bash\nset -eu\necho \"docker $*\" >>\"$CALLS\"\n"
        "if [[ \"$*\" == *'--no-deps --force-recreate minutes-api'* && \"${FAIL_DOCKER_ONCE:-0}\" == 1 && ! -e \"$DOCKER_FAILED\" ]]; then "
        "touch \"$DOCKER_FAILED\"; exit 42; fi\n"
        "if [[ \"$*\" == *\"SELECT EXISTS\"* ]]; then echo 1; "
        "elif [[ \"$*\" == *\"SELECT count\"* ]]; then echo \"$CREDENTIALS\"; fi\n",
    )
    executable(
        bin_dir / "curl",
        "#!/bin/bash\nset -eu\necho \"curl $*\" >>\"$CALLS\"\n"
        "if [[ -n \"${CURL_NOTIFY_FILE:-}\" ]]; then touch \"$CURL_NOTIFY_FILE\"; fi\n"
        "[[ \"${FAIL_CURL:-0}\" != 1 ]] || exit 22\necho '{\"status\":\"ok\"}'\n",
    )
    env_file = tmp_path / "profile.env"
    env_file.write_text(
        "# keep-comment\nAM_API_PORT=8787\nAM_PUBLIC_BASE_URL=http://localhost:8787\n"
        "AM_RP_ID=localhost\nAM_ALLOWED_ORIGINS=http://localhost:8787\nSECRET=preserve-me\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(home),
        "AM_PROFILE_ENV_OVERRIDE": str(env_file),
        "SERVE_STATE": str(state),
        "CALLS": str(calls),
        "CREDENTIALS": str(credentials),
        "DOCKER_FAILED": str(tmp_path / "docker-failed"),
        "AM_TAILSCALE_HEALTH_TIMEOUT": "0",
    }
    return env, env_file, state


def client_settings(env: dict[str, str], *, extra: dict | None = None) -> Path:
    support = Path(env["HOME"]) / "Library/Application Support/AudioMinutes"
    support.mkdir(parents=True, exist_ok=True)
    settings = support / "settings.json"
    value = {
        "schema_version": "client-settings/1",
        "profile": "macos-colima-cpu",
        "api_base_url": "http://localhost:8787",
        "deploy_dir": str(ROOT / "deploy"),
        "keep": "yes",
    }
    value.update(extra or {})
    settings.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
    return settings


def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "scripts/configure-tailscale.sh"), "--profile", "macos-colima-cpu"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_empty_serve_is_applied_and_env_and_client_are_preserved(tmp_path: Path) -> None:
    env, env_file, state = fixture(tmp_path)
    settings = client_settings(env)
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert "TAILSCALE_READY origin=https://mini.tail.example.ts.net" in result.stdout
    content = env_file.read_text(encoding="utf-8")
    assert "AM_PUBLIC_BASE_URL=https://mini.tail.example.ts.net" in content
    assert "AM_RP_ID=mini.tail.example.ts.net" in content
    assert "AM_ALLOWED_ORIGINS=https://mini.tail.example.ts.net" in content
    assert "# keep-comment" in content and "SECRET=preserve-me" in content
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "schema_version": "client-settings/1",
        "profile": "macos-colima-cpu",
        "api_base_url": "https://mini.tail.example.ts.net",
        "deploy_dir": str(ROOT / "deploy"),
        "keep": "yes",
    }
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert settings.stat().st_mode & 0o777 == 0o600
    assert state.read_text(encoding="utf-8") != "{}"
    calls = Path(env["CALLS"]).read_text(encoding="utf-8")
    assert "--no-deps --force-recreate minutes-api" in calls
    assert "serve --bg --yes --https=443 http://127.0.0.1:8787" in calls


def test_exact_serve_rerun_does_not_recreate_serve(tmp_path: Path) -> None:
    exact = {"TCP": {"443": {"HTTPS": True}}, "Web": {"mini.tail.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}, "AllowFunnel": {}, "Services": {}, "Foreground": {}}
    env, env_file, _ = fixture(tmp_path, serve=exact)
    env_file.write_text(env_file.read_text().replace("localhost:8787", "mini.tail.example.ts.net").replace("AM_RP_ID=localhost", "AM_RP_ID=mini.tail.example.ts.net").replace("http://mini", "https://mini"), encoding="utf-8")
    result = run(env)
    assert result.returncode == 0, result.stderr
    calls = Path(env["CALLS"]).read_text(encoding="utf-8")
    assert "serve --bg" not in calls


@pytest.mark.parametrize(
    "conflict",
    [
        {"TCP": {"22": {"TCPForward": "127.0.0.1:22"}}},
        {"TCP": {"443": {"HTTPS": True}}, "Web": {"mini.tail.example.ts.net:443": {"Handlers": {"/other": {"Proxy": "http://127.0.0.1:8787"}}}}},
        {"TCP": {"443": {"HTTPS": True}}, "Web": {"mini.tail.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}, "AllowFunnel": {"mini.tail.example.ts.net:443": True}},
    ],
    ids=["tcp-forward", "path-handler", "funnel"],
)
def test_conflicting_serve_changes_nothing(tmp_path: Path, conflict: dict) -> None:
    env, env_file, state = fixture(tmp_path, serve=conflict)
    before_env, before_state = env_file.read_bytes(), state.read_bytes()
    result = run(env)
    assert result.returncode == 5
    assert env_file.read_bytes() == before_env
    assert state.read_bytes() == before_state


def test_duplicate_env_key_is_rejected(tmp_path: Path) -> None:
    env, env_file, _ = fixture(tmp_path)
    env_file.write_text(env_file.read_text() + "AM_RP_ID=duplicate\n", encoding="utf-8")
    before = env_file.read_bytes()
    result = run(env)
    assert result.returncode == 4
    assert env_file.read_bytes() == before


def test_existing_passkey_rejects_rp_change(tmp_path: Path) -> None:
    env, env_file, state = fixture(tmp_path, credentials=2)
    before_env, before_state = env_file.read_bytes(), state.read_bytes()
    result = run(env)
    assert result.returncode == 6
    assert "登録済み WebAuthn" in result.stderr
    assert env_file.read_bytes() == before_env and state.read_bytes() == before_state


def test_mismatched_client_settings_are_rejected_without_changes(tmp_path: Path) -> None:
    env, env_file, state = fixture(tmp_path)
    support = Path(env["HOME"]) / "Library/Application Support/AudioMinutes"
    support.mkdir(parents=True)
    settings = support / "settings.json"
    settings.write_text(json.dumps({"schema_version": "client-settings/1", "profile": "other", "api_base_url": "http://localhost", "deploy_dir": str(ROOT / "deploy")}), encoding="utf-8")
    before_env, before_settings = env_file.read_bytes(), settings.read_bytes()
    result = run(env)
    assert result.returncode == 7
    assert env_file.read_bytes() == before_env and settings.read_bytes() == before_settings
    assert json.loads(state.read_text(encoding="utf-8")) == {}


def test_health_failure_rolls_back_env_client_api_and_new_serve(tmp_path: Path) -> None:
    env, env_file, state = fixture(tmp_path)
    settings = client_settings(env, extra={"nested": {"untouched": [1, 2, 3]}})
    env["FAIL_CURL"] = "1"
    before = env_file.read_bytes()
    before_settings = settings.read_bytes()
    result = run(env)
    assert result.returncode != 0
    assert env_file.read_bytes() == before
    assert settings.read_bytes() == before_settings
    assert json.loads(state.read_text(encoding="utf-8")) == {}
    calls = Path(env["CALLS"]).read_text(encoding="utf-8")
    assert "serve --yes --https=443 off" in calls
    assert calls.count("--no-deps --force-recreate minutes-api") == 2


def test_api_recreate_exit_42_restores_env_and_client_bytes_and_retries_api(tmp_path: Path) -> None:
    env, env_file, state = fixture(tmp_path)
    settings = client_settings(env, extra={"secretless-user-field": "preserve byte-for-byte"})
    env["FAIL_DOCKER_ONCE"] = "1"
    before_env, before_settings = env_file.read_bytes(), settings.read_bytes()
    result = run(env)
    assert result.returncode == 42
    assert env_file.read_bytes() == before_env
    assert settings.read_bytes() == before_settings
    assert json.loads(state.read_text(encoding="utf-8")) == {}
    calls = Path(env["CALLS"]).read_text(encoding="utf-8")
    assert calls.count("--no-deps --force-recreate minutes-api") == 2


def test_hup_restores_env_and_client_bytes(tmp_path: Path) -> None:
    env, env_file, state = fixture(tmp_path)
    settings = client_settings(env, extra={"keep-format": True})
    wait_file = tmp_path / "curl-waiting"
    env["CURL_NOTIFY_FILE"] = str(wait_file)
    env["FAIL_CURL"] = "1"
    env["AM_TAILSCALE_HEALTH_TIMEOUT"] = "60"
    before_env, before_settings = env_file.read_bytes(), settings.read_bytes()
    process = subprocess.Popen(
        [str(ROOT / "scripts/configure-tailscale.sh"), "--profile", "macos-colima-cpu"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(100):
        if wait_file.exists():
            break
        time.sleep(0.05)
    assert wait_file.exists(), "health待機へ到達しませんでした"
    process.send_signal(signal.SIGHUP)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 129, (stdout, stderr)
    assert env_file.read_bytes() == before_env
    assert settings.read_bytes() == before_settings
    assert json.loads(state.read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize("boundary", ["env", "settings"])
def test_hup_immediately_after_atomic_move_restores_both_files(tmp_path: Path, boundary: str) -> None:
    env, env_file, state = fixture(tmp_path)
    settings = client_settings(env, extra={"format": "must remain byte-identical"})
    before_env, before_settings = env_file.read_bytes(), settings.read_bytes()
    real_mv = shutil.which("mv")
    assert real_mv is not None
    mv_wrapper = Path(env["PATH"].split(":", 1)[0]) / "mv"
    executable(
        mv_wrapper,
        "#!/bin/bash\nset -eu\n"
        f"{real_mv!s} \"$@\"\n"
        "destination=${*: -1}\n"
        "if [[ \"$destination\" == \"$SIGNAL_MOVE_TARGET\" && ! -e \"$SIGNAL_SENT\" ]]; then\n"
        "  touch \"$SIGNAL_SENT\"\n"
        "  kill -HUP \"$PPID\"\n"
        "fi\n",
    )
    env["SIGNAL_MOVE_TARGET"] = str(env_file if boundary == "env" else settings)
    env["SIGNAL_SENT"] = str(tmp_path / "signal-sent")
    result = run(env)
    assert result.returncode == 129, result.stderr
    assert env_file.read_bytes() == before_env
    assert settings.read_bytes() == before_settings
    assert json.loads(state.read_text(encoding="utf-8")) == {}
    assert Path(env["SIGNAL_SENT"]).exists()


def test_serve_apply_failure_rolls_back_env_api_and_partial_serve(tmp_path: Path) -> None:
    env, env_file, state = fixture(tmp_path)
    env["FAIL_SERVE_APPLY"] = "1"
    before = env_file.read_bytes()
    result = run(env)
    assert result.returncode == 9
    assert env_file.read_bytes() == before
    assert json.loads(state.read_text(encoding="utf-8")) == {}
    calls = Path(env["CALLS"]).read_text(encoding="utf-8")
    assert "serve --yes --https=443 off" in calls
    assert calls.count("--no-deps --force-recreate minutes-api") == 2


@pytest.mark.parametrize("script", ["configure-tailscale.sh", "install.sh", "install-remote.sh", "service.sh"])
def test_tailscale_related_shell_parses(script: str) -> None:
    subprocess.run(["/bin/bash", "-n", str(ROOT / "scripts" / script)], check=True)


def test_remote_wrapper_forwards_tailscale_as_fixed_flag(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "ssh-calls"
    executable(
        bin_dir / "ssh",
        "#!/bin/sh\necho \"$*\" >>\"$SSH_CALLS\"\n"
        "case \" $* \" in *' AM_PREPARE=1 '*) exit 0 ;; esac\n"
        "for value do case \"$value\" in *AM_STAGE_ID=*) rest=${value#*AM_STAGE_ID=}; stage=${rest%% *}; "
        "echo \"REMOTE_HOSTNAME_${stage}=fixture-host\" ;; esac; done\n",
    )
    executable(bin_dir / "scp", "#!/bin/sh\nexit 0\n")
    result = subprocess.run(
        [str(ROOT / "scripts/install-remote.sh"), "--tailscale"],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SSH_CALLS": str(calls)},
        input="y\nDEPLOY fixture-host\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "AM_ENABLE_TAILSCALE=1" in calls.read_text(encoding="utf-8")


def test_install_failure_recovery_keeps_tailscale_flag(tmp_path: Path) -> None:
    fixture_root = tmp_path / "repo"
    scripts = fixture_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/install.sh", scripts / "install.sh")
    executable(
        scripts / "common.sh",
        "#!/bin/bash\nrepo_root=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd -P)\"\n"
        "validate_profile(){ :; }\nprofile_env(){ echo \"$repo_root/deploy/profiles/$1/.env\"; }\n"
        "ensure_install_dependencies(){ return 41; }\n",
    )
    required = [
        "deploy/compose.yml",
        "deploy/settings.schema.json",
        "services/minutes-api/uv.lock",
        "services/transcription-worker/uv.lock",
        "services/minutes-worker/uv.lock",
        "scripts/doctor.sh",
        "scripts/service.sh",
        "scripts/readiness.sh",
        "scripts/configure-tailscale.sh",
        "deploy/profiles/macos-colima-cpu/.env.example",
    ]
    for relative in required:
        path = fixture_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    result = subprocess.run(
        [str(scripts / "install.sh"), "--profile", "macos-colima-cpu", "--tailscale"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 41
    assert f"復旧: {scripts / 'install.sh'} --profile macos-colima-cpu --tailscale" in result.stderr


def test_remote_transfer_failure_recovery_keeps_tailscale_flag(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    executable(
        bin_dir / "ssh",
        "#!/bin/sh\ncase \" $* \" in *' AM_PREPARE=1 '*) exit 0 ;; esac\n"
        "for value do case \"$value\" in *AM_STAGE_ID=*) rest=${value#*AM_STAGE_ID=}; stage=${rest%% *}; "
        "echo \"REMOTE_HOSTNAME_${stage}=fixture-host\" ;; esac; done\n",
    )
    executable(bin_dir / "scp", "#!/bin/sh\nexit 23\n")
    result = subprocess.run(
        [str(ROOT / "scripts/install-remote.sh"), "--tailscale"],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        input="y\nDEPLOY fixture-host\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 23
    assert f"scripts/install-remote.sh --host shonoshono@192.168.0.31 --tailscale" in result.stderr
