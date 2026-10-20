#!/bin/bash
# gitee_ci.sh - CI/CT script for GitHub Actions, triggered by Gitee PR
# Build configs are dynamically fetched from open-vela/public-actions
# to stay in sync with GitHub PR CI.

set -euo pipefail

WORKSPACE=$(pwd)
GITEE_URL=https://gitee.com
GITEE_API_URL=${GITEE_API_URL:-https://gitee.com/api/v5}
BUILD_URL=${BUILD_URL:-""}
PR_BRANCH=${PR_BRANCH:-trunk}

# --- Logging ---
log() {
    local level=$1 message=$2
    case $level in
        INFO)    echo -e "\033[0;32m[INFO] $message\033[0m" ;;
        WARNING) echo -e "\033[0;33m[WARNING] $message\033[0m" ;;
        ERROR)   echo -e "\033[0;31m[ERROR] $message\033[0m" ;;
        DEBUG)   echo -e "\033[0;34m[DEBUG] $message\033[0m" ;;
    esac
}

# --- Gitee API helpers ---
gitee_api() {
    local method=$1 endpoint=$2
    shift 2
    curl -s -X "$method" \
        -H "Authorization: Bearer $GITEE_API_TOKEN" \
        -H "Content-Type: application/json" \
        "$@" \
        "$GITEE_API_URL/$endpoint"
}

reset_label() {
    for PR_INFO in $ALL_PR; do
        local repo_path=$(echo "$PR_INFO" | cut -d ':' -f 1 | xargs)
        local pr_num=$(echo "$PR_INFO" | cut -d ':' -f 2)
        local labels=$(gitee_api GET "repos/$repo_path/pulls/$pr_num/labels" | jq -r '.[].name // empty')
        for label in $labels; do
            if [[ $label == ci-* ]] || [[ $label == ct-* ]]; then
                gitee_api DELETE "repos/$repo_path/pulls/$pr_num/labels/$(python3 -c "import urllib.parse; print(urllib.parse.quote('$label', safe=''))")" > /dev/null
            fi
        done
    done
}

set_label() {
    local type=$1 status=$2 repo_path=$3 pr_num=$4
    if [ "$type" == "cict" ] && [ "$status" == "start" ]; then return; fi
    local label_data
    if [ "$type" == "cict" ]; then
        label_data=$(jq -nc --arg a "ci-$status" --arg b "ct-$status" '[$a, $b]')
    else
        label_data=$(jq -nc --arg a "$type-$status" '[$a]')
    fi
    gitee_api POST "repos/$repo_path/pulls/$pr_num/labels" -d "$label_data" > /dev/null
}

set_comment() {
    local type=$1 status=$2

    if [ "${SKIP_GITEE_NOTIFY:-false}" = "true" ]; then
        log "INFO" "[$type] $status (notification skipped - isolated mode)"
        return
    fi
    if [ -z "${GITEE_API_TOKEN:-}" ]; then
        log "WARNING" "GITEE_API_TOKEN not set, skipping notification"
        return
    fi

    local desc
    case $status in
        success) desc="执行成功！" ;;
        failed)  desc="执行失败！" ;;
        start)   desc="已触发" ;;
    esac

    for PR_INFO in $ALL_PR; do
        local repo_path=$(echo "$PR_INFO" | cut -d ':' -f 1 | xargs)
        local pr_num=$(echo "$PR_INFO" | cut -d ':' -f 2)
        local body
        body=$(jq -nc --arg b "$type $desc，查看详情: $BUILD_URL" '{"body": $b}')
        gitee_api POST "repos/$repo_path/pulls/$pr_num/comments" -d "$body" > /dev/null
        set_label "$type" "$status" "$repo_path" "$pr_num"
    done
}

# --- Workspace management ---
do_clean_workspace() {
    repo forall -c '
        if [ -f ".git/CHERRY_PICK_HEAD" ]; then
            git cherry-pick --abort 2>/dev/null || true
        fi
    ' 2>/dev/null || true
    repo forall -c 'git clean -dfx; rm -rf .git/{MERGE_HEAD,MERGE_MSG,REVERT_HEAD,CHERRY_PICK_HEAD,sequencer,rebase-apply,rebase-merge}; git reset --hard HEAD' > /dev/null 2>&1 || true
}

do_clean_pr_branch() {
    repo forall -c 'git branch -D pr-branch' > /dev/null 2>&1 || true
}

# --- Shared matrix + executor -------------------------------------------
# The task list and the pinned tool versions are read out of the GitHub CI
# workflow by tools/ci/ci_matrix.py, and every task is run by
# tools/ci/task_exec.sh. Both are shared with .github/workflows/ci.yml so there
# is exactly one implementation of "what a task means".
CI_TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")/tools/ci" && pwd)"

# Which upstream workflow serves this PR's target branch is decided by the
# dispatcher every openvela PR goes through, so it is read rather than guessed:
# a new branch family upstream needs no change here.
DISPATCHER_URL="https://raw.githubusercontent.com/open-vela/public-actions/dev/.github/workflows/ci.yml"

# Downloads the upstream workflow for the PR's target branch into $1.
fetch_upstream_ci() {
    local dest=$1 branch=$2 disp url
    disp="$(dirname "$dest")/.dispatcher.yml"
    curl -fsSL "$DISPATCHER_URL" -o "$disp" || return 1
    if ! python3 "$CI_TOOLS/ci_matrix.py" --dispatcher "$disp" \
            --resolve-branch "$branch" --format route > "$disp.route"; then
        return 1
    fi
    cat "$disp.route" >&2
    # shellcheck disable=SC1090
    . "$disp.route"
    url="https://raw.githubusercontent.com/open-vela/public-actions/${CI_UPSTREAM_REF}/${CI_UPSTREAM_PATH}"
    log "INFO" "Upstream CI: $url (via ${CI_UPSTREAM_VIA})" >&2
    curl -fsSL "$url" -o "$dest"
}

# ============================================================
# MAIN
# ============================================================

CURRENT_PR="$GITEE_URL/$ORG_NAME/$REPO_NAME/pulls/$PR_ID"

# Parse depends-on from PR description, or use pre-parsed ALL_PR from env
if [ -n "${PR_DEPENDS_ON:-}" ]; then
    # ALL_PR already parsed by parse job (safe, validated)
    ALL_PR="$PR_DEPENDS_ON"
    log "INFO" "Using pre-parsed ALL_PR: $ALL_PR"
else
    PR_DESC_FILE="${PR_DESCRIPTION_FILE:-/tmp/pr_description.txt}"
    DEPENDS_ON=""
    if [ -f "$PR_DESC_FILE" ]; then
        DEPENDS_ON=$(grep -oP 'depends-on:\s*\[[^]]+\]' "$PR_DESC_FILE" 2>/dev/null || true)
    fi
    PR_LIST=$(echo "$DEPENDS_ON" | grep -oP '(?<=\[)[^]]+(?=\])' || true)
    PR_LIST="$CURRENT_PR $PR_LIST"
    PR_LIST=$(echo $PR_LIST | awk '{for(i=1;i<=NF;i++) if(!a[$i]++) printf "%s%s",$i,(i==NF?ORS:OFS)}')
    log "INFO" "PR_LIST: $PR_LIST"

    # Build ALL_PR pairs with strict validation
    ALL_PR=""
    for pr_url in $PR_LIST; do
        if echo "$pr_url" | grep -qP '^https://gitee\.com/[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+/pulls/[0-9]+$'; then
            repo_pr=$(echo "$pr_url" | sed -n 's#https://gitee.com/\([^/]\+/[^/]\+\)/pulls/\([0-9]\+\)#\1:\2#p')
            [ -n "$repo_pr" ] && ALL_PR="$ALL_PR $repo_pr"
        else
            log "WARNING" "Skipping invalid PR URL: $pr_url"
        fi
    done
    manifest_part=$(echo "$ALL_PR" | grep -o 'open-vela/manifests[^ ]*' || true)
    if [ -n "$manifest_part" ]; then
        without_manifest=$(echo "$ALL_PR" | sed "s|$manifest_part||" | xargs)
        ALL_PR="$manifest_part $without_manifest"
    fi
    ALL_PR=$(echo $ALL_PR | xargs)
fi
log "INFO" "ALL_PR: $ALL_PR"

# Notify start
if [ "${SKIP_GITEE_NOTIFY:-false}" != "true" ]; then
    set_comment "cict" "start"
    reset_label
fi

# --- Repo init & sync ---
do_clean_workspace
do_clean_pr_branch

log "INFO" "repo init: $MANIFEST_URL branch: ${PR_BRANCH}"
repo init -u "$MANIFEST_URL" -b "${PR_BRANCH:-trunk}" -m openvela.xml --depth=1 --git-lfs
if [ $? -ne 0 ]; then
    log "ERROR" "repo init failed"
    set_comment "ci" "failed"
    echo "CI_RESULT=FAILURE"
    exit 1
fi

repo sync -c -d --no-tags --force-sync -j$(nproc)
if [ $? -ne 0 ]; then
    log "ERROR" "repo sync failed"
    set_comment "ci" "failed"
    echo "CI_RESULT=FAILURE"
    exit 1
fi

# --- Cherry-pick PR branches ---
for PR_INFO in $ALL_PR; do
    cd "$WORKSPACE"
    REPO_FULL_PATH=$(echo "$PR_INFO" | cut -d ':' -f 1 | xargs)
    PR_NUMBER=$(echo "$PR_INFO" | cut -d ':' -f 2)
    PROJECT_NAME=$(echo "$REPO_FULL_PATH" | cut -d '/' -f 2)

    # Skip invalid/placeholder PRs
    if [ "$PR_NUMBER" = "0" ] || [ "$REPO_FULL_PATH" = "none/none" ] || [ -z "$PR_NUMBER" ]; then
        log "INFO" "Skipping invalid PR: $PR_INFO"
        continue
    fi

    if [ "$PROJECT_NAME" == "manifests" ]; then
        cd .repo/manifests
        git config user.email "openvela-robot@xiaomi.com"
        git config user.name "openvela-robot"
        git fetch origin "pull/$PR_NUMBER/head:pr-branch"
        if ! git cherry-pick $(git rev-list --reverse HEAD..pr-branch); then
            log "ERROR" "Cherry-pick failed for manifests PR#$PR_NUMBER"
            set_comment "ci" "failed"
            echo "CI_RESULT=FAILURE"
            exit 1
        fi
        cd "$WORKSPACE"
        repo sync -c -d --no-tags --force-sync -j$(nproc)
        if [ $? -ne 0 ]; then
            log "ERROR" "repo sync after manifest change failed"
            set_comment "ci" "failed"
            echo "CI_RESULT=FAILURE"
            exit 1
        fi
    else
        REPO_PATH=$(grep "\"$PROJECT_NAME\"" .repo/manifests/openvela.xml | awk -F'"' '{print $2}')
        if [ -z "$REPO_PATH" ]; then
            log "WARNING" "Could not find path for project $PROJECT_NAME, skipping"
            continue
        fi
        cd "$REPO_PATH"
        git config user.email "openvela-robot@xiaomi.com"
        git config user.name "openvela-robot"
        git fetch "https://gitee.com/$REPO_FULL_PATH.git" "pull/$PR_NUMBER/head:pr-branch"
        if ! git cherry-pick $(git rev-list --reverse HEAD..pr-branch); then
            log "ERROR" "Cherry-pick failed for $REPO_FULL_PATH PR#$PR_NUMBER"
            set_comment "ci" "failed"
            echo "CI_RESULT=FAILURE"
            exit 1
        fi
    fi
    cd "$WORKSPACE"
done

# --- Resolve the task matrix from the GitHub CI workflow -----------------
log "INFO" "Resolving the task matrix for branch: $PR_BRANCH"
UPSTREAM_CI="$WORKSPACE/.upstream-ci.yml"
if ! fetch_upstream_ci "$UPSTREAM_CI" "$PR_BRANCH"; then
    log "ERROR" "Cannot fetch the upstream CI workflow for $PR_BRANCH"
    set_comment "ci" "failed"
    echo "CI_RESULT=FAILURE"
    exit 1
fi

# Any drift the executor cannot reproduce faithfully -- an unknown task flag, a
# missing version pin, a step upstream gates on a group name -- fails here
# rather than silently building something different from GitHub CI.
if ! MATRIX_JSON=$(python3 "$CI_TOOLS/ci_matrix.py" --workflow "$UPSTREAM_CI" --format matrix); then
    log "ERROR" "Upstream CI drift; see the error above"
    set_comment "ci" "failed"
    echo "CI_RESULT=FAILURE"
    exit 1
fi
python3 "$CI_TOOLS/ci_matrix.py" --workflow "$UPSTREAM_CI" --format report

# Pins (image, build flags, NTFC versions) come from the same file.
while IFS= read -r line; do
    [ -n "$line" ] || continue
    export "${line?}"
done < <(python3 "$CI_TOOLS/ci_matrix.py" --workflow "$UPSTREAM_CI" --format env)

export CI_WORKSPACE="$WORKSPACE"
export CI_TRIGGER_REPO="${REPO_NAME}"
export CI_PR_NUMBER="${PR_ID}"
export CI_QUIRKS_FILE="$CI_TOOLS/quirks.conf"

# --- Run every group through the shared executor -------------------------
GROUP_COUNT=$(echo "$MATRIX_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["include"]))')
log "INFO" "Groups to run: $GROUP_COUNT"

FAILED_GROUPS=""
PASSED=0
for idx in $(seq 0 $((GROUP_COUNT - 1))); do
    group=$(echo "$MATRIX_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['include'][$idx]['group'])")
    tasks=$(echo "$MATRIX_JSON" | python3 -c "import json,sys; print(' '.join(json.load(sys.stdin)['include'][$idx]['tasks']))")
    needs_ntfc=$(echo "$MATRIX_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['include'][$idx]['ntfc'])")

    log "INFO" "=== group $group: $tasks ==="
    group_failed=false

    if ! bash "$CI_TOOLS/task_exec.sh" build "$group" $tasks; then
        group_failed=true
    fi

    if [ "$group_failed" = false ] && [ "$needs_ntfc" = "true" ]; then
        export NTFC_CONTAINER="ntfc-${REPO_NAME}-${PR_ID}-${group}"
        if ! bash "$CI_TOOLS/task_exec.sh" ntfc "$group"; then
            group_failed=true
        fi
        bash "$CI_TOOLS/task_exec.sh" collect "$group" || true
    fi

    if [ "$group_failed" = true ]; then
        log "ERROR" "group failed: $group"
        FAILED_GROUPS="$FAILED_GROUPS $group"
    else
        PASSED=$((PASSED + 1))
    fi
done

log "INFO" "Group summary: $PASSED/$GROUP_COUNT passed"
if [ -n "$FAILED_GROUPS" ]; then
    log "ERROR" "Failed groups:$FAILED_GROUPS"
    set_comment "ci" "failed"
    echo "CI_RESULT=FAILURE"
    exit 2
fi

# --- Success ---
set_comment "cict" "success"
log "INFO" "CI/CT completed successfully!"
echo "CI_RESULT=SUCCESS"
