"""Protect production deployment gates and recovery without using Docker or GitHub."""
import copy
from email.message import Message
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest


pytest.importorskip("fcntl", reason="Deployment tooling requires POSIX file locking")


SPEC = importlib.util.spec_from_file_location(
    "gp11_deploy", Path(__file__).resolve().parents[1] / "scripts" / "deploy.py"
)
deploy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deploy
SPEC.loader.exec_module(deploy)

SHA = "a" * 40
OLD_SHA = "b" * 40
NEW_SHA = "c" * 40


def candidate_compose(manager):
    return {
        "services": {
            "web": {
                "environment": {"DATABASE": "/data/election_system.db"},
                "volumes": [{"type": "volume", "source": "election-data", "target": "/data"}],
                "networks": {"default": {}},
                "healthcheck": {"test": ["CMD", "python", "-c", "pass"]},
            }
        },
        "volumes": {"election-data": {"external": True, "name": manager.config.volume}},
        "networks": {"default": {"name": manager.config.project + "_default"}},
    }


def existing_web(manager):
    return {
        "Id": "old-web",
        "Image": "sha256:old-image",
        "State": {"Running": True},
        "Mounts": [{"Type": "volume", "Name": manager.config.volume, "Destination": "/data"}],
        "Config": {"Labels": {}},
        "NetworkSettings": {"Networks": {manager.config.project + "_default": {}}},
    }


@pytest.fixture
def manager(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=" + "s" * 48 + "\n", encoding="utf-8")
    config = deploy.Config(
        repository="EthanMok4268/GP11_EMV_System",
        branch="master",
        workflow="ci.yml",
        project="test-gp11",
        volume="test-gp11_election-data",
        env_file=env_file,
        state_dir=tmp_path / "state",
        token_file=None,
    )
    return deploy.Deployer(config)


def workflow_run(**changes):
    run = {
        "id": 200,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "master",
        "head_sha": SHA,
        "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "EthanMok4268/GP11_EMV_System"},
        "head_repository": {"full_name": "EthanMok4268/GP11_EMV_System"},
    }
    run.update(changes)
    return run


def set_runs(monkeypatch, manager, *runs):
    monkeypatch.setattr(manager, "api_runs", lambda sha: {"workflow_runs": list(runs)})


def test_ci_accepts_only_the_tested_master_commit(manager, monkeypatch):
    set_runs(monkeypatch, manager, workflow_run())
    assert manager.ci_passed(SHA) is True


def test_ci_accepts_github_workflow_path_with_ref_suffix(manager, monkeypatch):
    set_runs(monkeypatch, manager, workflow_run(path=".github/workflows/ci.yml@refs/heads/master"))
    assert manager.ci_passed(SHA) is True


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "queued", "conclusion": None},
        {"status": "in_progress", "conclusion": None},
        {"status": "completed", "conclusion": "failure"},
        {"status": "completed", "conclusion": "cancelled"},
        {"status": "completed", "conclusion": "skipped"},
        {"head_sha": OLD_SHA},
        {"head_branch": "feature"},
        {"event": "pull_request"},
        {"path": ".github/workflows/unrelated.yml"},
        {"repository": {"full_name": "someone/another-repository"}},
    ],
)
def test_ci_rejects_untrusted_or_unsuccessful_runs(manager, monkeypatch, changes):
    set_runs(monkeypatch, manager, workflow_run(**changes))
    assert manager.ci_passed(SHA) is False


def test_missing_ci_run_does_not_allow_deployment(manager, monkeypatch):
    set_runs(monkeypatch, manager)
    assert manager.ci_passed(SHA) is False


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("completed", "failure")])
def test_newer_ci_run_blocks_an_older_success(manager, monkeypatch, status, conclusion):
    set_runs(
        monkeypatch,
        manager,
        workflow_run(id=199),
        workflow_run(id=200, status=status, conclusion=conclusion),
    )
    assert manager.ci_passed(SHA) is False


def test_newest_attempt_must_succeed_even_when_an_older_attempt_passed(manager, monkeypatch):
    set_runs(
        monkeypatch,
        manager,
        workflow_run(run_attempt=1),
        workflow_run(run_attempt=2, status="in_progress", conclusion=None),
    )
    assert manager.ci_passed(SHA) is False


def test_workflow_query_is_bound_to_the_commit_and_does_not_hide_failed_reruns(manager, monkeypatch):
    requested = []

    def request_json(url):
        requested.append(url)
        return {"workflow_runs": []}

    monkeypatch.setattr(manager, "request_json", request_json)
    assert manager.api_runs(SHA) == {"workflow_runs": []}
    assert len(requested) == 1
    url = urlparse(requested[0])
    assert url.scheme == "https" and url.netloc == "api.github.com"
    assert url.path == "/repos/EthanMok4268/GP11_EMV_System/actions/workflows/ci.yml/runs"
    query = parse_qs(url.query)
    assert query["head_sha"] == [SHA]
    assert query["branch"] == ["master"]
    assert query["event"] == ["push"]
    assert "status" not in query


def persist(manager, **state):
    manager.state.update(state)
    manager.save()


@pytest.fixture
def operation(manager, monkeypatch):
    """Fake external work while exercising the actual deployment transaction."""
    events = []
    old_web = existing_web(manager)
    persist(manager, deployed_sha=OLD_SHA)
    monkeypatch.setattr(manager, "fetch_target", lambda: SHA)
    monkeypatch.setattr(manager, "ci_passed", lambda sha: True)
    monkeypatch.setattr(manager, "current_web", lambda: copy.deepcopy(old_web))
    monkeypatch.setattr(manager, "build", lambda sha: events.append(("build", sha)))

    def backup(web, sha):
        events.append(("backup", web["Id"], sha))
        folder = manager.root / "backups" / "test-backup"
        folder.mkdir(parents=True)
        deploy.write_json(folder / "metadata.json", {"outcome": "pending"})
        manager.state["in_progress"] = {"sha": sha, "backup": folder.name, "old_container": web["Id"]}
        manager.save()
        return folder

    def compose(*args, **kwargs):
        events.append(("compose", args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def command(args, **kwargs):
        args = [str(arg) for arg in args]
        events.append(("command", args))
        output = "new-web\n" if args[:3] == ["docker", "ps", "-aq"] else ""
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(manager, "backup", backup)
    monkeypatch.setattr(manager, "compose", compose)
    monkeypatch.setattr(manager, "command", command)
    return manager, events


def test_unchanged_commit_does_not_query_ci_or_touch_containers(operation, monkeypatch):
    manager, events = operation
    persist(manager, deployed_sha=SHA)
    monkeypatch.setattr(manager, "ci_passed", lambda sha: pytest.fail("No new commit needs a CI query"))
    assert manager.check() == "unchanged"
    assert events == []


def test_unsuccessful_ci_does_not_start_a_build_or_stop_web(operation, monkeypatch):
    manager, events = operation
    monkeypatch.setattr(manager, "ci_passed", lambda sha: False)
    assert manager.check() == "waiting"
    assert events == []
    assert deploy.read_json(manager.state_file)["deployed_sha"] == OLD_SHA


def test_build_failure_leaves_old_web_and_successful_sha_untouched(operation, monkeypatch):
    manager, events = operation

    def fail_build(sha):
        raise deploy.DeployError("Build failed")

    monkeypatch.setattr(manager, "build", fail_build)
    with pytest.raises(deploy.DeployError, match="Build failed"):
        manager.check()
    assert events == []
    state = deploy.read_json(manager.state_file)
    assert state["deployed_sha"] == OLD_SHA and not state.get("in_progress")


def test_superseded_build_never_stops_the_live_web(operation, monkeypatch):
    manager, events = operation
    targets = iter([SHA, NEW_SHA])
    monkeypatch.setattr(manager, "fetch_target", lambda: next(targets))
    assert manager.check() == "superseded"
    assert events == [("build", SHA)]
    assert deploy.read_json(manager.state_file)["deployed_sha"] == OLD_SHA


def test_successful_sha_is_saved_only_after_the_new_web_is_healthy(operation, monkeypatch):
    manager, events = operation

    def healthy(*args, **kwargs):
        state = deploy.read_json(manager.state_file)
        assert state["deployed_sha"] == OLD_SHA
        assert state["in_progress"]["sha"] == SHA
        assert args == ("up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "120", "web")
        events.append(("healthy", SHA))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "compose", healthy)
    assert manager.check() == "deployed"
    assert events == [("build", SHA), ("backup", "old-web", SHA), ("healthy", SHA)]
    state = deploy.read_json(manager.state_file)
    assert state["deployed_sha"] == SHA and not state.get("in_progress")
    assert deploy.read_json(manager.root / "backups/test-backup/metadata.json")["outcome"] == "success"


def test_backup_failure_restarts_previous_web_without_starting_new_web(operation, monkeypatch):
    manager, events = operation

    def failed_backup(web, sha):
        raise deploy.DeployError("Backup failed")

    monkeypatch.setattr(manager, "backup", failed_backup)
    with pytest.raises(deploy.DeployError, match="Backup failed"):
        manager.check()
    assert events == [("build", SHA), ("command", ["docker", "start", "old-web"])]
    state = deploy.read_json(manager.state_file)
    assert state["deployed_sha"] == OLD_SHA and not state.get("in_progress")


def test_backup_failure_and_restart_failure_pauses_automation(operation, monkeypatch):
    manager, _events = operation

    def fail_backup(*args):
        raise deploy.DeployError("backup")

    def fail_restart(*args, **kwargs):
        raise deploy.DeployError("restart")

    monkeypatch.setattr(manager, "backup", fail_backup)
    monkeypatch.setattr(manager, "command", fail_restart)
    with pytest.raises(deploy.DeployError):
        manager.check()
    assert deploy.read_json(manager.state_file)["paused"] is True


def test_failed_health_stops_candidate_pauses_and_retains_failure_backup(operation, monkeypatch):
    manager, events = operation

    def unhealthy(*args, **kwargs):
        raise deploy.DeployError("Health check failed")

    monkeypatch.setattr(manager, "compose", unhealthy)
    with pytest.raises(deploy.DeployError, match="Health check failed"):
        manager.check()
    assert ("command", ["docker", "stop", "new-web"]) in events
    state = deploy.read_json(manager.state_file)
    assert state["paused"] is True
    assert state["deployed_sha"] == OLD_SHA
    assert state["in_progress"]["sha"] == SHA
    metadata = deploy.read_json(manager.root / "backups/test-backup/metadata.json")
    assert metadata["outcome"] == "failed"
    assert (manager.root / "backups/test-backup/web.log").is_file()
    events.clear()
    assert manager.check() == "paused"
    assert events == []


def test_process_interruption_is_detected_before_any_external_work(operation):
    manager, events = operation
    persist(manager, in_progress={"sha": SHA, "backup": "test-backup"})
    assert manager.check() == "paused"
    assert events == []
    assert deploy.read_json(manager.state_file)["paused"] is True


def test_overlapping_checks_return_locked_without_external_work(operation):
    manager, events = operation
    with manager.lock():
        assert manager.check() == "locked"
    assert events == []


def test_api_rate_limit_defers_requests_until_server_reset(manager, monkeypatch):
    headers = Message()
    headers["Retry-After"] = "120"
    headers["X-RateLimit-Reset"] = "1500"
    error = HTTPError("https://api.github.com/test", 429, "rate limited", headers, None)
    monkeypatch.setattr(deploy.time, "time", lambda: 1000)

    def limited(*args, **kwargs):
        raise error

    monkeypatch.setattr(deploy, "urlopen", limited)
    assert manager.ci_passed(SHA) is False
    assert deploy.read_json(manager.state_file)["next_api_at"] == 1500
    monkeypatch.setattr(manager, "api_runs", lambda sha: pytest.fail("Rate-limited API must not be queried"))
    assert manager.ci_passed(SHA) is False


def test_api_network_error_keeps_the_deployment_gate_closed(manager, monkeypatch):
    def offline(*args, **kwargs):
        raise URLError("offline")

    monkeypatch.setattr(deploy, "urlopen", offline)
    assert manager.ci_passed(SHA) is False


def test_retention_keeps_seven_successes_and_every_failed_or_pending_backup(manager, monkeypatch):
    removed = []
    monkeypatch.setattr(manager, "command", lambda args, **kwargs: removed.append(args))
    for number in range(10):
        folder = manager.root / "backups" / f"202601{number + 1:02}"
        deploy.write_json(folder / "metadata.json", {"outcome": "success", "previous_image": f"old:{number}"})
    for name, outcome in [("20250101-failed", "failed"), ("20250102-pending", "pending")]:
        deploy.write_json(manager.root / "backups" / name / "metadata.json", {"outcome": outcome})
    manager.retain_backups()
    assert sorted(folder.name for folder in (manager.root / "backups").iterdir()) == [
        "20250101-failed", "20250102-pending", *[f"202601{number:02}" for number in range(4, 11)]
    ]
    assert len(removed) == 3
    assert all(args[:3] == ["docker", "image", "rm"] for args in removed)


@pytest.mark.parametrize("backup", ["../backup", "/tmp/backup", ".", "..", ""])
def test_restore_rejects_paths_before_running_commands(manager, monkeypatch, backup):
    monkeypatch.setattr(manager, "command", lambda *args, **kwargs: pytest.fail("Invalid restore must do no work"))
    with pytest.raises(deploy.DeployError):
        manager.restore(backup, confirmed=True)


def test_restore_requires_explicit_data_loss_acknowledgement(manager, monkeypatch):
    monkeypatch.setattr(manager, "command", lambda *args, **kwargs: pytest.fail("Unconfirmed restore must do no work"))
    with pytest.raises(deploy.DeployError, match="confirm-data-loss"):
        manager.restore("backup")


def test_backup_saves_recovery_marker_before_stopping_the_previous_web(manager, monkeypatch):
    old_web = {"Id": "old-web", "Image": "sha256:old-image"}
    events = []
    persist(manager, deployed_sha=OLD_SHA)

    def command(args, **kwargs):
        if args[:2] == ["docker", "stop"]:
            marker = deploy.read_json(manager.state_file)["in_progress"]
            assert marker["sha"] == SHA and marker["old_container"] == "old-web"
        events.append(args[:3])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "command", command)
    monkeypatch.setattr(manager, "snapshot_compose", lambda *args: events.append("snapshot"))
    monkeypatch.setattr(manager, "backup_database", lambda *args: events.append("database"))
    folder = manager.backup(old_web, SHA)
    assert events == [["docker", "image", "tag"], "snapshot", ["docker", "stop", "--time"], "database"]
    assert deploy.read_json(folder / "metadata.json")["previous_sha"] == OLD_SHA
    assert folder.stat().st_mode & 0o777 == 0o700


def test_build_pins_exact_sha_and_external_existing_database_volume(manager, monkeypatch):
    commands = []
    candidate = candidate_compose(manager)

    def command(args, **kwargs):
        commands.append([str(arg) for arg in args])
        stdout = json.dumps(candidate) if list(args)[:2] == ["docker", "compose"] else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(manager, "command", command)
    monkeypatch.setattr(manager, "current_web", lambda: existing_web(manager))
    monkeypatch.setattr(manager, "read_current_compose", lambda web: copy.deepcopy(candidate))
    manager.build(SHA)
    config = deploy.read_json(manager.override)
    assert config["volumes"]["election-data"] == {"external": True, "name": manager.config.volume}
    assert config["services"]["web"]["image"] == manager.image_name(SHA)
    assert commands[0][-4:] == ["checkout", "--force", "--detach", SHA]
    build_command = next(args for args in commands if args[:2] == ["docker", "build"])
    assert f"org.opencontainers.image.revision={SHA}" in build_command
    compose_command = next(args for args in commands if args[:2] == ["docker", "compose"])
    assert compose_command[compose_command.index("--env-file") + 1] == str(manager.config.env_file)
    assert compose_command[compose_command.index("--project-name") + 1] == manager.config.project
    assert not (manager.checkout_dir / ".env").exists()


def test_subprocess_environment_cannot_override_persistent_compose_configuration(manager, monkeypatch):
    captured = {}
    for key in ("SECRET_KEY", "TUNNEL_TOKEN", "DATABASE", "COMPOSE_PROJECT_NAME", "COMPOSE_FILE"):
        monkeypatch.setenv(key, "unrelated-shell-value")

    def run(args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    manager.command(["docker", "compose", "version"])
    assert not ({"SECRET_KEY", "TUNNEL_TOKEN", "DATABASE", "COMPOSE_PROJECT_NAME", "COMPOSE_FILE"} & captured.keys())
    assert captured["GIT_TERMINAL_PROMPT"] == "0"


def test_candidate_configuration_preserves_current_data_and_tunnel_network(manager):
    manager.validate_candidate(candidate_compose(manager), existing_web(manager))


@pytest.mark.parametrize(
    "unsafe_change",
    [
        "missing_data_mount",
        "different_data_mount",
        "bind_mount",
        "read_only_database",
        "different_database",
        "missing_database",
        "different_volume_name",
        "non_external_volume",
        "missing_healthcheck",
        "disabled_healthcheck",
        "none_healthcheck",
        "different_network",
        "no_network",
    ],
)
def test_unsafe_candidate_is_rejected_before_the_current_web_can_be_stopped(manager, unsafe_change):
    config = candidate_compose(manager)
    web = config["services"]["web"]
    if unsafe_change == "missing_data_mount":
        web["volumes"] = []
    elif unsafe_change == "different_data_mount":
        web["volumes"][0]["target"] = "/other-data"
    elif unsafe_change == "bind_mount":
        web["volumes"][0].update(type="bind", source="/tmp/database")
    elif unsafe_change == "read_only_database":
        web["volumes"][0]["read_only"] = True
    elif unsafe_change == "different_database":
        web["environment"]["DATABASE"] = "/tmp/empty.db"
    elif unsafe_change == "missing_database":
        del web["environment"]["DATABASE"]
    elif unsafe_change == "different_volume_name":
        config["volumes"]["election-data"]["name"] = "empty-new-volume"
    elif unsafe_change == "non_external_volume":
        config["volumes"]["election-data"]["external"] = False
    elif unsafe_change == "missing_healthcheck":
        del web["healthcheck"]
    elif unsafe_change == "disabled_healthcheck":
        web["healthcheck"]["disable"] = True
    elif unsafe_change == "none_healthcheck":
        web["healthcheck"]["test"] = ["NONE"]
    elif unsafe_change == "different_network":
        config["networks"]["default"]["name"] = "new-network"
    elif unsafe_change == "no_network":
        web["networks"] = {}
    with pytest.raises(deploy.DeployError):
        manager.validate_candidate(config, existing_web(manager))


def test_restore_can_recover_after_compose_removed_the_web_container(manager, monkeypatch):
    folder = manager.root / "backups" / "recoverable"
    deploy.write_json(folder / "metadata.json", {"previous_image": "rollback:previous", "previous_sha": OLD_SHA})
    deploy.write_json(folder / "previous-compose.json", candidate_compose(manager))
    (folder / "database.db").write_bytes(b"database snapshot")
    persist(manager, deployed_sha=SHA, paused=True, in_progress={"sha": SHA})
    commands = []
    helper_commands = []
    restored_compose = []
    monkeypatch.setattr(manager, "web_ids", lambda: [])
    monkeypatch.setattr(manager, "current_web", lambda: pytest.fail("A removed web has no inspectable container"))

    def command(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def run(args, **kwargs):
        helper_commands.append(args)
        if "stdout" in kwargs:
            kwargs["stdout"].write(b"preserved current database and WAL files")
        if "stdin" in kwargs:
            assert kwargs["stdin"].read() == b"database snapshot"
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def compose(*args, **kwargs):
        restored_compose.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "command", command)
    monkeypatch.setattr(deploy.subprocess, "run", run)
    monkeypatch.setattr(manager, "compose", compose)
    manager.restore(folder.name, confirmed=True)
    assert commands == [["docker", "image", "inspect", "rollback:previous"]]
    assert len(helper_commands) == 2
    assert f"type=volume,src={manager.config.volume},dst=/data,readonly" in helper_commands[0]
    assert f"type=volume,src={manager.config.volume},dst=/data" in helper_commands[1]
    assert len(list(folder.glob("before-restore-*.tar"))) == 1
    assert restored_compose[0][1]["files"] == [folder / "previous-compose.json"]
    assert "--wait" in restored_compose[0][0]
    state = deploy.read_json(manager.state_file)
    assert state["deployed_sha"] == OLD_SHA and state["paused"] is True
    assert "in_progress" not in state


def test_restore_rejects_a_backup_directory_symlink(manager, monkeypatch, tmp_path):
    backups = manager.root / "backups"
    backups.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (backups / "alias").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(manager, "command", lambda *args, **kwargs: pytest.fail("Symlink restore must do no work"))
    with pytest.raises(deploy.DeployError, match="backup directory"):
        manager.restore("alias", confirmed=True)
