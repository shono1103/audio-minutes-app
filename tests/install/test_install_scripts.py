from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _profile_env(path: Path) -> Path:
    path.write_text(
        "AM_DOCKER_CONTEXT=fixture-context\n"
        "AM_COLIMA_PROFILE=audio-minutes-cpu\n"
        "AM_IMAGE_TAG=0.1.0\n"
        "AM_RETENTION_LOG_DAYS=14\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("existing", [True, False], ids=["stopped", "new"])
def test_colima_start_preserves_global_context(tmp_path: Path, existing: bool) -> None:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is required for the shell fixture")
    fixture_bin = tmp_path / "bin"
    log = tmp_path / "calls.log"
    context = tmp_path / "context"
    context.write_text("desktop-linux\n", encoding="utf-8")
    _executable(
        fixture_bin / "docker",
        "#!/bin/sh\n"
        "echo \"docker $*\" >>\"$AM_FIXTURE_LOG\"\n"
        "if [ \"$1 $2\" = \"context show\" ]; then cat \"$AM_FIXTURE_CONTEXT\"; exit 0; fi\n"
        "if [ \"$1 $2\" = \"context use\" ]; then printf '%s\\n' \"$3\" >\"$AM_FIXTURE_CONTEXT\"; exit 0; fi\n"
        "exit 0\n",
    )
    _executable(
        fixture_bin / "colima",
        "#!/bin/sh\n"
        "echo \"colima $*\" >>\"$AM_FIXTURE_LOG\"\n"
        "[ \"$1\" = status ] && exit 1\n"
        "if [ \"$1\" = list ]; then [ \"$AM_FIXTURE_EXISTING\" = 1 ] && echo '{\"name\":\"audio-minutes-cpu\"}'; exit 0; fi\n"
        "if [ \"$1\" = start ]; then\n"
        "  case \" $* \" in *' --activate=false '*) : ;; *) printf 'colima-mutated\\n' >\"$AM_FIXTURE_CONTEXT\" ;; esac\n"
        "fi\n",
    )
    _executable(
        fixture_bin / "sysctl",
        "#!/bin/sh\ncase \"$2\" in hw.logicalcpu) echo 8 ;; hw.memsize) echo 17179869184 ;; esac\n",
    )
    (fixture_bin / "jq").symlink_to(jq)
    env = {
        **os.environ,
        "PATH": f"{fixture_bin}:{os.environ['PATH']}",
        "AM_PROFILE_ENV_OVERRIDE": str(_profile_env(tmp_path / "profile.env")),
        "AM_FIXTURE_LOG": str(log),
        "AM_FIXTURE_CONTEXT": str(context),
        "AM_FIXTURE_EXISTING": "1" if existing else "0",
    }
    result = subprocess.run(
        ["/bin/bash", "-c", f"source '{ROOT / 'scripts/common.sh'}'; ensure_colima_running macos-colima-cpu"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    calls = log.read_text(encoding="utf-8")
    assert "colima start --profile audio-minutes-cpu --activate=false" in calls
    assert context.read_text(encoding="utf-8") == "desktop-linux\n"
    assert "global Docker context を保持しました: desktop-linux" in result.stdout
    if existing:
        assert " --cpu " not in calls
    else:
        assert " --cpu 4 --memory 8 --disk 60" in calls


def test_macos_missing_brew_dependency_is_installed_only_with_yes(tmp_path: Path) -> None:
    fixture_bin = tmp_path / "bin"
    template = _executable(tmp_path / "tool", "#!/bin/sh\nexit 0\n")
    for name in ("docker", "colima", "node", "npm", "swift", "git", "openssl", "curl"):
        (fixture_bin / name).parent.mkdir(parents=True, exist_ok=True)
        (fixture_bin / name).symlink_to(template)
    log = tmp_path / "brew.log"
    _executable(
        fixture_bin / "brew",
        "#!/bin/sh\n"
        "echo \"$*\" >>\"$AM_FIXTURE_LOG\"\n"
        "/bin/cp \"$AM_FIXTURE_TOOL\" \"$AM_FIXTURE_BIN/jq\"\n"
        "/bin/chmod 755 \"$AM_FIXTURE_BIN/jq\"\n",
    )
    env = {
        **os.environ,
        "PATH": str(fixture_bin),
        "AM_HOST_KERNEL_OVERRIDE": "Darwin",
        "AM_FIXTURE_LOG": str(log),
        "AM_FIXTURE_TOOL": str(template),
        "AM_FIXTURE_BIN": str(fixture_bin),
    }
    command = [
        "/bin/bash",
        "-c",
        f"source '{ROOT / 'scripts/common.sh'}'; ensure_install_dependencies macos-colima-cpu 1",
    ]
    denied = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"source '{ROOT / 'scripts/common.sh'}'; ensure_install_dependencies macos-colima-cpu 0",
        ],
        env=env,
        input="n\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert denied.returncode == 2
    assert "PENDING code=consent_required" in denied.stderr
    assert not log.exists()
    subprocess.run(command, env=env, check=True, capture_output=True, text=True)
    assert log.read_text(encoding="utf-8") == "install jq\n"


def test_missing_swift_is_structured_pending_without_os_install(tmp_path: Path) -> None:
    fixture_bin = tmp_path / "bin"
    template = _executable(tmp_path / "tool", "#!/bin/sh\nexit 0\n")
    for name in ("docker", "colima", "jq", "node", "npm", "git", "openssl", "curl"):
        (fixture_bin / name).parent.mkdir(parents=True, exist_ok=True)
        (fixture_bin / name).symlink_to(template)
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"source '{ROOT / 'scripts/common.sh'}'; ensure_install_dependencies macos-colima-cpu 1",
        ],
        env={**os.environ, "PATH": str(fixture_bin), "AM_HOST_KERNEL_OVERRIDE": "Darwin"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "PENDING code=os_action_required component=swift" in result.stderr


def test_linux_missing_dependencies_never_invokes_package_manager(tmp_path: Path) -> None:
    fixture_bin = tmp_path / "bin"
    template = _executable(tmp_path / "tool", "#!/bin/sh\nexit 0\n")
    for name in ("git", "openssl", "curl", "apt", "dnf", "yum"):
        (fixture_bin / name).parent.mkdir(parents=True, exist_ok=True)
        (fixture_bin / name).symlink_to(template)
    env = {**os.environ, "PATH": str(fixture_bin), "AM_HOST_KERNEL_OVERRIDE": "Linux"}
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"source '{ROOT / 'scripts/common.sh'}'; ensure_install_dependencies linux-cpu 1",
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "PENDING code=package_manager_required" in result.stderr


def test_client_install_is_idempotent_and_preserves_user_settings(tmp_path: Path) -> None:
    fixture_bin = tmp_path / "fixture"
    cli = _executable(fixture_bin / "audio-minutes", "#!/bin/sh\necho cli\n")
    _executable(fixture_bin / "AudioMinutesApp", "#!/bin/sh\necho app\n")
    _executable(fixture_bin / "NativeBridge", "#!/bin/sh\necho bridge\n")
    profile_env = tmp_path / "profile.env"
    profile_env.write_text(
        "AM_PUBLIC_BASE_URL=http://localhost:18787\n"
        "AM_DOCKER_CONTEXT=fixture-context\n"
        "AM_COLIMA_PROFILE=fixture-colima\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "AM_SKIP_CLIENT_BUILD": "1",
        "AM_FIXTURE_CLI_BIN": str(cli),
        "AM_FIXTURE_MAC_BIN_DIR": str(fixture_bin),
        "AM_CLIENT_ROOT": str(tmp_path / "support"),
        "AM_USER_APPLICATIONS_DIR": str(tmp_path / "Applications"),
        "AM_USER_BIN_DIR": str(tmp_path / "bin"),
        "AM_CHROME_NATIVE_HOST_DIR": str(tmp_path / "native-hosts"),
        "AM_PROFILE_ENV_OVERRIDE": str(profile_env),
    }
    command = [str(ROOT / "scripts/install-client.sh"), "--profile", "macos-colima-cpu"]
    subprocess.run(command, env=env, check=True, capture_output=True, text=True)
    settings_path = tmp_path / "support/settings.json"
    manifest_path = tmp_path / "native-hosts/dev.audio_minutes.bridge.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert settings["api_base_url"] == "http://localhost:18787"
    assert settings["docker_context"] == "fixture-context"
    assert manifest["allowed_origins"] == ["chrome-extension://cglcpocpendfgbhepidgbpilokapdlnm/"]
    settings_path.write_text('{"user":"unchanged"}\n', encoding="utf-8")
    manifest_path.write_text('{"user":"unchanged"}\n', encoding="utf-8")

    subprocess.run(command, env=env, check=True, capture_output=True, text=True)
    assert settings_path.read_text(encoding="utf-8") == '{"user":"unchanged"}\n'
    assert manifest_path.read_text(encoding="utf-8") == '{"user":"unchanged"}\n'
    assert (tmp_path / "Applications/AudioMinutes.app/Contents/MacOS/NativeBridge").is_file()
    assert (tmp_path / "bin/audio-minutes").is_symlink()


def test_prepare_only_generates_secrets_once(tmp_path: Path) -> None:
    profile_env = tmp_path / "profile.env"
    env = {**os.environ, "AM_PROFILE_ENV_OVERRIDE": str(profile_env)}
    command = [str(ROOT / "install.sh"), "--profile", "macos-colima-cpu", "--prepare-only"]
    subprocess.run(command, env=env, check=True, capture_output=True, text=True)
    first = profile_env.read_bytes()
    subprocess.run(command, env=env, check=True, capture_output=True, text=True)
    assert profile_env.read_bytes() == first
    values = dict(
        line.split("=", 1)
        for line in first.decode().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    for key in ("AM_API_DB_PASSWORD", "AM_WORKER_DB_PASSWORD", "AM_SECRET_KEY", "AM_INTERNAL_TOKEN"):
        assert values[key]


def test_shared_log_probe_only_chmods_directories_owned_by_each_service(tmp_path: Path) -> None:
    """API 所有の probe root を後続 worker が chmod しないことを固定する。"""
    calls_path = tmp_path / "compose-calls"
    command = (
        f"source '{ROOT / 'scripts/common.sh'}'; "
        "compose(){ printf 'CALL\\0' >>\"$AM_FIXTURE_CALLS\"; "
        "printf '%s\\0' \"$@\" >>\"$AM_FIXTURE_CALLS\"; printf 'END\\0' >>\"$AM_FIXTURE_CALLS\"; }; "
        "verify_shared_log_permissions macos-colima-cpu"
    )
    result = subprocess.run(
        ["/bin/bash", "-c", command],
        env={**os.environ, "AM_FIXTURE_CALLS": str(calls_path)},
        check=True,
        capture_output=True,
        text=True,
    )
    tokens = calls_path.read_bytes().split(b"\0")
    calls: list[list[str]] = []
    current: list[str] | None = None
    for raw in tokens:
        if raw == b"CALL":
            current = []
        elif raw == b"END":
            assert current is not None
            calls.append(current)
            current = None
        elif current is not None and raw:
            current.append(raw.decode())

    assert len(calls) == 6  # probe root + 3 service + retention + cleanup
    probe_values = {
        value.split("=", 1)[1]
        for call in calls
        for value in call
        if value.startswith("AM_PERMISSION_PROBE=")
    }
    assert len(probe_values) == 1
    probe = probe_values.pop()
    assert probe.startswith(".permission-probe-")
    assert len(probe.removeprefix(".permission-probe-")) == 24

    initializer = calls[0]
    assert "minutes-api" in initializer
    assert 'mkdir "$probe_root"' in initializer[-1]
    assert 'chmod 2775 "$probe_root"' in initializer[-1]
    assert 'mkdir -p "$probe_root"' not in initializer[-1]

    for service, call in zip(
        ("minutes-api", "transcription-worker", "minutes-worker"), calls[1:4], strict=True
    ):
        assert service in call
        assert f"AM_PERMISSION_SERVICE={service}" in call
        script = call[-1]
        assert 'chmod 2775 "$service_root"' in script
        assert 'chmod 2775 "$nested"' in script
        assert 'chmod 2775 "$probe_root"' not in script
        assert 'chmod 777' not in script and 'chmod 0777' not in script

    retention = calls[4]
    assert retention[retention.index("--entrypoint") + 1] == "python"
    assert f"AM_LOG_DIR=/var/log/audio-minutes/{probe}" in retention
    assert "sweep_log_files" in retention[-1]
    cleanup = calls[5]
    assert f"AM_PERMISSION_PROBE={probe}" in cleanup
    assert 'rm -rf -- "$target"' in cleanup[-1]
    assert "共有 logs volume: 3 UID" in result.stdout


def test_backup_leave_stopped_mode_does_not_restart_write_services(tmp_path: Path) -> None:
    fixture_root = tmp_path / "repo"
    scripts = fixture_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/backup.sh", scripts / "backup.sh")
    (scripts / "common.sh").write_text(
        "repo_root=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd -P)\"\n"
        "validate_profile(){ :; }\n"
        "compose(){\n"
        "  echo \"compose $*\" >>\"$AM_FIXTURE_LOG\"\n"
        "  case \" $* \" in\n"
        "    *' stop '*) [ \"${AM_FIXTURE_STOP_FAIL:-0}\" = 1 ] && return 17; return 0 ;;\n"
        "    *' ps '*) printf '%s' \"${AM_FIXTURE_RUNNING:-}\" ;;\n"
        "    *' pg_dump '*) printf database ;;\n"
        "    *' tar -cz '*) printf archive ;;\n"
        "  esac\n"
        "}\n",
        encoding="utf-8",
    )
    log = tmp_path / "calls.log"
    stopped_backup = tmp_path / "stopped"
    subprocess.run(
        [str(scripts / "backup.sh"), "--profile", "macos-colima-cpu", "--leave-stopped", str(stopped_backup)],
        env={**os.environ, "AM_FIXTURE_LOG": str(log)},
        check=True,
        capture_output=True,
        text=True,
    )
    stopped_calls = log.read_text(encoding="utf-8")
    assert "compose macos-colima-cpu stop minutes-api" in stopped_calls
    assert "compose macos-colima-cpu ps --services --status running --status restarting" in stopped_calls
    assert stopped_calls.index(" stop ") < stopped_calls.index(" ps ") < stopped_calls.index(" pg_dump ")
    assert "compose macos-colima-cpu up -d" not in stopped_calls
    assert (stopped_backup / "manifest.txt").is_file()

    log.unlink()
    resumed_backup = tmp_path / "resumed"
    subprocess.run(
        [str(scripts / "backup.sh"), "--profile", "macos-colima-cpu", str(resumed_backup)],
        env={**os.environ, "AM_FIXTURE_LOG": str(log)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert "compose macos-colima-cpu up -d" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("extra_env", "message"),
    [
        ({"AM_FIXTURE_STOP_FAIL": "1"}, "書き込み側サービスの停止に失敗"),
        ({"AM_FIXTURE_RUNNING": "minutes-worker\n"}, "書き込み側サービスが稼働中"),
    ],
    ids=["stop-error", "still-running"],
)
def test_backup_never_dumps_before_all_write_services_stop(
    tmp_path: Path, extra_env: dict[str, str], message: str
) -> None:
    fixture_root = tmp_path / "repo"
    scripts = fixture_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/backup.sh", scripts / "backup.sh")
    (scripts / "common.sh").write_text(
        "repo_root=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd -P)\"\n"
        "validate_profile(){ :; }\n"
        "compose(){\n"
        "  echo \"compose $*\" >>\"$AM_FIXTURE_LOG\"\n"
        "  case \" $* \" in\n"
        "    *' stop '*) [ \"${AM_FIXTURE_STOP_FAIL:-0}\" = 1 ] && return 17; return 0 ;;\n"
        "    *' ps '*) printf '%s' \"${AM_FIXTURE_RUNNING:-}\" ;;\n"
        "    *' pg_dump '*|*' tar -cz '*) echo dump-started >>\"$AM_FIXTURE_LOG\" ;;\n"
        "  esac\n"
        "}\n",
        encoding="utf-8",
    )
    log = tmp_path / "calls.log"
    destination = tmp_path / "failed"
    result = subprocess.run(
        [str(scripts / "backup.sh"), "--profile", "macos-colima-cpu", str(destination)],
        env={**os.environ, "AM_FIXTURE_LOG": str(log), **extra_env},
        check=False,
        capture_output=True,
        text=True,
    )
    calls = log.read_text(encoding="utf-8")
    assert result.returncode != 0
    assert message in result.stderr
    assert "dump-started" not in calls
    assert " pg_dump " not in calls and " tar -cz " not in calls
    assert not (destination / "manifest.txt").exists()
    assert "compose macos-colima-cpu up -d" in calls, "一部停止済み service は復旧を試みる"


def test_update_rolls_back_images_and_backup_when_readiness_fails(tmp_path: Path) -> None:
    fixture_root = tmp_path / "repo"
    scripts = fixture_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/update.sh", scripts / "update.sh")
    (scripts / "common.sh").write_text(
        "repo_root=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd -P)\"\n"
        "log(){ echo \"$*\" >>\"$AM_FIXTURE_LOG\"; }\n"
        "validate_profile(){ :; }\nensure_install_dependencies(){ log dependencies \"$@\"; }\n"
        "ensure_colima_running(){ log colima \"$@\"; }\n"
        "compose(){ log compose \"$@\"; }\nprofile_value(){ [ \"$2\" = AM_IMAGE_TAG ] && echo 0.1.0; }\n"
        "compose_profile(){ echo cpu; }\n"
        "docker_for_profile(){ log docker \"$@\"; [ \"$2 $3\" = 'image inspect' ] && return 0; return 0; }\n"
        "migrate_shared_volume_permissions(){ log migrate \"$@\"; }\n"
        "verify_shared_log_permissions(){ log log-permissions \"$@\"; }\n"
        "set_update_maintenance(){ log maintenance \"$@\"; }\n"
        "prepare_models(){ log models \"$@\"; }\n"
        "install_client_for_profile(){ log client \"$@\"; }\n",
        encoding="utf-8",
    )
    for path in (
        "deploy/compose.yml",
        "deploy/settings.schema.json",
        "services/minutes-api/uv.lock",
        "services/transcription-worker/uv.lock",
        "services/minutes-worker/uv.lock",
        "scripts/install-client.sh",
    ):
        target = fixture_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    log = tmp_path / "calls.log"
    _executable(
        scripts / "doctor.sh",
        "#!/bin/sh\necho doctor \"$*\" >>\"$AM_FIXTURE_LOG\"\n",
    )
    _executable(
        scripts / "backup.sh",
        "#!/bin/sh\necho backup \"$*\" >>\"$AM_FIXTURE_LOG\"\nfor value do destination=$value; done\n/bin/mkdir -p \"$destination\"\n",
    )
    _executable(
        scripts / "restore.sh",
        "#!/bin/sh\necho restore \"$*\" >>\"$AM_FIXTURE_LOG\"\n",
    )
    _executable(
        scripts / "readiness.sh",
        "#!/bin/sh\necho readiness \"$*\" >>\"$AM_FIXTURE_LOG\"\nexit 9\n",
    )
    backup = tmp_path / "backup"
    result = subprocess.run(
        [str(scripts / "update.sh"), "--profile", "macos-colima-cpu", "--backup-dir", str(backup), "--yes"],
        env={**os.environ, "AM_FIXTURE_LOG": str(log)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 9
    calls = log.read_text(encoding="utf-8")
    assert "models macos-colima-cpu" in calls
    assert f"backup --profile macos-colima-cpu --leave-stopped {backup}" in calls
    assert "log-permissions macos-colima-cpu" in calls
    assert "maintenance macos-colima-cpu enable" in calls
    assert "maintenance macos-colima-cpu disable" in calls
    assert "client macos-colima-cpu 0" in calls
    assert "readiness --profile macos-colima-cpu --timeout 300" in calls
    assert "restore --profile macos-colima-cpu" in calls
    assert calls.count("image tag") >= 6  # 旧 image 退避 3 + rollback 3
    assert "ROLLBACK_READY" in result.stderr
    order = [
        calls.index("backup "),
        calls.index("maintenance macos-colima-cpu enable"),
        calls.index("compose macos-colima-cpu up -d --no-build --remove-orphans"),
        calls.index("readiness --profile macos-colima-cpu --timeout 300"),
        calls.index("compose macos-colima-cpu stop minutes-api"),
        calls.index("maintenance macos-colima-cpu disable"),
        calls.index("restore --profile macos-colima-cpu"),
    ]
    assert order == sorted(order), "readiness 失敗時も maintenance 下で停止してから rollback する"


@pytest.mark.skipif(sys.platform != "linux", reason="setgid/GID 契約は Linux container 内で検証する")
def test_container_log_runner_keeps_daily_archive_for_api_retention(tmp_path: Path) -> None:
    old = tmp_path / "stdout/test/2000-01-01.log"
    old.parent.mkdir(parents=True)
    current_gid = os.getgid()
    for directory in (tmp_path, tmp_path / "stdout", tmp_path / "stdout/test"):
        directory.chmod(0o2775)
    old.write_text("old\n", encoding="utf-8")
    os.utime(old, (1, 1))
    env = {
        **os.environ,
        "AM_LOG_DIR": str(tmp_path),
        "AM_SHARED_GID": str(current_gid),
    }
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "deploy/container-log-runner.py"),
            "--service",
            "test",
            "--",
            "/bin/sh",
            "-c",
            "printf 'stdout-line\\n'; printf 'stderr-line\\n' >&2",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == "stdout-line\nstderr-line\n"
    assert old.exists(), "保持期限の正本は DB なので runner は削除しない"
    archives = list((tmp_path / "stdout/test").glob("*.log"))
    assert len(archives) == 2
    current = next(path for path in archives if path != old)
    assert current.read_text(encoding="utf-8") == result.stdout
    assert current.stat().st_mode & 0o7777 == 0o664
    for directory in (tmp_path, tmp_path / "stdout", tmp_path / "stdout/test"):
        assert directory.stat().st_mode & 0o7777 == 0o2775
        assert directory.stat().st_gid == current_gid


def test_installer_shell_scripts_parse() -> None:
    scripts = sorted((ROOT / "scripts").glob("*.sh")) + [ROOT / "install.sh"]
    subprocess.run(["/bin/bash", "-n", *map(str, scripts)], check=True)


def test_remote_installer_help_documents_fixed_safe_defaults() -> None:
    result = subprocess.run(
        [str(ROOT / "scripts/install-remote.sh"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "shonoshono@192.168.0.31" in result.stdout
    assert "macos-colima-cpu" in result.stdout
    assert "未コミット変更を含む" in result.stdout
    assert "OpenSSH の通常設定" in result.stdout


def test_remote_installer_rejects_ssh_option_injection_before_connecting(tmp_path: Path) -> None:
    fixture_bin = tmp_path / "bin"
    marker = tmp_path / "ssh-called"
    _executable(fixture_bin / "ssh", f"#!/bin/sh\ntouch '{marker}'\nexit 99\n")
    for tool in ("git", "tar", "scp", "shasum", "mktemp"):
        executable = shutil.which(tool)
        assert executable is not None
        (fixture_bin / tool).symlink_to(executable)
    result = subprocess.run(
        [str(ROOT / "scripts/install-remote.sh"), "--host", "-oProxyCommand=bad"],
        env={**os.environ, "PATH": f"{fixture_bin}:/usr/bin:/bin"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "USER@HOST" in result.stderr
    assert not marker.exists()


def test_remote_installer_keeps_host_key_and_normal_auth_policy() -> None:
    script = (ROOT / "scripts/install-remote.sh").read_text(encoding="utf-8")
    assert "StrictHostKeyChecking=no" not in script
    assert "StrictHostKeyChecking=accept-new" not in script
    assert "StrictHostKeyChecking=ask" in script
    assert "UserKnownHostsFile=/dev/null" not in script
    assert "PreferredAuthentications=" not in script
    assert "sshpass" not in script
    assert "set -x" not in script
    assert script.count('ssh "${ssh_host_key_options[@]}" -T -- "$remote_host"') == 2
    assert 'ssh "${ssh_host_key_options[@]}" -tt -- "$remote_host"' in script
    assert "./install.sh --yes --profile macos-colima-cpu" in script
    assert 'install_pipeline_codes=("${PIPESTATUS[@]}")' in script
    assert 'install_pipeline_codes[1]' in script
    assert "rsync" not in script


def test_remote_installer_archive_includes_worktree_changes_but_excludes_sensitive_data(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "repo"
    fixture_scripts = fixture_root / "scripts"
    fixture_scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/install-remote.sh", fixture_scripts / "install-remote.sh")
    (fixture_scripts / "install-remote.sh").chmod(0o755)
    subprocess.run(["git", "init", "-q", str(fixture_root)], check=True)

    included = {
        "src/tracked.txt": "tracked\n",
        "src/untracked.txt": "untracked\n",
        "deploy/profile/.env.example": "example\n",
    }
    excluded = {
        "deploy/profile/.env": "secret\n",
        "credentials/token.txt": "credential\n",
        "recordings/meeting.wav": "audio\n",
        "build/generated.txt": "generated\n",
    }
    for relative, content in {**included, **excluded, "src/deleted.txt": "old\n"}.items():
        path = fixture_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(fixture_root),
            "add",
            "src/tracked.txt",
            "src/deleted.txt",
            "deploy/profile/.env.example",
            "deploy/profile/.env",
            "credentials/token.txt",
            "recordings/meeting.wav",
            "build/generated.txt",
        ],
        check=True,
    )
    (fixture_root / "src/deleted.txt").unlink()

    fixture_bin = tmp_path / "bin"
    captured_archive = tmp_path / "source.tar"
    connection_log = tmp_path / "connections"
    _executable(
        fixture_bin / "ssh",
        "#!/bin/sh\n"
        "echo ssh >>\"$AM_CONNECTION_LOG\"\n"
        "case \" $* \" in\n"
        "  *' AM_PREPARE=1 '*) exit 0 ;;\n"
        "esac\n"
        "for value do\n"
        "  case \"$value\" in *AM_STAGE_ID=*)\n"
        "    rest=${value#*AM_STAGE_ID=}; stage=${rest%% *}; echo \"REMOTE_HOSTNAME_${stage}=fixture-host\" ;;\n"
        "  esac\n"
        "done\n",
    )
    _executable(
        fixture_bin / "scp",
        "#!/bin/sh\n"
        "echo scp >>\"$AM_CONNECTION_LOG\"\n"
        "for value do\n"
        "  case \"$value\" in */source.tar) /bin/cp \"$value\" \"$AM_CAPTURE_ARCHIVE\" ;; esac\n"
        "done\n",
    )
    env = {
        **os.environ,
        "PATH": f"{fixture_bin}:{os.environ['PATH']}",
        "AM_CAPTURE_ARCHIVE": str(captured_archive),
        "AM_CONNECTION_LOG": str(connection_log),
    }
    result = subprocess.run(
        [str(fixture_scripts / "install-remote.sh")],
        env=env,
        input="y\nDEPLOY fixture-host\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    members = set(
        subprocess.run(
            ["tar", "-tf", str(captured_archive)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    assert set(included) <= members
    assert "src/deleted.txt" not in members
    assert set(excluded).isdisjoint(members)
    assert connection_log.read_text(encoding="utf-8").splitlines() == [
        "ssh",
        "ssh",
        "scp",
        "ssh",
    ]


@pytest.mark.parametrize("link_kind", ["target", "ancestor", "broken", "cycle"])
def test_remote_installer_rejects_local_symlink_boundaries(
    tmp_path: Path, link_kind: str
) -> None:
    fixture_root = tmp_path / "repo"
    scripts = fixture_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/install-remote.sh", scripts / "install-remote.sh")
    (scripts / "install-remote.sh").chmod(0o755)
    subprocess.run(["git", "init", "-q", str(fixture_root)], check=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "tracked.txt").write_text("outside\n", encoding="utf-8")
    source = fixture_root / "src"
    source.mkdir()
    tracked = source / "tracked.txt"
    if link_kind in ("target", "broken", "cycle"):
        if link_kind == "target":
            tracked.symlink_to(external / "tracked.txt")
        elif link_kind == "broken":
            tracked.symlink_to(external / "missing.txt")
        else:
            tracked.symlink_to("tracked.txt")
        subprocess.run(["git", "-C", str(fixture_root), "add", "src/tracked.txt"], check=True)
    else:
        tracked.write_text("inside\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(fixture_root), "add", "src/tracked.txt"], check=True)
        shutil.rmtree(source)
        source.symlink_to(external, target_is_directory=True)
    result = subprocess.run(
        [str(scripts / "install-remote.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 10
    assert "symlink" in result.stderr and "拒否" in result.stderr


def test_remote_installer_materializes_allowlisted_repo_links_as_regular_files(
    tmp_path: Path,
) -> None:
    fixture_bin = tmp_path / "bin"
    captured_archive = tmp_path / "source.tar"
    _executable(
        fixture_bin / "ssh",
        "#!/bin/sh\n"
        "case \" $* \" in *' AM_PREPARE=1 '*) exit 0 ;; esac\n"
        "for value do\n"
        "  case \"$value\" in *AM_STAGE_ID=*)\n"
        "    rest=${value#*AM_STAGE_ID=}; stage=${rest%% *}; echo \"REMOTE_HOSTNAME_${stage}=fixture-host\" ;;\n"
        "  esac\n"
        "done\n",
    )
    _executable(
        fixture_bin / "scp",
        "#!/bin/sh\n"
        "for value do\n"
        "  case \"$value\" in */source.tar) /bin/cp \"$value\" \"$AM_CAPTURE_ARCHIVE\" ;; esac\n"
        "done\n",
    )
    result = subprocess.run(
        [str(ROOT / "scripts/install-remote.sh")],
        env={
            **os.environ,
            "PATH": f"{fixture_bin}:{os.environ['PATH']}",
            "AM_CAPTURE_ARCHIVE": str(captured_archive),
        },
        input="y\nDEPLOY fixture-host\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    inspect_member = "scripts/inspect-host.sh"
    schema_member = "packages/python/audio_minutes_contracts/schemas/job.v1.schema.json"
    with tarfile.open(captured_archive) as bundle:
        members = {member.name: member for member in bundle.getmembers()}
        assert all(member.isfile() for member in members.values())
        assert members[inspect_member].isfile()
        assert members[schema_member].isfile()
        inspect_file = bundle.extractfile(inspect_member)
        schema_file = bundle.extractfile(schema_member)
        assert inspect_file is not None and schema_file is not None
        assert inspect_file.read() == (ROOT / inspect_member).read_bytes()
        assert schema_file.read() == (ROOT / "contracts/schemas/job.v1.schema.json").read_bytes()


def test_remote_installer_skips_unstaged_deleted_schema_behind_allowed_link(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "repo"
    scripts = fixture_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/install-remote.sh", scripts / "install-remote.sh")
    (scripts / "install-remote.sh").chmod(0o755)
    schema_source = fixture_root / "contracts/schemas"
    schema_source.mkdir(parents=True)
    kept = schema_source / "kept.json"
    deleted = schema_source / "deleted.json"
    kept.write_text('{"kept":true}\n', encoding="utf-8")
    deleted.write_text('{"deleted":true}\n', encoding="utf-8")
    schema_link = fixture_root / "packages/python/audio_minutes_contracts/schemas"
    schema_link.parent.mkdir(parents=True)
    schema_link.symlink_to("../../../contracts/schemas", target_is_directory=True)
    subprocess.run(["git", "init", "-q", str(fixture_root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(fixture_root),
            "add",
            "contracts/schemas/kept.json",
            "contracts/schemas/deleted.json",
            "packages/python/audio_minutes_contracts/schemas",
        ],
        check=True,
    )
    deleted.unlink()

    fixture_bin = tmp_path / "bin"
    captured_archive = tmp_path / "source.tar"
    _executable(
        fixture_bin / "ssh",
        "#!/bin/sh\n"
        "case \" $* \" in *' AM_PREPARE=1 '*) exit 0 ;; esac\n"
        "for value do\n"
        "  case \"$value\" in *AM_STAGE_ID=*)\n"
        "    rest=${value#*AM_STAGE_ID=}; stage=${rest%% *}; echo \"REMOTE_HOSTNAME_${stage}=fixture-host\" ;;\n"
        "  esac\n"
        "done\n",
    )
    _executable(
        fixture_bin / "scp",
        "#!/bin/sh\n"
        "for value do\n"
        "  case \"$value\" in */source.tar) /bin/cp \"$value\" \"$AM_CAPTURE_ARCHIVE\" ;; esac\n"
        "done\n",
    )
    result = subprocess.run(
        [str(scripts / "install-remote.sh")],
        env={
            **os.environ,
            "PATH": f"{fixture_bin}:{os.environ['PATH']}",
            "AM_CAPTURE_ARCHIVE": str(captured_archive),
        },
        input="y\nDEPLOY fixture-host\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    link_prefix = "packages/python/audio_minutes_contracts/schemas"
    with tarfile.open(captured_archive) as bundle:
        members = {member.name for member in bundle.getmembers()}
    assert f"{link_prefix}/kept.json" in members
    assert f"{link_prefix}/deleted.json" not in members


def _embedded_remote_script(marker: str) -> str:
    script_text = (ROOT / "scripts/install-remote.sh").read_text(encoding="utf-8")
    return script_text.split(f"<<'{marker}' || true\n", 1)[1].split(f"\n{marker}", 1)[0]


def test_remote_preflight_rejects_mismatched_client_settings_without_secret_output(
    tmp_path: Path,
) -> None:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is required for ClientSettings conflict detection")
    home = tmp_path / "home"
    settings = home / "Library/Application Support/AudioMinutes/settings.json"
    settings.parent.mkdir(parents=True)
    secret = "must-not-be-printed"
    settings.write_text(
        json.dumps({"deploy_dir": "/different/audio-minutes/deploy", "token": secret}),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["/bin/bash", "-c", _embedded_remote_script("REMOTE_PREFLIGHT")],
        env={**os.environ, "HOME": str(home), "AM_STAGE_ID": "fixture-stage"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 26
    assert "deploy_dir が固定配置先と一致しません" in result.stderr
    assert secret not in result.stdout + result.stderr


def test_remote_preflight_rejects_existing_compose_project_for_cold_install(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fixture_bin = tmp_path / "bin"
    _executable(
        fixture_bin / "docker",
        "#!/bin/sh\n"
        "case \"$1 $2\" in\n"
        "  'context inspect') exit 0 ;;\n"
        "esac\n"
        "case \" $* \" in *' ps -a '*) echo existing-container ;; esac\n",
    )
    result = subprocess.run(
        ["/bin/bash", "-s"],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fixture_bin}:/usr/bin:/bin",
            "AM_STAGE_ID": "fixture-stage",
        },
        input=_embedded_remote_script("REMOTE_PREFLIGHT"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 27
    assert "同名の Compose service" in result.stderr
    assert "既存配置を確認して移行" in result.stderr


def test_remote_preflight_allows_same_compose_objects_for_managed_update(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / ".local/share/audio-minutes/repository"
    target.mkdir(parents=True)
    (target / ".audio-minutes-remote-managed").write_text(
        "audio-minutes-remote-managed-v1\n", encoding="utf-8"
    )
    fixture_bin = tmp_path / "bin"
    _executable(
        fixture_bin / "docker",
        "#!/bin/sh\n"
        "case \"$1 $2\" in 'context inspect') exit 0 ;; esac\n"
        "case \" $* \" in *' ps -a '*) echo existing-container ;; esac\n",
    )
    _executable(
        fixture_bin / "uname",
        "#!/bin/sh\ncase \"$1\" in -s) echo Darwin ;; -m) echo arm64 ;; esac\n",
    )
    _executable(fixture_bin / "hostname", "#!/bin/sh\necho fixture-host\n")
    _executable(fixture_bin / "sysctl", "#!/bin/sh\necho fixture-value\n")
    _executable(fixture_bin / "sw_vers", "#!/bin/sh\necho 14.2\n")
    result = subprocess.run(
        ["/bin/bash", "-s"],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fixture_bin}:/usr/bin:/bin",
            "AM_STAGE_ID": "fixture-stage",
        },
        input=_embedded_remote_script("REMOTE_PREFLIGHT"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "REMOTE_HOSTNAME_fixture-stage=fixture-host" in result.stdout
    assert not (home / ".local/share/audio-minutes/staging").exists(), "検査接続はread-only"


def test_remote_preflight_prepare_branch_runs_from_stdin_and_hands_off_lock(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = home / ".local/share/audio-minutes/repository"
    target.mkdir(parents=True)
    (target / ".audio-minutes-remote-managed").write_text(
        "audio-minutes-remote-managed-v1\n", encoding="utf-8"
    )
    fixture_bin = tmp_path / "bin"
    _executable(
        fixture_bin / "uname",
        "#!/bin/sh\ncase \"$1\" in -s) echo Darwin ;; -m) echo arm64 ;; esac\n",
    )
    _executable(fixture_bin / "hostname", "#!/bin/sh\necho fixture-host\n")
    _executable(fixture_bin / "sysctl", "#!/bin/sh\necho fixture-value\n")
    _executable(fixture_bin / "sw_vers", "#!/bin/sh\necho 14.2\n")
    result = subprocess.run(
        ["/bin/bash", "-s"],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fixture_bin}:/usr/bin:/bin",
            "AM_STAGE_ID": "fixture-stage",
            "AM_PREPARE": "1",
            "AM_EXPECTED_HOSTNAME": "fixture-host",
        },
        input=_embedded_remote_script("REMOTE_PREFLIGHT"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    lock = home / ".local/share/audio-minutes/install-remote.lock"
    assert (lock / "owner").read_text(encoding="utf-8") == "fixture-stage\n"
    assert (home / ".local/share/audio-minutes/staging/fixture-stage").is_dir()


def test_remote_wrapper_runs_stdin_preflight_without_pty_echo_and_reaches_prepare(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = home / ".local/share/audio-minutes/repository"
    target.mkdir(parents=True)
    (target / ".audio-minutes-remote-managed").write_text(
        "audio-minutes-remote-managed-v1\n", encoding="utf-8"
    )
    fixture_bin = tmp_path / "bin"
    ssh_calls = tmp_path / "ssh-calls"
    _executable(
        fixture_bin / "ssh",
        "#!/bin/sh\n"
        "mode= last=\n"
        "for value do\n"
        "  case \"$value\" in -T|-tt) mode=$value ;; esac\n"
        "  last=$value\n"
        "done\n"
        "echo \"$mode\" >>\"$AM_SSH_CALLS\"\n"
        "[ \"$mode\" = -T ] && exec /bin/sh -c \"$last\"\n"
        "exit 0\n",
    )
    _executable(fixture_bin / "scp", "#!/bin/sh\nexit 0\n")
    _executable(
        fixture_bin / "uname",
        "#!/bin/sh\ncase \"$1\" in -s) echo Darwin ;; -m) echo arm64 ;; esac\n",
    )
    _executable(fixture_bin / "hostname", "#!/bin/sh\necho fixture-host\n")
    _executable(fixture_bin / "sysctl", "#!/bin/sh\necho fixture-value\n")
    _executable(fixture_bin / "sw_vers", "#!/bin/sh\necho 14.2\n")
    result = subprocess.run(
        [str(ROOT / "scripts/install-remote.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fixture_bin}:{os.environ['PATH']}",
            "AM_SSH_CALLS": str(ssh_calls),
        },
        input="y\nDEPLOY fixture-host\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "REMOTE_HOSTNAME_" in result.stdout and "=fixture-host" in result.stdout
    assert "preflight_cleanup()" not in result.stdout
    assert "set -euo pipefail" not in result.stdout
    assert ssh_calls.read_text(encoding="utf-8").splitlines() == ["-T", "-T", "-tt"]
    lock = home / ".local/share/audio-minutes/install-remote.lock"
    owner = (lock / "owner").read_text(encoding="utf-8").strip()
    assert owner
    assert (home / f".local/share/audio-minutes/staging/{owner}").is_dir()


def test_remote_preflight_fails_closed_when_colima_inspection_fails(tmp_path: Path) -> None:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is required for Colima fixture")
    home = tmp_path / "home"
    home.mkdir()
    fixture_bin = tmp_path / "bin"
    _executable(fixture_bin / "colima", "#!/bin/sh\nexit 9\n")
    (fixture_bin / "jq").symlink_to(jq)
    result = subprocess.run(
        ["/bin/bash", "-s"],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fixture_bin}:/usr/bin:/bin",
            "AM_STAGE_ID": "fixture-stage",
        },
        input=_embedded_remote_script("REMOTE_PREFLIGHT"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 28
    assert "衝突不明のまま cold install を続行しません" in result.stderr


def test_remote_preflight_rejects_concurrent_owner_lock(tmp_path: Path) -> None:
    home = tmp_path / "home"
    lock = home / ".local/share/audio-minutes/install-remote.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text("other-stage\n", encoding="utf-8")
    result = subprocess.run(
        ["/bin/bash", "-c", _embedded_remote_script("REMOTE_PREFLIGHT")],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "AM_STAGE_ID": "fixture-stage",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 25
    assert "owner=other-stage" in result.stderr
    assert lock.is_dir()


def test_remote_preflight_rejects_managed_staging_symlink_before_mkdir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    base = home / ".local/share/audio-minutes"
    base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "staging").symlink_to(outside, target_is_directory=True)
    result = subprocess.run(
        ["/bin/bash", "-c", _embedded_remote_script("REMOTE_PREFLIGHT")],
        env={**os.environ, "HOME": str(home), "AM_STAGE_ID": "fixture-stage"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 21
    assert "管理パスが symlink" in result.stderr
    assert list(outside.iterdir()) == []


def _remote_deploy_fixture(tmp_path: Path, install_exit: int = 0) -> dict[str, object]:
    home = tmp_path / "home"
    base = home / ".local/share/audio-minutes"
    target = base / "repository"
    stage_id = "fixture-stage"
    stage = base / f"staging/{stage_id}"
    lock = base / "install-remote.lock"
    source = tmp_path / "source"
    install_calls = tmp_path / "install-calls"
    required = (
        "scripts/install.sh",
        "scripts/doctor.sh",
        "scripts/readiness.sh",
        "scripts/service.sh",
        "scripts/configure-tailscale.sh",
        "deploy/compose.yml",
        "deploy/profiles/macos-colima-cpu/.env.example",
        "services/minutes-api/uv.lock",
        "services/transcription-worker/uv.lock",
        "services/minutes-worker/uv.lock",
    )
    for relative in required:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture: {relative}\n", encoding="utf-8")
        if relative.startswith("scripts/"):
            path.chmod(0o755)
    _executable(
        source / "install.sh",
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >\"$AM_FIXTURE_INSTALL_CALLS\"\n"
        f"exit {install_exit}\n",
    )
    (source / "src/current.txt").parent.mkdir(parents=True)
    (source / "src/current.txt").write_text("current\n", encoding="utf-8")
    python_source = source / "services/minutes-api/src/minutes_api/__init__.py"
    python_source.parent.mkdir(parents=True, exist_ok=True)
    python_source.write_text("VALUE = 1\n", encoding="utf-8")
    target_env = target / "deploy/profiles/macos-colima-cpu/.env"
    target_env.parent.mkdir(parents=True, exist_ok=True)
    target_env.write_bytes(b"AM_SECRET_KEY=preserved\n")
    (target / ".audio-minutes-remote-managed").write_text(
        "audio-minutes-remote-managed-v1\n", encoding="utf-8"
    )
    stage.mkdir(parents=True)
    base.chmod(0o700)
    stage.parent.chmod(0o700)
    stage.chmod(0o700)
    lock.mkdir()
    (lock / "owner").write_text(f"{stage_id}\n", encoding="utf-8")
    archive = stage / "source.tar"
    members = [
        "install.sh",
        *required,
        "src/current.txt",
        "services/minutes-api/src/minutes_api/__init__.py",
    ]
    subprocess.run(["tar", "-C", str(source), "-cf", str(archive), *members], check=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    env = {
        **os.environ,
        "HOME": str(home),
        "AM_STAGE_ID": stage_id,
        "AM_ARCHIVE_SHA256": digest,
        "AM_FIXTURE_INSTALL_CALLS": str(install_calls),
    }
    return {
        "script": _embedded_remote_script("REMOTE_DEPLOY"),
        "home": home,
        "base": base,
        "target": target,
        "stage": stage,
        "lock": lock,
        "source": source,
        "archive": archive,
        "env": env,
    }


def test_remote_deploy_rerun_replaces_tree_and_preserves_existing_env_bytes(tmp_path: Path) -> None:
    script_text = (ROOT / "scripts/install-remote.sh").read_text(encoding="utf-8")
    remote_script = script_text.split("<<'REMOTE_DEPLOY' || true\n", 1)[1].split(
        "\nREMOTE_DEPLOY", 1
    )[0]
    home = tmp_path / "home"
    base = home / ".local/share/audio-minutes"
    target = base / "repository"
    stage_id = "fixture-stage"
    stage = base / f"staging/{stage_id}"
    lock = base / "install-remote.lock"
    source = tmp_path / "source"
    install_calls = tmp_path / "install-calls"

    required = (
        "scripts/install.sh",
        "scripts/doctor.sh",
        "scripts/readiness.sh",
        "scripts/service.sh",
        "scripts/configure-tailscale.sh",
        "deploy/compose.yml",
        "deploy/profiles/macos-colima-cpu/.env.example",
        "services/minutes-api/uv.lock",
        "services/transcription-worker/uv.lock",
        "services/minutes-worker/uv.lock",
    )
    for relative in required:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture: {relative}\n", encoding="utf-8")
        if relative.startswith("scripts/"):
            path.chmod(0o755)
    _executable(
        source / "install.sh",
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >\"$AM_FIXTURE_INSTALL_CALLS\"\n",
    )
    (source / "src/current.txt").parent.mkdir(parents=True)
    (source / "src/current.txt").write_text("current\n", encoding="utf-8")
    python_source = source / "services/minutes-api/src/minutes_api/__init__.py"
    python_source.parent.mkdir(parents=True, exist_ok=True)
    python_source.write_text("VALUE = 1\n", encoding="utf-8")

    target_env = target / "deploy/profiles/macos-colima-cpu/.env"
    target_env.parent.mkdir(parents=True, exist_ok=True)
    original_env = b"AM_SECRET_KEY=byte-preserved-\xff\n"
    target_env.write_bytes(original_env)
    (target / ".audio-minutes-remote-managed").write_text(
        "audio-minutes-remote-managed-v1\n", encoding="utf-8"
    )
    (target / "src/removed.txt").parent.mkdir(parents=True)
    (target / "src/removed.txt").write_text("obsolete\n", encoding="utf-8")

    stage.mkdir(parents=True)
    base.chmod(0o700)
    stage.parent.chmod(0o700)
    stage.chmod(0o700)
    lock.mkdir()
    (lock / "owner").write_text(f"{stage_id}\n", encoding="utf-8")
    archive = stage / "source.tar"
    archive_members = [
        "install.sh",
        *required,
        "src/current.txt",
        "services/minutes-api/src/minutes_api/__init__.py",
    ]
    subprocess.run(
        ["tar", "-C", str(source), "-cf", str(archive), *archive_members], check=True
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    result = subprocess.run(
        ["/bin/bash", "-c", remote_script],
        env={
            **os.environ,
            "HOME": str(home),
            "AM_STAGE_ID": stage_id,
            "AM_ARCHIVE_SHA256": digest,
            "AM_FIXTURE_INSTALL_CALLS": str(install_calls),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert target_env.read_bytes() == original_env
    assert target_env.stat().st_mode & 0o777 == 0o600
    assert (target / ".audio-minutes-remote-managed").stat().st_mode & 0o777 == 0o600
    assert target.stat().st_mode & 0o777 == 0o755
    assert (target / "services/minutes-api/src/minutes_api").stat().st_mode & 0o777 == 0o755
    assert (target / "services/minutes-api/src/minutes_api/__init__.py").stat().st_mode & 0o777 == 0o644
    assert (target / "deploy/compose.yml").stat().st_mode & 0o777 == 0o644
    assert (target / "install.sh").stat().st_mode & 0o777 == 0o755
    assert (target / "scripts/doctor.sh").stat().st_mode & 0o777 == 0o755
    assert base.stat().st_mode & 0o777 == 0o700
    assert (base / "staging").stat().st_mode & 0o777 == 0o700
    assert (base / "logs").stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o002 == 0 for path in target.rglob("*"))
    assert (target / "src/current.txt").is_file()
    assert not (target / "src/removed.txt").exists()
    assert install_calls.read_text(encoding="utf-8") == "--yes --profile macos-colima-cpu\n"
    assert not stage.exists()
    assert not lock.exists()


@pytest.mark.parametrize(
    ("expected_digest", "install_exit", "expected_code", "expected_stage"),
    [
        ("mismatch", 0, 33, "checksum"),
        (None, 17, 17, "install"),
    ],
    ids=["checksum-mismatch", "installer-failure"],
)
def test_remote_deploy_reports_exact_failure_and_releases_lock(
    tmp_path: Path,
    expected_digest: str | None,
    install_exit: int,
    expected_code: int,
    expected_stage: str,
) -> None:
    fixture = _remote_deploy_fixture(tmp_path, install_exit=install_exit)
    env = dict(fixture["env"])
    if expected_digest is not None:
        env["AM_ARCHIVE_SHA256"] = expected_digest
    result = subprocess.run(
        ["/bin/bash", "-c", str(fixture["script"])],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected_code
    assert f"stage={expected_stage} exit={expected_code}" in result.stderr
    assert not Path(fixture["lock"]).exists()
    assert Path(fixture["stage"]).exists(), "失敗時の調査・再開用 stage は保持する"


def test_remote_deploy_reports_tee_failure(tmp_path: Path) -> None:
    fixture = _remote_deploy_fixture(tmp_path)
    fixture_bin = tmp_path / "bin"
    _executable(fixture_bin / "tee", "#!/bin/sh\nexit 19\n")
    env = dict(fixture["env"])
    env["PATH"] = f"{fixture_bin}:{env['PATH']}"
    result = subprocess.run(
        ["/bin/bash", "-c", str(fixture["script"])],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 19
    assert "stage=log exit=19" in result.stderr
    assert not Path(fixture["lock"]).exists()


def test_remote_deploy_installer_failure_recovery_keeps_tailscale_flag(tmp_path: Path) -> None:
    fixture = _remote_deploy_fixture(tmp_path, install_exit=17)
    env = dict(fixture["env"])
    env["AM_ENABLE_TAILSCALE"] = "1"
    result = subprocess.run(
        ["/bin/bash", "-c", str(fixture["script"])],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 17
    assert "./install.sh --yes --profile macos-colima-cpu --tailscale" in result.stderr


def test_remote_deploy_rejects_archive_path_traversal_before_extract(tmp_path: Path) -> None:
    fixture = _remote_deploy_fixture(tmp_path)
    archive = Path(fixture["archive"])
    with tarfile.open(archive, "w") as bundle:
        member = tarfile.TarInfo("../outside.txt")
        payload = b"outside\n"
        member.size = len(payload)
        bundle.addfile(member, io.BytesIO(payload))
    env = dict(fixture["env"])
    env["AM_ARCHIVE_SHA256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    result = subprocess.run(
        ["/bin/bash", "-c", str(fixture["script"])],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 39
    assert not (tmp_path / "outside.txt").exists()
    assert "安全でない archive member" in result.stderr


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_remote_deploy_rejects_archive_links_before_activation(
    tmp_path: Path, link_kind: str
) -> None:
    fixture = _remote_deploy_fixture(tmp_path)
    archive = Path(fixture["archive"])
    with tarfile.open(archive, "a") as bundle:
        linked = tarfile.TarInfo("linked")
        linked.type = tarfile.SYMTYPE if link_kind == "symlink" else tarfile.LNKTYPE
        linked.linkname = "src/current.txt"
        bundle.addfile(linked)
    env = dict(fixture["env"])
    env["AM_ARCHIVE_SHA256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    result = subprocess.run(
        ["/bin/bash", "-c", str(fixture["script"])],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (39, 40)
    assert not (Path(fixture["target"]) / "linked").exists()


def test_remote_deploy_rejects_existing_env_symlink(tmp_path: Path) -> None:
    fixture = _remote_deploy_fixture(tmp_path)
    target_env = Path(fixture["target"]) / "deploy/profiles/macos-colima-cpu/.env"
    target_env.unlink()
    target_env.symlink_to(tmp_path / "outside.env")
    result = subprocess.run(
        ["/bin/bash", "-c", str(fixture["script"])],
        env=dict(fixture["env"]),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 37
    assert "既存 .env" in result.stderr
    assert Path(fixture["target"]).is_dir()


def test_remote_deploy_rejects_symlink_ancestor_of_preserved_profile_env(
    tmp_path: Path,
) -> None:
    fixture = _remote_deploy_fixture(tmp_path)
    target = Path(fixture["target"])
    profiles = target / "deploy/profiles"
    shutil.rmtree(profiles)
    outside = tmp_path / "outside-profiles"
    remote_profile = outside / "macos-colima-cpu"
    remote_profile.mkdir(parents=True)
    (remote_profile / ".env").write_text("SECRET=outside\n", encoding="utf-8")
    profiles.symlink_to(outside, target_is_directory=True)
    result = subprocess.run(
        ["/bin/bash", "-c", str(fixture["script"])],
        env=dict(fixture["env"]),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 37
    assert "祖先が symlink" in result.stderr
    assert profiles.is_symlink()


@pytest.mark.parametrize(
    "profile",
    ["macos-colima-cpu", "macos-colima-krunkit-vulkan", "linux-cpu"],
)
def test_compose_log_policy_is_consistent(tmp_path: Path, profile: str) -> None:
    docker = shutil.which("docker")
    if docker is None or subprocess.run(
        [docker, "compose", "version"], check=False, capture_output=True
    ).returncode != 0:
        pytest.skip("docker compose is not installed")
    profile_env = tmp_path / ".env"
    source = (ROOT / f"deploy/profiles/{profile}/.env.example").read_text(encoding="utf-8")
    source = source.replace("AM_API_DB_PASSWORD=", "AM_API_DB_PASSWORD=fixture-api", 1)
    source = source.replace("AM_WORKER_DB_PASSWORD=", "AM_WORKER_DB_PASSWORD=fixture-worker", 1)
    source = source.replace("AM_SECRET_KEY=", "AM_SECRET_KEY=fixture-secret", 1)
    source = source.replace("AM_INTERNAL_TOKEN=", "AM_INTERNAL_TOKEN=fixture-token", 1)
    profile_env.write_text(source, encoding="utf-8")
    command = (
        f"source '{ROOT / 'scripts/common.sh'}'; "
        "AM_PROFILE_ENV_OVERRIDE=\"$1\"; export AM_PROFILE_ENV_OVERRIDE; "
        f"validate_log_policy {profile}"
    )
    subprocess.run(["/bin/bash", "-c", command, "fixture", str(profile_env)], check=True)
