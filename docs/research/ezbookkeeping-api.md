# ezBookkeeping API、MCP、Skill 研究：本專案 beancount API 的設計參考

查詢日期：2026-09-29。

## 研究目的

使用者不改用 ezBookkeeping。使用者要替自己的 beancount（本 repo 的 fava image）加上 API。本文把 ezBookkeeping 的 REST API、MCP server、Agent Skill 當作**設計參考**，逐項判斷哪些設計可以沿用到 beancount 上。

驗收案例是下面兩筆交易。新 API 必須能正確寫入這兩筆：

- 2026-09-24 晚餐 190 TWD，付款帳戶：錢包
- 2026-09-25 晚餐 410 TWD，付款帳戶：錢包

**架構前提**沿用 `agent-write-api-and-public-exposure.md` 開頭「2026-09-21 複核更正」的方案 (a)：fava 是唯一寫入 process，自建 API 經由 fava JSON API 寫入，並在 API 層補上認證、冪等、驗證、`!` flag、git commit。方案 (a) 的取捨見該筆記第 5.2 節。本文不重寫該筆記的內容，只引用章節。

本文的結構：

- 第 2-11 節：ezBookkeeping 的事實（REST、MCP、Skill）與 fava `add_entries` 的對照。這些是參考資料。
- 第 12 節：對本專案 beancount API 的借鏡。
- 第 13 節：驗收案例。

每一項主張都指向原始碼或官方文件。標示「推論」者由已查證事實推導，但未實際執行。標示「未知」者代表第一手來源沒有答案。標示「實測」者是在本機以本 repo 的 image 執行 Python 片段得到的結果，沒有啟動 fava 服務，也沒有寫入任何 ledger。本文沒有對任何實際服務發出請求。curl、CLI、JSON-RPC 範例都未執行。

引用的版本：

| 對象 | 版本 | commit | 說明 |
|---|---|---|---|
| ezBookkeeping 後端與 Skill | tag `v2.0.1` | `323cf0c683d7889ad0c28377039ced1a44167c1e` | 2026-09-25 發佈，為查詢日的最新 release |
| ezBookkeeping 官方文件網站原始檔 | `mayswind/ezBookkeeping-Website` `main` | `59efbc5e6c94001501dbc5c3c886d302eca57887` | 網站 <https://ezbookkeeping.mayswind.net> 的來源。文件沒有版本 tag，內容對應查詢日的 main |
| fava | tag `v1.30.16` | `d8d426f9cbf08ace852a4a319adb4b9dda853b69` | 與本 repo image 釘選的版本相同 |
| beancount | tag `3.2.3` | `eeda2aa2a3204ffb980dab11220b2abde5e80b38` | 與本 repo image 釘選的版本相同 |
| 實測環境 | image `gn00678465/beancount-fava@sha256:b77b0a1c…` | — | 容器內 `beancount 3.2.3`、`fava 1.30.16`、`beanquery 0.2.0` |

連結縮寫：

- `EZ` = `https://github.com/mayswind/ezbookkeeping/blob/v2.0.1`
- `WEB` = `https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs`
- `FAVA` = `https://github.com/beancount/fava/blob/v1.30.16`
- `BC` = `https://github.com/beancount/beancount/blob/3.2.3`

fava JSON API 的通用事實（路由、錯誤碼、`deserialise` 支援的 directive、寫入非原子）已寫在 `agent-write-api-and-public-exposure.md`。本文只引用章節，不重複。

---

## 1. 摘要

### 1.1 對本專案 beancount API 的結論

詳細理由見第 12 節。

| 主題 | 結論 |
|---|---|
| 認證 | 沿用「預設停用」與「REST token、MCP token 分離」。不沿用「token 沒有權限範圍」：本 API 只提供新增，不提供修改與刪除 |
| 金額 | 不沿用 1/100 整數。beancount 用 `Decimal`，fava `add_entries` 接受字串 `"190 TWD"`。API 接受十進位字串，原樣交給 fava |
| TWD 精度 | beancount 沒有內建幣別精度。整數金額不參與 tolerance 推定，沒有 `inferred_tolerance_default` 時容差為 0。用自動補平的 posting 時兩筆都平衡（實測） |
| 時間 | beancount 交易只有日期。API 必須用設定的帳本時區（`Asia/Taipei`）把時刻換成日期。image 沒有設定 `TZ`（推論：容器時區為 UTC），不能用容器時區 |
| 帳戶名稱 | beancount 3.2.3 **不接受** `Assets:錢包`：每個帳戶元件的第一個字元必須是 Unicode 大寫字母（`\p{Lu}`）或數字，中文字是 `\p{Lo}`（原始碼 + 實測）。用 `open` 的 metadata（例如 `name: "錢包"`）做顯示名稱與別名。fava 沒有別名功能，但 `ledger_data` 會回傳 `open` 的 metadata |
| 分類 | ezBookkeeping 的二級分類對應 beancount 的 `Expenses:*` 帳戶。「晚餐」對應哪個帳戶由使用者決定 |
| 冪等 | 不沿用記憶體 `clientSessionId`。沿用既有筆記第 6.3 節的 link 方案。API 查 link 前要先呼叫 `GET api/changed`，因為 fava 的 JSON API 請求不會自動重新載入 |
| dry_run | 沿用 ezBookkeeping MCP 的 `dry_run` 參數。fava 沒有對應的 endpoint，要依既有筆記第 6.5 節在 API 內實作 |
| MCP | 最小 tool 集合：`add_transaction`、`list_accounts`、`query_balances`、`query_transactions`。前兩個對應 fava 的 `add_entries`、`ledger_data`，後兩個對應 fava 的 `query`（beanquery） |
| Skill | 「SKILL.md + 腳本呼叫 REST」的結構可以沿用。ezBookkeeping 腳本的缺點（`.env` 路徑與文件不符、不送重送保護欄位）要避開 |

### 1.2 ezBookkeeping 的事實摘要

| 主題 | 結論 |
|---|---|
| 程式化介面 | ezBookkeeping 有三種官方介面：HTTP REST API（`/api/v1/...`）、內建於後端的 MCP server（`/mcp`）、Agent Skill（包裝 REST API 的 shell 腳本）。三者都在主 repo 中 |
| REST 認證 | `Authorization: Bearer <token>`。API token 預設**停用**，要設定 `enable_api_token = true`（`[security]` 區段）或環境變數 `EBK_SECURITY_ENABLE_API_TOKEN=true` |
| MCP 認證 | 使用獨立的 MCP token（token type 5）。MCP 預設**停用**，要設定 `enable_mcp = true`（`[mcp]` 區段）。MCP token 不能呼叫 REST API，API token 也不能呼叫 MCP |
| 新增交易 | REST：`POST /api/v1/transactions/add.json`，帳戶與分類以 **ID** 指定。MCP：tool `add_transaction`，帳戶與分類以**名稱**指定 |
| 金額單位（REST） | `sourceAmount` 是 int64，單位是 **1/100**。190 TWD 要送 `19000`。與幣別無關 |
| 金額單位（MCP） | `amount` 是**十進位字串**，例如 `"190"`。伺服器端再乘 100 |
| 時間（REST） | `time` 是 **Unix 秒**，`utcOffset` 是分鐘。另外**必須**送 `X-Timezone-Name` 或 `X-Timezone-Offset` header，否則回 400 |
| 冪等 | REST 有未寫入文件的 `clientSessionId` 欄位。伺服器端用**記憶體**快取，預設保留 300 秒。重啟即失效。MCP 與 Skill 都不送這個欄位 |
| beancount | ezBookkeeping 只能**匯入** beancount 檔，不能匯出成 beancount。匯入不支援 `include`、多於兩個 posting 的交易、沒有寫金額的 posting |
| API 穩定性 | 官方沒有穩定性聲明。v2.0.0 與 v2.0.1（patch 版）的 release notes 都在 `[Breaking]` 下列出 API 行為變更 |

### 1.3 待使用者決定的項目

1. 「錢包」對應的 beancount 帳戶名稱（例如 `Assets:Cash`、`Assets:Cash:Wallet`）。中文不能當帳戶元件的開頭字元（第 12.4 節）。
2. 「晚餐」對應的費用帳戶（例如 `Expenses:Food:Dinner`、`Expenses:Food`）。
3. 帳戶是否加上 `name:` metadata 作為中文顯示名稱與 API 別名。
4. Agent 寫入的 flag：`!`（既有筆記第 6.2 節的建議）或 `*`。
5. 帳本時區：本文假設 `Asia/Taipei`。
6. 交易時刻是否保存。beancount 交易沒有時刻欄位，要保存時用 metadata（第 12.3 節）。

---

## 2. ezBookkeeping REST 認證

### 2.1 Token 類型

`EZ/pkg/core/token_claims.go` 定義 token type：

| 值 | 常數 | 用途 |
|---|---|---|
| 1 | `USER_TOKEN_TYPE_NORMAL` | 網頁登入 |
| 2 | `USER_TOKEN_TYPE_REQUIRE_2FA` | 登入後等待 2FA |
| 5 | `USER_TOKEN_TYPE_MCP` | MCP |
| 8 | `USER_TOKEN_TYPE_API` | API token |

其他值（3、4、6、7）用於 email 驗證、重設密碼、OAuth2 callback。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/core/token_claims.go#L21-L31>

`/api/v1` 路由群組套用 `JWTAuthorizationByHeader` 與 `APITokenIpLimit`。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go#L336-L338>

`jwtAuthorization` 只接受 type 1 與 type 8。type 8 要求 `config.EnableAPIToken` 為 true，否則回 `ErrAPITokenNotEnabled`（HTTP 403）。type 5（MCP token）在此被拒絕，回 `ErrCurrentInvalidTokenType`（HTTP 401）。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/middlewares/authorization.go#L149-L185>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/errs/token.go>

### 2.2 啟用 API token

設定檔 `[security]` 區段：

```ini
# Set to true to enable API token generation
enable_api_token = false

# Allowed remote IPs for using the API token ... leave blank to allow all remote IPs
api_token_allowed_remote_ips =
```

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/conf/ezbookkeeping.ini#L431-L435>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/settings/setting.go#L1106-L1107>

每個設定項都可用環境變數 `EBK_{SECTION}_{OPTION}` 覆寫，所以對應的環境變數是 `EBK_SECURITY_ENABLE_API_TOKEN=true`。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/settings/setting.go#L1556-L1580>（前綴常數 `EBK` 在 L21）、`WEB/configuration/index.md` 第 13 行

`api_token_allowed_remote_ips` 非空時，`APITokenIpLimit` 只放行符合的 IP，否則回 `ErrIPForbidden`。這個限制只套用在 type 8，不套用在網頁登入 token。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/middlewares/api_token_ip_limit.go>

### 2.3 取得 token

官方文件寫了兩種取得 API token 的方式：桌面版「使用者設定 → 安全」頁的「Generate Token」按鈕，以及 CLI 指令 `user-session-new`。
來源：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/httpapi/index.md>（網站頁面：<https://ezbookkeeping.mayswind.net/httpapi/>）

**(a) CLI**

```sh
ezbookkeeping userdata user-session-new --username <USER> --type api --expiresInSeconds 0
```

- `--type` 接受 `api` 與 `mcp`，預設 `api`。
- `--expiresInSeconds` 必填，範圍 0 到 4294967295。CLI 說明寫「0 means no expiration」。
- `enable_api_token` 為 false 時，CLI 也拒絕產生 API token（`ErrAPITokenNotEnabled`）。

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/user_data.go#L253-L275>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/user_data.go#L722-L758>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/cli/user_data.go#L409-L434>

CLI 的執行檔名稱與在 Docker 中的呼叫方式，本文沒有查證。

**(b) REST（由網頁登入 token 換成 API token）**

1. 登入：`POST /api/authorize.json`，body `{"loginName": "...", "password": "..."}`。回應 `result` 含 `token` 與 `need2FA`。這個 endpoint 只在 `enable_internal_auth = true`（預設 true）時註冊。
   來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go#L289-L291>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/user.go#L160-L163>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/auth_response.go>
2. 產生 API token：`POST /api/v1/tokens/generate/api.json`，body `{"password": "...", "expiresInSeconds": 0}`，header 帶步驟 1 的 token。
   - 呼叫者必須是 type 1（網頁登入 token），且密碼要正確。
   - 回應 `result` 是 `{"token": "...", "apiBaseUrl": "<root_url>api"}`。
   來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/tokens.go#L87-L141>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/token_record.go#L29-L32>

### 2.4 Header 格式與驗證

- Header 名稱 `Authorization`，值的前 7 個字元與 `bearer ` 做不分大小寫比對。
  來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/core/context_web.go#L32-L35>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/core/context_web.go#L152-L160>
- Token 是 JWT。驗證時伺服器從資料庫讀 token record，用 record 的 `Secret` 驗簽，並檢查 `ExpiredUnixTime`。所以 token 可以撤銷（刪除 record 即失效）。
  來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/services/tokens.go#L364-L419>

### 2.5 Token 有效期

| Token | 有效期 | 來源 |
|---|---|---|
| 網頁登入（type 1） | `token_expired_time`，預設 2592000 秒（30 天），最小 60 | <https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/conf/ezbookkeeping.ini#L415-L416>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/settings/setting.go#L1068-L1074> |
| API（type 8）、MCP（type 5） | 由 `expiresInSeconds` 決定。值為 0 或未給時，程式以常數 `tokenMaxExpiredAtUnixTime`（9999-12-31 23:59:59 UTC）計算期限 | <https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/services/tokens.go#L23>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/services/tokens.go#L111-L122> |

推論：程式用 `time.Unix(max,0).Sub(time.Now())` 算時長。Go 的 `time.Duration` 上限約 292 年，`Sub` 溢位時回傳上限值。所以實際寫入的期限約為 292 年後，不是 9999 年。結果上等同不過期。未實測。

### 2.6 API token 的權限範圍

- API token 沒有 scope 設計。它能呼叫 `/api/v1` 下的大部分 endpoint，包括刪除交易（`/transactions/delete.json`、`/transactions/batch_delete.json`）。
  來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go#L336-L513>
- 部分 handler 另外要求 type 1：產生／撤銷 token、清除資料、更新個人資料、2FA、外部認證、雲端設定同步。v2.0.1 的 release notes 列為 `[Breaking]`：「API tokens no longer support creating / revoke other tokens, clearing data, updating user profile, configuring two-factor authentication, configuring external authentication or configuring application settings sync」。
  來源：<https://github.com/mayswind/ezbookkeeping/releases/tag/v2.0.1>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/tokens.go#L106>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/data_managements.go#L167>

---

## 3. REST：新增交易

### 3.1 Endpoint

`POST /api/v1/transactions/add.json`，`Content-Type: application/json`。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go#L415>、`WEB/httpapi/transaction_api.md` 第 155-192 行（網站：<https://ezbookkeeping.mayswind.net/httpapi/transaction_api>）

必要 header：

| Header | 值 |
|---|---|
| `Authorization` | `Bearer <API token>` |
| `X-Timezone-Name` 或 `X-Timezone-Offset` | IANA 名稱（`Asia/Taipei`）或與 UTC 的分鐘差（`480`）。兩者都有時以 `X-Timezone-Name` 為準 |

Handler 一開始就呼叫 `c.GetClientTimezone()`。兩個 header 都沒有時，`strconv.Atoi("")` 失敗，回 `ErrClientTimezoneOffsetInvalid`（HTTP 400，errorCode 200008）。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/transactions.go#L1165-L1170>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/core/context_web.go#L193-L231>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/errs/global.go#L18>

### 3.2 Request body

`models.TransactionCreateRequest`：

```go
type TransactionCreateRequest struct {
	Type                 TransactionType                `json:"type" binding:"required"`
	CategoryId           int64                          `json:"categoryId,string"`
	Time                 int64                          `json:"time" binding:"required,min=1"`
	UtcOffset            int16                          `json:"utcOffset" binding:"min=-720,max=840"`
	SourceAccountId      int64                          `json:"sourceAccountId,string" binding:"required,min=1"`
	DestinationAccountId int64                          `json:"destinationAccountId,string" binding:"min=0"`
	SourceAmount         int64                          `json:"sourceAmount" binding:"validTransactionAmount"`
	DestinationAmount    int64                          `json:"destinationAmount" binding:"validTransactionAmount"`
	HideAmount           bool                           `json:"hideAmount"`
	TagIds               []string                       `json:"tagIds"`
	PictureIds           []string                       `json:"pictureIds"`
	Comment              string                         `json:"comment" binding:"max=255"`
	GeoLocation          *TransactionGeoLocationRequest `json:"geoLocation" binding:"omitempty"`
	ClientSessionId      string                         `json:"clientSessionId"`
}
```

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/transaction.go#L165-L180>

| 欄位 | JSON 型別 | 語意 |
|---|---|---|
| `type` | number | 1 餘額調整、2 收入、**3 支出**、4 轉帳（[transaction.go L24-L27](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/transaction.go#L24-L27)） |
| `categoryId` | **string** | 二級分類 ID。tag `,string` 表示 JSON 中是字串 |
| `time` | number | **Unix 秒**。伺服器端乘 1000 存成毫秒（[datetimes.go L401-L403](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/utils/datetimes.go#L401-L403)） |
| `utcOffset` | number | 交易時區與 UTC 的分鐘差，-720 到 840。台灣是 `480` |
| `sourceAccountId` | **string** | 帳戶 ID |
| `destinationAccountId` | string | 只用於轉帳。非轉帳時必須省略或為 0，否則回 `ErrTransactionDestinationAccountCannotBeSet` |
| `sourceAmount` | number | 金額，見 3.3 |
| `destinationAmount` | number | 只用於轉帳。非轉帳時必須為 0 |
| `tagIds`、`pictureIds` | string[] | 選用 |
| `comment` | string | 備註，最多 255 字元 |
| `clientSessionId` | string | 選用。重複送出保護，見第 5 節。**官方文件的欄位表沒有列出此欄位** |

請求中沒有幣別欄位。`Transaction` 資料表模型也沒有幣別欄位，幣別由帳戶的 `currency` 決定（[transaction.go L127-L153](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/transaction.go#L127-L153)、[account.go L182-L202](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/account.go#L182-L202)）。所以「190 TWD」的 TWD 要由「錢包」帳戶的幣別為 TWD 來保證。

### 3.3 金額單位：整數，單位 1/100

三個第一手來源一致：

1. 官方文件：`sourceAmount` 型別 `integer`，「Supports up to two decimals. For example, a value of `1234` represents an amount of `12.34`」。
   來源：`WEB/httpapi/transaction_api.md` 第 186 行
2. `utils.ParseAmount`（MCP 與匯入使用）把十進位字串轉成 `sign*integer*100 + sign*decimals`，小數最多兩位。這個轉換不看幣別。
   來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/utils/converter.go#L115-L177>
3. 官方 Skill 腳本的參數說明：「for an expense transaction, '1234' represents an expense of '12.34'」。
   來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/skills/ezbookkeeping/scripts/ebktools.sh#L440>

所以 190 TWD → `19000`，410 TWD → `41000`。支出金額送正數。

驗證範圍：`MinimumTransactionAmount = -999999999999999`、`MaximumTransactionAmount = 999999999999999`。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/transaction.go#L14-L15>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/validators/transaction_amount.go>

### 3.4 伺服器端驗證

`TransactionCreateHandler` 與 `TransactionService` 在寫入前檢查以下項目。任一失敗即回 HTTP 400，不寫入。

| 檢查 | 錯誤 | 來源 |
|---|---|---|
| JSON 綁定與 `binding` tag | `ErrIncompleteOrIncorrectSubmission`（200000） | [transactions.go L1157-L1163](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/transactions.go#L1157-L1163) |
| 時區 header | `ErrClientTimezoneOffsetInvalid`（200008） | 同上 L1165-L1170 |
| `type` 範圍、轉帳欄位一致性 | 多個 | 同上 L1194-L1215 |
| 使用者的「可編輯時間範圍」設定 | `ErrCannotCreateTransactionWithThisTransactionTime` | 同上 L1237-L1241；[user.go L239](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/user.go#L239)。註冊時預設 `TRANSACTION_EDIT_SCOPE_ALL`（[users.go L81](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/users.go#L81)） |
| 帳戶存在、未隱藏、**不是母帳戶**（type 2 多子帳戶） | `ErrCannotAddTransactionToHiddenAccount`、`ErrCannotAddTransactionToParentAccount` | [transactions.go（service）L2706-L2720](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/services/transactions.go#L2706-L2720) |
| 分類存在、未隱藏、**不是一級分類**、分類類型與交易類型相符 | `ErrTransactionCategoryNotFound`、`ErrCannotUsePrimaryCategoryForTransaction`、`ErrTransactionCategoryTypeInvalid` | [transactions.go（service）L3458-L3500](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/services/transactions.go#L3458-L3500) |

寫入在資料庫交易（`DoTransaction`）中執行。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/services/transactions.go#L566-L631>

### 3.5 回應與錯誤格式

成功（HTTP 200）：

```json
{"success": true, "result": { ...TransactionInfoResponse... }}
```

`TransactionInfoResponse` 含 `id`（字串）、`type`、`categoryId`、`time`、`utcOffset`、`sourceAccountId`、`sourceAmount`（整數）、`tagIds`、`comment`、`editable` 等欄位。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/utils/api.go#L42-L47>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/transaction.go#L412-L433>

失敗：

```json
{"success": false, "errorCode": 200008, "errorMessage": "client timezone offset is invalid", "path": "/api/v1/transactions/add.json"}
```

- HTTP status 取自錯誤定義（400、401、403 等）。
- `errorCode = category*100000 + subCategory*1000 + index`。
- 錯誤帶 context 時，另有 `context` 欄位。
- 驗證錯誤的 `errorMessage` 取自第一個 validator 錯誤。

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/utils/api.go#L15-L69>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/errs/error.go#L72-L86>、`WEB/httpapi/index.md`

---

## 4. 新增前要查的 ID

### 4.1 帳戶：`GET /api/v1/accounts/list.json`

- Query 參數：`visible_only`（bool，選用）。
- `result` 是 `AccountInfoResponse[]`。欄位含 `id`（字串）、`name`、`parentId`、`category`（1 為現金）、`type`（1 單一帳戶、2 有子帳戶）、`currency`、`balance`（v2.0.0 起為字串）、`hidden`、`subAccounts`。
- 交易只能掛在 `type = 1` 的帳戶或子帳戶上（3.4 節）。

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go#L393>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/account.go#L150-L202>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/account.go#L68-L69>

### 4.2 分類：`GET /api/v1/transaction/categories/list.json`

- Query 參數：`type`（0 表示全部）、`parent_id`（預設 -1）。
- `result` 是**以分類類型為 key 的物件**。一級分類在陣列中，二級分類在 `subCategories` 中。
- **分類類型的數值與交易類型不同**：分類 1 收入、**2 支出**、3 轉帳；交易 **3 是支出**。支出交易要用 key `"2"` 下的二級分類。

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/transaction_category.go#L13-L15>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/transaction_category.go#L38-L41>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/transaction_categories.go#L463-L510>

預設分類中沒有「晚餐」。前端預設支出分類的第一組是 `Food & Drink` → `Food`、`Drink`、`Fruit & Snack`，繁體中文翻譯為「食品飲料」→「食品」、「飲料」、「水果零食」。使用者可能已改名或自建分類，所以要查實際資料。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/src/consts/category.ts#L1-L25>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/src/locales/zh_Hant.json#L995-L998>

### 4.3 查詢範例（未執行）

```sh
EBK="https://<ezbookkeeping-host>"      # 替換
TOKEN="<API token>"                     # 替換

# 帳戶：列出可記帳的帳戶（含子帳戶）
curl -sS "$EBK/api/v1/accounts/list.json?visible_only=true" \
  -H "Authorization: Bearer $TOKEN" |
  jq '.result[] | {id, name, type, currency, subAccounts: [.subAccounts[]? | {id, name, currency}]}'

# 分類：只列支出分類（分類類型 2）
curl -sS "$EBK/api/v1/transaction/categories/list.json?type=2" \
  -H "Authorization: Bearer $TOKEN" |
  jq '.result["2"][] | {id, name, sub: [.subCategories[]? | {id, name}]}'
```

---

## 5. 冪等與重複送出保護

### 5.1 `clientSessionId`

`TransactionCreateHandler` 的流程：

1. `EnableDuplicateSubmissionsCheck` 為 true 且 `clientSessionId` 非空時，以 `(DUPLICATE_CHECKER_TYPE_NEW_TRANSACTION, uid, clientSessionId)` 查快取。
2. 命中時讀出先前的交易 ID，回傳該筆交易（HTTP 200），不新增。
3. 未命中時新增交易，成功後把新交易 ID 寫入快取。

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/transactions.go#L1261-L1297>

設定：

```ini
[duplicate_checker]
checker_type = in_memory
cleanup_interval = 60
# Set to 0 to disable duplicate checker for new data submissions, default is 300 (5 minutes)
duplicate_submissions_interval = 300
```

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/conf/ezbookkeeping.ini#L389-L398>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/settings/setting.go#L1036-L1045>

實作是 `go-cache` 記憶體快取，key 為 `"<type>|<uid>|<clientSessionId>"`，存活時間 = `duplicate_submissions_interval`。`checker_type` 只支援 `in_memory`。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/duplicatechecker/in_memory_duplicate_checker.go#L21-L54>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/duplicatechecker/in_memory_duplicate_checker.go#L110-L112>

### 5.2 限制

| 限制 | 依據 |
|---|---|
| 預設只保護 300 秒 | 設定預設值 |
| 伺服器重啟後失效 | 記憶體快取（程式碼事實） |
| 多個 ezBookkeeping instance 之間不共用 | 推論：快取在各 process 記憶體中 |
| 同一個 `clientSessionId` 併發送出時可能都寫入 | 推論：「查快取 → 寫資料庫 → 寫快取」三步之間沒有鎖。未實測 |
| 官方文件沒有列出此欄位 | `WEB/httpapi/transaction_api.md` 第 178-192 行的欄位表 |
| Skill 腳本不送此欄位 | `transactions-add` 的參數清單（[ebktools.sh L417-L418](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/skills/ezbookkeeping/scripts/ebktools.sh#L417-L418)） |
| MCP `add_transaction` 沒有此欄位 | `MCPAddTransactionRequest`（第 8.4 節） |

結論：`clientSessionId` 是短時間的重送保護，不是持久的冪等 key。要持久去重，client 端要在送出前以 `GET /api/v1/transactions/list.json` 查詢同日、同帳戶、同金額的交易。這是推論，官方沒有描述這種做法。

---

## 6. 官方程式化介面、穩定性、beancount

### 6.1 官方文件

| 頁面 | 內容 |
|---|---|
| <https://ezbookkeeping.mayswind.net/httpapi/>（`WEB/httpapi/index.md`） | 認證、回應格式、時區 header、API Tools 腳本 |
| `WEB/httpapi/` 下 6 個檔案 | Token、Account、Transaction、Transaction Category、Transaction Tag、Exchange Rate API |
| <https://ezbookkeeping.mayswind.net/mcp/>（`WEB/mcp/index.md`） | MCP 設定與 tool 清單 |
| <https://ezbookkeeping.mayswind.net/agent/skill>（`WEB/agent/skill.md`） | Agent Skill |

文件只涵蓋部分 endpoint。例如 `clientSessionId`、`/api/authorize.json`、`tokens/generate/api.json` 都不在文件的欄位表或 API 清單中。

沒有 OpenAPI 規格檔。在 v2.0.1 原始碼與網站 repo 中沒有找到 OpenAPI 或 Swagger 檔案（本次未全面搜尋，列為未知）。

### 6.2 API 穩定性

- 文件網站沒有 API 穩定性或版本相容聲明。在網站 repo 的英文文件中搜尋 `stab`、`breaking`、`compatib`、`deprecat`，只找到與 API 無關的結果。FAQ 的相容聲明只針對**資料庫 schema**：「ezBookkeeping's database design is forward-compatible」。
  來源：`WEB/faq/index.md` 第 107 行
- Release notes 在 `[Breaking]` 下列出 API 變更，patch 版也有：
  - v2.0.0（2026-09-16）：`/accounts/list.json` 等 endpoint 的金額、餘額欄位從 number 改成 string；API token 不能再存取 `/avatar/`、`/pictures/`、`/proxy/`、`/_AMapService/`。
  - v2.0.1（2026-09-25）：API token 不能再建立／撤銷 token、清除資料等。
  來源：<https://github.com/mayswind/ezbookkeeping/releases/tag/v2.0.0>、<https://github.com/mayswind/ezbookkeeping/releases/tag/v2.0.1>
- 推論：路徑有 `/v1` 前綴，但同一個 `/v1` 下仍有破壞性變更。整合時要釘選 ezBookkeeping 版本，升版前讀 release notes 的 `[Breaking]` 段落。

### 6.3 beancount 匯入與匯出

**匯入**：`fileType = "beancount"` 對應 `BeancountTransactionDataImporter`。匯入 endpoint 在 `enable_import = true`（預設 true）時註冊：`POST /api/v1/transactions/parse_import.json`（multipart，欄位 `fileType`），再 `POST /api/v1/transactions/import.json`。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/converters/transaction_data_converters.go#L70-L71>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go#L426-L431>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/conf/ezbookkeeping.ini#L567-L572>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/transactions.go#L2423-L2445>

匯入的格式限制（來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/converters/beancount/beancount_data_reader.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/converters/beancount/beancount_transaction_data_table.go>）：

| 項目 | 行為 |
|---|---|
| `include` | 回錯誤 `ErrBeancountFileNotSupportInclude`（reader L76-L77） |
| `plugin` | 略過（L78） |
| `option` | 只讀取帳戶根名稱（`name_assets` 等），其他略過（L202-L230） |
| `pushtag` / `poptag` | 支援（L85-L92） |
| `commodity`、`price`、`note`、`document`、`event`、`balance`、`pad`、`query`、`custom` | 略過（L154-L163） |
| posting 數量 | 必須剛好 2 個。1 個以下回 `ErrInvalidBeancountFile`，3 個以上回 `ErrNotSupportedSplitTransactions`（table L120、L225-L231） |
| posting 金額 | 每個 posting 都要寫金額。空白回 `ErrAmountInvalid`（reader L419-L423）。所以 beancount 的自動補平寫法不能匯入 |
| 交易時間 | 只取日期，時刻設為 `00:00:00`（table L117） |
| 交易類型 | 由兩個帳戶的根類型推定：Expenses+Assets/Liabilities 為支出，Income/Equity+Assets/Liabilities 為收入，Assets/Liabilities 之間為轉帳，其他組合回 `ErrThereAreNotSupportedTransactionType`（table L145-L223） |

**匯出**：只有 ezBookkeeping 自有的 CSV 與 TSV（`/api/v1/data/export.csv`、`export.tsv`）。沒有 beancount 匯出。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/converters/transaction_data_converters.go#L24-L33>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go#L386-L389>

---

## 7. ezBookkeeping REST 範例：新增兩筆晚餐（參考）

以下範例未執行。假設用餐時刻是當日 19:00 Asia/Taipei（使用者未提供時刻）：

| 本地時間 | Unix 秒 |
|---|---|
| 2026-09-24 19:00:00 +08:00 | `1790247600` |
| 2026-09-25 19:00:00 +08:00 | `1790334000` |

（以 `TZ=Asia/Taipei date -d '2026-09-24 19:00:00' +%s` 計算）

```sh
EBK="https://<ezbookkeeping-host>"   # 替換：伺服器網址
TOKEN="<API token>"                  # 替換：第 2.3 節取得
ACCOUNT_ID="<錢包帳戶 ID>"           # 替換：第 4.1 節查得，type=1 或子帳戶，幣別 TWD
CATEGORY_ID="<二級支出分類 ID>"      # 替換：第 4.2 節查得，例如「食品」

add_dinner() {  # $1=Unix 秒  $2=金額（1/100 單位）  $3=clientSessionId
  jq -n --arg cat "$CATEGORY_ID" --arg acc "$ACCOUNT_ID" \
        --argjson time "$1" --argjson amount "$2" --arg sid "$3" '{
    type: 3,
    categoryId: $cat,
    time: $time,
    utcOffset: 480,
    sourceAccountId: $acc,
    sourceAmount: $amount,
    comment: "晚餐",
    clientSessionId: $sid
  }' |
  curl -sS -X POST "$EBK/api/v1/transactions/add.json" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -H "X-Timezone-Name: Asia/Taipei" \
    --data-binary @-
}

add_dinner 1790247600 19000 "dinner-2026-09-24"   # 190 TWD
add_dinner 1790334000 41000 "dinner-2026-09-25"   # 410 TWD
```

要點：

- `type: 3` 是支出交易。分類要取自分類類型 `"2"`（支出）下的二級分類。
- `categoryId`、`sourceAccountId` 是字串；`time`、`utcOffset`、`sourceAmount` 是數字。
- `clientSessionId` 只在 300 秒內、同一個 process 中防止重送（第 5 節）。
- 成功時看 `.success == true` 與 `.result.id`。失敗時看 `.errorCode` 與 `.errorMessage`。

---

## 8. MCP

### 8.1 位置、傳輸、啟用

- **內建於後端**，不是獨立專案。路由在 `cmd/webserver.go`，tool 實作在 `pkg/mcp/`。
- 路徑 `/mcp`，只在 `config.EnableMCPServer` 為 true 時註冊。
- 只有 `POST /mcp`，body 是 JSON-RPC。`GET /mcp` 回 Method Not Allowed。所以沒有 SSE 串流，也沒有 stdio 模式。
- 支援的 JSON-RPC method：`initialize`、`resources/list`、`resources/read`、`tools/list`、`tools/call`、`ping`。`notifications/initialized` 直接回 HTTP 202。
- 程式中沒有 `Mcp-Session-Id` 的處理。

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go#L253-L272>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go#L594-L630>

- 支援的 MCP 協定版本：`2025-06-18`（最新）、`2025-03-26`、`2024-11-05`。client 要求其他版本時，`initialize` 回應 `2025-06-18`。
  來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/mcp/model_context_protocol.go#L13-L35>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/model_context_protocols.go#L89-L97>
- 官方文件把傳輸寫成 `"type": "streamable-http"`。
  來源：`WEB/mcp/index.md`（網站：<https://ezbookkeeping.mayswind.net/mcp/>）

啟用設定（`[mcp]` 區段，環境變數 `EBK_MCP_ENABLE_MCP`、`EBK_MCP_MCP_ALLOWED_REMOTE_IPS`）：

```ini
[mcp]
enable_mcp = false
mcp_allowed_remote_ips =
```

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/conf/ezbookkeeping.ini#L40-L45>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/settings/setting.go#L727-L728>

### 8.2 認證

- `/mcp` 套用 `MCPServerIpLimit` 與 `JWTMCPAuthorization`。後者只接受 type 5（MCP token），header 同樣是 `Authorization: Bearer <token>`。
  來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/middlewares/authorization.go#L106-L125>
- MCP token 與 API token 互不通用：`/api/v1` 拒絕 type 5（2.1 節），`/mcp` 拒絕 type 8。
- MCP token 不需要 `enable_api_token`。
- 取得方式：
  - 桌面版「使用者設定 → 安全」的「Generate Token」，要輸入密碼（`WEB/mcp/index.md`）。
  - CLI：`user-session-new --type mcp --expiresInSeconds <秒>`。
  - REST：`POST /api/v1/tokens/generate/mcp.json`，body `{"password": "...", "expiresInSeconds": 0}`，呼叫者必須是 type 1。回應 `{"token": "...", "mcpUrl": "<root_url>mcp"}`。
  來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/tokens.go#L144-L198>
- 有效期規則與 API token 相同（2.5 節）。
- 權限範圍：沒有 scope。能呼叫全部 7 個 tool，其中 `add_transaction` 會寫入。使用者若被設定 `USER_FEATURE_RESTRICTION_TYPE_MCP_ACCESS` 限制，`tools/call` 回 `ErrNotPermittedToPerformThisAction`。
  來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/model_context_protocols.go#L200-L231>

### 8.3 Tool 清單

註冊順序：
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/mcp/mcp_container.go#L73-L79>；參數說明見 `WEB/mcp/index.md`

| Tool | 用途 | 輸入 |
|---|---|---|
| `add_transaction` | 新增交易 | 見 8.4 |
| `query_transactions` | 查詢交易 | `start_time`、`end_time`（RFC 3339，必填），`type`、`category_name`、`account_name`、`keyword`、`match_mode`、`count`（預設 100）、`page`、`response_fields` |
| `query_all_accounts` | 列出帳戶名稱（依帳戶類別分組） | 無 |
| `query_all_accounts_balance` | 列出帳戶餘額 | 無 |
| `query_all_transaction_categories` | 列出分類名稱（一級 → 二級） | 無 |
| `query_all_transaction_tags` | 列出標籤名稱 | 無 |
| `query_latest_exchange_rates` | 查匯率 | `currencies`（逗號分隔） |

### 8.4 `add_transaction` 的參數

```go
type MCPAddTransactionRequest struct {
	Type                   string   `json:"type"`                  // income, expense, transfer
	Time                   string   `json:"time"`                  // RFC 3339
	SecondaryCategoryName  string   `json:"category_name"`
	AccountName            string   `json:"account_name"`
	Amount                 string   `json:"amount"`                // e.g. "12.34"
	DestinationAccountName string   `json:"destination_account_name,omitempty"`
	DestinationAmount      string   `json:"destination_amount,omitempty"`
	Tags                   []string `json:"tags,omitempty"`        // 最多 10 個
	Comment                string   `json:"comment,omitempty"`
	DryRun                 bool     `json:"dry_run,omitempty"`
}
```

（jsonschema tag 省略）
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/mcp/add_transaction_tool_handler.go#L22-L34>

| 項目 | 行為 | 來源（`add_transaction_tool_handler.go`） |
|---|---|---|
| 帳戶 | 以**名稱**比對。只比對未隱藏、非母帳戶的帳戶。名稱放進 map，同名帳戶只保留一個（哪一個取決於列表順序，推論） | L95-L107；[accounts.go L893-L910](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/services/accounts.go#L893-L910) |
| 分類 | 以**二級分類名稱**比對，略過隱藏與一級分類，取第一個類型相符者 | L124-L157 |
| 標籤 | 以名稱比對。找不到的標籤只記 log，不報錯 | L162-L179 |
| 金額 | 十進位字串，經 `ParseAmount` 轉成 1/100 單位。送 `"190"` | L295-L304 |
| 時間與時區 | `time.Parse("2006-01-02T15:04:05Z07:00")`，必須帶時區。`utcOffset` 由字串中的時區推出，不需要 `X-Timezone-*` header | L288-L293、L311-L312 |
| `dry_run` | true 時只驗證，不寫入 | L193-L200 |
| 重複送出保護 | 無。沒有 `clientSessionId` 欄位 | L22-L34 |

### 8.5 ezBookkeeping MCP 範例：新增兩筆晚餐（參考）

MCP client 送出的 `tools/call` 請求如下（未執行）。`category_name` 要換成 `query_all_transaction_categories` 回傳的實際二級分類名稱；`account_name` 要與 ezBookkeeping 中的帳戶名稱完全相同。

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "add_transaction", "arguments": {
   "type": "expense",
   "time": "2026-09-24T19:00:00+08:00",
   "category_name": "<二級支出分類名稱>",
   "account_name": "錢包",
   "amount": "190",
   "comment": "晚餐",
   "dry_run": true}}}
```

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
 "params": {"name": "add_transaction", "arguments": {
   "type": "expense",
   "time": "2026-09-25T19:00:00+08:00",
   "category_name": "<二級支出分類名稱>",
   "account_name": "錢包",
   "amount": "410",
   "comment": "晚餐",
   "dry_run": true}}}
```

建議流程：先以 `dry_run: true` 送出，確認帳戶與分類名稱都能比對成功，再改成 `false` 或省略 `dry_run`。成功時回應含 `success` 與 `account_balance`（交易後的帳戶餘額）。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/mcp/add_transaction_tool_handler.go#L36-L42>

在 Claude Code 中以自然語言操作時，Claude 會自行組出上述 `tools/call`。以上是 JSON-RPC 層的內容。

### 8.6 在 Claude Code 中註冊

ezBookkeeping 官方文件提供的設定（`WEB/mcp/index.md`）：

```json
{
    "mcpServers": {
        "ezbookkeeping-mcp": {
            "type": "streamable-http",
            "url": "http://{domain}/mcp",
            "headers": {
                "Authorization": "Bearer {token}"
            }
        }
    }
}
```

Claude Code 官方文件（<https://code.claude.com/docs/en/mcp>，2026-09-29 查詢）的寫法：

- CLI：`claude mcp add --transport http <name> <url> --header "Authorization: Bearer <token>"`，可加 `--scope`（`-s`）。
- `.mcp.json`：`"type": "http"`，`headers` 可用 `${VAR}` 展開環境變數。文件寫明 `streamable-http` 是 `http` 的別名，所以上面的 ezBookkeeping 設定可以直接使用。

套用到本案（未執行）：

```sh
claude mcp add --transport http --scope user ezbookkeeping \
  "https://<ezbookkeeping-host>/mcp" \
  --header "Authorization: Bearer <MCP token>"
```

或在專案的 `.mcp.json`（token 放環境變數，不要 commit）：

```json
{
  "mcpServers": {
    "ezbookkeeping": {
      "type": "http",
      "url": "https://<ezbookkeeping-host>/mcp",
      "headers": {"Authorization": "Bearer ${EZBOOKKEEPING_MCP_TOKEN}"}
    }
  }
}
```

---

## 9. Agent Skill

### 9.1 位置與檔案結構

主 repo 的 `skills/ezbookkeeping/`：

```
skills/ezbookkeeping/
├── SKILL.md
└── scripts/
    ├── ebktools.sh    # Linux / macOS
    └── ebktools.ps1   # Windows
```

來源：<https://github.com/mayswind/ezbookkeeping/tree/v2.0.1/skills/ezbookkeeping>

`SKILL.md` 的 frontmatter：

```yaml
name: ezbookkeeping
description: Use ezBookkeeping API Tools script to record new transactions, query transactions, retrieve account information, retrieve categories, retrieve tags, and retrieve exchange rate data in the self hosted personal finance application ezBookkeeping.
```

正文只說明三件事：`ebktools.sh list` 列出指令、`ebktools.sh help <command>` 看說明、`ebktools.sh [global-options] <command> [command-options]` 呼叫 API。另說明兩個環境變數。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/skills/ezbookkeeping/SKILL.md>

### 9.2 它呼叫什麼

- **呼叫 REST API**，不是 MCP。腳本組出 `${EBKTOOL_SERVER_BASEURL}/api/v1/<path>`，以 `curl` 送出，header 帶 `Authorization: Bearer $authToken`。
  來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/skills/ezbookkeeping/scripts/ebktools.sh#L1103-L1170>
- 官方文件：「Unlike MCP (Model Context Protocol), ezBookkeeping API Tools use an API token.」所以伺服器要開 `enable_api_token`。
  來源：`WEB/agent/skill.md`（網站：<https://ezbookkeeping.mayswind.net/agent/skill>）
- 相依工具：`grep sed awk date curl jq`。
  來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/skills/ezbookkeeping/scripts/ebktools.sh#L1198>

### 9.3 設定

| 環境變數 | 必填 | 說明 |
|---|---|---|
| `EBKTOOL_SERVER_BASEURL` | 是 | 例如 `https://<host>` |
| `EBKTOOL_TOKEN` | 是 | API token |

環境變數未設定時，腳本依序讀取 `$(pwd)/.env`、上一層目錄的 `.env`、`$HOME/.env`。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/skills/ezbookkeeping/scripts/ebktools.sh#L586-L620>

注意：腳本用的是**目前工作目錄**（`pwd`），不是腳本所在目錄。官方文件寫「in the `scripts` directory ..., in the ezBookkeeping skill directory, or in your user home directory」。兩者不一致。在 Claude Code 中工作目錄通常是專案目錄，所以放 `$HOME/.env` 或設定環境變數最可靠（推論）。

時區：全域選項 `--tz-name <IANA>` 或 `--tz-offset <分鐘>`。兩者都沒給時用系統時區，並送出 `X-Timezone-Name` 或 `X-Timezone-Offset`。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/skills/ezbookkeeping/scripts/ebktools.sh#L811-L822>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/skills/ezbookkeeping/scripts/ebktools.sh#L955-L979>

### 9.4 安裝

官方文件的兩種方式（`WEB/agent/skill.md`）：

1. `npx skills add mayswind/ezbookkeeping@ezbookkeeping`
2. 從 GitHub 下載 `skills/ezbookkeeping` 目錄，放到 agent 平台的 skills 目錄。

Claude Code 的個人 skills 目錄是 `~/.claude/skills/<skill-name>/SKILL.md`，專案 skills 目錄是 `.claude/skills/<skill-name>/SKILL.md`。
來源：<https://code.claude.com/docs/en/skills>（2026-09-29 查詢）

官方文件的下載連結指向 `main` 分支，不是 release tag。要與伺服器版本一致時，從對應的 tag 取檔（推論）。

### 9.5 指令

v2.0.1 的 12 個指令：`tokens-list`、`accounts-list`、`accounts-add`、`transaction-categories-list`、`transaction-categories-add`、`transaction-tags-list`、`transaction-tags-add`、`transactions-list`、`transactions-list-all`、`transactions-add`、`exchangerates-latest`、`server-version`。
來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/skills/ezbookkeeping/scripts/ebktools.sh#L9-L510>

`transactions-add`：

- 必要參數：`type`、`categoryId`、`time`、`utcOffset`、`sourceAccountId`、`sourceAmount`。
- 選用參數：`destinationAccountId`、`destinationAmount`、`hideAmount`、`tagIds`、`pictureIds`、`comment`、`geoLocation`。
- 參數語意與 REST 相同：ID、Unix 秒、1/100 單位整數。腳本不做名稱轉 ID，也不做金額換算。

來源：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/skills/ezbookkeeping/scripts/ebktools.sh#L411-L445>

### 9.6 ezBookkeeping Skill 範例：新增兩筆晚餐（參考）

Agent 的操作順序（指令未執行）：

```sh
S=~/.claude/skills/ezbookkeeping/scripts/ebktools.sh

# 1. 查「錢包」的帳戶 ID
sh "$S" accounts-list

# 2. 查二級支出分類 ID（分類類型 2）
sh "$S" transaction-categories-list

# 3. 新增兩筆
sh "$S" --tz-name Asia/Taipei transactions-add \
  --type 3 --categoryId <二級支出分類 ID> --time 1790247600 --utcOffset 480 \
  --sourceAccountId <錢包帳戶 ID> --sourceAmount 19000 --comment 晚餐

sh "$S" --tz-name Asia/Taipei transactions-add \
  --type 3 --categoryId <二級支出分類 ID> --time 1790334000 --utcOffset 480 \
  --sourceAccountId <錢包帳戶 ID> --sourceAmount 41000 --comment 晚餐
```

這條路徑沒有重送保護。Agent 重試時可能產生重複交易。

---

## 10. 對照：fava JSON API 新增同樣兩筆

### 10.1 Request body 的形狀（v1.30.16 原始碼確認）

- `put_add_entries(entries: list[Any])`：body 是 `{"entries": [...]}`，每個元素經 `deserialise` 轉換，再呼叫 `insert_entries`。回應 `data` 是 `"Stored N entries."`。
  來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py#L543-L553>
- `deserialise` 對 `t == "Transaction"`：`meta`、`narration`、`tags`、`links`、`postings` 用 `[...]` 取值，缺少時丟 `KeyError`（`put_add_entries` 轉成 `FavaAPIError`，HTTP 500）。`flag`、`payee` 用 `.get()`，可省略。`date` 經 `parse_date`。
  來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/serialisation.py#L128-L153>
- `deserialise_posting`：`amount` 是**字串**，拼進 `Assets:Account {amount}` 後交給 beancount parser。空字串表示讓 beancount 補平。`account` 必填。
  來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/serialisation.py#L108-L125>
- 路由 `/<bfile>/api/...`，加上 `--prefix` 時為 `<prefix>/<bfile>/api/...`。詳見 `agent-write-api-and-public-exposure.md` 第 3.1 節與第 10.1 節。

### 10.2 範例（未執行）

使用者沒有提供帳戶名稱。`<EXPENSES_ACCOUNT>`（例如 `Expenses:Food:Dinner`）與 `<WALLET_ACCOUNT>`（例如 `Assets:Cash`）都要換成 ledger 中已 `open` 的帳戶。

```sh
FAVA="http://<fava-host>:5000<prefix>"   # 替換：無 --prefix 時 <prefix> 為空
BFILE="<bfile>"                          # 替換：ledger slug

curl -sS -X PUT "$FAVA/$BFILE/api/add_entries" \
  -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"entries": [
  {"t": "Transaction", "date": "2026-09-24", "flag": "*", "payee": "",
   "narration": "晚餐", "meta": {}, "tags": [], "links": [],
   "postings": [
     {"account": "<EXPENSES_ACCOUNT>", "amount": "190 TWD"},
     {"account": "<WALLET_ACCOUNT>", "amount": ""}]},
  {"t": "Transaction", "date": "2026-09-25", "flag": "*", "payee": "",
   "narration": "晚餐", "meta": {}, "tags": [], "links": [],
   "postings": [
     {"account": "<EXPENSES_ACCOUNT>", "amount": "410 TWD"},
     {"account": "<WALLET_ACCOUNT>", "amount": ""}]}
]}
EOF
```

預期回應：`{"data": "Stored 2 entries.", "mtime": "..."}`（依 `json_api.py` 推論）。

注意：

- fava 沒有認證（前一份研究第 3.3 節）。這個請求只能在受保護的網路內送出，或經過 Cloudflare Access 等外層認證。
- 一次請求可送多筆。ezBookkeeping REST 與 MCP 一次只新增一筆。
- `insert_entries` 在寫檔前不檢查帳戶是否已 `open`。它直接呼叫 `insert_entry` 寫入文字。
  來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/file.py#L239-L261>
  帳戶名稱錯誤時，推論：檔案仍被寫入，錯誤在 beancount 重新載入後出現在 fava 的錯誤清單。未實測。
- 沒有重送保護。前一份研究第 6.3 節建議用 link（`^...`）做冪等 key。

---

## 11. 比較

| 面向 | ezBookkeeping REST | ezBookkeeping MCP | ezBookkeeping Skill | fava JSON API |
|---|---|---|---|---|
| 位置 | 後端內建 `/api/v1` | 後端內建 `/mcp` | repo 內 shell 腳本，呼叫 REST | fava 內建 `/<bfile>/api` |
| 預設狀態 | API token 停用 | MCP 停用 | 需要 API token | 啟用（未設 `--read-only` 時可寫） |
| 認證 | Bearer API token（type 8），可選 IP 白名單 | Bearer MCP token（type 5），可選 IP 白名單 | 同 REST | 無 |
| 權限範圍 | 無 scope，可刪除交易 | 無 scope，7 個 tool | 同 REST，但腳本只包 12 個指令 | 無 |
| 寫入模型 | 資料庫 row，在 DB transaction 中寫入 | 同左 | 同左 | 修改 beancount 文字檔，非原子（前一份研究第 2.1 節） |
| 指定帳戶與分類 | ID（字串） | 名稱 | ID | beancount 帳戶全名 |
| 金額 | 整數，1/100 單位 | 十進位字串 | 整數，1/100 單位 | 字串，beancount posting 語法（含幣別） |
| 幣別 | 由帳戶決定 | 由帳戶決定 | 由帳戶決定 | 每個 posting 自帶 |
| 時間 | Unix 秒 + `utcOffset` + 時區 header | RFC 3339 字串（含時區） | 同 REST | 只有日期 |
| 寫入前驗證 | 帳戶、分類、類型、可編輯時間範圍，失敗即不寫 | 同左，另有名稱比對；有 `dry_run` | 同 REST | 只驗證 posting 金額語法；帳戶是否 `open` 不檢查 |
| 一次筆數 | 1 | 1 | 1 | 多筆 |
| 重送保護 | `clientSessionId`，記憶體，預設 300 秒 | 無 | 無 | 無 |
| 錯誤格式 | `{"success":false,"errorCode","errorMessage","path"}` + HTTP status | JSON-RPC error | 腳本印出回應 | `{"error": "..."}` + HTTP status |
| 官方文件 | 有，部分 endpoint | 有 | 有 | 無 JSON API 文件（前一份研究第 3.3 節） |
| 穩定性聲明 | 無；release notes 有 `[Breaking]` 項目 | 無 | 無 | 無 |

---

## 12. 對本專案 beancount API 的借鏡

架構前提是方案 (a)：自建 API 是 fava JSON API 的 client，fava 是唯一寫入 process（見文件開頭「研究目的」）。每一小節先列 ezBookkeeping 的做法，再判斷在 beancount 上是否適用。

### 12.1 認證

| ezBookkeeping 的做法 | 本文章節 | 是否沿用 | 理由 |
|---|---|---|---|
| API token 預設停用，要明確開啟 | 2.2 | **沿用** | fava 沒有認證（既有筆記第 3.3 節）。自建 API 是唯一的認證關卡。沒有設定 token 時，API 應拒絕所有寫入請求，不能以「無認證」模式執行（設計建議） |
| REST 用 API token，MCP 用另一種 token，兩者互不通用 | 2.1、8.2 | **有條件沿用** | 既有筆記第 7.5 節建議 MCP 先用 stdio。stdio 模式由 MCP client 在本機啟動 process，不經網路，不需要 HTTP token。改用 Streamable HTTP 時，才需要獨立的 MCP token，好處是兩者可以分別撤銷（推論） |
| Token 沒有權限範圍，API token 可以刪除交易 | 2.6 | **不沿用** | ezBookkeeping 的 API token 可以呼叫 `/transactions/delete.json` 與 `/transactions/batch_delete.json`（[webserver.go L423-L424](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go#L423-L424)）。本 API 只提供新增，不提供修改與刪除。API 也不能把 fava 的 `PUT source`、`PUT/DELETE source_slice` 轉給 client。權限分級見既有筆記第 6.4 節 |
| Token 存在資料庫，可以撤銷 | 2.4 | **簡化** | 個人使用只有一個 client 群。token 放在 secret 檔或環境變數，撤銷方式是換 token 後重啟 API（設計建議） |
| `api_token_allowed_remote_ips` 限制來源 IP | 2.2 | **改用網路配置** | 既有筆記第 14.3 節建議 API 不上公網。來源限制由網路位置達成，不在 API 內做 IP 白名單（設計建議） |

### 12.2 金額

**ezBookkeeping**：int64，單位 1/100，不看幣別（第 3.3 節）。`ParseAmount` 拒絕超過兩位的小數（[converter.go L156-L158](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/utils/converter.go#L156-L158)）。

**beancount 與 fava**：

- posting 的金額是 `Decimal`。fava `add_entries` 的 posting `amount` 是字串，fava 把它拼進 `Assets:Account {amount}` 交給 beancount parser（第 10.1 節）。空字串表示讓 beancount 補平。
- 該字串接受完整的 posting 語法，包括 `@` 價格與 `{}` 成本（既有筆記第 3.2 節）。
- 格式錯誤時，`deserialise_posting` 丟 `InvalidAmountError`。實測：`"190.5.1 TWD"` → `InvalidAmountError: Invalid amount: 190.5.1 TWD`。

**判斷：不沿用 1/100 整數。**

1. beancount 本身就是十進位，換成整數再換回來只會多一個錯誤來源。
2. API 的欄位設計成 `amount`（十進位字串）與 `currency`（字串）兩個欄位。API 以 `^\d+(\.\d+)?$` 驗證 `amount`，再組成 `"190 TWD"` 交給 fava。不把 client 的字串原樣放進 fava 的 `amount`，因為那樣 client 可以寫入 `@` 或 `{}`（設計建議）。
3. `currency` 省略時用 `ledger_data.options.operating_currency` 的第一個值（[internal_api.py L84-L98](https://github.com/beancount/fava/blob/v1.30.16/src/fava/internal_api.py#L84-L98)）（設計建議）。

**TWD 精度與 tolerance：**

- ISO 4217 的 TWD minor unit 是 2（SIX 發佈的 list-one.xml，`Pblshd="2026-09-17"`：`<Ccy>TWD</Ccy>` … `<CcyMnrUnts>2</CcyMnrUnts>`）。來源：<https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-one.xml>
- beancount 沒有內建幣別精度表。容差由交易中的數字推定：
  - `infer_tolerances` 的 docstring：「Integer amounts aren't contributing to the determination of precision.」小數位數為負指數時，容差 = `10^exponent × tolerance_multiplier`。
    來源：<https://github.com/beancount/beancount/blob/3.2.3/beancount/core/interpolate.py#L97-L200>（指數判斷在 L186）
  - `tolerance_multiplier` 預設 `0.5`（`inferred_tolerance_multiplier` 是已棄用的別名）。
    來源：<https://github.com/beancount/beancount/blob/3.2.3/beancount/parser/options.py#L513-L560>
  - `inferred_tolerance_default` 預設 `{}`。說明原文：「By default, the tolerance allowed for currencies without an inferred value is zero.」
    來源：<https://github.com/beancount/beancount/blob/3.2.3/beancount/parser/options.py#L483-L512>
- 實測（`loader.load_string`，無 tolerance 相關 option）：

| 交易 | `__tolerances__` | 結果 |
|---|---|---|
| `Expenses:Food:Dinner 190 TWD` + `Assets:Cash`（留白） | `{}` | 平衡，`Assets:Cash` 補成 `-190 TWD` |
| `Expenses:Food:Dinner 410.004 TWD` + `Assets:Cash -410 TWD` | `{'TWD': Decimal('0.0005')}` | 錯誤：`Transaction does not balance: (0.004 TWD)` |

**結論**：驗收案例是整數金額，而且付款帳戶的 posting 留白，由 beancount 補成精確的負值。所以兩筆都不受 tolerance 影響。API 建議固定用這種寫法：費用 posting 寫金額，付款 posting 留白。API 另外可以依 ISO 4217 拒絕 TWD 超過兩位的小數（設計建議）。

### 12.3 時間與時區

**ezBookkeeping**：交易存 Unix 時間與 `utcOffset`。REST 另外要求 `X-Timezone-*` header，用來判斷「可編輯時間範圍」（第 3.1、3.4 節）。MCP 要求 RFC 3339 字串，由字串中的時區推出 `utcOffset`（第 8.4 節）。

**beancount**：`Transaction` 的欄位是 `meta, date, flag, payee, narration, tags, links, postings`，`date` 的型別是 `datetime.date`。沒有時刻，也沒有時區。
來源：<https://github.com/beancount/beancount/blob/3.2.3/beancount/core/data.py#L239-L263>

**為什麼本 API 仍然需要時區**：client 送來的常常是時刻（例如 agent 取得的「現在」）。從時刻取日期時，要選一個時區。不同時區會得到不同日期：

| 時刻 | Asia/Taipei 的日期 | UTC 的日期 |
|---|---|---|
| 2026-09-24T19:00:00+08:00（驗收案例假設的晚餐時刻） | 2026-09-24 | 2026-09-24（11:00Z） |
| 2026-09-25T07:00:00+08:00 | 2026-09-25 | **2026-09-24**（23:00Z） |

（以 `TZ=UTC date -d '2026-09-25 07:00:00 +0800'` 計算）

本 repo 的 `Dockerfile`、`compose.example.yaml`、`docker-entrypoint.sh` 都沒有設定 `TZ`（以 `grep` 確認）。推論：容器的本地時區是 UTC。所以 API 不能用容器時區取日期。

**判斷：沿用「時區必須明確」的原則，不沿用 header 的形式。**

1. API 設定一個帳本時區（例如 `LEDGER_TIMEZONE=Asia/Taipei`）（設計建議）。
2. Request 接受 `date`（`YYYY-MM-DD`，原樣使用）或 `time`（RFC 3339，必須帶時區偏移，與 ezBookkeeping MCP 相同）。給 `time` 時，API 把它換到帳本時區再取日期（設計建議）。
3. 兩者都沒給時回 400。不以伺服器時間預設，避免 agent 在跨日時記錯日期（設計建議）。
4. 要保存時刻時，寫進交易 metadata。fava 的 `deserialise` 接受 `meta`，會輸出成 metadata 行。實測：`"meta": {"time": "19:00"}` 輸出成 `  time: "19:00"`。

### 12.4 帳戶指定

**ezBookkeeping**：REST 用帳戶 ID，MCP 用帳戶名稱。MCP 把名稱放進 map，同名帳戶只留一個（第 8.4 節）。

**beancount**：帳戶名稱本身就是識別碼。不需要 ID 查詢。API 只要確認帳戶存在於 `ledger_data.accounts`，而且 `account_details[帳戶].close_date` 沒有早於交易日期（[internal_api.py L53-L76](https://github.com/beancount/fava/blob/v1.30.16/src/fava/internal_api.py#L53-L76)、[core/accounts.py L92-L110](https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/accounts.py#L92-L110)）。這個檢查是必要的：fava 的 `insert_entries` 寫檔前不檢查帳戶是否已 `open`（第 10.2 節）。

**中文帳戶名稱（例如 `Assets:錢包`）：beancount 3.2.3 不接受。**

1. Lexer 接受 UTF-8。`ACCOUNTTYPE` 與 `ACCOUNTNAME` 都允許 `{UTF-8-ONLY}` 當第一個字元。
   來源：<https://github.com/beancount/beancount/blob/3.2.3/beancount/parser/lexer.l#L121-L130>、<https://github.com/beancount/beancount/blob/3.2.3/beancount/parser/lexer.l#L272-L273>
2. 但 parser 的 `account()` callback 另外用 `valid_account_regexp` 檢查，失敗時產生 `ParserError("Invalid account name: ...")`。
   來源：<https://github.com/beancount/beancount/blob/3.2.3/beancount/parser/grammar.py#L276-L293>
3. `valid_account_regexp` 由 `name_*` option 與 `ACC_COMP_NAME_RE` 組成。`ACC_COMP_NAME_RE = r"[\p{Lu}\p{Nd}][\p{L}\p{Nd}\-]*"`：每個元件的第一個字元必須是 Unicode 大寫字母或十進位數字。
   來源：<https://github.com/beancount/beancount/blob/3.2.3/beancount/parser/grammar.py#L109-L127>、<https://github.com/beancount/beancount/blob/3.2.3/beancount/core/account.py#L30-L41>
4. 中文字的 Unicode 類別是 `Lo`（Letter, other），不是 `Lu`。所以「錢」不能當元件的第一個字元，但可以出現在大寫字母之後（`\p{L}` 包含 `Lo`）。

實測（`loader.load_string` 與 `beancount.core.account.is_valid`）：

| 帳戶 | parser 錯誤 | `account.is_valid()` |
|---|---|---|
| `Assets:錢包` | `Invalid account name: Assets:錢包` | False |
| `Expenses:餐飲:晚餐` | `Invalid account name: Expenses:餐飲:晚餐` | — |
| `Assets:Cash:錢包` | 無 | **False** |
| `Expenses:Food:晚餐` | 無 | **False** |
| `Expenses:Food:Dinner` | 無 | True |

`Assets:Cash:錢包` 沒有 parser 錯誤，但 `is_valid` 回 False。原因：`grammar.py` 用 `regex.match`（只比對開頭），`is_valid` 用 `regex.fullmatch`（[account.py L75-L84](https://github.com/beancount/beancount/blob/3.2.3/beancount/core/account.py#L75-L84)）。開頭的 `Assets:Cash` 符合後，後面的 `:錢包` 就沒有被檢查。推論：這是實作上的缺口，不是語法承諾。本 API 不應依賴它，應以 `account.is_valid()` 為準，拒絕這類名稱。

**中文顯示名稱與別名：用 `open` 的 metadata。**

- `open` directive 接受 metadata。實測：`2026-01-01 open Assets:Cash TWD` 下一行 `  name: "錢包"`，載入後 `Open.meta` 含 `{'name': '錢包'}`，沒有錯誤。metadata key 的規則見既有筆記第 6.3 節。
- fava 把 `open` 的 metadata 存進 `AccountData.meta`（[core/accounts.py L98-L99](https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/accounts.py#L98-L99)），並由 `GET api/ledger_data` 的 `account_details` 回傳（[internal_api.py L53-L76](https://github.com/beancount/fava/blob/v1.30.16/src/fava/internal_api.py#L53-L76)）。
- fava 沒有帳戶別名或顯示名稱功能。`help/options.md` 的 option 清單中沒有這類 option（[options.md](https://github.com/beancount/fava/blob/v1.30.16/src/fava/help/options.md)）。推論：fava UI 只顯示帳戶全名。

**判斷**：

1. API 的 `account` 與 `category` 欄位接受兩種值：beancount 帳戶全名，或 `open` metadata `name` 的值（設計建議）。
2. API 從 `ledger_data.account_details` 建立別名表。兩個帳戶有相同的 `name` 時，API 回錯誤，不像 ezBookkeeping MCP 那樣只留一個（設計建議）。
3. 新帳戶不能經 fava `add_entries` 建立，因為 `deserialise` 不支援 `Open`（既有筆記第 3.3 節）。帳戶由使用者在 fava 編輯器中建立。

### 12.5 分類

**ezBookkeeping**：分類有兩層。交易必須用二級分類，而且分類類型要與交易類型相符（第 3.4、4.2 節）。

**beancount**：分類就是 `Expenses`（或 `name_expenses` option 的值）下的帳戶。交易類型不是欄位，而是由 posting 的帳戶根類型決定。ezBookkeeping 的 beancount 匯入器就是這樣推定交易類型（第 6.3 節）。fava 的 `ledger_data.options` 回傳 `name_expenses` 等 option（[internal_api.py L84-L98](https://github.com/beancount/fava/blob/v1.30.16/src/fava/internal_api.py#L84-L98)）。

**判斷**：

1. API 的 `category` 欄位對應一個費用帳戶。API 檢查它在 `name_expenses` 根之下（設計建議）。
2. beancount 不區分一級與二級，帳戶層級由使用者決定。推論：beancount 沒有「只能記在葉帳戶」的限制；本次沒有查到相關檢查，未深入搜尋。
3. **「晚餐」對應哪個帳戶由使用者決定**（待決）。例如 `Expenses:Food:Dinner` 加上 `name: "晚餐"`。

### 12.6 冪等

| 項目 | ezBookkeeping `clientSessionId`（第 5 節） | link 方案（既有筆記第 6.3 節） |
|---|---|---|
| 存放位置 | process 記憶體 | ledger 檔案中的 `^link` |
| 存活期 | 預設 300 秒 | 與帳本相同 |
| 重啟後 | 失效 | 仍有效 |
| 同 key 併發 | 推論：可能重複寫入 | 既有筆記要求查詢與寫入在同一個鎖區間內 |
| 命中時 | 回傳既有交易，HTTP 200 | 同左（既有筆記建議） |
| 字元限制 | 無 | `[A-Za-z0-9\-_/.]+`（[lexer.l L292-L293](https://github.com/beancount/beancount/blob/3.2.3/beancount/parser/lexer.l#L292-L293)） |

**判斷：用 link 方案。沿用 ezBookkeeping「命中時回傳既有交易」的回應方式。**

方案 (a) 下的兩個具體注意點：

1. **查 link 前要先讓 fava 重新載入。** fava 的 `before_request` 只在 `request.blueprint != "json_api"` 時呼叫 `ledger.changed()`（[application.py L259-L268](https://github.com/beancount/fava/blob/v1.30.16/src/fava/application.py#L259-L268)）。`get_ledger_data` 與 `get_query` 不會重新載入；`get_changed` 會（[json_api.py L320-L322](https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py#L320-L322)、[L337-L341](https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py#L337-L341)）。所以 API 的順序是：`GET api/changed` → `GET api/ledger_data`（看 `links`）→ `PUT api/add_entries`。推論：少了第一步，剛寫入的 link 可能還不在 `links` 中，重試會重複寫入。
2. **API 內要自己加鎖。** fava 的 `_lock` 只包住 `insert_entries` 本身（[core/file.py L239-L261](https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/file.py#L239-L261)），不包住 API 的「查 link」。API 要把上面三步放在同一個 process 內鎖中，並且只執行一個 API instance（推論）。

### 12.7 dry_run

**ezBookkeeping**：MCP `add_transaction` 有 `dry_run`，走相同的驗證，不寫入（第 8.4 節）。REST 沒有 dry_run。

**fava**：沒有驗證用的 endpoint。`PUT api/format_source` 只格式化文字，不驗證（既有筆記第 3.2 節）。

**判斷：沿用 `dry_run` 參數，REST 與 MCP 都提供。** 實作分兩層（設計建議）：

1. 輕量檢查（不讀檔）：帳戶存在且未關閉、`account.is_valid()`、金額格式、幣別、日期、link 是否已存在。資料來自 `GET api/changed` 與 `GET api/ledger_data`。
2. 完整檢查：依既有筆記第 6.5 節，在副本中插入 entry，以 `beancount.loader` 載入，和 baseline errors 做差集。方案 (a) 下 API 要能唯讀讀取 ledger 檔案（推論）。

兩層都回傳將要寫入的 beancount 文字。文字用 fava 的 `fava.beans.str.to_string` 產生，與 fava 實際寫入的格式一致（實測輸出見第 13.4 節）。

### 12.8 MCP tool 集合

以 ezBookkeeping 的 7 個 tool（第 8.3 節）為起點：

| ezBookkeeping tool | 本專案 tool | 底層 | 備註 |
|---|---|---|---|
| `add_transaction` | `add_transaction` | `GET api/changed` → `GET api/ledger_data`（帳戶、link）→ `PUT api/add_entries` | `dry_run` 與冪等由 API 實作，fava 沒有對應 |
| `query_all_accounts`、`query_all_transaction_categories` | `list_accounts`（合併） | `GET api/ledger_data`：`accounts`、`account_details`（`open` metadata、`close_date`）、`options.name_*` | 以根類型分組；分類就是 `Expenses` 下的帳戶 |
| `query_all_accounts_balance` | `query_balances` | `GET api/query?query_string=...`（beanquery） | 例如 `SELECT account, sum(position) AS balance WHERE account ~ '^Assets:' GROUP BY account` |
| `query_transactions` | `query_transactions` | `GET api/query?query_string=...` | 例如 `SELECT date, flag, narration, account, position, links WHERE date >= 2026-09-24 AND date <= 2026-09-25 AND account ~ '^Expenses:'` |
| `query_all_transaction_tags` | 不列入最小集合 | `GET api/ledger_data` 的 `tags` | — |
| `query_latest_exchange_rates` | 不列入最小集合 | `GET api/commodities`（[json_api.py L695](https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py#L695)） | 單一幣別 TWD 帳本不需要 |

- 兩個 beanquery 查詢已在容器中以 `beanquery.connect("beancount:", ...)` 實測可執行，結果正確。**沒有**經由 fava 的 `GET api/query` 執行。fava 的 `get_query` 使用 `g.ledger.query_shell`（[json_api.py L337-L341](https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py#L337-L341)），回應格式未查。
- `get_query` 不會重新載入（12.6 節），查詢前要先呼叫 `GET api/changed`。
- ezBookkeeping `add_transaction` 的回應含交易後的帳戶餘額（第 8.5 節）。本專案可以在寫入後以 `query_balances` 取得付款帳戶餘額，一併回傳（設計建議）。
- 傳輸方式與 REST、MCP 共用核心的架構，見既有筆記第 7.5 節。

### 12.9 Skill

**ezBookkeeping 的結構**（第 9 節）：`SKILL.md` 很短，只說明怎麼列指令、看說明、呼叫。指令定義（路徑、參數、型別、說明、回應結構）都寫在腳本內。腳本呼叫 REST，用 API token。官方文件說明預設輸出盡量精簡，以減少 token 用量（`WEB/agent/skill.md`）。

**判斷：沿用這個結構。** 理由：

1. Skill 只需要 REST。先完成 REST，再加 Skill，不需要 MCP（與既有筆記第 7.5 節「REST 先做」一致）。
2. 指令說明放在腳本的 `help` 中，agent 需要時才讀，`SKILL.md` 保持精簡。

**要避開的問題**：

| ezBookkeeping 腳本的問題 | 本文章節 | 本專案的做法（設計建議） |
|---|---|---|
| `.env` 從 `pwd` 讀，與文件說的 skill 目錄不符 | 9.3 | 只讀環境變數與一個固定路徑的設定檔 |
| 不送重送保護欄位，agent 重試會重複記帳 | 5.2、9.6 | `add` 指令要求 `--idempotency-key`。key 由 agent 為每一筆交易產生一次，重試時沿用。不用交易內容推導 key：內容相同的交易可能是兩筆合法交易（既有筆記第 6.3 節對 `noduplicates` 的說明） |
| 帳戶與分類要先查 ID | 9.6 | 不需要：beancount 帳戶就是名稱，另外支援 `name` metadata 別名（12.4 節） |
| 金額與時間要自己換算成整數與 Unix 秒 | 9.5 | 直接接受 `"190"` 與 `2026-09-24` |

Claude Code 的 skills 目錄見第 9.4 節。

---

## 13. 驗收案例

以下是本專案新 API 的**設計草案**與預期結果。endpoint 路徑與欄位名稱是本文的建議，不是既有實作。

### 13.1 前提與佔位

| 佔位 | 意義 | 狀態 |
|---|---|---|
| `<WALLET_ACCOUNT>` | 「錢包」對應的帳戶，例如 `Assets:Cash` | **待使用者決定** |
| `<DINNER_ACCOUNT>` | 「晚餐」對應的費用帳戶，例如 `Expenses:Food:Dinner` | **待使用者決定** |
| `<API_BASE>` | 自建 API 的網址 | 待部署 |
| `<API_TOKEN>` | 自建 API 的 token | 待部署 |
| `<FAVA_BASE>`、`<bfile>` | fava 的網址（含 `--prefix`）與 ledger slug | 見既有筆記第 3.1、10.1 節 |
| flag | 本文用 `!`（既有筆記第 6.2 節的建議） | **待使用者決定** |
| 帳本時區 | `Asia/Taipei` | **待使用者決定** |

帳本中必須已有兩個 `open`（由使用者在 fava 編輯器中加入）。以佔位的範例名稱表示：

```beancount
2026-01-01 open Assets:Cash TWD
  name: "錢包"
2026-01-01 open Expenses:Food:Dinner TWD
  name: "晚餐"
```

實測：這段加上第 13.4 節的兩筆交易，以 `loader.load_string` 載入，沒有錯誤。

### 13.2 REST 請求（設計草案，未執行）

```sh
API="<API_BASE>"
TOKEN="<API_TOKEN>"

curl -sS -X POST "$API/v1/transactions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "date": "2026-09-24",
    "narration": "晚餐",
    "category": "晚餐",
    "account": "錢包",
    "amount": "190",
    "currency": "TWD",
    "idempotency_key": "dinner-2026-09-24",
    "dry_run": false
  }'

curl -sS -X POST "$API/v1/transactions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "date": "2026-09-25",
    "narration": "晚餐",
    "category": "晚餐",
    "account": "錢包",
    "amount": "410",
    "currency": "TWD",
    "idempotency_key": "dinner-2026-09-25",
    "dry_run": false
  }'
```

- `category` 與 `account` 可以是帳戶全名，或 `open` 的 `name` metadata（12.4 節）。
- 可用 `"time": "2026-09-24T19:00:00+08:00"` 取代 `date`（12.3 節）。
- `idempotency_key` 必須符合 link 字元類 `[A-Za-z0-9\-_/.]+`。API 加上前綴 `ik-` 後寫成 link。

預期回應（設計草案）：

| 情況 | HTTP | body 要點 |
|---|---|---|
| 第一次送出 | 201 | `created: true`、`entry`（13.4 節的文字）、`link: "ik-dinner-2026-09-24"` |
| 相同 `idempotency_key` 重送 | 200 | `created: false`，不寫入 |
| `dry_run: true` | 200 | `created: false`、`entry`，不寫入 |
| 帳戶或別名不存在、別名重複、`account.is_valid()` 為 False | 422 | 錯誤說明，不呼叫 fava |

### 13.3 MCP tool call（設計草案，未執行）

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "add_transaction", "arguments": {
   "date": "2026-09-24", "narration": "晚餐",
   "category": "晚餐", "account": "錢包",
   "amount": "190", "currency": "TWD",
   "idempotency_key": "dinner-2026-09-24", "dry_run": false}}}
```

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
 "params": {"name": "add_transaction", "arguments": {
   "date": "2026-09-25", "narration": "晚餐",
   "category": "晚餐", "account": "錢包",
   "amount": "410", "currency": "TWD",
   "idempotency_key": "dinner-2026-09-25", "dry_run": false}}}
```

參數與 REST 相同。與 ezBookkeeping MCP 的差異：多了 `idempotency_key`；`account` 與 `category` 除了名稱，也接受帳戶全名。

### 13.4 預期寫入 ledger 的 beancount 文字

以 `<WALLET_ACCOUNT> = Assets:Cash`、`<DINNER_ACCOUNT> = Expenses:Food:Dinner` 為例。以下由容器中的 `fava.serialisation.deserialise` 加上 `fava.beans.str.to_string` 產生（實測；flag、link 依本節設定）：

```beancount
2026-09-24 ! "晚餐" ^ik-dinner-2026-09-24
  Expenses:Food:Dinner                                  190 TWD
  Assets:Cash

2026-09-25 ! "晚餐" ^ik-dinner-2026-09-25
  Expenses:Food:Dinner                                  410 TWD
  Assets:Cash
```

- 實測的呼叫使用 `to_string` 的預設 `currency_column` 與 `indent`。fava 實際寫檔時套用 `currency-column` 與 `indent` fava option（第 10.1 節引用的 `insert_entries`），欄位對齊可能不同。
- 寫入哪個檔案、哪個位置，由 fava 的 `insert-entry` option 與 `default-file` 決定（既有筆記第 2.5 節）。
- `payee` 為空字串時不輸出。

### 13.5 底層送給 fava 的請求

API 對每一筆呼叫一次（未執行）：

```sh
curl -sS -X PUT "<FAVA_BASE>/<bfile>/api/add_entries" \
  -H "Content-Type: application/json" \
  -d '{"entries": [
    {"t": "Transaction", "date": "2026-09-24", "flag": "!", "payee": "",
     "narration": "晚餐", "meta": {}, "tags": [], "links": ["ik-dinner-2026-09-24"],
     "postings": [
       {"account": "<DINNER_ACCOUNT>", "amount": "190 TWD"},
       {"account": "<WALLET_ACCOUNT>", "amount": ""}]}]}'
```

第二筆只改 `date`（`2026-09-25`）、`links`（`ik-dinner-2026-09-25`）、`amount`（`410 TWD`）。

每一筆的完整序列（12.6 節）：

1. API 取得 process 內鎖。
2. `GET <FAVA_BASE>/<bfile>/api/changed`
3. `GET <FAVA_BASE>/<bfile>/api/ledger_data`：確認兩個帳戶存在、未關閉；解析別名；確認 `ik-dinner-2026-09-24` 不在 `links` 中。
4. `PUT <FAVA_BASE>/<bfile>/api/add_entries`。預期 fava 回應 `{"data": "Stored 1 entries.", "mtime": "..."}`（依 [json_api.py L543-L553](https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py#L543-L553) 推論）。
5. 釋放鎖。git commit 依既有筆記第 6.1 節處理。

### 13.6 驗收檢查

| # | 檢查 | 預期 |
|---|---|---|
| 1 | 兩筆 REST 請求 | 各回 201 |
| 2 | 以相同 `idempotency_key` 再送一次 | 回 200、`created: false`；ledger 沒有新增文字 |
| 3 | `GET api/changed` 後，`GET api/query?query_string=SELECT date, flag, narration, account, position, links WHERE date >= 2026-09-24 AND date <= 2026-09-25 AND account ~ '^Expenses:'` | 兩列：`2026-09-24 ! 晚餐 <DINNER_ACCOUNT> 190 TWD {ik-dinner-2026-09-24}`、`2026-09-25 ! 晚餐 <DINNER_ACCOUNT> 410 TWD {ik-dinner-2026-09-25}` |
| 4 | 付款帳戶在兩筆前後的餘額差 | `-600 TWD` |
| 5 | `GET api/errors` 的數量 | 與寫入前相同 |
| 6 | `dry_run: true` | 回傳 13.4 節的文字，ledger 不變 |
| 7 | 用 `"account": "Assets:錢包"` 送出 | 422，不呼叫 fava |

檢查 3、4 的查詢句已在容器中以 beanquery 0.2.0 直接實測（以 `Expenses:Food:Dinner`、`Assets:Cash` 代入）：結果為上述兩列，`Assets:Cash` 的 `sum(position)` 為 `-600 TWD`。

---

## 14. 未知與待確認

1. ezBookkeeping 是否有 OpenAPI 或其他機器可讀的 API 規格：未全面搜尋，未知。
2. CLI 在官方 Docker image 中的確切呼叫方式（執行檔路徑、是否需要指定設定檔）：未查證。
3. `clientSessionId` 在併發請求下是否會重複寫入：依程式碼順序推論會，未實測。
4. API token「不給期限」時的實際到期時間：推論約 292 年後，未實測。
5. MCP `add_transaction` 遇到同名帳戶時選哪一個：取決於 `GetAllAccountsByUid` 的排序，未查。
6. ezBookkeeping MCP server 是否相容比 `2025-06-18` 更新的 MCP 協定版本（例如前一份研究第 7.1 節提到的無 `initialize` 版本）：程式只處理三個版本，對新版 client 的行為未知。
7. fava `GET api/query` 的回應格式：本次只以 beanquery 直接實測查詢句，沒有經 fava HTTP 執行。
8. fava `deserialise` 丟出的 `InvalidAmountError` 對應的 HTTP status：本次未查。
9. beancount `grammar.py` 只檢查帳戶名稱開頭（`regex.match`）的行為在未來版本是否改為完整比對：未知。本文建議不依賴此行為（12.4 節）。
10. beancount 是否限制 posting 只能記在葉帳戶：未深入搜尋（12.5 節）。
11. 容器的本地時區：`Dockerfile` 等檔案沒有設定 `TZ`，推論為 UTC，未實測。
12. 待使用者決定：「錢包」與「晚餐」對應的帳戶名稱、是否加 `name` metadata、Agent 寫入的 flag、帳本時區、是否保存時刻（第 1.3 節）。

---

## 15. 來源清單

### ezBookkeeping 原始碼（tag `v2.0.1`，commit `323cf0c`）

- 路由註冊：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/webserver.go>
- CLI `user-session-new`：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/cmd/user_data.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/cli/user_data.go>
- 認證 middleware：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/middlewares/authorization.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/middlewares/api_token_ip_limit.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/middlewares/mcp_server_ip_limit.go>
- Header 與時區：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/core/context_web.go>
- Token：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/core/token_claims.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/services/tokens.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/tokens.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/token_record.go>
- 登入：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/authorizations.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/user.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/auth_response.go>
- 交易：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/transactions.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/transaction.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/services/transactions.go>
- 金額：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/utils/converter.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/validators/transaction_amount.go>
- 時間：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/utils/datetimes.go>
- 帳戶與分類：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/account.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/services/accounts.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/transaction_category.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/transaction_categories.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/src/consts/category.ts>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/src/locales/zh_Hant.json>
- 回應與錯誤：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/utils/api.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/errs/error.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/errs/global.go>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/errs/token.go>
- 重送保護：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/duplicatechecker/in_memory_duplicate_checker.go>
- 設定：<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/conf/ezbookkeeping.ini>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/settings/setting.go>
- beancount 匯入：<https://github.com/mayswind/ezbookkeeping/tree/v2.0.1/pkg/converters/beancount>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/converters/transaction_data_converters.go>
- MCP：<https://github.com/mayswind/ezbookkeeping/tree/v2.0.1/pkg/mcp>、<https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/api/model_context_protocols.go>
- Skill：<https://github.com/mayswind/ezbookkeeping/tree/v2.0.1/skills/ezbookkeeping>

### ezBookkeeping Release notes

- <https://github.com/mayswind/ezbookkeeping/releases/tag/v2.0.0>
- <https://github.com/mayswind/ezbookkeeping/releases/tag/v2.0.1>

### ezBookkeeping 官方文件（網站 repo commit `59efbc5`）

- API 概觀：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/httpapi/index.md>（<https://ezbookkeeping.mayswind.net/httpapi/>）
- Transaction API：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/httpapi/transaction_api.md>
- Account API：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/httpapi/account_api.md>
- Transaction Category API：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/httpapi/transaction_category_api.md>
- Token API：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/httpapi/token_api.md>
- MCP：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/mcp/index.md>（<https://ezbookkeeping.mayswind.net/mcp/>）
- Agent Skill：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/agent/skill.md>（<https://ezbookkeeping.mayswind.net/agent/skill>）
- 設定：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/configuration/index.md>
- 匯入匯出：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/export_and_import/index.md>
- FAQ：<https://github.com/mayswind/ezBookkeeping-Website/blob/59efbc5e6c94001501dbc5c3c886d302eca57887/docs/faq/index.md>

### fava 原始碼（tag `v1.30.16`，commit `d8d426f`）

- <https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py>
- <https://github.com/beancount/fava/blob/v1.30.16/src/fava/serialisation.py>
- <https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/file.py>
- <https://github.com/beancount/fava/blob/v1.30.16/src/fava/application.py>
- <https://github.com/beancount/fava/blob/v1.30.16/src/fava/internal_api.py>
- <https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/accounts.py>
- <https://github.com/beancount/fava/blob/v1.30.16/src/fava/help/options.md>

### beancount 原始碼（tag `3.2.3`，commit `eeda2aa`）

- Lexer：<https://github.com/beancount/beancount/blob/3.2.3/beancount/parser/lexer.l>
- 帳戶名稱檢查：<https://github.com/beancount/beancount/blob/3.2.3/beancount/parser/grammar.py>、<https://github.com/beancount/beancount/blob/3.2.3/beancount/core/account.py>
- Tolerance：<https://github.com/beancount/beancount/blob/3.2.3/beancount/core/interpolate.py>、<https://github.com/beancount/beancount/blob/3.2.3/beancount/parser/options.py>
- 資料結構：<https://github.com/beancount/beancount/blob/3.2.3/beancount/core/data.py>

### ISO 4217

- SIX list-one.xml（`Pblshd="2026-09-17"`）：<https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-one.xml>

### 實測

在本機以 image `gn00678465/beancount-fava@sha256:b77b0a1c964439cb4dd31b006f459318f60f10a910074a3d6059ae0ccaac8be4` 執行 `docker run --rm --entrypoint python`，只跑 Python 片段：帳戶名稱檢查、tolerance、`deserialise` + `to_string` 輸出、beanquery 查詢。沒有啟動 fava，沒有掛載或寫入任何 ledger 檔。

### Claude Code 官方文件（2026-09-29 查詢，頁面無版本號）

- MCP：<https://code.claude.com/docs/en/mcp>
- Skills：<https://code.claude.com/docs/en/skills>
