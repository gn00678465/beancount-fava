# 可寫入 API（AI Agent）與公網發佈研究

查詢日期：2026-09-21。本文補足 `beancount-fava-docker.md` 缺少的部分，不重複其中已確認的事實。

每一項主張都指向原始碼、官方文件或第一方 API。標示「推論」者由已查證事實推導但未實際執行。標示「未知」者代表第一手來源沒有答案。

程式碼引用的版本：fava `v1.30.16`、beancount `3.2.3`（部分引用 `master`，已於各處註明）。

> **2026-09-21 複核更正**（直接對照 fava `v1.30.16` 與 `main` 7096e25 原始碼）
>
> 程式碼層面的事實經複核成立：寫入非原子、只有 `threading.Lock`、`--read-only` 以 `abort(401)` 攔截非 GET、`deserialise` 只支援三種 directive、`ExternallyChangedError` 回 500、watcher 監看父目錄、35 個 endpoint。
>
> 以下三項有誤，已在原處修正：
>
> 1. **引用歸屬錯誤**：`docs/api.rst` 的「no stability guarantee」說的是 Python 模組 API，不是 JSON HTTP API。官方對 JSON API 沒有任何穩定性聲明（3.3 節）。
> 2. **推論錯誤**：「`add_entries` 沒有樂觀鎖」不是缺陷。插入操作在鎖內即時讀檔，不依據客戶端的舊內容（2.3(c) 節）。
> 3. **建議不成立**：第 5.3 節建議方案 (d)（fava `--read-only`）。它的理由有兩條建立在上述兩項錯誤上，並且漏了一個需求：agent 以 `!` flag 提出交易後，人類要在 fava 中改成 `*` 核准，fava 設成 read-only 後這個流程沒有 UI。5.2(a) 的「致命點」也言過其實：公網上的 fava 由 Cloudflare Access 認證，可寫入的 fava 正是使用者要的 web UI。**修正後的建議是方案 (a)**：fava 是唯一寫入 process，自建 API 經由 fava 的 JSON API 寫入，並在 API 層補上認證、冪等、驗證、`!` flag、git commit。第 1.2、5.3、14.2、15.2 節中依 (d) 寫的內容以本更正為準。

---

## 1. 摘要

### 1.1 關鍵結論

| 主題 | 結論 |
|---|---|
| fava 寫入是否原子 | **否**。所有寫入都是 `path.open("w")` 就地截斷後重寫。原始碼中沒有 `os.replace`、`os.rename`、`tempfile`、`fsync`、`fcntl`/`flock` 任何一項 |
| fava 的並行保護 | 只有 process 內的 `threading.Lock`（`FileModule._lock`）。**沒有跨 process 的檔案鎖** |
| 外部修改偵測 | 靠 sha256sum 比對（`put_source`、`put_source_slice`、`delete_source_slice`），但 `put_add_entries` 走插入路徑，**不做 sha256 檢查** |
| fava 重新載入 | `watchfiles`（inotify）背景執行緒追蹤 mtime；前端每 5 秒輪詢 `GET api/changed` |
| `--prefix` 與 API 路徑 | `DispatcherMiddleware` 設定 `SCRIPT_NAME`，`url_for` 一併帶前綴。API 實際路徑是 `<prefix>/<bfile>/api/<endpoint>`。**已驗證，解除既有研究的未知事項 9** |
| fava 認證 | 原始碼中 `csrf`、`cors`、`session[`、`SECRET_KEY`、`authenticat` 全部 0 筆命中 |
| git 稽核 | fava **內建** `fava.ext.auto_commit` extension，每次寫入後 `git commit` |
| 建議的寫入者歸屬 | **方案 (d) 變體**：API 服務是唯一寫入者，fava 以 `--read-only` 執行。詳見第 5 節 |

### 1.2 建議摘要

1. **不要讓兩個程序同時寫 ledger。** fava 的寫入不是原子的，也沒有跨 process 鎖。兩個寫入者在同一次寫入視窗內重疊會產生截斷或交錯的檔案，而不是「後者覆蓋前者」。
2. **API 服務作為唯一寫入者，fava 以 `--read-only` 執行。** `--read-only` 是 `before_request` 層級的 `abort(401)`，攔截所有非 GET 請求，是執行期強制，不靠約定。
3. **API 服務的寫入要自己做原子換檔**：寫 temp file → `os.replace()`。`os.replace` 在 POSIX 上保證原子。
4. **驗證先於落盤**：組出完整檔案內容後用 `beancount.loader.load_string()` 檢查 errors，通過才換檔。
5. **AI Agent 寫入一律用 `!` flag**，beancount 官方語意就是「Incomplete transaction, needs confirmation or revision」。人類在 fava 中改成 `*` 表示核准 —— 但 fava 是 read-only 時無法改，所以核准動作也要走 API。
6. **冪等性用 link（`^`）**，不要用 metadata。fava 的 `ledger_data` 直接回傳所有 link 的清單，查重成本低。
7. **git 稽核不要用 fava 的 auto_commit**（因為 fava 不寫入了），改由 API 服務在每次成功換檔後自行 commit。
8. **對 AI Agent 同時提供 REST 與 MCP**，但兩者共用同一個寫入核心。MCP 2026-07-28 版已無 session、無 initialize 握手，作為 HTTP 服務的實作成本比舊版低很多。
9. **公網只發佈 fava 的 UI**，API 盡量不上公網。若必須上，用 Cloudflare Access service token + origin 端驗證 JWT。

---

## 2. 研究問題 A1：Fava 如何寫入 ledger

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/file.py>

### 2.1 寫入不是原子的

`file.py` 中所有寫入路徑用同一個模式：

```python
newline = _file_newline_character(path)
with path.open("w", encoding="utf-8", newline=newline) as file:
    file.write(source)
```

`"w"` 模式會先把檔案截斷成 0 bytes，再寫入新內容。這出現在五個函式：

| 函式 | 行為 |
|---|---|
| `FileModule.set_source` | 整檔覆寫 |
| `insert_metadata_in_file` | 讀全檔 → 插入一行 → 整檔覆寫 |
| `save_entry_slice` | 讀全檔 → 替換 entry 的行 → 整檔覆寫 |
| `delete_entry_slice` | 讀全檔 → 刪除 entry 的行 → 整檔覆寫 |
| `insert_entry` | 讀全檔 → 插入或附加 → 整檔覆寫 |

在整個 fava 的 `src/` 目錄搜尋 `os.replace`、`os.rename`、`.rename(`、`NamedTemporaryFile`、`tempfile`、`fcntl`、`flock`、`fsync`，**0 筆命中**（實際執行，2026-09-21，對 `v1.30.16` 的 source tarball）。

**結果**：寫入中途 container 被 SIGKILL、磁碟寫滿、或 process 崩潰，會留下被截斷的 ledger 檔。既有研究第 8 節的未知事項 3 在此確認為「**非原子**」。

**對既有研究建議的直接影響**：既有研究 7.3 節建議「fava 是唯一的 ledger 寫入者」。在得知 fava 寫入非原子之後，這個建議需要修正 —— 見第 5 節。

### 2.2 鎖：只有 process 內的 threading.Lock

```python
class FileModule(FavaModule):
    def __init__(self, ledger: FavaLedger) -> None:
        super().__init__(ledger)
        self._lock = threading.Lock()
```

`set_source`、`insert_metadata`、`save_entry_slice`、`delete_entry_slice`、`insert_entries` 都以 `with self._lock:` 包住。

這個鎖的範圍是：

- **一個 `FavaLedger` 實例**。fava 為每個 ledger 檔建立一個 `FavaLedger`（`_LedgerSlugLoader._load`），各自有獨立的 `FileModule` 與鎖。
- **一個 Python process**。跨 container、跨 process 完全無效。

fava 用 cheroot 的多執行緒 WSGI server，所以這個鎖確實有用 —— 它防的是 fava 自己的兩個 HTTP 請求執行緒同時寫。它防不了任何外部程序。

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/file.py>、<https://github.com/beancount/fava/blob/v1.30.16/src/fava/application.py>

### 2.3 外部修改偵測：sha256sum 樂觀鎖，但涵蓋不完整

fava 的 hash 是對**字串內容**取 sha256：

```python
def _sha256_str(val: str) -> str:
    return sha256(encode(val, encoding="utf-8")).hexdigest()
```

三個端點做檢查：

**（a）`set_source`（整檔）** —— 在鎖內重新從磁碟讀檔，比對：

```python
with self._lock:
    _, original_sha256sum = self.get_source(path)
    if original_sha256sum != sha256sum:
        raise ExternallyChangedError(path)
```

`get_source` 是 `path.read_text("utf-8")`，即時讀磁碟，不是讀快取。所以外部修改會被抓到。

**（b）`save_entry_slice` / `delete_entry_slice`（單一 entry）** —— hash 的對象不是整個檔案，而是**這個 entry 的那幾行**：

```python
entry_lines = find_entry_lines(lines, first_entry_line)
entry_source = "".join(entry_lines).rstrip("\n")
if _sha256_str(entry_source) != sha256sum:
    raise ExternallyChangedError(path)
```

這代表：外部程式修改檔案的**其他部分**不會被偵測到。更嚴重的是 `lineno` 來自記憶體中已載入的 entry（`_get_position(entry)`）。若外部程式在該 entry 之前插入了行，記憶體中的 lineno 已經過時，`find_entry_lines` 會取到**錯誤的行**，然後 sha256 不符而拋 `ExternallyChangedError` —— 這算是意外的保護。但若外部插入的內容恰好讓該位置的行內容不變（例如在檔案結尾附加），lineno 仍然正確，沒有問題。

**（c）`put_add_entries`（新增 entry）—— 完全不做 sha256 檢查。**

```python
def insert_entries(self, entries: Sequence[Directive]) -> None:
    with self._lock:
        self.ledger.changed()
        ...
        path, updated_insert_options = insert_entry(...)
```

它只呼叫 `self.ledger.changed()`（觸發重新載入以更新 `insert_entry` 選項），然後直接讀檔、插入、覆寫。API 的 request body 也沒有 `sha256sum` 欄位：

```python
@api_endpoint
def put_add_entries(entries: list[Any]) -> str:
    entries = [deserialise(entry) for entry in entries]
    g.ledger.file.insert_entries(entries)
```

**2026-09-21 複核更正**：本段原先把「沒有 sha256 檢查」當成缺陷，這個推論不成立。樂觀鎖保護的是「客戶端依據舊內容送出修改」的情況（整檔編輯、單一 entry 編輯）。`add_entries` 是插入操作，客戶端不持有檔案的舊內容：`insert_entry` 在 `_lock` 內即時從磁碟讀檔、計算插入位置、寫回。只要 fava 是唯一寫入的 process，這條路徑不需要樂觀鎖，也不會遺失人類在 fava 編輯器中的修改。只有在另一個 process 同時寫同一個檔案時才有競爭，這是「兩個寫入者」的問題，不是這個 endpoint 的問題。

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/file.py>、<https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py>

### 2.4 外部修改後 fava 如何重新載入

**監看機制**：`FavaLedger.__init__` 依 `--poll-watcher` 選擇實作：

```python
self.watcher = WatchfilesWatcher() if not poll_watcher else Watcher()
```

**預設 `WatchfilesWatcher`**（來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/watcher.py>）：

- 起**兩個 daemon thread**：一個非遞迴看 ledger 檔所在的**父目錄**（註解說明是為了抓「編輯器用檔案替換方式儲存」的情況），一個遞迴看 documents 資料夾。
- 用 `watchfiles.watch(...)`，參數 `ignore_permission_denied=True`。
- 執行緒不直接觸發重新載入，只更新 `self.mtime`（取 `st_mtime_ns` 的最大值）。

**`--poll-watcher` 的 `Watcher`**：沒有背景執行緒，也**沒有輪詢間隔**。`_get_latest_mtime()` 在被呼叫時才走訪所有檔案與目錄取 `st_mtime_ns`。所謂「輪詢」是由**前端每 5 秒打一次 API** 驅動的，不是後端自己的計時器。

**觸發重新載入的點**：

```python
def changed(self) -> bool:
    if self._is_encrypted:
        return False
    changed = self.watcher.check()
    if changed:
        self.load_file()
    return changed
```

`check()` 比對 `max(_get_latest_mtime(), last_notified) > last_checked`。

**誰呼叫 `changed()`**：

```python
@fava_app.before_request
def _perform_global_filters() -> None:
    if request.endpoint in {"json_api.get_changed", "json_api.get_errors"}:
        return
    ledger = getattr(g, "ledger", None)
    if ledger:
        if request.blueprint != "json_api":
            ledger.changed()
        ledger.extensions.before_request()
```

注意 `request.blueprint != "json_api"` —— **JSON API 的請求不會在 before_request 自動重新載入**。各個 GET endpoint 自己呼叫 `g.ledger.changed()`（`get_journal`、`get_statistics`、`get_options` 等）。

**輪詢間隔**：前端 `setInterval(poll_for_changes, 5000)`，每 5 秒呼叫 `GET <base>/api/changed`。

```javascript
function poll_for_changes(): void {
  get_changed().catch(log_error);
}
...
setInterval(poll_for_changes, 5000);
```

來源：<https://github.com/beancount/fava/blob/v1.30.16/frontend/src/app.ts>

**`notify()` 的作用**：fava 自己寫入後呼叫 `self.ledger.watcher.notify(path)`，把 `last_notified` 推到該檔案的新 mtime。這讓下一次 `check()` 一定回傳 True，即使 inotify 事件還沒到。

**對容器的意義**：既有研究 3.6(f) 提到網路檔案系統上 `watchfiles` 可能失效。若 API 服務與 fava 在**不同 container**，API 服務寫入後 fava 這邊的 inotify 能否收到事件，取決於是否為同一個 bind mount / volume 與核心的 inotify 傳遞。在同一台主機的 Docker volume 上是同一個核心，inotify 正常。跨主機的網路檔案系統不保證。（推論）

### 2.5 `insert-entry` 與插入位置邏輯

**官方說明**（來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/help/options.md>）：

> This option can be used to specify where entries are inserted. The argument to this option should be a regular expression matching account names. This option can be given multiple times. When adding an entry, the account of the entry (for a transaction, the account of the last posting is used) is matched against all `insert-entry` options and the entry will be inserted before the datewise latest of the matching options before the entry date. If the entry is a Transaction and no `insert-entry` option matches the account of the last posting the account of the second to last posting and so on will be tried. If no `insert-entry` option matches or none is given, the entry will be inserted at the end of the default file.

**實作**（`find_insert_position`）：

```python
accounts = get_entry_accounts(entry)
insert_options = sorted(insert_options, key=attrgetter("date"), reverse=True)
for account in accounts:
    for insert_option in insert_options:
        if insert_option.date >= entry.date:
            continue
        if insert_option.re.match(account):
            return (insert_option.filename, insert_option.lineno - 1)
return (default_filename, None)
```

`InsertEntryOption` 的欄位是 `(date, re, filename, lineno)`，其中 `filename` 與 `lineno` 是那個 `custom "fava-option" "insert-entry"` directive **自己所在的位置**。

**`lineno is None` 表示附加到檔尾**：

```python
if lineno is None:
    contents += "\n" + content
else:
    contents.insert(lineno, content + "\n")
```

注意 `contents` 是 `readlines()` 的 list，`contents += "\n" + content` 會把字串**逐字元**追加成 list 元素（Python 的 `list += str` 語意）。最後 `writelines` 把它們接起來，結果字串正確。這是可用但脆弱的寫法。

**插入後的 lineno 修正**：插入非檔尾時，同一檔案中位於插入點之後的 `insert_entry` 選項的 lineno 會加上新增行數，直接改寫 `self.ledger.fava_options.insert_entry`。這是記憶體狀態，下次 `load_file()` 會重算。

**`default_file`**：

```python
self.ledger.fava_options.default_file or self.ledger.beancount_file_path
```

`default-file` 選項若不給值，就是該 custom directive 所在的檔案；若給相對路徑，以該檔案的目錄解析成絕對路徑。

**排序**：`insert_entries` 用 `_incomplete_sortkey` 排序後逐一插入，同一天的順序為 Open(-2) → Balance(-1) → 其他(0) → Document(1) → Close(2)。

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/file.py>、<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/fava_options.py>

### 2.6 include 多檔的處理

**讀取限制**：`get_source` 只允許讀 beancount 自己回報的 include 清單內的檔案：

```python
if str(path) not in self.ledger.options["include"]:
    raise NonSourceFileError(path)
```

`set_source` 先呼叫 `get_source`，所以**寫入也受同一個限制**。這是一個實質的安全性質：`PUT api/source` 無法寫入 ledger 之外的任意路徑。

**寫入目標的決定**：`insert_entry` 只會寫到 `insert-entry` 選項指定的檔案或 `default_file`。多檔 ledger 中，fava 不會自己猜要寫哪個 include 檔 —— 必須靠 `insert-entry` 或 `default-file` 明示。

**監看範圍**：`paths_to_watch()` 回傳 `options["include"]` 的所有檔案，加上 documents 資料夾，所以任何 include 檔的變動都會觸發重新載入。

### 2.7 換行與編碼

**編碼**：讀寫一律 `encoding="utf-8"`。讀到非 UTF-8 會拋 `InvalidUnicodeError`（`UnicodeDecodeError` 轉譯）。

**換行**：寫入前先偵測檔案原有的換行字元：

```python
def _file_newline_character(path: Path) -> str:
    with path.open("rb") as file:
        firstline = file.readline()
        if firstline.endswith(b"\r\n"):
            return "\r\n"
        if firstline.endswith(b"\n"):
            return "\n"
        return os.linesep
```

只看**第一行**。空檔案或第一行沒有換行時，退回 `os.linesep`（Linux container 內是 `"\n"`）。混合換行的檔案會被統一成第一行的樣式。

自建寫入者若要與 fava 相容，應套用同樣的規則。（推論：fava 未文件化此行為，但這是它的實際契約）

---

## 3. 研究問題 A2：Fava JSON API 的寫入類 endpoint

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py>

### 3.1 路由與協定

- Blueprint 註冊於 `/<bfile>/api`（`application.py`），`<bfile>` 是 ledger 的 slug（由 `title` 選項或檔名 slugify 而來）。
- 函式名第一個底線前是 HTTP method（僅 `get`、`put`、`delete`；**不支援 POST**）。
- PUT 的參數來自 JSON body（必須是 JSON object，否則 `InvalidJsonRequestError`）；GET/DELETE 來自 query string。
- 參數型別只支援 `str`、`int`、`list[Any]`，且只做淺層驗證。

**成功回應**：

```python
return jsonify({"data": data, "mtime": str(g.ledger.mtime)})
```

`mtime` 是 `watcher.last_checked`（奈秒整數轉字串）。

**錯誤回應**：`{"error": "<message>"}`，HTTP status 依例外類別而定。

| 例外 | HTTP status |
|---|---|
| `ValidationError`（缺參數、型別錯、body 非 JSON） | 400 |
| `FilterError` | 400 |
| `NoFileUploadedError`、`UploadedFileIsMissingFilenameError`、`NotAValidDocumentOrImportFileError` | 400 |
| `EntryNotFoundForHashError`、`NotFoundError`、`FileDoesNotExistError` | 404 |
| `TargetPathAlreadyExistsError` | 409 |
| `GeneratedEntryError`、`DocumentDirectoryMissingError`、`NotAFileError` | 422 |
| `FavaAPIError`（基底，含 `ExternallyChangedError`、`NonSourceFileError`、`InvalidUnicodeError`） | **500** |
| `OSError` | 500 |

**注意**：`ExternallyChangedError` 是 `FavaAPIError` 的子類，**不是** `FavaJSONAPIError`，所以樂觀鎖衝突回傳的是 **HTTP 500**，不是 409。錯誤訊息是 `The file at '<path>' changed externally.`。客戶端只能靠字串比對或「500 就重試」來處理衝突。這是把外部契約建在 fava JSON API 上的具體代價。

### 3.2 寫入類 endpoint 逐一

#### `PUT /<bfile>/api/add_entries`

Request body：

```json
{"entries": [ {…entry…}, … ]}
```

每個 entry 經 `fava.serialisation.deserialise` 轉換。支援三種 `t`：

**Transaction**（必要欄位：`t`、`date`、`narration`、`meta`、`tags`、`links`、`postings`；選用：`flag`、`payee`）

```json
{
  "t": "Transaction",
  "date": "2026-09-21",
  "flag": "!",
  "payee": "Some Payee",
  "narration": "Coffee",
  "meta": {},
  "tags": [],
  "links": ["agent-01JXYZ"],
  "postings": [
    {"account": "Expenses:Food:Coffee", "amount": "120 TWD"},
    {"account": "Assets:Cash", "amount": ""}
  ]
}
```

`amount` 是**字串**，由 fava 拼進一個假交易再用 `beancount.parser.parser.parse_string` 解析：

```python
entries, errors, _ = parse_string(f'2000-01-01 * "" ""\n Assets:Account {amount}')
if errors:
    raise InvalidAmountError(amount)
```

空字串代表留白讓 beancount 自動補值。這也表示 `amount` 欄位接受完整的 beancount posting 語法（`@ price`、`{cost}` 等）。

**Balance**：`{"t":"Balance","date":…,"account":…,"amount":{"number":"…","currency":"…"},"meta":{}}`

**Note**：`{"t":"Note","date":…,"account":…,"comment":"…","meta":{}}`。`comment` 中的 `"` 會被直接移除。

回應：`{"data": "Stored N entries.", "mtime": "..."}`

**沒有 sha256sum 參數，沒有樂觀鎖。**

`deserialise` 的 docstring 自己承認：`"This is not intended to work well enough for full roundtrips yet."`
來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/serialisation.py>

#### `GET /<bfile>/api/source` 與 `PUT /<bfile>/api/source`

GET：query `?filename=<path>`（可省略，預設用 `default_file` 或主檔）。回應 `{"file_path", "sha256sum", "source"}`。

PUT body：`{"file_path": "...", "source": "...", "sha256sum": "..."}`。回應 `data` 是新內容的 sha256sum。

這是**唯一有完整整檔樂觀鎖**的寫入端點。自建客戶端若要安全寫入，應走「GET source → 在本地改 → PUT source 帶原 sha256sum」。

#### `GET /<bfile>/api/source_slice`、`PUT`、`DELETE`

GET：`?entry_hash=<hash>` → `{"sha256sum", "slice"}`。
PUT body：`{"entry_hash", "source", "sha256sum"}`。
DELETE：`?entry_hash=<hash>&sha256sum=<sum>`。

`entry_hash` 由 `fava.beans.funcs.hash_entry` 產生，是 fava 內部的 entry 識別碼，**每次重新載入後可能改變**（它依 entry 內容計算）。客戶端必須先取得 entry 才能拿到 hash。

`slice` 的範圍由 `find_entry_lines` 決定：從 entry 起始行開始，往下吃到第一個空行或第一個以非空白字元開頭的行為止。

#### `PUT /<bfile>/api/format_source`

body：`{"source": "..."}` → 回傳對齊後的字串。**純函式，不寫檔。** 可安全用於「把 agent 產生的文字格式化成 fava 慣例」。

#### `PUT /<bfile>/api/add_document`、`PUT /<bfile>/api/upload_import_file`

multipart form，不是 JSON。`add_document` 需要 form 欄位 `folder`、`account`，選用 `hash`（給定時會在該 entry 下插入 `document:` metadata），以及檔案欄位 `file`。

路徑組合經 `filepath_in_document_folder` 檢查：`folder` 必須在 `options["documents"]` 內、`account` 必須是已宣告的帳戶、檔名中的路徑分隔字元被換成空白。**路徑穿越受限。**
來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/documents.py>

#### `PUT /<bfile>/api/attach_document`

body：`{"filename": "...", "entry_hash": "..."}`。在 entry 的下一行插入 `document: "<filename>"`。重複 key 由 `next_key` 改成 `document-2` 之類。

#### `PUT /<bfile>/api/move`

body：`{"account", "new_name", "filename"}`。`shutil.move` 搬移**檔案**（document），不動 ledger 文字。

#### `DELETE /<bfile>/api/document`

`?filename=<path>`。`file_path.unlink()`。只允許已宣告的 Document entry 或 import 目錄下的檔案。

### 3.3 「API 服務作為 fava 的 client」的可行性與風險

**可行**。技術上沒有障礙：純 HTTP、JSON、無認證、無 CSRF token、無 session。一個內網的 HTTP 客戶端可以直接呼叫。

**具體風險**：

| 風險 | 說明 |
|---|---|
| 沒有文件，跨版本可能改變 | 官方對 JSON HTTP API 沒有穩定或不穩定的聲明。`docs/api.rst` 的「no stability guarantee」說的是 Python 模組 API，不適用於此（2026-09-21 複核更正）。`json_api.py` 隨前端重構頻繁變動，見 <https://github.com/beancount/fava/commits/main/src/fava/json_api.py>。對策：釘選 fava 版本，升版時跑 contract test |
| `add_entries` 無樂觀鎖 | 見 2.3(c) 的更正。fava 是唯一寫入 process 時不構成風險 |
| 衝突回傳 500 | `ExternallyChangedError` 對應 500 而非 409，客戶端難以區分「衝突」與「伺服器壞了」 |
| 路徑綁 slug | `<bfile>` 來自 `title` 選項；改 title 會改 URL |
| `entry_hash` 不穩定 | 修改任一 entry 後重新載入，hash 依內容重算 |
| 與 `--read-only` 互斥 | 要讓 API 經 fava 寫入，fava 就不能 `--read-only`，也就等於 fava 的 UI 同時開放寫入 |
| 寫入仍非原子 | 走 fava 不會讓寫入變成原子的；只是把非原子寫入集中在一個程序 |
| 序列化不支援完整往返 | `deserialise` 只支援 Transaction / Balance / Note 三種 directive。Open、Close、Price、Commodity、Event、Document、Custom **無法透過 `add_entries` 新增** |

**最後一點值得強調**：AI Agent 若要開新帳戶（`Open` directive），`add_entries` 做不到。只能走 `PUT source`（整檔覆寫）。

---

## 4. 研究問題 A3：自建寫入者的做法

### 4.1 產生 entries 文字

**兩條路徑**：

**（a）`beancount.parser.printer`**（beancount 原生）

```python
from beancount.parser import printer
text = printer.format_entry(entry, dcontext=None, render_weights=False, prefix=None, write_source=False)
```

`format_entry` 回傳可被 parser 重新接受的字串。`print_entries` 額外處理 directive 之間的空行分隔。
來源：<https://github.com/beancount/beancount/blob/master/beancount/parser/printer.py>

**（b）`fava.beans.str.to_string`**（與 fava 的輸出一致）

```python
to_string(entry, currency_column, indent)
```

內部呼叫 `beancount.parser.printer.format_entry`，再經 `fava.core.misc.align` 做貨幣對齊。fava 的 `insert_entry` 就是用這個。
來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/beans/str.py>

**建議用 (b)**，因為輸出格式會與人類在 fava UI 中新增的交易一致（同樣的 `currency-column` 與 `indent`）。代價是相依 fava（但本專案的 image 本來就裝了 fava）。

**建立 entry 物件**：v3 有公開的 `beancount.api`（`import beancount as bn`），匯出 `Amount`、`Posting`、`Directive`、`dtypes`、`new_metadata` 等。其 docstring：

> The future of Beancount (v3) will bind the majority of public symbols on the root package... Note: This API may change over time, though we're not expecting to remove any symbols on the v3 branch.

來源：<https://github.com/beancount/beancount/blob/master/beancount/api.py>

也可直接用 `beancount.core.data.Transaction` / `Posting` 這些 NamedTuple，或 fava 的 `fava.beans.create` helper（`create.transaction(...)`、`create.balance(...)`、`create.amount(...)`）。

### 4.2 寫入前驗證

**`beancount.loader.load_string(string) -> (entries, errors, options_map)`**

```python
def load_string(
    string: str,
    log_timings=None,
    log_errors=None,
    extra_validations: list[Any] | None = None,
    dedent: bool = False,
    encoding: str | None = None,
) -> tuple[data.Directives, list[data.BeancountError], OptionsMap]:
```

來源：<https://github.com/beancount/beancount/blob/master/beancount/loader.py>

`errors` 非空即代表有問題。`log_errors` 留 None 可避免印到 stderr。

**`extra_validations` 與 HARDCORE**：`bean-check` 用的是

```python
from beancount.ops import validation
entries, errors, _ = loader.load_file(
    filename,
    log_timings=logging.info,
    log_errors=log_errors,
    extra_validations=validation.HARDCORE_VALIDATIONS,
)
```

註解寫「Force slow and hardcore validations, just for check.」。自建 API 可以用同一組。
來源：<https://github.com/beancount/beancount/blob/v3.2.3/beancount/scripts/check.py>

**`bean-check --json`**（3.2.3 已有，實際確認該版本檔案內含 `json_output`）輸出：

```json
{"errors": [{"message": "...", "filename": "...", "lineno": 123}]}
```

離開碼：有 error 為 1，無 error 為 0。適合作為 dry-run endpoint 的實作捷徑（`subprocess` 呼叫），但直接用 `loader.load_file` 避免 process 開銷更好。

**關鍵限制**：`load_string` 驗證的是**一段完整的 ledger 文字**，不是單一 entry。一個 entry 是否合法（帳戶已 Open、balance assertion 成立、金額配平）只有放在整份 ledger 的脈絡中才有意義。所以正確的驗證方式是：

1. 讀出目標檔案的現有內容。
2. 在記憶體中組出「插入新 entry 之後」的完整檔案內容。
3. 用 `load_file` 載入**主檔**（因為 include 關係必須從主檔進入），但主檔讀的是磁碟上的舊內容 —— 這就有落差。

**實務上可行的兩種做法**：

- **做法 A（寫入 → 驗證 → 失敗則回滾）**：先原子換檔，立刻 `load_file(主檔)`，若 errors 增加就換回備份。缺點是有一段時間 ledger 是壞的，fava 可能剛好在此時重新載入。
- **做法 B（沙箱驗證）**：把整個 ledger 目錄複製到 tmpfs，在副本上插入 entry，`load_file` 副本的主檔，通過才對正本原子換檔。缺點是複製成本，以及「副本驗證通過」與「正本換檔時狀態未變」之間仍有 TOCTOU 窗口 —— 但若 API 服務是**唯一寫入者**且持有跨 process 鎖，這個窗口不存在。

**建議做法 B**，並以「唯一寫入者 + 檔案鎖」消除 TOCTOU。

### 4.3 原子寫入

**`os.replace`**：

> Rename the file or directory *src* to *dst*. If *dst* is a non-empty directory, `OSError` will be raised. If *dst* exists and is a file, it will be replaced silently if the user has permission. The operation may fail if *src* and *dst* are on different filesystems. **If successful, the renaming will be an atomic operation (this is a POSIX requirement).**

來源：<https://github.com/python/cpython/blob/main/Doc/library/os.rst>（`os.replace` 條目）

**完整的安全換檔序列**（每一步都有理由）：

1. 在**與目標檔案同一個目錄**建立暫存檔（確保同一個 filesystem，否則 `os.replace` 會失敗）。
2. 寫入新內容，換行字元沿用目標檔案第一行的樣式（與 fava 的 `_file_newline_character` 一致）。
3. `file.flush()` 然後 `os.fsync(file.fileno())` —— 確保內容真的到磁碟。沒有這一步，`os.replace` 只保證「名字的替換」是原子的，不保證斷電後檔案內容完整。
4. `os.replace(tmp, target)`。
5. 對**目錄** fsync，確保 directory entry 的變更落盤。

**權限保留**：`os.replace` 換上去的是暫存檔的 inode，權限與 owner 是暫存檔的。要先 `os.stat` 目標檔案再 `os.chmod`/`os.chown` 暫存檔。在 container 內以固定 uid 執行時，chown 通常不需要。

**與 fava 監看的互動**：`os.replace` 會產生 inotify 的 `IN_MOVED_TO` 事件。fava 的 `_FilesWatchfilesThread` 監看的是**檔案的父目錄**（非遞迴），過濾條件是 `Path(path) in files`。註解明確寫這是為了「to check changes done by file replacements by some editors」—— 也就是說 **fava 的監看設計本來就考慮了原子換檔**。所以 API 服務用 `os.replace` 寫入，fava 會正確偵測到。

**跨 process 鎖**：Python 標準庫沒有可攜的檔案鎖，但 Linux container 內可用 `fcntl.flock()` 對一個 sidecar lock 檔（例如 `<ledger>.lock`）加 `LOCK_EX`。若採「唯一寫入者」架構，這個鎖是防禦性的（防 API 服務自己被啟動兩份），不是協調兩個不同程式。

---

## 5. 研究問題 A4：寫入者歸屬的架構選項

### 5.1 前提事實彙整

1. fava 的寫入**非原子**（2.1）。
2. fava 的鎖只在 process 內（2.2）。
3. fava 最常用的寫入端點 `add_entries` **沒有樂觀鎖**（2.3c）。
4. fava JSON API **無穩定性保證、無認證**（3.3）。
5. `--read-only` 是 `before_request` 的 `abort(401)`，攔截所有非 GET，包含 extension 的 POST/PUT/DELETE（application.py）。
6. ledger 是純文字檔，beancount 本身不提供任何鎖或交易機制（未在任何第一手來源中找到相關機制，標為已查無）。
7. fava 的 watcher 明確設計成能偵測「檔案替換式」的儲存（2.4、4.3）。

### 5.2 四個選項的取捨

#### (a) API 服務一律經由 fava 的 JSON API 寫入（fava 是唯一寫入者）

| | |
|---|---|
| 優點 | 只有一個程序碰檔案；不必自己實作插入位置邏輯（`insert-entry` 由 fava 處理）；寫入格式與 UI 一致 |
| 缺點 | 外部契約綁在無穩定性保證的內部 API；`add_entries` 無樂觀鎖；衝突回 500；**寫入仍非原子**；fava 不能 `--read-only`，代表公網上的 fava UI 也是可寫的；無法新增 Open 等 directive |
| 致命點 | fava 必須可寫 ⇒ 公網發佈的 fava 就是一個無認證的可寫入介面。這與「用 Cloudflare Access 保護」是互補而非互斥，但攻擊面明顯變大 |

#### (b) API 服務直接寫檔，與 fava 並存（兩個寫入者）

| | |
|---|---|
| 優點 | API 可自己控制原子性與驗證；人類仍可在 fava 編輯器中改 |
| 缺點 | **靠 fava 的 sha256sum 協調是不夠的**：`add_entries` 路徑沒有 sha256 檢查；fava 的寫入是「讀全檔 → 改 → 截斷重寫」，若 API 在 fava 讀完之後、寫回之前完成換檔，API 的寫入會被 fava 靜默覆蓋 |
| 檔案鎖能否解決 | 理論上可以，但需要**修改 fava**（fava 原始碼中沒有 `fcntl`/`flock`）。不修改 fava 就無法讓 fava 參與鎖協定 |
| 結論 | **不建議**。這個選項的安全性需要 fava 端的配合，而 fava 沒有提供 |

#### (c) API 以 fava extension 實作，同一個 process

| | |
|---|---|
| 優點 | 共用 `FileModule._lock`，真正避免 process 內競爭；ledger 只載入一份；可用 `after_insert_entry` 等 hook |
| 缺點 | extension 系統的官方說明：「The whole extension system should be considered unstable and it might change drastically.」<https://github.com/beancount/fava/blob/v1.30.16/src/fava/help/extensions.md>；`--read-only` 會**一併擋掉** extension 的非 GET 請求；API 與 UI 共用埠，無法在網路層分離；extension 由 ledger 檔的 `custom "fava-extension"` 載入，代表 image 必須允許掛載使用者的 Python 檔，image 不能是封閉的 |
| 仍未解決 | 寫入依然非原子（走的是同一套 `file.py`），除非 extension 完全繞過 `FileModule` 自己寫 —— 那就退化成 (b) 的 process 內版本 |

#### (d) API 服務是唯一寫入者，fava 以 `--read-only` 執行

| | |
|---|---|
| 優點 | 寫入者只有一個，且是自己寫的程式 —— 可以做原子換檔、驗證後落盤、git commit、冪等檢查、權限分級；`--read-only` + volume `:ro` 是**執行期強制**，不是約定；公網上的 fava 是純唯讀介面，攻擊面最小；API 契約自己定義，不受 fava 內部改動影響 |
| 缺點 | **人類無法在 fava 的編輯器中改帳本**。這是本方案唯一的實質損失 |
| 要自己實作 | `insert-entry` 的插入位置邏輯（或簡化為「一律附加到 default file 末尾」）、格式化（可直接用 `fava.beans.str.to_string`）、換行偵測 |

### 5.3 建議：(d)，並以 API 補回人類編輯能力

> **已由文件開頭的「2026-09-21 複核更正」取代，修正後的建議是方案 (a)。** 以下保留原文供對照。

**理由**：

1. 使用者明確要求「API 必須可以寫入 ledger」，且主要使用者是 AI Agent。Agent 寫入必須可驗證、可稽核、可回復、可冪等。這四項 fava 全部不提供，都要自己做 —— 既然要自己做，就沒有理由讓 fava 也持有寫入權。
2. fava 要透過 Cloudflare Tunnel 上公網。一個**唯讀**的 fava 上公網，風險遠低於一個可寫入且無認證的 fava。`--read-only` 讓這件事變成執行期不變量。
3. 兩個寫入者的協調在 fava 端沒有支援（5.2b），不要建立需要上游配合而上游沒提供的設計。
4. (c) 把外部 API 契約綁在官方自稱 unstable 的 extension 系統上。

**人類編輯能力的補回方式**（三選一，不必一開始就做）：

- API 提供 `GET /source` 與 `PUT /source`（帶 sha256sum 樂觀鎖），前端可以是任何編輯器；或
- 人類直接在主機上用文字編輯器改（ledger 是純文字檔，volume 在主機上）；然後 fava 的 watcher 會偵測到。**但這會引入第二個寫入者** —— 若人類編輯器用原子換檔（多數現代編輯器如 vim 的 `backupcopy=no`、VS Code 預設都是），與 API 的 `os.replace` 之間仍可能互相覆蓋。這是可接受的風險（人類不會在 agent 寫入的那毫秒內存檔），但應在文件中說明；或
- 接受「所有寫入都經 API」，人類透過 agent 或一個簡單的編輯 UI 操作。

### 5.4 Container 佈局：同一 image、兩個 container

沿用既有研究 6.4 的方案 C，但寫入者歸屬對調：

```
                     ledger volume (named volume 或 bind mount)
                     ├── main.beancount
                     ├── 2026.beancount
                     ├── documents/
                     └── .git/
                            │
          ┌─────────────────┴─────────────────┐
          │ :ro                               │ :rw
   ┌──────▼───────┐                   ┌───────▼────────┐
   │ fava         │                   │ api            │
   │ --read-only  │                   │ 唯一寫入者      │
   │ 同一 image   │                   │ 同一 image     │
   │ CMD: fava    │                   │ CMD: api-serve │
   └──────────────┘                   └────────────────┘
```

**為什麼兩個 container 而不是一個 process**：

- `--read-only` 與「可寫入」在同一個 Flask app 裡無法並存（`_read_only` 是全域 `before_request`）。
- 兩個 container 各自是 PID 1 單一程序，訊號、log、健康檢查維持標準語意。
- fava 的 volume 可以真的掛 `:ro`，把唯讀變成核心層級的保證。

**記憶體代價**：ledger 載入兩份。既有研究已標為未知事項 6，本次未測，維持未知。

**`:ro` 掛載的一個副作用**：fava 的 documents 資料夾也變成唯讀，`put_add_document` 會失敗 —— 但 `--read-only` 本來就擋掉了 PUT，所以沒有行為差異。

---

## 6. 研究問題 A5：AI Agent 寫入的安全措施

### 6.1 git 稽核與回復

**beancount / fava 的原生支援程度**：

fava **內建** `fava.ext.auto_commit` extension：

```python
class AutoCommit(FavaExtensionBase):
    def _run(self, args: list[str]) -> None:
        cwd = Path(self.ledger.beancount_file_path).parent
        call(args, cwd=cwd, stdout=DEVNULL)

    def after_write_source(self, path: str, source: str) -> None:
        message = "autocommit: file saved"
        self._run(["git", "add", path])
        self._run(["git", "commit", "-m", message])

    def after_insert_entry(self, entry: Directive) -> None:
        message = f"autocommit: entry on {entry.date}"
        self._run(["git", "commit", "-am", message])
    # 另有 after_insert_metadata / after_delete_entry / after_entry_modified
```

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/ext/auto_commit.py>

啟用方式：在 ledger 檔加 `2010-01-01 custom "fava-extension" "fava.ext.auto_commit"`。

**限制**（全部來自原始碼觀察）：

- 用 `subprocess.call`，**不檢查離開碼**，`stdout=DEVNULL`。git 失敗完全靜默。
- `cwd` 是主 ledger 檔的父目錄，所以 git repo 必須在那裡。
- 多數 hook 用 `git commit -am`，會把工作目錄中**所有**已追蹤檔案的變更一起 commit，不只是這次改的。
- 沒有 `git add -A`，所以新檔案（例如新上傳的 document）不會被加入，除非走 `after_write_source` 路徑。
- 該檔的 module docstring 自承：「This mainly serves as an example how Fava's extension systems... works.」

**對本專案的意義**：既然採方案 (d)，fava 不寫入，這個 extension 不會被觸發。**git commit 應由 API 服務自己做。**

**每次寫入一個 commit 的代價**（推論，未測）：

- 一個 commit 的成本是：blob（改動後的整份檔案）+ tree + commit object。beancount ledger 是文字檔，zlib 壓縮後的 blob 很小，但**每次 commit 都存整份檔案的新 blob**（git 不是 delta 儲存，packing 才做 delta）。
- 個人帳本一年幾千筆交易 → 幾千個 commit。以每份 ledger 100 KB、壓縮後 20 KB 計，鬆散物件約 60 MB/年，`git gc` 之後會大幅縮小。可接受。
- 真正的代價是**可讀性**：幾千個 `autocommit:` commit 會淹沒有意義的歷史。

**建議**：

- API 服務在**成功原子換檔之後**執行 `git add <file> && git commit`，並**檢查離開碼**。
- commit message 帶上 agent 的識別與 idempotency key，讓 `git log --grep` 可以查。
- git commit 是 best-effort 的副作用，**不應該讓它的失敗導致寫入失敗**（帳已經記了）。若 commit 失敗，記 log 並在下一次寫入時一併補上（`git add -A`）。這符合「必要副作用先落盤，best-effort 副作用失敗則記錄並調和」。
- image 必須裝 `git`，且需設定 `user.name` / `user.email`（否則 `git commit` 失敗）。container 內可用 `GIT_AUTHOR_NAME` 等環境變數，或 repo 內的 `.git/config`。
- ledger volume 內要有 `.git`。**若 fava 掛 `:ro`，fava container 不需要 git。**

### 6.2 暫存／待審佇列與 `!` flag

**beancount 的官方語意**（來源：<https://beancount.github.io/docs/beancount_language_syntax/>）：

> A flag is used to indicate the status of a transaction, and the particular meaning of the flag is yours to define.

建議的解讀：

- `*` — 「Completed transaction, known amounts, 'this looks correct.'」
- `!` — 「Incomplete transaction, needs confirmation or revision, 'this looks incorrect.'」

posting 層級也可以帶 flag：

> You can also attach flags to the postings themselves, if you want to flag one of the transaction's legs in particular.

**原生支援程度**：

- beancount 只定義語意，**不強制任何工作流程**。`!` 交易一樣會被計入餘額。
- 沒有「待審佇列」這種原生概念。beancount 沒有 staging 區。
- fava 的 filter 語法支援按 flag 篩選（fava 的 `filters.md` 說明進階 filter 語法），所以「列出所有未確認交易」在 fava 中是一個 filter 查詢。fava 的 journal 也以不同顏色呈現不同 flag（`flag_to_type` template filter）。
- fava 的 `deserialise` 接受 `flag` 欄位，所以透過 `add_entries` 也能寫 `!`。

**建議的分級**：

| 層級 | 做法 | 原生支援 |
|---|---|---|
| 最輕 | Agent 寫入的交易一律 `flag = "!"`，人類在 fava 中檢視後改成 `*` | beancount 語意原生；fava 可篩選；**但方案 (d) 下 fava 唯讀，改 flag 要走 API** |
| 中 | 寫進一個獨立的 `staging.beancount`，主檔預設不 include；核准時由 API 把 entry 搬到正式檔 | 完全自建。beancount 的 `include` 是靜態的，沒有「條件 include」 |
| 重 | 不寫 ledger，寫入 API 自己的待審資料庫，核准才落盤 | 完全自建。這是唯一能讓「未核准的內容完全不影響帳本」的做法 |

**建議用「最輕 + API 提供核准端點」**：`!` 是官方語意，不需要額外機制；未核准的交易仍在帳本中（餘額會變動），但這對個人記帳通常是想要的 —— 花費已經發生了，只是分類還沒確認。

### 6.3 冪等性

**目標**：Agent 重試時不重複記帳。

**beancount 可用的載體**：

| 載體 | 語法 | 字元限制 | 查詢成本 |
|---|---|---|---|
| metadata | `key: "value"` | key 為 `[a-z][a-zA-Z0-9\-_]+`（至少 2 字元）；value 可為 String / Account / Currency / Date / Tag / Number / Amount | 要掃描所有 entry 的 meta |
| tag | `#tag` | `[A-Za-z0-9\-_/.]+` | fava 的 `ledger_data` 直接回傳所有 tag |
| link | `^link` | `[A-Za-z0-9\-_/.]+` | fava 的 `ledger_data` 直接回傳所有 link |

字元類來源：<https://github.com/beancount/beancount/blob/master/beancount/parser/lexer.l>（`#[A-Za-z0-9\-_/.]+`、`\^[A-Za-z0-9\-_/.]+`、`[a-z][a-zA-Z0-9\-_]+/:`）

metadata 的 key 規則與 value 型別來源：<https://beancount.github.io/docs/beancount_language_syntax/>

> Keys must begin with a lowercase character from a-z and may contain (uppercase or lowercase) letters, numbers, dashes and underscores.

> the values can be any of the following data types: Strings, Accounts, Currency, Dates (datetime.date), Tags, Numbers (Decimal), Amount

**link 的官方語意**：

> Transactions can also be linked together. You may think of the link as a special kind of tag that can be used to group together a set of financially related transactions over time.

**建議：用 link 當 idempotency key。**

理由：

1. UUID（hex + 連字號）完全符合 link 的字元類，不需要編碼。
2. fava 的 `get_ledger_data` 回傳 `ledger.attributes.links`，是一個去重後的 link 清單 —— 查「這個 key 是否已寫入」是 O(1) 的集合查詢，不必掃 entry。自建 API 用 `beancount.core.getters` 也能取得同樣的集合。
3. 語意上「把相關交易連起來」與「標記一次 agent 操作」相符。
4. 缺點：link 會出現在 fava UI 上，視覺上有噪音。若介意，改用 metadata（`idempotency-key: "..."`）並自行維護索引。

**beancount 原生的重複偵測**：有 `beancount.plugins.noduplicates` plugin：

```python
def validate_no_duplicates(entries, unused_options_map):
    unused_hashes, errors = compare.hash_entries(entries, exclude_meta=True)
    return entries, errors
```

來源：<https://github.com/beancount/beancount/blob/master/beancount/plugins/noduplicates.py>

它以**整個 entry 的內容 hash**（排除 meta）判斷重複。這偵測得到「完全相同的兩筆交易」，但偵測不到「同一次操作重試但金額四捨五入不同」。而且真實帳本中可能有合法的重複交易（同一天買兩杯一樣的咖啡），啟用這個 plugin 會產生偽陽性。**不建議用它做冪等性**，但可作為 dry-run 時的警告來源。

**建議的冪等協定**：

1. Agent 產生 idempotency key（UUIDv4 或 v7），放在 `links` 中，例如 `^ik-0198f3a2-...`。
2. API 在寫入前於已載入的 ledger 中查該 link 是否存在。存在 → 回傳既有交易，HTTP 200，不寫入。
3. 這個查詢與寫入必須在同一個鎖區間內，否則兩個並行的同 key 請求可能都通過檢查。
4. Key 的存活期等於帳本的存活期（link 永遠在檔案裡），所以不需要過期機制。

### 6.4 權限範圍

**fava 提供的粒度**：只有兩檔 —— `--read-only`（全部非 GET 回 401）或全開。**沒有「只能 append」這種中間層級。**

**自建 API 可以提供的層級**（全部自建，beancount/fava 無原生支援）：

| 層級 | 實作 |
|---|---|
| 只能 append 新 entry | API 不提供修改/刪除端點；寫入一律走「讀現有檔案 → 在指定位置插入 → 換檔」，不允許改動既有行 |
| 只能寫特定檔案 | 限制目標檔案為 `default-file` 或一個專供 agent 的 include 檔（例如 `agent-inbox.beancount`） |
| 只能寫特定帳戶子樹 | 檢查 posting 的 account 是否在允許的 prefix 清單內 |
| 金額上限 | 檢查單筆與當日累計金額 |
| 只能寫 `!` flag | 拒絕 `flag = "*"` 的寫入請求，核准改由另一個端點（另一組憑證）執行 |

**最能落實「只能 append」的做法**：給 agent 一個**專屬的 include 檔**。

```beancount
; main.beancount
include "agent-inbox.beancount"
2010-01-01 custom "fava-option" "insert-entry" "..." ; 不指向 agent-inbox
```

API 對 agent 的寫入一律附加到 `agent-inbox.beancount` 的末尾。這讓：

- 「append-only」成為檔案層級的性質（永遠只在末尾加，從不改前面的行）。
- git diff 永遠只有新增行，稽核容易。
- 人類核准後可以把 entry 搬到正式檔（由 API 的核准端點做）。
- 萬一 agent 寫壞，刪掉這個檔案就回到乾淨狀態。

**注意**：`insert-entry` 選項是給 fava 用的，自建 API 不必沿用。但若希望兩邊行為一致（例如未來改回讓 fava 也能寫），應讀取同一組選項（`fava.core.fava_options.parse_options`）。

### 6.5 dry-run 驗證 endpoint

**原生支援**：`bean-check` 就是一個 dry-run 工具，3.2.3 起有 `--json` 輸出結構化錯誤。但它驗證的是磁碟上的檔案，不是「假設插入這筆之後」。

**建議的 dry-run 端點行為**：

1. 接受與正式寫入相同的 request body。
2. 在 tmpfs 上的 ledger 副本中插入該 entry。
3. `beancount.loader.load_file(副本主檔, extra_validations=validation.HARDCORE_VALIDATIONS)`。
4. 回傳：新增的 errors（相對於未插入時的 baseline errors）、格式化後的 entry 文字、會寫入哪個檔案的哪一行、idempotency key 是否已存在。
5. **不做任何寫入、不做 git commit。**

回傳「格式化後的 entry 文字」很重要 —— 讓 agent（與人類）看到實際會落盤的內容，而不是 JSON。

**baseline errors 的必要性**：真實帳本常年帶著幾個既有 error（例如未來日期的 balance assertion）。dry-run 若只回傳「有 N 個 error」會永遠是紅的。必須做差集。

---

## 7. 研究問題 A6：給 AI Agent 的介面形式（REST vs MCP）

### 7.1 MCP 規格現況（查詢日期 2026-09-21）

**規格版本**：官方 repo `docs/specification/` 下有 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`、`2026-07-28`、`draft`。**最新正式版是 `2026-07-28`**。
來源：<https://github.com/modelcontextprotocol/modelcontextprotocol/tree/main/docs/specification>

**2026-07-28 的重大變更**（直接影響 server 實作成本，逐條引自官方 changelog）：

1. 「Remove protocol-level sessions and the `Mcp-Session-Id` header from the Streamable HTTP transport.」—— **不再需要 session 管理**。需要跨呼叫狀態的 server 改用「server-minted handles passed as ordinary tool arguments」。
2. 「Make MCP stateless: remove the `initialize`/`notifications/initialized` handshake. Every request now carries its protocol version and client capabilities in `_meta`.」—— **沒有握手**。
3. 「Add `server/discover`: servers **MUST** implement this RPC to advertise their supported protocol versions, capabilities, and identity.」
4. 「Replace the HTTP GET endpoint and `resources/subscribe`/`resources/unsubscribe` with `subscriptions/listen`」。
5. 「Remove SSE stream resumability and message redelivery (the `Last-Event-ID` header and SSE event IDs) from the Streamable HTTP transport.」
6. 「Require standard MCP request headers (`Mcp-Method`, `Mcp-Name`) on Streamable HTTP POST requests」。
7. 棄用：「Deprecate the Roots, Sampling, and Logging features」。

來源：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx>

**對本專案的意義**：一個無狀態的 Streamable HTTP MCP server 現在在架構上幾乎等同一個 REST 端點 —— 單一 POST URL，每個請求自包含。**沒有長連線需求，Cloudflare Tunnel 不需要任何特殊設定。**（除非用 `subscriptions/listen`，本專案不需要。）

**授權規格**（來源：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/authorization/index.mdx>）：

- 「Authorization is **OPTIONAL** for MCP implementations.」
- HTTP transport：「**SHOULD** conform to this specification」。
- STDIO transport：「**SHOULD NOT** follow this specification, and instead retrieve credentials from the environment.」
- 基於 OAuth 2.1 IETF DRAFT（`draft-ietf-oauth-v2-1-13`）、RFC 6750、RFC 8414、RFC 7591、RFC 8707、RFC 9728、RFC 9207。
- 「MCP servers **MUST** implement OAuth 2.0 Protected Resource Metadata (RFC9728).」
- 「Authorization servers **MUST** implement OAuth 2.1 with appropriate security measures for both confidential and public clients.」
- Dynamic Client Registration (RFC7591) 現已「deprecated and retained for backwards compatibility」，優先用 Client ID Metadata Documents。

**這是本專案的一個重要決策點**：要做一個**符合規格的、有授權的 HTTP MCP server**，就等於要架一個 OAuth 2.1 authorization server 並實作 RFC 9728 metadata endpoint。這遠超出「一個 container 跑 fava + 一個 API」的規模。

**兩條務實路徑**：

- **stdio transport**：規格明說 stdio **不該**走這套授權，改從環境取憑證。若 AI Agent 與 ledger 在同一台機器（例如 Claude Code 在本機跑），stdio MCP server 是最簡單且規格允許的做法。
- **HTTP transport + 外部授權層**：授權是 OPTIONAL。把 MCP server 放在 Cloudflare Access 後面，用 service token 認證，MCP server 本身不實作 OAuth。這偏離規格的 SHOULD，但不違反 MUST（因為整段授權是 OPTIONAL）。要在文件中明說這是刻意的偏離。

### 7.2 MCP 官方 Python SDK 現況

- PyPI 套件 `mcp`，**最新版 2.2.0**，上傳 **2026-09-07**。`requires-python >=3.10`。
  來源：<https://pypi.org/pypi/mcp/json>
- GitHub release `v2.2.0`，發佈於 2026-09-07；repo 最後推送 2026-09-19。
  來源：<https://github.com/modelcontextprotocol/python-sdk/releases/latest>
- README 自述：「**This is v2 of the MCP Python SDK, the current stable release line.** It is a major rework of the SDK, both to support the [2026-07-28 MCP specification](https://modelcontextprotocol.io/specification/2026-07-28) (and every earlier revision) and to fix long-standing architectural issues.」
- 「Speak every standard transport: stdio, Streamable HTTP, and SSE」。
- 啟動 Streamable HTTP：`uv run mcp run server.py --transport streamable-http`。

來源：<https://github.com/modelcontextprotocol/python-sdk/blob/main/README.md>

**對 image 的影響**：`mcp>=2.2.0` 要求 Python `>=3.10`，與 fava 的 `>=3.10` 相容。既有研究建議的 Python 3.13 可用。SDK 是純 Python（推論；未逐一檢查 wheel 標籤）。

### 7.3 既有的 beancount / fava MCP server（全部非官方，僅供參考）

透過 GitHub search API `search/repositories?q=beancount+mcp` 查得（2026-09-21），按相關性列出：

| Repo | Stars | 最後推送 | 支援 v3 | 可寫入 | 觀察 |
|---|---|---|---|---|---|
| `StdioA/beancount-mcp` | 11 | 2025-05-08 | 未確認 | **是** | README：「can execute beancount query, and submit transaction to the ledger」。`uvx beancount-mcp [--transport=stdio/sse] your_ledger.bean`。仍提供 SSE transport，代表尚未跟上 2026-07-28。約 16 個月未更新 |
| `vanto/beanquery-mcp` | 53 | 2025-04-01 | 用 beanquery | 否 | 只有 `set_ledger_file` 與 `run_query` 兩個 tool。README 自稱 experimental。附有明確的隱私警告：ledger 內容可能被送到第三方 LLM 服務 |
| `klinikal/beanie-mcp` | 3 | 2026-07-04 | **是**（README 明寫 v3） | 否（read-only） | 功能最完整的唯讀實作：10 個 tool、5 個 resource，含 `bean_check`、`net_worth`、`holdings`、`find_unmatched_transfers`。BQL 查詢有 200 列上限與 offset 分頁。**這份的 tool 設計值得作為本專案唯讀端點的參考** |
| `mekanics/mcp-beancount` | 1 | 2026-08-10 | 未確認 | 否（明寫 read-only） | 有 `ACCOUNT_ALLOWLIST` 環境變數做帳戶白名單，是一個值得借鏡的權限設計 |
| `cookie223/beancount-fava-mcp` | 0 | 2025-12-18 | 未確認 | **是（WIP）** | **架構與本專案的選項 (a) 相同**：不直接讀 beancount 檔，而是呼叫一個 fava 實例的 API。有 `add_transaction(...)`，但 README 的 Future Plans 仍寫「add_transaction added but still WIP」。支援 `FAVA_USERNAME`/`FAVA_PASSWORD` 的簡易認證（推測是 HTTP Basic，由 fava 前面的反向代理提供） |
| `CPUtester5465/countbean-plugin` | 37 | 2026-08-26 | 未確認 | 未確認 | 是一個 Claude Code plugin，從 countbean monorepo 發佈，非 MCP server 本身。未細查 |
| `barcia/beancount-mcp`、`wangyw15/beancount-mcp`、`onesvat/mcp-beancount`、`toof-jp/beancount-mcp-server` 等 | 0–1 | 2025-10 至 2026-07 | — | — | 星數極低，未細查 |

來源：GitHub search API 與各 repo 的 `/readme`（2026-09-21）。

**綜合觀察**：

1. **沒有任何一個是官方專案。** beancount 與 fava 的 GitHub org 底下都沒有 MCP server。
2. **唯讀的實作成熟度明顯高於可寫入的。** 兩個宣稱可寫入的（`StdioA/beancount-mcp`、`cookie223/beancount-fava-mcp`）都超過 9 個月沒更新，其中一個自承 WIP。
3. **沒有任何一個做了本文第 6 節的安全措施**（冪等性、dry-run、git 稽核、待審佇列）。這是本專案的差異化價值所在。
4. **沒有任何一個跟上 2026-07-28 規格**（最新的 `klinikal/beanie-mcp` 推送於 2026-07-04，早於規格發佈）。

### 7.4 REST/OpenAPI 與 MCP 的比較

| 面向 | REST + OpenAPI | MCP |
|---|---|---|
| 消費者 | 任何 HTTP 客戶端；LLM 需要中介層把 OpenAPI 轉成 tool definition | MCP client（Claude Desktop、Claude Code、其他 MCP host）直接消費 |
| Tool 描述 | OpenAPI schema，需轉譯 | `tools/list` 原生回傳 JSON Schema 2020-12（2026-07-28 放寬為「any JSON Schema 2020-12 keywords」） |
| 認證 | 自由（Bearer token、mTLS、Cloudflare Access header） | 規格建議 OAuth 2.1 + RFC 9728（HTTP transport）；stdio 用環境變數 |
| 本機使用 | 需要跑 HTTP server | stdio 可直接由 client 啟動子程序，零網路曝露 |
| 公網使用 | 成熟 | Streamable HTTP；2026-07-28 起無狀態，部署變簡單 |
| 人類可用性 | curl、瀏覽器、Postman 都能用 | 需要 MCP client |
| 穩定性 | HTTP/OpenAPI 極穩定 | 規格 18 個月內改了 5 個版本，2026-07-28 是破壞性重構 |

### 7.5 建議：兩者並存，共用同一個寫入核心

**架構**：

```
┌──────────────────────────────────────────┐
│  ledger-writer（Python package）          │
│  - 驗證（load_string / load_file）        │
│  - 原子換檔（os.replace + fsync）          │
│  - 跨 process 鎖（flock）                  │
│  - 冪等檢查（link 集合）                    │
│  - git commit                             │
└────────────┬──────────────┬───────────────┘
             │              │
     ┌───────▼──────┐  ┌────▼─────────┐
     │ REST/OpenAPI │  │ MCP server   │
     │ (FastAPI)    │  │ (mcp>=2.2.0) │
     └──────────────┘  └──────────────┘
```

**理由**：

1. **寫入的正確性只實作一次。** 兩個介面是薄殼。這也讓「AI Agent 透過 MCP 寫」與「未來的手機 App 透過 REST 寫」共用同一組不變量。
2. **REST 先做。** 它是 MCP server 自己的測試工具，也是人類的除錯介面。MCP 規格還在快速變動（7.1），把唯一介面綁在上面有風險。
3. **MCP 用 stdio 起步。** 規格明說 stdio 不該走 OAuth，從環境取憑證即可。若 agent 跑在同一台主機（Claude Code、Claude Desktop），stdio 完全夠用，而且**零網路曝露** —— 這對財務資料是最好的性質。
4. **MCP over Streamable HTTP 只在「agent 不在本機」時才需要。** 屆時放在 Cloudflare Access 後面用 service token。

**對 image / container 架構的影響**：

| 介面 | 部署方式 | 對 image 的要求 |
|---|---|---|
| REST | 長駐 container，`CMD` 指向 uvicorn/gunicorn | image 需含 FastAPI + uvicorn |
| MCP (stdio) | **不是長駐服務**。由 MCP client 以 `docker run -i --rm` 啟動，或直接在主機上 `uvx` | image 需含 `mcp`；entrypoint 要能切到 stdio 模式；**不能對 stdout 寫任何非協定輸出**（log 必須走 stderr） |
| MCP (Streamable HTTP) | 第三個長駐 container，或與 REST 同一個 process 掛在不同路徑 | 同 REST |

**stdio 模式的一個具體陷阱**：既有研究 7.1 建議加 tini 處理 SIGTERM。stdio MCP server 以 `docker run -i` 啟動時，協定走 stdin/stdout，tini 不影響協定，但 log 一定要導到 stderr，否則會污染 JSON-RPC 訊息流。（推論；`mcp` SDK 的預設行為未查證）

**不建議一開始就做的**：完整的 OAuth 2.1 authorization server。規格允許 authorization 為 OPTIONAL，先用 Cloudflare Access 的 service token 做網路層認證，等真的有第三方 MCP client 需要標準流程時再補。

---

## 8. 研究問題 B1：cloudflared 的容器部署

### 8.1 官方 image

- image 名稱 **`cloudflare/cloudflared`**，官方文件只列 **Docker Hub**：「A Docker image of `cloudflared` is available on DockerHub」。文件與 GitHub README 都未提及 GHCR。
  來源：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/>、<https://github.com/cloudflare/cloudflared>、<https://hub.docker.com/r/cloudflare/cloudflared>
- 官方範例一律用 `:latest`：`docker run cloudflare/cloudflared:latest tunnel --no-autoupdate run --token <TUNNEL_TOKEN>`。
  來源：<https://developers.cloudflare.com/tunnel/setup/>
- Docker Hub 上同時有版本 tag（2026-09-21 觀察到 `2026.9.1`、`2026.9.1-arm64`、`latest-arm64`）。
- 支援政策：Cloudflare 只支援距最新版一年內的 `cloudflared`。**官方沒有給 tag 釘選建議。**
  來源：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/#deprecated-releases>

**對本專案**：把 `cloudflare/cloudflared` 交給 Renovate 的 `docker-compose` 或 `dockerfile` manager，釘版本 tag + digest，與既有研究的 base image 策略一致。

### 8.2 remotely-managed 與 locally-managed

官方明確建議 remotely-managed：

> Cloudflare recommends creating a remotely-managed tunnel for most use cases. Remotely-managed tunnels store their configuration on Cloudflare, which allows you to manage the tunnel from any machine using the dashboard, API, or Terraform.

> Locally-managed tunnels are intended for specific scenarios such as local development, testing, or legacy configurations.

來源：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/>

| 面向 | remotely-managed | locally-managed |
|---|---|---|
| 設定存放 | Cloudflare | 本地 `config.yml` + credentials file |
| 執行所需 | 只要 token。「A remotely-managed tunnel only requires a token to run. Anyone with the token can run the tunnel.」 | credentials JSON（等同該 tunnel 的 token）；`cert.pem` 只在建立/刪除 tunnel 與改 DNS 時需要 |
| 參數 | `--token` / `TUNNEL_TOKEN`；2025.4.0 起有 `--token-file` / `TUNNEL_TOKEN_FILE` | `--config` |
| ingress 規則 | dashboard 的 Public Hostname（Service URL 欄） | `config.yml` 的 `ingress` 陣列 |
| 版本控制 | 設定不在 git 內 | 設定可進 git |

來源：<https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/>、<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/#token>、<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/local-tunnel-terms/>

**token 可輪替**：輪替後舊 token 無法建立新連線，既有 connector 直到重啟前仍有效。

**本專案的取捨**：`--token-file` 讓 token 可用 Docker secret 提供，不必進環境變數。但 ingress 規則（尤其是 B4 的 path 路由）在 remotely-managed 下存在 Cloudflare 而非 git 裡。若希望「路徑規劃可 code review、可回溯」，locally-managed 較合適；若希望設定簡單，remotely-managed 較合適。**建議 remotely-managed**，理由是本專案的 ingress 規則極少（一到兩條），放在 dashboard 的維護成本低於維護第二份設定檔。

### 8.3 docker compose 拓樸

**官方沒有 Docker 或 docker-compose 部署指南。** deployment guides 只有 Ansible、AWS、Azure、GCP、Kubernetes、Terraform。
來源：<https://developers.cloudflare.com/tunnel/guides/>

最接近的官方範例是 Kubernetes guide：cloudflared 以獨立 deployment 部署在應用旁，透過叢集內部 DNS 名稱連到應用服務（範例為 `httpbin-service:80`），應用不對外曝露。官方說明「Each `cloudflared` replica / pod can reach all Kubernetes services in the cluster.」
來源：<https://developers.cloudflare.com/tunnel/guides/kubernetes/>

ingress 的 `service` 欄位接受主機名稱加連接埠，官方 HTTP 型別範例為 `http://localhost:8000`、`http://192.0.2.1:80`。寫成 compose 服務名稱 `http://fava:5000` 是同一語法形式，但**官方文件沒有直接出現 container 名稱的範例**（推論：DNS 解析由 compose 的內建 DNS 提供，與 Kubernetes guide 的 `httpbin-service:80` 同構）。
來源：<https://developers.cloudflare.com/tunnel/concepts/routing/#supported-protocols>

**防火牆需求**：出站 `7844` TCP/UDP 到 `region1.v2.argotunnel.com` / `region2.v2.argotunnel.com`。443 為選用（自動更新；以及啟用 originRequest 的 `access` 驗證時，需連 `<team>.cloudflareaccess.com`）。
來源：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/>

**特殊權限**：官方 Kubernetes manifest 中 cloudflared 容器沒有 privileged 設定。唯一的 `securityContext` 是 pod 層的 `sysctls: net.ipv4.ping_group_range = "65532 65532"`，註解說明用途是「Allows ICMP traffic (ping, traceroute) to resources behind cloudflared」。**純 HTTP 代理情境不需要任何特殊權限。**

### 8.4 healthcheck 與 metrics

- 官方 Kubernetes 範例用 `/ready` 作 livenessProbe，manifest 註解：「Cloudflared has a /ready endpoint which returns 200 if and only if it has an active connection to Cloudflare's network.」設定為 `path: /ready, port: 2000, failureThreshold: 1, initialDelaySeconds: 10, periodSeconds: 10`，搭配 `--metrics 0.0.0.0:2000`。
- metrics server 預設位址：非容器環境 `127.0.0.1:<PORT>`，**容器環境（Docker、Kubernetes）預設 `0.0.0.0:<PORT>`**，PORT 取 `20241`–`20245` 第一個可用者。Prometheus 格式，路徑 `/metrics`。
  來源：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/monitor-tunnels/metrics/>
- `cloudflared` 原始碼另有 `/healthcheck` 路由（同一個 metrics server）。**官方文件未說明此路由**，只在原始碼可見。
  來源：<https://github.com/cloudflare/cloudflared/blob/master/metrics/metrics.go>

### 8.5 Replica 與高可用

- 同一 tunnel 最多 **100 條連線（25 個 replica）**。以系統服務執行時每台主機只允許一個 cloudflared 實例。
  來源：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-availability/deploy-replicas/>
- 官方警告：「`cloudflared` does not load balance across replicas; replicas are strictly for high availability.」不建議搭配 autoscaling。切換 replica 時長連線（WebSocket）與 TCP 連線會被丟棄。

**對本專案**：單人記帳系統不需要 replica。一個 cloudflared container 即可。

### 8.6 originRequest 選項

官方表格（含預設值），來源：<https://developers.cloudflare.com/tunnel/reference/origin-parameters/>

| 類別 | 選項 | 預設 | 與本專案（HTTP origin）相關 |
|---|---|---|---|
| TLS | `originServerName` | `""` | 否 |
| TLS | `matchSNItoHost` | `false` | 否 |
| TLS | `caPool` | `""` | 否 |
| TLS | `noTLSVerify` | `false` | 否 |
| TLS | `tlsTimeout` | `10s` | 否 |
| TLS | `http2Origin` | `false` | 否 |
| HTTP | `httpHostHeader` | `""` | **可能**，見 10.4 |
| HTTP | `disableChunkedEncoding` | `false` | 否 |
| 連線 | `connectTimeout` | `30s` | 是 |
| 連線 | `keepAliveTimeout` | `1m30s` | 是 |
| 連線 | `keepAliveConnections` | `100` | 否 |
| 連線 | `tcpKeepAlive` | `30s` | 否 |
| 連線 | `noHappyEyeballs` | `false` | 否 |
| Access | `access` | — | **是**，見 9.5 |

origin 是純 HTTP（`http://fava:5000`）時所有 TLS 類選項都不適用。

---

## 9. 研究問題 B2：認證（Cloudflare Access / Zero Trust）

### 9.1 保護 self-hosted application 的官方步驟

1. Zero Trust > Access controls > Applications > **Create new application** > **Self-hosted and private** > **Add public hostname**。
2. 加入 Access policy。官方原文：「All Access applications are deny by default — a user must match an Allow policy before they are granted access.」
3. 設定 identity provider；單一 IdP 時建議開 **Apply instant authentication**。
4. 設定 **Session Duration**（application token 有效期）。
5. Additional settings 可設 App Launcher、自訂 block page、CORS、Cookie，以及 **「401 Response for Service Auth policies」**（缺少正確 service token 時回 401 而非導向登入頁）。
6. 建立 Cloudflare Tunnel 連接 origin。
7. **驗證 Access token**。

來源：<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/>

**同頁的關鍵順序警告**：

> We recommend creating an Access application before setting up the tunnel route. If you do not have an Access application in place, the published application will be available to anyone on the Internet.

**對本專案是硬性要求**：fava 沒有任何認證（見第 3 節與既有研究 6.1）。Tunnel route 一旦建立而 Access application 還沒設定，整個帳本就在公網上裸奔。**必須先建 Access application，再建 route。**

### 9.2 免費方案的使用者數

- `developers.cloudflare.com` 的 Account limits 頁**沒有 seat 上限的數字**。只列 Access applications 500、Service tokens 50、Reusable policies 500、Rules per rule group 1,000。
  來源：<https://developers.cloudflare.com/cloudflare-one/account-limits/>
- Seat management 頁只說 seat 數取決於購買量，用盡時新使用者登入會被阻擋。未給免費方案數字。
  來源：<https://developers.cloudflare.com/cloudflare-one/team-and-resources/users/seat-management/>
- 官方 Access 產品頁的定價區塊寫：「Free Plan — $0 forever — **Best for teams under 50 users** or enterprise proof-of-concept tests.」下一級為 Pay-as-you-go `$7 per user/month`。
  來源：<https://www.cloudflare.com/zero-trust/products/access/>

**結論**：官方以「適合 50 人以下」陳述，**是否為硬性上限在官方文件中未明確說明，標示為未知**。個人單人使用遠低於此門檻，實務上不是問題。

### 9.3 Identity provider 選項

- 建立 Zero Trust 組織時，Cloudflare **自動加入 Cloudflare identity provider** 作為預設登入方式，使用者可直接用 Cloudflare 帳號憑證登入。
  來源：<https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/cloudflare/>
- **One-time PIN**：不需外部 IdP。「You can also send a one-time PIN (OTP) to approved email addresses. No configuration needed — simply add a user's email address to an Access policy...」
  來源：<https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/>
- 其他：Entra ID、Google、Google Workspace、GitHub、Okta、Keycloak、AWS Cognito、ADFS、JumpCloud、Facebook、Generic OIDC、Generic SAML。官方建議 OIDC 優於 SAML。
  來源：<https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/>

**對本專案**：單人使用時 One-time PIN 或 Cloudflare identity provider 就夠，不需要接外部 IdP。

### 9.4 Service token

來源：<https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/>、<https://developers.cloudflare.com/cloudflare-one/access-controls/policies/>

- 建立後取得 **Client ID** 與 **Client Secret**（Secret 只顯示一次）。
- 請求帶兩個 header：`CF-Access-Client-Id: <CLIENT_ID>`、`CF-Access-Client-Secret: <CLIENT_SECRET>`。
- 用戶端只支援單一 header 時，可設定應用接受 `Authorization` header 內的 JSON 形式。
- **建立時必須指定有效期**，從數小時到數年（例如 `8760h` 一年）。
- 自 2026-08-26 起新的 Client Secret 格式為 `cfast_` + 40 字元 + 8 字元校驗碼，便於憑證掃描工具偵測。
- 生命週期操作：**Rotate**（保留 Client ID、更換 Secret，可設 1 小時至 30 天寬限期，期間新舊皆有效）、**Renew**（延長）、**Disable / Delete**（撤銷）。可設定到期前一週通知。
- 在政策中允許：policy action 設為 **Service Auth**（不要求 IdP 登入），selector 用 **Service Token**（指定的 token）或 **Any Access Service Token**（帳號內任一 token）。
- 帳號上限 **50 個** service token。

**官方對 AI agent 的建議**：Cloudflare 有專門頁面 `Authenticate agents`，列三種方式：

1. cloudflared 以使用者身分驗證（互動式，首次請求觸發瀏覽器登入）。
2. **Service tokens**，用於 headless / 自動化流程，不需瀏覽器互動。
3. OAuth 2.0 + PKCE（RFC 9728 resource metadata、dynamic client registration、authorization code with PKCE）。

來源：<https://developers.cloudflare.com/cloudflare-one/access-controls/authenticate-agents/>

**注意第三項與 MCP 2026-07-28 授權規格（7.1）用的是同一組 RFC**。若未來要做符合規格的 HTTP MCP server，Cloudflare 這條路徑是現成的 —— 但這需要進一步查證 Cloudflare 的 OAuth 實作能否直接充當 MCP 規格要求的 authorization server。**未查證，標示為未知。**

### 9.5 origin 端驗證 `Cf-Access-Jwt-Assertion`

來源：<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/>、<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/>

**token 在哪**：

> When Cloudflare sends a request to your origin, the request will include an application token as a `Cf-Access-Jwt-Assertion` request header. Requests made through a browser will also pass the token as a `CF_Authorization` cookie.

> We recommend validating the `Cf-Access-Jwt-Assertion` header instead of the `CF_Authorization` cookie, since the cookie is not guaranteed to be passed.

**公鑰端點**：`https://<your-team-name>.cloudflareaccess.com/cdn-cgi/access/certs`，回傳 `keys`（JWK，含當前與前一把）、`public_cert`（PEM，當前）、`public_certs`（PEM，兩把）。

**輪替**：金鑰每 **6 週**輪替一次，舊金鑰在輪替後 **7 天**內仍有效。

**官方兩點警告**：

1. 不要把公鑰寫死，要從端點取得。
2. 不要用 `public_cert`（可能讀到過期快取），要用 JWT 的 `kid` 去 `public_certs` 比對。

**要驗的 claim**：

| claim | 驗什麼 |
|---|---|
| `aud` | 應用的 **Application Audience (AUD) Tag**，從 dashboard > 應用 > Additional settings 取得。除非刪除重建，否則不變 |
| `iss` | 必須是 `https://<your-team-name>.cloudflareaccess.com` |
| `exp` | 未過期 |

payload 另有 `iat`、`nbf`、`sub`、`email`、`type`、`identity_nonce`、`country`。

**官方範例程式**：Cloudflare Workers（jose）、Go（go-oidc）、**Python（PyJWT）**、Node.js / Express（jose）。本專案的 API 是 Python，官方有直接可用的範例。

**其他**：`https://<team>.cloudflareaccess.com/cdn-cgi/access/get-identity` 可取得完整身分（含 groups）。官方警告 JWT 內的 custom claim 序列化超過約 1 KB 時會被裁切，**不要用來做授權決策**。service token 認證產生的 JWT payload 內容與 identity-based 不同。

### 9.6 不驗證 JWT 的風險

**官方兩段文字存在張力，兩段都引在這裡。**

self-hosted-public-app 頁：

> To secure your origin, you must validate the application token issued by Cloudflare Access. **Token validation ensures that any requests which bypass Cloudflare Access (for example, due to a network misconfiguration) are rejected.**

來源：<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/>

application-token 頁：

> **Unless your application is connected to Access through Cloudflare Tunnel**, your application must validate the token to ensure the security of your origin. Validation of the header alone is not sufficient — the JWT and signature must be confirmed to avoid identity spoofing.

來源：<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/>

後者暗示走 Tunnel 就可免驗；前者仍要求驗證。**官方沒有明文說「走 Tunnel 就絕對無法繞過」。**

**Tunnel 隔離性質的官方陳述**：

> `cloudflared` initiates an outbound connection through your firewall from the origin to the Cloudflare global network... You can then configure your firewall to allow only these outbound connections and block all inbound traffic, effectively blocking access to your origin from anything other than **Cloudflare**.

注意用詞是「anything other than Cloudflare」，不是「anything other than this tunnel」。

> Because Cloudflare Tunnel does not use an inbound listener on your origin, Authenticated Origin Pulls has no effect on hostnames routed through Cloudflare Tunnel.

來源：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/>

> The `cfargotunnel.com` subdomain only proxies traffic for DNS records in the same Cloudflare account. If someone discovers your tunnel UUID, they cannot create a DNS record in another account to proxy traffic through it.

來源：<https://developers.cloudflare.com/tunnel/concepts/routing/>

**實際的繞過途徑（在本專案架構下）**：

1. **同一個 Cloudflare 帳號內的設定錯誤**：例如在同一帳號下另建一個指向同一 tunnel 但沒有 Access application 的 DNS 記錄。`cfargotunnel.com` 的限制只擋跨帳號，不擋同帳號。
2. **Docker network 內的其他 container**：cloudflared 與 fava 在同一個 compose network，任何加入該 network 的 container 都能直接打 `http://fava:5000`，完全繞過 Cloudflare。
3. **fava 的 port 被 publish 到 host**：若誤加 `ports: ["5000:5000"]`，主機網路上的任何人都能直連。

**兩種官方支援的緩解**：

- **（a）在 Tunnel 設定開啟 Protect with Access**（即 originRequest 的 `access`，欄位為 `required`、`teamName`、`audTag`）。由 cloudflared 代為驗證 JWT，origin 不必改程式。**這對 fava 是唯一可行的做法**，因為 fava 無法改程式碼去驗 JWT。啟用後防火牆需額外放行 443 到 `<team>.cloudflareaccess.com`。
  來源：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/>、<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/>
- **（b）在 origin 自行驗證**。對自建的 API 服務適用（官方有 Python/PyJWT 範例）。

**帳號層的第三道防線**：**Require Access protection** 設定。開啟後「traffic to any hostname without a matching Access application is automatically blocked」，顯示 Error 1050 Default-Deny。開啟前必須確認所有公開 hostname 都有 Allow 或 Bypass 政策。
來源：<https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/require-access-protection/>

**建議**：三道全開。fava 用 (a)，API 用 (b)，帳號層開 Require Access protection。財務資料值得這個成本。

---

## 10. 研究問題 B3：Fava 在 tunnel / 反向代理後的注意事項

### 10.1 `--prefix` 與 API 路徑（解除既有研究的未知事項 9）

**已驗證，結論：API 路徑會一併帶上 prefix，前端與後端一致。**

證據鏈：

1. `cli.py`：

```python
if prefix:
    from werkzeug.middleware.dispatcher import DispatcherMiddleware
    from fava.util import simple_wsgi
    app.wsgi_app = DispatcherMiddleware(simple_wsgi, {prefix: app.wsgi_app})
```

`simple_wsgi` 是一個回傳 `200 OK` 加空 body 的 WSGI app（`fava/util/__init__.py`），作為 prefix 之外的 fallback。

2. `DispatcherMiddleware.__call__`：

```python
original_script_name = environ.get("SCRIPT_NAME", "")
environ["SCRIPT_NAME"] = original_script_name + script
environ["PATH_INFO"] = path_info
```

來源：<https://github.com/pallets/werkzeug/blob/main/src/werkzeug/middleware/dispatcher.py>

Flask / Werkzeug 的 `url_for` 以 `SCRIPT_NAME` 為 base 產生 URL。

3. `internal_api.py` 的 `get_ledger_data()` 把 `url_for("index")` 當作 `base_url` 回傳給前端。

4. 前端 `frontend/src/api/index.ts` 用這個 base_url 組 API URL：

```javascript
const $base_url = store_get(base_url);
const url = new URL(`${$base_url}api/${endpoint}`, window.location.href);
```

**結論**：`fava --prefix /fava` 之下，

- UI 在 `https://host/fava/<bfile>/...`
- API 在 `https://host/fava/<bfile>/api/<endpoint>`
- prefix 之外的路徑（例如 `https://host/`）由 `simple_wsgi` 回傳空的 200，**不是 404**。這是一個容易誤判的行為：健康檢查若打根路徑會得到 200 卻沒有內容。

**對 Cloudflare 的影響**：cloudflared **不會剝除 path**（見 10.5），所以 `--prefix` 的值必須與 Cloudflare 上的 path 一致。

### 10.2 Cloudflare 的 request body 上限

| Free | Pro | Business | Enterprise |
|---|---|---|---|
| **100 MB** | 100 MB | 200 MB | 可自助至 5 GB |

可在 zone 的 **Network** 頁調整。官方註記：超大上傳可能先撞到連線或讀取逾時而失敗，看起來像與大小無關的錯誤。
來源：<https://developers.cloudflare.com/cache/concepts/default-cache-behavior/#customization-options-and-limits>

**對 fava**：`put_add_document` 與 `put_upload_import_file` 是 multipart 上傳。銀行對帳單 PDF 與 CSV 遠小於 100 MB，不是問題。

### 10.3 Timeout

Cloudflare 與 origin 之間（來源：<https://developers.cloudflare.com/fundamentals/reference/connection-limits/>）：

| 類型 | 秒數 | 觸發狀態碼 | 可調整 |
|---|---|---|---|
| Complete TCP Connection | 19 | 522 | 否 |
| TCP ACK Timeout | 90 | 522 | 否 |
| TCP Keep-Alive Interval | 30 | 520 | 否 |
| Proxy Idle Timeout | 900 | 520 | 否 |
| **Proxy Read Timeout** | **125** | **524** | 僅 Enterprise |
| Proxy Write Timeout | 30 | 524 | 否 |
| HTTP/2 Connection Idle | 900 | — | 否 |

用戶端與 Cloudflare 之間：HTTP/1.1 Keep-Alive 與 HTTP/2 Idle 均為 400 秒。請求 URL 上限 16 KB，請求標頭總計 128 KB，回應標頭總計 128 KB。

**沒有「100 秒」這個數字** —— 常見的說法不準確，實際是 **125 秒的 Proxy Read Timeout**，逾時回 524。
來源：<https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/>

**對本專案**：

- fava 載入大型 ledger 的首次請求若超過 125 秒會回 524。個人帳本不太可能，但**帳本規模的載入時間未測，標示為未知**（與既有研究未知事項 6 相關）。
- 自建 API 的 dry-run（要載入完整 ledger 做驗證）同樣受 125 秒限制。若載入很慢，應改為「啟動時預載並快取」而非每次請求重載。

### 10.4 WebSocket、SSE 與 Host header

**fava 不使用 WebSocket，也不使用 SSE。** 在 `frontend/src/` 下搜尋 `EventSource`、`WebSocket`、`text/event-stream`，**0 筆命中**（實際執行，2026-09-21，`v1.30.16`）。

fava 偵測檔案變更的方式是**前端輪詢**：

```javascript
setInterval(poll_for_changes, 5000);
```

每 5 秒打一次 `GET <base>/api/changed`。
來源：<https://github.com/beancount/fava/blob/v1.30.16/frontend/src/app.ts>

**結論**：Cloudflare Tunnel 不需要任何 WebSocket 或 SSE 設定。但要注意這個輪詢對 rate limiting 的影響（見 12.3）。

**（參考，本專案用不到）** Cloudflare 支援 WebSocket「without additional configuration」且「on all Cloudflare plans」，dashboard 的 Network > WebSockets 有開關；Tunnel 故障排除頁把「WebSockets are not enabled」列為 `websocket: bad handshake` 的成因之一。閒置逾時官方只說「a period of time」，**具體秒數未公開**。
來源：<https://developers.cloudflare.com/network/websockets/>、<https://developers.cloudflare.com/tunnel/troubleshooting/>

**（參考）** SSE 有明確的官方記載：

> Proxied traffic through Cloudflare Tunnel is **buffered by default unless the origin server includes the `Content-Type: text/event-stream` response header**. This header tells `cloudflared` to stream data as it arrives instead of buffering the entire response.

來源：<https://developers.cloudflare.com/tunnel/troubleshooting/#cloudflare-tunnel-is-buffering-my-streaming-response-instead-of-streaming-it-live>

**若未來做 MCP over Streamable HTTP**，其回應串流走的是 `text/event-stream`，這條規則直接適用 —— 不必額外設定，但必須確保回應帶上該 Content-Type。

**Host header**：

- `httpHostHeader`（UI 名稱 **HTTP Host Header**，預設 `""`）：「Sets the HTTP `Host` header on requests sent to the local service.」
- **cloudflared 在未設定時送往 origin 的 Host 值，官方文件未說明 → 未知。**

**fava 對 Host header 的敏感度**：fava 用 Flask 的 `url_for`，預設產生相對 URL（`_external=False`），不依賴 Host。`static_url` 與 `url_for` 都是相對路徑。所以**推論 fava 對 Host header 不敏感**，不需要設 `httpHostHeader`。未實測。

### 10.5 HTTPS 終結在 Cloudflare 時的 URL 產生

**Cloudflare 會送 `X-Forwarded-Proto`**：

> `X-Forwarded-Proto` is used to identify the protocol (HTTP or HTTPS) that a visitor used to connect to Cloudflare. By default, the protocol used is `https`, unless the visitor selected a different encryption mode.

> For incoming requests, the value of this header will be set to the protocol the client used (`http` or `https`). **If the client set a different value, it will be overwritten.**

另有 `CF-Visitor`（JSON，只含 `scheme`，例如 `CF-Visitor: {"scheme":"https"}`）。官方建議取真實用戶端 IP 用 `CF-Connecting-IP` 或 `True-Client-IP`，**不要用** `X-Forwarded-For`。
來源：<https://developers.cloudflare.com/fundamentals/reference/http-headers/>

**fava 是否處理 `X-Forwarded-Proto`**：fava 的 `application.py` **沒有**套用 `werkzeug.middleware.proxy_fix.ProxyFix`（搜尋無命中）。

**這是否造成問題**：不會。因為 fava 產生的都是**相對 URL**（`url_for` 預設 `_external=False`），相對 URL 不含 scheme，瀏覽器會沿用目前頁面的 scheme（https）。所以即使 fava 內部以為自己是 http，產生的連結仍然正確。（**推論**：未逐一檢查每個 `url_for` 呼叫是否都用相對形式，也未實測。）

**唯一需要注意的**：若未來要讓 fava 產生絕對 URL（例如 email 通知、OAuth redirect），就必須加 `ProxyFix`。目前 fava 沒有這類功能。

### 10.6 cloudflared 不改寫 path

> `cloudflared` matches request paths to evaluate rules, but it forwards the full request path to your service without modifying or stripping it. For example, if an ingress rule matches `path: /api`, a request to `https://example.com/api/users` is sent to your service as `http://localhost:8000/api/users`.

若需要剝除前綴，官方建議用 URL Rewrite Rules 在邊緣改寫，或在 cloudflared 後面再放一層本地反向代理。
來源：<https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/>

**對本專案的直接後果**：採「同一 hostname 不同路徑」時，fava 必須用 `--prefix` 對齊 Cloudflare 上的 path。例如 Cloudflare path `/fava` → `fava --prefix /fava`。**不要**期待 cloudflared 幫忙剝掉 `/fava`。

---

## 11. 研究問題 B4：API 與 fava 的公網路徑規劃

### 11.1 Tunnel ingress 支援 path 路由

支援。ingress 規則三個欄位：`hostname`（可用 wildcard 如 `*.example.com`）、`path`（**正規表示式**，Go regexp 語法）、`service`（必填）。

官方範例：

```yaml
ingress:
  - hostname: example.com
    service: https://localhost:8000
  - hostname: static.example.com
    path: \.(jpg|png|css|js)$
    service: https://localhost:8001
  - hostname: "*.example.com"
    service: https://localhost:8002
  - service: https://localhost:8003
```

**含 ingress 規則的設定檔必須以 catch-all 規則結尾**（最後一條不帶 hostname / path，匹配所有流量）。規則未指定 path 時匹配所有 path。
來源：<https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/>

remotely-managed：dashboard 的 Add route 步驟也有 path 欄位。
來源：<https://developers.cloudflare.com/cloudflare-one/networks/routes/add-routes/>

### 11.2 Access application 支援 path 範圍

支援。

> Application paths define the URLs protected by an Access policy. When adding a self-hosted application to Access, you can choose to protect the entire website by entering its apex domain, or alternatively, protect specific subdomains and paths.

- Wildcard：`example.com/*` 涵蓋 apex 與其下所有 path；`example.com/api/*` 涵蓋 `/api/users`；`example.com/foo*/bar` 可匹配 `/foo/bar`、`/food/bar`、`/food/stuff/bar`。
- **優先序**：「When multiple rules are set for a common root path, the more specific rule takes precedence.」所以 `example.com/api` 可套用與 `example.com` 不同的政策。

**限制**：

| 限制 | 說明 |
|---|---|
| 每兩個斜線之間最多一個 wildcard | `example.com/foo*bar*baz` 不允許 |
| **不支援 query string** | `?foo=bar` 無法納入 path 比對 |
| 不支援 anchor | `#` |
| port 會被剝除 | 導向預設 port |

來源：<https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/>

**「不支援 query string」對 fava 的影響**：fava 的 JSON API 中，GET 端點的參數全在 query string（`?entry_hash=...`、`?filename=...`）。所以**無法用 Access policy 區分「讀某個檔案」與「讀另一個檔案」**。但這不是問題 —— 路徑層級（`/<bfile>/api/*`）的粒度已足夠。

### 11.3 Access policy 的 action

來源：<https://developers.cloudflare.com/cloudflare-one/access-controls/policies/>

| Action | 語意 |
|---|---|
| **Allow** | 符合條件者放行，可用 Include / Require / Exclude 細化 |
| **Block** | 拒絕符合條件者。因預設拒絕，Block 主要用於在 Allow 之外挖例外 |
| **Bypass** | 對特定流量**完全停用** Access 強制。官方警告：「Bypass does not enforce any Access security controls and **requests are not logged**.」不支援身分類的規則型別 |
| **Service Auth** | 不要求 IdP 登入即可強制認證，支援 service token 與 mutual TLS |

規則邏輯：Include = OR、Require = AND、Exclude = NOT。

相關 selector：**Service Token**（必須帶該應用設定的特定 token 標頭）、**Any Access Service Token**（接受帳號內任一 token）、Emails、Emails ending in、One-time PIN、Everyone。

**絕對不要用 Bypass**：它不記 log，等於在財務資料前面開一個沒有稽核的洞。

### 11.4 建議的路徑規劃

**建議：API 完全不上公網；只發佈 fava。**

理由：

1. 使用者的三項需求中，只有「fava 是對外的 web UI，透過 Cloudflare Tunnel 發佈到公網」明確要求上公網。**API 沒有這個要求。**
2. AI Agent 若跑在同一主機（Claude Code、Claude Desktop），MCP stdio transport 完全不需要網路（見 7.5）。這是財務資料最安全的形態：**零網路曝露**。
3. 公網上的 API 是可寫入的端點。即使有 Access service token，多一層就多一個失效模式（token 洩漏、policy 設錯、Require Access protection 忘了開）。

**若 agent 確實不在本機，退而求其次的方案（按安全性排序）**：

**方案一：不同 hostname**（建議）

```
fava.example.com  → Access application（IdP 登入，Allow）→ tunnel → fava:5000
api.example.com   → Access application（Service Auth，Service Token）→ tunnel → api:8000
```

優點：

- 兩個 Access application 完全獨立，policy 不會互相干擾。
- API 的 application 可開「401 Response for Service Auth policies」，agent 得到乾淨的 401 而非登入頁的 HTML。
- fava 不必設 `--prefix`。
- path 的優先序規則（11.2）不會意外讓某一邊的 policy 套到另一邊。

**方案二：同一 hostname 不同路徑**

```
example.com/*      → Access application（IdP 登入）→ tunnel → fava:5000  （fava --prefix 不設）
example.com/api/*  → Access application（Service Auth）→ tunnel → api:8000
```

缺點：

- 依賴 11.2 的「more specific rule takes precedence」。設定錯誤時 fail-open 的風險較高。
- cloudflared 不剝 path（10.6），所以 API 服務自己要接受 `/api` 前綴，或在 Cloudflare 加 URL Rewrite Rule。
- fava 若也要放在子路徑下，要加 `--prefix`，多一個設定要對齊。

**兩個方案共通的必備設定**：

1. fava 的 Access application 開 **Protect with Access**（originRequest 的 `access`），由 cloudflared 驗 JWT。因為 fava 無法自己驗。
2. API 的服務自己驗 `Cf-Access-Jwt-Assertion`（Python + PyJWT，官方有範例），驗 `aud`、`iss`、`exp`，用 `kid` 從 `/cdn-cgi/access/certs` 的 `public_certs` 取金鑰。
3. 帳號層開 **Require Access protection**。
4. fava 與 API 的 container **都不要 publish port 到 host**。

---

## 12. 研究問題 B5：財務資料上公網的額外風險與緩解

### 12.1 風險清單

| 風險 | 本專案的具體形態 |
|---|---|
| fava 無任何認證 | 一旦 Access 被繞過或忘了設，整份帳本可被讀取。fava 原始碼中認證相關的字串 0 筆命中（第 3 節） |
| fava 可寫入 | 若不用 `--read-only`，繞過 Access 者可任意改寫帳本。這是建議用 `--read-only` 的主因之一 |
| 帳本內容高度敏感 | 帳戶名稱、收入、消費明細、payee 名稱、醫療支出。`vanto/beanquery-mcp` 的 README 對此有明確警告 |
| 無稽核 log（免費方案） | 逐請求的 Access log 是 Enterprise 功能（12.4）。免費方案只有 24 小時的認證 log |
| Bypass policy 不記錄 | 官方明文（11.3） |
| 同 network 的 container 可繞過 | 見 9.6 |

### 12.2 WAF（免費方案有什麼）

Managed Rules 可用性（來源：<https://developers.cloudflare.com/waf/managed-rules/>）：

| Ruleset | Free | Pro | Business | Enterprise |
|---|---|---|---|---|
| **Free Managed Ruleset** | **是** | 是 | 是 | 是 |
| Cloudflare Managed Ruleset | **否** | 是 | 是 | 是 |
| Cloudflare OWASP Core Ruleset | **否** | 是 | 是 | 是 |
| Sensitive Data Detection | 否 | 否 | 否 | 是 |

**Cloudflare Free Managed Ruleset**：「Available on all Cloudflare plans. Provides protection against high-impact and widely exploited vulnerabilities.」

Custom rules（來源：<https://developers.cloudflare.com/waf/custom-rules/>）：

| 項目 | Free | Pro | Business | Enterprise |
|---|---|---|---|---|
| 規則數 | **5** | 20 | 100 | 1,000 |
| 支援動作 | 除 Log 外全部 | 除 Log 外全部 | 除 Log 外全部 | 全部 |
| **Regex 支援** | **否** | 否 | 是 | 是 |
| zone 自訂 ruleset 數 | 1 | 2 | 5 | 10 |

**對本專案**：免費方案的 5 條 custom rule 夠用（例如「只允許特定國家」、「阻擋已知的掃描 User-Agent」）。但**沒有 regex**，所以無法寫複雜的 path 比對。真正的防線是 Access，不是 WAF。

### 12.3 Rate limiting（免費方案的限制）

| 項目 | Free |
|---|---|
| 規則數 | **1** |
| 表示式可用欄位 | **僅 Path、Verified Bot** |
| 計數特徵 | **僅 IP** |
| 計數週期 | **固定 10 秒** |
| 緩解期間 | **固定 10 秒** |
| 自訂計數表示式 | 否 |
| Cache exclusion | 否 |

官方註記：rate limiting 不保證精確，偵測與計數更新之間可能有數秒延遲，超量請求仍可能到達 origin。
來源：<https://developers.cloudflare.com/waf/rate-limiting-rules/>

**與 fava 輪詢的互動**：fava 前端每 5 秒打一次 `GET .../api/changed`（10.4）。免費方案的計數週期固定 10 秒，所以一個開著的 fava 分頁在 10 秒內至少 2 次請求，加上頁面切換時的其他 API 呼叫。**rate limit 的閾值必須高於這個基線**，否則正常使用會被自己擋掉。開多個分頁時倍增。

**建議**：免費方案的 rate limiting 用處有限（單一規則、只能按 IP、10 秒窗）。對單人使用的 fava，Access 的 deny-by-default 已經是更強的防線。**不建議把有限的一條規則用在 fava 上**；若要用，留給 API 的 hostname。

### 12.4 稽核 log

Zero Trust log 保留期（來源：<https://developers.cloudflare.com/cloudflare-one/insights/logs/#log-retention>）：

| | Free | Standard | Access | Gateway | Enterprise |
|---|---|---|---|---|---|
| Admin logs | 18 個月 | 18 個月 | 18 個月 | 18 個月 | 18 個月 |
| **Access logs** | **24 小時** | 30 天 | 30 天 | 24 小時 | 180 天 |
| DNS logs | 24 小時 | 30 天 | 24 小時 | 30 天 | 180 天 |
| Network logs | 24 小時 | 30 天 | 24 小時 | 30 天 | 30 天 |
| HTTP logs | 24 小時 | 30 天 | 24 小時 | 30 天 | 30 天 |
| Device posture logs | 30 天 | 30 天 | 30 天 | 30 天 | 30 天 |

Access 有兩類稽核 log：

- **Authentication audit logs**：每次登入嘗試（成功或失敗），含 email、IP、user ID、應用名稱/網域/UID、IdP、允許或拒絕、時間戳、Ray ID、國家。位置：Zero Trust > Insights > Logs > Access authentication logs。
- **Per-request audit logs**：已驗證使用者對受保護 path 的個別 HTTP 請求。官方：「**Enterprise customers** have access to detailed logs of requests on their Cloudflare dashboard.」→ **免費方案沒有逐請求稽核。**

非身分類（含 **service token**）的認證事件不在 dashboard，需用 GraphQL Analytics API 查詢。
來源：<https://developers.cloudflare.com/cloudflare-one/insights/logs/dashboard-logs/access-authentication-logs/>

**Logpush**：Zero Trust 的 Logpush 整合標示為 **Enterprise-only**。
來源：<https://developers.cloudflare.com/cloudflare-one/insights/logs/logpush/>

Log Explorer（Beta）可把 Zero Trust log 存進 R2，支援 `access_requests` 等資料集；**方案需求該頁未說明，標示為未知**。
來源：<https://developers.cloudflare.com/cloudflare-one/insights/logs/#log-explorer-beta>

**對本專案的重要結論**：**不要把 Cloudflare 當成稽核來源。** 免費方案只有 24 小時的認證 log，service token 的事件還不在 dashboard 上。真正的稽核要在**自己的層級**做：

1. API 服務自己記 log（誰、什麼時候、寫了什麼、idempotency key、JWT 的 `sub`/`email`）。
2. **git 是最可靠的稽核來源**（6.1）—— 每一次寫入都是一個 commit，永久保存，可離線分析。這比任何雲端 log 保留期都長。

---

## 13. 研究問題 C：發佈到 Docker Hub

### 13.1 C1：GitHub Actions 推送到 Docker Hub 的認證

**`docker/login-action` 的 `registry` 欄位可以不填。** 官方 inputs 表寫 `registry` 型別 String、預設 `docker.io`，說明「Server address of Docker registry. If not set then will default to Docker Hub」。
來源：<https://github.com/docker/login-action#inputs>

**官方 Docker Hub 範例用 PAT，不用密碼**：「use a personal access token. Don't use your account password.」範例為 `username: ${{ vars.DOCKERHUB_USERNAME }}`、`password: ${{ secrets.DOCKERHUB_TOKEN }}`，沒有 `registry` 欄位。注意 **username 用 `vars`（repository variable），token 才用 `secrets`**。
來源：<https://github.com/docker/login-action#docker-hub>

**secrets 命名慣例**：`DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN`。Docker 官方文件多個頁面一致使用這組名稱。
來源：<https://docs.docker.com/build/ci/github-actions/push-multi-registries/>、<https://docs.docker.com/build/ci/github-actions/attestations/>

**PAT 與 OAT**：

| | PAT（personal access token） | OAT（organization access token） |
|---|---|---|
| 權限範圍 | Read、Write、Delete | repository 層級（pull、push、delete、tag 管理、webhook、**immutable tag 設定**）+ organization 層級（列私有 repo、建 repo、讀 registry 用量） |
| 到期日 | 可設定；**建立後不可修改**，必須另建新 token | **必須**設定 |
| 數量 | 自動產生的 token 每帳號最多 5 個 | Team 每組織最多 10 個，Business 最多 100 個 |
| 方案需求 | 所有方案 | **僅 Team 與 Business** |
| rate limit | 佔用個人帳號額度 | 獨立額度，不佔個人帳號 |
| 其他限制 | — | 不能用於 Docker Desktop 與 Image Access Management |

來源：<https://docs.docker.com/security/access-tokens/>、<https://docs.docker.com/security/access-tokens/organization-access-tokens/>

**GitHub OIDC 免密鑰登入：Docker Hub 有支援，但限組織帳號，且限 Team / Business 方案。**

- docker/docs 的 `data/summary.yaml` 記錄 `OIDC connections: subscription: [Team, Business]`。
- 官方文件寫「only organization accounts can sign in using OIDC」。
- **個人免費帳號無法使用。**

來源：<https://docs.docker.com/security/authentication/oidc-connections/>、<https://docs.docker.com/security/authentication/oidc-connections/create-manage/>、<https://github.com/docker/docs/blob/main/data/summary.yaml>

OIDC 的設定方式（官方 README 原文，若未來升級方案可用）：需要 `docker/login-action` **v4.5.0 以上**；workflow 授予 `id-token: write`；`username` 填 Docker Hub 組織名稱；**省略 `password`**；用環境變數 `DOCKERHUB_OIDC_CONNECTIONID` 帶入連線 ID。

```yaml
permissions:
  contents: read
  id-token: write
...
      - uses: docker/login-action@v4
        env:
          DOCKERHUB_OIDC_CONNECTIONID: ${{ vars.DOCKERHUB_OIDC_CONNECTIONID }}
        with:
          username: ${{ vars.DOCKERHUB_ORGANIZATION }}
```

OIDC token 為短期憑證：「All tokens created and exchanged during an OIDC workflow are short-lived and issued on a per-workflow basis.」

**permissions 需求**：用 PAT 時 login-action 不需要任何特殊 workflow permission（官方 Docker Hub 範例沒有 `permissions` 區塊）。用 OIDC 時必須 `id-token: write`。

**對本專案的結論**：**用 PAT**，權限設為 Read + Write（不給 Delete），設定到期日並記在行事曆。OIDC 不適用（需要 Team 方案）。

### 13.2 C2：Docker Hub 免費方案限制（2026-09-21 查得）

**pull rate limit**（官方表格，單位為**每 6 小時**）：

| 帳號類型 | 每 6 小時 pull 上限 | 公開 repo 數 | 私有 repo 數 |
|---|---|---|---|
| Business（已認證） | Unlimited | Unlimited | Unlimited |
| Team（已認證） | Unlimited | Unlimited | Unlimited |
| Pro（已認證） | Unlimited | Unlimited | Unlimited |
| **Personal（已認證）** | **200** | **Unlimited** | **最多 1** |
| 未認證 | 100，per IPv4 address or IPv6 /64 subnet | 不適用 | 不適用 |

來源：<https://docs.docker.com/docker-hub/usage/>

> **數字衝突，需注意**：`www.docker.com/pricing` 的 Personal 欄位寫「100 pulls/hr per user」，與 docs 的「200 per 6 hours」不一致。docs 頁面較具體（有明確表格與 6 小時視窗），本文以 docs 為準。
> 來源：<https://www.docker.com/pricing/> 對比 <https://docs.docker.com/docker-hub/usage/>

**multi-arch image 每個架構算一次 pull**：「A pull for a multi-arch image will count as one pull for each architecture」。version check 不計入。
來源：<https://docs.docker.com/docker-hub/usage/pulls/>

**另有獨立的 abuse rate limit**：依 IPv4 / IPv6 /64 計算，數量級為每分鐘數千次請求，所有方案一律適用，回傳單純的 `429 Too Many Requests`（pull limit 則回傳含文件連結的較長訊息）。

**fair use**：官方保留對「excessive data and storage consumption」帳號加限制或額外收費的權利。

**image 保留政策：目前官方文件未記載任何自動刪除 inactive image 的政策。** 在 docker/docs repo 的 `content/manuals/docker-hub/` 下全文搜尋「inactive / retention / 6 months」，唯一命中的是 Image Management 頁的註記：

> Images that haven't been pulled in over 6 months are marked as **Stale** in the **Status** column.

**只是 UI 標記，不是刪除。**
來源：<https://docs.docker.com/docker-hub/repos/manage/hub-images/manage/>、<https://github.com/docker/docs/blob/main/content/manuals/docker-hub/repos/manage/hub-images/manage.md>

**storage 計費：官方公告為「無限期延後」，不是取消。** Docker 官方部落格（2025-02-21 發布，2025-04-08 更新）原文：

> we have decided to indefinitely delay any storage charges... **If and when storage charges are introduced, we will provide a six-month notice.**

來源：<https://www.docker.com/blog/revisiting-docker-hub-policies-prioritizing-developer-experience/>

**multi-architecture manifest**：Docker Hub 原生支援，文件未記載額外限制。官方說明 image index 指向多個架構專屬 image：「This structure enables multi-architecture support through a single reference.」registry API 遵循 OCI distribution specification。
來源：<https://docs.docker.com/docker-hub/repos/manage/hub-images/manage/>、<https://docs.docker.com/docker-hub/repos/manage/hub-images/oci-artifacts/>

**對本專案的結論**：

- **必須用 public repo**（免費方案只有 1 個 private repo）。這本來就是發佈 image 的目的。
- 免費個人帳號的 200 pulls / 6 小時是**對這個帳號 pull 別人 image 的限制**，不是別人 pull 我的 image 的限制（拉取方受自己帳號的限制）。CI 中若從 Docker Hub 拉 base image，要注意這個額度 —— 但本專案的 base image 是 `python:*`，GitHub Actions runner 上未認證拉取受 100/6h per IP 限制，而 runner 的 IP 是共用的。**建議在 CI 中也登入 Docker Hub 再拉 base image**，把額度掛在自己帳號上。
- 沒有自動刪除政策，不必擔心 image 被清掉。

### 13.3 C3：Docker Hub repo 說明頁同步

**沒有官方的 README 同步 Action。** Docker 自己的文件頁「Update Docker Hub description with GitHub Actions」**直接建議使用第三方 action**，並以 commit SHA pin 住版本：

```yaml
- name: Update repo description
  uses: peter-evans/dockerhub-description@e98e4d1628a5f3be2be7c231e50981aee98723ae # v4.0.0
  with:
    username: ${{ vars.DOCKERHUB_USERNAME }}
    password: ${{ secrets.DOCKERHUB_TOKEN }}
    repository: user/app
```

來源：<https://docs.docker.com/build/ci/github-actions/update-dockerhub-desc/>

**官方 Hub API 沒有更新既有 repo `full_description` 的端點。** 逐條檢查官方 OpenAPI 規格 `latest.yaml`：

- `POST /v2/namespaces/{namespace}/repositories` —— 建立時可帶 `full_description`
- `GET /v2/namespaces/{namespace}/repositories/{repository}` —— 唯讀，無 PATCH/PUT
- `PATCH /v2/namespaces/{namespace}/repositories/{repository}/immutabletags`

**沒有任何 PATCH/PUT 可更新既有 repo 的 description 或 full_description。**
來源：<https://docs.docker.com/reference/api/hub/latest/>、規格檔 <https://docs.docker.com/reference/api/hub/latest.yaml>

**推論**：`peter-evans/dockerhub-description` 使用的是未記載於官方 API 規格的舊版端點。官方文件未說明這點，**標示為未知**。

**`peter-evans/dockerhub-description` 現況（非官方，第三方）**：

| 項目 | 值 |
|---|---|
| 最新 release | **v5.0.0**，發布於 **2025-10-01** |
| 封存狀態 | 未封存（`archived: false`） |
| 最後 push | 2026-09-03 |
| 最後更新 | 2026-09-20 |
| Stars | 385 |
| 授權 | MIT |

**仍在維護中**（近三週內有活動）。

**注意**：Docker 官方文件引用的是 **v4.0.0**，比目前最新版落後一個 major。
來源：`gh api repos/peter-evans/dockerhub-description/releases/latest`、`gh api repos/peter-evans/dockerhub-description`（2026-09-21）、<https://github.com/peter-evans/dockerhub-description>

**建議**：用這個 action（官方文件都在推薦它），**以 commit SHA pin 住**（官方範例就是這樣做），並讓 Renovate 的 `github-actions` manager 追蹤更新。用 v5.0.0 而非官方文件的 v4.0.0。

### 13.4 C4：供應鏈

**`provenance` 的預設值取決於 GitHub repo 的可見性**（官方原文）：

- **公開 repo：自動加上 `mode=max` 的 provenance attestation**
- 私有 repo：自動加上 `mode=min`
- 使用 `docker` exporter 或 `load: true` 時：不加任何 attestation（這些輸出格式不支援）

來源：<https://docs.docker.com/build/ci/github-actions/attestations/>

**`sbom` 預設關閉**：「SBOM attestations aren't automatically added to the image. To add SBOM attestations, set the `sbom` input of the `docker/build-push-action` to true.」

**公開 repo 用 `mode=max` 的洩密風險（官方警告）**：

> the provenance attestations attached to your image by default contains the values of build arguments

若誤用 build args 傳 secret，會直接曝露在 provenance 中。

**對本專案**：Dockerfile 若用 `ARG FAVA_VERSION=1.30.16` 這類版本參數（既有研究 5.2 的 Renovate custom regex manager 方案），這些值會出現在 provenance 中 —— 版本號不是 secret，沒有問題。**但不要用 build args 傳任何憑證。**

**官方對 `mode=max` 的建議**：「It's recommended that you build your images with max-level provenance attestations.」私有 repo 需手動設定。

**有 attestation 就必須直接 push 到 registry**：「the local image store doesn't support loading images with attestations.」這代表 CI 中**不能**先 `load: true` 做 smoke test 再 push（與既有研究提到的 `tarioch/docker-fava` CI smoke test 模式有衝突）。

**變通做法（推論，未驗證）**：分兩個 build step —— 一個 `load: true` 且 `provenance: false` 供 smoke test，一個 `push: true` 帶 attestation。或先 push 到暫時 tag，再 pull 下來測。

**產生的內容**：provenance 為 SLSA provenance；SBOM 遵循 SPDX 標準，以 in-toto SPDX predicate 格式附加為 JSON 編碼的 SPDX 文件。
來源：<https://docs.docker.com/build/metadata/attestations/sbom/>、<https://docs.docker.com/build/metadata/attestations/slsa-provenance/>

**attestation 在 image index 中的呈現**：

> Attestations are stored as manifest objects in the image index

> the `platform` property of the attestation manifest will be set to `unknown/unknown`

這是刻意設計，讓 registry 與 container engine 不會把 attestation 當成可執行 image 去 pull。descriptor 帶 `vnd.docker.reference.type: attestation-manifest` 與 `vnd.docker.reference.digest` 兩個 annotation。OCI artifact 模式下 artifact type 為 `application/vnd.docker.attestation.manifest.v1+json`。
來源：<https://docs.docker.com/build/metadata/attestations/attestation-storage/>

**對 multi-arch 的實際後果**：`docker buildx imagetools inspect` 會顯示額外的 `unknown/unknown` 條目。這是正常的，不是錯誤。

**Docker Hub 支援儲存 attestation**：Docker Hub 的 OCI artifacts 頁明確把「Attestations」「Provenance data」「Software Bill of Materials (SBOM)」「Digital signatures」列為可儲存的 OCI artifact 類型，registry API 遵循 OCI distribution spec。
來源：<https://docs.docker.com/docker-hub/repos/manage/hub-images/oci-artifacts/>

**Docker Hub UI 如何呈現 attestation：官方文件未記載 → 未知。**

**cosign 官方明列支援 Docker Hub**：

> Cosign has been tested and works against: AWS Elastic Container Registry, GCP's Artifact Registry and Container Registry, **Docker Hub**, Azure Container Registry, ... GitHub Container Registry, ...

來源：<https://docs.sigstore.dev/cosign/system_config/registry_support/>

**但 Docker Hub 不支援 cosign 的簽章刪除**：「Some registries support deletion too **(DockerHub does not)**」—— `cosign clean` 在 Docker Hub 上不可用。
來源：<https://docs.sigstore.dev/cosign/signing/signing_with_containers/>

**keyless signing 的官方 GitHub Actions 做法**：

```yaml
permissions:
  contents: read
  packages: write
  id-token: write # needed for signing the images with GitHub OIDC Token
...
      - name: Install Cosign
        uses: sigstore/cosign-installer@v4.0.0
...
      - run: cosign sign --yes ${images}
```

`--yes` 表示接受 Sigstore 使用條款；`id-token: write` 是取得 OIDC token 的必要權限。
來源：<https://docs.sigstore.dev/quickstart/quickstart-ci/>

**`sigstore/cosign-installer` 最新版 `v4.1.2`，發布於 2026-05-07**。README 範例用 `sigstore/cosign-installer@v4.1.0`，可用 `cosign-release` 輸入 pin 住 cosign 版本。安裝時會驗證 cosign release 的完整性。
來源：`gh api repos/sigstore/cosign-installer/releases/latest`（2026-09-21）、<https://github.com/sigstore/cosign-installer>

**Docker Scout 免費額度：1 個 repo。** docker/docs 原文：「A Personal subscription includes up to 1 repository. Upgrade for more.」pricing 頁對應：Personal「1 Docker Scout-enabled repo」、Pro「2」、Team / Business「Unlimited」。
來源：<https://docs.docker.com/scout/>、<https://www.docker.com/pricing/>

**對本專案**：只有一個 image，剛好用完免費額度。可以啟用。

### 13.5 C5：與既有的 Renovate 自動更新建議銜接

**Docker Hub 的 image 名稱不帶 registry 前綴。** `metadata-action` 的 `images` 官方範例直接並列兩種格式：

```yaml
images: |
  name/foo
  ghcr.io/name/bar
  # or
  name=name/foo
  name=ghcr.io/name/bar
```

`images` 為 List，支援延伸屬性 `name=<string>` 與 `enable=<true|false>`（預設 `true`）。
來源：<https://github.com/docker/metadata-action#images-input>

**同時推 GHCR 與 Docker Hub 的官方做法：兩個 login step、一次 build-push。**

Docker 官方「Push to multiple registries with GitHub Actions」頁面的做法：先 `docker/login-action` 登入 Docker Hub（`username: ${{ vars.DOCKERHUB_USERNAME }}`、`password: ${{ secrets.DOCKERHUB_TOKEN }}`），再用同一個 action 加 `registry: ghcr.io`、`username: ${{ github.repository_owner }}`、`password: ${{ secrets.GITHUB_TOKEN }}` 登入 GHCR，然後單一 `docker/build-push-action` 的 `tags` 同時列出兩邊的完整 image 名稱；搭配 `setup-qemu-action` + `setup-buildx-action` 做 `linux/amd64,linux/arm64` 多平台建置。
來源：<https://docs.docker.com/build/ci/github-actions/push-multi-registries/>

> 注意：該官方頁面直接寫死 `tags`，未搭配 metadata-action，也未列 `permissions` 區塊（推 GHCR 用 `secrets.GITHUB_TOKEN` 通常需 `packages: write`，此頁未記載；既有研究 5.4 已記錄這點）。

**tag 命名規則**（canonical 定義為 distribution reference）：

Docker CLI 文件把 <https://pkg.go.dev/github.com/distribution/reference> 指為「the canonical definition of the format」：

| 項目 | 規則 |
|---|---|
| tag 文法 | `tag := /[\w][\w.-]{0,127}/` — 首字元須為 word character，總長**最多 128 字元**，可用 word character、`.`、`-` |
| tag 大小寫 | **可以含大寫字母** |
| repository path component | `path-component := alpha-numeric [separator alpha-numeric]*`，alpha-numeric 為 `/[a-z0-9]+/`，separator 為 `_`、`.`、`-` |
| repository 名稱大小寫 | **必須全小寫**（`ErrNameContainsUppercase`: "repository name must be lowercase"） |
| repository 名稱總長 | `RepositoryNameTotalLengthMax = 255` |

來源：<https://docs.docker.com/reference/cli/docker/image/tag/>、<https://pkg.go.dev/github.com/distribution/reference>

**檢驗既有研究 5.3 的 tag 策略**：

| Tag | 長度 | 合法 |
|---|---|---|
| `1.30.16-beancount3.2.3` | 22 | 是 |
| `1.30.16` | 7 | 是 |
| `1.30` | 4 | 是 |
| `latest` | 6 | 是 |
| `1.30.16-beancount3.2.3-20260921` | 31 | 是 |

**既有研究的 tag 策略在 Docker Hub 上完全可用，不需要修改。** 唯一要注意的是 **repository 名稱必須全小寫**（例如 `<user>/beancount-fava`，不能是 `<user>/Beancount-Fava`）。

**Docker Hub 支援 immutable tags（防覆蓋），目前為 Beta。**

docker/docs 的 `data/summary.yaml` 記錄 `Immutable tags: availability: Beta`，**未標註任何 subscription 限制**（對照組：同檔案的 `OIDC connections` 標 `subscription: [Team, Business]`、`Registry access management` 標 `[Business]`）。
來源：<https://github.com/docker/docs/blob/main/data/summary.yaml>

三種設定模式（repo Settings > General > Tag mutability settings）：

- All tags are mutable（預設）
- All tags are immutable —— **包含 `latest` tag**
- Specific tags are immutable —— 用 regex 指定，採 Go regexp（RE2）語法

啟用後 tag 無法被指向不同 image，且「Images associated with immutable tags can't be deleted. Only items associated with mutable tags can be deleted.」
來源：<https://docs.docker.com/docker-hub/repos/manage/hub-images/immutable-tags/>、<https://docs.docker.com/docker-hub/repos/manage/hub-images/manage/>

API：`PATCH /v2/namespaces/{namespace}/repositories/{repository}/immutabletags`（需 repo 管理權限，接受 OAT bearer token）；另有 `POST .../immutabletags/verify`。
來源：<https://docs.docker.com/reference/api/hub/latest/>

**這對既有研究 5.3 的建議是一個實質改進**：既有研究說「若採週期性重建，完整組合 tag 會被覆蓋，建議額外加一個帶日期或 sha 的不可變 tag」。Docker Hub 的 **Specific tags are immutable** 可以用 regex 直接把 `^\d+\.\d+\.\d+-beancount` 這類 tag 標成不可變，由 registry 強制，而不是靠命名慣例自律。

**但要小心**：

- 標為不可變後**無法刪除**。建錯的 tag 會永久留著。
- 「All tags are immutable」模式會把 `latest` 也鎖住，導致 `latest` 無法更新 —— **不要用這個模式**。
- 功能是 Beta。
- 方案限制未明（見 15.2）。

**建議**：先不啟用 immutable tags（Beta + 不可刪除的風險），維持既有研究「加一個帶日期/sha 的不可變命名慣例」的做法。等功能 GA 且方案限制明朗後再評估。

**Renovate 端需要的調整**：**沒有。** Renovate 的 `pep621`、`dockerfile`、`github-actions` manager 與 registry 選擇無關。唯一的差異在 workflow 的 login step 與 `metadata-action` 的 `images` 值，這兩者都不是 Renovate 管的對象。

**一個新增的 Renovate 對象**：若用 docker compose 部署 cloudflared，把 `cloudflare/cloudflared` 交給 Renovate 的 `docker-compose` manager 追蹤。

---

## 14. 整體建議架構

### 14.1 Container、volume、network

```
                          ┌─────────────────────────────────────┐
                          │  Cloudflare edge                     │
                          │  - Access application (fava)         │
                          │    policy: Allow / IdP (OTP)         │
                          │  - Access application (api)  [選用]  │
                          │    policy: Service Auth / token      │
                          │  - Require Access protection: ON     │
                          │  - Free Managed Ruleset (WAF)        │
                          └───────────────┬─────────────────────┘
                                          │ outbound only, TCP/UDP 7844
                                          │ （origin 沒有 inbound listener）
  ════════════════════════════════════════╪══════════════════════════════════
   主機 / docker compose                  │
                          ┌───────────────▼─────────────────────┐
                          │ cloudflared                          │
                          │ image: cloudflare/cloudflared:<ver>  │
                          │ 無 published port                    │
                          │ originRequest.access = required      │
                          │   （代 fava 驗 Cf-Access-Jwt-…）      │
                          │ healthcheck: GET :2000/ready         │
                          └──────┬────────────────────┬──────────┘
                                 │ internal network    │
                    ┌────────────▼────────┐   ┌────────▼──────────────┐
                    │ fava                 │   │ api  [僅在需要時上公網]│
                    │ 同一個 image         │   │ 同一個 image           │
                    │ CMD: fava --read-only│   │ CMD: api-serve         │
                    │ FAVA_HOST=0.0.0.0    │   │ 自行驗 JWT (PyJWT)     │
                    │ :5000（不 publish）   │   │ :8000（不 publish）     │
                    │ ledger volume :ro    │   │ ledger volume :rw      │
                    └──────────┬───────────┘   └────────┬──────────────┘
                               │ 唯讀                     │ 唯一寫入者
                          ┌────▼─────────────────────────▼────┐
                          │ ledger volume                       │
                          │ ├── main.beancount                  │
                          │ ├── agent-inbox.beancount           │
                          │ ├── documents/                      │
                          │ └── .git/                           │
                          └─────────────────────────────────────┘

   本機的 AI Agent（Claude Code 等）
        └── MCP stdio：docker run -i --rm <image> mcp-stdio
            共用同一個 ledger volume，無任何網路曝露  ← 建議的主要路徑
```

### 14.2 寫入者歸屬

**唯一寫入者：`api` container。**

強制手段（由外而內）：

1. fava 的 volume 以 `:ro` 掛載 —— 核心層級。
2. fava 以 `--read-only` 執行 —— 應用層級，所有非 GET 回 401。
3. `api` 內部以 `flock` 持有 ledger 的排他鎖 —— 防禦自己被啟動兩份。

**寫入序列**（每一步的順序都有理由）：

```
1. 取得 flock（排他，有限等待）
2. 冪等檢查：idempotency link 已存在 → 直接回傳既有結果，結束
3. 載入目前的 ledger，取得 baseline errors
4. 產生 entry 文字（fava.beans.str.to_string，套用 currency-column / indent）
5. 沙箱驗證：在副本上插入，load_file，與 baseline 做差集
   ├─ 有新 error → 回傳 422，不寫入，結束
   └─ 無新 error → 繼續
6. 原子換檔（必要副作用）：
   temp file（同目錄）→ write → flush → fsync → os.replace → fsync(dir)
   ├─ 失敗 → 回傳 500，結束（帳本未變）
   └─ 成功 → 繼續
7. git add + git commit（best-effort 副作用）
   └─ 失敗 → 記 log，不影響回應。下次寫入時 git add -A 補上
8. 釋放 flock
9. 回傳 201 與寫入的 entry 文字、目標檔案、行號、idempotency link
```

**第 6 步是必要副作用，第 7 步是 best-effort**：帳已經記了，git commit 失敗不該讓 agent 以為記帳失敗而重試（重試會被第 2 步的冪等檢查擋下，但仍浪費一趟）。

**fava 如何看到變更**：`os.replace` 產生 inotify 的 `IN_MOVED_TO`，fava 的 `_FilesWatchfilesThread` 監看檔案的父目錄，正是為這種情況設計的（2.4）。前端 5 秒內輪詢到 `changed` 回 true，自動重新載入。

### 14.3 公網路徑

**建議：只發佈 fava，API 不上公網。**

```
fava.example.com  → Access（Allow / One-time PIN 或 Cloudflare IdP）
                  → tunnel → fava:5000（唯讀）
```

AI Agent 走本機 MCP stdio，零網路曝露。

**若 agent 必須遠端**，加第二個 hostname：

```
api.example.com   → Access（Service Auth / Service Token，開 401 Response）
                  → tunnel → api:8000（自行驗 Cf-Access-Jwt-Assertion）
```

**不要用同一 hostname 不同路徑**，除非有明確理由 —— 兩個獨立的 Access application 比依賴 path 優先序更不容易設錯。

### 14.4 Image 與發佈

沿用既有研究第 7 節，加上本次的調整：

| 項目 | 既有研究 | 本次調整 |
|---|---|---|
| base image | `python:3.13-slim-trixie` | 不變 |
| 平台 | `linux/amd64` + `linux/arm64` | 不變 |
| 相依 | `pyproject.toml` + `uv.lock` | **加上** FastAPI、uvicorn、`mcp>=2.2.0`、PyJWT |
| 系統套件 | 未提及 | **加上 `git`**（auto-commit 稽核需要） |
| init | tini 或 `--init` | 不變。**MCP stdio 模式的 log 必須走 stderr** |
| 非 root | 固定 uid/gid | 不變。**git 需要 `user.name` / `user.email` 設定** |
| registry | GHCR 或 Docker Hub | **Docker Hub public repo**（名稱全小寫）；可同時推 GHCR |
| 認證 | — | **PAT（Read + Write）**，secrets 名為 `DOCKERHUB_TOKEN`，username 放 `vars.DOCKERHUB_USERNAME` |
| Tag | `<fava>-beancount<bc>`、`<fava>`、`latest`、帶日期/sha | 不變，Docker Hub 完全相容 |
| provenance | 未提及 | 公開 repo 自動 `mode=max`。**不要用 build args 傳 secret** |
| SBOM | 未提及 | 預設關閉，可設 `sbom: true` |
| 簽章 | 未提及 | cosign keyless（`sigstore/cosign-installer@v4.1.2`，`id-token: write`）。**Docker Hub 不支援 `cosign clean`** |
| README 同步 | 未提及 | `peter-evans/dockerhub-description@v5.0.0`（非官方，但 Docker 官方文件推薦），以 SHA pin |
| Docker Scout | 未提及 | 免費 1 個 repo，剛好夠用 |

---

## 15. 對既有研究的更新

### 15.1 解除的未知事項

| 既有研究的未知事項 | 本次結論 |
|---|---|
| **3. fava 寫入 ledger 檔時是否使用原子換檔** | **非原子。** 所有寫入都是 `path.open("w")` 就地截斷重寫。`src/fava/` 下搜尋 `os.replace`、`os.rename`、`tempfile`、`fsync`、`fcntl`、`flock` 皆 0 筆命中。寫入中途被終止會留下截斷的檔案（第 2.1 節） |
| **7. 是否有官方發佈的 beancount 或 fava container image** | **Docker Hub 上沒有官方 namespace 的 image。** 以 Docker Hub search API 查 `fava` 與 `beancount`（2026-09-21），結果全是社群帳號（`yegle/fava`、`tarioch/fava`、`alexiri/fava` 等），沒有 `beancount/*` 或官方組織的 repo。GHCR 未窮舉，仍標未知 |
| **9. `--prefix` 與反向代理搭配時 fava JSON API 路徑的完整行為** | **已驗證：前後端一致，API 路徑會帶前綴。** `DispatcherMiddleware` 設 `SCRIPT_NAME`，`url_for("index")` 成為前端的 `base_url`，前端以 `${base_url}api/${endpoint}` 組 URL。另外發現 **prefix 之外的路徑由 `simple_wsgi` 回傳空的 200，不是 404**（第 10.1 節） |

### 15.2 需要修正的建議

**（a）既有研究 6.4 與 7.3：「fava 是唯一的 ledger 寫入者」→ 應改為「API 服務是唯一寫入者，fava 以 `--read-only` 執行」。**

理由：

1. fava 寫入非原子（2.1），不適合作為可寫入系統的唯一寫入路徑。
2. fava 最常用的寫入端點 `add_entries` 沒有樂觀鎖（2.3c）。
3. 新需求「API 必須可寫入 ledger，主要使用者是 AI Agent」要求驗證、稽核、冪等、權限分級 —— fava 全部不提供。
4. 新需求「fava 透過 Cloudflare Tunnel 上公網」使得「fava 必須可寫入」變成一個重大的攻擊面（fava 無任何認證）。

既有研究的**方案 C（同一 image、兩個 container、共用 ledger volume）結構不變**，只是寫入者歸屬對調，volume 的 `:ro` 掛在 fava 而不是 API。

**（b）既有研究 3.6(c) 的措辭「fava 寫的是檔案內容，不是原子換檔（未逐一查證，標示為未知）」→ 現在可以去掉「未知」，確認為非原子。**

**（c）既有研究 5.3 的 tag 策略：不需要因 Docker Hub 而改。**

tag 文法 `[\w][\w.-]{0,127}`，既有策略的所有 tag 都合法。唯一新增的要求是 **repository 名稱必須全小寫**。

Docker Hub 的 **immutable tags**（Beta）可以由 registry 強制既有研究想要的「不可變 tag」性質，但目前不建議啟用（Beta + 不可刪除 + 方案限制未明）。

**（d）既有研究提到的 CI smoke test 模式（`load: true` 再測）與 provenance attestation 有衝突。**

「the local image store doesn't support loading images with attestations」。公開 repo 預設會加 `mode=max` provenance。要保留 smoke test，需要分開兩個 build step（一個 `load: true` + `provenance: false`，一個 `push: true` 帶 attestation），或先 push 到暫時 tag 再 pull 下來測。**未驗證哪一種在實務上較順，標示為待決。**

**（e）既有研究 7.1 的「加 init（tini）處理 SIGTERM」在 MCP stdio 模式下需要額外注意。**

stdio MCP server 以 `docker run -i --rm` 啟動，協定走 stdin/stdout。tini 不影響協定，但**任何寫到 stdout 的 log 都會污染 JSON-RPC 訊息流**。entrypoint 必須確保 log 走 stderr。

### 15.3 新增的相依與系統套件

既有研究 7.1 的 image 規格需要補上：

- **`git`**（系統套件）—— 稽核用。fava 內建的 `auto_commit` extension 也需要它（雖然本專案不用該 extension）。
- **`PyJWT`** —— 驗證 `Cf-Access-Jwt-Assertion`（官方有 Python 範例）。
- **`mcp>=2.2.0`** —— MCP server。`requires-python >=3.10`，與既有研究選的 Python 3.13 相容。
- **FastAPI + uvicorn** —— REST API。既有研究 4.3 已預見這種情況：「若未來需要 base image，觸發條件應該是：出現第二個消費者，且它的 Python 相依集合與 fava 顯著不同（例如 API 服務要 FastAPI + uvicorn 而 fava 不要）」。

**這正是既有研究預留的觸發條件。** 但本次仍建議維持**單一 image**，理由：FastAPI + uvicorn + mcp + PyJWT 都是純 Python wheel，安裝成本低；image 多幾 MB 換取單一 Dockerfile、單一版本矩陣、單一 CI pipeline，划算。**等 image 大小成為實際問題時再分層。**

### 15.4 既有研究未涵蓋而本次新增的事實

- fava 內建 `fava.ext.auto_commit` extension（6.1）—— 既有研究 6.2 討論 extension 機制時未提到這個現成的例子。
- fava 前端每 5 秒輪詢 `api/changed`（10.4）—— 影響 rate limiting 規劃。
- fava 不使用 WebSocket 或 SSE（10.4）。
- fava 的寫入受 `options["include"]` 限制，無法寫任意路徑（2.6）—— 這是一個正面的安全性質，既有研究未提。
- `--prefix` 之外的路徑回傳空的 200 而非 404（10.1）—— 健康檢查容易誤判。

---

## 16. 未知事項與待決問題

### 16.1 無法從第一手來源確認

| # | 項目 | 說明 |
|---|---|---|
| 1 | **Zero Trust 免費方案的硬性 seat 上限** | `developers.cloudflare.com` 的 Account limits 頁沒有 seat 數字；官方產品頁只說「Best for teams under 50 users」。是否為硬性上限未明文 |
| 2 | **cloudflared 未設 `httpHostHeader` 時送往 origin 的 Host 值** | 官方文件未說明 |
| 3 | **官方是否建議釘選特定 cloudflared tag** | 只有「支援一年內版本」的政策，無 tag 策略建議 |
| 4 | **cloudflared 的官方 GHCR image** | 官方文件與 README 只提 Docker Hub |
| 5 | **Cloudflare Access 的 OAuth 實作能否充當 MCP 規格要求的 authorization server** | 兩者用同一組 RFC（9728、PKCE），但未查證相容性 |
| 6 | **Docker Hub UI 是否顯示 SLSA provenance / SBOM attestation** | 官方文件未記載 |
| 7 | **官方 Hub API 更新既有 repo `full_description` 的端點** | 官方 OpenAPI 規格中不存在；`peter-evans/dockerhub-description` 實際呼叫的端點未列於官方文件 |
| 8 | **Docker Hub immutable tags 的方案限制** | `data/summary.yaml` 只標 `Beta`，未標 subscription；pricing 頁也未列 |
| 9 | **Cloudflare WebSocket 閒置逾時的具體秒數** | 官方只說「a period of time」。本專案用不到，但未來若做 MCP subscriptions 會相關 |
| 10 | **Log Explorer（Beta）的方案需求** | 官方頁未說明 |
| 11 | **GHCR 上是否有官方的 beancount / fava image** | 本次只查了 Docker Hub |

### 16.2 需要實測

| # | 項目 | 為什麼重要 |
|---|---|---|
| 1 | **ledger 的實際載入時間** | Cloudflare 的 Proxy Read Timeout 是 125 秒（10.3）。若載入超過這個時間，首次請求與 dry-run 都會回 524。也影響既有研究未知事項 6（記憶體用量） |
| 2 | **`os.replace` 換檔後 fava 的 watchfiles 是否確實偵測到** | 原始碼註解說設計上支援（`_FilesWatchfilesThread` 監看父目錄，註解寫「file replacements by some editors」），但未在 Docker volume 上實測。若失效，退路是 `--poll-watcher` |
| 3 | **Docker volume / bind mount 上的 inotify 傳遞** | 跨 container 的 inotify 事件在同一主機的 Docker volume 上應該正常（同一個核心），但 Docker Desktop 的 bind mount 有已知差異 |
| 4 | **provenance attestation 與 CI smoke test 的並存方式** | 「local image store doesn't support loading images with attestations」，需要實際試出可行的 workflow 結構 |
| 5 | **`mcp` SDK 在 stdio 模式的預設 log 輸出目的地** | 若預設寫 stdout 會污染協定 |
| 6 | **fava `--prefix` 加 Cloudflare path 的完整端到端行為** | 程式碼層面已驗證（10.1），但未實際部署驗證 |

### 16.3 待決的設計問題

1. **人類是否需要在 fava 中編輯帳本？** 若需要，`--read-only` 方案就必須補一個編輯介面（5.3）。這是本次建議的唯一實質取捨。
2. **AI Agent 跑在哪裡？** 本機（MCP stdio，零網路曝露）還是遠端（需要 API 上公網）。這決定 B 部分一半的複雜度。
3. **`!` flag 的核准流程由誰執行？** fava 唯讀時無法在 UI 中改 flag。需要 API 提供核准端點，或接受「`!` 只是標記，不做核准」。
4. **agent 寫入是否進獨立的 include 檔？** 6.4 建議的 `agent-inbox.beancount` 讓 append-only 成為檔案層級的性質，但增加一個檔案與一次搬移流程。
5. **是否同時推 GHCR？** Docker Hub 是需求；GHCR 是選項。同時推的邊際成本很低（多一個 login step），但多一個要維護的 registry。
6. **Tunnel 用 remotely-managed 還是 locally-managed？** 前者官方建議且設定簡單；後者讓 ingress 規則進 git（8.2）。

---

## 17. 來源清單

### Fava 原始碼（v1.30.16）

- `src/fava/core/file.py`（寫入實作、sha256 樂觀鎖、insert 位置、換行處理）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/file.py>
- `src/fava/core/watcher.py`（watchfiles 與輪詢式 watcher）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/watcher.py>
- `src/fava/core/__init__.py`（`changed()`、`load_file()`、`mtime`）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/__init__.py>
- `src/fava/core/fava_options.py`（`InsertEntryOption`、`default_file`）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/fava_options.py>
- `src/fava/core/documents.py`（documents 路徑檢查）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/core/documents.py>
- `src/fava/json_api.py`（endpoint、錯誤對應、request/response 格式）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py>
- `src/fava/serialisation.py`（`deserialise`、支援的 directive 型別）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/serialisation.py>
- `src/fava/application.py`（blueprint prefix、`_read_only`、`_perform_global_filters`）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/application.py>
- `src/fava/internal_api.py`（`base_url = url_for("index")`）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/internal_api.py>
- `src/fava/cli.py`（`--prefix` 的 DispatcherMiddleware）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/cli.py>
- `src/fava/util/__init__.py`（`simple_wsgi`）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/util/__init__.py>
- `src/fava/beans/str.py`（`to_string`）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/beans/str.py>
- `src/fava/ext/auto_commit.py`（內建 git auto-commit extension）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/ext/auto_commit.py>
- `src/fava/help/options.md`（`insert-entry`、`default-file`、`currency-column`、`indent`）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/help/options.md>
- `src/fava/help/extensions.md`（extension hook 清單與不穩定性聲明）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/help/extensions.md>
- `frontend/src/app.ts`（5 秒輪詢）：<https://github.com/beancount/fava/blob/v1.30.16/frontend/src/app.ts>
- `frontend/src/api/index.ts`（API URL 組法）：<https://github.com/beancount/fava/blob/v1.30.16/frontend/src/api/index.ts>
- `docs/api.rst`（無穩定性保證）：<https://github.com/beancount/fava/blob/main/docs/api.rst>
- `contrib/deployment.rst`（`--prefix` 與反向代理）：<https://github.com/beancount/fava/blob/v1.30.16/contrib/deployment.rst>
- 搜尋結果（0 筆命中）：`os.replace` / `os.rename` / `tempfile` / `fsync` / `fcntl` / `flock`、`csrf` / `cors` / `session[` / `SECRET_KEY` / `authenticat`、`EventSource` / `WebSocket` / `text/event-stream` —— 實際對 `v1.30.16` source tarball 執行（2026-09-21）

### Beancount

- `beancount/loader.py`（`load_file`、`load_string` 簽章）：<https://github.com/beancount/beancount/blob/master/beancount/loader.py>
- `beancount/parser/printer.py`（`format_entry`、`print_entries`）：<https://github.com/beancount/beancount/blob/master/beancount/parser/printer.py>
- `beancount/parser/lexer.l`（tag / link / metadata key 的字元類）：<https://github.com/beancount/beancount/blob/master/beancount/parser/lexer.l>
- `beancount/scripts/check.py`（`bean-check --json`、`HARDCORE_VALIDATIONS`）：<https://github.com/beancount/beancount/blob/v3.2.3/beancount/scripts/check.py>
- `beancount/plugins/noduplicates.py`：<https://github.com/beancount/beancount/blob/master/beancount/plugins/noduplicates.py>
- `beancount/api.py`（v3 public API）：<https://github.com/beancount/beancount/blob/master/beancount/api.py>
- Beancount Language Syntax（flag 語意、metadata 規則、tags 與 links）：<https://beancount.github.io/docs/beancount_language_syntax/>

### Python / Werkzeug

- `os.replace` 的原子性：<https://github.com/python/cpython/blob/main/Doc/library/os.rst>
- `DispatcherMiddleware` 的 `SCRIPT_NAME` 處理：<https://github.com/pallets/werkzeug/blob/main/src/werkzeug/middleware/dispatcher.py>

### MCP

- 規格版本目錄：<https://github.com/modelcontextprotocol/modelcontextprotocol/tree/main/docs/specification>
- 2026-07-28 changelog（移除 session、移除 initialize、`server/discover`、移除 SSE resumability）：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx>
- 2026-07-28 authorization（OAuth 2.1、RFC 9728、stdio 例外）：<https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/authorization/index.mdx>
- Python SDK PyPI metadata（2.2.0，2026-09-07）：<https://pypi.org/pypi/mcp/json>
- Python SDK 最新 release：<https://github.com/modelcontextprotocol/python-sdk/releases/latest>
- Python SDK README（transport 支援）：<https://github.com/modelcontextprotocol/python-sdk/blob/main/README.md>

### 社群 beancount MCP server（非官方，僅供參考）

- <https://github.com/StdioA/beancount-mcp>（可寫入，2025-05-08）
- <https://github.com/vanto/beanquery-mcp>（唯讀，2025-04-01）
- <https://github.com/klinikal/beanie-mcp>（唯讀，明寫支援 v3，2026-07-04）
- <https://github.com/mekanics/mcp-beancount>（唯讀，有 `ACCOUNT_ALLOWLIST`，2026-08-10）
- <https://github.com/cookie223/beancount-fava-mcp>（透過 fava API 寫入，WIP，2025-12-18）
- 檢索來源：GitHub search API `search/repositories?q=beancount+mcp`（2026-09-21）

### Cloudflare Tunnel

- 官方 image 與下載：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/>、<https://hub.docker.com/r/cloudflare/cloudflared>
- Setup（`--token` 範例）：<https://developers.cloudflare.com/tunnel/setup/>
- Tunnel token：<https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/>
- Run parameters：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/>
- Local management（remotely vs locally-managed 的官方建議）：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/>
- Local tunnel terms（credentials file、cert.pem）：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/local-tunnel-terms/>
- Deployment guides（沒有 Docker）：<https://developers.cloudflare.com/tunnel/guides/>
- Kubernetes guide（`/ready` probe、replica 不做 load balance）：<https://developers.cloudflare.com/tunnel/guides/kubernetes/>
- Routing / 支援的 protocol：<https://developers.cloudflare.com/tunnel/concepts/routing/>
- Origin parameters（originRequest 完整表格）：<https://developers.cloudflare.com/tunnel/reference/origin-parameters/>、<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/>
- Configuration file（ingress path 規則、不剝除 path、catch-all 要求）：<https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/>
- Metrics：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/monitor-tunnels/metrics/>
- `metrics.go`（`/healthcheck` 路由，文件未載）：<https://github.com/cloudflare/cloudflared/blob/master/metrics/metrics.go>
- Replica：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-availability/deploy-replicas/>
- 防火牆需求：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/>
- Tunnel 概念（outbound-only、Authenticated Origin Pulls 無效）：<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/>
- Troubleshooting（SSE buffering、WebSocket handshake）：<https://developers.cloudflare.com/tunnel/troubleshooting/>
- Add routes：<https://developers.cloudflare.com/cloudflare-one/networks/routes/add-routes/>

### Cloudflare Access / Zero Trust

- Self-hosted public app（設定步驟、先建 application 的警告、必須驗證 token）：<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/>
- Account limits：<https://developers.cloudflare.com/cloudflare-one/account-limits/>
- Seat management：<https://developers.cloudflare.com/cloudflare-one/team-and-resources/users/seat-management/>
- Access 產品頁定價（「Best for teams under 50 users」）：<https://www.cloudflare.com/zero-trust/products/access/>
- Identity providers：<https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/>
- Cloudflare IdP：<https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/cloudflare/>
- One-time PIN：<https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/>
- Service tokens（建立、輪替、`cfast_` 格式、有效期）：<https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/>
- Policies（Allow / Block / Bypass / Service Auth、Include/Require/Exclude 邏輯）：<https://developers.cloudflare.com/cloudflare-one/access-controls/policies/>
- Application paths（wildcard、優先序、不支援 query string）：<https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/>
- Authenticate agents（service token 為自動化流程的建議做法）：<https://developers.cloudflare.com/cloudflare-one/access-controls/authenticate-agents/>
- Validating JSON（`Cf-Access-Jwt-Assertion`、`/cdn-cgi/access/certs`、6 週輪替、Python 範例）：<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/>
- Application token（「Unless your application is connected to Access through Cloudflare Tunnel」）：<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/>
- Require Access protection（Error 1050 Default-Deny）：<https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/require-access-protection/>
- Log retention（免費方案 Access logs 24 小時）：<https://developers.cloudflare.com/cloudflare-one/insights/logs/#log-retention>
- Access authentication logs（per-request 為 Enterprise、service token 事件需走 GraphQL）：<https://developers.cloudflare.com/cloudflare-one/insights/logs/dashboard-logs/access-authentication-logs/>
- Logpush（Enterprise-only）：<https://developers.cloudflare.com/cloudflare-one/insights/logs/logpush/>

### Cloudflare 平台限制

- Connection limits（timeout 表格、URL/header 上限）：<https://developers.cloudflare.com/fundamentals/reference/connection-limits/>
- Error 524：<https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/>
- Error 522：<https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-522/>
- Maximum upload size：<https://developers.cloudflare.com/cache/concepts/default-cache-behavior/#customization-options-and-limits>
- HTTP headers（`X-Forwarded-Proto`、`CF-Visitor`、`CF-Connecting-IP`）：<https://developers.cloudflare.com/fundamentals/reference/http-headers/>
- WebSockets：<https://developers.cloudflare.com/network/websockets/>
- WAF Managed Rules（方案矩陣）：<https://developers.cloudflare.com/waf/managed-rules/>
- WAF Custom rules（免費 5 條、無 regex）：<https://developers.cloudflare.com/waf/custom-rules/>
- Rate limiting rules（免費 1 條、僅 IP、10 秒窗）：<https://developers.cloudflare.com/waf/rate-limiting-rules/>

### Docker Hub / GitHub Actions

- `docker/login-action` README（inputs、Docker Hub 範例、OIDC 設定）：<https://github.com/docker/login-action>
- `docker/metadata-action` README（`images` 輸入）：<https://github.com/docker/metadata-action#images-input>
- Push to multiple registries：<https://docs.docker.com/build/ci/github-actions/push-multi-registries/>
- Attestations in CI（provenance 預設值、sbom 預設關閉、build args 洩密警告、無法 load）：<https://docs.docker.com/build/ci/github-actions/attestations/>
- Update Docker Hub description（官方推薦第三方 action）：<https://docs.docker.com/build/ci/github-actions/update-dockerhub-desc/>
- Access tokens（PAT 權限與到期）：<https://docs.docker.com/security/access-tokens/>
- Organization access tokens（Team/Business only）：<https://docs.docker.com/security/access-tokens/organization-access-tokens/>
- OIDC connections（限組織、限 Team/Business）：<https://docs.docker.com/security/authentication/oidc-connections/>、<https://docs.docker.com/security/authentication/oidc-connections/create-manage/>
- `data/summary.yaml`（OIDC 與 immutable tags 的方案標記）：<https://github.com/docker/docs/blob/main/data/summary.yaml>
- Docker Hub usage（pull rate limit 表格、abuse rate limit、fair use）：<https://docs.docker.com/docker-hub/usage/>
- Pulls（multi-arch 每架構算一次）：<https://docs.docker.com/docker-hub/usage/pulls/>
- Image management（Stale 標記、image index）：<https://docs.docker.com/docker-hub/repos/manage/hub-images/manage/>
- OCI artifacts（attestation / SBOM / 簽章的儲存）：<https://docs.docker.com/docker-hub/repos/manage/hub-images/oci-artifacts/>
- Immutable tags（Beta、三種模式、不可刪除）：<https://docs.docker.com/docker-hub/repos/manage/hub-images/immutable-tags/>
- Hub API 參考：<https://docs.docker.com/reference/api/hub/latest/>
- Docker pricing：<https://www.docker.com/pricing/>
- Storage 計費延後公告：<https://www.docker.com/blog/revisiting-docker-hub-policies-prioritizing-developer-experience/>
- Docker Scout（免費 1 repo）：<https://docs.docker.com/scout/>
- `docker image tag` CLI（指向 distribution reference）：<https://docs.docker.com/reference/cli/docker/image/tag/>
- distribution reference（tag / repository 文法）：<https://pkg.go.dev/github.com/distribution/reference>
- Attestation storage（`unknown/unknown` platform）：<https://docs.docker.com/build/metadata/attestations/attestation-storage/>
- SLSA provenance：<https://docs.docker.com/build/metadata/attestations/slsa-provenance/>
- SBOM attestations：<https://docs.docker.com/build/metadata/attestations/sbom/>
- Docker Hub search API（查無官方 namespace 的 fava / beancount image）：`https://hub.docker.com/v2/search/repositories/?query=fava`、`?query=beancount`（2026-09-21）

### 供應鏈簽章

- cosign registry 支援（明列 Docker Hub）：<https://docs.sigstore.dev/cosign/system_config/registry_support/>
- cosign 容器簽章（Docker Hub 不支援刪除）：<https://docs.sigstore.dev/cosign/signing/signing_with_containers/>
- Sigstore CI quickstart（keyless + `id-token: write`）：<https://docs.sigstore.dev/quickstart/quickstart-ci/>
- `sigstore/cosign-installer`（v4.1.2，2026-05-07）：<https://github.com/sigstore/cosign-installer>

### 第三方 action（非官方）

- `peter-evans/dockerhub-description`（v5.0.0，2025-10-01，MIT，維護中）：<https://github.com/peter-evans/dockerhub-description>
