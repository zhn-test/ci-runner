#!/usr/bin/env python3
"""Gitee CI task sets (tools/ci/tasks.yml).

  matrix --branch B   validate and print the GitHub Actions matrix for branch B
  env                 print the NTFC versions as KEY=VALUE
  drift  --branch B   compare B's task set with its upstream workflow; warn only
"""

import argparse
import fnmatch
import json
import sys
import urllib.request
from pathlib import Path

import yaml

TASKS = Path(__file__).with_name("tasks.yml")
BUILD_SYSTEMS = {"make", "cmake"}
TEST_KINDS = {"none", "ntfc"}
TOOLCHAINS = {"riscv-xpack"}
RAW = "https://raw.githubusercontent.com/open-vela/public-actions"


def fail(msg):
    sys.exit(f"::error::tools/ci/tasks.yml: {msg}")


def load():
    return yaml.safe_load(TASKS.read_text())


def task_set(doc, branch):
    for pattern, name in doc["branches"].items():
        if fnmatch.fnmatchcase(branch, pattern):
            return name, doc["sets"][name]
    fail(f"no task set for target branch {branch!r}")


def validate(name, tset):
    seen = set()
    for g in tset["groups"]:
        group = g.get("group")
        if not group or group in seen:
            fail(f"{name}: missing or duplicate group {group!r}")
        seen.add(group)
        if g.get("toolchain") not in (None, *TOOLCHAINS):
            fail(f"{name}/{group}: unknown toolchain {g['toolchain']!r}")
        tasks = g.get("tasks") or []
        if not tasks:
            fail(f"{name}/{group}: no tasks")
        for t in tasks:
            if not t.get("config") or t.get("build_system") not in BUILD_SYSTEMS \
                    or t.get("test_kind") not in TEST_KINDS:
                fail(f"{name}/{group}: invalid task {t!r}")
        # The NTFC runtime step runs one task per job.
        if any(t["test_kind"] == "ntfc" for t in tasks) and len(tasks) != 1:
            fail(f"{name}/{group}: an ntfc group must hold exactly one task")


def as_upstream_task(t):
    flags = (["cmake"] if t["build_system"] == "cmake" else []) + \
            (["ntfc"] if t["test_kind"] == "ntfc" else [])
    return "@".join([t["config"], *flags])


def cmd_matrix(doc, branch):
    name, tset = task_set(doc, branch)
    validate(name, tset)
    include = []
    for g in tset["groups"]:
        ntfc = g["tasks"][0]["test_kind"] == "ntfc"
        include.append({
            "group": g["group"],
            "tasks": json.dumps(g["tasks"], separators=(",", ":")),
            "ntfc": ntfc,
            "toolchain": g.get("toolchain", ""),
        })
    print(json.dumps({"include": include}, separators=(",", ":")))


def cmd_env(doc):
    n = doc["ntfc"]
    print(f"NTFC_SPEC={n['ntfc']}")
    print(f"PYTEST_SPEC={n['pytest']}")
    print(f"NTFC_TESTING_REPO={n['testing_repo']}")
    print(f"NTFC_TESTING_REF={n['testing_ref']}")


def cmd_drift(doc, branch):
    """Warn when the hand-maintained set no longer matches upstream."""
    name, tset = task_set(doc, branch)
    up = tset["upstream"]
    url = f"{RAW}/{up['ref']}/{up['path']}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            text = r.read().decode()
        include = yaml.safe_load(text)["jobs"]["ci-tasks"]["strategy"]["matrix"]["include"]
    except Exception as exc:  # warn only; never block the run
        print(f"::warning::cannot read upstream {url}: {exc}")
        return
    ours = {g["group"]: [as_upstream_task(t) for t in g["tasks"]] for g in tset["groups"]}
    theirs = {i["group"]: list(i["tasks"]) for i in include}
    diffs = []
    for g in sorted(set(ours) | set(theirs)):
        if ours.get(g) != theirs.get(g):
            diffs.append(f"{g}: gitee={ours.get(g)} upstream={theirs.get(g)}")
    if any(t["test_kind"] == "ntfc" for g in tset["groups"] for t in g["tasks"]):
        for key in ("ntfc", "pytest", "testing_ref"):
            if doc["ntfc"][key] not in text:
                diffs.append(f"NTFC {key} {doc['ntfc'][key]!r} not found upstream")
    if diffs:
        print(f"::warning::Gitee task set {name!r} (aligned to {up['commit']}) differs from "
              f"{up['path']}@{up['ref']}; update tools/ci/tasks.yml")
        for d in diffs:
            print(f"::warning::  {d}")
    else:
        print(f"task set {name!r} matches {up['path']}@{up['ref']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("matrix", "env", "drift"))
    ap.add_argument("--branch")
    args = ap.parse_args()
    doc = load()
    if args.command == "env":
        cmd_env(doc)
    elif not args.branch:
        ap.error("--branch is required")
    elif args.command == "matrix":
        cmd_matrix(doc, args.branch)
    else:
        cmd_drift(doc, args.branch)


if __name__ == "__main__":
    main()
