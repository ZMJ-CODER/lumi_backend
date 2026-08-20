"""DockerSandbox 安全参数与宿主文件隔离契约。"""

import asyncio
from pathlib import Path

from app.agents.sandbox.docker import DockerSandbox


def test_docker_sandbox_rejects_transfer_outside_workspace(tmp_path: Path):
    sandbox = DockerSandbox()
    try:
        sandbox._validated_transfer({"source": str(tmp_path), "target": "/etc", "mode": "ro"})
    except ValueError as exc:
        assert "不安全" in str(exc)
    else:
        raise AssertionError("必须拒绝 workspace 外的容器挂载目标")


def test_docker_sandbox_refuses_execution_when_runtime_unavailable(monkeypatch):
    sandbox = DockerSandbox()

    def unavailable():
        return False, "Docker 未就绪"

    monkeypatch.setattr(sandbox, "is_available", unavailable)
    result = asyncio.run(sandbox.run_script("print(1)"))
    assert result.status == "rejected"
    assert "Docker 未就绪" in (result.error or "")


def test_docker_sandbox_writes_input_through_running_tmpfs(monkeypatch, tmp_path: Path):
    """只读 rootfs 时不应再用 docker cp 写入输入。"""
    sandbox = DockerSandbox()
    source = tmp_path / "input.csv"
    source.write_text("name,score\nA,100\n", encoding="utf-8")
    calls = []

    def available():
        return True, ""

    async def fake_cli(*args, stdin=None, timeout=10):
        calls.append((args, stdin))
        if args[0] == "create":
            return 0, b"sandbox-id\n", b""
        if args[0] == "exec" and args[-3:] == ("python", "-I", "-"):
            return 0, b"done\n", b""
        return 0, b"", b""

    monkeypatch.setattr(sandbox, "is_available", available)
    monkeypatch.setattr(sandbox, "_run_cli", fake_cli)
    result = asyncio.run(
        sandbox.run_script(
            "print('done')",
            mounts=[{"source": str(source), "target": "/workspace/input.csv", "mode": "ro"}],
        )
    )

    assert result.status == "success"
    assert all(args[0] != "cp" for args, _ in calls)
    assert any(args[0] == "start" for args, _ in calls)
    assert any(args[0] == "exec" and b"name,score" in (stdin or b"") for args, stdin in calls)


def test_docker_sandbox_reads_output_without_docker_cp(monkeypatch, tmp_path: Path):
    sandbox = DockerSandbox()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    calls = []

    async def fake_cli(*args, stdin=None, timeout=10):
        calls.append(args)
        if args[0] == "exec" and any("find" in str(arg) for arg in args):
            return 0, b"/workspace/output/nested/scores.txt\0", b""
        if args[0] == "exec" and "cat" in args:
            return 0, b"name\tscore\nA\t100\n", b""
        return 0, b"", b""

    monkeypatch.setattr(sandbox, "_run_cli", fake_cli)
    asyncio.run(sandbox._copy_output_tree("sandbox-id", output_dir, "/workspace/output"))

    assert (output_dir / "nested" / "scores.txt").read_bytes() == b"name\tscore\nA\t100\n"
    assert all(args[0] != "cp" for args in calls)


def test_docker_sandbox_rejects_hostile_output_path(monkeypatch, tmp_path: Path):
    sandbox = DockerSandbox()
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    async def fake_cli(*args, stdin=None, timeout=10):
        return 0, b"/workspace/output/../../escape.txt\0", b""

    monkeypatch.setattr(sandbox, "_run_cli", fake_cli)
    try:
        asyncio.run(sandbox._copy_output_tree("sandbox-id", output_dir, "/workspace/output"))
    except RuntimeError as exc:
        assert "越界" in str(exc)
    else:
        raise AssertionError("必须拒绝越界沙箱产物")
