#!/usr/bin/env python3
"""Derive the Gitee CI matrix from the GitHub CI workflow in open-vela/public-actions.

The point of this script is that adding or removing a task or a whole group
upstream needs no change here: the matrix, the per-group capabilities and the
pinned tool versions are all read out of the upstream workflow at run time.

What it refuses to do is guess. Anything it cannot express with the known task
flags, or any pin it cannot locate, is a hard error, so the Gitee run fails with
a message naming the drift instead of silently falling back to a plain build.

Usage:
    ci_matrix.py --workflow <file> [--quirks <file>] [--format matrix|env|report]

Output (--format matrix), suitable for strategy.matrix via fromJSON:
    {"include": [{"group": "...", "tasks": [...], "ntfc": "true", ...}, ...]}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - the workflow installs PyYAML
    sys.exit("ci_matrix.py: PyYAML is required (pip install pyyaml)")

# ---------------------------------------------------------------------------
# Flag registry
#
# A task is "<name>" optionally followed by "@flag" groups, e.g.
# "qemu-armv8a:citest@cmake@ntfc". Every flag must be listed here; an unknown
# flag is drift we cannot act on, so it fails the run. That is the whole
# safety property: a task that gains a flag we do not implement must not be
# quietly built as if the flag were absent.
# ---------------------------------------------------------------------------
KNOWN_FLAGS = {
    "cmake": "build with CMake instead of Make",
    "ntfc": "run the NuttX Test Framework for Community on the build output",
}

# The job that holds the build matrix upstream.
CI_TASKS_JOB = "ci-tasks"

# Per-NTFC-task budget, minutes. Mirrors the upstream step timeout of 130 for a
# single task and scales if a group ever carries more than one.
NTFC_STEP_MINUTES = 130
# Measured upstream cost of setup, sync and image pull before the runtime step.
NTFC_JOB_OVERHEAD_MINUTES = 50
# GitHub's default job bound, kept for groups without a runtime test.
DEFAULT_JOB_MINUTES = 360
# GitHub-hosted jobs are capped at 6 h.
MAX_JOB_MINUTES = 360
MAX_STEP_MINUTES = 350


class Drift(Exception):
    """Upstream changed in a way this script will not guess about."""


@dataclass
class Task:
    raw: str
    name: str
    flags: list[str]

    @property
    def is_ntfc(self) -> bool:
        return "ntfc" in self.flags

    @property
    def is_cmake(self) -> bool:
        return "cmake" in self.flags

    @property
    def is_autotest(self) -> bool:
        # Upstream runs the OpenVela autotest suite for tasks whose *name* ends
        # in "-citest" (dash). The NTFC tasks use "board:config" (colon), so the
        # two paths do not overlap.
        return self.name.endswith("-citest")


@dataclass
class Group:
    name: str
    tasks: list[Task]

    @property
    def ntfc_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.is_ntfc]

    @property
    def has_ntfc(self) -> bool:
        return bool(self.ntfc_tasks)

    def step_minutes(self) -> int:
        return min(NTFC_STEP_MINUTES * max(1, len(self.ntfc_tasks)), MAX_STEP_MINUTES)

    def job_minutes(self) -> int:
        if not self.has_ntfc:
            return DEFAULT_JOB_MINUTES
        return min(self.step_minutes() + NTFC_JOB_OVERHEAD_MINUTES, MAX_JOB_MINUTES)


@dataclass
class Pins:
    """Versions and images read out of the upstream workflow."""

    docker_image: str
    build_flags: str
    ntfc_spec: str = ""
    pytest_spec: str = ""
    testing_repo: str = ""
    testing_ref: str = ""

    def as_env(self) -> dict[str, str]:
        return {
            "CI_DOCKER_IMAGE": self.docker_image,
            "CI_BUILD_FLAGS": self.build_flags,
            "CI_NTFC_SPEC": self.ntfc_spec,
            "CI_PYTEST_SPEC": self.pytest_spec,
            "CI_NTFC_TESTING_REPO": self.testing_repo,
            "CI_NTFC_TESTING_REF": self.testing_ref,
        }


@dataclass
class Route:
    """Which upstream workflow serves a given PR target branch."""

    ref: str
    path: str
    via: str

    def as_env(self) -> dict[str, str]:
        return {
            "CI_UPSTREAM_REF": self.ref,
            "CI_UPSTREAM_PATH": self.path,
            "CI_UPSTREAM_VIA": self.via,
        }


# The dispatcher every openvela PR goes through:
# nuttx/ci.yml -> public-actions/ci.yml@dev -> the per-branch CI workflow.
BASE_REF_EQ = re.compile(r"base\.ref\s*==\s*'([^']+)'")
BASE_REF_PREFIX = re.compile(
    r"startsWith\(\s*github\.event\.pull_request\.base\.ref\s*,\s*'([^']+)'\s*\)"
)
USES_UPSTREAM = re.compile(
    r"open-vela/public-actions/(\.github/workflows/[\w.\-]+\.ya?ml)@([\w.\-/]+)"
)


def resolve_route(dispatcher: Path, branch: str) -> Route:
    """Read the branch -> workflow mapping out of the dispatcher workflow.

    Hard-coding the mapping here is what makes a Gitee run diverge from GitHub
    CI when upstream adds a branch family, so it is read instead.
    """
    doc = load_workflow(dispatcher)
    exact: dict[str, Route] = {}
    prefixes: list[tuple[str, Route]] = []

    for job_name, job in (doc.get("jobs") or {}).items():
        uses = job.get("uses")
        cond = job.get("if")
        if not isinstance(uses, str) or not isinstance(cond, str):
            continue
        m = USES_UPSTREAM.search(uses)
        if not m:
            continue
        path, ref = m.group(1), m.group(2)
        # cla.yml carries no build matrix; it is not a CI route.
        if "cla" in path:
            continue
        route = Route(ref=ref, path=path, via=job_name)
        for literal in BASE_REF_EQ.findall(cond):
            exact.setdefault(literal, route)
        for prefix in BASE_REF_PREFIX.findall(cond):
            prefixes.append((prefix, route))

    if not exact and not prefixes:
        raise Drift(
            f"{dispatcher}: found no 'base.ref' conditioned job that calls a "
            "public-actions workflow; the dispatcher was restructured"
        )

    if branch in exact:
        return exact[branch]
    # Longest prefix wins, so 'dev-' beats a hypothetical 'd'.
    for prefix, route in sorted(prefixes, key=lambda p: len(p[0]), reverse=True):
        if branch.startswith(prefix):
            return route
    raise Drift(
        f"{dispatcher}: no route for target branch {branch!r}. "
        f"Exact: {sorted(exact)}; prefixes: {sorted(p for p, _ in prefixes)}. "
        "GitHub CI would run nothing for this branch, so neither will Gitee."
    )


@dataclass
class Extraction:
    groups: list[Group]
    pins: Pins
    fingerprint: str
    source: str
    quirk_tasks: list[str] = field(default_factory=list)

    @property
    def all_tasks(self) -> list[Task]:
        return [t for g in self.groups for t in g.tasks]


# ---------------------------------------------------------------------------
# Task parsing
# ---------------------------------------------------------------------------
def parse_task(raw: str) -> Task:
    raw = raw.strip()
    if not raw:
        raise Drift("empty task entry in the upstream matrix")
    name, sep, tail = raw.partition("@")
    flags = [f for f in tail.split("@") if f] if sep else []
    if not name:
        raise Drift(f"task {raw!r} has no configuration name before '@'")
    unknown = [f for f in flags if f not in KNOWN_FLAGS]
    if unknown:
        raise Drift(
            f"task {raw!r} carries unknown flag(s) {unknown}. "
            f"Known flags: {sorted(KNOWN_FLAGS)}. "
            "Teach tools/ci/task_exec.sh what the flag means, then add it to "
            "KNOWN_FLAGS in tools/ci/ci_matrix.py."
        )
    return Task(raw=raw, name=name, flags=flags)


# ---------------------------------------------------------------------------
# Matrix extraction
# ---------------------------------------------------------------------------
def load_workflow(path: Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise Drift(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or "jobs" not in doc:
        raise Drift(f"{path} does not look like a GitHub workflow (no 'jobs')")
    return doc


def extract_groups(doc: dict, path: Path) -> list[Group]:
    jobs = doc.get("jobs") or {}
    job = jobs.get(CI_TASKS_JOB)
    if job is None:
        raise Drift(
            f"{path} has no '{CI_TASKS_JOB}' job; upstream renamed the build job. "
            f"Jobs present: {sorted(jobs)}"
        )
    matrix = ((job.get("strategy") or {}).get("matrix")) or {}
    if isinstance(matrix, str):
        raise Drift(
            f"{path}: '{CI_TASKS_JOB}' builds its matrix from an expression "
            f"({matrix.strip()!r}); there is no static task list to read."
        )
    include = matrix.get("include")
    if not include:
        raise Drift(f"{path}: '{CI_TASKS_JOB}' matrix has no 'include' entries")

    declared = matrix.get("group")
    groups: list[Group] = []
    seen: set[str] = set()
    for entry in include:
        if not isinstance(entry, dict):
            raise Drift(f"{path}: matrix include entry is not a mapping: {entry!r}")
        name = entry.get("group")
        raw_tasks = entry.get("tasks")
        if not name or raw_tasks is None:
            # Upstream uses include entries purely to attach task lists to
            # groups. An entry without both is a shape we do not model.
            raise Drift(
                f"{path}: matrix include entry {entry!r} lacks 'group' or 'tasks'"
            )
        if isinstance(raw_tasks, str):
            raw_tasks = [raw_tasks]
        if name in seen:
            raise Drift(f"{path}: group {name!r} appears twice in include")
        seen.add(name)
        tasks = [parse_task(str(t)) for t in raw_tasks]
        if not tasks:
            raise Drift(f"{path}: group {name!r} declares an empty task list")
        groups.append(Group(name=name, tasks=tasks))

    # The group list and the include blocks have to agree, otherwise a group
    # would either never run or run with no tasks.
    if declared:
        declared_set = {str(g) for g in declared}
        include_set = set(seen)
        if declared_set != include_set:
            raise Drift(
                f"{path}: matrix.group and include disagree. "
                f"only in group list: {sorted(declared_set - include_set)}; "
                f"only in include: {sorted(include_set - declared_set)}"
            )
    return groups


# ---------------------------------------------------------------------------
# Pin extraction
#
# These live inside shell blocks, so they are matched with regexes. Every one
# of them fails loudly when it does not match: a missing pin means the upstream
# step was restructured, and running with a stale default would silently test
# something other than what GitHub CI tests.
# ---------------------------------------------------------------------------
def _find_step(job: dict, needle: str) -> dict | None:
    for step in job.get("steps") or []:
        if needle in (step.get("name") or ""):
            return step
    return None


def extract_pins(doc: dict, path: Path, want_ntfc: bool) -> Pins:
    job = (doc.get("jobs") or {}).get(CI_TASKS_JOB) or {}
    build_step = _find_step(job, "Run CI tasks")
    if build_step is None or "run" not in build_step:
        raise Drift(f"{path}: cannot find the 'Run CI tasks' step to read pins from")
    build_run = build_step["run"]

    image = re.search(r"(ghcr\.io/open-vela/[\w.\-]+(?::[\w.\-]+)?)", build_run)
    if not image:
        raise Drift(f"{path}: no ghcr.io/open-vela image found in the build step")
    docker_image = image.group(1)
    if ":" not in docker_image.rsplit("/", 1)[-1]:
        docker_image += ":latest"

    flags = re.search(
        r"\./build\.sh\s+\$task\s+\$cmake_flag\s+(?P<flags>[^\"\n]*?)\s*\"?\s*$",
        build_run,
        re.MULTILINE,
    )
    if not flags:
        raise Drift(
            f"{path}: cannot read the build.sh flags out of the build step; "
            "the build command was restructured upstream"
        )
    # Drop the upstream -j value; the Gitee runner sizes it from nproc.
    build_flags = " ".join(
        tok for tok in flags.group("flags").split() if not tok.startswith("-j")
    )
    if not build_flags:
        raise Drift(f"{path}: build.sh flags resolved to an empty string")

    pins = Pins(docker_image=docker_image, build_flags=build_flags)
    if not want_ntfc:
        return pins

    ntfc_step = _find_step(job, "Run NTFC runtime tests")
    if ntfc_step is None or "run" not in ntfc_step:
        raise Drift(
            f"{path}: tasks are flagged 'ntfc' but there is no "
            "'Run NTFC runtime tests' step to read the pins from"
        )
    run = ntfc_step["run"]

    # Either a git pin ("ntfc @ git+https://...@<sha>") or a PyPI pin.
    git_pin = re.search(r"ntfc\s*@\s*git\+(\S+?)(?:\\{0,2})?[\"']", run)
    pypi_pin = re.search(r"\bntfc(==[\w.]+)", run)
    if git_pin:
        pins.ntfc_spec = f"ntfc @ git+{git_pin.group(1)}"
    elif pypi_pin:
        pins.ntfc_spec = f"ntfc{pypi_pin.group(1)}"
    else:
        raise Drift(f"{path}: cannot determine how ntfc is pinned in the NTFC step")

    pytest_pin = re.search(r"\bpytest(==[\w.]+)", run)
    if not pytest_pin:
        raise Drift(f"{path}: cannot determine the pytest pin in the NTFC step")
    pins.pytest_spec = f"pytest{pytest_pin.group(1)}"

    repo = re.search(r"(https://github\.com/\S*?nuttx-ntfc-testing\.git)", run)
    if not repo:
        raise Drift(f"{path}: cannot find the nuttx-ntfc-testing clone URL")
    pins.testing_repo = repo.group(1)

    ref = re.search(r"--branch\s+(\S+)", run)
    if not ref:
        raise Drift(
            f"{path}: cannot find the --branch ref for the nuttx-ntfc-testing clone"
        )
    pins.testing_ref = ref.group(1)
    return pins


# ---------------------------------------------------------------------------
# Drift guard for group-gated steps
#
# Capabilities here are driven by task flags, not by group names. Upstream also
# gates one step on a literal group name (the RISC-V toolchain swap for
# ntfc-rv64). Those are listed in tools/ci/quirks.conf keyed by task name. If
# upstream gates a step on a group whose tasks are not covered there, we have no
# way to reproduce it and say so.
# ---------------------------------------------------------------------------
GROUP_EQ = re.compile(r"matrix\.group\s*==\s*'([^']+)'|matrix\.group\s*==\s*\"([^\"]+)\"")


def load_quirks(path: Path | None) -> dict[str, str]:
    quirks: dict[str, str] = {}
    if path is None or not path.exists():
        return quirks
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise Drift(f"{path}:{lineno}: expected 'task | quirk [| note]'")
        quirks[parts[0]] = parts[1]
    return quirks


def check_group_gated_steps(
    doc: dict, path: Path, groups: list[Group], quirks: dict[str, str]
) -> list[str]:
    job = (doc.get("jobs") or {}).get(CI_TASKS_JOB) or {}
    by_name = {g.name: g for g in groups}
    covered: list[str] = []
    for step in job.get("steps") or []:
        cond = step.get("if")
        if not isinstance(cond, str):
            continue
        for m in GROUP_EQ.finditer(cond):
            gname = m.group(1) or m.group(2)
            group = by_name.get(gname)
            step_name = step.get("name") or "<unnamed step>"
            if group is None:
                raise Drift(
                    f"{path}: step {step_name!r} is gated on group {gname!r}, "
                    "which is not in the matrix"
                )
            missing = [t.raw for t in group.tasks if t.name not in quirks]
            if missing:
                raise Drift(
                    f"{path}: step {step_name!r} only runs for group {gname!r}, "
                    f"and task(s) {missing} have no entry in tools/ci/quirks.conf. "
                    "Add the behaviour to tools/ci/task_exec.sh and record it "
                    "there, or the Gitee build would differ from GitHub CI."
                )
            covered.extend(t.name for t in group.tasks)
    return covered


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def extract(workflow: Path, quirks_path: Path | None) -> Extraction:
    raw = workflow.read_bytes()
    doc = load_workflow(workflow)
    groups = extract_groups(doc, workflow)
    want_ntfc = any(g.has_ntfc for g in groups)
    pins = extract_pins(doc, workflow, want_ntfc)
    quirks = load_quirks(quirks_path)
    quirk_tasks = check_group_gated_steps(doc, workflow, groups, quirks)
    return Extraction(
        groups=groups,
        pins=pins,
        fingerprint=hashlib.sha256(raw).hexdigest(),
        source=str(workflow),
        quirk_tasks=sorted(set(quirk_tasks)),
    )


def as_matrix(ex: Extraction) -> dict:
    include = []
    for g in ex.groups:
        include.append(
            {
                "group": g.name,
                "tasks": [t.raw for t in g.tasks],
                # Strings, not booleans: the workflow compares with == 'true'.
                "ntfc": "true" if g.has_ntfc else "false",
                "ntfc_tasks": [t.raw for t in g.ntfc_tasks],
                "job_minutes": g.job_minutes(),
                "step_minutes": g.step_minutes(),
            }
        )
    return {"include": include}


def as_report(ex: Extraction) -> str:
    out = [
        f"source      : {ex.source}",
        f"fingerprint : sha256:{ex.fingerprint}",
        f"image       : {ex.pins.docker_image}",
        f"build flags : {ex.pins.build_flags}",
    ]
    if ex.pins.ntfc_spec:
        out += [
            f"ntfc        : {ex.pins.ntfc_spec}",
            f"pytest      : {ex.pins.pytest_spec}",
            f"test cases  : {ex.pins.testing_repo} @ {ex.pins.testing_ref}",
        ]
    out.append(f"groups      : {len(ex.groups)}, tasks: {len(ex.all_tasks)}")
    for g in ex.groups:
        caps = []
        if g.has_ntfc:
            caps.append(f"ntfc x{len(g.ntfc_tasks)}")
        if any(t.is_autotest for t in g.tasks):
            caps.append("autotest")
        flags = sorted({f for t in g.tasks for f in t.flags})
        out.append(
            f"  {g.name:<18} {len(g.tasks):>2} task(s)  flags={flags or ['-']}  "
            f"job={g.job_minutes()}m step={g.step_minutes()}m"
            + (f"  [{', '.join(caps)}]" if caps else "")
        )
    if ex.quirk_tasks:
        out.append(f"quirked     : {', '.join(ex.quirk_tasks)}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workflow", type=Path, help="upstream CI workflow to read the matrix from")
    ap.add_argument(
        "--dispatcher",
        type=Path,
        help="public-actions ci.yml, read to resolve a branch to its CI workflow",
    )
    ap.add_argument("--resolve-branch", help="PR target branch to resolve with --dispatcher")
    ap.add_argument(
        "--quirks",
        type=Path,
        default=Path(__file__).with_name("quirks.conf"),
        help="task-keyed table of behaviours upstream gates on a group name",
    )
    ap.add_argument(
        "--format",
        choices=("matrix", "env", "report", "route"),
        default="matrix",
    )
    args = ap.parse_args()

    if args.format == "route" or args.dispatcher:
        if not args.dispatcher or not args.resolve_branch:
            print(
                "::error::--format route needs both --dispatcher and --resolve-branch",
                file=sys.stderr,
            )
            return 2
        try:
            route = resolve_route(args.dispatcher, args.resolve_branch)
        except Drift as exc:
            print(f"::error::upstream CI drift: {exc}", file=sys.stderr)
            return 1
        for key, value in route.as_env().items():
            print(f"{key}={value}")
        return 0

    if not args.workflow:
        print("::error::--workflow is required", file=sys.stderr)
        return 2

    try:
        ex = extract(args.workflow, args.quirks)
    except Drift as exc:
        print(f"::error::upstream CI drift: {exc}", file=sys.stderr)
        return 1

    if args.format == "matrix":
        print(json.dumps(as_matrix(ex), separators=(",", ":")))
    elif args.format == "env":
        for key, value in ex.pins.as_env().items():
            print(f"{key}={value}")
        print(f"CI_UPSTREAM_FINGERPRINT={ex.fingerprint}")
    else:
        print(as_report(ex))
    return 0


if __name__ == "__main__":
    sys.exit(main())
