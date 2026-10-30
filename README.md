# ci-runner

Gitee pull requests on open-vela are built and tested here. Jenkins sends a
`repository_dispatch` (`gitee-pr-ci`), `.github/workflows/ci.yml` runs, and
Jenkins reports the result back to Gitee.

## Tasks

The tasks are listed in `tools/ci/tasks.yml`, one set per target branch
family. Each set mirrors the `ci-tasks` matrix of an
[open-vela/public-actions](https://github.com/open-vela/public-actions)
workflow as of the commit recorded in the set. Each task states its
`config`, `build_system` (`make`/`cmake`) and `test_kind` (`none`/`ntfc`).

When the public-actions matrix or the NTFC versions change, update
`tools/ci/tasks.yml` and the recorded commit. The parse job compares the set
with upstream and only warns on a difference; it never blocks the run.

Build, NTFC runtime test, diagnostics collection and upload are separate
workflow steps, as in public-actions `ci-real.yml`.

`gitee_ci.sh` is not used by `ci.yml`.
