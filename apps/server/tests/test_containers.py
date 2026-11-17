"""Per-workspace containers: spec, argv, reuse, and the off-by-default gate.

No real container is started here — a fake `docker` on PATH records the argv it
was called with, which is the part ANA is responsible for getting right."""
import json
import os
from pathlib import Path

import pytest

from packages.orchestrator import containers
from packages.orchestrator.containers import (
    ContainerSpec,
    PortMapping,
    containers_enabled,
    ensure_running,
    exec_prefix,
    parse_ports,
    run_args,
)


def _spec(tmp_path, **kwargs) -> ContainerSpec:
    values = {
        "workspace_id": "demo",
        "workspace_path": str(tmp_path / "demo"),
        "auth_mounts": (),
    }
    values.update(kwargs)
    return ContainerSpec(**values)


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A `docker` that logs argv and answers `inspect` from a scripted file."""
    log = tmp_path / "docker.log"
    state = tmp_path / "inspect.json"
    state.write_text("[]", encoding="utf-8")
    script = tmp_path / "bin" / "docker"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        'if [ "$1" = "inspect" ]; then\n'
        f"  cat {state}\n"
        f'  [ "$(cat {state})" = "[]" ] && exit 1\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{script.parent}:{os.environ['PATH']}")
    return {"log": log, "state": state}


def test_ports_are_parsed_and_junk_dropped():
    ports = parse_ports(
        [
            {"host": 5173, "container": 5173},
            {"host": "8080"},  # container defaults to the host port
            {"host": "nonsense"},
            {"host": 99999, "container": 1},
            None,
        ]
    )

    assert [(p.host, p.container) for p in ports] == [(5173, 5173), (8080, 8080)]


def test_run_args_publish_ports_and_mount_the_workspace(tmp_path):
    spec = _spec(tmp_path, ports=(PortMapping(5173, 5173),))

    args = run_args(spec)

    assert args[:3] == ["docker", "run", "-d"]
    assert args[args.index("--name") + 1] == "ana-ws-demo"
    # Same path inside as outside, so every path the gate and the audit log
    # mention means one thing.
    assert f"{spec.workspace_path}:{spec.workspace_path}" in args
    assert args[args.index("-w") + 1] == spec.workspace_path
    assert "5173:5173" in args
    # A place to exec into, not a process to supervise.
    assert args[-2:] == ["sleep", "infinity"]


def test_default_tool_mounts_are_the_host_path_readonly(monkeypatch):
    monkeypatch.setenv("ANA_HOST_FLUTTER_SDK", "/home/hello/development/flutter")
    monkeypatch.setenv("ANA_HOST_ANDROID_SDK", "/home/hello/Android/sdk")
    monkeypatch.setenv("ANA_HOST_JAVA_HOME", "/opt/java")
    monkeypatch.delenv("ANA_HOST_MISSING", raising=False)

    mounts = containers.default_tool_mounts()

    assert "/home/hello/development/flutter:/home/hello/development/flutter" in mounts
    assert "/home/hello/development/flutter:/home/hello/development/flutter:ro" not in mounts
    assert "/home/hello/Android/sdk:/home/hello/Android/sdk:ro" in mounts
    assert "/opt/java:/opt/java:ro" in mounts


def test_default_tool_mounts_skip_unset_paths(monkeypatch):
    monkeypatch.delenv("ANA_HOST_FLUTTER_SDK", raising=False)
    monkeypatch.delenv("ANA_HOST_ANDROID_SDK", raising=False)
    monkeypatch.delenv("ANA_HOST_JAVA_HOME", raising=False)

    assert containers.default_tool_mounts() == ()


def test_default_tool_env_puts_flutter_and_adb_on_path(monkeypatch):
    monkeypatch.setenv("ANA_HOST_FLUTTER_SDK", "/home/hello/development/flutter")
    monkeypatch.setenv("ANA_HOST_ANDROID_SDK", "/home/hello/Android/sdk")
    monkeypatch.setenv("ANA_HOST_JAVA_HOME", "/opt/java")
    monkeypatch.setenv("ANA_ADB_GATEWAY_PORT", "15037")

    env = dict(containers.default_tool_env())

    assert env["FLUTTER_ROOT"] == "/home/hello/development/flutter"
    assert env["ANDROID_HOME"] == "/home/hello/Android/sdk"
    assert env["ANDROID_SDK_ROOT"] == "/home/hello/Android/sdk"
    assert env["JAVA_HOME"] == "/opt/java"
    assert env["ADB_SERVER_SOCKET"] == "tcp:host.docker.internal:15037"
    path = env["PATH"]
    assert path.startswith("/home/hello/development/flutter/bin:")
    assert "/home/hello/Android/sdk/platform-tools" in path


def test_bind_mount_args_keep_readonly_mode():
    assert containers.bind_mount_args("/sdk:/sdk:ro") == ["-v", "/sdk:/sdk:ro"]
    assert containers.bind_mount_args("/home/ana/.claude:/home/ana/.claude") == [
        "-v",
        "/home/ana/.claude:/home/ana/.claude",
    ]
    assert containers.bind_mount_args("not-a-mount") == []


def test_run_args_reach_the_host_loopback_and_host_sdks(tmp_path):
    spec = _spec(
        tmp_path,
        auth_mounts=("/sdk:/sdk:ro",),
        extra_env=(
            ("PATH", "/sdk/platform-tools:/usr/bin"),
            ("ANDROID_HOME", "/sdk"),
            ("ADB_SERVER_SOCKET", "tcp:host.docker.internal:15037"),
        ),
    )

    args = run_args(spec)

    # ssh -R and adb bind the host's 127.0.0.1; host-gateway is the address
    # from inside the compose network that still lands on that host.
    assert args[args.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    assert "/sdk:/sdk:ro" in args
    assert "ANDROID_HOME=/sdk" in args
    assert "ADB_SERVER_SOCKET=tcp:host.docker.internal:15037" in args
    assert "PATH=/sdk/platform-tools:/usr/bin" in args


def test_fingerprint_changes_with_tool_env_or_hosts(tmp_path):
    base = _spec(tmp_path)

    assert base.fingerprint() != _spec(
        tmp_path, extra_env=(("ANDROID_HOME", "/sdk"),)
    ).fingerprint()
    assert base.fingerprint() != _spec(tmp_path, extra_hosts=()).fingerprint()


def test_exec_prefix_passes_env_explicitly(tmp_path):
    spec = _spec(tmp_path)

    prefix = exec_prefix(spec, env={"ANA_GATE_TOKEN": "tok"})

    assert prefix[:3] == ["docker", "exec", "-i"]
    # `docker exec` does not inherit this process's environment, and the gate
    # token lives there.
    assert "ANA_GATE_TOKEN=tok" in prefix
    assert prefix[-1] == "ana-ws-demo"


def test_fingerprint_changes_with_image_or_ports(tmp_path):
    base = _spec(tmp_path)

    assert base.fingerprint() == _spec(tmp_path).fingerprint()
    assert base.fingerprint() != _spec(tmp_path, image="other:latest").fingerprint()
    assert base.fingerprint() != _spec(tmp_path, ports=(PortMapping(1, 1),)).fingerprint()


@pytest.mark.asyncio
async def test_ensure_running_starts_a_missing_container(tmp_path, fake_docker):
    spec = _spec(tmp_path)

    name = await ensure_running(spec)

    assert name == "ana-ws-demo"
    assert "run -d" in fake_docker["log"].read_text()


@pytest.mark.asyncio
async def test_ensure_running_reuses_a_matching_container(tmp_path, fake_docker):
    spec = _spec(tmp_path)
    fake_docker["state"].write_text(
        json.dumps(
            [{"State": {"Running": True}, "Config": {"Labels": {"ana.fingerprint": spec.fingerprint()}}}]
        ),
        encoding="utf-8",
    )

    await ensure_running(spec)

    assert "run -d" not in fake_docker["log"].read_text()


@pytest.mark.asyncio
async def test_ensure_running_replaces_a_container_from_an_older_spec(tmp_path, fake_docker):
    spec = _spec(tmp_path, ports=(PortMapping(5173, 5173),))
    fake_docker["state"].write_text(
        json.dumps([{"State": {"Running": True}, "Config": {"Labels": {"ana.fingerprint": "stale"}}}]),
        encoding="utf-8",
    )

    await ensure_running(spec)

    log = fake_docker["log"].read_text()
    # Its published ports and image are part of what the workspace asked for,
    # and `docker run` cannot change them in place.
    assert "rm -f ana-ws-demo" in log
    assert "run -d" in log


@pytest.mark.asyncio
async def test_ensure_running_starts_a_stopped_container(tmp_path, fake_docker):
    spec = _spec(tmp_path)
    fake_docker["state"].write_text(
        json.dumps(
            [{"State": {"Running": False}, "Config": {"Labels": {"ana.fingerprint": spec.fingerprint()}}}]
        ),
        encoding="utf-8",
    )

    await ensure_running(spec)

    assert "start ana-ws-demo" in fake_docker["log"].read_text()


def test_containers_are_off_without_the_flag(monkeypatch, fake_docker, tmp_path):
    socket = tmp_path / "docker.sock"
    socket.write_text("", encoding="utf-8")
    monkeypatch.setenv("DOCKER_HOST_SOCKET", str(socket))

    monkeypatch.delenv("ANA_WORKSPACE_CONTAINERS", raising=False)
    assert containers_enabled() is False

    monkeypatch.setenv("ANA_WORKSPACE_CONTAINERS", "on")
    assert containers_enabled() is True


def test_containers_are_off_without_the_socket(monkeypatch, fake_docker, tmp_path):
    monkeypatch.setenv("ANA_WORKSPACE_CONTAINERS", "on")
    monkeypatch.setenv("DOCKER_HOST_SOCKET", str(tmp_path / "missing.sock"))

    # The flag alone would only move the failure to the first turn.
    assert containers_enabled() is False


def test_adapters_run_through_the_prefix(tmp_path):
    from packages.adapters.claude_code import ClaudeCodeAdapter
    from packages.adapters.codex import CodexAdapter

    prefix = ["docker", "exec", "-i", "ana-ws-demo"]
    claude_args, _ = ClaudeCodeAdapter(
        workspace_dir=str(tmp_path), env={}, command_prefix=prefix
    )._build_args("hi", None)
    codex_args = CodexAdapter(
        workspace_dir=str(tmp_path), env={}, command_prefix=prefix
    )._build_args("hi", None)

    assert claude_args[:4] == prefix
    assert claude_args[4] == "claude"
    assert codex_args[:4] == prefix
    assert codex_args[4] == "codex"


def test_module_default_image_is_the_server_image():
    # The server image already carries both CLIs, so it is the sane default for
    # a workspace that does not name its own.
    assert containers.DEFAULT_IMAGE == "ana-server:latest"


@pytest.mark.asyncio
async def test_stop_workspace_removes_the_workspaces_container(tmp_path, fake_docker):
    """A deleted workspace must not leave its container running: it is named
    after the workspace, holds that workspace's published ports, and would be
    inherited by whatever next takes the id."""
    fake_docker["state"].write_text(
        json.dumps([{"State": {"Running": True}, "Config": {"Labels": {}}}]), encoding="utf-8"
    )

    assert await containers.stop_workspace("demo") is True
    assert "rm -f ana-ws-demo" in fake_docker["log"].read_text()


@pytest.mark.asyncio
async def test_stop_workspace_is_a_noop_without_a_container(tmp_path, fake_docker):
    assert await containers.stop_workspace("demo") is False
    assert "rm -f" not in fake_docker["log"].read_text()
