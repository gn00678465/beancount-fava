# fava 的 agent API

本文前半是部署步驟，後半是參考資料。

這個 image 的 fava 對每個請求檢查憑證。

- 瀏覽器經由 Cloudflare Access 登入。fava 驗證 Access 簽發的 JWT。
- Agent（例如 Claude Code）在區網以 Bearer token 連線。
- Agent 可以讀 fava `/api/` 的 GET 端點。Agent 經由 `AgentApi` extension 新增交易。
- Agent 新增的交易一律是 `!` flag。使用者在 fava 網頁核准交易。
- Agent 不能修改、刪除或核准交易。

## 部署到 Unraid

下列步驟使用這些值。角括號中的值請換成你的值。

| 項目 | 值 |
|---|---|
| fava 的區網網址 | `http://192.168.2.11:5656`（container 的 5000 port） |
| container 的使用者 | `99:100`（Extra Parameters 中的 `--user 99:100`） |
| ledger 目錄 | 主機 `/mnt/user/appdata/beancount/ledger`，container `/ledger` |
| ledger 主檔 | `/ledger/main.beancount`，URL slug 是 `beancount` |
| token 檔 | 主機 `/mnt/user/appdata/beancount/agent-token`，container `/run/secrets/agent-token` |
| Cloudflare Access 的 team | `<team>` |
| fava application 的 AUD tag | `<aud-tag>` |
| fava 的 Cloudflare 網址 | `https://<fava-hostname>` |

### 先閱讀這些限制

- 這次升級會使舊設定無法啟動。新 image 啟動時檢查憑證設定。設定不足時，container 以結束碼 1 停止。設定不足的意思是：沒有 `AGENT_API_TOKEN_FILE`，也沒有同時設定 `CF_ACCESS_TEAM_DOMAIN` 與 `CF_ACCESS_AUD`。log 是這一行：

  ```text
  beancount-fava-serve: no credential is configured: set AGENT_API_TOKEN_FILE for agents, or CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD for browsers behind Cloudflare Access
  ```

- 先做步驟 1 到 3，再更新 image。
- 升級後，區網瀏覽器開 `http://192.168.2.11:5656` 會得到 401。瀏覽器改用 fava 的 Cloudflare 網址（步驟 7）。
- 使用本變更合併後發佈的 image。舊 image 不讀這三個環境變數，也沒有憑證檢查。`compose.example.yaml` 的 digest 在 Renovate 更新前仍指向舊 image。

### 1. 建立 token 檔

在 Unraid 的終端機執行這些指令。

```sh
(umask 077 && openssl rand -hex 32 > /mnt/user/appdata/beancount/agent-token)
chown 99:100 /mnt/user/appdata/beancount/agent-token
chmod 400 /mnt/user/appdata/beancount/agent-token
```

- token 檔放在 ledger 目錄外。
- uid 99 必須能讀這個檔。檔案不能是空的。fava 會去掉 token 前後的空白與換行。
- fava 只在啟動時讀 token 檔。更換 token 後，重新啟動 container。

### 2. 取得 team domain 與 AUD tag

1. 在 Cloudflare dashboard 開啟 **Zero Trust** > **Settings**。記下 team domain `<team>.cloudflareaccess.com`。
2. 開啟 **Zero Trust** > **Access controls** > **Applications**。在 fava 的 application 選 **Configure**。在 **Additional settings** 複製 **Application Audience (AUD) Tag**。

這兩個值不是秘密。

fava 要能連到 `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` 取得公鑰。連不到時，瀏覽器的請求得到 401，agent 的 token 不受影響。

### 3. 更新 container 設定

在 Unraid 的 **Docker** 頁面編輯 fava container。

1. 新增一個 **Path**。**Container Path** 是 `/run/secrets/agent-token`，**Host Path** 是 `/mnt/user/appdata/beancount/agent-token`，**Access Mode** 是 **Read Only**。
2. 新增 **Variable** `AGENT_API_TOKEN_FILE`，值是 `/run/secrets/agent-token`。
3. 新增 **Variable** `CF_ACCESS_TEAM_DOMAIN`，值是 `https://<team>.cloudflareaccess.com`。值要以 `https://` 開頭。
4. 新增 **Variable** `CF_ACCESS_AUD`，值是 `<aud-tag>`。
5. 保留 `BEANCOUNT_FILE=/ledger/main.beancount`、ledger 的 Path、`--user 99:100` 與 5656 對 5000 的 port。
6. **Post Arguments** 保持空白。不要把指令改成 `fava`（見「風險」）。

同樣的設定以 `docker run` 表示如下。

```sh
docker run -d --name beancount-fava --user 99:100 -p 5656:5000 \
  -v /mnt/user/appdata/beancount/ledger:/ledger \
  -v /mnt/user/appdata/beancount/agent-token:/run/secrets/agent-token:ro \
  -e BEANCOUNT_FILE=/ledger/main.beancount \
  -e AGENT_API_TOKEN_FILE=/run/secrets/agent-token \
  -e CF_ACCESS_TEAM_DOMAIN=https://<team>.cloudflareaccess.com \
  -e CF_ACCESS_AUD=<aud-tag> \
  gn00678465/beancount-fava:<tag>
```

### 4. 在 main.beancount 載入 extension

在 `/mnt/user/appdata/beancount/ledger/main.beancount` 加入這一行。

```beancount
2020-01-01 custom "fava-extension" "beancount_agent_api"
```

沒有這一行時，憑證檢查仍然有效，但寫入端點與 MCP 端點回 404。

extension 把新交易寫入 `default-file` 指定的檔案。有符合的 `insert-entry` 選項時，依該選項寫入。

### 5. 更新 image 並檢查

1. 在 Unraid 套用設定。Unraid 拉取新 image 並重新建立 container。tag 沒有改變時，在 **Docker** 頁面的進階檢視對 fava 選 **Force Update**。
2. 檢查 log 中有 `Starting Fava on http://0.0.0.0:5000` 這一行。

   ```sh
   docker logs beancount-fava
   ```

3. 送一個沒有憑證的請求。回應必須是 `HTTP/1.1 401 Unauthorized`。

   ```sh
   curl -i http://192.168.2.11:5656/beancount/api/errors
   ```

4. 帶 token 送同一個請求。回應的 `data` 必須是 `[]`。`data` 中有 `beancount_agent_api` 的錯誤時，檢查步驟 4 的那一行。

   ```sh
   curl -sS -H "Authorization: Bearer $(cat /mnt/user/appdata/beancount/agent-token)" \
     http://192.168.2.11:5656/beancount/api/errors
   ```

### 6. 在 Claude Code 註冊 MCP 與 Skill

在執行 Claude Code 的電腦上做這些步驟。

1. 在 shell 的設定檔設定兩個環境變數。`BEANCOUNT_AGENT_TOKEN` 的值是 token 檔的內容。

   ```sh
   export BEANCOUNT_AGENT_TOKEN='<token 檔的內容>'
   export BEANCOUNT_FAVA_URL=http://192.168.2.11:5656/beancount
   ```

2. 在專案根目錄建立 `.mcp.json`。Claude Code 啟動時把 `${BEANCOUNT_AGENT_TOKEN}` 換成環境變數的值。不要把 token 本身寫進 `.mcp.json`。

   ```json
   {
     "mcpServers": {
       "beancount": {
         "type": "http",
         "url": "http://192.168.2.11:5656/beancount/extension/AgentApi/mcp",
         "headers": {
           "Authorization": "Bearer ${BEANCOUNT_AGENT_TOKEN}"
         }
       }
     }
   }
   ```

3. 在同一個目錄執行 `claude`，核准 `.mcp.json` 中的 `beancount` server。沒有核准時，`claude mcp list` 顯示 `Pending approval`。也可以在 `~/.claude/settings.json` 加入 `"enabledMcpjsonServers": ["beancount"]` 來核准。
4. 在同一個目錄執行 `claude mcp list`。輸出必須有這一行。

   ```text
   beancount: http://192.168.2.11:5656/beancount/extension/AgentApi/mcp (HTTP) - ✔ Connected
   ```

5. 把本 repo 的 `skills/beancount-ledger/` 複製到 `~/.claude/skills/beancount-ledger/`。只給一個專案用時，複製到該專案的 `.claude/skills/beancount-ledger/`。
6. 在 Claude Code 輸入一筆帳，例如「9/24 晚餐 190（錢包）」。Agent 回覆寫入的交易文字，交易的 flag 是 `!`。

沒有註冊 MCP 時，Skill 以 `curl` 讀寫。這條路徑需要 curl 8.3 以上。curl 以 `--variable` 從 `BEANCOUNT_FAVA_URL` 與 `BEANCOUNT_AGENT_TOKEN` 讀值，所以指令文字中沒有 token。在 Claude Code 允許 `Bash(curl:*)` 後，Agent 執行這些 curl 時不會逐次要求核准。

### 7. 瀏覽器改用 Cloudflare 網址

以 `https://<fava-hostname>` 開啟 fava。在 fava 網頁核准交易：把 `!` 改成 `*`。修改與刪除也在 fava 網頁做。

## 參考

### 環境變數

fava container 讀取這些變數。

| 變數 | 值 | 說明 |
|---|---|---|
| `BEANCOUNT_FILE` | `/ledger/main.beancount` | ledger 主檔的絕對路徑。多個檔以 `:` 分隔。 |
| `AGENT_API_TOKEN_FILE` | `/run/secrets/agent-token` | agent token 檔的路徑。檔案讀不到或是空的時，container 停止。 |
| `CF_ACCESS_TEAM_DOMAIN` | `https://<team>.cloudflareaccess.com` | JWT 的 `iss` 必須等於這個值。公鑰從 `<值>/cdn-cgi/access/certs` 取得。 |
| `CF_ACCESS_AUD` | `<aud-tag>` | JWT 的 `aud` 必須等於這個值。 |
| `FAVA_HOST` | `0.0.0.0` | fava 監聽的位址。image 的預設值是 `0.0.0.0`，port 固定是 5000。 |

至少要設定 `AGENT_API_TOKEN_FILE`，或同時設定兩個 `CF_ACCESS_*` 變數。只設定一個 `CF_ACCESS_*` 變數時，container 停止。

Claude Code 這一端讀取 `BEANCOUNT_AGENT_TOKEN`（token 的值）與 `BEANCOUNT_FAVA_URL`（`http://192.168.2.11:5656/beancount`）。

### 憑證與路徑規則

fava 依這個順序判斷身分。

1. `Authorization: Bearer <token>` 等於 token 檔的內容時，身分是 agent。
2. `Cf-Access-Jwt-Assertion` header 是有效的 JWT 時，身分是瀏覽器。fava 不讀 `CF_Authorization` cookie。有效的 JWT 符合這些條件：
   - 簽章是 RS256，`kid` 是 team 公鑰中的一把。
   - `aud` 等於 `CF_ACCESS_AUD`，`iss` 等於 `CF_ACCESS_TEAM_DOMAIN`。
   - `exp` 還沒到。
3. 其他情況回 `401 Unauthorized`，header 有 `WWW-Authenticate: Bearer realm="fava"`。

| 身分 | method | 路徑 | 結果 |
|---|---|---|---|
| 瀏覽器 | 全部 | 全部 | 交給 fava |
| agent | `GET` | `/<bfile>/api/<endpoint>` | 交給 fava |
| agent | `POST` | `/<bfile>/extension/AgentApi/<endpoint>` | 交給 extension |
| agent | 全部 | `/<bfile>/extension/AgentApi/mcp` | 交給 extension |
| agent | 其他 | 其他 | `403 Forbidden` |

`<endpoint>` 是一段路徑，不含 `/`。401 與 403 的 body 是純文字的狀態列。

### Agent 可以呼叫的 fava GET 端點

fava 1.30.16 的 GET 端點都開放給 agent：`account_report`、`balance_sheet`、`changed`、`commodities`、`context`、`documents`、`errors`、`events`、`extract`、`help`、`imports`、`income_statement`、`journal`、`journal_page`、`ledger_data`、`narration_transaction`、`narrations`、`options`、`payee_accounts`、`payee_transaction`、`query`、`source`、`source_slice`、`statistics`、`trial_balance`。

`GET /api/source` 回傳 ledger 檔的全文，所以持有 token 的人可以讀整本帳。Skill 只用 `ledger_data` 與 `query`。

fava 的 PUT 與 DELETE 端點對 agent 回 403：`PUT` 的 `add_document`、`add_entries`、`attach_document`、`format_source`、`move`、`source`、`source_slice`、`upload_import_file`，以及 `DELETE` 的 `document`、`source_slice`。

### `POST /<bfile>/extension/AgentApi/transactions`

body 是 JSON 物件，`Content-Type` 是 `application/json`。

| 欄位 | 必填 | 型別 | 規則 |
|---|---|---|---|
| `source` | 是 | string | 錢流出的帳戶。可以是帳戶全名、完整的 `name-zh`，或 `name-zh` 以 `/` 分段的最後一段。 |
| `target` | 是 | string | 錢流入的帳戶。格式同 `source`。 |
| `amount` | 是 | string | `[0-9]{1,15}(\.[0-9]{1,2})?`，大於 0。 |
| `key` | 是 | string | `[A-Za-z0-9\-_/.]+`。寫成 link `^ik-<key>`。 |
| `date` | `date` 與 `time` 擇一 | string | `YYYY-MM-DD`。 |
| `time` | `date` 與 `time` 擇一 | string | RFC 3339，要有時區，例如 `2026-09-25T07:00:00+08:00`。fava 換算成 `Asia/Taipei` 的日期，並加上 metadata `time: "07:00:00"`。 |
| `currency` | 否 | string | 預設是第一個 `operating_currency`。兩個帳戶都必須接受這個幣別。 |
| `narration` | 否 | string | 預設是空字串。不能有 Unicode 類別 `Cc`、`Cs`、`Zl`、`Zp` 的字元。 |
| `dry_run` | 否 | boolean | `true` 時回傳交易文字，不寫入。 |

其他規則：

- 不接受上表以外的欄位。
- 日期不能晚於 `Asia/Taipei` 的今天加 366 天。
- 帳戶在該日期必須是開啟的。開帳日不能晚於該日期。帳戶有關帳日時，關帳日不能早於該日期。
- `source` 與 `target` 不能是同一個帳戶。

寫入的交易是 `!` flag，沒有 payee，`target` 是正數，`source` 是負數。

```beancount
2026-09-24 ! ^ik-d1
  Expenses:Food:Dinner                                  190 TWD
  Assets:TW:Cash                                       -190 TWD
```

處理順序：

1. 取得寫入鎖。10 秒內取不到時回 503。
2. 重新載入 ledger，檢查請求。
3. ledger 已有 `^ik-<key>` 時，比較交易文字，不比較 flag。相同時回 200，不同時回 409。
4. `dry_run` 為 `true` 時回 200。
5. 寫入並回 201。fava 寫入前不檢查整本帳。

| 狀態 | body | 意思 |
|---|---|---|
| 201 | `{"created": true, "link", "entry", "errors": {"before", "after"}}` | 已寫入。`errors` 是寫入前後 fava 的錯誤數量。 |
| 200 | `{"created": false, "link", "entry"}` | 同一個 key 已有相同的交易，沒有寫入。已核准成 `*` 的交易也算相同。 |
| 200 | `{"created": false, "dry_run": true, "link", "entry"}` | 預覽，沒有寫入。 |
| 409 | `{"error": {...}, "entry"}` | `code` 是 `key_conflict`。這個 key 已用在另一筆交易。`entry` 是既有交易的文字。 |
| 422 | `{"error": {...}}` | 請求不符合規則。`code` 見下表。 |
| 503 | `{"error": {...}}` | `code` 是 `busy`。另一筆寫入還沒完成。以同一個 key 重送。 |
| 401 | `401 Unauthorized` | 沒有憑證或憑證無效。 |
| 403 | `403 Forbidden` | 這個身分不能用這個 method 或路徑。 |

`error` 物件一律有 `field`、`code`、`message`、`candidates` 四個欄位。`candidates` 只在 `ambiguous_account` 時有內容。

| 422 的 `code` | `field` | 原因 |
|---|---|---|
| `invalid_body` | 空字串 | body 不是 JSON 物件。 |
| `unknown_field` | 該欄位 | 不接受的欄位。 |
| `invalid_type` | 該欄位 | 型別錯誤。`dry_run` 要是 boolean，其他要是 string。 |
| `missing` | 該欄位 | 缺少必填欄位，或 ledger 沒有 `operating_currency` 且請求沒有 `currency`。 |
| `invalid_key` | `key` | key 有不允許的字元。 |
| `date_or_time` | `date` | `date` 與 `time` 都有或都沒有。 |
| `invalid_date` | `date` | 不是 `YYYY-MM-DD` 或不是有效日期。 |
| `invalid_time` | `time` | 不是帶時區的 RFC 3339。 |
| `date_out_of_range` | `date` 或 `time` | 日期晚於今天加 366 天。 |
| `invalid_amount` | `amount` | 格式錯誤、超過兩位小數或不大於 0。 |
| `invalid_currency` | `currency` | 不是幣別代碼。 |
| `invalid_narration` | `narration` | 有換行或控制字元。 |
| `invalid_account_name` | `source` 或 `target` | 不是別名，也不是有效的帳戶名稱。 |
| `unknown_account` | `source` 或 `target` | 帳戶名稱有效，但 ledger 沒有開這個帳戶。 |
| `ambiguous_account` | `source` 或 `target` | 別名符合多個帳戶，`candidates` 列出它們。 |
| `account_not_open` | `source` 或 `target` | 日期早於開帳日。 |
| `account_closed` | `source` 或 `target` | 日期晚於關帳日。 |
| `same_account` | `target` | `source` 與 `target` 是同一個帳戶。 |
| `currency_not_allowed` | `currency` | 帳戶不接受這個幣別。 |

### MCP 端點 `/<bfile>/extension/AgentApi/mcp`

- 只支援 MCP `2026-07-28`。每個請求獨立處理，回應一律是 JSON，沒有 SSE。
- 支援的方法：`server/discover`、`tools/list`、`tools/call`。
- notification（沒有 `id`）回 202，沒有 body。
- 其他方法（包含 `initialize`、`ping`、`resources/list`）回 HTTP 404 與 `-32601`。
- 每個請求都要有 `MCP-Protocol-Version` 與 `Mcp-Method` header。`tools/call` 另要 `Mcp-Name`。header 與 body 不符時回 400 與 `-32020`。
- `params._meta` 必須有 `io.modelcontextprotocol/protocolVersion` 與 `io.modelcontextprotocol/clientCapabilities`。缺少時回 400 與 `-32602`。
- 版本不是 `2026-07-28` 時回 400 與 `-32022`，`data.supported` 是 `["2026-07-28"]`。
- body 不是單一 JSON 物件（例如 batch 陣列）時回 400 與 `-32600`。body 不是 JSON 時回 400 與 `-32700`。
- 請求有 `Origin` header 時回 403 與 `-32600`。
- `GET`、`PUT`、`DELETE` 回 405，header 有 `Allow: POST`。
- 每個結果都有 `resultType: "complete"`。`server/discover` 與 `tools/list` 另有 `ttlMs: 3600000` 與 `cacheScope: "private"`。
- 每個請求在 container 的 stderr 寫一行 `mcp <method> <tool> <HTTP 狀態> [附註]`，例如 `mcp tools/call add_transaction 200`。

| tool | 參數 | 結果 |
|---|---|---|
| `list_accounts` | 無 | 與 `GET /<bfile>/api/ledger_data` 的 body 相同。 |
| `query` | `query_string`（BQL，必填） | 與 `GET /<bfile>/api/query` 的 body 相同，但沒有 fava 的篩選條件。 |
| `add_transaction` | 與 `POST transactions` 的欄位相同 | 與 `POST transactions` 的 body 相同。 |

`tools/call` 的結果有三個欄位。

- `content`：一個 text，內容是 body 的 JSON 文字。
- `structuredContent`：同一個 body。
- `isError`：body 對應的 HTTP 狀態是 400 以上時為 `true`，例如 422 的驗證錯誤。

未知的 tool 回 400 與 `-32602`。

## 風險

- **以 `fava` 覆寫指令會略過憑證檢查。** container 的指令改成 `fava` 時，fava 啟動時沒有 guard。任何人都能讀寫帳本。image 不阻擋這個設定。保持預設指令 `beancount-fava-serve`，並以步驟 5 的 401 檢查確認。
- **偽造的 JWT 可能拖慢 fava。** JWT 的 `kid` 不在已取得的公鑰中時，fava 重新下載公鑰，每次最多等 2 秒。能連到 5656 的人可以大量送出這種 JWT，佔住 fava 的 worker。這是由程式碼推論，沒有量測。本分支沒有修正。
- **區網的 token 以明文傳送。** 5656 是純 HTTP。能監聽區網封包的裝置可以取得 token。取得 token 的人可以讀整本帳並新增 `!` 交易，不能修改、刪除或核准。懷疑外洩時，更換 token 檔的內容並重新啟動 container。
- **JWT 可以在區網重放。** 取得有效 JWT 的人在 `exp` 之前可以直接送到 5656。縮短 Access application 的 session duration 可以縮小這個時間。
- **沒有 body 大小上限。** guard、extension 與 fava 都不限制請求 body 的大小。guard 先檢查憑證，所以只有持有 token 或有效 JWT 的人能送出大 body。
- **key 與 narration 沒有長度上限，narration 接受 `Cf` 類別的字元。** 例如 U+202E（right-to-left override）會讓顯示順序與實際文字不同。核准前仔細讀 narration，有疑問時在 fava 的編輯器檢查原始文字。
- **強制使用 legacy 協商的 MCP client 無法連線。** 這些 Claude Code 會連線失敗：設定了 `MCP_PROTOCOL_NEGOTIATION=legacy`、使用 v1 runtime，或版本低於 2.1.232。改用 Skill 的 `curl` 路徑。
- **MCP 的 `query` 不套用 fava 的篩選條件與時間範圍。** 在 BQL 的 `WHERE` 寫日期與帳戶條件。
- **`default-file` 指向 `txns/2026.beancount`。** 2027 年起，新增 `txns/2027.beancount`，在 `main.beancount` 加上 `include`，並把 `default-file` 改成新檔。
- **冪等只在同一個 fava process 內成立。** 寫入鎖在 process 內。兩個 fava container 掛同一個 ledger 時，同一個 key 可能寫入兩次。一個 ledger 只跑一個 fava container。
