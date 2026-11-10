#!/usr/bin/env python3
"""Poll trusted GitHub CI and update one existing Compose web service."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


LOG = logging.getLogger("gp11-deploy")


class DeployError(RuntimeError):
    """An operation could not be completed safely."""


class Locked(DeployError):
    pass


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path, default=None):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {} if default is None else default


@dataclass
class Config:
    repository: str = "EthanMok4268/GP11_EMV_System"
    branch: str = "master"
    workflow: str = "ci.yml"
    project: str = "gp11_emv_system"
    volume: str = "gp11_emv_system_election-data"
    env_file: Path = Path("/home/ryan/GP11_EMV_System/.env")
    state_dir: Path = Path("/home/ryan/.local/share/gp11-deploy")
    token_file: Path | None = None

    def __post_init__(self):
        self.env_file = Path(self.env_file).expanduser().resolve()
        self.state_dir = Path(self.state_dir).expanduser().resolve()
        if self.token_file:
            self.token_file = Path(self.token_file).expanduser().resolve()
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repository):
            raise DeployError("Invalid GitHub repository name")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", self.project):
            raise DeployError("Invalid Compose project name")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", self.volume):
            raise DeployError("Invalid Docker volume name")
        if Path(self.workflow).name != self.workflow or not self.workflow.endswith((".yml", ".yaml")):
            raise DeployError("workflow must be a YAML filename")
        if not self.branch or self.branch.startswith("-") or any(c.isspace() for c in self.branch):
            raise DeployError("Invalid branch")

    @classmethod
    def load(cls, path):
        return cls(**read_json(path)) if Path(path).is_file() else cls._missing()

    @staticmethod
    def _missing():
        raise DeployError("Deployment configuration is missing; run the installer first")


class Deployer:
    def __init__(self, config):
        self.config = config
        self.root = config.state_dir
        self.checkout_dir = self.root / "checkout"
        self.override = self.root / "runtime" / "compose.json"
        self.state_file = self.root / "state.json"
        self.state = read_json(self.state_file)
        self.previous_compose = None

    def save(self):
        write_json(self.state_file, self.state)

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Locked("Another deployment operation is running") from error
            self.state = read_json(self.state_file)
            yield

    def command(self, args, *, check=True, timeout=300, input=None):
        # Compose must use the persistent .env, not values inherited by a shell.
        env = {key: value for key, value in os.environ.items()
               if key not in ("SECRET_KEY", "TUNNEL_TOKEN", "DATABASE")
               and not key.startswith("COMPOSE_")}
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = subprocess.run([str(arg) for arg in args], input=input, text=True,
                                    capture_output=True, timeout=timeout, env=env, umask=0o022)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeployError(f"{args[0]} could not finish ({type(error).__name__})") from error
        if check and result.returncode:
            # Do not put resolved environment/configuration or API tokens in journals.
            error = DeployError(f"{args[0]} command failed (exit {result.returncode})")
            error.diagnostics = result.stdout + result.stderr
            raise error
        return result

    def compose(self, *args, files=None):
        command = ["docker", "compose", "--project-name", self.config.project,
                   "--project-directory", self.checkout_dir,
                   "--env-file", self.config.env_file]
        for file in files or [self.checkout_dir / "compose.yaml", self.override]:
            command += ["-f", file]
        return self.command([*command, *args], timeout=1500)

    def request_json(self, url):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "gp11-deploy",
                   "X-GitHub-Api-Version": "2026-03-10"}
        if self.config.token_file and self.config.token_file.is_file():
            token = self.config.token_file.read_text().strip()
            if token:
                headers["Authorization"] = "Bearer " + token
        try:
            with urlopen(Request(url, headers=headers), timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code in (403, 429):
                now = time.time()
                retry = error.headers.get("Retry-After", "60")
                try:
                    retry_at = now + float(retry)
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry).timestamp()
                    except (ValueError, TypeError):
                        retry_at = now + 60
                try:
                    reset = float(error.headers.get("X-RateLimit-Reset", "0"))
                except ValueError:
                    reset = 0
                self.state["next_api_at"] = max(now + 60, retry_at, reset)
                self.save()
            raise DeployError(f"GitHub API unavailable (HTTP {error.code})") from error
        except (URLError, TimeoutError, ValueError, OSError) as error:
            raise DeployError(f"GitHub API unavailable ({type(error).__name__})") from error

    def api_runs(self, sha):
        query = urlencode({"branch": self.config.branch, "event": "push", "head_sha": sha,
                           "per_page": 100})
        return self.request_json(
            f"https://api.github.com/repos/{self.config.repository}/actions/workflows/"
            f"{quote(self.config.workflow, safe='')}/runs?{query}")

    def ci_passed(self, sha):
        if self.state.get("next_api_at", 0) > time.time():
            LOG.info("Waiting for GitHub API rate limit reset")
            return False
        try:
            runs = self.api_runs(sha).get("workflow_runs", [])
            if not runs:
                return False
            latest = max(runs, key=lambda run: (int(run["id"]), int(run.get("run_attempt", 1))))
            accepted = (
                latest.get("head_sha") == sha
                and latest.get("head_branch") == self.config.branch
                and latest.get("event") == "push"
                and latest.get("head_repository", {}).get("full_name", "").lower()
                == self.config.repository.lower()
                and latest.get("repository", {}).get("full_name", "").lower()
                == self.config.repository.lower()
                and latest.get("path", "").split("@", 1)[0]
                == f".github/workflows/{self.config.workflow}"
                and latest.get("status") == "completed"
                and latest.get("conclusion") == "success"
            )
            if accepted:
                LOG.info("CI accepted %s: https://github.com/%s/actions/runs/%s",
                         sha, self.config.repository, latest["id"])
            return accepted
        except (DeployError, KeyError, TypeError, ValueError, AttributeError) as error:
            LOG.warning("CI gate remains closed: %s", error)
            return False

    @staticmethod
    def valid_sha(value):
        if not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise DeployError("Git did not return a valid commit SHA")
        return value

    def remote_sha(self):
        result = self.command(["git", "ls-remote", f"https://github.com/{self.config.repository}.git",
                               f"refs/heads/{self.config.branch}"], timeout=60)
        return self.valid_sha(result.stdout.split()[0] if result.stdout.split() else "")

    def fetch_target(self):
        if not (self.checkout_dir / ".git").is_dir():
            if self.checkout_dir.exists():
                raise DeployError("Deployment checkout exists but is not a Git repository")
            self.command(["git", "clone", "--no-checkout", "--", 
                          f"https://github.com/{self.config.repository}.git", self.checkout_dir])
        self.command(["git", "-C", self.checkout_dir, "fetch", "--no-tags", "origin",
                      f"refs/heads/{self.config.branch}"], timeout=60)
        return self.valid_sha(self.command(
            ["git", "-C", self.checkout_dir, "rev-parse", "FETCH_HEAD"]).stdout.strip())

    def current_web(self):
        ids = self.web_ids()
        if len(ids) != 1:
            raise DeployError("Expected exactly one existing web container")
        container = json.loads(self.command(["docker", "inspect", ids[0]]).stdout)[0]
        if not any(mount.get("Type") == "volume" and mount.get("Name") == self.config.volume
                   and mount.get("Destination") == "/data" for mount in container["Mounts"]):
            raise DeployError("Existing web container does not use the configured data volume")
        return container

    def web_ids(self):
        return self.command(["docker", "ps", "-aq", "--filter",
                             f"label=com.docker.compose.project={self.config.project}", "--filter",
                             "label=com.docker.compose.service=web"]).stdout.split()

    def validate_candidate(self, config, old_web):
        web = config["services"]["web"]
        mounts = [mount for mount in web.get("volumes", []) if mount.get("target") == "/data"]
        if (len(mounts) != 1 or mounts[0].get("type") != "volume"
                or mounts[0].get("read_only")
                or config.get("volumes", {}).get(mounts[0].get("source"), {}).get("name") != self.config.volume
                or not config["volumes"][mounts[0]["source"]].get("external")):
            raise DeployError("Candidate must preserve the existing database volume at /data")
        if web.get("environment", {}).get("DATABASE") != "/data/election_system.db":
            raise DeployError("Candidate must preserve DATABASE=/data/election_system.db")
        health = web.get("healthcheck", {})
        if health.get("disable") or not health.get("test") or health["test"][0] == "NONE":
            raise DeployError("Candidate must retain a healthcheck")
        networks = {config.get("networks", {}).get(key, {}).get("name") for key in web.get("networks", {})}
        if not set(old_web.get("NetworkSettings", {}).get("Networks", {})).issubset(networks):
            raise DeployError("Candidate must retain the existing web network for Cloudflare Tunnel")

    def build(self, sha):
        # Capture old configuration BEFORE switching this reusable checkout.
        old_web = self.current_web()
        self.previous_compose = self.read_current_compose(old_web)
        self.command(["git", "-C", self.checkout_dir, "checkout", "--force", "--detach", sha])
        # This private checkout never contains the user's .env or working edits.
        write_json(self.override, {"services": {"web": {"image": self.image_name(sha)}},
                                  "volumes": {"election-data": {
                                      "external": True, "name": self.config.volume}}})
        self.compose("config", "--quiet")
        self.validate_candidate(json.loads(self.compose("config", "--format", "json").stdout), old_web)
        try:
            result = self.command(["docker", "build", "--label", f"org.opencontainers.image.revision={sha}",
                                   "--tag", self.image_name(sha), self.checkout_dir],
                                  timeout=1500, check=False)
            (self.root / "build.log").write_text(result.stdout + result.stderr)
            os.chmod(self.root / "build.log", 0o600)
            if result.returncode:
                raise DeployError(f"Image build failed; see {self.root / 'build.log'}")
        except DeployError:
            LOG.error("Build did not finish for %s; existing web is unchanged", sha)
            raise

    def image_name(self, sha):
        return f"{self.config.project}-deploy:{sha}"

    def read_current_compose(self, old_web):
        labels = old_web["Config"].get("Labels", {})
        sources = labels.get("com.docker.compose.project.config_files", "").split(",")
        if not sources or not all(Path(source).is_file() for source in sources):
            raise DeployError("Cannot locate the currently deployed Compose configuration")
        return json.loads(self.compose("config", "--format", "json", files=sources).stdout)

    def snapshot_compose(self, old_web, folder, previous_image):
        previous = self.previous_compose or self.read_current_compose(old_web)
        # Save only web and its existing network/volume; resolved secrets stay mode 600.
        previous["services"] = {"web": previous["services"]["web"]}
        web = previous["services"]["web"]
        web["image"] = previous_image
        web.pop("build", None)
        previous["volumes"] = {"election-data": {"external": True, "name": self.config.volume}}
        for network in previous.get("networks", {}).values():
            network_name = network["name"]
            network.clear()
            network.update(external=True, name=network_name)
        previous.pop("secrets", None)
        write_json(folder / "previous-compose.json", previous)

    def backup_database(self, old_web, destination):
        script = (
            "import sqlite3,shutil,sys; "
            "source=sqlite3.connect('file:/data/election_system.db?mode=ro',uri=True); "
            "target=sqlite3.connect('/tmp/snapshot.db'); source.backup(target); "
            "target.close(); source.close(); "
            "shutil.copyfileobj(open('/tmp/snapshot.db','rb'),sys.stdout.buffer)"
        )
        args = ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
                "--user", "0:0", "--mount",
                f"type=volume,src={self.config.volume},dst=/data,readonly",
                "--entrypoint", "python", old_web["Image"], "-c", script]
        try:
            with destination.open("wb") as output:
                os.chmod(destination, 0o600)
                subprocess.run(args, stdout=output, stderr=subprocess.PIPE, timeout=120, check=True)
                output.flush()
                os.fsync(output.fileno())
            with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as database:
                if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise DeployError("SQLite backup did not pass quick_check")
        except (OSError, subprocess.SubprocessError, sqlite3.Error) as error:
            raise DeployError(f"Database backup failed ({type(error).__name__})") from error

    def backup(self, old_web, sha):
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + sha[:12]
        folder = self.root / "backups" / name
        folder.mkdir(parents=True, mode=0o700)
        previous_image = f"{self.config.project}-rollback:{name.lower()}"
        metadata = {"target_sha": sha, "previous_sha": self.state.get("deployed_sha"),
                    "previous_image": previous_image, "outcome": "pending"}
        write_json(folder / "metadata.json", metadata)
        self.command(["docker", "image", "tag", old_web["Image"], previous_image])
        self.snapshot_compose(old_web, folder, previous_image)
        self.state["in_progress"] = {"sha": sha, "backup": name, "old_container": old_web["Id"]}
        self.save()
        self.command(["docker", "stop", "--time", "30", old_web["Id"]], timeout=60)
        self.backup_database(old_web, folder / "database.db")
        return folder

    def mark_backup(self, folder, outcome):
        metadata = read_json(folder / "metadata.json")
        metadata["outcome"] = outcome
        write_json(folder / "metadata.json", metadata)

    def retain_backups(self):
        folders = sorted((self.root / "backups").glob("*"), reverse=True)
        successful = [folder for folder in folders if folder.is_dir()
                      and read_json(folder / "metadata.json").get("outcome") == "success"]
        expired = successful[7:]
        retained = [folder for folder in folders if folder not in expired]
        protected = {self.state.get("image")}
        for folder in retained:
            metadata = read_json(folder / "metadata.json")
            protected.add(metadata.get("previous_image"))
            for key in ("previous_sha", "target_sha"):
                if metadata.get(key):
                    protected.add(self.image_name(metadata[key]))
        for folder in expired:
            metadata = read_json(folder / "metadata.json")
            self.command(["docker", "image", "rm", metadata["previous_image"]], check=False)
            for key in ("previous_sha", "target_sha"):
                if metadata.get(key) and self.image_name(metadata[key]) not in protected:
                    self.command(["docker", "image", "rm", self.image_name(metadata[key])], check=False)
            shutil.rmtree(folder)

    def pause_after_failure(self, reason):
        self.state.update(paused=True, pause_reason=reason, last_error=reason)
        self.save()

    def check(self):
        try:
            with self.lock():
                if self.state.get("in_progress"):
                    self.pause_after_failure("Interrupted deployment: inspect backup and restore or retry")
                if self.state.get("paused"):
                    return "paused"
                sha = self.fetch_target()
                LOG.info("Latest %s commit: %s", self.config.branch, sha)
                if self.state.get("deployed_sha") == sha:
                    return "unchanged"
                if not self.ci_passed(sha):
                    return "waiting"
                old_web = self.current_web()
                if not old_web.get("State", {}).get("Running"):
                    raise DeployError("Existing web is stopped; recover it before deploying")
                self.build(sha)
                if self.fetch_target() != sha:
                    return "superseded"
                try:
                    folder = self.backup(old_web, sha)
                except Exception as error:
                    transaction = self.state.pop("in_progress", {})
                    if transaction.get("backup"):
                        self.mark_backup(self.root / "backups" / transaction["backup"], "failed")
                    try:
                        self.command(["docker", "start", old_web["Id"]])
                    except DeployError:
                        self.pause_after_failure("Backup failed and previous web could not restart")
                    self.state["last_error"] = "Backup failed; previous web restart requested"
                    self.save()
                    raise DeployError(str(error)) from error
                try:
                    self.compose("up", "-d", "--no-build", "--no-deps", "--wait",
                                 "--wait-timeout", "120", "web")
                except Exception as error:
                    self.mark_backup(folder, "failed")
                    self.pause_after_failure("New web failed health checks; restore or explicitly retry")
                    output = [getattr(error, "diagnostics", "")]
                    for container_id in self.web_ids():
                        self.command(["docker", "stop", container_id], check=False)
                        logs = self.command(["docker", "logs", "--tail", "200", container_id], check=False)
                        output += [logs.stdout, logs.stderr]
                    (folder / "web.log").write_text("".join(output))
                    os.chmod(folder / "web.log", 0o600)
                    raise DeployError(str(error)) from error
                self.mark_backup(folder, "success")
                self.state.update(deployed_sha=sha, image=self.image_name(sha), paused=False)
                for key in ("in_progress", "last_error", "pause_reason"):
                    self.state.pop(key, None)
                self.save()
                self.retain_backups()
                return "deployed"
        except Locked:
            return "locked"

    def restore(self, name, confirmed=False):
        if not confirmed:
            raise DeployError("Restore requires --confirm-data-loss; it discards records after the backup")
        if not name or Path(name).name != name or name in (".", ".."):
            raise DeployError("Backup must be a directory name, not a path")
        with self.lock():
            folder = self.root / "backups" / name
            if folder.is_symlink() or folder.resolve().parent != (self.root / "backups").resolve():
                raise DeployError("Backup must be inside the backup directory")
            if not (folder / "database.db").is_file() or not (folder / "previous-compose.json").is_file():
                raise DeployError("Backup is incomplete")
            metadata = read_json(folder / "metadata.json")
            self.command(["docker", "image", "inspect", metadata["previous_image"]])
            self.pause_after_failure("Manual restore in progress; resume only after verification")
            ids = self.web_ids()
            if len(ids) > 1:
                raise DeployError("Multiple web containers exist; inspect them before restoring")
            if ids:
                self.current_web()  # Verify that any existing container uses the expected volume.
                self.command(["docker", "stop", "--time", "30", ids[0]], timeout=60)
            # A raw archive also preserves corrupt databases/WALs that SQLite cannot open.
            archive = folder / ("before-restore-" + str(time.time_ns()) + ".tar")
            preserve = ["docker", "run", "--rm", "--network", "none", "--read-only", "--user", "0:0",
                        "--mount", f"type=volume,src={self.config.volume},dst=/data,readonly",
                        "--entrypoint", "python", metadata["previous_image"], "-c",
                        "import tarfile,sys; t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|'); "
                        "t.add('/data',arcname='data'); t.close()"]
            with archive.open("wb") as output:
                os.chmod(archive, 0o600)
                subprocess.run(preserve, stdout=output, stderr=subprocess.PIPE, check=True, timeout=120)
                output.flush()
                os.fsync(output.fileno())
            script = (
                "import os,sqlite3,sys; "
                "data=sys.stdin.buffer.read(); "
                "open('/data/election_system.db.restore','wb').write(data); "
                "db=sqlite3.connect('/data/election_system.db.restore'); "
                "assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'; db.close(); "
                "os.chown('/data/election_system.db.restore',10001,10001); "
                "os.chmod('/data/election_system.db.restore',0o600); "
                "f=open('/data/election_system.db.restore','rb'); os.fsync(f.fileno()); f.close(); "
                "[os.unlink('/data/election_system.db'+s) for s in ('-wal','-shm','-journal') "
                "if os.path.exists('/data/election_system.db'+s)]; "
                "os.replace('/data/election_system.db.restore','/data/election_system.db'); "
                "fd=os.open('/data',os.O_DIRECTORY); os.fsync(fd); os.close(fd)"
            )
            args = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
                    "--user", "0:0", "--mount", f"type=volume,src={self.config.volume},dst=/data",
                    "--entrypoint", "python", metadata["previous_image"], "-c", script]
            with (folder / "database.db").open("rb") as source:
                subprocess.run(args, stdin=source, capture_output=True, check=True, timeout=120)
            self.compose("up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "120",
                         "web", files=[folder / "previous-compose.json"])
            self.state.update(deployed_sha=metadata.get("previous_sha"),
                              image=metadata["previous_image"], paused=True,
                              pause_reason="Restored; verify the website, then resume")
            self.state.pop("in_progress", None)
            self.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("action", choices=("check", "check-ci", "status", "pause", "resume", "retry", "restore"))
    parser.add_argument("--backup")
    parser.add_argument("--confirm-data-loss", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.umask(0o077)
    try:
        deployer = Deployer(Config.load(args.config))
        if args.action == "check":
            LOG.info("Deployment result: %s", deployer.check())
        elif args.action == "check-ci":
            with deployer.lock():
                sha = deployer.remote_sha()
                if not deployer.ci_passed(sha):
                    raise DeployError("Latest master commit has not passed the configured push CI")
                print(f"CI ready: {sha}")
        elif args.action == "status":
            status = dict(deployer.state)
            status["backups"] = [dict(name=folder.name, **read_json(folder / "metadata.json"))
                                 for folder in sorted((deployer.root / "backups").glob("*"), reverse=True)
                                 if folder.is_dir()]
            print(json.dumps(status, indent=2))
        elif args.action == "restore":
            deployer.restore(args.backup, args.confirm_data_loss)
        else:
            with deployer.lock():
                if args.action == "pause":
                    deployer.state.update(paused=True, pause_reason="Paused by operator")
                else:
                    if deployer.state.get("in_progress") and args.action != "retry":
                        raise DeployError("Interrupted/failed cutover requires restore or explicit retry")
                    deployer.state.pop("in_progress", None)
                    deployer.state.pop("pause_reason", None)
                    deployer.state["paused"] = False
                deployer.save()
        return 0
    except (DeployError, OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        LOG.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
