#!/bin/bash
# gitee_ci.sh - CI/CT script for GitHub Actions, triggered by Gitee PR
# Security hardened version
#
# Required env vars (set by GitHub Actions workflow):
#   ORG_NAME, REPO_NAME, PR_ID, PR_BRANCH
#   PR_DESCRIPTION_FILE (path to file containing PR description)
#   MANIFEST_URL, REPO_HTTP_URL
#   GITEE_API_TOKEN, GITEE_API_URL
#   BUILD_URL (Jenkins URL for Gitee comment)

set -euo pipefail

WORKSPACE=$(pwd)
GITEE_URL=https://gitee.com
GITEE_API_URL=${GITEE_API_URL:-https://gitee.com/api/v5}
BUILD_URL=${BUILD_URL:-""}

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

# --- Gitee API helpers (all inputs JSON-escaped) ---
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

    # Skip if running in isolated mode (notify job handles this)
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
        # Use jq to safely construct JSON body (prevents injection)
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

# --- Build ---
do_build() {
    local config=$1 check_error=$2
    local ncpus=$(nproc)
    local werror=""
    [ "$check_error" -eq 1 ] && werror="-e -Werror"

    log "INFO" "Building: ./build.sh $config $werror -j$ncpus"
    if ! ./build.sh $config $werror -j$ncpus; then
        do_clean_workspace
        log "ERROR" "Build failed: $config"
        set_comment "ci" "failed"
        echo "CI_RESULT=FAILURE"
        exit 2
    fi
    log "INFO" "Build passed: $config"
}

# --- Test ---
do_test() {
    local test_config=$1 config_name=$2 type_name=$3
    cd "$WORKSPACE"
    log "INFO" "Testing: pytest -m '$test_config' in $config_name"
    cd tests/scripts/script
    if ! pytest -m "$test_config" ./ -B "$config_name" -L "$WORKSPACE" -P "$WORKSPACE" -F /tmp -R "$type_name" -v --disable-warnings --count=1 --json="$WORKSPACE/${config_name}_autotest.json"; then
        log "ERROR" "Test failed: $config_name"
        set_comment "ct" "failed"
        echo "CI_RESULT=FAILURE"
        exit 3
    fi
}

# ============================================================
# MAIN
# ============================================================

CURRENT_PR="$GITEE_URL/$ORG_NAME/$REPO_NAME/pulls/$PR_ID"

# Parse depends-on from PR description (read from file to avoid injection)
PR_DESC_FILE="${PR_DESCRIPTION_FILE:-/tmp/pr_description.txt}"
DEPENDS_ON=""
if [ -f "$PR_DESC_FILE" ]; then
    DEPENDS_ON=$(grep -oP 'depends-on:\s*\[[^]]+\]' "$PR_DESC_FILE" 2>/dev/null || true)
fi
PR_LIST=$(echo "$DEPENDS_ON" | grep -oP '(?<=\[)[^]]+(?=\])' || true)
PR_LIST="$CURRENT_PR $PR_LIST"
# Deduplicate
PR_LIST=$(echo $PR_LIST | awk '{for(i=1;i<=NF;i++) if(!a[$i]++) printf "%s%s",$i,(i==NF?ORS:OFS)}')
log "INFO" "PR_LIST: $PR_LIST"

# Build ALL_PR as "org/repo:pr_number" pairs (validate format)
ALL_PR=""
for pr_url in $PR_LIST; do
    # Strict validation: only accept gitee.com PR URLs
    if echo "$pr_url" | grep -qP '^https://gitee\.com/[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+/pulls/[0-9]+$'; then
        repo_pr=$(echo "$pr_url" | sed -n 's#https://gitee.com/\([^/]\+/[^/]\+\)/pulls/\([0-9]\+\)#\1:\2#p')
        [ -n "$repo_pr" ] && ALL_PR="$ALL_PR $repo_pr"
    else
        log "WARNING" "Skipping invalid PR URL: $pr_url"
    fi
done
# Ensure manifest PR is processed first
manifest_part=$(echo "$ALL_PR" | grep -o 'open-vela/manifests[^ ]*' || true)
if [ -n "$manifest_part" ]; then
    without_manifest=$(echo "$ALL_PR" | sed "s|$manifest_part||" | xargs)
    ALL_PR="$manifest_part $without_manifest"
fi
ALL_PR=$(echo $ALL_PR | xargs)
log "INFO" "ALL_PR: $ALL_PR"

# Notify start (skip if running in isolated mode - notify job handles this)
if [ "${SKIP_GITEE_NOTIFY:-false}" != "true" ]; then
    set_comment "cict" "start"
    reset_label
fi

# --- Repo init & sync (using Gitee manifest) ---
do_clean_workspace
do_clean_pr_branch

log "INFO" "repo init from Gitee manifest: $MANIFEST_URL branch: $PR_BRANCH"
repo init -u "$MANIFEST_URL" -b "$PR_BRANCH" -m openvela.xml --depth=1 --git-lfs
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

# --- Build ---
do_build "stm32h750b-dk:lvgl" 1
do_build "vendor/espressif/boards/esp32s3/esp32s3-box/configs/openvela" 1
do_build "vendor/espressif/boards/esp32s3/esp32s3-eye/configs/openvela" 1
do_build "vendor/openvela/boards/vela/configs/goldfish-arm64-v8a-ap" 1
do_build "vendor/openvela/boards/vela/configs/goldfish-armeabi-v7a-ap" 1
do_build "vendor/openvela/boards/vela/configs/goldfish-x86_64-ap" 1
do_build "vendor/openvela/boards/vela/configs/goldfish-armeabi-v7a-ap-citest" 0

# --- Test ---
do_test "common or goldfish_armeabi_v7a_ap" "goldfish-armeabi-v7a-ap" "qemu"

# --- Success ---
set_comment "cict" "success"
log "INFO" "CI/CT completed successfully!"
echo "CI_RESULT=SUCCESS"
