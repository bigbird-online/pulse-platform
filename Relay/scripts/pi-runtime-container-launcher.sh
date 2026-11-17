#!/bin/bash
set -euo pipefail

engine=''
engine_home=''
image=''
container_user=''
network=''
workspace=''
session_dir=''
isolation=''
expected_version=''
expected_commit=''
declare -a env_keys=()

die() {
  printf 'pi-runtime-container-launcher: %s\n' "$1" >&2
  exit 64
}

while (($#)); do
  case "$1" in
    --engine) (($# >= 2)) || die 'missing --engine value'; engine=$2; shift 2 ;;
    --engine-home) (($# >= 2)) || die 'missing --engine-home value'; engine_home=$2; shift 2 ;;
    --image) (($# >= 2)) || die 'missing --image value'; image=$2; shift 2 ;;
    --user) (($# >= 2)) || die 'missing --user value'; container_user=$2; shift 2 ;;
    --network) (($# >= 2)) || die 'missing --network value'; network=$2; shift 2 ;;
    --workspace) (($# >= 2)) || die 'missing --workspace value'; workspace=$2; shift 2 ;;
    --session-dir) (($# >= 2)) || die 'missing --session-dir value'; session_dir=$2; shift 2 ;;
    --isolation) (($# >= 2)) || die 'missing --isolation value'; isolation=$2; shift 2 ;;
    --expected-pi-version) (($# >= 2)) || die 'missing version value'; expected_version=$2; shift 2 ;;
    --expected-pi-commit) (($# >= 2)) || die 'missing commit value'; expected_commit=$2; shift 2 ;;
    --env-key) (($# >= 2)) || die 'missing env key'; env_keys+=("$2"); shift 2 ;;
    --) shift; break ;;
    *) die "unknown launcher argument: $1" ;;
  esac
done

[[ $isolation == container ]] || die 'this launcher only accepts isolation=container'
[[ $engine == /* && -x $engine ]] || die 'container engine must be an executable absolute path'
[[ $engine_home == /* && -d $engine_home ]] || die 'container engine home must be an existing absolute directory'
[[ $(basename "$engine") == docker || $(basename "$engine") == podman ]] || die 'only docker or podman is accepted'
[[ $image == *@sha256:* ]] || die 'container image must be digest-pinned'
[[ $container_user =~ ^[1-9][0-9]*:[1-9][0-9]*$ ]] || die 'container user must be a non-root numeric uid:gid'
[[ $network == bridge || $network == none ]] || die 'network must be explicitly bridge or none'
[[ -d $workspace && $workspace == /* ]] || die 'workspace must be an absolute directory'
[[ -d $session_dir && $session_dir == /* ]] || die 'session directory must be an absolute directory'
[[ $expected_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'invalid expected Pi version'
[[ $expected_commit =~ ^[0-9a-f]{40}$ ]] || die 'invalid expected Pi commit'
(($# >= 1)) || die 'missing Pi command after --'

# Relay gives Pi a tenant-scoped HOME. The host container engine also consults
# HOME for its image/config store, so keep those two homes explicitly separate.
container_home=${HOME-}
[[ $container_home == /* ]] || die 'container HOME must be absolute'
export HOME=$engine_home

revision=$($engine image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")
[[ $revision == "$expected_commit" ]] || die 'container image revision does not match the pinned Pi commit'

declare -a run_args=(
  run --rm --init --interactive
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges
  --pids-limit 256
  --memory 4g
  --cpus 2
  --network "$network"
  --user "$container_user"
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m
  --volume "$workspace:$workspace:rw"
  --volume "$session_dir:$session_dir:rw"
  --workdir "$workspace"
  --env "HOME=$container_home"
)
if [[ $(basename "$engine") == podman ]]; then
  # Rootless Podman otherwise maps a non-root container UID to a subordinate
  # host UID, which cannot use the Relay-owned workspace/session bind mounts.
  # keep-id preserves the caller UID/GID while all other isolation remains on.
  run_args+=(--userns keep-id)
fi

for key in "${env_keys[@]}" PULSE_PI_TENANT_ID PI_CODING_AGENT_DIR SCL_SESSION_REF; do
  [[ $key =~ ^[A-Z][A-Z0-9_]*$ ]] || die 'invalid environment key'
  run_args+=(--env "$key")
done

declare -a pi_args=("$@")
for ((i = 0; i < ${#pi_args[@]}; i++)); do
  case "${pi_args[$i]}" in
    --extension|--skill)
      ((i + 1 < ${#pi_args[@]})) || die "missing path after ${pi_args[$i]}"
      resource=${pi_args[$((i + 1))]}
      [[ $resource == /* && -e $resource ]] || die 'reviewed runtime resource must be an existing absolute path'
      [[ $resource == "$workspace"/* ]] || die 'reviewed runtime resource must stay inside the tenant workspace'
      run_args+=(--volume "$resource:$resource:ro")
      ((i += 1))
      ;;
  esac
done

pi_binary=${pi_args[0]}
[[ $pi_binary == /* ]] || die 'Pi binary inside the container must be absolute'
declare -a verify_args=(
  run --rm --init
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges
  --pids-limit 32
  --memory 256m
  --cpus 1
  --network none
  --user "$container_user"
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m
)
if [[ $(basename "$engine") == podman ]]; then
  verify_args+=(--userns keep-id)
fi
version_output=$($engine "${verify_args[@]}" "$image" "$pi_binary" --version 2>&1)
[[ $version_output =~ (^|[^0-9])$expected_version([^0-9]|$) ]] || die 'Pi binary version does not match the pin'

# The only execution path is the externally isolated container. There is no
# host-process fallback if image inspection, version verification, or run fails.
exec "$engine" "${run_args[@]}" "$image" "${pi_args[@]}"
