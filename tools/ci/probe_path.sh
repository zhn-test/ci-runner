#!/bin/bash
set -e
line=$(grep -E "<project [^>]*name=\"$PROJECT_NAME\"" .repo/manifests/openvela.xml | head -1 || true)
    REPO_PATH=$(sed -n 's/.*path="\([^"]*\)".*/\1/p' <<< "$line")
    [ -n "$REPO_PATH" ] || REPO_PATH=$(sed -n 's/.*name="\([^"]*\)".*/\1/p' <<< "$line")
    if [ -z "$REPO_PATH" ]; then
      echo "::error::$REPO_FULL_PATH is not in the manifest, so PR #$PR_NUMBER cannot be built"
      exit 1
    fi
echo "$REPO_PATH"
