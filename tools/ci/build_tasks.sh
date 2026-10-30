#!/bin/bash
# Build the tasks of one Gitee CI group, as the public-actions ci-tasks step does.
#
#   build_tasks.sh <group> '<tasks json>'
#
# Env: CI_IMAGE, CI_TRIGGER_REPO, CI_PR_NUMBER. Artifacts go to
# <group>/<config>_<repo>-<pr>/. For an ntfc task, NTFC_TASK and NTFC_ARTIFACT
# are written to $GITHUB_ENV for the NTFC runtime step.

set -euo pipefail

group="$1"
tasks_json="$2"
workspace="$(pwd)"

jq -c '.[]' <<< "$tasks_json" | while read -r t; do
  config=$(jq -r .config <<< "$t")
  build_system=$(jq -r .build_system <<< "$t")
  test_kind=$(jq -r .test_kind <<< "$t")
  echo "=== Task: $config ($build_system, test: $test_kind) ==="

  rm -rf output
  cmake_flag=""
  out_dir="nuttx"
  if [ "$build_system" = cmake ]; then
    cmake_flag="--cmake"
    out_dir="cmake_out/*"
  fi
  target="$config"
  vela_config=false
  case "$config" in
    *:*|*vendor/*) ;;
    *) target="vendor/openvela/boards/vela/configs/$config"; vela_config=true ;;
  esac
  build_cmd="./build.sh $target $cmake_flag -e -Werror -j8"
  echo "$build_cmd"

  docker run --rm -v "$workspace":/workspace -w /workspace "$CI_IMAGE" \
    /bin/bash -c "df -h && $build_cmd && ls -atl nuttx"

  artifact="${group}/${config//:/_}_${CI_TRIGGER_REPO}-${CI_PR_NUMBER}"
  mkdir -p "$artifact"
  # out_dir may be the glob cmake_out/*.
  # shellcheck disable=SC2086
  cp $out_dir/nuttx $out_dir/.config "$artifact"
  if $vela_config; then
    # Optional: only full-image vela configs produce vela_*.bin.
    # shellcheck disable=SC2086
    cp $out_dir/vela_*.bin "$artifact" 2>/dev/null || true
  fi

  echo "=== Cleaning up after $config ==="
  sudo ~/bin/repo forall -v -c 'git clean -dfx; git reset --hard HEAD' > /dev/null 2>&1 || true
  sudo rm -rf cmake_out

  if [ "$test_kind" = ntfc ]; then
    echo "NTFC_TASK=$config" >> "$GITHUB_ENV"
    echo "NTFC_ARTIFACT=$artifact" >> "$GITHUB_ENV"
  fi
done
