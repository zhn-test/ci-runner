#!/bin/bash
set -e
# Parse depends-on. Every entry must be an open-vela Gitee PR URL; one
# that cannot be used fails here rather than being silently skipped.
echo "$CLIENT_PAYLOAD" | jq -r '.pr_description // empty' > /tmp/pr_desc.txt
DEPENDS=$(grep -ioP 'depends-on:\s*\[[^]]+\]' /tmp/pr_desc.txt 2>/dev/null | grep -oP '(?<=\[)[^]]+(?=\])' || true)
if [ -z "$DEPENDS" ] && grep -qi 'depends-on' /tmp/pr_desc.txt; then
  echo "::error::Malformed depends-on; expected: depends-on: [https://gitee.com/open-vela/<repo>/pulls/<N>, ...]"
  exit 1
fi
ALL_PR="$ORG/$REPO:$PR_ID"
for url in ${DEPENDS//,/ }; do
  if [[ ! "$url" =~ ^https://gitee\.com/([A-Za-z0-9_-]+/[A-Za-z0-9_.-]+)/pulls/([0-9]+)$ ]]; then
    echo "::error::Invalid depends-on entry: $url"
    exit 1
  fi
  if [[ "${BASH_REMATCH[1]}" != open-vela/* ]]; then
    echo "::error::Cross-organization dependencies are forbidden: $url"
    exit 1
  fi
  pair="${BASH_REMATCH[1]}:${BASH_REMATCH[2]}"
  [[ " $ALL_PR " == *" $pair "* ]] || ALL_PR="$ALL_PR $pair"
done
# manifests first: setup applies them before generating the snapshot.
ALL_PR=$({ printf '%s\n' $ALL_PR | grep '^open-vela/manifests:' || true
           printf '%s\n' $ALL_PR | grep -v '^open-vela/manifests:' || true; } | xargs)
echo "all_pr=$ALL_PR" >> $GITHUB_OUTPUT
