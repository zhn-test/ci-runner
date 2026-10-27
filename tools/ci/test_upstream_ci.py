#!/usr/bin/env python3
"""Tests for tools/ci/upstream_ci.py.

  python3 tools/ci/test_upstream_ci.py            against testdata/ snapshots
  UPSTREAM_LIVE=1 python3 tools/ci/test_upstream_ci.py   also against current upstream

The end-to-end cases run the real upstream ci-tasks steps from the snapshot,
with docker and sudo replaced by stubs, so they exercise GITHUB_ENV hand-off
between steps, the if/always()/success() logic and the artifact decision
exactly as the upstream workflow defines them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import upstream_ci as uc  # noqa: E402

DATA = HERE / "testdata"
DEV = DATA / "dev-ci-real.yml"
DEV55 = DATA / "dev-ci-real_trunk-5.5.yml"
TRUNK = DATA / "trunk-ci.yml"
DISPATCHER = DATA / "dev-ci.yml"


def ev(**ctx):
    base = uc.gitee_contexts(ctx.pop("matrix", {}), {})
    base.update(ctx)
    return uc.Evaluator(base, ctx.get("_status", "success"))


class Expressions(unittest.TestCase):
    def test_upstream_job_timeout(self):
        expr = "${{ startsWith(matrix.group, 'ntfc') && 180 || 360 }}"
        self.assertEqual(uc.interpolate(expr, ev(matrix={"group": "ntfc-sim"})), 180)
        self.assertEqual(uc.interpolate(expr, ev(matrix={"group": "goldfish"})), 360)

    def test_archive_condition(self):
        cond = "${{ success() || (!cancelled() && startsWith(matrix.group, 'ntfc')) }}"
        for group, status, want in [("goldfish", "success", True), ("goldfish", "failure", False),
                                    ("ntfc-sim", "failure", True), ("ntfc-sim", "success", True)]:
            e = uc.Evaluator(uc.gitee_contexts({"group": group}, {}), status)
            self.assertEqual(uc.eval_if(cond, e), want, (group, status))

    def test_implicit_success(self):
        e = uc.Evaluator(uc.gitee_contexts({"group": "ntfc-rv64"}, {}), "failure")
        self.assertFalse(uc.eval_if("${{ matrix.group == 'ntfc-rv64' }}", e))
        self.assertTrue(uc.eval_if("always() && matrix.group == 'ntfc-rv64'", e))

    def test_github_semantics(self):
        e = ev()
        self.assertTrue(uc.evaluate("'ABC' == 'abc'", e))
        self.assertTrue(uc.evaluate("1 == '1'", e))
        self.assertTrue(uc.evaluate("null == 0", e))
        self.assertEqual(uc.evaluate("'' || 'x'", e), "x")
        self.assertEqual(uc.evaluate("format('{0}-{1}', 'a', 2)", e), "a-2")
        self.assertEqual(uc.interpolate("${{ join(matrix.tasks, ' ') }}",
                                        ev(matrix={"tasks": ["a@cmake", "b"]})), "a@cmake b")

    def test_unknown_context_is_drift(self):
        for expr in ("vars.FOO", "steps.x.outputs.y", "needs.setup.outputs.ALL_PR",
                     "secrets.OTHER", "hashFiles('x')"):
            with self.assertRaises(uc.Drift, msg=expr):
                uc.evaluate(expr, ev())

    def test_env_missing_is_null(self):
        e = ev(env={"A": "1"})
        self.assertEqual(uc.evaluate("env.A", e), "1")
        self.assertIsNone(uc.evaluate("env.B", e))

    def test_github_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "env"
            p.write_text("A=1\nB<<EOF\nx\ny\nEOF\nA=2\nC=k=v\n")
            self.assertEqual(uc.read_env_file(p), {"A": "2", "B": "x\ny", "C": "k=v"})


class Matrix(unittest.TestCase):
    def entries(self, path):
        doc = uc.load_workflow(path)
        return uc.expand_matrix(uc.ci_job(doc), lambda c: uc.Evaluator(uc.gitee_contexts(c, {})))

    def test_snapshots(self):
        for path, groups, tasks, ntfc in [(DEV, 12, 32, 7), (DEV55, 5, 25, 0), (TRUNK, 5, 21, 0)]:
            e = self.entries(path)
            self.assertEqual(len(e), groups, path)
            self.assertEqual(sum(len(x["tasks"]) for x in e), tasks, path)
            self.assertEqual(sum(x["gitee_job_minutes"] == 180 for x in e), ntfc, path)
            self.assertTrue(all(x["gitee_label"] == x["group"] for x in e))

    def test_added_group_needs_no_change(self):
        doc = uc.load_workflow(DEV)
        m = uc.ci_job(doc)["strategy"]["matrix"]
        m["group"].append("ntfc-new")
        m["include"].append({"group": "ntfc-new", "tasks": ["new:citest@whatever"]})
        e = uc.expand_matrix(uc.ci_job(doc), lambda c: uc.Evaluator(uc.gitee_contexts(c, {})))
        self.assertEqual(e[-1]["group"], "ntfc-new")
        self.assertEqual(e[-1]["gitee_job_minutes"], 180)

    def test_include_without_group_list(self):
        job = {"strategy": {"matrix": {"include": [{"group": "a", "tasks": ["x"]}]}}}
        e = uc.expand_matrix(job, lambda c: uc.Evaluator(uc.gitee_contexts(c, {})))
        self.assertEqual([x["group"] for x in e], ["a"])


class Routing(unittest.TestCase):
    def test_routes(self):
        for branch, path, ref in [("dev", "ci-real.yml", "dev"),
                                  ("dev-ai-contest-2026", "ci-real_trunk-5.5.yml", "dev"),
                                  ("trunk", "ci.yml", "trunk"),
                                  ("rebase_dev_trunk_55", "ci.yml", "trunk")]:
            r = uc.resolve_route(DISPATCHER, branch)
            self.assertEqual((Path(r["CI_UPSTREAM_PATH"]).name, r["CI_UPSTREAM_REF"]), (path, ref))
        with self.assertRaises(uc.Drift):
            uc.resolve_route(DISPATCHER, "unknown")


class Plan(unittest.TestCase):
    def check(self, doc):
        return uc.check_plan(doc, uc._contexts_for_check)

    def test_snapshots_pass(self):
        for path in (DEV, DEV55, TRUNK):
            kinds = [k for k, _ in self.check(uc.load_workflow(path))]
            self.assertEqual(kinds[-1], "upload")
            self.assertIn("run", kinds)

    def mutate(self, fn):
        doc = uc.load_workflow(DEV)
        fn(uc.ci_job(doc))
        with self.assertRaises(uc.Drift):
            self.check(doc)

    def test_drift_cases(self):
        steps = lambda j: j["steps"]  # noqa: E731
        idx = lambda j, name: [s.get("name") for s in j["steps"]].index(name)  # noqa: E731
        cases = {
            "new source-preparation step":
                lambda j: steps(j).insert(2, {"name": "Prime cache", "run": "true"}),
            "boundary renamed":
                lambda j: steps(j)[idx(j, uc.BOUNDARY_STEP)].update(name="check result"),
            "unknown action after boundary":
                lambda j: steps(j).insert(-1, {"uses": "foo/bar@v1"}),
            "unsupported shell":
                lambda j: steps(j).insert(-1, {"run": "print(1)", "shell": "python"}),
            "unknown context":
                lambda j: steps(j).insert(-1, {"run": "echo ${{ vars.X }}"}),
            "github-only setup output":
                lambda j: steps(j).insert(-1, {"run": "echo ${{ needs.setup.outputs.ALL_PR }}"}),
            "reads env only GitHub prep sets":
                lambda j: steps(j).insert(-1, {"run": "echo $CP_PR_CODE"}),
            "upload not last":
                lambda j: steps(j).append({"run": "true"}),
            "job container":
                lambda j: j.update(container="ubuntu"),
            "expression runs-on":
                lambda j: j.update({"runs-on": "${{ matrix.os }}"}),
        }
        for name, fn in cases.items():
            with self.subTest(name):
                self.mutate(fn)


# ---------------------------------------------------------------------------
# End to end: the real upstream steps, stubbed docker and sudo.
# ---------------------------------------------------------------------------
DOCKER_STUB = r"""#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  pull|login|rm|builder|image) exit 0 ;;
esac
all="$*"
if [[ "$all" == *"--name"* ]]; then
  # NTFC runtime container.
  case "${STUB_NTFC:-pass}" in
    pass) exit 0 ;;
    fail) mkdir -p nuttx/result && echo console > nuttx/result/console.log; exit 1 ;;
    hang) mkdir -p nuttx/result && echo partial > nuttx/result/console.log
          echo dbg > nuttx/pytest.debug.log; sleep 600 ;;
  esac
fi
if [[ "$all" == *"./build.sh"* ]]; then
  [ "${STUB_BUILD:-pass}" = pass ] || exit 2
  mkdir -p nuttx && echo elf > nuttx/nuttx && echo cfg > nuttx/.config
  if [[ "$all" == *"--cmake"* ]]; then
    mkdir -p cmake_out/b && echo elf > cmake_out/b/nuttx && echo cfg > cmake_out/b/.config
  fi
fi
exit 0
"""

# Only mv/chown inside the workspace are executed; everything else (apt, rm of
# system paths, systemctl, repo) is logged and skipped.
SUDO_STUB = r"""#!/bin/bash
echo "sudo $*" >> "$STUB_LOG"
[ "$1" = "-E" ] && shift
case "$1" in
  mv|chown)
    for a in "${@:2}"; do
      case "$a" in -*|*:*) continue ;; /*) [[ "$a" == "$GITHUB_WORKSPACE"* ]] || exit 0 ;; esac
    done
    exec "$@" ;;
esac
exit 0
"""


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upstream-ci-"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        stub = self.tmp / "bin"
        stub.mkdir()
        for name, body in (("docker", DOCKER_STUB), ("sudo", SUDO_STUB)):
            (stub / name).write_text(body)
            (stub / name).chmod(0o755)
        self.log = self.tmp / "stub.log"
        self.out = self.tmp / "out"
        self.env = {
            **os.environ,
            "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_WORKSPACE": str(self.ws),
            "RUNNER_TEMP": str(self.tmp),
            "GITHUB_RUN_ID": "77", "GITHUB_RUN_ATTEMPT": "2",
            "GITEE_PR_ID": "10", "GITEE_PR_BRANCH": "dev", "GITEE_TRIGGER_REPO": "nuttx",
            "GITHUB_OUTPUT": str(self.out),
            "STUB_LOG": str(self.log),
        }
        # Board metadata the upstream NTFC step looks for.
        for board, cfg in (("sim", "citest"), ("rv-virt", "citest64")):
            d = self.ws / "nuttx/boards/arch/chip" / board / "configs" / cfg
            d.mkdir(parents=True)
            (d / "config.yaml").write_text("x: 1\n")
            (d / "run.sh").write_text("#!/bin/sh\n")
            (d / "run.sh").chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_group(self, path, group, **extra):
        doc = uc.load_workflow(path)
        entry = next(e for e in uc.expand_matrix(
            uc.ci_job(doc), lambda c: uc.Evaluator(uc.gitee_contexts(c, {}))) if e["group"] == group)
        p = subprocess.run(
            [sys.executable, str(HERE / "upstream_ci.py"), "run", "--workflow", str(path),
             "--matrix", json.dumps(entry)],
            env={**self.env, **extra}, cwd=self.ws, capture_output=True, text=True, timeout=300)
        outputs = uc.read_env_file(self.out) if self.out.exists() else {}
        log = self.log.read_text() if self.log.exists() else ""
        return p, outputs, log

    def test_build_group(self):
        p, out, log = self.run_group(DEV, "goldfish")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        builds = [l for l in log.splitlines() if "./build.sh" in l]
        self.assertEqual(len(builds), 3)
        self.assertTrue(all("--cmake -e -Werror -j8" in l for l in builds))
        self.assertNotIn("--name", log)
        self.assertEqual((out["archive"], out["archive_name"]), ("true", "Build_Package_goldfish"))
        self.assertTrue((self.ws / "goldfish/goldfish-x86_64-ap_nuttx-10/nuttx").exists())

    def test_build_failure_skips_archive(self):
        p, out, _ = self.run_group(DEV, "goldfish", STUB_BUILD="fail")
        self.assertEqual(p.returncode, 1)
        self.assertEqual((out["status"], out["archive"]), ("failure", "false"))

    def test_ntfc_group(self):
        p, out, log = self.run_group(DEV, "ntfc-sim")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        build = next(l for l in log.splitlines() if "./build.sh" in l)
        self.assertIn("./build.sh sim:citest  -e -Werror -j8", build)
        self.assertIn("--name ntfc-77-2", log)
        self.assertIn("docker rm -f ntfc-77-2", log)
        self.assertIn("ARTIFACTCONFDIR=/workspace/ntfc-sim/sim_citest_nuttx-10", log)
        self.assertNotIn("riscv-none-elf", log)
        self.assertEqual(out["archive_name"], "Build_Package_ntfc-sim")

    def test_rv64_runs_its_group_gated_step(self):
        p, _, log = self.run_group(DEV, "ntfc-rv64")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("riscv-none-elf", log)

    def test_ntfc_failure_collects_and_archives(self):
        p, out, _ = self.run_group(DEV, "ntfc-sim", STUB_NTFC="fail")
        self.assertEqual(p.returncode, 1)
        self.assertEqual((out["status"], out["archive"]), ("failure", "true"))
        self.assertTrue((self.ws / "ntfc-sim/sim_citest_nuttx-10/ntfc/result/console.log").exists())

    def test_ntfc_step_timeout(self):
        # 130 "minutes" of 0.05 s: the upstream step timeout fires, the always()
        # collector still runs and the ntfc group still uploads.
        p, out, log = self.run_group(DEV, "ntfc-sim", STUB_NTFC="hang",
                                     UPSTREAM_CI_MINUTE_SECONDS="0.05")
        self.assertEqual(p.returncode, 1)
        self.assertIn("timed out after 130 min", p.stdout)
        self.assertIn("docker rm -f ntfc-77-2", log)
        self.assertEqual(out["archive"], "true")
        dest = self.ws / "ntfc-sim/sim_citest_nuttx-10/ntfc"
        self.assertTrue((dest / "result/console.log").exists())
        self.assertTrue((dest / "pytest.debug.log").exists())

    def test_dev_derived_workflow(self):
        p, out, log = self.run_group(DEV55, "sil")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(sum("./build.sh" in l for l in log.splitlines()), 4)
        self.assertNotIn("--name", log)
        self.assertEqual(out["archive"], "true")

    def test_trunk_workflow(self):
        p, out, log = self.run_group(TRUNK, "qemu")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(any("./build.sh" in l for l in log.splitlines()))
        self.assertEqual(out["archive"], "true")


@unittest.skipUnless(os.environ.get("UPSTREAM_LIVE"), "set UPSTREAM_LIVE=1")
class Live(unittest.TestCase):
    def test_current_upstream(self):
        base = "https://raw.githubusercontent.com/open-vela/public-actions"
        with tempfile.TemporaryDirectory() as d:
            disp = Path(d) / "dispatcher.yml"
            urllib.request.urlretrieve(f"{base}/dev/.github/workflows/ci.yml", disp)
            for branch in ("dev", "dev-x", "trunk"):
                r = uc.resolve_route(disp, branch)
                wf = Path(d) / f"{branch}.yml"
                urllib.request.urlretrieve(f"{base}/{r['CI_UPSTREAM_REF']}/{r['CI_UPSTREAM_PATH']}", wf)
                uc.check_plan(uc.load_workflow(wf), uc._contexts_for_check)


if __name__ == "__main__":
    unittest.main(verbosity=1)
