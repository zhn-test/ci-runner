#!/bin/bash
# log_pusher.sh - 后台运行，定期把增量日志推送到 Jenkins
# Security: AUTH_TOKEN via env var, HTTPS recommended

set -u

LOG_FILE="${LOG_FILE:-/tmp/ci.log}"
JENKINS_LOG_URL="${JENKINS_LOG_URL:-}"
AUTH_TOKEN="${AUTH_TOKEN:-}"
PUSH_INTERVAL="${PUSH_INTERVAL:-10}"

if [ -z "$JENKINS_LOG_URL" ]; then
    echo "[log_pusher] JENKINS_LOG_URL not set, exiting"
    exit 0  # Not an error, log pushing is optional
fi

last_offset=0

push_chunk() {
    local chunk_file=$1
    local headers=(-H "Content-Type: text/plain")
    [ -n "$AUTH_TOKEN" ] && headers+=(-H "X-Auth-Token: $AUTH_TOKEN")
    curl -s -X POST "${headers[@]}" --data-binary @"$chunk_file" "$JENKINS_LOG_URL" > /dev/null 2>&1 || true
}

while true; do
    sleep "$PUSH_INTERVAL"

    [ -f "$LOG_FILE" ] || continue

    current_size=$(stat -c%s "$LOG_FILE" 2>/dev/null || stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$current_size" -gt "$last_offset" ]; then
        tail -c +$((last_offset + 1)) "$LOG_FILE" | head -c $((current_size - last_offset)) > /tmp/log_chunk.txt
        chunk_size=$(stat -c%s /tmp/log_chunk.txt 2>/dev/null || stat -f%z /tmp/log_chunk.txt 2>/dev/null)

        if [ "$chunk_size" -gt 0 ]; then
            push_chunk /tmp/log_chunk.txt
        fi

        last_offset=$current_size
    fi

    # Check if parent process (CI script) is still running
    if ! kill -0 "$PPID" 2>/dev/null; then
        # Final push
        if [ -f "$LOG_FILE" ]; then
            current_size=$(stat -c%s "$LOG_FILE" 2>/dev/null || stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
            if [ "$current_size" -gt "$last_offset" ]; then
                tail -c +$((last_offset + 1)) "$LOG_FILE" > /tmp/log_chunk.txt
                push_chunk /tmp/log_chunk.txt
            fi
        fi
        break
    fi
done
