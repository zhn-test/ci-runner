#!/usr/bin/env python3
"""Run the GitHub CI of open-vela/public-actions for a Gitee pull request.

Gitee CI does not keep its own copy of what a CI task means. At run time it
reads the upstream workflow (pinned to one public-actions commit) and:

  * expands the upstream ci-tasks matrix, so groups and tasks added or removed
    upstream are picked up with no change here;
  * evaluates the upstream job timeout and runner label per matrix entry;
  * executes the upstream ci-tasks steps themselves, after the point where the
    source tree is ready, with the same if/env/timeout/continue-on-error and
    GITHUB_ENV semantics as the GitHub runner.

Only the source preparation is Gitee's own: the Gitee manifest, the snapshot
and the cherry-pick of the Gitee pull requests. Those upstream steps, which
talk to GitHub, are the ones this script deliberately does not run.

What it refuses to do is guess. A step it cannot run faithfully (an unknown
action, an expression context Gitee has no value for, a new step in the
source-preparation phase, an unsupported shell) fails the run and names the
step, instead of quietly running something other than GitHub CI.

Subcommands:
  route  --dispatcher F --branch B     which upstream workflow serves branch B
  check  --workflow F                  validate, print the execution plan
  matrix --workflow F                  matrix JSON for strategy.matrix
  info   --workflow F                  KEY=VALUE (runs-on)
  run    --workflow F --matrix JSON    execute the upstream steps for one entry
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - the workflow installs PyYAML
    sys.exit("upstream_ci.py: PyYAML is required")


class Drift(Exception):
    """Upstream changed in a way Gitee CI cannot reproduce faithfully."""


# ---------------------------------------------------------------------------
# The contract with upstream. These are the only places that name upstream
# structure, and each one fails loudly when upstream no longer matches it.
# ---------------------------------------------------------------------------

# The job that holds the build matrix.
CI_TASKS_JOB = "ci-tasks"

# Upstream steps up to and including this one prepare the source tree from
# GitHub. Gitee prepares it from Gitee instead, then runs everything after it.
BOUNDARY_STEP = "check cherry-pick pr result"

# Every upstream step before the boundary must be one Gitee replaces. A new one
# fails the run, because skipping it could change what is built.
REPLACED_RUN_STEPS = {
    "Download and install repo": "Gitee installs repo itself",
    "Repo Init": "Gitee inits from the Gitee manifest and snapshot",
    "Repo Sync": "Gitee syncs from the Gitee snapshot",
    "Fetch PR": "Gitee cherry-picks the Gitee pull requests",
}
REPLACED_ACTIONS = {
    "actions/download-artifact": "Gitee downloads its own snapshot",
    "easimon/maximize-build-space": "Gitee frees disk space itself",
    "jlumbroso/free-disk-space": "Gitee frees disk space itself",
}

# Actions after the boundary. Anything else is drift.
LOGIN_ACTION = "docker/login-action"
UPLOAD_ACTION = "actions/upload-artifact"

# GITHUB_ENV values the replaced steps would have set and later steps may read.
SEEDED_ENV = ("REPO_INIT", "REPO_ROOT")

# Job keys that are understood. Anything else (container, services, defaults,
# a job-level if ...) changes how steps run and is drift.
KNOWN_JOB_KEYS = {
    "name", "needs", "runs-on", "timeout-minutes", "env", "strategy", "steps",
    "outputs", "permissions",
}
KNOWN_STEP_KEYS = {
    "name", "id", "if", "uses", "with", "run", "env", "shell",
    "working-directory", "timeout-minutes", "continue-on-error",
}
SHELLS = {
    None: ["bash", "-e", "{0}"],
    "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail", "{0}"],
    "sh": ["sh", "-e", "{0}"],
}
DEFAULT_JOB_MINUTES = 360
# Seconds per timeout minute. Only the tests shorten it.
MINUTE_SECONDS = float(os.environ.get("UPSTREAM_CI_MINUTE_SECONDS", "60"))


# ---------------------------------------------------------------------------
# GitHub Actions expressions
#
# The subset upstream uses, with GitHub's semantics: && and || return operand
# values, == compares strings case-insensitively and coerces mixed types to
# numbers, and an if without a status function is implicitly success() && ...
# Any context or property Gitee has no value for raises Drift.
# ---------------------------------------------------------------------------
class Strict(dict):
    """A context object whose missing properties are drift, not null."""


class StatusFn:
    def __init__(self, name: str):
        self.name = name


_TOKEN = re.compile(
    r"""\s*(?:
        (?P<num>0x[0-9a-fA-F]+|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
      | (?P<str>'(?:[^']|'')*')
      | (?P<op>&&|\|\||==|!=|<=|>=|[<>!().\[\],*])
      | (?P<id>[A-Za-z_][A-Za-z0-9_-]*)
    )""",
    re.VERBOSE,
)

STATUS_FUNCTIONS = {"success", "failure", "always", "cancelled"}


def _tokenize(text: str) -> List[Tuple[str, Any]]:
    out: List[Tuple[str, Any]] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise Drift(f"cannot parse expression {text!r} at {text[pos:]!r}")
        pos = m.end()
        if m.group("num") is not None:
            raw = m.group("num")
            out.append(("lit", float(int(raw, 16)) if raw.startswith("0x") else float(raw)))
        elif m.group("str") is not None:
            out.append(("lit", m.group("str")[1:-1].replace("''", "'")))
        elif m.group("op") is not None:
            out.append(("op", m.group("op")))
        else:
            ident = m.group("id")
            if ident == "true":
                out.append(("lit", True))
            elif ident == "false":
                out.append(("lit", False))
            elif ident == "null":
                out.append(("lit", None))
            else:
                out.append(("id", ident))
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return out


class _Parser:
    """Recursive-descent parser producing a small AST of tuples."""

    def __init__(self, text: str):
        self.text = text
        self.toks = _tokenize(text)
        self.i = 0

    def peek(self, value: Optional[str] = None) -> bool:
        if self.i >= len(self.toks):
            return False
        kind, val = self.toks[self.i]
        return value is None or (kind == "op" and val == value)

    def take(self, value: Optional[str] = None):
        if not self.peek(value):
            raise Drift(f"cannot parse expression {self.text!r}: expected {value!r}")
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def parse(self):
        node = self.or_()
        if self.i != len(self.toks):
            raise Drift(f"cannot parse expression {self.text!r}: trailing tokens")
        return node

    def or_(self):
        node = self.and_()
        while self.peek("||"):
            self.take()
            node = ("or", node, self.and_())
        return node

    def and_(self):
        node = self.eq()
        while self.peek("&&"):
            self.take()
            node = ("and", node, self.eq())
        return node

    def eq(self):
        node = self.cmp()
        while self.peek("==") or self.peek("!="):
            op = self.take()[1]
            node = ("cmp", op, node, self.cmp())
        return node

    def cmp(self):
        node = self.unary()
        while any(self.peek(o) for o in ("<", ">", "<=", ">=")):
            op = self.take()[1]
            node = ("cmp", op, node, self.unary())
        return node

    def unary(self):
        if self.peek("!"):
            self.take()
            return ("not", self.unary())
        return self.postfix()

    def postfix(self):
        node = self.primary()
        while True:
            if self.peek("."):
                self.take()
                kind, val = self.take()
                if kind == "op" and val == "*":
                    raise Drift(f"object filters are not supported: {self.text!r}")
                if kind != "id":
                    raise Drift(f"cannot parse expression {self.text!r}: bad property")
                node = ("prop", node, ("lit", val))
            elif self.peek("["):
                self.take()
                if self.peek("*"):
                    raise Drift(f"object filters are not supported: {self.text!r}")
                idx = self.or_()
                self.take("]")
                node = ("prop", node, idx)
            else:
                return node

    def primary(self):
        if self.peek("("):
            self.take()
            node = self.or_()
            self.take(")")
            return node
        kind, val = self.take()
        if kind == "lit":
            return ("lit", val)
        if kind != "id":
            raise Drift(f"cannot parse expression {self.text!r}: unexpected {val!r}")
        if self.peek("("):
            self.take()
            args = []
            if not self.peek(")"):
                args.append(self.or_())
                while self.peek(","):
                    self.take()
                    args.append(self.or_())
            self.take(")")
            return ("call", val, args)
        return ("ctx", val)


def _walk_uses_status(node) -> bool:
    if isinstance(node, list):
        return any(_walk_uses_status(n) for n in node)
    if not isinstance(node, tuple):
        return False
    if node[0] == "call" and node[1] in STATUS_FUNCTIONS:
        return True
    return any(_walk_uses_status(n) for n in node[1:])


def truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, float) and (v == 0 or math.isnan(v)):
        return False
    if isinstance(v, int) and not isinstance(v, bool) and v == 0:
        return False
    if v == "":
        return False
    return True


def to_number(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return 0.0
        try:
            return float(int(s, 16)) if s.lower().startswith("0x") else float(s)
        except ValueError:
            return float("nan")
    return float("nan")


def to_str(v: Any) -> str:
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "Array"
    if isinstance(v, dict):
        return "Object"
    return str(v)


def _kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, str):
        return "str"
    return "obj"


def _equal(a: Any, b: Any) -> bool:
    ka, kb = _kind(a), _kind(b)
    if ka == kb:
        if ka == "str":
            return a.casefold() == b.casefold()
        if ka == "num":
            return float(a) == float(b)
        if ka == "obj":
            return a is b
        return a == b
    if "obj" in (ka, kb):
        return False
    na, nb = to_number(a), to_number(b)
    return na == nb


def _compare(op: str, a: Any, b: Any) -> bool:
    if op == "==":
        return _equal(a, b)
    if op == "!=":
        return not _equal(a, b)
    if _kind(a) == "str" and _kind(b) == "str":
        x, y = a.casefold(), b.casefold()
    else:
        x, y = to_number(a), to_number(b)
        if math.isnan(x) or math.isnan(y):
            return False
    return {"<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]


class Evaluator:
    def __init__(self, contexts: Dict[str, Any], status: str = "success"):
        self.contexts = contexts
        self.status = status  # success | failure

    def eval(self, node) -> Any:
        tag = node[0]
        if tag == "lit":
            return node[1]
        if tag == "ctx":
            name = node[1]
            if name not in self.contexts:
                raise Drift(f"expression context {name!r} has no Gitee equivalent")
            return self.contexts[name]
        if tag == "prop":
            obj = self.eval(node[1])
            key = self.eval(node[2])
            return self._prop(obj, key, node)
        if tag == "not":
            return not truthy(self.eval(node[1]))
        if tag == "and":
            left = self.eval(node[1])
            return self.eval(node[2]) if truthy(left) else left
        if tag == "or":
            left = self.eval(node[1])
            return left if truthy(left) else self.eval(node[2])
        if tag == "cmp":
            return _compare(node[1], self.eval(node[2]), self.eval(node[3]))
        if tag == "call":
            return self._call(node[1], [self.eval(a) for a in node[2]])
        raise Drift(f"unsupported expression node {tag!r}")

    @staticmethod
    def _prop(obj: Any, key: Any, node) -> Any:
        if isinstance(obj, list):
            n = to_number(key)
            if math.isnan(n) or n < 0 or n >= len(obj) or not float(n).is_integer():
                return None
            return obj[int(n)]
        if isinstance(obj, dict):
            k = to_str(key)
            for real in obj:
                if real.casefold() == k.casefold():
                    return obj[real]
            if isinstance(obj, Strict):
                raise Drift(f"expression property {k!r} has no Gitee equivalent")
            return None
        return None

    def _call(self, name: str, args: List[Any]) -> Any:
        lname = name.lower()
        if lname == "success":
            return self.status == "success"
        if lname == "failure":
            return self.status == "failure"
        if lname == "always":
            return True
        if lname == "cancelled":
            # A cancelled Gitee job stops this script; it never sees it.
            return False
        if lname in ("startswith", "endswith", "contains"):
            if len(args) != 2:
                raise Drift(f"{name}() takes 2 arguments")
            hay, needle = args
            if lname == "contains" and isinstance(hay, list):
                return any(_equal(x, needle) for x in hay)
            h, n = to_str(hay).casefold(), to_str(needle).casefold()
            return h.startswith(n) if lname == "startswith" else (
                h.endswith(n) if lname == "endswith" else n in h)
        if lname == "join":
            sep = to_str(args[1]) if len(args) > 1 else ","
            seq = args[0]
            if isinstance(seq, list):
                return sep.join(to_str(x) for x in seq)
            return to_str(seq)
        if lname == "format":
            fmt = to_str(args[0])
            vals = [to_str(a) for a in args[1:]]
            out = re.sub(r"\{(\d+)\}", lambda m: vals[int(m.group(1))], fmt)
            return out.replace("{{", "{").replace("}}", "}")
        if lname == "tojson":
            return json.dumps(args[0], indent=2)
        if lname == "fromjson":
            return json.loads(to_str(args[0]))
        raise Drift(f"expression function {name}() is not supported")


_EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def parse_expr(text: str):
    return _Parser(text).parse()


def evaluate(text: str, ev: Evaluator) -> Any:
    return ev.eval(parse_expr(text))


def interpolate(value: Any, ev: Evaluator) -> Any:
    """Expand ${{ }} in a workflow value. A whole-string expression keeps its type."""
    if not isinstance(value, str):
        return value
    whole = _EXPR.fullmatch(value.strip())
    if whole:
        return evaluate(whole.group(1), ev)
    return _EXPR.sub(lambda m: to_str(evaluate(m.group(1), ev)), value)


def eval_if(cond: Any, ev: Evaluator) -> bool:
    """Evaluate a step/job if, applying GitHub's implicit success()."""
    if cond is None:
        return ev.status == "success"
    if isinstance(cond, bool):
        return cond and ev.status == "success"
    text = str(cond).strip()
    whole = _EXPR.fullmatch(text)
    if whole:
        text = whole.group(1)
    node = parse_expr(text)
    if not _walk_uses_status(node):
        node = ("and", ("call", "success", []), node)
    return truthy(ev.eval(node))


# ---------------------------------------------------------------------------
# Workflow loading
# ---------------------------------------------------------------------------
def load_workflow(path: Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise Drift(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        raise Drift(f"{path} does not look like a GitHub workflow (no 'jobs')")
    return doc


def ci_job(doc: dict) -> dict:
    job = doc["jobs"].get(CI_TASKS_JOB)
    if not isinstance(job, dict):
        raise Drift(f"upstream has no '{CI_TASKS_JOB}' job; jobs: {sorted(doc['jobs'])}")
    return job


def action_name(uses: str) -> str:
    return uses.split("@", 1)[0]


# ---------------------------------------------------------------------------
# Branch routing, read from the dispatcher every openvela PR goes through:
# nuttx/ci.yml -> public-actions/ci.yml@dev -> the per-branch CI workflow.
# ---------------------------------------------------------------------------
BASE_REF_EQ = re.compile(r"base\.ref\s*==\s*'([^']+)'")
BASE_REF_PREFIX = re.compile(
    r"startsWith\(\s*github\.event\.pull_request\.base\.ref\s*,\s*'([^']+)'\s*\)")
USES_UPSTREAM = re.compile(
    r"open-vela/public-actions/(\.github/workflows/[\w.\-]+\.ya?ml)@([\w.\-/]+)")


def resolve_route(dispatcher: Path, branch: str) -> Dict[str, str]:
    doc = load_workflow(dispatcher)
    exact: Dict[str, Dict[str, str]] = {}
    prefixes: List[Tuple[str, Dict[str, str]]] = []
    for job_name, job in doc["jobs"].items():
        uses, cond = job.get("uses"), job.get("if")
        if not isinstance(uses, str) or not isinstance(cond, str):
            continue
        m = USES_UPSTREAM.search(uses)
        if not m:
            continue
        route = {"CI_UPSTREAM_PATH": m.group(1), "CI_UPSTREAM_REF": m.group(2),
                 "CI_UPSTREAM_VIA": job_name}
        for lit in BASE_REF_EQ.findall(cond):
            exact.setdefault(lit, route)
        for pre in BASE_REF_PREFIX.findall(cond):
            prefixes.append((pre, route))
    if not exact and not prefixes:
        raise Drift(f"{dispatcher}: no 'base.ref' conditioned job calls a public-actions workflow")
    if branch in exact:
        route = exact[branch]
    else:
        route = next((r for p, r in sorted(prefixes, key=lambda x: -len(x[0]))
                      if branch.startswith(p)), None)
        if route is None:
            raise Drift(f"{dispatcher}: no route for target branch {branch!r}; "
                        "GitHub CI runs nothing for it, so neither does Gitee")
    # cla.yml or any other workflow without the build job is not a CI route;
    # that is checked when the routed workflow is loaded.
    return route


# ---------------------------------------------------------------------------
# Matrix expansion, following GitHub's include/exclude rules.
# ---------------------------------------------------------------------------
def expand_matrix(job: dict, ev_factory) -> List[Dict[str, Any]]:
    strategy = job.get("strategy") or {}
    matrix = strategy.get("matrix")
    if matrix is None:
        return [{}]
    if isinstance(matrix, str):
        raise Drift(f"'{CI_TASKS_JOB}' builds its matrix from an expression; nothing static to read")
    matrix = dict(matrix)
    include = matrix.pop("include", None) or []
    exclude = matrix.pop("exclude", None) or []
    for key, values in matrix.items():
        if not isinstance(values, list):
            raise Drift(f"matrix key {key!r} is not a list")
    keys = list(matrix)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*matrix.values())] if keys else []

    def matches(combo, pattern):
        return all(k in combo and combo[k] == v for k, v in pattern.items())

    combos = [c for c in combos if not any(matches(c, e) for e in exclude)]
    base_keys = set(keys)
    extra: List[Dict[str, Any]] = []
    for inc in include:
        if not isinstance(inc, dict):
            raise Drift(f"matrix include entry is not a mapping: {inc!r}")
        hit = False
        for c in combos:
            if all(c[k] == v for k, v in inc.items() if k in base_keys):
                # An include may add keys, never overwrite an original value.
                for k, v in inc.items():
                    if k not in base_keys:
                        c[k] = v
                hit = True
        if not hit:
            extra.append(dict(inc))
    combos.extend(extra)
    if not combos:
        raise Drift(f"'{CI_TASKS_JOB}' matrix expands to no jobs")
    labels = keys or sorted({k for c in combos for k in c})
    out = []
    for c in combos:
        ev = ev_factory(c)
        entry = dict(c)
        entry["gitee_label"] = ", ".join(to_str(c.get(k)) for k in labels if k in c
                                         and not isinstance(c.get(k), (list, dict)))
        entry["gitee_job_minutes"] = job_minutes(job, ev)
        out.append(entry)
    return out


def job_minutes(job: dict, ev: Evaluator) -> int:
    raw = job.get("timeout-minutes")
    if raw is None:
        return DEFAULT_JOB_MINUTES
    val = interpolate(raw, ev) if isinstance(raw, str) else raw
    n = to_number(val)
    if math.isnan(n) or n <= 0:
        raise Drift(f"job timeout-minutes {raw!r} evaluated to {val!r}")
    return int(n)


# ---------------------------------------------------------------------------
# Plan: which upstream steps Gitee replaces, which it runs.
# ---------------------------------------------------------------------------
def split_steps(job: dict) -> Tuple[List[dict], List[dict]]:
    steps = job.get("steps") or []
    names = [s.get("name") for s in steps]
    if names.count(BOUNDARY_STEP) != 1:
        raise Drift(f"expected exactly one step named {BOUNDARY_STEP!r} in '{CI_TASKS_JOB}', "
                    f"found {names.count(BOUNDARY_STEP)}; the source-preparation phase was restructured")
    cut = names.index(BOUNDARY_STEP)
    return steps[: cut + 1], steps[cut + 1:]


def _step_label(step: dict) -> str:
    return step.get("name") or step.get("uses") or (step.get("run") or "").splitlines()[0][:40]


def _step_text(step: dict) -> str:
    return yaml.safe_dump(step, sort_keys=True)


def check_plan(doc: dict, contexts_for) -> List[Tuple[str, str]]:
    """Validate the upstream job against the contract; return the plan."""
    job = ci_job(doc)
    unknown = sorted(set(job) - KNOWN_JOB_KEYS)
    if unknown:
        raise Drift(f"'{CI_TASKS_JOB}' uses job keys Gitee does not reproduce: {unknown}")
    if not isinstance(job.get("runs-on"), str) or "${{" in job["runs-on"]:
        raise Drift(f"'{CI_TASKS_JOB}' runs-on is not a plain label: {job.get('runs-on')!r}")
    for key in ("defaults",):
        if key in doc:
            raise Drift(f"upstream workflow sets top-level {key!r}, which Gitee does not reproduce")

    pre, post = split_steps(job)
    plan: List[Tuple[str, str]] = []
    written_by_pre = set()
    for step in pre:
        label = _step_label(step)
        if "uses" in step:
            reason = REPLACED_ACTIONS.get(action_name(step["uses"]))
        elif step.get("name") == BOUNDARY_STEP:
            reason = "Gitee checks its own cherry-pick result"
        else:
            reason = REPLACED_RUN_STEPS.get(step.get("name") or "")
        if reason is None:
            raise Drift(f"upstream step {label!r} runs before {BOUNDARY_STEP!r}, where Gitee "
                        "prepares the source tree itself; Gitee does not know whether skipping it "
                        "changes what is built. Teach .github/workflows/ci.yml what it does and add "
                        "it to REPLACED_* in tools/ci/upstream_ci.py.")
        plan.append(("replaced", f"{label}  ({reason})"))
        written_by_pre.update(re.findall(r"\b([A-Z][A-Z0-9_]*)=[^\n]*>>\s*\"?\$\{?GITHUB_ENV",
                                         step.get("run") or ""))

    uploads = [i for i, s in enumerate(post) if action_name(s.get("uses", "")) == UPLOAD_ACTION]
    if len(uploads) > 1:
        raise Drift("upstream uploads more than one artifact after the build; Gitee uploads one")
    if uploads and uploads[0] != len(post) - 1:
        raise Drift("upstream uploads its artifact before the last step; Gitee uploads it last")

    post_text = "\n".join(_step_text(s) for s in post)
    for name in sorted(written_by_pre - set(SEEDED_ENV)):
        if re.search(rf"\b{name}\b", post_text):
            raise Drift(f"a step after {BOUNDARY_STEP!r} reads {name}, which only the replaced "
                        "GitHub source-preparation steps set")

    for step in post:
        label = _step_label(step)
        unknown = sorted(set(step) - KNOWN_STEP_KEYS)
        if unknown:
            raise Drift(f"upstream step {label!r} uses keys Gitee does not reproduce: {unknown}")
        if "uses" in step:
            act = action_name(step["uses"])
            if act == LOGIN_ACTION:
                plan.append(("gitee", f"{label}  (Gitee logs in to the registry before this phase)"))
            elif act == UPLOAD_ACTION:
                plan.append(("upload", label))
            else:
                raise Drift(f"upstream step {label!r} uses action {act!r}; Gitee can only run "
                            f"'run' steps, {LOGIN_ACTION} and {UPLOAD_ACTION} here")
        elif "run" in step:
            if step.get("shell") not in SHELLS:
                raise Drift(f"upstream step {label!r} uses shell {step.get('shell')!r}")
            plan.append(("run", label))
        else:
            raise Drift(f"upstream step {label!r} has neither run nor uses")

    # Every expression must be evaluable with what Gitee knows, for every
    # matrix entry, in both job states. A missing context fails here, before
    # any job starts, rather than halfway through a build.
    for entry in expand_matrix(job, lambda c: Evaluator(contexts_for(c))):
        for status in ("success", "failure"):
            ev = Evaluator(contexts_for(entry), status)
            for scope in (doc.get("env") or {}, job.get("env") or {}):
                for v in scope.values():
                    interpolate(v, ev)
            for step in post:
                eval_if(step.get("if"), ev)
                for key in ("run", "working-directory", "timeout-minutes", "continue-on-error",
                            "shell"):
                    if key in step:
                        interpolate(step[key], ev)
                for scope in (step.get("env") or {}, step.get("with") or {}):
                    for v in scope.values():
                        interpolate(v, ev)
    return plan


# ---------------------------------------------------------------------------
# Contexts: what the upstream expressions may read, mapped onto the Gitee PR.
# ---------------------------------------------------------------------------
def gitee_contexts(matrix: Dict[str, Any], env: Optional[Dict[str, str]] = None,
                   status: str = "success") -> Dict[str, Any]:
    e = os.environ if env is None else env
    pr = e.get("GITEE_PR_ID", "0")
    return {
        "matrix": Strict(matrix),
        "github": Strict({
            "workspace": e.get("GITHUB_WORKSPACE", os.getcwd()),
            "run_id": e.get("GITHUB_RUN_ID", "0"),
            "run_attempt": e.get("GITHUB_RUN_ATTEMPT", "1"),
            "run_number": e.get("GITHUB_RUN_NUMBER", "0"),
            "event_name": "pull_request_target",
            "actor": e.get("GITHUB_ACTOR", ""),
            "token": e.get("GITEE_CI_GITHUB_TOKEN", ""),
            "event": Strict({"pull_request": Strict({
                "number": int(pr) if pr.isdigit() else pr,
                "base": Strict({"ref": e.get("GITEE_PR_BRANCH", "")}),
            })}),
        }),
        "needs": Strict({"setup": Strict({"outputs": Strict({
            "TRIGGER_REPO_NAME": e.get("GITEE_TRIGGER_REPO", ""),
            "SNAPSHOT": e.get("GITEE_SNAPSHOT", ""),
        })})}),
        "secrets": Strict({"GITHUB_TOKEN": e.get("GITEE_CI_GITHUB_TOKEN", "")}),
        "runner": Strict({"os": "Linux", "temp": e.get("RUNNER_TEMP", "/tmp"),
                          "arch": e.get("RUNNER_ARCH", "X64")}),
        "job": Strict({"status": status}),
        "env": {},
    }


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------
def read_env_file(path: Path) -> Dict[str, str]:
    """Parse a GITHUB_ENV file: NAME=value or NAME<<DELIM ... DELIM. Last wins."""
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    lines = path.read_text(errors="replace").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip():
            continue
        heredoc = re.match(r"^([^=<]+)<<(.+)$", line)
        if heredoc and ("=" not in line or line.index("<<") < line.index("=")):
            name, delim = heredoc.group(1), heredoc.group(2)
            body = []
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            if i >= len(lines):
                raise Drift(f"unterminated {delim!r} block for {name} in GITHUB_ENV")
            i += 1
            out[name] = "\n".join(body)
        elif "=" in line:
            name, value = line.split("=", 1)
            out[name] = value
        else:
            raise Drift(f"malformed GITHUB_ENV line: {line!r}")
    return out


def _kill_tree(proc: subprocess.Popen) -> None:
    # Same order as the GitHub runner on a step timeout: SIGINT, then SIGTERM,
    # then SIGKILL, each after a grace period.
    for sig, grace in ((signal.SIGINT, 7.5), (signal.SIGTERM, 2.5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def run_step(script: str, shell: Optional[str], env: Dict[str, str], cwd: str,
             timeout_min: Optional[int]) -> Tuple[str, int]:
    """Run one step; return (outcome, exit code). outcome: success|failure|timeout."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False,
                                     dir=env.get("RUNNER_TEMP") or None) as fh:
        fh.write(script)
        path = fh.name
    argv = [path if a == "{0}" else a for a in SHELLS[shell]]
    sys.stdout.flush()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True)
    try:
        rc = proc.wait(timeout=timeout_min * MINUTE_SECONDS if timeout_min else None)
        return ("success" if rc == 0 else "failure"), rc
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return "timeout", -1
    finally:
        os.unlink(path)


def run_job(doc: dict, matrix: Dict[str, Any], out_path: Optional[Path]) -> int:
    job = ci_job(doc)
    _, post = split_steps(job)
    base = dict(os.environ)
    workspace = base.get("GITHUB_WORKSPACE", os.getcwd())
    temp = Path(base.get("RUNNER_TEMP") or tempfile.gettempdir())
    outputs: Dict[str, str] = {"archive": "false"}

    status = "success"
    ev = Evaluator(gitee_contexts(matrix, base, status), status)
    static_env: Dict[str, str] = {}
    for scope in (doc.get("env") or {}, job.get("env") or {}):
        for k, v in scope.items():
            ev.contexts["env"] = dict(static_env)
            static_env[k] = to_str(interpolate(v, ev))
    exported: Dict[str, str] = {"REPO_INIT": "true", "REPO_ROOT": workspace}
    extra_path: List[str] = []

    for n, step in enumerate(post, 1):
        label = _step_label(step)
        ev = Evaluator(gitee_contexts(matrix, base, status), status)
        ev.contexts["env"] = {**static_env, **exported}
        print(f"::group::[upstream {n}/{len(post)}] {label}", flush=True)
        try:
            if not eval_if(step.get("if"), ev):
                print(f"skipped: if {step.get('if')!r} is false (job {status})")
                continue
            if "uses" in step:
                act = action_name(step["uses"])
                if act == UPLOAD_ACTION:
                    with_ = {k: to_str(interpolate(v, ev)) for k, v in (step.get("with") or {}).items()}
                    outputs.update({
                        "archive": "true",
                        "archive_name": with_.get("name", "artifact"),
                        "archive_path": with_.get("path", ""),
                        "archive_if_no_files_found": with_.get("if-no-files-found", "warn"),
                    })
                    print(f"handed to the Gitee upload step: {outputs['archive_name']}")
                else:
                    print("done by Gitee before this phase")
                continue

            step_env = {}
            ev.contexts["env"] = {**static_env, **exported}
            for k, v in (step.get("env") or {}).items():
                step_env[k] = to_str(interpolate(v, ev))
            ev.contexts["env"] = {**static_env, **exported, **step_env}
            script = to_str(interpolate(step["run"], ev))
            timeout = step.get("timeout-minutes")
            timeout = int(to_number(interpolate(timeout, ev))) if timeout is not None else None
            coe = truthy(interpolate(step.get("continue-on-error", False), ev))
            wd = step.get("working-directory")
            cwd = os.path.join(workspace, to_str(interpolate(wd, ev))) if wd else workspace

            files = {k: temp / f"upstream-{n}-{k.lower()}" for k in
                     ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STATE")}
            for f in files.values():
                f.write_text("")
            env = {**base, **static_env, **exported, **step_env,
                   **{k: str(v) for k, v in files.items()}}
            if extra_path:
                env["PATH"] = os.pathsep.join(extra_path + [base.get("PATH", "")])
            print(f"timeout: {timeout or '-'} min, continue-on-error: {coe}")
            outcome, rc = run_step(script, step.get("shell"), env, cwd, timeout)

            exported.update(read_env_file(files["GITHUB_ENV"]))
            extra_path = [p for p in files["GITHUB_PATH"].read_text().splitlines()
                          if p.strip()][::-1] + extra_path
            for f in files.values():
                f.unlink()
            if outcome == "timeout":
                print(f"::error::upstream step {label!r} timed out after {timeout} min")
            elif outcome == "failure":
                print(f"::error::upstream step {label!r} failed with exit code {rc}")
            if outcome != "success" and not coe:
                status = "failure"
        finally:
            print("::endgroup::", flush=True)

    outputs["status"] = status
    if out_path:
        with out_path.open("a") as fh:
            for k, v in outputs.items():
                # Heredoc form: upstream paths are often multi-line.
                v = v.strip("\n")
                fh.write(f"{k}<<UPSTREAM_CI_EOF\n{v}\nUPSTREAM_CI_EOF\n")
    print(f"upstream steps finished: {status}")
    return 0 if status == "success" else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _contexts_for_check(entry: Dict[str, Any]) -> Dict[str, Any]:
    ctx = gitee_contexts(entry, {})
    ctx["env"] = {}
    return ctx


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("route", "check", "matrix", "info", "run"))
    ap.add_argument("--workflow", type=Path)
    ap.add_argument("--dispatcher", type=Path)
    ap.add_argument("--branch")
    ap.add_argument("--matrix", help="one matrix entry as JSON (run)")
    args = ap.parse_args(argv)

    try:
        if args.command == "route":
            if not args.dispatcher or not args.branch:
                ap.error("route needs --dispatcher and --branch")
            for k, v in resolve_route(args.dispatcher, args.branch).items():
                print(f"{k}={v}")
            return 0
        if not args.workflow:
            ap.error(f"{args.command} needs --workflow")
        doc = load_workflow(args.workflow)
        job = ci_job(doc)
        if args.command == "check":
            plan = check_plan(doc, _contexts_for_check)
            entries = expand_matrix(job, lambda c: Evaluator(_contexts_for_check(c)))
            print(f"runs-on : {job['runs-on']}")
            print(f"jobs    : {len(entries)}")
            for e in entries:
                print(f"  {e['gitee_label']:<20} timeout={e['gitee_job_minutes']}m")
            print("steps   :")
            for kind, label in plan:
                print(f"  {kind:<9} {label}")
            return 0
        if args.command == "matrix":
            check_plan(doc, _contexts_for_check)
            entries = expand_matrix(job, lambda c: Evaluator(_contexts_for_check(c)))
            print(json.dumps({"include": entries}, separators=(",", ":")))
            return 0
        if args.command == "info":
            print(f"runs_on={job['runs-on']}")
            return 0
        if args.command == "run":
            if not args.matrix:
                ap.error("run needs --matrix")
            check_plan(doc, _contexts_for_check)
            out = os.environ.get("GITHUB_OUTPUT")
            return run_job(doc, json.loads(args.matrix), Path(out) if out else None)
    except Drift as exc:
        print(f"::error::upstream CI drift: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
