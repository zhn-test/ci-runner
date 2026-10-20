#!/usr/bin/env bash
# Static regression for the dynamic matrix/executor pair.
#
# Run from the repository root:  bash tools/ci/selftest.sh [upstream-yaml-dir]
#
# Covers the property the whole design rests on: a task or group added to or
# removed from the upstream workflow is picked up with no edit here, while
# anything we cannot reproduce faithfully fails loudly.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UPSTREAM_DIR="${1:-/tmp/ci-sync}"
MATRIX="python3 $ROOT/tools/ci/ci_matrix.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0 fail=0
ok()   { printf '  \033[0;32mok\033[0m    %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '  \033[0;31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
head2(){ printf '\n== %s\n' "$*"; }

# expect_ok <yaml> <desc> -- then compare stdout against expectations
expect_ok() {
  local f="$1" desc="$2"
  if out=$($MATRIX --workflow "$f" --format matrix 2>&1); then
    ok "$desc"
    printf '%s' "$out" > "$TMP/last.json"
    return 0
  fi
  bad "$desc -> $out"
  return 1
}

expect_drift() {
  local f="$1" desc="$2" needle="$3"
  if out=$($MATRIX --workflow "$f" --format matrix 2>&1); then
    bad "$desc (expected failure, got success)"
    return 1
  fi
  if printf '%s' "$out" | grep -q "$needle"; then
    ok "$desc"
  else
    bad "$desc (wrong message: $out)"
  fi
}

jqq() { python3 -c "import json,sys;d=json.load(open('$TMP/last.json'));print($1)"; }

# ---------------------------------------------------------------------------
head2 "1. real upstream workflows"
declare -a REAL=(
  "pa-dev-ci-real.yml:11:31"
  "pa-trunk-ci.yml:5:21"
  "pa-dev-ci-real-trunk55.yml:5:25"
)
for spec in "${REAL[@]}"; do
  f="${spec%%:*}"; rest="${spec#*:}"; wantg="${rest%%:*}"; wantt="${rest#*:}"
  path="$UPSTREAM_DIR/$f"
  if [ ! -f "$path" ]; then bad "$f missing under $UPSTREAM_DIR (skipped)"; continue; fi
  if expect_ok "$path" "$f parses"; then
    g=$(jqq "len(d['include'])")
    t=$(jqq "sum(len(e['tasks']) for e in d['include'])")
    [ "$g" = "$wantg" ] && ok "$f groups=$g" || bad "$f groups=$g want=$wantg"
    [ "$t" = "$wantt" ] && ok "$f tasks=$t"  || bad "$f tasks=$t want=$wantt"
  fi
done

# dev must carry the NTFC pins and exactly the upstream timeouts
if [ -f "$UPSTREAM_DIR/pa-dev-ci-real.yml" ]; then
  env_out=$($MATRIX --workflow "$UPSTREAM_DIR/pa-dev-ci-real.yml" --format env)
  grep -q '^CI_NTFC_SPEC=ntfc @ git+https://github.com/apache/nuttx-ntfc.git@9e17dbeacd' <<<"$env_out" \
    && ok "ntfc git pin extracted" || bad "ntfc pin: $(grep '^CI_NTFC_SPEC=' <<<"$env_out")"
  grep -q '^CI_PYTEST_SPEC=pytest==9.1.1$' <<<"$env_out" \
    && ok "pytest pin extracted" || bad "pytest pin: $(grep '^CI_PYTEST_SPEC=' <<<"$env_out")"
  grep -q '^CI_NTFC_TESTING_REF=release-0.0.2$' <<<"$env_out" \
    && ok "test-case ref extracted" || bad "testing ref: $(grep '^CI_NTFC_TESTING_REF=' <<<"$env_out")"
  grep -q '^CI_BUILD_FLAGS=-e -Werror$' <<<"$env_out" \
    && ok "build flags extracted without -j" || bad "build flags: $(grep '^CI_BUILD_FLAGS=' <<<"$env_out")"

  expect_ok "$UPSTREAM_DIR/pa-dev-ci-real.yml" "dev matrix reread" >/dev/null
  ntfc_groups=$(jqq "sum(1 for e in d['include'] if e['ntfc']=='true')")
  [ "$ntfc_groups" = "6" ] && ok "6 ntfc groups flagged" || bad "ntfc groups=$ntfc_groups want=6"
  jm=$(jqq "sorted({e['job_minutes'] for e in d['include']})")
  [ "$jm" = "[180, 360]" ] && ok "job bounds {180,360} match upstream" || bad "job bounds=$jm"
  sm=$(jqq "max(e['step_minutes'] for e in d['include'] if e['ntfc']=='true')")
  [ "$sm" = "130" ] && ok "ntfc step bound 130 matches upstream" || bad "step bound=$sm"
fi

# ---------------------------------------------------------------------------
head2 "2. task add / remove / rename needs no change here"
synth() {
  cat > "$TMP/$1" <<YAML
jobs:
  ci-tasks:
    strategy:
      matrix:
        group: [$2]
        include:
$3
    steps:
      - name: Run CI tasks inside Docker container
        run: |
          docker run --rm ghcr.io/open-vela/openvela-ci-linux_from_apache-nuttx:latest /bin/bash -c "./build.sh \$task \$cmake_flag -e -Werror -j8"
      - name: Run NTFC runtime tests
        run: |
          pip install "ntfc @ git+https://github.com/apache/nuttx-ntfc.git@deadbeef" pytest==9.1.1
          git clone --depth=1 --branch release-0.0.2 https://github.com/apache/nuttx-ntfc-testing.git x
YAML
}

synth added.yml "alpha" "          - group: alpha
            tasks:
              - one@cmake
              - two@cmake
              - three@cmake"
if expect_ok "$TMP/added.yml" "group with 3 tasks"; then
  [ "$(jqq "len(d['include'][0]['tasks'])")" = "3" ] && ok "3 tasks picked up" || bad "task count"
fi

synth removed.yml "alpha" "          - group: alpha
            tasks:
              - one@cmake"
if expect_ok "$TMP/removed.yml" "same group after a task is deleted"; then
  [ "$(jqq "len(d['include'][0]['tasks'])")" = "1" ] && ok "deletion picked up" || bad "task count"
fi

synth newgroup.yml "alpha, ntfc-brandnew" "          - group: alpha
            tasks:
              - one@cmake
          - group: ntfc-brandnew
            tasks:
              - newboard:citest@ntfc"
if expect_ok "$TMP/newgroup.yml" "brand-new ntfc group needs no edit"; then
  n=$(jqq "[e for e in d['include'] if e['group']=='ntfc-brandnew'][0]")
  python3 - "$TMP/last.json" <<'PY' && ok "new group flagged ntfc with 180/130 bounds" || bad "new group derived wrong"
import json,sys
d=json.load(open(sys.argv[1]))
e=[x for x in d['include'] if x['group']=='ntfc-brandnew'][0]
assert e['ntfc']=='true', e
assert e['job_minutes']==180 and e['step_minutes']==130, e
PY
fi

synth inline.yml "alpha" "          - group: alpha
            tasks: [one@cmake, two@cmake]"
if expect_ok "$TMP/inline.yml" "inline flow-sequence task list"; then
  [ "$(jqq "len(d['include'][0]['tasks'])")" = "2" ] && ok "inline list parsed" || bad "inline list"
fi

synth multintfc.yml "ntfc-multi" "          - group: ntfc-multi
            tasks:
              - a:citest@ntfc
              - b:citest@cmake@ntfc"
if expect_ok "$TMP/multintfc.yml" "two ntfc tasks in one group"; then
  python3 - "$TMP/last.json" <<'PY' && ok "bounds scale to 260/310" || bad "multi-ntfc bounds"
import json,sys
e=json.load(open(sys.argv[1]))['include'][0]
assert e['step_minutes']==260, e
assert e['job_minutes']==310, e
assert len(e['ntfc_tasks'])==2, e
PY
fi

# ---------------------------------------------------------------------------
head2 "3. drift must fail loudly, never degrade"
synth unknownflag.yml "alpha" "          - group: alpha
            tasks:
              - one@cmake@coverage"
expect_drift "$TMP/unknownflag.yml" "unknown flag rejected" "unknown flag"

synth mismatch.yml "alpha, beta" "          - group: alpha
            tasks:
              - one@cmake"
expect_drift "$TMP/mismatch.yml" "group list vs include mismatch rejected" "disagree"

cat > "$TMP/norun.yml" <<'YAML'
jobs:
  ci-tasks:
    strategy:
      matrix:
        group: [alpha]
        include:
          - group: alpha
            tasks: [one@cmake]
    steps:
      - name: Something else entirely
        run: echo hi
YAML
expect_drift "$TMP/norun.yml" "missing build step rejected" "Run CI tasks"

cat > "$TMP/nojob.yml" <<'YAML'
jobs:
  build-everything:
    steps:
      - run: echo hi
YAML
expect_drift "$TMP/nojob.yml" "renamed ci-tasks job rejected" "no 'ci-tasks' job"

synth nopin.yml "ntfc-x" "          - group: ntfc-x
            tasks:
              - a:citest@ntfc"
python3 - "$TMP/nopin.yml" <<'PY'
import re,sys,pathlib
p=pathlib.Path(sys.argv[1]); t=p.read_text()
t=t.replace('pytest==9.1.1','pytest')
p.write_text(t)
PY
expect_drift "$TMP/nopin.yml" "missing pytest pin rejected" "pytest pin"

# a step gated on a group whose tasks have no quirk entry
cat > "$TMP/newquirk.yml" <<'YAML'
jobs:
  ci-tasks:
    strategy:
      matrix:
        group: [ntfc-weird]
        include:
          - group: ntfc-weird
            tasks: [weird:citest@ntfc]
    steps:
      - name: Special sauce for weird
        if: ${{ matrix.group == 'ntfc-weird' }}
        run: echo doing something we do not model
      - name: Run CI tasks inside Docker container
        run: |
          docker run --rm ghcr.io/open-vela/openvela-ci-linux_from_apache-nuttx:latest /bin/bash -c "./build.sh $task $cmake_flag -e -Werror -j8"
      - name: Run NTFC runtime tests
        run: |
          pip install "ntfc @ git+https://github.com/apache/nuttx-ntfc.git@deadbeef" pytest==9.1.1
          git clone --depth=1 --branch release-0.0.2 https://github.com/apache/nuttx-ntfc-testing.git x
YAML
expect_drift "$TMP/newquirk.yml" "new group-gated step rejected" "quirks.conf"

# the real rv64 quirk is covered, so dev must NOT trip that guard
if [ -f "$UPSTREAM_DIR/pa-dev-ci-real.yml" ]; then
  if $MATRIX --workflow "$UPSTREAM_DIR/pa-dev-ci-real.yml" --format report | grep -q 'rv-virt:citest64'; then
    ok "rv64 toolchain quirk recognised as covered"
  else
    bad "rv64 quirk not reported as covered"
  fi
fi

# ---------------------------------------------------------------------------
head2 "4. flag parsing in the executor"
cat > "$TMP/flags.sh" <<'SH'
TASK_NAME=""; TASK_FLAGS=""
parse_task() {
  local raw="$1"
  TASK_NAME="${raw%%@*}"
  if [[ "$raw" == *@* ]]; then TASK_FLAGS="${raw#*@}"; else TASK_FLAGS=""; fi
}
has_flag() { [[ "@${TASK_FLAGS}@" == *"@$1@"* ]]; }
check() { # raw expect_name expect_cmake expect_ntfc
  parse_task "$1"
  local c=no n=no
  has_flag cmake && c=yes
  has_flag ntfc && n=yes
  if [ "$TASK_NAME" = "$2" ] && [ "$c" = "$3" ] && [ "$n" = "$4" ]; then
    echo "ok   $1 -> name=$TASK_NAME cmake=$c ntfc=$n"
  else
    echo "FAIL $1 -> name=$TASK_NAME cmake=$c ntfc=$n (want $2/$3/$4)"; exit 1
  fi
}
check 'goldfish-x86_64-ap@cmake'            'goldfish-x86_64-ap'   yes no
check 'sim:citest@ntfc'                     'sim:citest'           no  yes
check 'qemu-armv8a:citest@cmake@ntfc'       'qemu-armv8a:citest'   yes yes
check 'qemu-armv8a:citest_smp@cmake@ntfc'   'qemu-armv8a:citest_smp' yes yes
check 'rv-virt:citest64@ntfc'               'rv-virt:citest64'     no  yes
check 'plain-task'                          'plain-task'           no  no
check 'vendor/openvela/boards/x/configs/y@cmake' 'vendor/openvela/boards/x/configs/y' yes no
check 'tricky@ntfc2'                        'tricky'               no  no
check 'tricky@myntfc'                       'tricky'               no  no
check 'tricky@cmaker'                       'tricky'               no  no
SH
if out=$(bash "$TMP/flags.sh" 2>&1); then
  printf '%s\n' "$out" | sed 's/^/    /'
  ok "flag parsing: all cases"
else
  printf '%s\n' "$out" | sed 's/^/    /'
  bad "flag parsing"
fi

# every real upstream task must parse to a known flag set
if [ -f "$UPSTREAM_DIR/pa-dev-ci-real.yml" ]; then
  if $MATRIX --workflow "$UPSTREAM_DIR/pa-dev-ci-real.yml" --format matrix >/dev/null 2>&1; then
    ok "all 31 upstream dev tasks carry known flags only"
  else
    bad "an upstream dev task has an unknown flag"
  fi
fi

# ---------------------------------------------------------------------------
head2 "5. shell and workflow syntax"
for f in "$ROOT"/tools/ci/*.sh "$ROOT"/gitee_ci.sh; do
  [ -f "$f" ] || continue
  if bash -n "$f" 2>/dev/null; then ok "bash -n $(basename "$f")"; else bad "bash -n $(basename "$f")"; fi
done
if python3 -c "
import yaml,sys
d=yaml.safe_load(open('$ROOT/.github/workflows/ci.yml'))
assert 'jobs' in d, 'no jobs'
j=d['jobs']['ci-tasks']
assert isinstance(j['strategy']['matrix'], str), 'matrix must be an expression'
" 2>/dev/null; then ok "workflow ci.yml parses, matrix is dynamic"; else bad "workflow ci.yml"; fi

# no group-name literals left in the workflow
if grep -nE \"'(ntfc-[a-z0-9-]+|goldfish|aurix|flagchip)'\" "$ROOT/.github/workflows/ci.yml" >/dev/null 2>&1; then
  bad "workflow still contains hard-coded group names"
else
  ok "workflow has no hard-coded group names"
fi

# every shell block in the workflow is valid bash
python3 - "$ROOT/.github/workflows/ci.yml" <<'PY' > "$TMP/blocks.txt"
import yaml,sys,re,pathlib
d=yaml.safe_load(open(sys.argv[1]))
n=0
out=pathlib.Path('/tmp/_wf_blocks'); out.mkdir(exist_ok=True)
for jn,jb in d['jobs'].items():
    for i,s in enumerate(jb.get('steps') or []):
        r=s.get('run')
        if not r: continue
        n+=1
        # Neutralise ${{ }} so bash -n sees a placeholder, not a syntax error.
        txt=re.sub(r'\$\{\{[^}]*\}\}', 'EXPR', r)
        (out/f'{jn}_{i}.sh').write_text(txt)
print(n)
PY
nblocks=$(cat "$TMP/blocks.txt")
bad_blocks=0
for b in /tmp/_wf_blocks/*.sh; do bash -n "$b" 2>/dev/null || { echo "    bad block: $b"; bad_blocks=$((bad_blocks+1)); }; done
rm -rf /tmp/_wf_blocks
[ "$bad_blocks" = "0" ] && ok "$nblocks workflow shell blocks pass bash -n" || bad "$bad_blocks workflow shell block(s) fail bash -n"

# ---------------------------------------------------------------------------
printf '\n== summary: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
