# ci-runner

Gitee pull requests on open-vela are built and tested here by the GitHub CI of
[open-vela/public-actions](https://github.com/open-vela/public-actions).
Jenkins sends a `repository_dispatch` (`gitee-pr-ci`), `.github/workflows/ci.yml`
runs, and Jenkins reports the result back to Gitee.

## How the GitHub CI is reused

| Part | Source |
|---|---|
| route: which workflow serves the target branch | dispatcher job conditions in `public-actions/ci.yml@dev` |
| matrix, runner, job timeout | that workflow's `ci-tasks` job |
| source tree | Gitee: manifest, snapshot, cherry-pick of the Gitee PRs |
| steps after `check cherry-pick pr result` | upstream, run by `tools/ci/upstream_ci.py` with GitHub's step semantics |

The upstream workflow is pinned to one public-actions commit per run. Added or
removed groups and tasks, task flags, step contents and timeouts are followed
with no change here.

Differences on purpose: the matrix runs with `fail-fast: false`, and executed
steps get no GitHub token.

## When it fails on purpose

The parse job fails before any build, naming the step, if upstream changes in a
way that cannot be followed faithfully:

- a new step before `check cherry-pick pr result`;
- an action other than `docker/login-action` or `actions/upload-artifact` after it;
- an expression Gitee has no value for (`vars`, `steps`, other `needs` outputs,
  a GitHub token in an executed step);
- a target branch matching no CI route, or several;
- a job-level `container`, `services`, `defaults`, or a non-literal `runs-on`.

`gitee_ci.sh` is not used by `ci.yml`.
