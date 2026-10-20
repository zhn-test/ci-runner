#!/usr/bin/env bash
# Flag-driven task executor for the Gitee CI runner.
#
# Behaviour comes from the task string, never from the group name:
#
#   <name>                     Make build
#   <name>@cmake               CMake build
#   <name>@ntfc                Make build, then the NTFC runtime suite
#   <name>@cmake@ntfc          CMake build, then the NTFC runtime suite
#   <name> ending in -citest   additionally runs the OpenVela autotest suite
#
# So a task added to or removed from open-vela/public-actions is picked up with
# no change here. An unknown flag is rejected by tools/ci/ci_matrix.py before we
# get this far, so a flag we do not implement can never degrade into a plain
# build.
#
# Subcommands (each is a separate workflow step so it can be bounded and
# conditioned on its own):
#   build   <group> <task>...   build every task, save artifacts, record NTFC work
#   ntfc    <group>             run the NTFC suite for the recorded tasks
#   collect <group>             recover NTFC diagnostics on any outcome
#
# Pins (image, build flags, NTFC versions) are passed in by the caller after
# being read out of the upstream workflow; this script has no defaults for them
# on purpose.

set -euo pipefail

WORKSPACE="${CI_WORKSPACE:-$(pwd)}"
GROUP=""
QUIRKS_FILE="${CI_QUIRKS_FILE:-$(dirname "${BASH_SOURCE[0]}")/quirks.conf}"
REPO_BIN="${CI_REPO_BIN:-$HOME/bin/repo}"
JOBS="${CI_JOBS:-$(nproc 2>/dev/null || echo 4)}"

log()  { printf '\033[0;32m[exec]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[exec]\033[0m %s\n' "$*"; }
die()  { printf '::error::%s\n' "$*" >&2; exit 1; }

require_env() {
  local missing=()
  for name in "$@"; do
    [ -n "${!name:-}" ] || missing+=("$name")
  done
  [ ${#missing[@]} -eq 0 ] || die "missing required environment: ${missing[*]}"
}

# State shared between the build step and the NTFC step. GITHUB_ENV only
# carries scalars and would keep just the last task, so use a file.
ntfc_manifest() { printf '%s/.ci-ntfc-tasks' "$WORKSPACE"; }

# ---------------------------------------------------------------------------
# Task parsing
# ---------------------------------------------------------------------------
TASK_NAME=""
TASK_FLAGS=""

parse_task() {
  local raw="$1"
  TASK_NAME="${raw%%@*}"
  if [[ "$raw" == *@* ]]; then
    TASK_FLAGS="${raw#*@}"
  else
    TASK_FLAGS=""
  fi
  [ -n "$TASK_NAME" ] || die "task '$raw' has no configuration name"
}

# Sentinel match so "ntfc" never matches "ntfc2" or "myntfc".
has_flag() {
  [[ "@${TASK_FLAGS}@" == *"@$1@"* ]]
}

# ---------------------------------------------------------------------------
# Quirks: behaviour upstream gates on a group name rather than a flag.
# Recorded per task in quirks.conf; ci_matrix.py fails the run if upstream adds
# a group-gated step whose tasks are not listed there.
# ---------------------------------------------------------------------------
quirk_for() {
  local task="$1"
  [ -f "$QUIRKS_FILE" ] || return 0
  awk -F'|' -v t="$task" '
    { sub(/#.*/, "") }
    {
      gsub(/^[ \t]+|[ \t]+$/, "", $1)
      gsub(/^[ \t]+|[ \t]+$/, "", $2)
    }
    $1 == t { print $2; exit }
  ' "$QUIRKS_FILE"
}

apply_quirk() {
  local task="$1" quirk
  quirk="$(quirk_for "$task")"
  [ -n "$quirk" ] || return 0
  case "$quirk" in
    riscv_xpack_toolchain) quirk_riscv_xpack ;;
    *) die "task '$task' declares unimplemented quirk '$quirk' in $QUIRKS_FILE" ;;
  esac
}

# Point the manifest's prebuilt riscv-none-elf GCC at the xPack toolchain the CI
# image ships. Idempotent so a group with several RISC-V tasks stays correct.
quirk_riscv_xpack() {
  local tc="prebuilts/gcc/linux-x86_64/riscv-none-elf"
  if [ -L "$WORKSPACE/$tc" ]; then
    log "quirk riscv_xpack_toolchain: already applied"
    return 0
  fi
  [ -d "$WORKSPACE/$tc" ] || die "quirk riscv_xpack_toolchain: $tc not found in the workspace"
  log "quirk riscv_xpack_toolchain: swapping in the image toolchain"
  docker run --rm -v "${WORKSPACE}":/workspace -w /workspace \
    "${CI_DOCKER_IMAGE}" \
    /bin/bash -c '
      set -e
      tc=/workspace/prebuilts/gcc/linux-x86_64/riscv-none-elf
      echo "=== image toolchain ==="
      riscv-none-elf-gcc --version | head -2
      echo "=== manifest toolchain ==="
      "$tc/bin/riscv-none-elf-gcc" --version | head -2
      mv "$tc" "$tc.manifest"
      ln -s /tools/riscv-none-elf-gcc "$tc"
      "$tc/bin/riscv-none-elf-gcc" --version | head -1 | grep -q "xPack"
    '
}

# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
artifact_dir_for() {
  local task="$1"
  printf '%s/%s_%s-%s' "$GROUP" "${task//:/_}" "${CI_TRIGGER_REPO}" "${CI_PR_NUMBER}"
}

cleanup_tree() {
  log "cleaning the source tree"
  sudo "$REPO_BIN" forall -v -c 'git clean -dfx; git reset --hard HEAD' >/dev/null 2>&1 || true
  sudo rm -rf "$WORKSPACE/cmake_out" || true
}

run_autotest() {
  local task="$1" artifact="$2"
  local standard_config="${task%-citest*}"
  local test_model="${standard_config//-/_}"
  log "autotest: $task (model $test_model)"
  docker run --rm \
    -v "${WORKSPACE}":/workspace \
    -w /workspace \
    "${CI_DOCKER_IMAGE}" \
    /bin/bash -c "free -m && df -h && cp $artifact/* nuttx/ && ls -atl nuttx && \
      cd tests/scripts/script && \
      pytest -m \"common or $test_model\" ./ -B $standard_config -L /workspace \
        -P /workspace -F /tmp -R qemu -v --disable-warnings --count=1 \
        --json=/workspace/autotest.json --maxfail=10"

  cd "$WORKSPACE"
  mkdir -p output
  cp ./*.log ./autotest.json "$artifact" 2>/dev/null || true
  local failed
  failed="$(jq '.report.summary.failed' autotest.json)"
  if [ "$failed" != "null" ] && [ "$failed" != "0" ]; then
    die "autotest failed: $failed"
  fi
  log "autotest passed: $task"
}

cmd_build() {
  require_env CI_DOCKER_IMAGE CI_BUILD_FLAGS CI_TRIGGER_REPO
  : "${CI_PR_NUMBER:=0}"
  local tasks=("$@")
  [ ${#tasks[@]} -gt 0 ] || die "build: no tasks given for group '$GROUP'"

  : > "$(ntfc_manifest)"
  cd "$WORKSPACE"

  for raw in "${tasks[@]}"; do
    parse_task "$raw"
    local task="$TASK_NAME"
    local cmake_flag="" cp_base_dir="nuttx"
    if has_flag cmake; then
      cmake_flag="--cmake"
      cp_base_dir="cmake_out/*"
    fi

    log "=== task $raw -> name=$task flags=${TASK_FLAGS:--} ==="
    rm -rf output || true
    apply_quirk "$task"

    local build_config="$task"
    case "$task" in
      *:*|*vendor/*|./*) ;;
      *) build_config="vendor/openvela/boards/vela/configs/$task" ;;
    esac
    local build_cmd="./build.sh $build_config $cmake_flag ${CI_BUILD_FLAGS} -j${JOBS}"
    log "$build_cmd"

    docker run --rm \
      -v "${WORKSPACE}":/workspace \
      -w /workspace \
      "${CI_DOCKER_IMAGE}" \
      /bin/bash -c "pwd && df -h && $build_cmd && ls -atl nuttx"

    local artifact
    artifact="$(artifact_dir_for "$task")"
    log "saving artifacts to $artifact"
    mkdir -p "$artifact"
    # Unquoted on purpose: cp_base_dir may be the glob cmake_out/*.
    cp ${cp_base_dir}/nuttx ${cp_base_dir}/.config "$artifact"
    # Only full-image configs produce vela_*.bin.
    cp ${cp_base_dir}/vela_*.bin "$artifact" 2>/dev/null || true

    cleanup_tree

    if [[ "$task" == *-citest ]]; then
      run_autotest "$task" "$artifact"
    fi

    if has_flag ntfc; then
      log "recording $task for the NTFC step"
      printf '%s\t%s\n' "$task" "$artifact" >> "$(ntfc_manifest)"
    fi
  done

  log "build step done for group '$GROUP'"
}

# ---------------------------------------------------------------------------
# ntfc
# ---------------------------------------------------------------------------
cmd_ntfc() {
  require_env CI_DOCKER_IMAGE CI_NTFC_SPEC CI_PYTEST_SPEC CI_NTFC_TESTING_REPO \
    CI_NTFC_TESTING_REF NTFC_CONTAINER
  local manifest
  manifest="$(ntfc_manifest)"
  [ -s "$manifest" ] || die "group '$GROUP' is flagged ntfc but no task recorded any work"
  cd "$WORKSPACE"

  # Fresh list so a re-run does not inherit container names from a past attempt.
  : > "${WORKSPACE}/.ci-ntfc-containers"
  local index=0
  while IFS=$'\t' read -r task artifact; do
    [ -n "$task" ] || continue
    index=$((index + 1))
    log "=== NTFC runtime test: $task ==="

    local board="${task%%:*}" config="${task##*:}"
    local cfgdir
    cfgdir="$(ls -d nuttx/boards/*/*/"${board}"/configs/"${config}" 2>/dev/null | head -1 || true)"
    if [ -z "$cfgdir" ] || [ ! -f "$cfgdir/config.yaml" ] || [ ! -x "$cfgdir/run.sh" ]; then
      die "$task is flagged ntfc but needs both config.yaml and an executable run.sh in its board config directory"
    fi
    log "NTFC metadata: $cfgdir"

    # One container name per task so the collector can stop whichever is live.
    local container="${NTFC_CONTAINER}-${index}"
    printf '%s\n' "$container" >> "${WORKSPACE}/.ci-ntfc-containers"

    # ntfc and pytest go into a throwaway virtualenv so the image's own pinned
    # pytest is untouched. NTFCDIR stays outside the uploaded group directory.
    # Build output is restored into both nuttx/ and nuttx/build/ because the
    # Make-built boards' config.yaml reads ./.config and ./nuttx while the
    # CMake-built ones read ./build/.config and ./build/nuttx.
    docker run --name "$container" \
      -v "${WORKSPACE}":/workspace \
      -w /workspace \
      -e NTFCDIR=/workspace/.ntfc \
      -e CURRENTCONFDIR="/workspace/${cfgdir}" \
      -e ARTIFACTCONFDIR="/workspace/${artifact}" \
      "${CI_DOCKER_IMAGE}" \
      /bin/bash -c "set -e && python3 --version && \
        python3 -m venv /workspace/.ntfc-venv && \
        /workspace/.ntfc-venv/bin/pip install --quiet \
          \"${CI_NTFC_SPEC}\" \"${CI_PYTEST_SPEC}\" && \
        export PATH=/workspace/.ntfc-venv/bin:\$PATH && ntfc --help > /dev/null && \
        rm -rf /workspace/.ntfc && mkdir -p /workspace/.ntfc/external && \
        git clone --depth=1 --branch ${CI_NTFC_TESTING_REF} \
          ${CI_NTFC_TESTING_REPO} \
          /workspace/.ntfc/external/nuttx-testing && \
        cp -a ${artifact}/. nuttx/ && \
        mkdir -p nuttx/build && cp -a ${artifact}/. nuttx/build/ && \
        ls -atl nuttx/nuttx nuttx/.config && \
        bash /workspace/${cfgdir}/run.sh"

    log "NTFC runtime test passed: $task"
  done < "$manifest"
}

# ---------------------------------------------------------------------------
# collect
#
# run.sh moves its results only after ntfc test returns, so a timeout leaves
# them in nuttx/, outside the uploaded directory. Runs on any outcome so the
# named containers are always removed.
# ---------------------------------------------------------------------------
cmd_collect() {
  local list="${WORKSPACE}/.ci-ntfc-containers"
  if [ -f "$list" ]; then
    while read -r container; do
      [ -n "$container" ] || continue
      docker rm -f "$container" >/dev/null 2>&1 || true
    done < "$list"
  fi

  cd "$WORKSPACE"
  local manifest
  manifest="$(ntfc_manifest)"
  [ -s "$manifest" ] || { log "collect: nothing recorded, nothing to do"; return 0; }

  local collected=0
  while IFS=$'\t' read -r task artifact; do
    [ -n "$artifact" ] || continue
    local dest="${artifact}/ntfc"
    mkdir -p "$dest"
    if [ -d nuttx/result ]; then sudo mv nuttx/result "$dest/" || true; fi
    if [ -f nuttx/pytest.debug.log ]; then sudo mv nuttx/pytest.debug.log "$dest/" || true; fi
    sudo chown -R "$(id -u):$(id -g)" "$dest" || true
    local n
    n="$(find "$dest" -type f 2>/dev/null | wc -l | tr -d ' ')"
    collected=$((collected + n))
  done < "$manifest"
  log "collect: $collected file(s) under the group directory"
}

# ---------------------------------------------------------------------------
main() {
  local cmd="${1:-}"
  shift || true
  GROUP="${1:-}"
  shift || true
  [ -n "$cmd" ] || die "usage: task_exec.sh {build|ntfc|collect} <group> [task...]"
  [ -n "$GROUP" ] || die "$cmd: missing group name"

  case "$cmd" in
    build)   cmd_build "$@" ;;
    ntfc)    cmd_ntfc ;;
    collect) cmd_collect ;;
    *)       die "unknown subcommand '$cmd'" ;;
  esac
}

main "$@"
