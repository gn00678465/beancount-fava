---
name: beancount-ledger
description: 在使用者的 beancount 帳本（fava）記帳與查帳。使用者給出支出、收入或轉帳（例如「9/24 晚餐 190（錢包）」、「今天早上 7 點早餐 80（錢包）」），問某天或某月花了多少，問帳本的帳戶、餘額或交易，或要求核准、修改、刪除交易時，先載入這個 Skill，再呼叫 beancount 的 MCP tool 或 curl。帳戶名稱找不到時也適用。
---

# 在 beancount 帳本記帳

你只能讀取帳本與新增交易。新增的交易一律是 `!`（待核准）。核准、修改、刪除交易由使用者在 fava 網頁操作，你不能做。

以使用者的語言回覆。使用者寫中文時，用繁體中文與台灣用語。

## 選擇路徑

兩條路徑的結果相同。讀花費只有 curl 路徑。

- 有 MCP tool `list_accounts`、`query`、`add_transaction` 時，用 MCP tool。
- 沒有這些 tool 時，用 `curl`（8.3 以上）。每個 curl 都以下面這段開頭。curl 自己從環境變數讀取網址與 token，指令中不要寫 `$`，也不要印出 token。你看不到這兩個環境變數，但它們已經設定好。不要先檢查環境，也不要向使用者要網址或 token，直接執行 curl。只有 curl 回報 `Variable 'BEANCOUNT_...' import fail, not set` 時，才請使用者設定該變數。

  ```sh
  curl -sS --variable %BEANCOUNT_FAVA_URL --variable %BEANCOUNT_AGENT_TOKEN --expand-header 'Authorization: Bearer {{BEANCOUNT_AGENT_TOKEN}}'
  ```

| 動作 | MCP tool | 接在 curl 開頭之後 |
|---|---|---|
| 讀帳戶 | `list_accounts` | `--expand-url '{{BEANCOUNT_FAVA_URL}}/api/ledger_data'` |
| 查交易 | `query`，參數 `query_string` | `-G --data-urlencode 'query_string=<BQL>' --expand-url '{{BEANCOUNT_FAVA_URL}}/api/query'` |
| 新增交易 | `add_transaction`，參數同右方 JSON | `-X POST -H 'Content-Type: application/json' -d '<JSON>' --expand-url '{{BEANCOUNT_FAVA_URL}}/extension/AgentApi/transactions'` |
| 讀一天的花費 | 沒有，有 MCP tool 時也用 curl | `-X POST -H 'Content-Type: application/json' -d '{"date": "2026-10-05"}' --expand-url '{{BEANCOUNT_FAVA_URL}}/extension/AgentApi/spending'` |

直接讀 curl 的 JSON 輸出，不要 pipe 到其他指令。不要送 PUT 或 DELETE 到 `/api/`，也不要開 fava 的網頁路徑。這些請求會得到 403。

## 記一筆帳

1. 查同一天、同金額的交易，例如 `SELECT date, flag, narration, account, position, links WHERE date = 2026-09-24 AND number = 190`。
2. 查詢結果有同日同額的交易時，列出該交易並問使用者是否仍要新增。不要寫入。
3. 新增交易。帳戶由 fava 解析，不必先讀帳戶。
   - `source`：錢流出的帳戶，例如括號內的「錢包」。
   - `target`：錢流入的帳戶，例如「晚餐」。
   - 帳戶可以寫全名、完整的 `name-zh`，或 `name-zh` 的最後一段。照使用者的用詞送出，不要自己換成別的帳戶。
   - `date`：`YYYY-MM-DD`。使用者沒寫年份時用今年。
   - `time`：使用者給了時刻時，改送 `time`，格式是 RFC 3339 並帶 `+08:00`，例如 `2026-09-25T07:00:00+08:00`。`date` 與 `time` 只送一個。
   - `amount`：正數字串，最多兩位小數，例如 `"190"`。
   - `narration`：使用者的描述，例如 `晚餐`。
   - `payee`：交易對象，例如代墊人。只有使用者提到時才送。
   - `tags`：字串陣列，只能用英數字與 `- _ / .`。中文要放 `meta`。只有使用者提到時才送。
   - `meta`：對帳註記，例如 `{"note": "還代墊"}`。只有使用者提到時才送。
   - `key`：每筆交易產生一個新的 key，只用英數字與 `- _ / .`，例如 `20260924-dinner-190-k3f9`。
   - 不要先送 `dry_run`。只有使用者要求預覽時才送 `"dry_run": true`。
4. 把回應中的 `entry` 原文給使用者看，並說明交易是 `!`，要在 fava 網頁核准。

一次記多筆時，每筆各自產生 key。查交易只做一次，查詢條件涵蓋所有日期。

## 讀回應

| 回應 | 意思與動作 |
|---|---|
| 201 | 已寫入。給使用者看 `entry`。 |
| 200，`created` 為 false | 同一個 key 已經寫入過，帳本沒有變。告訴使用者這筆已存在。有 `dry_run` 時是預覽，沒有寫入。 |
| 409 `key_conflict` | 這個 key 已用在另一筆交易。不要換 key 重送，先問使用者。 |
| 422 `ambiguous_account` | `candidates` 列出候選帳戶。列出它們並問使用者要哪一個。 |
| 422 `unknown_account` 或 `invalid_account_name` | 找不到帳戶。讀帳戶清單，告訴使用者找不到哪個帳戶，並列出相近的帳戶。不要猜。 |
| 其他 422 | 依 `message` 修正該欄位後重送，沿用同一個 key。 |
| 503 `busy`、逾時或連線中斷 | 以同一個 key 與同樣的欄位重送一次。 |
| 401 | token 錯誤或沒有設定。請使用者檢查 `BEANCOUNT_AGENT_TOKEN`。 |
| 403 | 這個路徑不開放給 agent。不要換路徑重試。 |

帳戶清單的全名在 `data.accounts`，中文別名在 `data.account_details["<帳戶>"].meta["name-zh"]`，例如 `食物/晚餐`。

MCP tool 回傳 `isError: true` 時，`structuredContent` 與上表的 body 相同。

不確定是否已寫入時（503、逾時、連線中斷），重送要沿用原來的 key 與完全相同的欄位。同一個 key 只會寫入一次。

## 查花費

使用者問某一天花了什麼、某天或那個月的費用合計、還有哪些花費待核准時，用讀花費的 curl，不要自己寫 BQL。這個端點不受其他請求的篩選條件影響，合計也已經算好。其他問題用 `query`，例如一段日期區間、某個帳戶的交易、餘額，以及記帳前查同日同額的交易。

body 只有 `date`，格式 `YYYY-MM-DD`。使用者沒寫年份時用今年。問「這個月」時送這個月的任一天，讀 `month`。

讀回應：

- `transactions`：那天有費用的交易，依帳本順序。只在資產與負債之間轉帳的交易不會列出。
  - `flag` 是 `!` 時是待核准，`*` 是已核准。
  - `expenses` 是費用的 posting，`funding` 是付款的 posting。
  - `name_zh` 是帳戶的中文名稱，沒有時是 `null`。回覆時優先用它。
- `month.total`：那個月每種幣別的費用淨額，包含待核准的交易，退款以負數抵減。`month.pending` 只算待核准的交易。
- `number` 是字串，以 posting 的幣別計，有匯率也不換算。不同幣別分開列，不要自己加總。
- `ledger_errors` 大於 0 時，帳本有錯誤，出錯的交易不會出現，合計可能不完整。告訴使用者，並請使用者到 fava 網頁檢查。
- `key`：交易的 `^ik-` link 去掉 `ik-` 的部分，通常就是新增交易時送出的 key。要跨次指同一筆交易時用它。沒有這種 link 的交易是 `null`。交易有多個 `^ik-` link 時（例如使用者在 fava 手動加上），只回字母排序最前的一個，所以不一定是當初送出的 key。用 `key` 找不到你記得的交易時，不要斷定交易不存在，改用 `query` 查 `links`。
- `id`：這筆交易目前這個版本的識別。交易改過、被核准，或它在檔案中的位置改變後，`id` 就不同。不要記住 `id` 拿來指同一筆交易。

剛新增交易或使用者剛核准後，讀到的資料可能還是之前的。數字看起來沒更新時，再讀一次。

| 回應 | 意思與動作 |
|---|---|
| 200 | 照上面讀。 |
| 422 `invalid_date` | 日期格式錯誤或不存在，例如 2 月 30 日。修正日期後重送。 |
| 422 `missing`、`unknown_field`、`invalid_body` | body 只能是 `{"date": "YYYY-MM-DD"}`。照這個形狀重送。 |
| 401 | token 錯誤或沒有設定。請使用者檢查 `BEANCOUNT_AGENT_TOKEN`。 |
| 403 | 用了 `POST` 以外的 method。只用 `POST`。 |

## 核准、修改、刪除

使用者要求核准、修改或刪除交易時，說明你做不到。請使用者在 fava 網頁操作。核准是把交易的 `!` 改成 `*`。使用者要從 Cloudflare 網址開啟 fava 網頁。瀏覽器直接開 `BEANCOUNT_FAVA_URL` 會得到 401。
