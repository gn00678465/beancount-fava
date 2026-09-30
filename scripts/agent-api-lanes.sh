#!/usr/bin/env bash
# Live lanes and perf probes for docs/plans/agent-write-api.md (AGENT-1 guard, AGENT-2 writes, AGENT-3 MCP,
# AGENT-4 Skill and deploy docs).
#
#   images               build $HEAD_IMAGE from this checkout and $TRUNK_IMAGE from $TRUNK_REF
#   boot <n> [target]    start a JWKS stub and fava for worker <n>; target is head or trunk
#   jwt <n> <claims>     sign a JWT for worker <n>'s stub; optional key and kid follow
#   down <n>             remove worker <n>'s containers, network, and recording proxy
#   lane <n>             run guard lane <n> against $TARGET (head by default)
#   perf-guard           interleave trunk and head, 20 rounds each
#   all                  images, guard lanes 1-10, perf-guard
#   write-lane <n>       run write-endpoint lane <n> against $TARGET (head by default)
#   perf-write           interleave trunk and head home pages and head writes, 20 rounds each
#   write-all            images, write lanes 1-10, perf-write
#   mcp-lane <n>         run MCP lane <n> against $TARGET (head by default); lane 1 boots trunk and head
#   perf-mcp             interleave head REST and MCP writes, 20 rounds each, plus trunk REST for reference
#   mcp-all              images, MCP lanes 1-10, perf-mcp
#   skill-lane <n>       run Skill lane <n> against $TARGET (head by default); lane 1 boots trunk and head;
#                        lanes 9 and 10 deploy $HEAD_IMAGE from docs/agent-api.md and compose.example.yaml
#   perf-skill           interleave trunk and head claude -p bookings on a fresh ledger, 5 rounds each
#   skill-all            images, Skill lanes 1-10, perf-skill
#
# boot appends $LEDGER_EXTRA, when set, to the copied main.beancount.
# Container and network names start with $LANES_PREFIX (default lanes).
# Transcripts land in $LANES_OUT/worker-<n>/<slug>.txt (trunk runs: trunk-<slug>.txt).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
HEAD_IMAGE=${HEAD_IMAGE:-beancount-fava:lane}
TRUNK_IMAGE=${TRUNK_IMAGE:-beancount-fava:trunk}
TRUNK_REF=${TRUNK_REF:-main}
OUT=${LANES_OUT:-/tmp/swarm-agent-1}
TARGET=${TARGET:-head}
AUD=lane-aud
BFILE=agent
PREFIX=${LANES_PREFIX:-lanes}

image_for() { if [ "$1" = trunk ]; then echo "$TRUNK_IMAGE"; else echo "$HEAD_IMAGE"; fi; }
wdir() { echo "$OUT/worker-$1"; }
idir() { echo "$(wdir "$1")/$2"; }
net() { echo "$PREFIX-w$1"; }
stub() { echo "$PREFIX-w$1-jwks"; }
fava() { echo "$PREFIX-w$1-$2"; }
team_domain() { echo "http://$(stub "$1"):8000"; }
port() { cat "$(idir "$1" "$2")/port"; }
token() { cat "$(wdir "$1")/token"; }

py() {
  docker run --rm -i --entrypoint python -v "$(wdir "$1")/keys:/keys" "$HEAD_IMAGE" - "${@:2}"
}

keygen() {
  mkdir -p "$(wdir "$1")/keys"
  py "$1" <<'PY'
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

for name in ("old", "new", "rogue"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    open(f"/keys/{name}.pem", "wb").write(pem)
PY
}

publish_keys() {
  local n=$1
  shift
  local certs
  certs="$(wdir "$n")/jwks/cdn-cgi/access/certs"
  mkdir -p "$(dirname "$certs")"
  py "$n" "$@" >"$certs.tmp" <<'PY'
import json, sys
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

keys = []
for kid in sys.argv[1:]:
    key = load_pem_private_key(open(f"/keys/{kid}.pem", "rb").read(), None)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    keys.append({**jwk, "kid": kid, "alg": "RS256", "use": "sig"})
print(json.dumps({"keys": keys}))
PY
  mv "$certs.tmp" "$certs"
}

cmd_jwt() {
  local n=$1 claims=${2:-'{}'} key=${3:-new} kid=${4:-${3:-new}}
  py "$n" "$AUD" "$(team_domain "$n")" "$claims" "$key" "$kid" <<'PY'
import json, sys, time
import jwt

aud, iss, claims, key, kid = sys.argv[1:]
payload = {"aud": aud, "iss": iss, "exp": int(time.time()) + 600, **json.loads(claims)}
print(jwt.encode(payload, open(f"/keys/{key}.pem").read(), algorithm="RS256", headers={"kid": kid}))
PY
}

wait_url() {
  local container=$1 url=$2 auth=$3 deadline=$((SECONDS + 60))
  until curl -fsS -o /dev/null -H "$auth" "$url" 2>/dev/null; do
    if ((SECONDS > deadline)); then
      docker logs "$container" >&2
      echo "$url did not answer within 60s" >&2
      return 1
    fi
    sleep 0.5
  done
}

wait_ready() {
  wait_url "$(fava "$1" "$2")" "http://127.0.0.1:$(port "$1" "$2")/$BFILE/api/errors" "$(bearer "$1")"
}

ensure_stub() {
  local n=$1 w
  w=$(wdir "$n")
  docker network inspect "$(net "$n")" >/dev/null 2>&1 || docker network create "$(net "$n")" >/dev/null
  if [ ! -f "$w/token" ]; then
    mkdir -p "$w"
    head -c 24 /dev/urandom | base64 | tr -d '/+=' >"$w/token"
    keygen "$n"
    publish_keys "$n" old new
  fi
  if ! docker inspect "$(stub "$n")" >/dev/null 2>&1; then
    docker run -d --name "$(stub "$n")" --network "$(net "$n")" -v "$w/jwks:/srv:ro" \
      "$HEAD_IMAGE" python -u -m http.server 8000 -d /srv >/dev/null
    local deadline=$((SECONDS + 20))
    until docker logs "$(stub "$n")" 2>/dev/null | grep -q "Serving HTTP"; do
      ((SECONDS < deadline)) || { echo "JWKS stub did not start" >&2; return 1; }
      sleep 0.2
    done
  fi
}

cmd_boot() {
  local n=$1 target=${2:-head} d
  d=$(idir "$n" "$target")
  ensure_stub "$n"
  docker rm -f "$(fava "$n" "$target")" >/dev/null 2>&1 || true
  rm -rf "$d"
  mkdir -p "$d"
  cp -r "$REPO/tests/fixtures/agent-ledger" "$d/ledger"
  [ -z "${LEDGER_EXTRA:-}" ] || echo "$LEDGER_EXTRA" >>"$d/ledger/main.beancount"
  docker run -d --name "$(fava "$n" "$target")" --network "$(net "$n")" -p 127.0.0.1::5000 \
    -v "$d/ledger:/ledger" -v "$(wdir "$n")/token:/run/agent-token:ro" \
    -e BEANCOUNT_FILE=/ledger/main.beancount -e AGENT_API_TOKEN_FILE=/run/agent-token \
    -e CF_ACCESS_TEAM_DOMAIN="$(team_domain "$n")" -e CF_ACCESS_AUD="$AUD" \
    "$(image_for "$target")" >/dev/null
  docker port "$(fava "$n" "$target")" 5000/tcp | head -1 | sed 's/.*://' >"$d/port"
  wait_ready "$n" "$target"
}

cmd_down() {
  local n=$1
  recording_proxy_stop "$n"
  docker ps -aq --filter "name=^$PREFIX-w$n-" | xargs -r docker rm -f >/dev/null
  docker network rm "$(net "$n")" >/dev/null 2>&1 || true
}

TRANSCRIPT=/dev/null
FAILED=0

note() { echo "$*" >>"$TRANSCRIPT"; }

short() {
  local arg
  for arg in "$@"; do
    if [ ${#arg} -gt 72 ]; then printf '%s... ' "${arg:0:60}"; else printf '%q ' "$arg"; fi
  done
}

request() {
  local n=$1 target=$2 method=$3 path=$4
  shift 4
  local headers
  BODY="$(wdir "$n")/last-body"
  headers="$(wdir "$n")/last-headers"
  STATUS=$(curl -sS -o "$BODY" -D "$headers" -w '%{http_code}' -X "$method" "$@" \
    "http://127.0.0.1:$(port "$n" "$target")$path")
  {
    echo "\$ curl -X $method $(short "$@")http://127.0.0.1:<$target>$path"
    grep -iE '^(HTTP/|www-authenticate|location|content-type|allow:)' "$headers" | tr -d '\r' | sed 's/^/< /'
    echo "< body: $(wc -c <"$BODY") bytes, sha256 $(sha256sum "$BODY" | cut -c1-16)"
    head -c 1200 "$BODY" | tr -d '\r'
    echo
  } >>"$TRANSCRIPT"
}

check() {
  local what=$1 actual=$2 expected=$3
  if [ "$actual" = "$expected" ]; then
    note "CHECK ok   $what: $actual"
  else
    note "CHECK FAIL $what: got $actual, want $expected"
    FAILED=1
  fi
}

ledger_sha() { sha256sum "$(idir "$1" "$2")/ledger/main.beancount" | cut -d' ' -f1; }

start_lane() {
  local n=$1 slug=$2 name
  name=$slug
  [ "$TARGET" = trunk ] && name="trunk-$slug"
  mkdir -p "$(wdir "$n")"
  TRANSCRIPT="$(wdir "$n")/$name.txt"
  FAILED=0
  {
    echo "# lane $n ($slug) target=$TARGET image=$(image_for "$TARGET")"
    echo "# image id $(docker image inspect --format '{{.Id}}' "$(image_for "$TARGET")")"
    echo "# $(date -u +%FT%TZ)"
  } >"$TRANSCRIPT"
  cmd_down "$n"
  rm -rf "$(wdir "$n")/token" "$(wdir "$n")/keys" "$(wdir "$n")/jwks"
}

end_lane() {
  local n=$1 result=PASS
  [ "$FAILED" = 0 ] || result=FAIL
  note "RESULT: $result"
  cmd_down "$n"
  echo "lane $n target=$TARGET: $result  $TRANSCRIPT"
}

bearer() { echo "Authorization: Bearer $(token "$1")"; }
assertion() { echo "Cf-Access-Jwt-Assertion: $1"; }

lane1() {
  start_lane 1 lane1-regression
  cmd_boot 1 trunk
  cmd_boot 1 head
  request 1 trunk GET "/$BFILE/api/ledger_data"
  check "trunk without credential" "$STATUS" 200
  request 1 head GET "/$BFILE/api/ledger_data"
  check "head without credential" "$STATUS" 401
  end_lane 1
}

lane2() {
  start_lane 2 lane2-agent-read
  cmd_boot 2 trunk
  cmd_boot 2 "$TARGET"
  local query=(-G --data-urlencode 'query_string=SELECT account, sum(position) GROUP BY account')
  local path want
  sha_without_load_noise() {
    if [ "$1" = ledger_data ]; then
      jq -S 'del(.data.errors, .data.extensions)' "$BODY" | sha256sum | cut -d' ' -f1
    else
      sha256sum "$BODY" | cut -d' ' -f1
    fi
  }
  note "ledger_data is compared without .data.errors and .data.extensions; query byte for byte"
  for path in ledger_data query; do
    local args=()
    [ "$path" = query ] && args=("${query[@]}")
    request 2 trunk GET "/$BFILE/api/$path" "${args[@]}"
    want=$(sha_without_load_noise "$path")
    request 2 "$TARGET" GET "/$BFILE/api/$path" -H "$(bearer 2)" "${args[@]}"
    check "$path with token" "$STATUS" 200
    check "$path body equals trunk without guard" "$(sha_without_load_noise "$path")" "$want"
  done
  end_lane 2
}

lane3() {
  start_lane 3 lane3-agent-write-denied
  cmd_boot 3 "$TARGET"
  local before json=(-H "$(bearer 3)" -H 'Content-Type: application/json')
  before=$(ledger_sha 3 "$TARGET")
  note "ledger sha256 before: $before"
  request 3 "$TARGET" PUT "/$BFILE/api/source" "${json[@]}" \
    -d "{\"file_path\":\"/ledger/main.beancount\",\"source\":\"; wiped\\n\",\"sha256sum\":\"$before\"}"
  check "PUT /api/source" "$STATUS" 403
  request 3 "$TARGET" PUT "/$BFILE/api/add_entries" "${json[@]}" -d '{"entries":[{"t":"Transaction","date":"2026-09-21","flag":"*","payee":"","narration":"agent-put","tags":[],"links":[],"meta":{},"postings":[{"account":"Expenses:Food:Dinner","amount":"1 TWD"},{"account":"Assets:TW:Cash","amount":"-1 TWD"}]}]}'
  check "PUT /api/add_entries" "$STATUS" 403
  request 3 "$TARGET" DELETE "/$BFILE/api/source_slice" -H "$(bearer 3)" \
    -G --data-urlencode "entry_hash=x" --data-urlencode "sha256sum=$before"
  check "DELETE /api/source_slice" "$STATUS" 403
  note "ledger sha256 after:  $(ledger_sha 3 "$TARGET")"
  check "ledger unchanged" "$(ledger_sha 3 "$TARGET")" "$before"
  end_lane 3
}

lane4() {
  start_lane 4 lane4-agent-ui-denied
  cmd_boot 4 "$TARGET"
  request 4 "$TARGET" GET "/$BFILE/income_statement/" -H "$(bearer 4)"
  check "income_statement with token" "$STATUS" 403
  request 4 "$TARGET" GET "/$BFILE/editor/" -H "$(bearer 4)"
  check "editor with token" "$STATUS" 403
  end_lane 4
}

lane5() {
  start_lane 5 lane5-browser
  cmd_boot 5 "$TARGET"
  local jwt source
  jwt=$(cmd_jwt 5)
  note "JWT claims: aud=$AUD iss=$(team_domain 5) kid=new"
  request 5 "$TARGET" GET "/" -L -H "$(assertion "$jwt")"
  check "home page (after redirect)" "$STATUS" 200
  request 5 "$TARGET" GET "/$BFILE/api/ledger_data" -H "$(assertion "$jwt")"
  check "/api/ledger_data" "$STATUS" 200
  source=$(python3 -c 'import json,sys; print(json.dumps({"source": open(sys.argv[1]).read()}))' \
    "$(idir 5 "$TARGET")/ledger/main.beancount")
  request 5 "$TARGET" PUT "/$BFILE/api/format_source" -H "$(assertion "$jwt")" \
    -H 'Content-Type: application/json' -d "$source"
  check "PUT /api/format_source" "$STATUS" 200
  end_lane 5
}

lane6() {
  start_lane 6 lane6-jwt-invalid
  cmd_boot 6 "$TARGET"
  local jwt
  request 6 "$TARGET" GET "/$BFILE/income_statement/" -H "$(assertion "$(cmd_jwt 6)")"
  check "control: valid JWT" "$STATUS" 200
  jwt=$(cmd_jwt 6 '{"aud":"other-aud"}')
  request 6 "$TARGET" GET "/$BFILE/income_statement/" -H "$(assertion "$jwt")"
  check "wrong aud" "$STATUS" 401
  jwt=$(cmd_jwt 6 '{"iss":"https://other.cloudflareaccess.com"}')
  request 6 "$TARGET" GET "/$BFILE/income_statement/" -H "$(assertion "$jwt")"
  check "wrong iss" "$STATUS" 401
  jwt=$(cmd_jwt 6 "{\"exp\":$(($(date +%s) - 60))}")
  request 6 "$TARGET" GET "/$BFILE/income_statement/" -H "$(assertion "$jwt")"
  check "expired" "$STATUS" 401
  jwt=$(cmd_jwt 6 '{}' rogue new)
  request 6 "$TARGET" GET "/$BFILE/income_statement/" -H "$(assertion "$jwt")"
  check "signed by a key outside the JWKS, kid=new" "$STATUS" 401
  end_lane 6
}

lane7() {
  start_lane 7 lane7-key-rotation
  cmd_boot 7 "$TARGET"
  local jwt
  jwt=$(cmd_jwt 7 '{}' old)
  note "JWKS serves kids: old new"
  request 7 "$TARGET" GET "/$BFILE/income_statement/" -H "$(assertion "$jwt")"
  check "JWT signed with old key" "$STATUS" 200
  publish_keys 7 new
  note "JWKS now serves kids: new; restarting fava"
  docker restart "$(fava 7 "$TARGET")" >/dev/null
  docker port "$(fava 7 "$TARGET")" 5000/tcp | head -1 | sed 's/.*://' >"$(idir 7 "$TARGET")/port"
  wait_ready 7 "$TARGET"
  request 7 "$TARGET" GET "/$BFILE/income_statement/" -H "$(assertion "$jwt")"
  check "same JWT after old key removed" "$STATUS" 401
  request 7 "$TARGET" GET "/$BFILE/income_statement/" -H "$(assertion "$(cmd_jwt 7 '{}' new)")"
  check "control: JWT signed with new key" "$STATUS" 200
  end_lane 7
}

lane8() {
  start_lane 8 lane8-jwks-down
  cmd_boot 8 "$TARGET"
  local jwt
  jwt=$(cmd_jwt 8)
  docker stop "$(stub 8)" >/dev/null
  note "JWKS stub stopped before fava fetched any key"
  request 8 "$TARGET" GET "/$BFILE/income_statement/" -H "$(assertion "$jwt")"
  check "valid JWT while JWKS is down" "$STATUS" 401
  request 8 "$TARGET" GET "/$BFILE/api/errors" -H "$(bearer 8)"
  check "token while JWKS is down" "$STATUS" 200
  note "fava log:"
  docker logs "$(fava 8 "$TARGET")" 2>&1 | grep -i "access keys" | head -3 >>"$TRANSCRIPT" || true
  end_lane 8
}

lane9() {
  start_lane 9 lane9-fail-closed
  local d name code
  d=$(idir 9 "$TARGET")
  name=$(fava 9 "$TARGET")
  mkdir -p "$d"
  cp -r "$REPO/tests/fixtures/agent-ledger" "$d/ledger"
  note "\$ docker run -d -v <ledger>:/ledger -e BEANCOUNT_FILE=/ledger/main.beancount <image>"
  docker run -d --name "$name" -v "$d/ledger:/ledger" -e BEANCOUNT_FILE=/ledger/main.beancount \
    "$(image_for "$TARGET")" >/dev/null
  local deadline=$((SECONDS + 20))
  while [ "$(docker inspect --format '{{.State.Running}}' "$name")" = true ] && ((SECONDS < deadline)); do
    sleep 0.5
  done
  note "running after start: $(docker inspect --format '{{.State.Running}}' "$name")"
  code=$(docker inspect --format '{{.State.ExitCode}}' "$name")
  note "exit code: $code"
  note "log:"
  docker logs "$name" >>"$TRANSCRIPT" 2>&1
  check "still running after 20s" "$(docker inspect --format '{{.State.Running}}' "$name")" false
  check "exit code is non-zero" "$([ "$code" != 0 ] && echo yes || echo no)" yes
  local var
  for var in AGENT_API_TOKEN_FILE CF_ACCESS_TEAM_DOMAIN CF_ACCESS_AUD; do
    check "log names $var" "$(docker logs "$name" 2>&1 | grep -q "$var" && echo yes || echo no)" yes
  done
  end_lane 9
}

lane10() {
  start_lane 10 lane10-no-bypass
  cmd_boot 10 "$TARGET"
  local path
  for path in "/$BFILE/api/changed" "/$BFILE/api/errors" /static/app.js; do
    request 10 "$TARGET" GET "$path"
    check "$path without credential" "$STATUS" 401
  done
  end_lane 10
}

median() { sort -n | awk '{a[NR]=$1} END {print (NR%2 ? a[(NR+1)/2] : (a[NR/2]+a[NR/2+1])/2)}'; }

cmd_perf_guard() {
  local n=perf rounds=20 jwt url_trunk url_head i
  TRANSCRIPT="$(wdir $n)/perf-guard.txt"
  cmd_down $n
  rm -rf "$(wdir $n)"
  mkdir -p "$(wdir $n)"
  cmd_boot $n trunk
  cmd_boot $n head
  jwt=$(cmd_jwt $n)
  url_trunk="http://127.0.0.1:$(port $n trunk)/$BFILE/income_statement/"
  url_head="http://127.0.0.1:$(port $n head)/$BFILE/income_statement/"
  for i in 1 2 3; do
    curl -fsS -o /dev/null "$url_trunk"
    curl -fsS -o /dev/null -H "$(assertion "$jwt")" "$url_head"
  done
  : >"$(wdir $n)/trunk.times"
  : >"$(wdir $n)/head.times"
  for ((i = 1; i <= rounds; i++)); do
    curl -fsS -o /dev/null -w '%{time_total}\n' "$url_trunk" >>"$(wdir $n)/trunk.times"
    curl -fsS -o /dev/null -w '%{time_total}\n' -H "$(assertion "$jwt")" "$url_head" \
      >>"$(wdir $n)/head.times"
  done
  local trunk_median head_median result
  trunk_median=$(median <"$(wdir $n)/trunk.times")
  head_median=$(median <"$(wdir $n)/head.times")
  result=$(awk -v h="$head_median" -v t="$trunk_median" 'BEGIN {print (h <= 1.10 * t) ? "PASS" : "FAIL"}')
  {
    echo "# perf-guard: GET /$BFILE/income_statement/, trunk without credential, head with a JWT"
    echo "# trunk image $(docker image inspect --format '{{.Id}}' "$TRUNK_IMAGE")"
    echo "# head image  $(docker image inspect --format '{{.Id}}' "$HEAD_IMAGE")"
    echo "# 3 warm-up requests each, then $rounds interleaved rounds; seconds from curl time_total"
    paste -d' ' "$(wdir $n)/trunk.times" "$(wdir $n)/head.times" | nl -w2 -s' ' | sed 's/^/round /'
    echo "trunk median: $trunk_median s"
    echo "head median:  $head_median s"
    echo "ratio head/trunk: $(awk -v h="$head_median" -v t="$trunk_median" 'BEGIN {printf "%.3f", h / t}')"
    echo "RESULT: $result (head median must be <= 1.10 x trunk median)"
  } >"$TRANSCRIPT"
  cmd_down $n
  echo "perf-guard: $result  $TRANSCRIPT"
}

TXNS=txns/2026.beancount
WRITE_PATH="/$BFILE/extension/AgentApi/transactions"
DINNER_ENTRY='2026-09-24 ! ^ik-d1
  Expenses:Food:Dinner                                  190 TWD
  Assets:TW:Cash                                       -190 TWD'

txns_file() { echo "$(idir "$1" "$2")/ledger/$TXNS"; }

tree_sha() {
  (cd "$(idir "$1" "$2")/ledger" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
}

link_count() { grep -rhoE -- "\\^$3([^A-Za-z0-9_/.-]|\$)" "$(idir "$1" "$2")/ledger" | wc -l; }

count() { grep -c . <<<"$1" || true; }

ledger_diff() {
  local fixture="$REPO/tests/fixtures/agent-ledger/$TXNS"
  note "\$ diff -u <fixture>/$TXNS <ledger>/$TXNS"
  diff -u "$fixture" "$(txns_file "$1" "$2")" >>"$TRANSCRIPT" || true
  ADDED=$(diff "$fixture" "$(txns_file "$1" "$2")" | sed -n 's/^> //p' || true)
  ADDED_HEADERS=$(grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2} ' <<<"$ADDED" || true)
}

field() { jq -r "$1" "$BODY" 2>/dev/null || echo "<body is not JSON>"; }

yes_no() { if "$@"; then echo yes; else echo no; fi; }

post() {
  local n=$1 target=$2 payload=$3
  shift 3
  note "request body: $payload"
  request "$n" "$target" POST "$WRITE_PATH" -H 'Content-Type: application/json' \
    --data-binary "$payload" "$@"
}

dinner() {
  printf '{"date":"2026-09-24","source":"錢包","target":"晚餐","amount":"190","key":"%s"}' "$1"
}

wlane1() {
  start_lane 1 lane1-regression
  cmd_boot 1 trunk
  cmd_boot 1 head
  post 1 trunk "$(dinner d1)" -H "$(bearer 1)"
  check "trunk has no write endpoint" "$STATUS" 404
  post 1 head "$(dinner d1)" -H "$(bearer 1)"
  check "head creates the entry" "$STATUS" 201
  note "tail of $TXNS:"
  tail -n 4 "$(txns_file 1 head)" >>"$TRANSCRIPT"
  check "$TXNS is the fixture plus the expected entry, byte for byte" \
    "$(yes_no cmp -s <(cat "$REPO/tests/fixtures/agent-ledger/$TXNS"; printf '\n%s\n' "$DINNER_ENTRY") \
      "$(txns_file 1 head)")" yes
  end_lane 1
}

wlane2() {
  start_lane 2 lane2-browser-write
  cmd_boot 2 "$TARGET"
  post 2 "$TARGET" "$(dinner lane2)" -H "$(assertion "$(cmd_jwt 2)")"
  check "write with a valid JWT" "$STATUS" 201
  check "entry header keeps the ! flag" "$(field .entry | head -1)" "2026-09-24 ! ^ik-lane2"
  check "file holds the ! entry once" \
    "$(grep -cxF '2026-09-24 ! ^ik-lane2' "$(txns_file 2 "$TARGET")" || true)" 1
  end_lane 2
}

wlane3() {
  start_lane 3 lane3-alias
  cmd_boot 3 "$TARGET"
  local key accounts
  for key in lane3-short lane3-full; do
    local target=晚餐
    [ "$key" = lane3-full ] && target=食物/晚餐
    post 3 "$TARGET" "{\"date\":\"2026-09-24\",\"source\":\"錢包\",\"target\":\"$target\",\"amount\":\"190\",\"narration\":\"晚餐\",\"key\":\"$key\"}" \
      -H "$(bearer 3)"
    check "$target: status" "$STATUS" 201
    accounts=$(field .entry | awk 'NR > 1 && NF { print $1 }' | paste -sd' ')
    check "$target: posting accounts" "$accounts" "Expenses:Food:Dinner Assets:TW:Cash"
    check "$target: entry is in the file" \
      "$(grep -cxF "2026-09-24 ! \"晚餐\" ^ik-$key" "$(txns_file 3 "$TARGET")" || true)" 1
  done
  end_lane 3
}

wlane4() {
  start_lane 4 lane4-ambiguous
  cmd_boot 4 "$TARGET"
  local before
  before=$(tree_sha 4 "$TARGET")
  post 4 "$TARGET" '{"date":"2026-09-24","source":"錢包","target":"午餐","amount":"120","key":"lane4"}' \
    -H "$(bearer 4)"
  check "ambiguous alias" "$STATUS" 422
  check "error code" "$(field .error.code)" ambiguous_account
  check "candidates" "$(field '.error.candidates | join(" ")')" "Expenses:Food:Lunch Expenses:Work:Lunch"
  check "ledger unchanged" "$(tree_sha 4 "$TARGET")" "$before"
  end_lane 4
}

wlane5() {
  start_lane 5 lane5-invalid-account
  cmd_boot 5 "$TARGET"
  local before
  before=$(tree_sha 5 "$TARGET")
  post 5 "$TARGET" '{"date":"2026-09-24","source":"悠遊卡","target":"晚餐","amount":"190","key":"lane5-closed"}' \
    -H "$(bearer 5)"
  check "account closed on 2026-06-30: status" "$STATUS" 422
  check "account closed on 2026-06-30: code" "$(field .error.code)" account_closed
  post 5 "$TARGET" '{"date":"2025-12-31","source":"錢包","target":"晚餐","amount":"190","key":"lane5-early"}' \
    -H "$(bearer 5)"
  check "date before the 2026-01-01 open: status" "$STATUS" 422
  check "date before the 2026-01-01 open: code" "$(field .error.code)" account_not_open
  post 5 "$TARGET" '{"date":"2026-09-24","source":"Assets:錢包","target":"晚餐","amount":"190","key":"lane5-name"}' \
    -H "$(bearer 5)"
  check "Assets:錢包: status" "$STATUS" 422
  check "Assets:錢包: code" "$(field .error.code)" invalid_account_name
  check "ledger unchanged" "$(tree_sha 5 "$TARGET")" "$before"
  end_lane 5
}

wlane6() {
  start_lane 6 lane6-amount
  cmd_boot 6 "$TARGET"
  local before amount i=0
  before=$(tree_sha 6 "$TARGET")
  for amount in 190.5.1 '1 @ 2' -5 0.001; do
    i=$((i + 1))
    post 6 "$TARGET" "{\"date\":\"2026-09-24\",\"source\":\"錢包\",\"target\":\"晚餐\",\"amount\":\"$amount\",\"key\":\"lane6-$i\"}" \
      -H "$(bearer 6)"
    check "amount '$amount': status" "$STATUS" 422
    check "amount '$amount': code" "$(field .error.code)" invalid_amount
  done
  check "ledger unchanged" "$(tree_sha 6 "$TARGET")" "$before"
  end_lane 6
}

wlane7() {
  start_lane 7 lane7-dry-run
  cmd_boot 7 "$TARGET"
  local before preview body
  before=$(tree_sha 7 "$TARGET")
  body=$(dinner lane7)
  post 7 "$TARGET" "${body%\}},\"dry_run\":true}" -H "$(bearer 7)"
  check "dry run: status" "$STATUS" 200
  check "dry run: created" "$(field .created)" false
  preview=$(field .entry)
  check "dry run: ledger unchanged" "$(tree_sha 7 "$TARGET")" "$before"
  post 7 "$TARGET" "$body" -H "$(bearer 7)"
  check "real write: status" "$STATUS" 201
  check "real write returns the dry-run entry" "$(field .entry)" "$preview"
  check "file ends with the dry-run entry" \
    "$(tail -n "$(wc -l <<<"$preview")" "$(txns_file 7 "$TARGET")")" "$preview"
  end_lane 7
}

wlane8() {
  start_lane 8 lane8-idempotent
  cmd_boot 8 "$TARGET"
  local w i url
  w=$(wdir 8)
  url="http://127.0.0.1:$(port 8 "$TARGET")$WRITE_PATH"
  post 8 "$TARGET" "$(dinner lane8)" -H "$(bearer 8)"
  check "first request" "$STATUS" 201
  race() {
    local key=$1
    note "5 concurrent requests with key $key"
    for i in 1 2 3 4 5; do
      curl -sS -o "$w/race-$i.body" -w '%{http_code}\n' -X POST -H "$(bearer 8)" \
        -H 'Content-Type: application/json' --data-binary "$(dinner "$key")" "$url" \
        >"$w/race-$i.code" &
    done
    wait
    for i in 1 2 3 4 5; do
      note "< $(cat "$w/race-$i.code") $(jq -c '{created, link}' "$w/race-$i.body" 2>/dev/null)"
    done
  }
  race lane8
  local codes
  codes=$(cat "$w"/race-*.code | sort | paste -sd' ')
  check "concurrent retries" "$codes" "200 200 200 200 200"
  check "concurrent retries: created" \
    "$(jq -r .created "$w"/race-*.body 2>/dev/null | sort -u | paste -sd' ')" false
  check "^ik-lane8 occurrences in the ledger" "$(link_count 8 "$TARGET" ik-lane8)" 1
  race lane8-cold
  codes=$(cat "$w"/race-*.code | sort | paste -sd' ')
  check "concurrent first writes" "$codes" "200 200 200 200 201"
  check "^ik-lane8-cold occurrences in the ledger" "$(link_count 8 "$TARGET" ik-lane8-cold)" 1
  end_lane 8
}

wlane9() {
  start_lane 9 lane9-time
  cmd_boot 9 "$TARGET"
  local row
  post 9 "$TARGET" '{"time":"2026-09-25T07:00:00+08:00","source":"錢包","target":"晚餐","amount":"80","key":"lane9"}' \
    -H "$(bearer 9)"
  check "write with time" "$STATUS" 201
  check "entry header" "$(field .entry | head -1)" "2026-09-25 ! ^ik-lane9"
  check "time metadata" "$(field .entry | sed -n 2p)" '  time: "07:00:00"'
  check "file holds the entry" \
    "$(grep -cxF '2026-09-25 ! ^ik-lane9' "$(txns_file 9 "$TARGET")" || true)" 1
  request 9 "$TARGET" GET "/$BFILE/api/query" -H "$(bearer 9)" -G --data-urlencode \
    'query_string=SELECT date, flag, links, account, position WHERE date >= 2026-09-24 AND date <= 2026-09-25'
  check "query" "$STATUS" 200
  row=$(jq -c '.data.rows[]' "$BODY" 2>/dev/null | grep -F ik-lane9 | head -1 || true)
  note "row: $row"
  check "query row dated 2026-09-25" "$(yes_no grep -qF '2026-09-25' <<<"$row")" yes
  end_lane 9
}

wlane10() {
  start_lane 10 lane10-errors
  cmd_boot 10 "$TARGET"
  local boot
  request 10 "$TARGET" GET "/$BFILE/api/errors" -H "$(bearer 10)"
  boot=$(field '.data | length')
  note "errors at boot: $boot"
  local body
  for body in "$(dinner lane10-a)" \
    '{"time":"2026-09-25T07:00:00+08:00","source":"錢包","target":"晚餐","amount":"80","key":"lane10-b"}' \
    '{"date":"2026-09-24","source":"Assets:TW:Cash","target":"食物/晚餐","amount":"12.50","narration":"晚餐 \"加蛋\"","key":"lane10-c"}'; do
    post 10 "$TARGET" "$body" -H "$(bearer 10)"
    check "write" "$STATUS" 201
    check "errors reported before and after the write" "$(field '"\(.errors.before) \(.errors.after)"')" "$boot $boot"
  done
  post 10 "$TARGET" "$(dinner lane10-d)" -H "$(assertion "$(cmd_jwt 10)")"
  check "browser write" "$STATUS" 201
  post 10 "$TARGET" "$(dinner lane10-a)" -H "$(bearer 10)"
  check "retry" "$STATUS" 200
  body=$(dinner lane10-e)
  post 10 "$TARGET" "${body%\}},\"dry_run\":true}" -H "$(bearer 10)"
  check "dry run" "$STATUS" 200
  post 10 "$TARGET" '{"date":"2026-09-24","source":"錢包","target":"午餐","amount":"120","key":"lane10-f"}' \
    -H "$(bearer 10)"
  check "ambiguous" "$STATUS" 422
  request 10 "$TARGET" GET "/$BFILE/api/errors" -H "$(bearer 10)"
  check "errors after all writes equal errors at boot" "$(field '.data | length')" "$boot"
  end_lane 10
}

p95() { sort -n | awk '{a[NR]=$1} END {i = int(NR * 0.95); if (i < NR * 0.95) i++; print a[i]}'; }

cmd_perf_write() {
  local n=perf rounds=20 jwt i w home_trunk home_head write_url
  w=$(wdir $n)
  TRANSCRIPT="$w/perf-write.txt"
  cmd_down $n
  rm -rf "$w"
  mkdir -p "$w"
  cmd_boot $n trunk
  cmd_boot $n head
  jwt=$(cmd_jwt $n)
  home_trunk="http://127.0.0.1:$(port $n trunk)/"
  home_head="http://127.0.0.1:$(port $n head)/"
  write_url="http://127.0.0.1:$(port $n head)$WRITE_PATH"
  perf_post() {
    curl -sS -o /dev/null -w "%{http_code} %{time_total}\n" -X POST -H "$(bearer $n)" \
      -H 'Content-Type: application/json' --data-binary "$(dinner "$1")" "$write_url"
  }
  for i in 1 2 3; do
    curl -fsS -L -o /dev/null -H "$(assertion "$jwt")" "$home_trunk"
    curl -fsS -L -o /dev/null -H "$(assertion "$jwt")" "$home_head"
    perf_post "perf-warm-$i" >/dev/null
  done
  : >"$w/trunk-home.times"
  : >"$w/head-home.times"
  : >"$w/head-write.raw"
  for ((i = 1; i <= rounds; i++)); do
    curl -fsS -L -o /dev/null -w '%{time_total}\n' -H "$(assertion "$jwt")" "$home_trunk" \
      >>"$w/trunk-home.times"
    curl -fsS -L -o /dev/null -w '%{time_total}\n' -H "$(assertion "$jwt")" "$home_head" \
      >>"$w/head-home.times"
    perf_post "perf-$i" >>"$w/head-write.raw"
  done
  cut -d' ' -f2 "$w/head-write.raw" >"$w/head-write.times"
  local trunk_median head_median write_p95 write_codes result
  trunk_median=$(median <"$w/trunk-home.times")
  head_median=$(median <"$w/head-home.times")
  write_p95=$(p95 <"$w/head-write.times")
  write_codes=$(cut -d' ' -f1 "$w/head-write.raw" | sort -u | paste -sd' ')
  result=$(awk -v h="$head_median" -v t="$trunk_median" -v p="$write_p95" -v c="$write_codes" \
    'BEGIN {print (h <= 1.10 * t && p <= 0.300 && c == "201") ? "PASS" : "FAIL"}')
  {
    echo "# perf-write: GET / (following the redirect) with a JWT on trunk and head;"
    echo "# POST $WRITE_PATH with the token on head, a new key each round"
    echo "# trunk image $(docker image inspect --format '{{.Id}}' "$TRUNK_IMAGE")"
    echo "# head image  $(docker image inspect --format '{{.Id}}' "$HEAD_IMAGE")"
    echo "# 3 warm-up rounds, then $rounds interleaved rounds; seconds from curl time_total"
    echo "# columns: trunk-home head-home head-write-status head-write"
    paste -d' ' "$w/trunk-home.times" "$w/head-home.times" "$w/head-write.raw" |
      nl -w2 -s' ' | sed 's/^/round /'
    echo "trunk home median: $trunk_median s"
    echo "head home median:  $head_median s"
    echo "ratio head/trunk:  $(awk -v h="$head_median" -v t="$trunk_median" 'BEGIN {printf "%.3f", h / t}')"
    echo "head write median: $(median <"$w/head-write.times") s"
    echo "head write p95:    $write_p95 s"
    echo "head write statuses: $write_codes"
    echo "RESULT: $result (head home median <= 1.10 x trunk; write p95 <= 0.300 s; every write 201)"
  } >"$TRANSCRIPT"
  cmd_down $n
  echo "perf-write: $result  $TRANSCRIPT"
}

MCP_PATH="/$BFILE/extension/AgentApi/mcp"
MCP_VERSION=2026-07-28

mcp_body() {
  jq -nc --arg method "$1" --argjson params "$2" --arg version "$MCP_VERSION" '{
    jsonrpc: "2.0", id: 1, method: $method,
    params: ($params | ._meta = (._meta // {}) + {
      "io.modelcontextprotocol/protocolVersion": $version,
      "io.modelcontextprotocol/clientCapabilities": {},
      "io.modelcontextprotocol/clientInfo": {name: "agent-api-lanes", version: "1"}})}'
}

tool_call_body() {
  mcp_body tools/call "$(jq -nc --arg name "$1" --argjson arguments "$2" '{name: $name, arguments: $arguments}')"
}

mcp_headers() {
  MCP_HEADERS=(-H "$(bearer "$1")" -H 'Content-Type: application/json'
    -H 'Accept: application/json, text/event-stream' -H "MCP-Protocol-Version: $MCP_VERSION" -H "Mcp-Method: $2")
  [ -z "${3:-}" ] || MCP_HEADERS+=(-H "Mcp-Name: $3")
}

mcp_post() {
  local n=$1 target=$2 method=$3 name=$4 body=$5
  shift 5
  mcp_headers "$n" "$method" "$name"
  note "request body: $body"
  request "$n" "$target" POST "$MCP_PATH" "${MCP_HEADERS[@]}" --data-binary "$body" "$@"
}

header() { grep -i "^$2:" "$(wdir "$1")/last-headers" | tr -d '\r' | sed 's/^[^:]*: *//' | tail -1; }

mcp_log_lines() { docker logs "$(fava "$1" "$2")" 2>&1 | grep '^mcp ' || true; }

recording_proxy_start() {
  local n=$1 target=$2 w deadline
  w=$(wdir "$n")
  recording_proxy_stop "$n"
  : >"$w/proxy.log"
  rm -f "$w/proxy.port"
  cat >"$w/proxy.py" <<'PY'
import http.client
import http.server
import os
import sys
import threading

UPSTREAM = int(sys.argv[1])
LOG, PORT_FILE = sys.argv[2], sys.argv[3]
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade"}
LOCK = threading.Lock()


def clip(data):
    return data[:400].decode("utf-8", "replace").replace("\r", "\\r").replace("\n", "\\n")


def forwardable(headers):
    drop = HOP_BY_HOP | {"content-length"}
    drop |= {name.strip().lower() for name in headers.get("Connection", "").split(",")}
    return [(name, value) for name, value in headers.items() if name.lower() not in drop]


class Proxy(http.server.BaseHTTPRequestHandler):
    def read_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks = []
            while size := int(self.rfile.readline().split(b";")[0], 16):
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            while self.rfile.readline().strip():
                pass
            return b"".join(chunks)
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def forward(self):
        body = self.read_body()
        try:
            upstream = http.client.HTTPConnection("127.0.0.1", UPSTREAM, timeout=120)
            upstream.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for name, value in forwardable(self.headers):
                upstream.putheader(name, value)
            if body or "Content-Length" in self.headers or "Transfer-Encoding" in self.headers:
                upstream.putheader("Content-Length", str(len(body)))
            upstream.endheaders(body)
            response = upstream.getresponse()
            status, reason, data = response.status, response.reason, response.read()
            headers = forwardable(response.headers)
            upstream.close()
        except OSError as error:
            status, reason, headers, data = 502, "Bad Gateway", [], str(error).encode()
        self.record(body, status, data)
        self.send_response_only(status, reason)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def record(self, body, status, data):
        lines = [f"{self.command} {self.path}"]
        for name in ("Mcp-Method", "Mcp-Name", "MCP-Protocol-Version", "Origin"):
            lines.append(f"  {name}: {self.headers.get(name, '-')}")
        lines.append("  Authorization: " + ("present" if "Authorization" in self.headers else "absent"))
        lines += [f"  request: {clip(body)}", f"  status: {status}", f"  response: {clip(data)}", "", ""]
        with LOCK, open(LOG, "a", encoding="utf-8") as log:
            log.write("\n".join(lines))


for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
    setattr(Proxy, f"do_{method}", Proxy.forward)

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
with open(PORT_FILE + ".tmp", "w") as out:
    out.write(str(server.server_address[1]))
os.replace(PORT_FILE + ".tmp", PORT_FILE)
server.serve_forever()
PY
  python3 "$w/proxy.py" "$(port "$n" "$target")" "$w/proxy.log" "$w/proxy.port" </dev/null >"$w/proxy.err" 2>&1 &
  echo $! >"$w/proxy.pid"
  deadline=$((SECONDS + 10))
  until [ -s "$w/proxy.port" ]; do
    ((SECONDS < deadline)) || { cat "$w/proxy.err" >&2; echo "proxy for worker $n did not start" >&2; return 1; }
    sleep 0.1
  done
}

recording_proxy_stop() {
  local pid
  pid="$(wdir "$1")/proxy.pid"
  [ -f "$pid" ] || return 0
  kill "$(cat "$pid")" 2>/dev/null || true
  rm -f "$pid"
}

recording_proxy_statuses() { awk '/^  status: / {print $2}' "$(wdir "$1")/proxy.log" | sort -u | paste -sd' '; }

# A throwaway CLAUDE_CONFIG_DIR and the worker dir as cwd keep the operator's config and any project
# .mcp.json out of `claude mcp add` and `claude mcp list`.
mcp_list() {
  local n=$1 target=$2 tag=$3 w cfg url
  shift 3
  w=$(wdir "$n")
  cfg="$w/claude-$tag"
  rm -rf "$cfg"
  mkdir -p "$cfg"
  recording_proxy_start "$n" "$target"
  url="http://127.0.0.1:$(cat "$w/proxy.port")$MCP_PATH"
  note "\$ claude mcp add --transport http beancount http://127.0.0.1:<proxy to $target>$MCP_PATH $(short "$@")"
  (cd "$w" && CLAUDE_CONFIG_DIR="$cfg" claude mcp add --transport http beancount "$url" "$@") \
    </dev/null >>"$TRANSCRIPT" 2>&1 || note "claude mcp add exit status: $?"
  note "\$ claude mcp list"
  (cd "$w" && CLAUDE_CONFIG_DIR="$cfg" timeout 120 claude mcp list) </dev/null >"$w/mcp-list.txt" 2>&1 || true
  recording_proxy_stop "$n"
  cat "$w/mcp-list.txt" >>"$TRANSCRIPT"
  note "proxy log ($target):"
  cat "$w/proxy.log" >>"$TRANSCRIPT"
  MCP_LINE=$(grep '^beancount: ' "$w/mcp-list.txt" || true)
}

STREAM_EVENTS='fromjson? |
  if .type == "assistant" then .message.content[]? | select(.type == "tool_use") | {kind: "tool_use", id, name, input}
  elif .type == "user" then .message.content[]? | select(.type == "tool_result") | {kind: "tool_result",
    id: .tool_use_id,
    text: (if (.content | type) == "string" then .content else [.content[]? | select(.type == "text") | .text] | join("\n") end)}
  elif .type == "result" then {kind: "result", text: .result}
  else empty end'

claude_run() {
  local n=$1 target=$2 slug=$3 tools=$4 prompt=$5 w config allowed status=0
  shift 5
  w=$(wdir "$n")
  allowed=$(sed 's/[^,]*/mcp__beancount__&/g' <<<"$tools")
  config=$(jq -nc --arg url "http://127.0.0.1:$(port "$n" "$target")$MCP_PATH" --arg auth "Bearer $(token "$n")" \
    '{mcpServers: {beancount: {type: "http", url: $url, headers: {Authorization: $auth}}}}')
  note "\$ claude -p <prompt> --model haiku --tools '' --setting-sources '' --strict-mcp-config --mcp-config <beancount at <$target>$MCP_PATH> --allowedTools $allowed $(short "$@")"
  note "prompt: $prompt"
  (cd "$w" && timeout 300 claude -p "$prompt" --model haiku --tools '' --setting-sources '' \
    --strict-mcp-config --mcp-config "$config" --allowedTools "$allowed" \
    --output-format stream-json --verbose --no-session-persistence "$@") \
    </dev/null >"$w/$slug.jsonl" 2>"$w/$slug.stderr" || status=$?
  note "claude exit status: $status (raw stream $w/$slug.jsonl)"
  record_stream "$w/$slug.jsonl"
}

record_stream() {
  CLAUDE_EVENTS="${1%.jsonl}.events"
  jq -cR "$STREAM_EVENTS" "$1" >"$CLAUDE_EVENTS"
  jq -r 'if .kind == "tool_use" then "tool_use \(.name) \(.input | tojson)"
    elif .kind == "tool_result" then "tool_result \((.text // "")[0:1500])"
    else "result: \(.text)" end' "$CLAUDE_EVENTS" >>"$TRANSCRIPT"
}

claude_text() { jq -r --arg kinds "$1" 'select(.kind | test("^(\($kinds))$")) | .text // empty' "$CLAUDE_EVENTS"; }

claude_calls() {
  jq -cs --arg name "mcp__beancount__$1" 'map(select(.kind == "tool_use" and .name == $name))' "$CLAUDE_EVENTS"
}

mlane1() {
  start_lane 1 lane1-regression
  cmd_boot 1 trunk
  cmd_boot 1 head
  note "claude --version: $(claude --version)"
  local log mcp_lines
  log=$(wdir 1)/proxy.log
  mcp_list 1 trunk trunk --header "$(bearer 1)"
  check "trunk: mcp list names beancount" "$(yes_no test -n "$MCP_LINE")" yes
  check "trunk: beancount is connected" "$(yes_no grep -qF '✔ Connected' <<<"$MCP_LINE")" no
  mcp_list 1 head head --header "$(bearer 1)"
  check "head: beancount is connected" \
    "$(yes_no grep -qE '^beancount: .*\(HTTP\) - ✔ Connected' <<<"$MCP_LINE")" yes
  check "head: server/discover answered with supportedVersions [\"$MCP_VERSION\"]" "$(yes_no awk -v RS= -v path="$MCP_PATH" '
    index($0, "POST " path "\n") == 1 && /\n  Mcp-Method: server\/discover\n/ &&
    /"supportedVersions": ?\[ ?"2026-07-28" ?\]/ {found = 1}
    END {exit !found}' "$log")" yes
  check "head: a later request carries MCP-Protocol-Version: $MCP_VERSION" "$(yes_no awk -v RS= '
    !discover && /\n  Mcp-Method: server\/discover\n/ {discover = NR; next}
    discover && /\n  MCP-Protocol-Version: 2026-07-28\n/ {found = 1}
    END {exit !found}' "$log")" yes
  mcp_lines=$(mcp_log_lines 1 head)
  note "head container log, lines starting with 'mcp ':"
  note "$mcp_lines"
  check "head: container log has 'mcp server/discover'" "$(yes_no grep -q '^mcp server/discover' <<<"$mcp_lines")" yes
  end_lane 1
}

mlane2() {
  start_lane 2 lane2-tools-list
  cmd_boot 2 "$TARGET"
  mcp_post 2 "$TARGET" tools/list "" "$(mcp_body tools/list '{}')"
  check "tools/list" "$STATUS" 200
  check "tool names" "$(field '[.result.tools[].name] | join(" ")')" "list_accounts query add_transaction"
  check "resultType" "$(field .result.resultType)" complete
  check "ttlMs type" "$(field '.result.ttlMs | type')" number
  check "cacheScope" "$(field .result.cacheScope)" private
  end_lane 2
}

mlane3() {
  start_lane 3 lane3-read-tools
  cmd_boot 3 "$TARGET"
  local calls query_string mcp_rows
  claude_run 3 "$TARGET" lane3-read-tools list_accounts,query \
    "Call the list_accounts tool. Then call the query tool with query_string exactly: SELECT account, sum(position) GROUP BY account
Then print every account name with its name-zh alias, and the query rows."
  check "list_accounts called" "$(claude_calls list_accounts | jq 'length > 0')" true
  calls=$(claude_calls query)
  check "query called" "$(jq 'length > 0' <<<"$calls")" true
  check "output contains Assets:TW:Cash" "$(yes_no grep -qF 'Assets:TW:Cash' <(claude_text 'tool_result|result'))" yes
  check "output contains 錢包" "$(yes_no grep -qF '錢包' <(claude_text 'tool_result|result'))" yes
  request 3 "$TARGET" GET "/$BFILE/api/ledger_data" -H "$(bearer 3)"
  check "/api/ledger_data" "$STATUS" 200
  query_string=$(jq -r '.[0].input.query_string // ""' <<<"$calls")
  mcp_rows=$(jq -cS --arg id "$(jq -r '.[0].id // ""' <<<"$calls")" \
    'select(.kind == "tool_result" and .id == $id) | .text | fromjson? | .data.rows' "$CLAUDE_EVENTS")
  request 3 "$TARGET" GET "/$BFILE/api/query" -H "$(bearer 3)" -G --data-urlencode "query_string=$query_string"
  check "/api/query" "$STATUS" 200
  note "rows from the MCP query tool: $mcp_rows"
  check "MCP query rows equal /api/query rows" "$mcp_rows" "$(jq -cS .data.rows "$BODY" 2>/dev/null || true)"
  end_lane 3
}

mlane4() {
  start_lane 4 lane4-dry-run
  cmd_boot 4 "$TARGET"
  local before
  before=$(tree_sha 4 "$TARGET")
  claude_run 4 "$TARGET" lane4-dry-run add_transaction \
    'Call add_transaction once with exactly these arguments: date "2026-09-24", source "錢包", target "晚餐", amount "190", key "lane4", dry_run true. Then show the entry text from the result verbatim.'
  check "add_transaction called with dry_run true" \
    "$(claude_calls add_transaction | jq 'any(.[]; .input.dry_run == true)')" true
  check "tool result holds the entry header" \
    "$(yes_no grep -qF '2026-09-24 ! ^ik-lane4' <(claude_text tool_result))" yes
  check "final text shows Expenses:Food:Dinner" "$(yes_no grep -qF 'Expenses:Food:Dinner' <(claude_text result))" yes
  check "ledger unchanged" "$(tree_sha 4 "$TARGET")" "$before"
  end_lane 4
}

mlane5() {
  start_lane 5 lane5-add
  cmd_boot 5 "$TARGET"
  local mcp_lines
  claude_run 5 "$TARGET" lane5-add list_accounts,query,add_transaction '記錄「9/24 晚餐 190（錢包）」' \
    --append-system-prompt "You record expenses in the user's beancount ledger through the beancount MCP tools."
  ledger_diff 5 "$TARGET"
  check "transactions added" "$(count "$ADDED_HEADERS")" 1
  check "added header starts with 2026-09-24 !" "$(cut -c1-12 <<<"$ADDED_HEADERS")" "2026-09-24 !"
  check "posting Expenses:Food:Dinner 190 TWD" \
    "$(yes_no grep -qE '^ +Expenses:Food:Dinner +190 TWD$' <<<"$ADDED")" yes
  check "posting Assets:TW:Cash" "$(yes_no grep -qE '^ +Assets:TW:Cash( |$)' <<<"$ADDED")" yes
  mcp_lines=$(mcp_log_lines 5 "$TARGET")
  note "container log, lines starting with 'mcp ':"
  note "$mcp_lines"
  check "container log has 'mcp tools/call add_transaction'" \
    "$(yes_no grep -q '^mcp tools/call add_transaction' <<<"$mcp_lines")" yes
  end_lane 5
}

mlane6() {
  start_lane 6 lane6-shared-key
  cmd_boot 6 "$TARGET"
  post 6 "$TARGET" "$(dinner shared)" -H "$(bearer 6)"
  check "REST write" "$STATUS" 201
  mcp_post 6 "$TARGET" tools/call add_transaction "$(tool_call_body add_transaction "$(dinner shared)")"
  check "MCP add_transaction" "$STATUS" 200
  check "isError" "$(field .result.isError)" false
  check "created" "$(field .result.structuredContent.created)" false
  check "^ik-shared occurrences in the ledger" "$(link_count 6 "$TARGET" ik-shared)" 1
  end_lane 6
}

mlane7() {
  start_lane 7 lane7-wrong-token
  cmd_boot 7 "$TARGET"
  mcp_list 7 "$TARGET" wrong --header "Authorization: Bearer wrong-token"
  check "mcp list names beancount" "$(yes_no test -n "$MCP_LINE")" yes
  check "beancount is connected" "$(yes_no grep -qF '✔ Connected' <<<"$MCP_LINE")" no
  check "proxy saw a POST to $MCP_PATH" "$(yes_no grep -qxF "POST $MCP_PATH" "$(wdir 7)/proxy.log")" yes
  check "every proxied status" "$(recording_proxy_statuses 7)" 401
  note "container log tail:"
  docker logs --tail 20 "$(fava 7 "$TARGET")" >>"$TRANSCRIPT" 2>&1
  end_lane 7
}

mlane8() {
  start_lane 8 lane8-no-token
  cmd_boot 8 "$TARGET"
  local before log
  before=$(tree_sha 8 "$TARGET")
  log=$(wdir 8)/proxy.log
  mcp_list 8 "$TARGET" none
  check "mcp list names beancount" "$(yes_no test -n "$MCP_LINE")" yes
  check "beancount is connected" "$(yes_no grep -qF '✔ Connected' <<<"$MCP_LINE")" no
  check "proxy saw a /.well-known/ path" "$(yes_no grep -qE '^[A-Z]+ /\.well-known/' "$log")" yes
  check "proxy saw POST /register" "$(yes_no grep -qxF 'POST /register' "$log")" yes
  check "every proxied status" "$(recording_proxy_statuses 8)" 401
  check "ledger unchanged" "$(tree_sha 8 "$TARGET")" "$before"
  end_lane 8
}

mlane9() {
  start_lane 9 lane9-protocol-errors
  cmd_boot 9 "$TARGET"
  local method
  for method in initialize resources/list; do
    mcp_post 9 "$TARGET" "$method" "" "$(mcp_body "$method" '{}')"
    check "$method: status" "$STATUS" 404
    check "$method: error code" "$(field .error.code)" -32601
  done
  mcp_post 9 "$TARGET" tools/list "" "[$(mcp_body tools/list '{}')]"
  check "JSON array body: status" "$STATUS" 400
  check "JSON array body: error code" "$(field .error.code)" -32600
  end_lane 9
}

mlane10() {
  start_lane 10 lane10-methods
  cmd_boot 10 "$TARGET"
  local method
  for method in GET DELETE; do
    request 10 "$TARGET" "$method" "$MCP_PATH" -H "$(bearer 10)"
    check "$method: status" "$STATUS" 405
    check "$method: Allow header" "$(header 10 allow)" POST
  done
  end_lane 10
}

cmd_perf_mcp() {
  local n=perf rounds=20 i w rest_url trunk_url mcp_url
  w=$(wdir $n)
  TRANSCRIPT="$w/perf-mcp.txt"
  cmd_down $n
  rm -rf "$w"
  mkdir -p "$w"
  cmd_boot $n trunk
  cmd_boot $n head
  rest_url="http://127.0.0.1:$(port $n head)$WRITE_PATH"
  trunk_url="http://127.0.0.1:$(port $n trunk)$WRITE_PATH"
  mcp_url="http://127.0.0.1:$(port $n head)$MCP_PATH"
  mcp_headers $n tools/call add_transaction
  perf_rest() {
    curl -sS -o /dev/null -w '%{http_code} %{time_total}\n' -X POST -H "$(bearer $n)" \
      -H 'Content-Type: application/json' --data-binary "$(dinner "$2")" "$1"
  }
  perf_mcp() {
    curl -sS -o "$w/$1.body" -w '%{http_code} %{time_total}\n' -X POST "${MCP_HEADERS[@]}" \
      --data-binary "$(tool_call_body add_transaction "$(dinner "$1")")" "$mcp_url"
  }
  for i in 1 2 3; do
    perf_rest "$rest_url" "perf-warm-rest-$i" >/dev/null
    perf_mcp "perf-warm-mcp-$i" >/dev/null
    perf_rest "$trunk_url" "perf-warm-trunk-$i" >/dev/null
  done
  : >"$w/rest.raw"
  : >"$w/mcp.raw"
  : >"$w/trunk.raw"
  : >"$w/mcp.flags"
  for ((i = 1; i <= rounds; i++)); do
    perf_rest "$rest_url" "perf-rest-$i" >>"$w/rest.raw"
    perf_mcp "perf-mcp-$i" >>"$w/mcp.raw"
    perf_rest "$trunk_url" "perf-trunk-$i" >>"$w/trunk.raw"
  done
  for ((i = 1; i <= rounds; i++)); do
    jq -r '"\(.result.isError) \(.result.structuredContent.created)"' "$w/perf-mcp-$i.body" 2>/dev/null ||
      echo "not-json -"
  done >"$w/mcp.flags"
  local rest_median mcp_median mcp_p95 rest_codes mcp_codes mcp_flags trunk_median result
  rest_median=$(cut -d' ' -f2 "$w/rest.raw" | median)
  mcp_median=$(cut -d' ' -f2 "$w/mcp.raw" | median)
  mcp_p95=$(cut -d' ' -f2 "$w/mcp.raw" | p95)
  trunk_median=$(cut -d' ' -f2 "$w/trunk.raw" | median)
  rest_codes=$(cut -d' ' -f1 "$w/rest.raw" | sort -u | paste -sd' ')
  mcp_codes=$(cut -d' ' -f1 "$w/mcp.raw" | sort -u | paste -sd' ')
  mcp_flags=$(sort -u "$w/mcp.flags" | paste -sd,)
  result=$(awk -v r="$rest_median" -v m="$mcp_median" -v p="$mcp_p95" -v rc="$rest_codes" -v mc="$mcp_codes" \
    -v f="$mcp_flags" \
    'BEGIN {print (m <= r + 0.020 && p <= 0.300 && rc == "201" && mc == "200" && f == "false true") ? "PASS" : "FAIL"}')
  {
    echo "# perf-mcp: POST $WRITE_PATH (REST) and POST $MCP_PATH tools/call add_transaction (MCP) on head,"
    echo "# the same dinner arguments with a new key each round; trunk REST for reference only"
    echo "# trunk image $(docker image inspect --format '{{.Id}}' "$TRUNK_IMAGE")"
    echo "# head image  $(docker image inspect --format '{{.Id}}' "$HEAD_IMAGE")"
    echo "# 3 warm-up rounds, then $rounds interleaved rounds; seconds from curl time_total"
    echo "# columns: rest-status rest mcp-status mcp mcp-isError mcp-created trunk-rest-status trunk-rest"
    paste -d' ' "$w/rest.raw" "$w/mcp.raw" "$w/mcp.flags" "$w/trunk.raw" | nl -w2 -s' ' | sed 's/^/round /'
    echo "REST median (baseline): $rest_median s"
    echo "MCP median:             $mcp_median s"
    echo "MCP p95:                $mcp_p95 s"
    echo "REST statuses: $rest_codes"
    echo "MCP statuses:  $mcp_codes; isError created: $mcp_flags"
    echo "trunk REST median (info): $trunk_median s"
    echo "RESULT: $result (MCP median <= REST median + 0.020 s; MCP p95 <= 0.300 s; every REST 201; every MCP 200 with created true)"
  } >"$TRANSCRIPT"
  cmd_down $n
  echo "perf-mcp: $result  $TRANSCRIPT"
}

cmd_images() {
  docker build -q -t "$HEAD_IMAGE" "$REPO" >/dev/null
  git -C "$REPO" archive "$TRUNK_REF" | docker build -q -t "$TRUNK_IMAGE" - >/dev/null
  echo "head  $HEAD_IMAGE $(docker image inspect --format '{{.Id}}' "$HEAD_IMAGE")"
  echo "trunk $TRUNK_IMAGE ($TRUNK_REF $(git -C "$REPO" rev-parse --short "$TRUNK_REF")) $(docker image inspect --format '{{.Id}}' "$TRUNK_IMAGE")"
}

BOOKING='9/24 晚餐 190（錢包）'
DOC="$REPO/docs/agent-api.md"

tools_call_count() { docker logs "$(fava "$1" "$2")" 2>&1 | grep -c '^mcp tools/call ' || true; }

skill_used() {
  if jq -es 'any(.[]; .kind == "tool_use" and .name == "Skill" and .input.skill == "beancount-ledger")' \
    "$CLAUDE_EVENTS" >/dev/null; then echo yes; else echo no; fi
}

proxied_403() { grep -cx '  status: 403' "$SKILL_PROXY_LOG" || true; }

dinner_booked() {
  if [ "$(count "$ADDED_HEADERS")" = 1 ] && [ "$(cut -c1-12 <<<"$ADDED_HEADERS")" = "2026-09-24 !" ] &&
    grep -qE '^ +Expenses:Food:Dinner +190 TWD$' <<<"$ADDED"; then echo yes; else echo no; fi
}

# --setting-sources project and --strict-mcp-config, in a fresh project dir, keep the operator's user
# settings, skills, plugins, CLAUDE.md, and MCP servers out of the session.
skill_run() {
  local n=$1 target=$2 slug=$3 mode=$4 prompt=$5 w project proxy config tools
  w=$(wdir "$n")
  project="$w/project-$slug"
  rm -rf "$project"
  mkdir -p "$project/.claude/skills"
  if [ "$target" = head ]; then
    cp -r "$REPO/skills/beancount-ledger" "$project/.claude/skills/"
    note "skill: $REPO/skills/beancount-ledger copied to <project>/.claude/skills/"
  elif git -C "$REPO" cat-file -e "$TRUNK_REF:skills/beancount-ledger" 2>/dev/null; then
    git -C "$REPO" archive "$TRUNK_REF" skills/beancount-ledger | tar -x -C "$project/.claude"
    note "skill: skills/beancount-ledger from $TRUNK_REF"
  else
    note "skill: none, $TRUNK_REF has no skills/beancount-ledger"
  fi
  recording_proxy_start "$n" "$target"
  proxy="http://127.0.0.1:$(cat "$w/proxy.port")"
  if [ "$mode" = mcp ]; then
    config=$(jq -nc --arg url "$proxy$MCP_PATH" --arg auth "Bearer $(token "$n")" \
      '{mcpServers: {beancount: {type: "http", url: $url, headers: {Authorization: $auth}}}}')
    tools=(--tools Skill
      --allowedTools 'Skill,mcp__beancount__list_accounts,mcp__beancount__query,mcp__beancount__add_transaction')
  else
    config='{"mcpServers":{}}'
    tools=(--tools 'Skill,Bash' --allowedTools Skill 'Bash(curl:*)')
  fi
  note "\$ cd <project> && TZ=Asia/Taipei BEANCOUNT_FAVA_URL=http://127.0.0.1:<proxy to $target>/$BFILE BEANCOUNT_AGENT_TOKEN=<worker token> timeout 300 claude -p <prompt> --model haiku --setting-sources project --strict-mcp-config --mcp-config <$mode config> ${tools[*]} --output-format stream-json --verbose --no-session-persistence"
  note "mcp config: ${config//$(token "$n")/<worker token>}"
  note "prompt: $prompt"
  SKILL_STATUS=0
  SKILL_START=$(date +%s.%N)
  (cd "$project" && TZ=Asia/Taipei BEANCOUNT_FAVA_URL="$proxy/$BFILE" BEANCOUNT_AGENT_TOKEN="$(token "$n")" \
    timeout 300 claude -p "$prompt" --model haiku --setting-sources project --strict-mcp-config \
    --mcp-config "$config" "${tools[@]}" --output-format stream-json --verbose --no-session-persistence) \
    </dev/null >"$w/$slug.jsonl" 2>"$w/$slug.stderr" || SKILL_STATUS=$?
  SKILL_SECONDS=$(awk -v s="$SKILL_START" -v e="$(date +%s.%N)" 'BEGIN {printf "%.1f", e - s}')
  recording_proxy_stop "$n"
  SKILL_PROXY_LOG="$w/$slug.proxy.log"
  cp "$w/proxy.log" "$SKILL_PROXY_LOG"
  note "claude exit status: $SKILL_STATUS after $SKILL_SECONDS s (raw stream $w/$slug.jsonl)"
  record_stream "$w/$slug.jsonl"
  note "Skill beancount-ledger used: $(skill_used)"
  note "container log, 'mcp tools/call' lines so far: $(tools_call_count "$n" "$target")"
  note "proxy log ($target):"
  cat "$SKILL_PROXY_LOG" >>"$TRANSCRIPT"
}

slane1() {
  start_lane 1 lane1-regression
  cmd_boot 1 trunk
  cmd_boot 1 head
  note "claude --version: $(claude --version)"
  skill_run 1 trunk trunk-booking mcp "$BOOKING"
  check "trunk: claude finished" "$SKILL_STATUS" 0
  ledger_diff 1 trunk
  note "trunk: tool calls: $(jq -r 'select(.kind == "tool_use") | .name' "$CLAUDE_EVENTS" | paste -sd' ')"
  note "trunk: transactions added: $(count "$ADDED_HEADERS")"
  note "trunk: container log, 'mcp tools/call' lines: $(tools_call_count 1 trunk)"
  note "trunk: reply: $(claude_text result)"
  skill_run 1 head head-booking mcp "$BOOKING"
  ledger_diff 1 head
  check "head: transactions added" "$(count "$ADDED_HEADERS")" 1
  check "head: added header starts with 2026-09-24 !" "$(cut -c1-12 <<<"$ADDED_HEADERS")" "2026-09-24 !"
  check "head: posting Expenses:Food:Dinner 190 TWD" \
    "$(yes_no grep -qE '^ +Expenses:Food:Dinner +190 TWD$' <<<"$ADDED")" yes
  check "head: posting Assets:TW:Cash -190 TWD" "$(yes_no grep -qE '^ +Assets:TW:Cash +-190 TWD$' <<<"$ADDED")" yes
  check "head: proxied 403 responses" "$(proxied_403)" 0
  check "head: Skill beancount-ledger used" "$(skill_used)" yes
  check "head: reply shows 2026-09-24 !" "$(yes_no grep -qF '2026-09-24 !' <(claude_text result))" yes
  note "head: container log, 'mcp tools/call' lines: $(tools_call_count 1 head)"
  end_lane 1
}

slane2() {
  start_lane 2 lane2-ambiguous
  cmd_boot 2 "$TARGET"
  local before reply
  before=$(tree_sha 2 "$TARGET")
  skill_run 2 "$TARGET" lane2-ambiguous mcp '9/24 午餐 120（錢包）'
  reply=$(claude_text result)
  check "ledger unchanged" "$(tree_sha 2 "$TARGET")" "$before"
  check "reply names Expenses:Food:Lunch" "$(yes_no grep -qE 'Expenses:Food:Lunch|食物/午餐' <<<"$reply")" yes
  check "reply names Expenses:Work:Lunch" "$(yes_no grep -qE 'Expenses:Work:Lunch|公務/午餐' <<<"$reply")" yes
  check "Skill beancount-ledger used" "$(skill_used)" yes
  end_lane 2
}

slane3() {
  start_lane 3 lane3-duplicate
  cmd_boot 3 "$TARGET"
  local reply
  skill_run 3 "$TARGET" session-a mcp "$BOOKING"
  ledger_diff 3 "$TARGET"
  check "session A: transactions added" "$(count "$ADDED_HEADERS")" 1
  check "session A: added header starts with 2026-09-24 !" "$(cut -c1-12 <<<"$ADDED_HEADERS")" "2026-09-24 !"
  skill_run 3 "$TARGET" session-b mcp "$BOOKING"
  ledger_diff 3 "$TARGET"
  check "after session B: transactions added" "$(count "$ADDED_HEADERS")" 1
  reply=$(claude_text result)
  note "session B reply: $reply"
  # The wording varies per run, so check the content: the existing 190 entry and a question back.
  check "session B: reply shows the existing 190 entry" "$(yes_no grep -q 190 <<<"$reply")" yes
  check "session B: reply asks the user" "$(yes_no grep -qE '[?？]' <<<"$reply")" yes
  check "session B: Skill beancount-ledger used" "$(skill_used)" yes
  end_lane 3
}

slane4() {
  start_lane 4 lane4-time
  local today extra='2026-01-01 open Expenses:Food:Breakfast TWD
  name-zh: "食物/早餐"'
  note "appended to main.beancount:"
  note "$extra"
  LEDGER_EXTRA=$extra cmd_boot 4 "$TARGET"
  today=$(TZ=Asia/Taipei date +%F)
  note "today in Asia/Taipei: $today"
  skill_run 4 "$TARGET" lane4-time mcp '今天早上 7 點早餐 80（錢包）'
  ledger_diff 4 "$TARGET"
  check "transactions added" "$(count "$ADDED_HEADERS")" 1
  check "added header starts with $today !" "$(cut -c1-12 <<<"$ADDED_HEADERS")" "$today !"
  check "time metadata" "$(yes_no grep -qE '^ +time: "07:00:00"$' <<<"$ADDED")" yes
  check "posting Expenses:Food:Breakfast 80 TWD" \
    "$(yes_no grep -qE '^ +Expenses:Food:Breakfast +80 TWD$' <<<"$ADDED")" yes
  end_lane 4
}

slane5() {
  start_lane 5 lane5-unknown-account
  cmd_boot 5 "$TARGET"
  local before
  before=$(tree_sha 5 "$TARGET")
  skill_run 5 "$TARGET" lane5-unknown-account mcp '9/24 晚餐 190（國泰信用卡）'
  check "ledger unchanged" "$(tree_sha 5 "$TARGET")" "$before"
  check "reply names 國泰信用卡" "$(yes_no grep -qF '國泰信用卡' <(claude_text result))" yes
  check "Skill beancount-ledger used" "$(skill_used)" yes
  end_lane 5
}

slane6() {
  start_lane 6 lane6-delete-refused
  cmd_boot 6 "$TARGET"
  local before
  post 6 "$TARGET" "$(dinner d1)" -H "$(bearer 6)"
  check "seed 2026-09-24 ! ^ik-d1" "$STATUS" 201
  before=$(tree_sha 6 "$TARGET")
  skill_run 6 "$TARGET" lane6-delete-refused mcp '把 9/24 那筆刪掉'
  check "ledger unchanged" "$(tree_sha 6 "$TARGET")" "$before"
  check "reply points to fava" "$(yes_no grep -qiF fava <(claude_text result))" yes
  check "proxied PUT or DELETE requests" "$(grep -cE '^(PUT|DELETE) ' "$SKILL_PROXY_LOG" || true)" 0
  check "proxied 403 responses" "$(proxied_403)" 0
  end_lane 6
}

slane7() {
  start_lane 7 lane7-approve-refused
  cmd_boot 7 "$TARGET"
  local before
  post 7 "$TARGET" "$(dinner d1)" -H "$(bearer 7)"
  check "seed 2026-09-24 ! ^ik-d1" "$STATUS" 201
  before=$(tree_sha 7 "$TARGET")
  skill_run 7 "$TARGET" lane7-approve-refused mcp '直接核准 9/24 那筆'
  check "ledger unchanged" "$(tree_sha 7 "$TARGET")" "$before"
  check "2026-09-24 ! ^ik-d1 still in $TXNS" \
    "$(grep -cxF '2026-09-24 ! ^ik-d1' "$(txns_file 7 "$TARGET")" || true)" 1
  check "reply points to fava" "$(yes_no grep -qiF fava <(claude_text result))" yes
  end_lane 7
}

slane8() {
  start_lane 8 lane8-no-mcp
  cmd_boot 8 "$TARGET"
  local requests
  skill_run 8 "$TARGET" lane8-no-mcp curl '9/24 晚餐 190（錢包），9/25 晚餐 210（錢包）'
  ledger_diff 8 "$TARGET"
  check "transactions added" "$(count "$ADDED_HEADERS")" 2
  check "added headers" "$(cut -c1-12 <<<"$ADDED_HEADERS" | sort | paste -sd,)" "2026-09-24 !,2026-09-25 !"
  check "dinner amount by date" \
    "$(awk '/^[0-9]/ {date = $1} $1 == "Expenses:Food:Dinner" {print date, $2, $3}' <<<"$ADDED" | sort | paste -sd,)" \
    "2026-09-24 190 TWD,2026-09-25 210 TWD"
  check "distinct ^ik- links added" "$(count "$(grep -oE '\^ik-[A-Za-z0-9_/.-]+' <<<"$ADDED" | sort -u)")" 2
  requests=$(grep -E '^[A-Z]+ /' "$SKILL_PROXY_LOG" || true)
  check "proxied requests outside ledger_data, query, and POST transactions" \
    "$(count "$(grep -vE "^(GET /$BFILE/api/(ledger_data|query)([?].*)?|POST $WRITE_PATH)\$" <<<"$requests" || true)")" 0
  check "proxied reads" "$(yes_no grep -qE "^GET /$BFILE/api/(ledger_data|query)" <<<"$requests")" yes
  check "proxied POSTs answered 201" "$(awk -v RS= -v path="$WRITE_PATH" '
    index($0, "POST " path "\n") == 1 && /\n  status: 201\n/ {n++} END {print n + 0}' "$SKILL_PROXY_LOG")" 2
  check "proxied 403 responses" "$(proxied_403)" 0
  end_lane 8
}

doc_code_block() {
  local step=$1 lang=$2
  awk -v section="### $step. " -v lang="$lang" '
    index($0, "### ") == 1 { in_section = index($0, section) == 1 }
    in_section && !open && $0 ~ "^ *```" lang "$" { open = 1; indent = index($0, "`") - 1; next }
    open && /^ *```$/ { exit }
    open { print substr($0, indent + 1) }' "$DOC"
}

# The doc's files belong to uid 99 or root, so only a root container can read or remove them.
as_root() { docker run --rm -i --user 0 --entrypoint "$2" -v "$1:/data" "$HEAD_IMAGE" "${@:3}"; }

slane9() {
  start_lane 9 lane9-deploy-doc
  ensure_stub 9
  local app d name line cmd part doc_token w
  w=$(wdir 9)
  app="$w/appdata"
  d=$(idir 9 deploy)
  name=$(fava 9 deploy)
  mkdir -p "$app" "$d"
  as_root "$app" find /data -mindepth 1 -delete
  note "$app stands for /mnt/user/appdata/beancount"
  cp -r "$REPO/tests/fixtures/agent-ledger" "$app/ledger"
  sed -i -e '/custom "fava-extension"/d' -e 's/^option "title" .*/option "title" "beancount"/' \
    "$app/ledger/main.beancount"
  line=$(doc_code_block 4 beancount)
  note "step 4 line: $line"
  check "step 4 shows one beancount line" "$(count "$line")" 1
  echo "$line" >>"$app/ledger/main.beancount"
  cmd=$(doc_code_block 1 sh | sed 's#/mnt/user/appdata/beancount#/data#g')
  note "step 1 as root, $app at /data, then chown -R 99:100 /data/ledger:"
  note "$cmd"
  as_root "$app" sh -e <<<"$cmd
chown -R 99:100 /data/ledger"
  as_root "$app" ls -lnR /data >>"$TRANSCRIPT"
  doc_token=$(as_root "$app" cat /data/agent-token)
  cmd=$(doc_code_block 3 sh | sed -e "s#/mnt/user/appdata/beancount#$app#g" -e "s#--name beancount-fava#--name $name#" \
    -e 's#-p 5656:5000#-p 127.0.0.1::5000#' -e "s#https://<team>.cloudflareaccess.com#$(team_domain 9)#" \
    -e "s#<aud-tag>#$AUD#" -e "s#gn00678465/beancount-fava:<tag>#$HEAD_IMAGE#" \
    -e "s#^docker run -d #docker run -d --network $(net 9) #")
  note "step 3, substituted:"
  note "$cmd"
  for part in "--network $(net 9)" "--name $name" "--user 99:100" "-p 127.0.0.1::5000" \
    "CF_ACCESS_TEAM_DOMAIN=$(team_domain 9)" "CF_ACCESS_AUD=$AUD" "$HEAD_IMAGE"; do
    check "step 3 command has $part" "$(yes_no grep -qF -- "$part" <<<"$cmd")" yes
  done
  bash -c "$cmd" >/dev/null
  docker port "$name" 5000/tcp | head -1 | sed 's/.*://' >"$d/port"
  wait_url "$name" "http://127.0.0.1:$(port 9 deploy)/beancount/api/errors" "Authorization: Bearer $doc_token"
  note "log:"
  docker logs "$name" >>"$TRANSCRIPT" 2>&1
  check "log has Starting Fava on http://0.0.0.0:5000" \
    "$(yes_no grep -qF 'Starting Fava on http://0.0.0.0:5000' <(docker logs "$name" 2>&1))" yes
  request 9 deploy GET /beancount/api/errors
  check "errors without credential" "$STATUS" 401
  request 9 deploy GET /beancount/api/errors -H "Authorization: Bearer $doc_token"
  check "errors with the token" "$STATUS" 200
  check "errors .data" "$(field '.data | tojson')" "[]"
  request 9 deploy GET /beancount/income_statement/ -H "$(assertion "$(cmd_jwt 9)")"
  check "income_statement with a JWT" "$STATUS" 200
  local project="$w/project-deploy" cfg="$w/claude-deploy"
  rm -rf "$project" "$cfg"
  mkdir -p "$project" "$cfg"
  recording_proxy_start 9 deploy
  doc_code_block 6 json | sed "s#http://192.168.2.11:5656#http://127.0.0.1:$(cat "$w/proxy.port")#g" >"$project/.mcp.json"
  echo '{"enabledMcpjsonServers":["beancount"]}' >"$cfg/settings.json"
  note "step 6 .mcp.json:"
  cat "$project/.mcp.json" >>"$TRANSCRIPT"
  note "CLAUDE_CONFIG_DIR settings.json, standing for the one-time approval: $(cat "$cfg/settings.json")"
  note "\$ BEANCOUNT_AGENT_TOKEN=<token file content> claude mcp list"
  (cd "$project" && CLAUDE_CONFIG_DIR="$cfg" BEANCOUNT_AGENT_TOKEN="$doc_token" timeout 120 claude mcp list) \
    </dev/null >"$w/mcp-list.txt" 2>&1 || true
  recording_proxy_stop 9
  cat "$w/mcp-list.txt" >>"$TRANSCRIPT"
  note "proxy log:"
  cat "$w/proxy.log" >>"$TRANSCRIPT"
  check "claude mcp list shows beancount connected" "$(yes_no grep -qE \
    '^beancount: http://127\.0\.0\.1:[0-9]+/beancount/extension/AgentApi/mcp \(HTTP\) - ✔ Connected' \
    "$w/mcp-list.txt")" yes
  end_lane 9
  as_root "$app" find /data -mindepth 1 -delete
}

slane10() {
  start_lane 10 lane10-compose
  ensure_stub 10
  local d example="$REPO/compose.example.yaml" project="$PREFIX-w10" status=0
  d=$(idir 10 compose)
  docker compose -p "$project" down -v >/dev/null 2>&1 || true
  rm -rf "$d"
  mkdir -p "$d"
  note "\$ docker compose -f compose.example.yaml config -q"
  docker compose -f "$example" config -q >>"$TRANSCRIPT" 2>&1 || status=$?
  check "compose.example.yaml config -q exit status" "$status" 0
  cp -r "$REPO/tests/fixtures/agent-ledger" "$d/ledger"
  cp "$(wdir 10)/token" "$d/agent-token"
  sed -e "s#^\( *image: \).*#\1$HEAD_IMAGE#" -e 's#"127.0.0.1:5000:5000"#"127.0.0.1::5000"#' "$example" >"$d/compose.yaml"
  note "\$ diff -u compose.example.yaml compose.yaml"
  diff -u "$example" "$d/compose.yaml" >>"$TRANSCRIPT" || true
  check "lines changed from the example" "$(diff "$example" "$d/compose.yaml" | grep -c '^>' || true)" 2
  docker compose -p "$project" -f "$d/compose.yaml" up -d >>"$TRANSCRIPT" 2>&1
  docker compose -p "$project" -f "$d/compose.yaml" port fava 5000 | sed 's/.*://' >"$d/port"
  wait_url "$project-fava-1" "http://127.0.0.1:$(port 10 compose)/$BFILE/api/errors" "$(bearer 10)"
  post 10 compose "$(dinner d1)" -H "$(bearer 10)"
  check "write" "$STATUS" 201
  request 10 compose GET "/$BFILE/api/errors" -H "$(bearer 10)"
  check "errors" "$STATUS" 200
  check "errors .data" "$(field '.data | tojson')" "[]"
  docker compose -p "$project" -f "$d/compose.yaml" down -v >>"$TRANSCRIPT" 2>&1
  end_lane 10
}

cmd_perf_skill() {
  local n=perf rounds=5 i target w changed trunk_median head_median result
  w=$(wdir $n)
  cmd_down $n
  rm -rf "$w"
  mkdir -p "$w"
  TRANSCRIPT="$w/perf-skill.txt"
  {
    echo "# perf-skill: claude -p '$BOOKING' in MCP mode; head loads the beancount-ledger Skill, trunk has none"
    echo "# trunk image $(docker image inspect --format '{{.Id}}' "$TRUNK_IMAGE")"
    echo "# head image  $(docker image inspect --format '{{.Id}}' "$HEAD_IMAGE")"
    echo "# $rounds interleaved rounds of trunk then head, each on a fresh fava and ledger"
  } >"$TRANSCRIPT"
  : >"$w/rows"
  for ((i = 1; i <= rounds; i++)); do
    for target in trunk head; do
      note "## round $i $target"
      cmd_boot $n $target
      skill_run $n $target "$target-$i" mcp "$BOOKING"
      ledger_diff $n $target
      changed=-
      cmp -s "$REPO/tests/fixtures/agent-ledger/$TXNS" "$(txns_file $n $target)" ||
        changed=$(awk -v s="$SKILL_START" -v m="$(stat -c %.3Y "$(txns_file $n $target)")" \
          'BEGIN {printf "%.1f", m - s}')
      echo "$i $target $SKILL_SECONDS $changed $(tools_call_count $n $target) $(dinner_booked) $(skill_used)" \
        >>"$w/rows"
      docker rm -f "$(fava $n $target)" >/dev/null
    done
  done
  trunk_median=$(awk '$2 == "trunk" {print $3}' "$w/rows" | median)
  head_median=$(awk '$2 == "head" {print $3}' "$w/rows" | median)
  result=$(awk -v h="$head_median" -v t="$trunk_median" '
    $2 == "head" && ($5 > 3 || $6 != "yes") {bad = 1}
    END {print (!bad && h <= 30) ? "PASS" : "FAIL"}' "$w/rows")
  {
    echo "# columns: round target claude-wall-s ledger-written-after-s tools/call dinner-written skill-used"
    sed 's/^/round /' "$w/rows"
    echo "trunk median wall (reference): $trunk_median s"
    echo "head median wall:             $head_median s"
    echo "ratio head/trunk: $(awk -v h="$head_median" -v t="$trunk_median" 'BEGIN {printf "%.3f", h / t}')"
    echo "RESULT: $result (every head run: tools/call <= 3 and the dinner written; head median wall <= 30 s)"
  } >>"$TRANSCRIPT"
  cmd_down $n
  echo "perf-skill: $result  $TRANSCRIPT"
}

case "${1:-}" in
images) cmd_images ;;
boot) cmd_boot "$2" "${3:-head}" ;;
jwt) cmd_jwt "${@:2}" ;;
down) cmd_down "$2" ;;
lane) "lane$2" ;;
perf-guard) cmd_perf_guard ;;
all)
  cmd_images
  for n in 1 2 3 4 5 6 7 8 9 10; do "lane$n"; done
  cmd_perf_guard
  ;;
write-lane) "wlane$2" ;;
perf-write) cmd_perf_write ;;
write-all)
  cmd_images
  for n in 1 2 3 4 5 6 7 8 9 10; do "wlane$n"; done
  cmd_perf_write
  ;;
mcp-lane) "mlane$2" ;;
perf-mcp) cmd_perf_mcp ;;
mcp-all)
  cmd_images
  for n in 1 2 3 4 5 6 7 8 9 10; do "mlane$n"; done
  cmd_perf_mcp
  ;;
skill-lane) "slane$2" ;;
perf-skill) cmd_perf_skill ;;
skill-all)
  cmd_images
  for n in 1 2 3 4 5 6 7 8 9 10; do "slane$n"; done
  cmd_perf_skill
  ;;
*)
  awk 'NR == 1 {next} !/^#/ {exit} {sub(/^# ?/, ""); print}' "$0"
  exit 2
  ;;
esac
