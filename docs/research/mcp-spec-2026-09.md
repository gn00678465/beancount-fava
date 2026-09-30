# MCP 規格現況與 AGENT-2 手寫端點的相容性

查詢日期：2026-09-30。

本文重新查證 repo 內與 MCP 有關的主張。對象是 `docs/plans/agent-write-api.md`（AGENT-2 與附錄）、`docs/research/agent-write-api-and-public-exposure.md` 第 7 節、`docs/research/ezbookkeeping-api.md` 第 8 節與第 12.8 節。

每項主張附第一手來源。「推論」代表由已查證事實推導，沒有直接證據。「未知」代表第一手來源沒有答案。「實測」代表本機執行過，方法見第 8 節。

引用的版本：

| 對象 | 版本 | 固定點 |
|---|---|---|
| MCP 規格 repo | tag `2026-07-28` | commit `5f5440bb26a62e2cf3440b92da5a667efa03b267` |
| MCP 規格 repo `main`（查 draft 與 tag 之後的變更） | — | commit `046fa30efd374370afb87ef830bd788eac5f217e`（2026-09-28） |
| Claude Code（本機） | `2.1.284` | `claude --version` |
| Claude Code changelog | 最新項目 `2.1.285` | `anthropics/claude-code` commit `ec44ca97dc86c33d934c8d55b24959aabf076871`（2026-09-29） |
| Claude Code 官方文件 | 頁面沒有版本號 | 2026-09-30 抓取 `code.claude.com/docs/en/mcp.md`、`env-vars.md` |
| Python SDK（PyPI `mcp`） | `2.2.0` | tag `v2.2.0`，commit `9972c21aa42054fb1450c5fc614761ed11847ec6` |
| fava | `v1.30.16` | tag `v1.30.16` |

下文連結的縮寫：

- `S` = `https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification`
- `CC` = `https://code.claude.com/docs/en`
- `CL` = `https://github.com/anthropics/claude-code/blob/ec44ca97dc86c33d934c8d55b24959aabf076871/CHANGELOG.md`
- `PY` = `https://github.com/modelcontextprotocol/python-sdk/blob/9972c21aa42054fb1450c5fc614761ed11847ec6`

---

## 1. 摘要

### 1.1 關鍵結論

1. **最新正式版仍是 `2026-07-28`。之後沒有新版本。** draft 的 changelog 是空的。tag 之後 `2026-07-28` 與 `draft` 目錄只有連結修正與文字修正，沒有協定變更（第 2 節）。
2. **只實作 `initialize` + `tools/list` + `tools/call` 的 server 在 `2026-07-28` 中叫做 legacy server。** 它不符合 `2026-07-28`，但可以符合 `2025-11-25`。Claude Code 2.1.284 是 dual-era client：先送 `server/discover`（`2026-07-28`），收到錯誤後退回 `initialize`，版本是 `2025-11-25`。以與計畫相同行為的 probe server 實測，退回後 `tools/call` 成功（第 4、8 節）。
3. **計畫的 Appendix C 描述不準確。** 原型不是「以回傳 client 提出的版本號」通過 `2026-07-28` 的檢查。Claude Code 在 `initialize` 送出的是 `2025-11-25`，server 回傳同一值，所以協商結果是 `2025-11-25`。這一點由 probe 重現。原型本身沒有重跑，所以「原型當時也是這樣」是推論（第 8 節）。
4. **Claude Code 對 401 的處理會影響 Lane 7。** 設定了 `headers.Authorization` 而被拒時，Claude Code 不啟動 OAuth，直接報連線失敗。沒有設定 token 時，Claude Code 會對同一 origin 發出 OAuth discovery（`/.well-known/...`）與 `POST /register`。固定 Bearer token 的做法可行，但 Lane 7 的預期結果與 log 內容要改（第 6 節）。
5. **Python SDK `mcp` 2.2.0 支援 `2026-07-28` 與所有舊版，但 HTTP transport 建在 Starlette/anyio（ASGI）上。** 它沒有 WSGI 介面。計畫「SDK 無法直接掛在 Flask 上」仍正確（第 7 節）。
6. **fava 對 extension 未註冊的 method 回 404，不是 405。** 計畫只註冊 `POST mcp`。Claude Code 在 legacy 流程中會送 `GET mcp`。`2025-11-25` 要求 server 對 GET 回 SSE 或 405。實測 Claude Code 對 404 也能繼續，但這不符合規格（第 5 節）。

### 1.2 對 AGENT-2 的建議摘要

- 決定協定版本。建議改為只實作 `2026-07-28`（方案 B）。理由：Claude Code 2.1.284 預設先試 `2026-07-28`；本機實測只實作 `2026-07-28` 的 probe 可以連線與呼叫 tool；每次連線只需 2 個 HTTP 請求，legacy 流程需要 5 個。代價：要做 header 驗證、`server/discover`、`resultType`、`ttlMs`/`cacheScope`。若設定 `MCP_PROTOCOL_NEGOTIATION=legacy`，這種 server 會連線失敗（實測）。
- 若保留 legacy（方案 A），至少補上：版本選擇規則、`MCP-Protocol-Version` 檢查、`GET mcp` 回 405、Origin 檢查、拒絕 JSON-RPC batch。
- 兩個方案都要：Origin 檢查、`GET`/`DELETE` 回 405、Lane 7 拆成「錯誤 token」與「沒有 token」、AGENT-3 的「MCP 呼叫次數」只計 `tools/call`。

細節見第 10 節。

---

## 2. 規格版本現況（研究問題 1）

- `docs/specification/` 下的目錄：`2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`、`2026-07-28`、`draft`。
  來源：<https://github.com/modelcontextprotocol/modelcontextprotocol/tree/046fa30efd374370afb87ef830bd788eac5f217e/docs/specification>
- repo tag：`2026-07-28`、`2026-07-28-RC`、`2025-11-25`、`2025-11-25-RC`、`2025-06-18`、`2025-03-26`、`2024-11-05` 等。沒有比 `2026-07-28` 新的 tag。
  來源：`gh api repos/modelcontextprotocol/modelcontextprotocol/tags`（2026-09-30）
- 官方網站 `https://modelcontextprotocol.io/specification/latest` 以 307 轉到 `/specification/2026-07-28`。版本頁寫「The **current** protocol version is **2026-07-28**」。
  來源：<https://modelcontextprotocol.io/specification/latest>、<https://modelcontextprotocol.io/specification/versioning>
- draft changelog 全文只有一句：「Changes since the most recent release will accumulate here.」
  來源：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/046fa30efd374370afb87ef830bd788eac5f217e/docs/specification/draft/changelog.mdx>
- tag `2026-07-28` 到 `main` 046fa30 共 263 個 commit。其中 `docs/specification/2026-07-28/` 與 `draft/` 的變更只有：`/docs/draft/...` 連結改成 `/docs/2026-07-28/...`、`subscriptions/listen` 結束時的回應措辭、`x-mcp-header` 整數範圍措辭、typo、OAuth 範例的 redirect URI。沒有新方法、新欄位或新的 MUST。
  來源：<https://github.com/modelcontextprotocol/modelcontextprotocol/compare/5f5440bb26a62e2cf3440b92da5a667efa03b267...046fa30efd374370afb87ef830bd788eac5f217e>
- 同一區間新增 `seps/2640-skills-extension.md`。這是 extension，不是核心協定。下一個規格版本的日期：**未知**。

---

## 3. `2025-06-18`、`2025-11-25`、`2026-07-28` 逐項比較（研究問題 1）

`2026-07-28` 的變更項目逐條列在 changelog：`S/2026-07-28/changelog.mdx`（<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2026-07-28/changelog.mdx>）。下表的「行」指該檔的行號。

| 項目 | 2025-06-18 | 2025-11-25 | 2026-07-28 | 來源 |
|---|---|---|---|---|
| 協定層 session、`Mcp-Session-Id` | server 可以指定（MAY） | 同左 | **移除**。跨呼叫狀態改用 server 產生的 handle，當作一般 tool 參數傳遞 | changelog 行 12；`S/2025-11-25/basic/transports.mdx#L192-L221` |
| `initialize` / `notifications/initialized` 握手 | 必須 | 必須 | **移除**。每個請求在 `_meta` 帶 `io.modelcontextprotocol/protocolVersion` 與 `io.modelcontextprotocol/clientCapabilities`（兩者必填）；`clientInfo` 是 SHOULD | changelog 行 14；`S/2026-07-28/basic/index.mdx#L365-L392` |
| `server/discover` | 無 | 無 | **新增，server MUST 實作**。回傳 `supportedVersions`、`capabilities`、`_meta.serverInfo`。client 呼叫它是 MAY | changelog 行 16；`S/2026-07-28/server/discover.mdx#L1-L101` |
| 缺少必填 `_meta` 欄位 | — | — | server MUST 回 `-32602`，HTTP 400 | `S/2026-07-28/basic/index.mdx#L380-L382` |
| 版本不支援 | `initialize` 時 server 回另一個自己支援的版本 | 同左；HTTP 上 `MCP-Protocol-Version` 無效或不支援時 MUST 回 400 | MUST 回 `UnsupportedProtocolVersionError`（`-32022`），HTTP 400，`data.supported` 列出支援版本 | `S/2025-11-25/basic/lifecycle.mdx#L165-L175`；`S/2025-11-25/basic/transports.mdx#L263-L280`；`S/2026-07-28/basic/versioning.mdx#L41-L78` |
| `MCP-Protocol-Version` header | 新增（`initialize` 之後的請求 MUST 帶） | 同左；沒有 header 時 server SHOULD 當成 `2025-03-26` | 每個 POST MUST 帶，而且 MUST 等於 `_meta` 中的版本，否則 400 + `HeaderMismatch` | `S/2025-06-18/changelog.mdx#L29-L30`（第 8 項）；`S/2026-07-28/basic/transports/streamable-http.mdx#L250-L281` |
| `Mcp-Method` / `Mcp-Name` header | 無 | 無 | **必填**。`Mcp-Method` 所有請求都要；`Mcp-Name` 用於 `tools/call`、`resources/read`、`prompts/get`。非 ASCII 值用 `=?base64?...?=`。server 若解析 body，MUST 檢查 header 與 body 一致，不一致回 400 + `-32020` | changelog 行 35；`S/2026-07-28/basic/transports/streamable-http.mdx#L286-L297`、`#L580-L630` |
| `Mcp-Param-{name}`（`x-mcp-header`） | 無 | 無 | server MAY 在 `inputSchema` 標註；client MUST 支援 | `S/2026-07-28/basic/transports/streamable-http.mdx#L356-L369` |
| HTTP GET 端點 | client MAY 開 GET SSE；server MUST 回 SSE 或 405 | 同左 | **移除**。改用 `subscriptions/listen`（一個 POST，回應是長時間 SSE）。只支援新版的 server 對 GET、DELETE SHOULD 回 405 | changelog 行 18；`S/2025-11-25/basic/transports.mdx#L133-L143`；`S/2026-07-28/basic/transports/streamable-http.mdx#L680-L687` |
| `resources/subscribe` / `unsubscribe` | 有 | 有 | 移除，併入 `subscriptions/listen` | changelog 行 18 |
| SSE 續傳（`Last-Event-ID`、event ID） | 有 | 有，並加入 polling | **移除**。串流中斷時 client MUST 以新 ID 重送請求 | changelog 行 28；`S/2026-07-28/basic/transports/streamable-http.mdx#L157` |
| server 在 SSE 上送 JSON-RPC request | 可以 | 可以 | 不可以。sampling、elicitation、roots 改用 MRTR（`InputRequiredResult`） | changelog 行 24；`S/2026-07-28/basic/transports/streamable-http.mdx#L117-L124` |
| POST 的 `Accept` header | client MUST 同時列出 `application/json` 與 `text/event-stream` | 同左 | 同左 | `S/2025-11-25/basic/transports.mdx#L92`；`S/2026-07-28/basic/transports/streamable-http.mdx#L76-L77` |
| 回應格式 | server 可選 `application/json` 或 SSE；client MUST 都支援 | 同左 | 同左 | `S/2026-07-28/basic/transports/streamable-http.mdx#L88-L91` |
| notification 的 POST | server 接受時 MUST 回 202 且無 body | 同左 | 同左。核心協定在 HTTP 上已沒有 client→server notification；取消改為關閉 SSE | `S/2026-07-28/basic/transports/streamable-http.mdx#L82-L105` |
| JSON-RPC batching | **移除** | 無 | 無。POST body MUST 是單一 request 或 notification，client MUST NOT 送 response | `S/2025-06-18/changelog.mdx#L12`；`S/2026-07-28/basic/transports/streamable-http.mdx#L80-L81` |
| `ping` | 有；接收方 MUST 立即回空結果 | 有 | **移除**（同時移除 `logging/setLevel`、`notifications/roots/list_changed`） | `S/2025-11-25/basic/utilities/ping.mdx#L29`；changelog 行 20 |
| 結果的 `resultType` | 無 | 無 | **所有結果必填**。一般結果為 `"complete"`。client 對舊版 server 缺少此欄位時 MUST 當成 `"complete"` | changelog 行 26；`S/2026-07-28/basic/index.mdx#L73-L85` |
| `ttlMs`、`cacheScope` | 無 | 無 | `tools/list` 等清單結果**必填** | changelog 行 36 |
| `tools/list` 順序 | 未規定 | 未規定 | server SHOULD 固定順序 | changelog 行 34 |
| tool 結果 `content` | 有 | 有 | 有 | `S/2026-07-28/server/tools.mdx#L404-L495` |
| `structuredContent` | **新增**（JSON 物件） | 同左 | 放寬為任何 JSON 值。回傳 structured content 的 tool SHOULD 另附序列化的 TextContent | `S/2025-06-18/changelog.mdx#L14`；`S/2025-11-25/server/tools.mdx#L324-L326`；`S/2026-07-28/server/tools.mdx#L496-L500` |
| `outputSchema` | **新增**；有 schema 時 server MUST 回符合 schema 的結果 | 同左 | 放寬為任何 JSON Schema 2020-12 關鍵字 | changelog 行 49；`S/2026-07-28/server/tools.mdx#L509-L515` |
| `isError` | tool 執行錯誤用 `isError: true` | 釐清：輸入驗證錯誤 SHOULD 用 `isError`，不用協定錯誤（SEP-1303） | 同左。未知 tool、格式錯誤的請求用 JSON-RPC 錯誤（例：`-32602`） | `S/2025-11-25/changelog.mdx#L28`；`S/2026-07-28/server/tools.mdx#L738-L785` |
| 未知方法的 HTTP 狀態 | 規格未規定 | 規格未規定 | MUST 回 **HTTP 404** + `-32601` | `S/2026-07-28/basic/transports/streamable-http.mdx#L271-L275` |
| 錯誤碼 | `-32002`（resource not found） | 另有 `-32042`（URL elicitation） | `-32000`～`-32019` 保留給舊實作；`-32020`～`-32099` 只給規格。新碼：`-32020` HeaderMismatch、`-32021` MissingRequiredClientCapability、`-32022` UnsupportedProtocolVersion。不得再送 `-32002`、`-32042` | changelog 行 37、行 61；`S/2026-07-28/basic/index.mdx#L109-L155` |
| Origin 檢查 | server MUST 檢查 | 釐清：`Origin` 存在且無效時 MUST 回 403 | 同左 | `S/2025-11-25/changelog.mdx#L26`；`S/2026-07-28/basic/transports/streamable-http.mdx#L54-L65` |
| 無狀態 | — | — | server MUST NOT 依賴同一連線上先前的請求 | `S/2026-07-28/basic/index.mdx#L182-L207` |
| 棄用 | — | — | Roots、Sampling、Logging 棄用；HTTP+SSE transport 列為 Deprecated；Dynamic Client Registration 棄用，改用 Client ID Metadata Documents | changelog 行 73、82、93 |
| tasks | — | 實驗性加入核心 | 移出核心，改為 extension `io.modelcontextprotocol/tasks` | changelog 行 22 |

---

## 4. 版本協商與向下相容（研究問題 2）

### 4.1 規格的分類

`2026-07-28` 把版本分成兩個時代（`S/2026-07-28/basic/versioning.mdx#L29-L39`）：

- **Modern**：每個請求帶版本、身分、capabilities（`2026-07-28` 起）。
- **Legacy**：以 `initialize` 建立 session（`2025-11-25` 以前）。
- **Dual-era**：兩者都支援的實作。

### 4.2 相容矩陣（規格原文摘要）

來源：`S/2026-07-28/basic/versioning.mdx#L159-L172`

| Client | Server | 結果 |
|---|---|---|
| Modern | Modern | 可用 |
| Modern | Legacy | **失敗** |
| Dual-era | Modern | 可用 |
| Dual-era | Legacy | 可用。HTTP 上 modern 請求收到 4xx 且 body 不是可辨識的 modern 錯誤時，client 退回 `initialize` |
| Legacy | Modern | **失敗**。請求缺少必填 header，server 回 400。legacy client 沒有往新版切換的機制 |
| Legacy | Dual-era | 可用 |

### 4.3 規格中的 MUST / SHOULD / MAY

- server 支援 legacy 與 modern 兩種 client 是 **MAY**（`S/2026-07-28/basic/versioning.mdx#L128-L130`）。
- HTTP 上的 dual-era client **MAY** 先送 modern 請求。收到 400 時 **SHOULD** 先看 body：是可辨識的 modern 錯誤就用 `supported` 重試；body 為空或不是 modern 錯誤就退回 `initialize`（`S/2026-07-28/basic/transports/streamable-http.mdx#L650-L668`）。
- client **SHOULD** 依 origin 快取判定結果（`S/2026-07-28/basic/versioning.mdx#L148-L152`）。
- 只支援 modern 的 server 收到 `initialize` 時，錯誤訊息 **SHOULD** 列出支援的版本（`S/2026-07-28/basic/versioning.mdx#L154-L157`）。
- 只支援 `2026-07-28` 的 server 收到舊 client 流量：GET 或 DELETE **SHOULD** 回 405；忽略 `Mcp-Session-Id`，不產生也不回傳 session ID；忽略 `Last-Event-ID`（`S/2026-07-28/basic/transports/streamable-http.mdx#L680-L687`）。
- 不支援 `2025-06-18` 以前 client 的 modern server，收到沒有 `MCP-Protocol-Version` 的請求 **MUST** 拒絕（`S/2026-07-28/basic/transports/streamable-http.mdx#L277-L281`）。
- legacy 協商（`2025-11-25`）：client 在 `initialize` 送自己支援的最新版。server 支援該版時 **MUST** 回同一版；不支援時 **MUST** 回另一個自己支援的版本（`S/2025-11-25/basic/lifecycle.mdx#L165-L175`）。

### 4.4 只做 `initialize` + `tools/list` + `tools/call` 的 server 的定位

- 對 `2026-07-28`：它是 legacy server，不合規。規格沒有要求 server 必須支援 modern，但 modern-only client 無法使用它（4.2 節）。
- 對 `2025-11-25`：合規的條件是遵守 4.3 節的 legacy 協商規則、`MCP-Protocol-Version` 檢查、GET 回 SSE 或 405、Origin 檢查。計畫目前的描述缺少這幾項（第 9 節）。
- 對目前的 Claude Code：可用。Claude Code 2.1.284 是 dual-era client（第 6、8 節）。
- 推論：只要 Claude Code 保留 legacy 退回路徑，legacy server 就能使用。Claude Code 何時移除 legacy 路徑：**未知**。規格的 12 個月棄用期（changelog 行 107）適用於「Deprecated 的功能」；`initialize` 在 `2026-07-28` 是直接移除，不是棄用。client 對舊版本的支援期限不在規格範圍內（推論）。

---

## 5. Streamable HTTP 的現行要求與手寫 server 的最小集合（研究問題 3）

### 5.1 兩個版本的共同要求

| 要求 | 強度 | 來源 |
|---|---|---|
| 單一 MCP endpoint，支援 POST | MUST | `S/2026-07-28/basic/transports/streamable-http.mdx#L47-L49` |
| 檢查 `Origin`；存在且無效時回 403 | MUST | 同檔 `#L58-L62` |
| 本機執行時只綁 127.0.0.1 | SHOULD | 同檔 `#L63-L64` |
| 所有連線做認證 | SHOULD | 同檔 `#L65` |
| client 的 `Accept` 同時列 `application/json` 與 `text/event-stream` | client MUST | 同檔 `#L76-L77` |
| request 的回應用 `application/json` 或 SSE，二選一 | MUST（server 可只用 JSON） | 同檔 `#L88-L91` |
| notification 被接受時回 202、無 body；不接受時回 4xx | MUST | 同檔 `#L82-L87` |
| body 是單一 JSON-RPC 訊息，沒有 batch | MUST | 同檔 `#L80-L81`；`S/2025-06-18/changelog.mdx#L12` |
| request `id` 不可為 `null` | MUST | `S/2026-07-28/basic/index.mdx#L46-L47` |

server 是否必須檢查 client 的 `Accept`：規格只對 client 下 MUST。server 端沒有對應的 MUST（依上列原文推論）。

### 5.2 Legacy（`2025-11-25`）server 的最小集合

1. `initialize`：依 4.3 節規則選版本，回 `protocolVersion`、`capabilities`（含 `tools`）、`serverInfo`。
2. `notifications/initialized` 與其他 notification：202、無 body。
3. `tools/list`、`tools/call`。
4. `ping`：回空結果（`S/2025-11-25/basic/utilities/ping.mdx#L29`）。
5. `MCP-Protocol-Version` 存在但無效或不支援：400（`S/2025-11-25/basic/transports.mdx#L279-L280`）。沒有此 header 時 SHOULD 當成 `2025-03-26`（同檔 `#L273-L277`）。
6. `GET` 到 MCP endpoint：回 405 或 SSE（同檔 `#L140-L142`）。
7. `DELETE`（結束 session）：server MAY 回 405（同檔 `#L217-L220`）。不發 session 時不會收到有效的 DELETE（推論）。
8. Origin 檢查（5.1 節）。

### 5.3 Modern（`2026-07-28`）server 的最小集合

1. 驗證 `MCP-Protocol-Version`：必須存在；不支援時 400 + `-32022`，`data.supported` 列出支援版本；與 `_meta.io.modelcontextprotocol/protocolVersion` 不同時 400 + `-32020`。
2. 驗證 `Mcp-Method` 等於 body 的 `method`；`tools/call` 驗證 `Mcp-Name` 等於 `params.name`，先解碼 `=?base64?...?=`。缺少或不一致：400 + `-32020`。
3. 驗證 `_meta` 必填欄位 `protocolVersion`、`clientCapabilities`。缺少：400 + `-32602`。
4. `server/discover`：回 `resultType`、`supportedVersions`、`capabilities`；`_meta.io.modelcontextprotocol/serverInfo` 是 SHOULD。
5. `tools/list`：回 `resultType: "complete"`、`tools`、`ttlMs`、`cacheScope`。
6. `tools/call`：回 `resultType: "complete"`、`content`，驗證錯誤用 `isError: true`。未知 tool 用 `-32602`。
7. 未知方法（含 `initialize`、`ping`）：HTTP 404 + `-32601`。錯誤訊息 SHOULD 列出支援的版本（4.3 節）。
8. notification：202。
9. `GET`、`DELETE`：SHOULD 回 405。忽略 `Mcp-Session-Id`、`Last-Event-ID`。
10. 每個結果的 `_meta` SHOULD 帶 `io.modelcontextprotocol/serverInfo`（`S/2026-07-28/basic/index.mdx#L394-L402`）。
11. 不使用 `x-mcp-header` 時，server 不需要驗證 `Mcp-Param-*`（依 `S/2026-07-28/basic/transports/streamable-http.mdx#L561-L566` 推論）。
12. Origin 檢查（5.1 節）。

規格只為部分錯誤指定 HTTP 狀態（`-32602` 缺 `_meta`、`-32020`、`-32021`、`-32022` 為 400，`-32601` 為 404）。其他 JSON-RPC 錯誤的 HTTP 狀態：規格**未規定**。官方 Python SDK 的對照表是 `-32700`、`-32600`、`-32602` → 400，`-32601` → 404，`-32603` → 200。
來源：`PY/src/mcp/shared/inbound.py#L306-L318`

### 5.4 fava extension 端點的 method 行為

fava 的 extension 路由接受 GET、POST、PUT、DELETE。`(endpoint, method)` 沒有註冊時 `abort(404)`。
來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/application.py#L347-L362>

所以計畫只註冊 `POST mcp` 時，`GET mcp` 會得到 404，不是 405。Claude Code 2.1.284 在 legacy 流程中會送 `GET mcp`（實測，第 8 節）。對 404 它仍能繼續（實測），但 `2025-11-25` 要求 405 或 SSE。

---

## 6. 授權（研究問題 4）

### 6.1 規格

- 「Authorization is **OPTIONAL** for MCP implementations.」有實作時，HTTP transport **SHOULD** 符合授權規格，stdio **SHOULD NOT**。
  來源：`S/2026-07-28/basic/authorization/index.mdx#L16-L25`
- 「clients and servers **MAY** negotiate their own custom authentication and authorization strategies.」
  來源：`S/2026-07-28/basic/index.mdx#L221-L231`
- 實作授權規格時，MCP server 是 OAuth 2.1 resource server。authorization server 可以與 resource server 同機，也可以是別的實體。MCP server **MUST** 實作 RFC 9728 Protected Resource Metadata。
  來源：`S/2026-07-28/basic/authorization/index.mdx#L47-L73`
- `2026-07-28` 的授權新增：authorization server SHOULD 在授權回應帶 `iss`（RFC 9207），client MUST 驗證；DCR 棄用，改用 Client ID Metadata Documents；client 憑證綁定發行的 authorization server。
  來源：changelog 行 38–48、行 93–99

**固定 Bearer token server 的定位**：它不實作授權規格，屬於規格允許的「custom authentication」（MAY）。它偏離 HTTP transport 的 SHOULD，但不違反任何 MUST，因為授權整體是 OPTIONAL。這與既有研究 7.1 節的結論一致，只是依據應改為 `basic/index.mdx` 的 MAY。

### 6.2 Claude Code 對 401 的處理

官方文件（`CC/mcp#authenticate-with-remote-mcp-servers`，2026-09-30 抓取）：

- 設定了 `Authorization` header（`headers` 或 `headersHelper`）的 server，連線時的 401 或 403 不標成「需要驗證」，而是報連線失敗。
- 「If you configured `headers.Authorization` for the server and the server rejects that header, Claude Code reports the connection as failed instead of falling back to OAuth.」
- 沒有登入過的 server 回 401 或 403 時，`/mcp` 標成需要驗證，讓使用者走 OAuth。
- 回 `WWW-Authenticate` 並指向 authorization server 的自訂 server，會得到同樣的自動 discovery。

本機實測（Claude Code 2.1.284，第 8 節）：

| 情境 | `claude mcp list` 顯示 | server 收到的請求 |
|---|---|---|
| 設定正確 token | `✔ Connected` | 只有 `/mcp` |
| 設定錯誤 token，server 回 401（有或沒有 `WWW-Authenticate: Bearer realm=...`） | `✘ Failed to connect — Server rejected the configured Authorization header (HTTP 401). Check that the token is valid for this MCP endpoint — OAuth fallback is disabled when headers.Authorization is set.` | `POST /mcp`（`server/discover`）→ 401，`POST /mcp`（`initialize`）→ 401。沒有 `/.well-known` 請求 |
| 沒有設定 token，server 回 401 | `✘ Failed to connect — Dynamic Client Registration rejected (HTTP 401):` | `POST /mcp` → 401；`GET /.well-known/oauth-protected-resource/mcp`、`GET /.well-known/oauth-protected-resource`、`GET /.well-known/oauth-authorization-server`、`GET /.well-known/openid-configuration`、`POST /register`；接著 `initialize` 與同一組請求再一次 |

`WWW-Authenticate: Bearer realm="agent"`（沒有 `resource_metadata`）不改變 Claude Code 的行為（實測）。

### 6.3 對本計畫 token 認證的影響

1. 使用者照計畫以 `--header "Authorization: Bearer ..."` 註冊時，token 錯誤只會顯示連線失敗，不會跳 OAuth。這是想要的行為。
2. Lane 7「不帶 token 註冊」會讓 Claude Code 對 fava 的根路徑送 OAuth discovery 與 `POST /register`。這些請求打到 fava 本身，不是 extension。fava 對這些路徑回什麼、`claude mcp list` 顯示什麼文字：**未實測**（推論：多半是 404，訊息會和上表不同）。container log 會多出這些請求。
3. 401 回應依 HTTP 語意應帶 `WWW-Authenticate`（RFC 9110 §15.5.2，<https://www.rfc-editor.org/rfc/rfc9110.html#section-15.5.2>）。帶 `Bearer realm=...` 不會觸發 Claude Code 的 OAuth（實測）。不要帶 `resource_metadata`，因為本計畫沒有 authorization server。
4. `.mcp.json` 的 `headers` 可用 `${VAR}`。Claude Code 把部分憑證變數（例如 `ANTHROPIC_API_KEY`、`NPM_TOKEN`）讀成空字串。清單外的名稱照常展開（`CC/mcp#credential-variables-that-read-as-empty`）。`AGENT_API_TOKEN` 是否在清單內：文件只列「such as」的例子，完整清單**未知**；推論不在內。

---

## 7. Claude Code 與 Python SDK（研究問題 5、6）

### 7.1 Claude Code

本機：`claude --version` → `2.1.284 (Claude Code)`。changelog 最新項目是 `2.1.285`（`CL`）。

`claude mcp add --help`（本機）：

- `-t, --transport <transport>`：`stdio`、`sse`、`http`，預設 `stdio`。
- `-H, --header <header...>`：HTTP/SSE server 的 header，可多個。
- `-s, --scope`：`local`（預設）、`user`、`project`。
- 另有 `--client-id`、`--client-secret`、`--callback-port`（OAuth）。

官方文件（`CC/mcp`，2026-09-30）：

- `claude mcp add --transport http <name> <url>`；帶 token 用 `--header "Authorization: Bearer your-token"`。JSON 設定的 `type` 接受 `streamable-http` 當作 `http` 的別名。
- `--transport http` 會先試 Streamable HTTP，server 不接受時改用 SSE。自動切換需要 v2.1.265 以上。
- 兩種 client runtime：v1（MCP TypeScript SDK 1.x）與 v2（TypeScript SDK 2.0，加入 `2026-07-28`）。會抓 feature flag 的 session 在 v2.1.232 以上用 v2；其他 session 在 v2.1.274 以上預設用 v2。
- v2 runtime「Asks HTTP servers whether they support the newer revision, and uses it with those that do.」
- 「Anthropic can keep a specific server on the earlier protocol ... with a feature flag Claude Code fetches.」
- `MCP_SDK_GENERATION`（`v1`/`v2`）與 `MCP_PROTOCOL_NEGOTIATION`（`auto`/`legacy`）可以覆寫。
  來源：`CC/mcp#mcp-client-runtimes`、`CC/env-vars`

changelog（`CL`）：

- 2.1.274：Bedrock、Vertex、Foundry 與關閉 telemetry 的安裝也改用 v2 client 與 `2026-07-28` 協商，「as other installs already do」（`CL#L987`）。
- 2.1.281：在 `2026-07-28` 連線上加入 URL-mode elicitation（`CL#L438`）。
- 2.1.283：修正無狀態 remote MCP server 短暫回 404 後整個 session 不可用的問題（`CL#L257`）。
- 2.1.238：修正 stdio server 在 `initialize` 前收到 `server/discover`（`CL#L2209`）。

Claude Code 支援哪些 protocol version：

- 實測：HTTP 連線先以 `2026-07-28` 送 `server/discover`，失敗後以 `2025-11-25` 送 `initialize`（第 8 節）。
- 推論（依本機 binary 內的字串，程式碼經過 minify）：legacy 版本清單為 `2025-11-25`、`2025-06-18`、`2025-03-26`、`2024-11-05`、`2024-10-07`；另有 `2026-07-28`。官方文件沒有列出完整清單。

### 7.2 Python SDK（PyPI `mcp`）

- 最新版 `2.2.0`，上傳 2026-09-07，`requires-python >=3.10`。同日另有 v1 線的 `1.30.0`。
  來源：<https://pypi.org/pypi/mcp/json>、<https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0>
- README：「This is v2 of the MCP Python SDK, the current stable release line. ... to support the 2026-07-28 MCP specification (and every earlier revision)」。`pip install mcp` 現在安裝 2.x；v1.x 在 `v1.x` 分支只收重大修正。
  來源：`PY/README.md#L17-L19`
- 依賴含 `starlette`、`sse-starlette`、`uvicorn`、`anyio`、`httpx2`、`pydantic`、`pyjwt[crypto]`、`mcp-types==2.2.0`。
  來源：<https://pypi.org/pypi/mcp/json>（`requires_dist`）
- `2026-07-28` 的 HTTP 處理在 `_streamable_http_modern.py`，入口是 `StreamableHTTPSessionManager.handle_request`，型別是 Starlette 的 `Request`、`Scope`、`Receive`、`Send`（ASGI）。
  來源：`PY/src/mcp/server/_streamable_http_modern.py#L1-L20`、`#L48-L50`
- WSGI：repo 中沒有 WSGI 介面（依 `src/mcp/server/` 檔案清單與上述型別推論）。SDK 能否透過第三方 ASGI→WSGI 轉接器在 fava 的 Flask route 內使用：**未研究**。

**手寫 server 的風險**（推論）：

1. 規格每次改版都要手動跟上。`2025-11-25` → `2026-07-28` 在 HTTP 層改了握手、header、錯誤碼、HTTP 狀態。
2. header 驗證的邊界情況（base64 sentinel、大小寫、數值比較）容易寫錯。SDK 的 `inbound.py` 可以當作對照。
3. 本專案只有一個 client（Claude Code），驗證面小。以 Claude Code 實測加上對規格 MUST 的單元測試，可以控制風險。

---

## 8. 本機實測

**目的**：確認 Claude Code 2.1.284 對計畫中的端點實際送出什麼，以及不同回應的結果。

**方法**：在 session scratchpad 寫一個 Python `http.server` probe，綁 `127.0.0.1`，記錄每個請求的 method、path、header、body。以 `claude -p --strict-mcp-config --mcp-config <json>` 呼叫 tool；以 `CLAUDE_CONFIG_DIR=<scratchpad 目錄>` 執行 `claude mcp add` 與 `claude mcp list`，不動使用者的設定。沒有連線到 192.168.2.11，沒有修改 repo 內其他檔案。probe 與 log 用完即丟。

**probe 模式**：

- `plan`：與 AGENT-2 計畫相同。`initialize` 回傳 client 提出的版本，notification 回 202，未知方法回 HTTP 200 + `-32601`，GET 回 405。
- `plan404`：同上，未知方法回 HTTP 404。
- `get404`：同 `plan`，GET 回 404（模擬 fava 的行為）。
- `modern`：只實作 `2026-07-28` 的 `server/discover`、`tools/list`、`tools/call`，其他方法回 404 + `-32601`。沒有做 header 驗證。
- `deny` / `denywww`：所有請求回 401，後者帶 `WWW-Authenticate: Bearer realm="agent"`。

**結果**：

| 模式 | 請求序列（Claude Code 2.1.284） | 結果 |
|---|---|---|
| `plan` | ① `POST server/discover`（`MCP-Protocol-Version: 2026-07-28`、`Mcp-Method: server/discover`）→ 200 + `-32601` ② `POST initialize`（body `protocolVersion: "2025-11-25"`，沒有 `MCP-Protocol-Version`）→ 回 `2025-11-25` ③ `POST notifications/initialized` → 202 ④ `GET /mcp`（`Accept: text/event-stream`）→ 405 ⑤ `POST tools/list` ⑥ `POST tools/call` | `claude -p` 取得 tool 結果；`claude mcp list` 顯示 `✔ Connected` |
| `plan404` | 同上，① 回 404 | 同上 |
| `get404` | 同 `plan`，④ 回 404 | `claude -p` 取得 tool 結果 |
| `modern` | ① `POST server/discover` → `supportedVersions: ["2026-07-28"]` ② `POST tools/list`（`Mcp-Method: tools/list`）③ `POST tools/call`（`Mcp-Method: tools/call`、`Mcp-Name: list_accounts`） | `claude -p` 取得 tool 結果；`claude mcp list` 顯示 `✔ Connected`。沒有 `initialize`、沒有 GET |
| `modern` + `MCP_PROTOCOL_NEGOTIATION=legacy` | 只有 `POST initialize` → 404 | 連線失敗 |
| `deny` / `denywww` | 見 6.2 節 | 見 6.2 節 |

其他觀察：

- Claude Code 所有請求都沒有 `Origin` header。
- 所有 POST 的 `Accept` 是 `application/json, text/event-stream`。
- legacy 流程中 `tools/call` 的 `_meta` 帶 `claudecode/toolUseId` 與 `progressToken`。
- `claude mcp list` 的健康檢查在 legacy 流程送 5 個請求（discover、initialize、initialized、GET、tools/list），在 modern 流程送 2 個（discover、tools/list）。

---

## 9. 逐項比對 repo 內的 MCP 主張（研究問題 7）

判定：「仍正確」、「需更正」（主張有錯或不完整）、「已過時」（被後來的決定或規格取代）。

### 9.1 `docs/plans/agent-write-api.md`

| # | 位置 | 主張 | 判定 | 依據 | 對 AGENT-2 的影響與建議 |
|---|---|---|---|---|---|
| P1 | L140 | 處理 `initialize`、`notifications/*`（202）、`tools/list`、`tools/call`、`ping`，其他方法回 `-32601` | 需更正 | 對 Claude Code 2.1.284 可用（實測）。但對 `2026-07-28` 不合規（第 4 節），對 `2025-11-25` 缺少版本選擇、header 檢查、GET 405、Origin 檢查（5.2 節）。`ping` 在 `2026-07-28` 已移除 | 依第 10 節的決定改寫 Build 項目 |
| P2 | L140 | 回應一律 `application/json`，不開 SSE | 仍正確 | 兩個版本都允許 server 只回 JSON（5.1 節） | 無 |
| P3 | L141 | 驗證錯誤以 `isError: true` 的 tool 結果回傳 | 仍正確 | `S/2025-11-25/changelog.mdx#L28`；`S/2026-07-28/server/tools.mdx#L760-L783` | 建議同時回 `structuredContent`（422 body）與同內容的 TextContent（SHOULD）。未知 tool 用 `-32602` |
| P4 | L145 | `claude mcp add --transport http ... --header "Authorization: Bearer ..."` 後 `claude mcp list` 顯示 `✔ Connected` | 仍正確 | CLI help 與文件；顯示格式實測相同 | 無 |
| P5 | L153 Lane 1 | trunk 沒有 MCP 端點，預期連線失敗 | 仍正確（推論） | fava 對未註冊端點回 404 | 建議 lane 1 另記錄：是否出現 `server/discover` 探測、協商出的版本、健康檢查的請求數 |
| P6 | L159 Lane 7 | 不帶 token 註冊後連線失敗，container log 有 401 | 需更正 | 不帶 token 時 Claude Code 會走 OAuth discovery 與 `POST /register`（實測，6.2 節）。log 會多出打在 fava 根路徑的請求，顯示文字也不是單純的 401 | 拆成兩條：7a 錯誤 token，預期 `Server rejected the configured Authorization header (HTTP 401)`，log 只有 401；7b 不帶 token，預期連線失敗，log 有 401 與 `/.well-known/*`、`/register`，並確認 fava 沒有對這些路徑回 2xx |
| P7 | L160 Lane 8 | `notifications/initialized` 回 202 且 body 為空 | 仍正確 | `S/2026-07-28/basic/transports/streamable-http.mdx#L82-L84` | 方案 B 時仍成立（notification 一律 202） |
| P8 | L161 Lane 9 | `resources/list` 回 `-32601` | 仍正確（不完整） | `2026-07-28` 另要求 HTTP 404 | 方案 B：Pass 條件加上 HTTP 404 |
| P9 | L255–L257 Appendix A 問題三 | Claude Code 2.1.284 以 `--transport http` 註冊後 `✔ Connected`，`claude -p` 可呼叫 tool | 仍正確 | probe 重現（第 8 節） | 補一句：協商結果是 `2025-11-25`，前面有一次失敗的 `server/discover` |
| P10 | L269 Appendix B | SDK 以 ASGI 為基礎，無法直接掛在 Flask（WSGI） | 仍正確 | 7.2 節 | 無 |
| P11 | L269 Appendix B | 手寫的 JSON-RPC 只需處理四個方法 | 需更正 | 計畫本身列了 5 個方法；另外要處理 GET 405、版本與 header 檢查、Origin | 改成列出第 5.2 或 5.3 節的清單 |
| P12 | L279 Appendix C | 2026-07-28 版取消了 `initialize` 握手 | 仍正確 | changelog 行 14 | 無 |
| P13 | L279 Appendix C | 原型以回傳 client 提出的版本號通過 Claude Code 2.1.284 的連線檢查 | 需更正 | Claude Code 在 `initialize` 送 `2025-11-25`（實測）。「回傳 client 提出的版本」對未知版本違反 `S/2025-11-25/basic/lifecycle.mdx#L170-L172` | 改寫為：Claude Code 先試 `2026-07-28` 的 `server/discover`，失敗後退回 `2025-11-25` 握手；server 應只在支援時回傳 client 的版本，否則回 `2025-11-25` |
| P14 | L279 Appendix C | 未來 client 只支援新版時可能失敗 | 仍正確 | 相容矩陣 Modern client × Legacy server = Fails（4.2 節） | 補上可觀察的觸發條件：Claude Code changelog 宣布移除 legacy 退回、或 `server/discover` 失敗時不再退回 |
| P15 | L223 AGENT-3 perf | 單筆記帳的 MCP 呼叫超過 4 次即失敗；probe 以 container log 的 MCP 請求數計 | 需更正 | 每次連線的 legacy 握手與健康檢查就有 5 個 HTTP 請求，modern 有 2 個（實測） | 規則改為只計 `tools/call`，或把連線建立的請求另列 |
| P16 | L141 | 回應內容與 `/api/query` 相同格式 | 不屬 MCP 規格 | — | 方案 B 時 `tools/call` 結果要加 `resultType` |

### 9.2 `docs/research/agent-write-api-and-public-exposure.md` 第 7 節

| # | 位置 | 主張 | 判定 | 依據 | 建議 |
|---|---|---|---|---|---|
| R1 | 7.1 | 規格目錄有 6 個，最新正式版是 `2026-07-28` | 仍正確 | 第 2 節 | 加上查證日期 2026-09-30 與「draft 無新變更」 |
| R2 | 7.1 | 2026-07-28 的 7 項變更（引自 changelog） | 需更正（不完整） | 引文正確。但漏了：移除 `ping`、`resultType` 必填、`ttlMs`/`cacheScope` 必填、MRTR、tasks 移為 extension、錯誤碼重編、未知方法回 404、HTTP+SSE 列為 Deprecated。第 6 項（`Mcp-Method`、`Mcp-Name`）在 changelog 屬 minor change | 以本文第 3 節的表取代 |
| R3 | 7.1 | 無狀態的 Streamable HTTP server 在架構上幾乎等同 REST 端點；Cloudflare Tunnel 不需特殊設定 | 仍正確（推論） | 單一 POST、每個請求自包含（`S/2026-07-28/basic/index.mdx#L182-L207`） | 補上：仍要做 header 驗證；若回 SSE 則要處理 proxy 緩衝（`X-Accel-Buffering: no`，`streamable-http.mdx#L136-L142`） |
| R4 | 7.1 | 授權 OPTIONAL、HTTP SHOULD、stdio SHOULD NOT、RFC 9728 MUST、DCR 棄用 | 仍正確 | 6.1 節 | 補上 `basic/index.mdx` 的「MAY negotiate their own custom authentication」 |
| R5 | 7.1 | 符合規格的授權 HTTP MCP server 等於要架一個 OAuth 2.1 authorization server | 需更正 | MCP server 是 resource server；authorization server 可以是另一個實體（`S/2026-07-28/basic/authorization/index.mdx#L47-L58`） | 改為：server 要實作 RFC 9728 metadata 並驗證外部 AS 發的 token；AS 可以用現成服務 |
| R6 | 7.1 | HTTP + 外部授權層偏離 SHOULD 但不違反 MUST | 仍正確 | 6.1 節 | 無 |
| R7 | 7.2 | `mcp` 最新版 2.2.0，2026-09-07，`>=3.10`，支援 2026-07-28 與所有舊版，支援 stdio、Streamable HTTP、SSE | 仍正確 | 7.2 節 | 補上 v1 線 `1.30.0` 與 ASGI 依賴 |
| R8 | 7.2 | SDK 是純 Python（推論） | 未重查 | — | 保留推論標示 |
| R9 | 7.3 | 沒有任何 beancount MCP server 跟上 2026-07-28 | 未重查 | 不在本次範圍 | — |
| R10 | 7.4 | 規格 18 個月內改了 5 個版本 | 需更正 | `2024-11-05` 到 `2026-07-28` 約 20 個月，共 5 個版本（含第一版） | 改為「約 20 個月內發佈 5 個版本」 |
| R11 | 7.4 | `tools/list` 回傳 JSON Schema 2020-12，2026-07-28 放寬 | 仍正確 | changelog 行 49 | 無 |
| R12 | 7.5 | REST + MCP 雙介面，MCP 用 stdio 起步、`mcp>=2.2.0`、獨立 container | 已過時 | 計畫改為 fava extension 內手寫 HTTP MCP（計畫 Appendix B） | 由 AGENT-3 的更正區塊處理 |

### 9.3 `docs/research/ezbookkeeping-api.md` 第 8 節、第 12.8 節

| # | 位置 | 主張 | 判定 | 依據 | 建議 |
|---|---|---|---|---|---|
| E1 | 8.1 | ezBookkeeping 只有 `POST /mcp`，`notifications/initialized` 回 202，支援到 `2025-06-18` | 仍正確（未重查 ezBookkeeping 原始碼） | 描述 ezBookkeeping v2.0.1 | 補一句：依 `2026-07-28` 分類，它是 legacy server |
| E2 | 8.1 | 「所以沒有 SSE 串流」的根據是 `GET /mcp` 回 Method Not Allowed | 仍正確 | 這正是 `2025-11-25` 對「不提供 GET 串流」的 405 要求 | 可作為 AGENT-2 註冊 `GET mcp` 回 405 的參考 |
| E3 | 8.6 | Claude Code 的 `--transport http`、`--header`、`--scope`、`.mcp.json` `type: "http"`、`${VAR}` 展開、`streamable-http` 別名 | 仍正確 | `CC/mcp`（2026-09-30）；本機 `claude mcp add --help` | 補上：部分憑證變數名稱會讀成空字串（6.3 節第 4 點） |
| E4 | 12.8 | tool 集合 `list_accounts`、`query_balances`、`query_transactions`、`add_transaction`，底層經 fava HTTP API | 已過時 | 計畫改為 `list_accounts`、`query`、`add_transaction`，在 process 內呼叫 fava 函式（計畫 L140） | 不是規格問題。AGENT-3 可在此節加註 |
| E5 | 12.8 | 傳輸架構見既有筆記 7.5 節 | 已過時 | 同 R12 | 同 R12 |
| E6 | 13.3 | MCP `tools/call` 的 JSON 範例 | 仍正確（legacy 形狀） | `2026-07-28` 由 client 另加 `params._meta` 與 header；`arguments` 不變 | 可加註「client 會自動補上 `_meta`」 |
| E7 | 14 第 6 項 | ezBookkeeping 對新版 client 的行為未知 | 部分可推論 | Claude Code 2.1.284 會先試 `server/discover`，失敗後以 `2025-11-25` 送 `initialize`（實測，對 probe）。ezBookkeeping 會回 `2025-06-18`（8.1 節）。該版在 Claude Code 的版本清單中（binary 字串，推論） | 推論：可連線。仍未實測 |

---

## 10. 對 AGENT-2 的建議修改

只列建議。不修改計畫檔。

### 10.1 決定協定版本

| 方案 | 內容 | 對 Claude Code 2.1.284 | 代價 | 風險 |
|---|---|---|---|---|
| A | 只做 legacy（`2025-11-25`），即目前計畫加上修正 | 可用（實測） | 最少 | 相容矩陣中 modern-only client 會失敗；Claude Code 何時移除 legacy 退回：未知 |
| B | 只做 modern（`2026-07-28`） | 可用（實測，`claude -p` 與 `claude mcp list`） | 要做 5.3 節的驗證 | `MCP_PROTOCOL_NEGOTIATION=legacy`、v1 runtime、Claude Code < 2.1.232 會失敗（實測其中第一項）。Anthropic 可用 feature flag 讓特定 server 走舊版（文件），對本 server 是否適用：未知 |
| C | 兩者都做（dual-era） | 可用（推論） | 最多 | 兩條路徑都要測 |

建議方案 B。理由：

1. 被觀察到的 caller（Claude Code 2.1.284）預設先試 `2026-07-28`，官方文件寫 v2 runtime 已是預設。
2. `2026-07-28` 是目前版本。legacy 路徑在規格中已是「舊時代」。
3. 每次連線的請求數從 5 降到 2，對 AGENT-2 perf 與 AGENT-3 的請求數規則有利。
4. 只維護一條路徑。

若使用者想保留原型已證明的路徑，選方案 A，並套用 10.3 節。方案 C 只有在出現必須使用 legacy 的 client 時才值得做。

### 10.2 方案 B 的具體修改

- Build：處理 `server/discover`、`tools/list`、`tools/call`。notification 回 202。其他方法（含 `initialize`、`ping`）回 HTTP 404 + `-32601`，訊息寫明支援 `2026-07-28`。
- Build：驗證 `MCP-Protocol-Version`、`Mcp-Method`、`Mcp-Name`（含 base64 sentinel 解碼）與 `_meta` 必填欄位，錯誤碼與 HTTP 狀態依 5.3 節。
- Build：所有結果加 `resultType: "complete"` 與 `_meta.io.modelcontextprotocol/serverInfo`。`tools/list` 加 `ttlMs` 與 `cacheScope`。推論：帳本資料受 token 保護，`cacheScope` 用 `"private"`。tool 依固定順序回傳。
- Build：不宣告 `listChanged`，這樣 client 不會開 `subscriptions/listen`（實測：`capabilities: {"tools": {}}` 時 Claude Code 沒有送 `subscriptions/listen`）。
- 測試：unit test 加入 header 缺少、header 與 body 不符、版本不支援、缺 `_meta` 四個案例。
- Lane 2、9：`curl` 範例要帶 `MCP-Protocol-Version`、`Mcp-Method`、`Mcp-Name` 與 `_meta`。
- Appendix A 問題三：註明原型證明的是 legacy 路徑；方案 B 的證據是本文第 8 節的 probe，正式實作要在 lane 1 重新證明。
- Appendix C：風險改寫為「Claude Code 設成 `MCP_PROTOCOL_NEGOTIATION=legacy` 或使用 v1 runtime 時無法連線」。

### 10.3 方案 A 的具體修改

- `initialize`：支援版本集合寫死（例如 `2025-11-25`、`2025-06-18`）。client 的版本在集合內就回同一值，否則回 `2025-11-25`。不要無條件回傳 client 的版本。
- 請求帶 `MCP-Protocol-Version` 且不在集合內時回 400。
- 保留 `ping`。
- 對 `server/discover` 回 `-32601`（HTTP 200 或 404 都可，實測兩者 Claude Code 都會退回）。
- Appendix C：改寫 P13（9.1 節）。

### 10.4 兩個方案都要做

- 在 extension 註冊 `GET mcp` 與 `DELETE mcp`，回 405。fava 預設回 404（5.4 節）。
- Origin 檢查：`Origin` 存在且不在允許清單時回 403。Claude Code 不送 `Origin`（實測），所以允許清單可以是空的。
- body 是 JSON 陣列（batch）時回 `-32600`。
- 認證在協定處理之前做。401 帶 `WWW-Authenticate: Bearer realm="..."`，不帶 `resource_metadata`。
- 未知 tool 回 `-32602`。驗證錯誤回 `isError: true`，並附 `structuredContent` 與同內容的 TextContent。
- Lane 7 拆成錯誤 token 與沒有 token 兩條（9.1 節 P6）。
- Lane 1 記錄 Claude Code 版本、是否出現 `server/discover`、協商出的版本、健康檢查的請求數。
- AGENT-3 perf 規則只計 `tools/call`（9.1 節 P15）。
- 合併前的 `interrogate` 審查加入本文 5.2 或 5.3 節的清單。

---

## 11. 未知

1. 下一個 MCP 規格版本的日期與內容。draft changelog 目前是空的。
2. Claude Code 何時移除 legacy 退回路徑，或何時改變 `MCP_PROTOCOL_NEGOTIATION` 的預設。
3. Anthropic 的 feature flag 是否會讓本 server 走舊版協定（文件說可以對「specific server」這樣做，條件未公開）。
4. Claude Code 完整的 legacy 版本清單。本文只有 binary 字串的推論。
5. 原型（Appendix A）當時的實際請求序列。本文以 probe 重現，原型沒有重跑。
6. fava 對 `/.well-known/oauth-*`、`/.well-known/openid-configuration`、`POST /register` 的實際回應，以及 Lane 7b 的 `claude mcp list` 顯示文字。
7. Claude Code 對「header 驗證嚴格的 modern server」的行為。probe 的 `modern` 模式沒有做驗證；Claude Code 送出的 header 與 body 一致（實測），所以推論不會觸發 `-32020`。
8. ASGI→WSGI 轉接器能否讓 SDK 在 fava 的 Flask route 內運作。
9. Claude Code 讀成空字串的憑證變數完整清單。
10. ezBookkeeping v2.0.1 對 Claude Code 2.1.284 的實際連線結果。

---

## 12. 來源清單

### MCP 規格（tag `2026-07-28`，commit `5f5440b`）

- changelog：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2026-07-28/changelog.mdx>
- Streamable HTTP：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2026-07-28/basic/transports/streamable-http.mdx>
- Versioning：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2026-07-28/basic/versioning.mdx>
- Discover：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2026-07-28/server/discover.mdx>
- Base protocol：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2026-07-28/basic/index.mdx>
- Tools：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2026-07-28/server/tools.mdx>
- Authorization：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2026-07-28/basic/authorization/index.mdx>

### MCP 規格（`2025-11-25`、`2025-06-18`，同一 commit）

- `2025-11-25` changelog：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2025-11-25/changelog.mdx>
- `2025-11-25` transports：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2025-11-25/basic/transports.mdx>
- `2025-11-25` lifecycle：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2025-11-25/basic/lifecycle.mdx>
- `2025-11-25` ping：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2025-11-25/basic/utilities/ping.mdx>
- `2025-11-25` tools：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2025-11-25/server/tools.mdx>
- `2025-06-18` changelog：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/5f5440bb26a62e2cf3440b92da5a667efa03b267/docs/specification/2025-06-18/changelog.mdx>

### MCP 規格 `main`（commit `046fa30`）

- draft changelog：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/046fa30efd374370afb87ef830bd788eac5f217e/docs/specification/draft/changelog.mdx>
- tag 之後的差異：<https://github.com/modelcontextprotocol/modelcontextprotocol/compare/5f5440bb26a62e2cf3440b92da5a667efa03b267...046fa30efd374370afb87ef830bd788eac5f217e>
- 官方網站：<https://modelcontextprotocol.io/specification/latest>、<https://modelcontextprotocol.io/specification/versioning>

### Claude Code

- 文件（2026-09-30 抓取，頁面無版本號）：<https://code.claude.com/docs/en/mcp>、<https://code.claude.com/docs/en/env-vars>
- changelog（commit `ec44ca9`）：<https://github.com/anthropics/claude-code/blob/ec44ca97dc86c33d934c8d55b24959aabf076871/CHANGELOG.md>
- 本機：`claude --version`、`claude mcp --help`、`claude mcp add --help`、第 8 節的 probe

### Python SDK（tag `v2.2.0`，commit `9972c21`）

- README：<https://github.com/modelcontextprotocol/python-sdk/blob/9972c21aa42054fb1450c5fc614761ed11847ec6/README.md>
- `_streamable_http_modern.py`：<https://github.com/modelcontextprotocol/python-sdk/blob/9972c21aa42054fb1450c5fc614761ed11847ec6/src/mcp/server/_streamable_http_modern.py>
- `inbound.py`：<https://github.com/modelcontextprotocol/python-sdk/blob/9972c21aa42054fb1450c5fc614761ed11847ec6/src/mcp/shared/inbound.py>
- PyPI：<https://pypi.org/pypi/mcp/json>

### fava（tag `v1.30.16`）

- `application.py` extension 路由：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/application.py#L347-L362>
