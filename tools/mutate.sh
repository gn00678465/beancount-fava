#!/bin/sh
# Manual mutation for files no mutation tool can instrument (Dockerfile,
# workflows, Renovate config). Each mutant must be killed by the named test.
# Usage: tools/mutate.sh <report-file>
# rc 0 = every mutant killed; rc 1 = a mutant survived; rc 2 = the run itself broke.
set -u

REPORT=${1:?usage: tools/mutate.sh <report-file>}
LOG=$REPORT.log
MUTATED_FILE=""

restore() {
  if [ -n "$MUTATED_FILE" ]; then
    git checkout -- "$MUTATED_FILE"
    MUTATED_FILE=""
  fi
}
trap restore EXIT INT TERM

if [ -n "$(git status --porcelain)" ]; then
  echo "FAIL: working tree not clean; mutation would mix with local edits" >&2
  exit 2
fi

printf '# Manual mutation\n\n| mutant | file | killed by |\n|---|---|---|\n' > "$REPORT"
: > "$LOG"
ran=0
survived=0

# mutant <id> <file> <sed expression> <test function>
# Killed means: the named test ended FAILED and nothing ended ERROR. A build or
# fixture error also makes pytest exit 1, and must not count as a kill.
mutant() {
  id=$1 file=$2 expression=$3 target=$4
  MUTATED_FILE=$file
  sed -i "$expression" "$file" || { echo "FAIL: sed broke on $id" >&2; exit 2; }
  if git diff --quiet -- "$file"; then
    echo "FAIL: mutant $id changed nothing in $file (fail closed)" >&2
    exit 2
  fi
  echo "=== mutant $id ==="
  uv run pytest -q -p no:randomly -rfE -k "$target" > "$LOG.current" 2>&1
  rc=$?
  cat "$LOG.current" >> "$LOG"
  restore
  if [ "$rc" -eq 0 ]; then
    result=SURVIVED
    survived=$((survived + 1))
  elif [ "$rc" -eq 1 ] && grep -q "^FAILED .*::$target" "$LOG.current" &&
    ! grep -q "^ERROR " "$LOG.current"; then
    result=$(grep "^FAILED .*::$target" "$LOG.current" | head -n 1 | cut -d' ' -f2)
  else
    echo "FAIL: mutant $id did not end in a FAILED $target (pytest rc=$rc); see $LOG" >&2
    exit 2
  fi
  printf '| %s | %s | %s |\n' "$id" "$file" "$result" >> "$REPORT"
  rm -f "$LOG.current"
  ran=$((ran + 1))
}

mutant no-user Dockerfile '/^USER 1000:1000$/d' test_runs_as_non_root
mutant no-fava-host Dockerfile '/^ENV FAVA_HOST=0\.0\.0\.0$/d' test_fava_serves_on_published_port
mutant no-tini Dockerfile '/^ENTRYPOINT \["tini", "--"\]$/d' test_stops_on_sigterm
mutant with-dev-group Dockerfile 's/uv sync --locked --no-dev/uv sync --locked/' test_installed_packages_equal_lock
mutant keep-system-pip Dockerfile '/^RUN python -m pip uninstall /d' test_installed_packages_equal_lock
mutant frozen-lock Dockerfile 's/uv sync --locked --no-dev/uv sync --frozen --no-dev/' test_build_fails_on_lock_drift
mutant root-owned-ledger Dockerfile 's|install -d -o fava -g fava /ledger|install -d /ledger|' test_ledger_dir_empty_and_writable
mutant no-concurrency .github/workflows/publish.yaml '/^concurrency:$/,/^$/d' test_only_publish_workflow_pushes
mutant ci-without-tests .github/workflows/ci.yaml '/^      - run: uv run pytest$/d' test_only_publish_workflow_pushes
mutant automerge-major renovate.json 's/"patch", "minor"/"major", "patch", "minor"/' test_renovate_automerge_allowlist

expected=10
if [ "$ran" -ne "$expected" ]; then
  echo "FAIL: ran $ran mutants, expected $expected" >&2
  exit 2
fi
if [ "$survived" -ne 0 ]; then
  echo "FAIL: $survived mutant(s) survived; see $REPORT" >&2
  exit 1
fi
echo "=== mutation: $ran/$expected mutants killed ==="
