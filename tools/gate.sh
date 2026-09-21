#!/bin/sh
# Verification gate entry point: runs every layer in order, fails on the first
# broken one, and refuses to report success unless every expected layer ran.
# Usage: tools/gate.sh <base-ref>
set -eu

GATE_EXPECTED_LAYERS="source-state tests lint-format suite-health multi-arch-build secrets supply-chain mutation source-state-end"
GATE_COMPLETED_LAYERS=""
SCOPE=image
PLATFORMS=linux/amd64,linux/arm64
BUILDER=bf-gate

run_layer() {
  layer=$1
  shift
  case " $GATE_EXPECTED_LAYERS " in
    *" $layer "*) ;;
    *) echo "FAIL: unknown layer '$layer'" >&2; return 2 ;;
  esac
  case " $GATE_COMPLETED_LAYERS " in
    *" $layer "*) echo "FAIL: duplicate layer '$layer'" >&2; return 2 ;;
  esac
  printf '=== %s ===\n' "$layer"
  if "$@"; then
    GATE_COMPLETED_LAYERS="$GATE_COMPLETED_LAYERS $layer"
  else
    rc=$?
    printf "FAIL: layer '%s' failed (rc=%s)\n" "$layer" "$rc" >&2
    return "$rc"
  fi
}

finish_gate() {
  missing=0
  for layer in $GATE_EXPECTED_LAYERS; do
    case " $GATE_COMPLETED_LAYERS " in
      *" $layer "*) ;;
      *) echo "FAIL: missing layer '$layer'" >&2; missing=1 ;;
    esac
  done
  [ "$missing" -eq 0 ] || return 1
  echo "=== gate: every expected layer ran to completion ==="
}

# Layer functions run inside `if`, where `set -e` is off: every command a
# layer depends on is chained with `&&` so its failure is the layer's failure.

clean_tree() {
  status=$(git status --porcelain) || return 2
  if [ -n "$status" ]; then
    echo "working tree not clean:" >&2
    echo "$status" >&2
    return 1
  fi
}

lint_format() {
  uv run ruff check . &&
    uv run ruff format --check . &&
    docker run --rm -i hadolint/hadolint < Dockerfile &&
    docker run --rm -v "$REPO_MOUNT:/repo" -w /repo rhysd/actionlint
}

multi_arch_build() {
  if ! docker buildx inspect "$BUILDER" > /dev/null 2>&1; then
    docker buildx create --name "$BUILDER" --driver docker-container > /dev/null || return 2
  fi
  docker buildx build --builder "$BUILDER" --platform "$PLATFORMS" --output type=cacheonly .
}

# The tree is clean when this runs, so the git scan covers every tracked file
# and the whole history. A directory scan would also read .venv and .git.
secrets_scan() {
  docker run --rm -v "$REPO_MOUNT:/repo" ghcr.io/gitleaks/gitleaks:latest git --no-banner /repo
}

# PYSEC-2026-2447 (diskcache, via beanprice) has no fixed release; the spec's
# known limits record why it is accepted. Remove the ignore once one exists.
supply_chain() {
  uv export --locked --no-dev --no-emit-project --format requirements-txt -q \
    -o "$ARTIFACTS/requirements.txt" &&
    uv run pip-audit --disable-pip --require-hashes --ignore-vuln PYSEC-2026-2447 \
      -r "$ARTIFACTS/requirements.txt"
}

# Resolve and validate inputs before touching the previous run's artifacts.
BASE_REF=${1:?usage: tools/gate.sh <base-ref>}
cd "$(git rev-parse --show-toplevel)"
git rev-parse --verify --quiet "$BASE_REF^{commit}" > /dev/null || {
  echo "gate: base ref '$BASE_REF' does not resolve" >&2
  exit 2
}
for tool in git uv docker npx; do
  command -v "$tool" > /dev/null || { echo "gate: '$tool' not found" >&2; exit 2; }
done

# Docker needs a native path for bind mounts; Git Bash would rewrite POSIX ones.
export MSYS_NO_PATHCONV=1
if command -v cygpath > /dev/null; then
  REPO_MOUNT=$(cygpath -w "$PWD")
else
  REPO_MOUNT=$PWD
fi

ARTIFACTS=.gate/$SCOPE
rm -rf "$ARTIFACTS"
mkdir -p "$ARTIFACTS"

echo "gate: scope=$SCOPE base=$(git rev-parse "$BASE_REF") head=$(git rev-parse HEAD)"

run_layer source-state clean_tree
run_layer tests uv run pytest -q
run_layer lint-format lint_format
run_layer suite-health uv run pytest -q
run_layer multi-arch-build multi_arch_build
run_layer secrets secrets_scan
run_layer supply-chain supply_chain
run_layer mutation sh tools/mutate.sh "$ARTIFACTS/mutation.md"
run_layer source-state-end clean_tree
finish_gate
