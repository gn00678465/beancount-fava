#!/bin/sh
# Manual mutation for files no mutation tool can instrument (Dockerfile,
# workflows, Renovate config). Each mutant must be killed by the named tests.
# Usage: tools/mutate.sh <report-file>
# rc 0 = every mutant killed; rc 1 = a mutant survived; rc 2 = the run itself broke.
set -u

REPORT=${1:?usage: tools/mutate.sh <report-file>}
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

printf '# Manual mutation\n\n| mutant | file | tests | pytest rc | result |\n|---|---|---|---|---|\n' > "$REPORT"
ran=0
survived=0

# mutant <id> <file> <sed expression> <pytest -k expression>
mutant() {
  id=$1 file=$2 expression=$3 tests=$4
  MUTATED_FILE=$file
  sed -i "$expression" "$file" || { echo "FAIL: sed broke on $id" >&2; exit 2; }
  if git diff --quiet -- "$file"; then
    echo "FAIL: mutant $id changed nothing in $file (fail closed)" >&2
    exit 2
  fi
  echo "=== mutant $id ==="
  uv run pytest -q -p no:randomly -k "$tests"
  rc=$?
  restore
  case $rc in
    1) result=killed ;;
    0) result=SURVIVED; survived=$((survived + 1)) ;;
    *) echo "FAIL: pytest rc=$rc on mutant $id is not a test failure" >&2; exit 2 ;;
  esac
  printf '| %s | %s | %s | %s | %s |\n' "$id" "$file" "$tests" "$rc" "$result" >> "$REPORT"
  ran=$((ran + 1))
}

mutant no-user Dockerfile '/^USER 1000:1000$/d' 'runs_as_non_root'
mutant loopback-host Dockerfile 's/FAVA_HOST=0\.0\.0\.0/FAVA_HOST=127.0.0.1/' 'fava_serves_on_published_port'
mutant no-tini Dockerfile '/^ENTRYPOINT \["tini", "--"\]$/d' 'stops_on_sigterm'
mutant with-dev-group Dockerfile 's/uv sync --locked --no-dev/uv sync --locked/' 'installed_packages_equal_lock'
mutant frozen-lock Dockerfile 's/uv sync --locked --no-dev/uv sync --frozen --no-dev/' 'build_fails_on_lock_drift'
mutant root-owned-ledger Dockerfile 's|install -d -o fava -g fava /ledger|install -d /ledger|' 'ledger_dir_empty_and_writable'
mutant no-cancel .github/workflows/publish.yaml 's/cancel-in-progress: true/cancel-in-progress: false/' 'only_publish_workflow_pushes'
mutant automerge-major renovate.json 's/"patch", "minor"/"major", "patch", "minor"/' 'renovate_automerge_allowlist'

expected=8
if [ "$ran" -ne "$expected" ]; then
  echo "FAIL: ran $ran mutants, expected $expected" >&2
  exit 2
fi
if [ "$survived" -ne 0 ]; then
  echo "FAIL: $survived mutant(s) survived; see $REPORT" >&2
  exit 1
fi
echo "=== mutation: $ran/$expected mutants killed ==="
