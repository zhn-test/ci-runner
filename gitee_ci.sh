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

# --- Fetch build configs from public-actions ---
fetch_build_configs() {
    local branch=$1
    local ci_file_url=""
    local configs=()

    # Determine which CI file to fetch based on branch
    if [ "$branch" = "dev" ]; then
        ci_file_url="https://raw.githubusercontent.com/open-vela/public-actions/dev/.github/workflows/ci-real.yml"
    else
        # trunk and other branches use trunk's ci.yml
        ci_file_url="https://raw.githubusercontent.com/open-vela/public-actions/trunk/.github/workflows/ci.yml"
    fi

    log "INFO" "Fetching build configs from: $ci_file_url"
    local ci_content
    ci_content=$(curl -s "$ci_file_url" 2>/dev/null || echo "")

    if [ -z "$ci_content" ]; then
        log "ERROR" "Failed to fetch CI config from public-actions"
        return 1
    fi

    # Parse tasks from YAML matrix using python
    python3 -c "
import sys, re, yaml

content = '''$ci_content'''

# Simple extraction: find all task entries
# Match lines like '- task_name@cmake' or '- vendor/path/config@cmake'
tasks = []
in_tasks = False
for line in content.split('\n'):
    stripped = line.strip()
    if 'tasks:' in stripped or 'tasks: [' in stripped:
        in_tasks = True
        # Handle inline array format: tasks: [a, b, c]
        m = re.search(r'tasks:\s*\[([^\]]+)\]', stripped)
        if m:
            for t in m.group(1).split(','):
                t = t.strip().strip('\"').strip(\"'\")
                if t:
                    tasks.append(t)
            in_tasks = False
        continue
    if in_tasks:
        if stripped.startswith('- '):
            task = stripped[2:].strip().strip('\"').strip(\"'\")
            if task and not task.startswith('#'):
                tasks.append(task)
        elif stripped and not stripped.startswith('#') and not stripped.startswith('-'):
            in_tasks = False

# Deduplicate while preserving order
seen = set()
for t in tasks:
    if t not in seen:
        seen.add(t)
        print(t)
" 2>/dev/null
}

# --- Build ---
do_build() {
    local task=$1
    local ncpus=$(nproc)
    local cmake_flag=""
    local config="$task"

    # Parse @cmake suffix
    if [[ "$task" == *@cmake ]]; then
        cmake_flag="--cmake"
        config="${task%@cmake}"
    fi

    # Add vendor prefix if needed (same logic as public-actions)
    local build_config="$config"
    if [[ "$config" != *:* ]] && [[ "$config" != vendor/* ]] && [[ "$config" != ./* ]]; then
        build_config="vendor/openvela/boards/vela/configs/$config"
    fi

    local build_cmd="./build.sh $build_config $cmake_flag -e -Werror -j$ncpus"
    log "INFO" "Building: $build_cmd"

    if ! $build_cmd; then
        log "ERROR" "Build failed: $task"
        return 1
    fi
    log "INFO" "Build passed: $task"

    # Clean up after build (same as public-actions)
    repo forall -c 'git clean -dfx; git reset --hard HEAD' > /dev/null 2>&1 || true
    rm -rf cmake_out || true
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

# --- Fetch build configs dynamically from public-actions ---
log "INFO" "Fetching build configs for branch: $PR_BRANCH"
BUILD_TASKS=$(fetch_build_configs "$PR_BRANCH")

if [ -z "$BUILD_TASKS" ]; then
    log "ERROR" "No build tasks found for branch $PR_BRANCH"
    set_comment "ci" "failed"
    echo "CI_RESULT=FAILURE"
    exit 1
fi

log "INFO" "Build tasks:"
echo "$BUILD_TASKS" | while read task; do
    log "INFO" "  - $task"
done

# --- Build all configs ---
BUILD_FAILED=false
TOTAL=0
PASSED=0
FAILED_TASKS=""

while IFS= read -r task; do
    [ -z "$task" ] && continue
    TOTAL=$((TOTAL + 1))
    log "INFO" "=== Building [$TOTAL]: $task ==="
    if do_build "$task"; then
        PASSED=$((PASSED + 1))
    else
        BUILD_FAILED=true
        FAILED_TASKS="$FAILED_TASKS $task"
        # Continue building other configs even if one fails
    fi
done <<< "$BUILD_TASKS"

log "INFO" "Build summary: $PASSED/$TOTAL passed"
if [ "$BUILD_FAILED" = true ]; then
    log "ERROR" "Failed tasks:$FAILED_TASKS"
    set_comment "ci" "failed"
    echo "CI_RESULT=FAILURE"
    exit 2
fi

# --- Success ---
set_comment "cict" "success"
log "INFO" "CI/CT completed successfully!"
echo "CI_RESULT=SUCCESS"
