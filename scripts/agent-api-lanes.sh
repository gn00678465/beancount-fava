#!/usr/bin/env bash
# Live lanes and perf probes for docs/plans/agent-write-api.md (AGENT-1 guard, AGENT-2 writes).
#
#   images               build $HEAD_IMAGE from this checkout and $TRUNK_IMAGE from $TRUNK_REF
#   boot <n> [target]    start a JWKS stub and fava for worker <n>; target is head or trunk
#   jwt <n> <claims>     sign a JWT for worker <n>'s stub; optional key and kid follow
#   down <n>             remove worker <n>'s containers and network
#   lane <n>             run guard lane <n> against $TARGET (head by default)
#   perf-guard           interleave trunk and head, 20 rounds each
#   all                  images, guard lanes 1-10, perf-guard
#   write-lane <n>       run write-endpoint lane <n> against $TARGET (head by default)
#   perf-write           interleave trunk and head home pages and head writes, 20 rounds each
#   write-all            images, write lanes 1-10, perf-write
#
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

image_for() { if [ "$1" = trunk ]; then echo "$TRUNK_IMAGE"; else echo "$HEAD_IMAGE"; fi; }
wdir() { echo "$OUT/worker-$1"; }
idir() { echo "$(wdir "$1")/$2"; }
net() { echo "lanes-w$1"; }
stub() { echo "lanes-w$1-jwks"; }
fava() { echo "lanes-w$1-$2"; }
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

wait_ready() {
  local n=$1 target=$2 deadline=$((SECONDS + 60))
  until curl -fsS -o /dev/null -H "Authorization: Bearer $(token "$n")" \
    "http://127.0.0.1:$(port "$n" "$target")/$BFILE/api/errors" 2>/dev/null; do
    if ((SECONDS > deadline)); then
      docker logs "$(fava "$n" "$target")" >&2
      echo "fava for worker $n ($target) did not answer within 60s" >&2
      return 1
    fi
    sleep 0.5
  done
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
  docker ps -aq --filter "name=^lanes-w$n-" | xargs -r docker rm -f >/dev/null
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
    grep -iE '^(HTTP/|www-authenticate|location|content-type)' "$headers" | tr -d '\r' | sed 's/^/< /'
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
  for path in ledger_data query; do
    local args=()
    [ "$path" = query ] && args=("${query[@]}")
    request 2 trunk GET "/$BFILE/api/$path" "${args[@]}"
    want=$(sha256sum "$BODY" | cut -d' ' -f1)
    request 2 "$TARGET" GET "/$BFILE/api/$path" -H "$(bearer 2)" "${args[@]}"
    check "$path with token" "$STATUS" 200
    check "$path body equals trunk without guard" "$(sha256sum "$BODY" | cut -d' ' -f1)" "$want"
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

cmd_images() {
  docker build -q -t "$HEAD_IMAGE" "$REPO" >/dev/null
  git -C "$REPO" archive "$TRUNK_REF" | docker build -q -t "$TRUNK_IMAGE" - >/dev/null
  echo "head  $HEAD_IMAGE $(docker image inspect --format '{{.Id}}' "$HEAD_IMAGE")"
  echo "trunk $TRUNK_IMAGE ($TRUNK_REF $(git -C "$REPO" rev-parse --short "$TRUNK_REF")) $(docker image inspect --format '{{.Id}}' "$TRUNK_IMAGE")"
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
*)
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
  ;;
esac
