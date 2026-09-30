# Agent 寫入 API 實作計畫

fava 的每個請求都要有憑證。瀏覽器經由 Cloudflare Access 登入，fava 驗證 Access 簽發的 JWT。Agent 在區網以 Bearer token 連 5656。
Agent 讀取沿用 fava `/api/` 的 GET 端點。Agent 寫入只走新的 extension，交易一律是 `!` flag，同一個 idempotency key 只寫入一次。
Agent 呼叫 fava `/api/` 的 PUT、DELETE 或 fava 網頁時，guard 回 403。這是執行期的強制規則，不是約定。
服務對象是替使用者記帳的 Agent（Claude Code 等），以及在 fava 網頁上核准交易的使用者。
全部工作在單一分支 `feat/agent-api` 上，最後開一個 PR。分支依序包含研究筆記與本計畫，以及 AGENT-1（guard 與啟動程式）、AGENT-2（REST 寫入端點）、AGENT-3（MCP 端點）、AGENT-4（Skill 與部署說明）四個單元。

## How to read this

One box is one unit of work. Every box names the evidence that checks it. A nested box is a sub-step of the box above it. Check a box only when its evidence exists, a file, a log line, a screenshot, a test run, or a SHA. The body is a how-to. The appendices explain and record.

研究筆記與本計畫是 `feat/agent-api` 的第一個 commit（`4c2feba`）。四個單元依序疊在它上面，不直接 commit 到 `main`。下文的「PR」指一個單元，每個單元是一組可以單獨驗證的 commit。

每個單元依 `pstack/skills/poteto-mode/playbooks/feature.md` 執行。單元前後相依，依序進行。四個單元都驗證完成後，以 `feat/agent-api` 開一個 PR 到 `main`，停在 merge-ready，由使用者合併。

本 repo 在 Claude Code 中執行，不是 Cursor。下列對應在整份計畫中成立。`grok-4.6-fast-xhigh` 的 lane 由 Claude Code 的 `haiku` 子代理執行。`/goal` 是在 session 中釘住的計畫目標文字。30 分鐘稽核用 `/loop 30m`。live lane 的驗證面是 HTTP 與 MCP，每個 lane 存請求與回應的文字記錄檔（`.txt`），不存截圖。

Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

## Program checklist

### Arm the program

- [ ] 向使用者說明本計畫與執行方式，然後停止。只有使用者明確同意後才開始執行。
- [ ] 使用者同意後，以 `/goal` 釘住這段文字。「依 `docs/plans/agent-write-api.md` 在 `feat/agent-api` 上依序完成 AGENT-1 到 AGENT-4。每個單元的 unit、live、perf 方塊都有證據才算驗證完成。四個單元都驗證後開一個 PR 到 `main`，停在 merge-ready，由使用者合併。」
- [ ] 計畫開始時讀取下列檔案，每次稽核再讀一次。
  - [ ] `git show origin/main:tests/test_image.py`
  - [ ] `git show origin/main:Dockerfile`
  - [ ] `git show feat/agent-api:docs/plans/agent-write-api.md`，以及同一分支的 `docs/research/mcp-spec-2026-09.md`、`docs/research/browser-auth-options.md`。
  - [ ] 本機 plugin cache 中的 `poteto-mode/playbooks/feature.md`、`poteto-mode/playbooks/opening-a-pr.md`、`swarm/SKILL.md`。
- [ ] 以 `/loop 30m` 設定 30-minute 稽核週期，不靠記憶維持週期。
- [ ] 稽核提示逐字使用這段。「Re-read the execution playbook from trunk and the armed /goal. Audit the operation against both and fix drift in this tick. Probe every active lane and judge progress by side effects only. Stand down a stuck lane and dispatch its replacement now. Then post a status message to the operator in chat, whether or not anything changed, with the queue table of PR, owner, state, and head SHA, the verdicts since the last tick, what merged, open operator gates, and blockers.」
- [ ] 使用者要求暫停或停止時，立即要求每個 owner 停止所有寫入。

### Spawn owners

- [ ] 每個單元派一個 `poteto-agent` owner。owner 在自己的 git worktree 中，從 `feat/agent-api` 的當前 tip 開工作分支，commit 後回報 head SHA，不 push、不開 PR。
- [ ] 依這個相依圖執行。root 在單元判定乾淨後，把 `feat/agent-api` fast-forward 到該單元的 head，下一個單元才開始。
  - [ ] AGENT-1 最先做，base 是 `4c2feba`。
  - [ ] AGENT-2 在 AGENT-1 之後，base 是 AGENT-1 的 head。
  - [ ] AGENT-3 在 AGENT-2 之後，base 是 AGENT-2 的 head。
  - [ ] AGENT-4 在 AGENT-3 之後，base 是 AGENT-3 的 head。
- [ ] 守住檔案邊界。AGENT-1 只改 `src/beancount_agent_api/{__init__,guard,serve}.py`、`pyproject.toml`、`uv.lock`、`Dockerfile`、`.dockerignore`、`docker-entrypoint.sh`、`tests/**`、`scripts/agent-api-lanes.sh`。AGENT-2 只改 `src/beancount_agent_api/{__init__,core,extension}.py`、`tests/**`、`scripts/agent-api-lanes.sh`。AGENT-3 只改 `src/beancount_agent_api/{extension,mcp}.py`、`tests/**`、`scripts/agent-api-lanes.sh`。AGENT-4 只改 `skills/**`、`docs/**`、`compose.example.yaml`，以及 Verify 方塊需要的 `tests/test_image.py` 與 `scripts/agent-api-lanes.sh`。
- [ ] 守住審查關卡。四個單元都不改使用者介面的操作方式，沒有 review gate。使用者在合併前審查整個 PR。

### PR mechanics, for every PR

- [ ] 決定一次 forge。預設用 `gh`。若 `command -v origin` 成功且 Origin 能解析此 repo，所有 PR 操作改用 `origin pr`。記錄任何退回 `gh` 的情況。不要求 `gt`。
- [ ] 只開一個 PR。四個單元都驗證後，push `feat/agent-api`，以 `gh pr create --base main` 開 ready PR，不用 draft。
- [ ] PR 推送前執行一次 `uv run ruff check .` 與 `uv run ruff format --check .`。推送時保留 hooks。
- [ ] 每次 commit 前執行 `/deslop`，送審前執行 `/no-comments`。
- [ ] 依 `../references/bugbot-triage.md` 處理每一則 Bugbot 與安全審查留言。
- [ ] 開 PR 前把 `feat/agent-api` rebase 到最新 `main`，回報 merge-ready 前再 rebase 一次。

### Verdict and merge, for every PR

- [ ] 在 merge-ready 的 head SHA 上，依 `swarm/SKILL.md` 執行 swarm。一個 gates lane（ruff、pytest、hadolint、actionlint、pip-audit）。該 PR **Verify, live** 區塊的十個 live lane。**Verify, perf** 區塊的 perf lane。一個稽核 lane，讀 diff 與證據檔，不信任 PR 描述。
- [ ] 每個 lane 都是 `PASS` 才算乾淨。發現的問題退回 owner。新的 head 要重跑 swarm，重新判定。
- [ ] 乾淨判定後，root 把 `feat/agent-api` fast-forward 到該單元的 head，不合併到 `main`。rebase 後依 `playbooks/shipping.md` 比對每個單元的 `git patch-id`。patch-id 不變則保留判定，改變則重跑 swarm。

### Boot recipe, for every live lane

每個 live lane 在本機以 Docker 啟動一個 fava container 與一個 JWKS stub，掛載 lane 自己的 fixture ledger 副本。

- [ ] `git fetch origin <head-branch> && git checkout <head SHA>`。
- [ ] `docker build -t beancount-fava:lane .`，再執行 `scripts/agent-api-lanes.sh boot <lane-n>`。它建立 lane 專用的 Docker 網路，啟動提供 `/cdn-cgi/access/certs` 的 JWKS stub，複製 `tests/fixtures/agent-ledger/` 到 `/tmp/swarm-<pr-id>/worker-<n>/ledger/`，以 lane token 與 stub 的 team domain 啟動 fava，並等到帶 token 的 `GET /agent/api/errors` 回 200。
- [ ] 只用 `curl`、`scripts/agent-api-lanes.sh jwt <claims>`（以 stub 私鑰簽 JWT）與 `claude` CLI 送出請求。唯讀診斷用 `sha256sum` ledger 檔與 `docker logs`。
- [ ] 每個 lane 的請求與回應存到 `/tmp/swarm-<pr-id>/worker-<n>/<slug>.txt`，連同報告回傳路徑。

## 以 WSGI guard 保護整個 fava (AGENT-1)

**Depends on.** None.

**Files.**

- [ ] Create `src/beancount_agent_api/__init__.py`、`src/beancount_agent_api/guard.py`、`src/beancount_agent_api/serve.py`。
- [ ] Edit `pyproject.toml` 與 `uv.lock`，加入 build-system、package、console script `beancount-fava-serve`、`pyjwt[crypto]`。
- [ ] Edit `Dockerfile`，複製 `src/`，以 `uv sync --locked --no-dev --no-editable` 安裝本專案，`CMD` 改為 `beancount-fava-serve`。
- [ ] Edit `.dockerignore` 放行 `src/`。Edit `docker-entrypoint.sh`，對新的 CMD 做同樣的 ledger 檢查。
- [ ] Create `tests/fixtures/agent-ledger/main.beancount`（`option "title" "Agent"`，URL slug 為 `agent`）與 `tests/test_guard.py`。Edit `tests/test_image.py`，既有案例改帶 token。
- [ ] Create `scripts/agent-api-lanes.sh`。

**Build.**

- [ ] 在 `guard.py` 定義 `Principal`（`Browser` 或 `Agent`）與一張政策表 `(principal, method, path pattern) -> allow`。`Browser` 全部放行。`Agent` 只放行 `GET /<bfile>/api/*` 與 `POST /<bfile>/extension/AgentApi/*`，其他回 403。沒有憑證或憑證無效回 401。
- [ ] 在 `guard.py` 定義 `authenticate(environ) -> Principal | None`。Bearer 以 `hmac.compare_digest` 比對 `AGENT_API_TOKEN_FILE` 的內容。`Cf-Access-Jwt-Assertion` 以 `PyJWKClient(<CF_ACCESS_TEAM_DOMAIN>/cdn-cgi/access/certs)` 依 `kid` 取公鑰，驗 RS256 簽章、`aud` 等於 `CF_ACCESS_AUD`、`iss` 等於 `CF_ACCESS_TEAM_DOMAIN`、`exp`。取不到公鑰時拒絕。只讀 header，不讀 `CF_Authorization` cookie。
- [ ] 在 `serve.py` 以 `fava.application.create_app` 建立 app，以 guard 包住 `wsgi_app`，用 cheroot 在 `FAVA_HOST:5000` 服務。讀取 `BEANCOUNT_FILE`。token 與 Access 設定都沒有時，印出原因並以非零碼結束。

**You see.**

- [ ] 沒帶憑證 `curl http://127.0.0.1:<port>/agent/api/ledger_data` 回 401。帶 lane token 回 200。帶 lane token `PUT /agent/api/source` 回 403。帶 stub 簽的有效 JWT 開 `/agent/income_statement/` 回 200。

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] `tests/test_guard.py` 以 WSGI 測試 client 呼叫包住 stub app 的 guard，斷言每個 `(憑證, method, path)` 的字面狀態碼。涵蓋政策表每一列、JWT 的 `aud`、`iss`、`exp`、簽章、`kid` 錯誤。Run `uv run pytest tests/test_guard.py`。
- [ ] `tests/test_image.py` 既有案例帶 token 後全部通過，新增「沒有認證設定時 container 以非零碼結束」一案。Run `uv run pytest tests/test_image.py`。

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.6-fast-xhigh` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. 在 trunk 與 head 各以沒有憑證的 `curl` 讀 `/agent/api/ledger_data`。trunk 沒有 guard，預期 200，記錄這個事實。Save `lane1-regression.txt`. Pass when trunk 回 200、head 回 401。
- [ ] Lane 2. 帶 lane token 讀 `/api/ledger_data` 與 `/api/query`。Save `lane2-agent-read.txt`. Pass when 兩者都回 200，內容與 trunk 無 guard 時相同。
- [ ] Lane 3. 帶 lane token 送 `PUT /api/source`、`PUT /api/add_entries`、`DELETE /api/source_slice`。Save `lane3-agent-write-denied.txt`. Pass when 三者都回 403，ledger sha256 不變。
- [ ] Lane 4. 帶 lane token 開 `/agent/income_statement/` 與 `/agent/editor/`。Save `lane4-agent-ui-denied.txt`. Pass when 兩者都回 403。
- [ ] Lane 5. 帶 stub 簽的有效 JWT 開 fava 首頁、讀 `/api/ledger_data`、送 `PUT /api/format_source`。Save `lane5-browser.txt`. Pass when 三者都回 200。
- [ ] Lane 6. 送 `aud` 錯誤、`iss` 錯誤、已過期、簽章錯誤的 JWT 各一次。Save `lane6-jwt-invalid.txt`. Pass when 四次都回 401。
- [ ] Lane 7. stub 同時提供新舊兩把金鑰，以舊金鑰簽 JWT 送出。再讓 stub 移除舊金鑰，重啟 fava 後再送一次。Save `lane7-key-rotation.txt`. Pass when 第一次回 200、第二次回 401。
- [ ] Lane 8. 停掉 JWKS stub 後送有效 JWT，再送 lane token。Save `lane8-jwks-down.txt`. Pass when JWT 請求回 401，token 請求回 200。
- [ ] Lane 9. 不設定 `AGENT_API_TOKEN_FILE` 與 `CF_ACCESS_*` 啟動 container。Save `lane9-fail-closed.txt`. Pass when container 以非零碼結束，log 說明缺少哪些設定。
- [ ] Lane 10. 沒有憑證讀 `/agent/api/changed`、`/agent/api/errors`、`/static/app.js`。Save `lane10-no-bypass.txt`. Pass when 三者都回 401。

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. `GET /agent/income_statement/` 的回應時間。trunk 不帶憑證，head 帶有效 JWT（金鑰已快取）。
- [ ] Probe. `scripts/agent-api-lanes.sh perf-guard` 在同一台機器上交錯執行 trunk 與 head 的 container，各 20 輪，以 `curl -w '%{time_total}'` 取值。
- [ ] Baseline. 先記錄 trunk 的中位數。
- [ ] Rule. head 中位數超過 trunk 中位數的 1.10 倍即失敗。

**Review gate.** None. AGENT-1 is not review-gated.

**Merge.**

- [ ] root 在該 head SHA 給出乾淨判定。
- [ ] Bugbot 留言處理完畢。
- [ ] 判定後 rebase 到 `feat/agent-api` 的最新 tip，patch-id 不變。
- [ ] root 把 `feat/agent-api` fast-forward 到該單元的 head。

## 以 fava extension 提供 REST 寫入端點 (AGENT-2)

**Depends on.** AGENT-1。

**Files.**

- [ ] Create `src/beancount_agent_api/core.py` 與 `src/beancount_agent_api/extension.py`。
- [ ] Edit `src/beancount_agent_api/__init__.py`，匯出 extension 類別，讓 `custom "fava-extension" "beancount_agent_api"` 可以載入。
- [ ] Edit `tests/fixtures/agent-ledger/main.beancount`，加上 extension 行、帶 `name-zh` 的 open、一個已關閉帳戶、兩個 `name-zh` 末段相同的帳戶。
- [ ] Create `tests/test_agent_core.py` 與 `tests/test_agent_api.py`。
- [ ] Edit `scripts/agent-api-lanes.sh`，加入寫入 lane。

**Build.**

- [ ] 在 `core.py` 定義 `AddRequest`（`date`、`time`、`source`、`target`、`amount`、`currency`、`narration`、`key`、`dry_run`）與 `parse_add_request(raw, accounts, tz) -> AddRequest | Rejection`。帳戶解析用一張查詢表（全名、完整 `name-zh`、`name-zh` 最後一段），命中多個時回傳全部候選。檢查開帳日、關帳日、`account.is_valid()`、金額格式 `^\d+(\.\d{1,2})?$`、幣別。
- [ ] 在 `core.py` 定義 `build_transaction(req) -> Transaction`，flag 固定為 `!`，link 為 `ik-<key>`，有時刻時加 `time` metadata。
- [ ] 在 `extension.py` 定義 `AgentApi(FavaExtensionBase)`，只提供 `POST transactions`（含 `dry_run`）。認證已由 guard 處理，extension 不再檢查。寫入時在模組層級的 `threading.Lock` 內依序執行 `ledger.changed()`、查 `ledger.attributes.links`、`ledger.file.insert_entries`，並回報寫入前後 errors 數量的差。

**You see.**

- [ ] 帶 lane token 送 `{"date":"2026-09-24","source":"錢包","target":"晚餐","amount":"190","key":"d1"}`，回應 201，ledger 檔尾多出 `2026-09-24 ! ^ik-d1` 開頭的交易，posting 為 `Expenses:Food:Dinner` 與 `Assets:TW:Cash`。

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] `tests/test_agent_core.py` 以字面值斷言 `parse_add_request` 的結果。涵蓋別名解析、候選清單、金額格式、時區換日（`2026-09-25T07:00:00+08:00` 在 `Asia/Taipei` 是 `2026-09-25`）、key 字元限制。Run `uv run pytest tests/test_agent_core.py`。
- [ ] `tests/test_agent_api.py` 啟動 image，對端點送出請求，以字面值斷言 ledger 檔新增的文字。Run `uv run pytest tests/test_agent_api.py`。

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.6-fast-xhigh` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. 在 trunk（AGENT-1 的 head）與 head 各帶 lane token 送同一筆 `POST /agent/extension/AgentApi/transactions`。trunk 沒有 extension，預期 404，記錄這個事實。Save `lane1-regression.txt`. Pass when trunk 回 404、head 回 201 且檔尾文字與預期逐字相同。
- [ ] Lane 2. 帶 stub 簽的有效 JWT 送同一種新增請求。Save `lane2-browser-write.txt`. Pass when 回 201，交易仍是 `!`。
- [ ] Lane 3. 以 `name-zh` 別名「錢包」與「晚餐」新增。Save `lane3-alias.txt`. Pass when posting 帳戶是 `Assets:TW:Cash` 與 `Expenses:Food:Dinner`。
- [ ] Lane 4. 用末段相同的別名新增。Save `lane4-ambiguous.txt`. Pass when 回 422，body 列出兩個候選帳戶，ledger sha256 不變。
- [ ] Lane 5. 對已關閉帳戶、開帳日前的日期、`Assets:錢包` 各送一次。Save `lane5-invalid-account.txt`. Pass when 三次都回 422，ledger sha256 不變。
- [ ] Lane 6. 金額送 `190.5.1`、`1 @ 2`、`-5`、`0.001`。Save `lane6-amount.txt`. Pass when 四次都回 422，ledger sha256 不變。
- [ ] Lane 7. `dry_run: true` 新增。Save `lane7-dry-run.txt`. Pass when 回 200，`entry` 文字與實際寫入時相同，ledger sha256 不變。
- [ ] Lane 8. 同一個 key 先送一次，再同時送五次。Save `lane8-idempotent.txt`. Pass when 第一次回 201，其餘都回 200 且 `created` 為 false，檔案中 `^ik-<key>` 只出現一次。
- [ ] Lane 9. 以 `time: 2026-09-25T07:00:00+08:00` 新增，再帶 token 以 `GET /agent/api/query` 查 `2026-09-24` 到 `2026-09-25` 的交易。Save `lane9-time.txt`. Pass when 交易日期是 `2026-09-25`，metadata 是 `time: "07:00:00"`，查詢結果包含這筆。
- [ ] Lane 10. 全部 lane 完成後帶 token 讀 `GET /agent/api/errors`。Save `lane10-errors.txt`. Pass when errors 數量與開機時相同。

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. fava 首頁回應時間，trunk 與 head 都量。trunk 沒有寫入端點，所以另外量 head 上 `POST transactions` 的回應時間。
- [ ] Probe. `scripts/agent-api-lanes.sh perf-write` 交錯執行 trunk 與 head，各 20 輪。
- [ ] Baseline. 先記錄 trunk 首頁的中位數。
- [ ] Rule. head 首頁中位數超過 trunk 的 1.10 倍即失敗。`POST transactions` 的 p95 超過 300 ms 即失敗（fixture ledger）。

**Review gate.** None. AGENT-2 is not review-gated.

**Merge.**

- [ ] root 在該 head SHA 給出乾淨判定。
- [ ] Bugbot 留言處理完畢。
- [ ] 判定後 rebase 到 `feat/agent-api` 的最新 tip，patch-id 不變。
- [ ] root 把 `feat/agent-api` fast-forward 到該單元的 head。

## 在同一個 extension 加上 MCP 端點 (AGENT-3)

**Depends on.** AGENT-2。

**Files.**

- [ ] Create `src/beancount_agent_api/mcp.py`。
- [ ] Edit `src/beancount_agent_api/extension.py`，註冊 `POST mcp`，並為 `GET mcp`、`DELETE mcp` 回 405。
- [ ] Edit `tests/test_agent_api.py`，加入 MCP 案例。
- [ ] Edit `scripts/agent-api-lanes.sh`，加入 MCP lane。

**Build.**

- [ ] 只實作 MCP `2026-07-28`，依 `docs/research/mcp-spec-2026-09.md` 第 5.3 節與第 10.2 節。處理 `server/discover`、`tools/list`、`tools/call`，notification 回 202。其他方法（含 `initialize`、`ping`）回 HTTP 404 與 `-32601`。驗證 `MCP-Protocol-Version`、`Mcp-Method`、`Mcp-Name` 與 `_meta` 必填欄位。body 是 JSON 陣列時回 `-32600`。結果帶 `resultType`，`tools/list` 帶 `ttlMs` 與 `cacheScope: "private"`。不宣告 `listChanged`。
- [ ] 以一張表定義三個 tool。`list_accounts` 與 `query` 在 process 內呼叫 fava 的 `get_ledger_data()` 與 `ledger.query_shell.execute_query_serialised()`，也就是 `/api/ledger_data` 與 `/api/query` 背後的同一段程式。`add_transaction` 呼叫 AGENT-2 的寫入函式。未知 tool 回 `-32602`。驗證錯誤回 `isError: true`，附 `structuredContent` 與同內容的 TextContent。

**You see.**

- [ ] `claude mcp add --transport http beancount <url>/agent/extension/AgentApi/mcp --header "Authorization: Bearer lane-token"` 之後，`claude mcp list` 顯示 `beancount: ... (HTTP) - ✔ Connected`，container log 出現 `server/discover`。

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] `tests/test_agent_api.py` 送 `server/discover`、`tools/list`、`tools/call`，斷言 tool 名稱清單與新增後 ledger 的字面文字。另外涵蓋 header 缺少、header 與 body 不符、版本不支援、缺 `_meta` 四個錯誤案例的字面錯誤碼。Run `uv run pytest tests/test_agent_api.py`。

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.6-fast-xhigh` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. 對 trunk（AGENT-2 的 head）與 head 執行 `claude mcp list`。trunk 沒有 MCP 端點，預期連線失敗，記錄這個事實。Save `lane1-regression.txt`. Pass when trunk 失敗、head 顯示 `✔ Connected`，記錄中有 Claude Code 版本、`server/discover` 請求與協商出的版本 `2026-07-28`。
- [ ] Lane 2. 以 `curl` 送帶完整 header 與 `_meta` 的 `tools/list`。Save `lane2-tools-list.txt`. Pass when 回傳剛好 `list_accounts`、`query`、`add_transaction` 三個 tool，結果帶 `resultType`、`ttlMs`、`cacheScope`。
- [ ] Lane 3. `claude -p --strict-mcp-config` 呼叫 `list_accounts` 與 `query`，再以 `curl` 帶 token 呼叫 `/api/ledger_data` 與 `/api/query` 做同樣的查詢。Save `lane3-read-tools.txt`. Pass when 輸出包含 `Assets:TW:Cash` 與 `錢包`，`query` 的 rows 與 `/api/query` 的 rows 相同。
- [ ] Lane 4. `claude -p` 呼叫 `add_transaction` 並帶 `dry_run`。Save `lane4-dry-run.txt`. Pass when 輸出含將寫入的交易文字，ledger sha256 不變。
- [ ] Lane 5. `claude -p` 記錄「9/24 晚餐 190（錢包）」。Save `lane5-add.txt`. Pass when ledger 新增一筆 `2026-09-24 !` 交易，posting 為 `Expenses:Food:Dinner 190 TWD` 與 `Assets:TW:Cash`。
- [ ] Lane 6. 先用 REST 以 key `shared` 新增，再用 MCP 以同一個 key 新增。Save `lane6-shared-key.txt`. Pass when MCP 結果的 `created` 為 false，檔案中 `^ik-shared` 只出現一次。
- [ ] Lane 7. 以錯誤 token 註冊後執行 `claude mcp list`。Save `lane7-wrong-token.txt`. Pass when 顯示連線失敗，container log 有 401。
- [ ] Lane 8. 不帶 token 註冊後執行 `claude mcp list`。Save `lane8-no-token.txt`. Pass when 顯示連線失敗，OAuth discovery 路徑與 `POST /register` 都回 401，ledger sha256 不變。
- [ ] Lane 9. 以 `curl` 送 `initialize`、`resources/list` 與一個 JSON 陣列 body。Save `lane9-protocol-errors.txt`. Pass when 前兩者回 HTTP 404 與 `-32601`，陣列回 `-32600`。
- [ ] Lane 10. 以 `curl` 送 `GET` 與 `DELETE` 到 MCP 端點。Save `lane10-methods.txt`. Pass when 兩者都回 405。

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. `add_transaction` 經 MCP 的回應時間，與同一筆經 REST 的回應時間。trunk 沒有 MCP，所以 MCP 另設絕對上限。
- [ ] Probe. `scripts/agent-api-lanes.sh perf-mcp` 交錯送出 REST 與 MCP 各 20 輪，每輪用不同 key。
- [ ] Baseline. 先記錄 REST 的中位數。
- [ ] Rule. MCP 中位數超過 REST 中位數加 20 ms 即失敗。MCP p95 超過 300 ms 即失敗。

**Review gate.** None. AGENT-3 is not review-gated.

**Merge.**

- [ ] root 在該 head SHA 給出乾淨判定，合併前以 `interrogate/SKILL.md` 對 `mcp-spec-2026-09.md` 第 5.3 節的清單做一次對抗審查。
- [ ] Bugbot 留言處理完畢。
- [ ] 判定後 rebase 到 `feat/agent-api` 的最新 tip，patch-id 不變。
- [ ] root 把 `feat/agent-api` fast-forward 到該單元的 head。

## 加入記帳 Skill、部署說明與研究筆記 (AGENT-4)

**Depends on.** AGENT-3。

**Files.**

- [ ] Create `skills/beancount-ledger/SKILL.md`。
- [ ] Create `docs/agent-api.md`。
- [ ] Edit `compose.example.yaml`，加入 `AGENT_API_TOKEN_FILE`、`CF_ACCESS_TEAM_DOMAIN`、`CF_ACCESS_AUD` 與 Unraid 的 `99:100` 說明。
- [ ] Edit `docs/research/agent-write-api-and-public-exposure.md`，在開頭更正區塊加入本計畫的決定，並依 `mcp-spec-2026-09.md` 第 9 節更正第 7 節。

**Build.**

- [ ] `SKILL.md` 不附腳本，寫兩條等價的路徑。已註冊 MCP 時用 MCP tool。沒有 MCP 時，讀取用帶 token 的 `curl` 呼叫 fava `GET /api/ledger_data` 與 `GET /api/query`，寫入用帶 token 的 `curl` 呼叫 extension 的 `POST transactions`。流程是先查帳戶，再查同日同額的交易，有疑似重複時先問使用者，每筆交易產生一個 key 並在重試時沿用，帳戶有多個候選時先問使用者。token 從 `BEANCOUNT_AGENT_TOKEN` 環境變數讀取。
- [ ] `docs/agent-api.md` 寫部署步驟。產生 token 檔、設定三個環境變數、在 `main.beancount` 加上 extension 行、在 `.mcp.json` 以 `${BEANCOUNT_AGENT_TOKEN}` 註冊 MCP、說明區網瀏覽器改走 Cloudflare 網址。

**You see.**

- [ ] 在載入此 Skill 的 `claude -p` session 中輸入「9/25 晚餐 410（錢包）」，ledger 新增一筆 `2026-09-25 !` 交易，Agent 回覆中列出寫入的交易文字。

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] `tests/test_image.py` 新增一案，以 `docs/agent-api.md` 的 ledger 設定行與 `compose.example.yaml` 的環境變數啟動 image，帶 token 讀 `GET /agent/api/errors`，斷言回傳空陣列。Run `uv run pytest tests/test_image.py`。

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.6-fast-xhigh` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. 在 trunk（AGENT-3 的 head，無 Skill）與 head 各以 `claude -p` 輸入「9/24 晚餐 190（錢包）」。trunk 沒有 Skill，記錄 Agent 實際做了什麼。Save `lane1-regression.txt`. Pass when head 寫入一筆正確交易，container log 中沒有 403。
- [ ] Lane 2. 輸入含末段相同別名的記帳要求。Save `lane2-ambiguous.txt`. Pass when Agent 列出候選並提問，ledger sha256 不變。
- [ ] Lane 3. 同一筆要求在新的 session 再輸入一次。Save `lane3-duplicate.txt`. Pass when Agent 回報疑似重複並提問，ledger 沒有第二筆。
- [ ] Lane 4. 輸入「今天早上 7 點早餐 80（錢包）」，session 時區為 `Asia/Taipei`。Save `lane4-time.txt`. Pass when 交易日期是當地日期，有 `time` metadata。
- [ ] Lane 5. 輸入一個不存在的帳戶名稱。Save `lane5-unknown-account.txt`. Pass when Agent 回報找不到帳戶，ledger sha256 不變。
- [ ] Lane 6. 要求 Agent「把 9/24 那筆刪掉」。Save `lane6-delete-refused.txt`. Pass when Agent 說明無法刪除並指向 fava 網頁，ledger sha256 不變。
- [ ] Lane 7. 要求 Agent「直接核准 9/24 那筆」。Save `lane7-approve-refused.txt`. Pass when Agent 說明核准要在 fava 網頁做，該筆仍是 `!`。
- [ ] Lane 8. 不註冊 MCP，只載入 Skill，一次輸入兩筆記帳。Save `lane8-no-mcp.txt`. Pass when 新增兩筆，各有不同的 `ik-` link，讀取請求打在 `/api/ledger_data` 或 `/api/query`，寫入請求打在 `POST transactions`。
- [ ] Lane 9. 依 `docs/agent-api.md` 的步驟，從空白 container 開始部署。Save `lane9-deploy-doc.txt`. Pass when 照文件執行後 `claude mcp list` 顯示 `✔ Connected`，沒帶憑證的請求回 401。
- [ ] Lane 10. 依 `compose.example.yaml` 啟動並送出一筆新增。Save `lane10-compose.txt`. Pass when 回 201，errors 為空。

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. 從輸入記帳要求到 ledger 出現交易的總時間，以及過程中的 `tools/call` 次數。trunk 沒有 Skill，所以兩者都設絕對上限。
- [ ] Probe. `scripts/agent-api-lanes.sh perf-skill` 把 lane 1 的記帳要求在 trunk 與 head 交錯各跑 5 次，每次用新的 ledger，記錄總時間與 container log 中的 `tools/call` 數。AGENT-4 的 lane 5 不寫入，所以改用 lane 1 的要求。
- [ ] Baseline. 先記錄 trunk 上同一要求的總時間與 `tools/call` 數。
- [ ] Rule. 單筆記帳的 `tools/call` 超過 3 次即失敗。總時間中位數超過 trunk 中位數的 1.5 倍即失敗。

**Review gate.** None. AGENT-4 is not review-gated.

**Merge.**

- [ ] root 在該 head SHA 給出乾淨判定。
- [ ] Bugbot 留言處理完畢。
- [ ] 判定後 rebase 到 `feat/agent-api` 的最新 tip，patch-id 不變。
- [ ] root 把 `feat/agent-api` fast-forward 到該單元的 head。

## Close the program

- [ ] 上面每個方塊都有證據並已勾選。
- [ ] 使用者完成部署。在 Unraid 產生 token 檔，設定 `AGENT_API_TOKEN_FILE`、`CF_ACCESS_TEAM_DOMAIN`、`CF_ACCESS_AUD`，更新 image，在 `main.beancount` 加入 extension 行，在 Claude Code 註冊 MCP。部署後區網瀏覽器直連 5656 會得到 401，瀏覽器改走 Cloudflare 網址。這些是使用者的操作，Agent 不代為執行。
- [ ] 回報 PR 連結、每個單元一行判定摘要，以及被擱置或排除的項目與原因。

## Appendix A. Prototype evidence

以下原型都用與伺服器相同的 image（`sha256:b77b0a1c964439cb4dd31b006f459318f60f10a910074a3d6059ae0ccaac8be4`，fava 1.30.16、beancount 3.2.3），放在 session scratchpad，沒有分支也沒有 commit，container 已刪除。

問題一。fava extension 能否提供 POST 端點並在同一個 process 內冪等寫入。實測結果如下。第一次新增回 201，同 key 重送回 200。同一個 key 同時送五次，回應是一個 201 與四個 200，檔案中該 link 只出現一次。`GET api/errors` 回傳空陣列。原型從 ledger 目錄載入 module。改由 image 內安裝的 package 載入是推論，依據是 `fava/ext/__init__.py` 的 `find_extensions` 只把 ledger 目錄加到 `sys.path` 前面再 `import_module`，AGENT-2 的測試會驗證。

問題二。extension 的 `before_request` hook 能否保護 fava 原有的 `/api/`。實測結果如下。沒有憑證時 `/api/ledger_data`、`/api/query`、`PUT /api/add_entries`、`income_statement/` 都回 401，帶 Bearer 或 Basic 都回 200。但 `/api/changed` 與 `/api/errors` 沒有憑證也回 200，原因是 `application.py` 的 `_perform_global_filters` 對這兩個端點提早返回，不執行 extension hook。這個漏洞是 AGENT-1 改用 WSGI middleware 的理由，lane 10 驗證漏洞已關閉。

問題三。fava `/api/query` 的回應格式。對伺服器的 fava 以 GET 實測，回傳 `{"data":{"rows":[...],"t":"table","types":[...]}}`，`position` 是 `{"cost":null,"units":{"currency":"TWD","number":190.00}}`。

問題四。MCP `2026-07-28` 與 Claude Code 2.1.284 的相容性。手寫的 legacy 原型在 `claude mcp list` 顯示 `✔ Connected`，`claude -p` 呼叫 `list_accounts` 回傳正確清單。`mcp-spec-2026-09.md` 第 8 節記錄了只實作 `2026-07-28` 的 probe，`claude -p` 與 `claude mcp list` 都成功，每次連線 2 個請求。正式實作要在 AGENT-3 lane 1 重新證明。

## Appendix B. Alternatives rejected

**獨立的 API container 經 HTTP 呼叫 fava。** 需要第二個 container、第二個 port、Unraid 自訂網路、FastAPI、uvicorn、`mcp` SDK，冪等檢查與寫入分屬兩個 process。extension 做法沒有這些成本，原型已證明冪等與並行去重成立。

**以 extension 的 `before_request` hook 做認證。** 成本最低，但有兩個缺點。`/api/changed` 與 `/api/errors` 不經過 hook（Appendix A 問題二）。防護依賴 ledger 中的 `custom "fava-extension"` 行，刪掉這行防護就消失。WSGI middleware 包住所有路徑，由 image 的啟動程式決定，與 ledger 內容無關。

**只包裝 fava `/api/`，不擋原本的 `/api/`。** 原本的 `/api/` 仍在同一個 port 且沒有認證，Agent 可以直接繞過包裝層。

**better-auth 登入頁（password、passkey、SSO）。** 依 `browser-auth-options.md`，`http://192.168.2.11:5656` 不是 secure context，也不能當 RP ID，所以不能用 passkey。RFC 9700 禁止 `http` redirect URI，所以不能用 SSO。password 在純 HTTP 上明文傳送。passkey 與 SSO 只能在 Cloudflare 網址使用，而那條路徑已經有 Access。better-auth 另外需要 Node 應用程式、資料庫與登入頁。

**以來源 IP 信任 cloudflared。** Docker 預設保留 `CAP_NET_RAW`，來源 IP 可以偽造。依 Unraid 網路模式，被信任的範圍可能擴大到整個 bridge、整個區網或整台主機。

**HTTP Basic。** 每個請求都以明文送出密碼，而且要在 image 中保存瀏覽器用的秘密。

**MCP 同時支援 legacy 與 `2026-07-28`。** 要維護兩條路徑，目前沒有必須使用 legacy 的 client。

**以 `mcp` Python SDK 實作 MCP。** SDK 的 HTTP 層是 Starlette/ASGI，無法掛在 fava 的 Flask/WSGI 上。

## Appendix C. Risks

- **image 的預設行為改變。** AGENT-1 之後，沒有認證設定的 container 拒絕啟動，舊的 compose 設定升級後會無法啟動。落在 AGENT-1。PR 描述與 `docs/agent-api.md` 要寫明升級步驟。
- **區網 token 明文傳送。** 5656 是純 HTTP。區網上能監聽封包的裝置可以取得 token，拿到後能讀整本帳並新增 `!` 交易，不能修改、刪除或核准。落在 AGENT-1，寫進 `docs/agent-api.md`。
- **JWT 可在區網重放。** 取得有效 JWT 的人在 `exp` 之前可以直接送到 5656。縮短 Access 的 session duration 可以縮小這個時間窗。落在 AGENT-1，寫進 `docs/agent-api.md`。
- **JWKS 取不到時瀏覽器無法使用。** fava 要能連到 `<team>.cloudflareaccess.com`。取不到時 guard 拒絕瀏覽器請求，Agent 的 token 路徑不受影響。落在 AGENT-1，lane 8 驗證。
- **啟動程式沒有重做 fava CLI 的所有選項。** `serve.py` 只支援目前用到的 `BEANCOUNT_FILE` 與 `FAVA_HOST`。其他 `FAVA_*` 選項需要時再加。落在 AGENT-1，Renovate 升 fava 版時由 `tests/test_image.py` 檢查 `create_app` 的簽章。
- **fava extension API 標為 unstable。** fava 升版可能改變 `FavaExtensionBase`、`extension_endpoint`、`ledger.file.insert_entries` 或 `ledger.attributes.links`。落在 AGENT-2，由 `tests/test_agent_api.py` 以實際 image 驗證。
- **MCP legacy client 無法連線。** Claude Code 設定 `MCP_PROTOCOL_NEGOTIATION=legacy`、使用 v1 runtime 或版本低於 2.1.232 時會失敗。落在 AGENT-3，寫進 `docs/agent-api.md`。
- **冪等只在單一 fava process 內成立。** 鎖是 process 內的 `threading.Lock`，fava 以單一 process 執行時成立。落在 AGENT-2，由 lane 8 的並行測試驗證。
- **寫入後帳本錯誤。** fava `insert_entries` 寫檔前不驗證整本帳。AGENT-2 在寫入前檢查帳戶的開帳日與關帳日，寫入後回報 errors 數量的差，不做自動回滾。
- **跨年。** `default-file` 固定指向 `txns/2026.beancount`，2027 年要由使用者更新。寫進 `docs/agent-api.md`。
- **以 `fava` 覆寫 CMD 會略過 guard。** `docker-entrypoint.sh` 接受 `fava` 指令，這時 fava 沒有 guard，任何人都能讀寫。依決定不阻擋。落在 AGENT-1，寫進 `docs/agent-api.md`。
- **偽造 JWT 的 `kid` 會觸發 JWKS 重新下載。** `kid` 不在快取中時，`PyJWKClient` 重新下載公鑰，每次最多等 2 秒，可能佔住 fava 的 worker。這是由程式碼推論，沒有量測。本分支沒有修正。落在 AGENT-1，寫進 `docs/agent-api.md`。
- **沒有請求 body 的大小上限。** guard 先檢查憑證，所以只有持有 token 或有效 JWT 的人能送出大 body。落在 AGENT-2，寫進 `docs/agent-api.md`。
- **key 與 narration 沒有長度上限，narration 接受 `Cf` 字元。** 例如 U+202E 會讓顯示順序與實際文字不同。使用者核准前要仔細讀 narration。落在 AGENT-2，寫進 `docs/agent-api.md`。
- **MCP 的 `query` 不套用 fava 的篩選條件與時間範圍。** 條件要寫在 BQL 的 `WHERE`。落在 AGENT-3，寫進 `docs/agent-api.md`。

## Appendix D. Links and reading list

- `docs/research/agent-write-api-and-public-exposure.md` 第 2、3、6、9 節。
- `docs/research/ezbookkeeping-api.md` 第 12、13 節。
- `docs/research/mcp-spec-2026-09.md` 第 5、8、10 節。
- `docs/research/browser-auth-options.md` 第 6 節與第 10 節。
- fava 1.30.16 原始碼 `src/fava/cli.py`、`src/fava/application.py` 第 255 至 362 行、`src/fava/ext/__init__.py`、`src/fava/core/file.py` 第 239 至 261 行。
- AGENT-1 動工前讀 pstack 的 `how/SKILL.md`。AGENT-3 合併前以 `interrogate/SKILL.md` 做一次對抗審查。
- 決策記錄依 `show-me-your-work/SKILL.md` 保留在本機，不 commit。
