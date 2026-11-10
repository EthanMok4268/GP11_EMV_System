#!/usr/bin/env bash
# Install the host-side deployment tool after the latest master CI succeeds.
set -euo pipefail
umask 077

usage() {
  cat <<'USAGE'
Usage: bash scripts/install-deploy.sh --user-service [options]
       sudo bash scripts/install-deploy.sh [options]

  --user-service       Install for the current user's systemd manager (no sudo)
  --user USER          Docker-capable deployment user (default: sudo caller)
  --project NAME       Existing Compose project (default: gp11_emv_system)
  --volume NAME        Existing /data volume (default: gp11_emv_system_election-data)
  --env-file PATH      Existing application .env (default: repository/.env)
  --state-dir PATH     Checkout, deployment state and backups
  --config-dir PATH    Host configuration and optional github-token file
  --install-dir PATH   Tool directory (user mode: ~/.local/lib/gp11-deploy;
                       system mode: /usr/local/lib/gp11-deploy)
  --no-enable          Install without enabling or starting the timer
  --help               Show this help

Default installation first verifies the latest master commit passed ci.yml.
--no-enable is for staging only; no containers are changed by this installer.
User-service mode requires login lingering to already be enabled for boot startup.
USAGE
}

fail() {
  printf 'Installation failed: %s\n' "$*" >&2
  exit 1
}

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(dirname -- "$script_dir")
deploy_user=${SUDO_USER:-ryan}
project=gp11_emv_system
volume=gp11_emv_system_election-data
env_file="$repo_dir/.env"
state_dir=
config_dir=
install_dir=
enable_timer=1
user_service=0
user_set=0
project_set=0
volume_set=0
env_file_set=0
state_dir_set=0

while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --user-service) user_service=1; shift ;;
    --no-enable) enable_timer=0; shift ;;
    --user|--project|--volume|--env-file|--state-dir|--config-dir|--install-dir)
      (($# >= 2)) || fail "Missing value for $1"
      [[ -n "$2" && "$2" != --* ]] || fail "Missing value for $1"
      case "$1" in
        --user) deploy_user=$2; user_set=1 ;;
        --project) project=$2; project_set=1 ;;
        --volume) volume=$2; volume_set=1 ;;
        --env-file) env_file=$2; env_file_set=1 ;;
        --state-dir) state_dir=$2; state_dir_set=1 ;;
        --config-dir) config_dir=$2 ;;
        --install-dir) install_dir=$2 ;;
      esac
      shift 2
      ;;
    *) fail "Unknown option: $1" ;;
  esac
done

if ((user_service)); then
  ((EUID != 0)) || fail "Run --user-service as the deployment user, without sudo."
  if ((!user_set)); then deploy_user=$(id -un); fi
  [[ "$deploy_user" == "$(id -un)" ]] || fail "--user-service must target the current user."
else
  ((EUID == 0)) || fail "Run with sudo for system installation, or use --user-service."
fi
[[ "$deploy_user" =~ ^[a-z_][a-z0-9_-]*\$?$ && "$deploy_user" != root ]] || fail "Choose a non-root deployment user."

for executable in python3 docker git systemctl getent install realpath; do
  command -v "$executable" >/dev/null || fail "Required command is missing: $executable"
done
passwd_entry=$(getent passwd "$deploy_user") || fail "Unknown deployment user: $deploy_user"
IFS=: read -r _ _ deploy_uid deploy_gid _ deploy_home _ <<< "$passwd_entry"
deploy_group=$(id -gn "$deploy_user")
state_dir=${state_dir:-"$deploy_home/.local/share/gp11-deploy"}
config_dir=${config_dir:-"$deploy_home/.config/gp11-deploy"}
systemctl_command=(systemctl)
runtime_owner=(-o root -g root)
config_owner=(-o "$deploy_user" -g "$deploy_group")
if ((user_service)); then
  install_dir=${install_dir:-"$deploy_home/.local/lib/gp11-deploy"}
  unit_dir="$deploy_home/.config/systemd/user"
  systemctl_command+=(--user)
  runtime_owner=()
  config_owner=()
  "${systemctl_command[@]}" show-environment >/dev/null || fail "The user systemd manager is unavailable."
  if ((enable_timer)); then
    command -v loginctl >/dev/null || fail "loginctl is needed to verify boot startup."
    [[ "$(loginctl show-user "$deploy_user" --property=Linger --value)" == yes ]] \
      || fail "User lingering is not enabled. Ask the host administrator to enable it, or stage with --no-enable."
  fi
  if systemctl is-active --quiet gp11-deploy.timer; then
    fail "The system-wide timer is active; stop it before choosing user-service mode."
  fi
else
  command -v runuser >/dev/null || fail "Required command is missing: runuser"
  install_dir=${install_dir:-/usr/local/lib/gp11-deploy}
  unit_dir=/etc/systemd/system
fi

for path in "$env_file" "$state_dir" "$config_dir" "$install_dir"; do
  [[ "$path" = /* && "$path" != *$'\n'* && "$path" != *$'\r'* ]] || fail "Use absolute paths without newlines."
done
state_dir=$(realpath -m -- "$state_dir")
config_dir=$(realpath -m -- "$config_dir")
install_dir=$(realpath -m -- "$install_dir")
for source_file in scripts/deploy.py deploy/gp11-deploy.service deploy/gp11-deploy.timer; do
  [[ -f "$repo_dir/$source_file" ]] || fail "$source_file is missing."
done
[[ -d /run/systemd/system ]] || fail "This host must boot with systemd."

as_user() {
  if ((user_service)); then
    "$@"
  else
    runuser -u "$deploy_user" -- "$@"
  fi
}

temp_dir=$(mktemp -d)
trap 'rm -rf -- "$temp_dir"' EXIT
# Preserve local configuration and the separate token on reinstalls. Only
# explicitly supplied options replace the corresponding saved setting.
if ((!user_service)); then chown "$deploy_uid:$deploy_gid" "$temp_dir"; fi
python3 - "$temp_dir/config.json" "$config_dir/config.json" "$project" "$volume" "$env_file" "$state_dir" "$config_dir" \
  "$project_set" "$volume_set" "$env_file_set" "$state_dir_set" > "$temp_dir/values" <<'PY'
import json
import pathlib
import sys

destination, existing, project, volume, env_file, state_dir, config_dir, *flags = sys.argv[1:]
defaults = {
    "repository": "EthanMok4268/GP11_EMV_System",
    "branch": "master",
    "workflow": "ci.yml",
    "project": project,
    "volume": volume,
    "env_file": env_file,
    "state_dir": state_dir,
    "token_file": str(pathlib.Path(config_dir) / "github-token"),
}
config = defaults.copy()
if pathlib.Path(existing).exists():
    saved = json.loads(pathlib.Path(existing).read_text())
    if not isinstance(saved, dict):
        raise SystemExit("Existing deployment configuration must be a JSON object.")
    config.update(saved)
for key, flag in zip(("project", "volume", "env_file", "state_dir"), flags):
    if flag == "1":
        config[key] = defaults[key]
for key in ("env_file", "state_dir"):
    path = pathlib.Path(config[key])
    if not path.is_absolute() or "\n" in str(path) or "\r" in str(path):
        raise SystemExit("Configured paths must be absolute and contain no newlines.")
    config[key] = str(path.resolve(strict=(key == "env_file")))
pathlib.Path(destination).write_text(json.dumps(config, indent=2) + "\n")
for key in ("project", "volume", "env_file", "state_dir"):
    sys.stdout.write(config[key] + "\0")
PY
mapfile -d '' -t resolved < "$temp_dir/values"
project=${resolved[0]}
volume=${resolved[1]}
env_file=${resolved[2]}
state_dir=${resolved[3]}
if ((!user_service)); then chown "$deploy_uid:$deploy_gid" "$temp_dir/config.json"; fi
[[ "$project" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail "Invalid Compose project name."
[[ "$volume" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail "Invalid Docker volume name."

as_user test -r "$env_file" || fail "The deployment user cannot read the application .env."
as_user docker info --format '{{.ServerVersion}}' >/dev/null || fail "The deployment user cannot access Docker."
as_user docker compose version >/dev/null || fail "The Docker Compose plugin is required."
as_user docker volume inspect "$volume" --format '{{.Name}}' >/dev/null || fail "The existing database volume was not found."
container_ids=$(as_user docker ps -aq --filter "label=com.docker.compose.project=$project" --filter 'label=com.docker.compose.service=web')
[[ -n "$container_ids" && "$container_ids" != *$'\n'* ]] || fail "Expected exactly one existing web container for $project."
as_user docker inspect "$container_ids" | python3 -c '
import json, sys
containers = json.load(sys.stdin)
matches = [m for m in containers[0]["Mounts"] if m["Destination"] == "/data"]
if len(matches) != 1 or matches[0]["Type"] != "volume" or matches[0].get("Name") != sys.argv[1]:
    raise SystemExit("Existing web /data mount does not match the configured volume.")
' "$volume" || fail "The existing web volume does not match; no containers were changed."

if ((enable_timer)); then
  as_user python3 "$repo_dir/scripts/deploy.py" --config "$temp_dir/config.json" check-ci \
    || fail "Latest master CI is not successful. Wait for ci.yml or use --no-enable to stage the installation."
fi

# Never interrupt an in-progress deployment while replacing its installed tool.
timer_was_active=0
if "${systemctl_command[@]}" is-active --quiet gp11-deploy.timer; then
  timer_was_active=1
  "${systemctl_command[@]}" stop gp11-deploy.timer
fi
service_state=$("${systemctl_command[@]}" show gp11-deploy.service --property=ActiveState --value 2>/dev/null || true)
case "$service_state" in
  ''|inactive|failed) ;;
  *)
    if ((timer_was_active)); then "${systemctl_command[@]}" start gp11-deploy.timer; fi
    fail "A deployment is in progress. Retry after gp11-deploy.service finishes."
    ;;
esac

install -d -m 0755 "${runtime_owner[@]}" -- "$install_dir" "$unit_dir"
install -d -m 0700 "${config_owner[@]}" -- "$config_dir" "$state_dir"
install -m 0644 "${runtime_owner[@]}" -- "$repo_dir/scripts/deploy.py" "$install_dir/deploy.py"
install -m 0600 "${config_owner[@]}" -- "$temp_dir/config.json" "$config_dir/config.json"

python3 - "$repo_dir/deploy/gp11-deploy.service" "$temp_dir/gp11-deploy.service" "$deploy_user" "$deploy_group" "$install_dir" "$config_dir" "$user_service" <<'PY'
import pathlib
import sys

source, destination, user, group, install_dir, config_dir, user_service = sys.argv[1:]

def unit_escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")

text = pathlib.Path(source).read_text()
for key, value in {"USER": user, "GROUP": group, "INSTALL_DIR": install_dir, "CONFIG_DIR": config_dir}.items():
    text = text.replace("@" + key + "@", unit_escape(value))
if user_service == "1":
    # User managers cannot order against system Docker/network units or select
    # another service user. Failed Docker/network checks retry on the next tick.
    text = "\n".join(line for line in text.splitlines()
                     if not line.startswith(("Wants=", "After=", "Requires=", "User=", "Group="))) + "\n"
pathlib.Path(destination).write_text(text)
PY
install -m 0644 "${runtime_owner[@]}" -- "$temp_dir/gp11-deploy.service" "$unit_dir/gp11-deploy.service"
install -m 0644 "${runtime_owner[@]}" -- "$repo_dir/deploy/gp11-deploy.timer" "$unit_dir/gp11-deploy.timer"
"${systemctl_command[@]}" daemon-reload
if ((enable_timer)); then
  "${systemctl_command[@]}" enable --now gp11-deploy.timer
  printf 'Installed and enabled gp11-deploy.timer for %s.\n' "$deploy_user"
else
  "${systemctl_command[@]}" disable --now gp11-deploy.timer
  printf 'Installed for %s; gp11-deploy.timer is disabled (--no-enable).\n' "$deploy_user"
fi
printf 'Configuration: %s/config.json\n' "$config_dir"
if ((user_service)); then
  printf 'Logs: journalctl --user -u gp11-deploy.service\n'
else
  printf 'Logs: journalctl -u gp11-deploy.service\n'
fi
