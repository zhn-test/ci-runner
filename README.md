# ci-runner

Gitee PR CI for openvela. Jenkins (`gitee-ci-proxy`) triggers this repository
over `repository_dispatch`, polls it and writes the result back to the Gitee PR,
so no Gitee credentials exist on the GitHub side.

## The task list is not maintained here

Build tasks are read at run time out of the GitHub CI workflow in
`open-vela/public-actions`, so a task or a whole group added or removed there is
picked up on the next Gitee run with no change in this repository.

| PR target branch | upstream workflow mirrored |
|---|---|
| `dev` | `public-actions@dev` `.github/workflows/ci-real.yml` |
| `dev-*` | `public-actions@dev` `.github/workflows/ci-real_trunk-5.5.yml` |
| anything else | `public-actions@trunk` `.github/workflows/ci.yml` |

## What a task means

Behaviour comes from the task string, never from the group name:

| task | build | runtime test |
|---|---|---|
| `goldfish-x86_64-ap@cmake` | CMake | none |
| `sim:citest@ntfc` | Make | NTFC |
| `qemu-armv8a:citest@cmake@ntfc` | CMake | NTFC |
| a name ending in `-citest` | as above | additionally the OpenVela autotest suite |

Flags are matched as whole tokens, so `@ntfc` does not match `ntfc2` or
`myntfc`. Per-group job and step timeouts are derived from how many NTFC tasks
the group carries; a group with one NTFC task gets the same 180/130 minute
bounds GitHub CI uses.

The image, the `build.sh` flags and the NTFC versions (`ntfc`, `pytest`, the
`nuttx-ntfc-testing` ref) are also read out of the upstream workflow, so version
bumps upstream do not need a change here either.

## Drift fails the run instead of degrading it

`tools/ci/ci_matrix.py` stops the run, with a message naming the problem, when:

- a task carries a flag that `tools/ci/task_exec.sh` does not implement;
- the upstream `ci-tasks` job, its matrix, or its build step is restructured;
- a version pin cannot be located;
- `matrix.group` and the `include` blocks disagree;
- upstream gates a step on a literal group name whose tasks are not recorded in
  `tools/ci/quirks.conf`.

That last one is how the RISC-V toolchain swap for `rv-virt:citest64` is
tracked. The alternative — quietly building a task as if the unknown flag or the
extra step were absent — is what this structure exists to prevent.

## Layout

| path | role |
|---|---|
| `.github/workflows/ci.yml` | dispatch entry point; one job per upstream group |
| `tools/ci/ci_matrix.py` | derives the matrix, the capabilities and the pins |
| `tools/ci/task_exec.sh` | `build` / `ntfc` / `collect` for one group |
| `tools/ci/quirks.conf` | behaviour upstream gates on a group name |
| `tools/ci/selftest.sh` | static regression for all of the above |
| `gitee_ci.sh` | Gitee comment/label layer; delegates to the same two tools |

## Running the checks

```bash
mkdir -p /tmp/ci-sync
for f in ci-real.yml ci-real_trunk-5.5.yml; do
  curl -fsSL -o "/tmp/ci-sync/pa-dev-${f}" \
    "https://raw.githubusercontent.com/open-vela/public-actions/dev/.github/workflows/${f}"
done
mv /tmp/ci-sync/pa-dev-ci-real_trunk-5.5.yml /tmp/ci-sync/pa-dev-ci-real-trunk55.yml
curl -fsSL -o /tmp/ci-sync/pa-trunk-ci.yml \
  https://raw.githubusercontent.com/open-vela/public-actions/trunk/.github/workflows/ci.yml

bash tools/ci/selftest.sh /tmp/ci-sync
```

To see what the current upstream resolves to without running anything:

```bash
python3 tools/ci/ci_matrix.py --workflow /tmp/ci-sync/pa-dev-ci-real.yml --format report
```
