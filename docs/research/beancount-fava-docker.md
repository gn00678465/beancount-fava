# Beancount + Fava Docker Image 研究

查詢日期：2026-09-21。所有版本號以該日的第一手來源為準。

本文的每一項主張都指向官方原始碼、官方文件、PyPI JSON API 或 GitHub API。凡標示「推論」者，代表由已查證的事實推導，但未實際執行驗證。凡標示「未知」者，代表第一手來源沒有答案。

---

## 1. 摘要

### 關鍵結論

| 項目 | 事實 | 日期 |
|---|---|---|
| beancount 最新版 | 3.2.3 | 2026-05-05 上傳 PyPI |
| beancount Python 需求 | `>=3.9`，wheel 涵蓋 cp39–cp314 | — |
| beancount wheel 平台 | manylinux x86_64 / aarch64、macOS、Windows。**沒有 musllinux** | — |
| fava 最新版 | 1.30.16 | 2026-08-18 上傳 PyPI |
| fava Python 需求 | `>=3.10` | — |
| fava 對 beancount 需求 | `beancount>=3.2.0,<4` | — |
| fava wheel | `py3-none-any`，**已含前端建置產物** | — |
| beanquery / beangulp / beanprice | 0.2.0 / 0.2.0 / 2.1.0 | — |

### 建議

1. **單一 image**。fava 以 Python library 方式 `import beancount`，兩者必須在同一個 Python 環境。beancount 本身沒有常駐服務，拆成獨立 container 沒有執行期意義。
2. **base image 用 `python:<版本>-slim-<debian 代號>`，不要用 alpine**。beancount 沒有 musllinux wheel，alpine 會強制從原始碼建置（需 C compiler、meson、flex、bison）。
3. **多架構只做 `linux/amd64` 與 `linux/arm64`**。這兩個架構 beancount 有現成 manylinux wheel。
4. **自動更新用 Renovate 的 `pep621` manager（`pyproject.toml` + `uv.lock`）加上 `dockerfile` manager**，比 Dependabot 更適合，因為它能同時處理 uv lockfile 與 base image，且支援 custom regex manager。
5. **對外 API 不要依賴 fava 的 JSON API**。fava 官方文件明確寫「沒有穩定性保證」，而且沒有任何認證機制。要自建 API 就用 `beancount.loader` + `beanquery`，放在**同一個 image、不同 container**，共用 ledger volume（唯讀）。

---

## 2. 研究問題一：Beancount v3 安裝方式

### 2.1 目前版本與 Python 支援

- 最新版本 **3.2.3**，`upload_time_iso_8601 = 2026-05-05T23:08:24Z`。v3 系列的發佈時間軸：3.0.0（2024-06-22）、3.1.0（2025-01-19）、3.2.0（2025-09-14）、3.2.1（2026-04-29）、3.2.2（2026-04-30）、3.2.3（2026-05-05）。
  來源：<https://pypi.org/pypi/beancount/json>
- `requires-python = ">=3.9"`。
  來源：<https://github.com/beancount/beancount/blob/master/pyproject.toml>
- 執行期相依只有三個：`click >=7.0`、`python-dateutil >=2.6.0`、`regex >=2022.9.13`。
  來源：同上。

### 2.2 PyPI wheel 涵蓋範圍

3.2.3 提供的 wheel（來源：<https://pypi.org/pypi/beancount/json> 的 `urls` 陣列）：

| 平台 | 架構 | 有無 |
|---|---|---|
| manylinux (`manylinux_2_17` / `manylinux_2_28`) | `x86_64` | 有 |
| manylinux (`manylinux_2_17` / `manylinux_2_28`) | `aarch64` | 有 |
| **musllinux** | 任何 | **沒有** |
| macOS | `x86_64`、`arm64` | 有 |
| Windows | `win32`、`win_amd64` | 有 |

Python ABI 標籤涵蓋 `cp39`、`cp310`、`cp311`、`cp312`、`cp313`、`cp314`，並含 free-threaded 的 `cp314t`。

沒有 musllinux wheel 是**刻意**的，不是遺漏。官方 wheel 建置流程明確跳過：

```yaml
- run: cibuildwheel
  env:
    CIBW_SKIP: '*-musllinux*'
```

來源：<https://github.com/beancount/beancount/blob/master/.github/workflows/wheels.yaml>

同一個 workflow 的 `upload` job 也只上傳 `macosx*`、`manylinux*`、`win32*`、`win_amd64*` 與 sdist 這幾個 pattern。

**對 image 的直接影響**：以 Debian 為基礎的 `python:slim`（glibc）可直接取用 manylinux wheel，不需要任何編譯工具鏈。Alpine（musl）會退回 sdist，必須自行建置。

### 2.3 從原始碼建置所需的工具鏈

beancount 的 build backend 是 `mesonpy`：

```toml
[build-system]
build-backend = "mesonpy"
requires = [
    'flex-bin ; sys_platform == "linux" or sys_platform == "darwin"',
    'bison-bin ; sys_platform == "linux" or sys_platform == "darwin"',
    'winflexbison-bin>=2.5.25.1 ; sys_platform == "win32"',
    "meson-python >= 0.14.0",
    "meson >= 1.2.1",
]
```

來源：<https://github.com/beancount/beancount/blob/master/pyproject.toml>

`meson.build` 指定的最低版本：

```
bison = find_program('bison', 'win_bison', version: '>=3.8.0')
flex = find_program('flex', 'win_flex', version: '>=2.6.4')
```

來源：<https://github.com/beancount/beancount/blob/master/meson.build>

重點：**flex 與 bison 由 PyPI 套件自動提供**，不需要用 apt 或 apk 安裝。`flex-bin` 與 `bison-bin` 都有 `musllinux_1_2` wheel：

- `flex-bin` 2.6.4.2：含 `musllinux_1_2_x86_64`、`musllinux_1_2_aarch64`、`musllinux_1_2_i686`、`musllinux_1_2_armv7l`。
  來源：<https://pypi.org/pypi/flex-bin/json>
- `bison-bin` 3.8.2.3：同樣含四種 musllinux wheel。
  來源：<https://pypi.org/pypi/bison-bin/json>

仍然需要的是 **C compiler** 與 **Python development headers**。官方安裝文件寫「Meson 和 Ninja required for building」、「GNU flex 和 GNU bison needed only if modifying the lexer or grammar」、Windows 需要 Visual C++ Build Tools。
來源：<https://docs.google.com/document/d/1FqyrTPwiHVLyncWTf3v5TcooCu9z5JRX8Nm41lVZi0U/edit>（官方鏡像 <https://beancount.github.io/docs/installing_beancount.html>）

> 註：安裝文件說 flex/bison 只在修改 lexer 或 grammar 時需要，但 `pyproject.toml` 的 `build-system.requires` 無條件把 `flex-bin`/`bison-bin` 列為建置相依。以 `pyproject.toml` 為準，因為它是實際被 pip 執行的契約。

官方 CI 確認 sdist 可以直接安裝並產生可用的 CLI：

```yaml
- run: python -m pip install dist/beancount-*.tar.gz
- run: |
    bean-check --help
    bean-doctor --help
    bean-example --help
    bean-format --help
```

來源：<https://github.com/beancount/beancount/blob/master/.github/workflows/install.yaml>

**推論**：在 Alpine 上以 `apk add build-base python3-dev` 加上 pip 自動抓取的 `flex-bin`/`bison-bin`/`meson`/`ninja`，應可成功建置 beancount。未實際執行驗證。

### 2.4 官方文件建議的安裝方式

安裝文件的建議順序（來源同 2.3 的 Google Doc）：

1. `uvx --from beancount <command>`（2026 年的建議做法）
2. `pipx install beancount`
3. `pip install beancount` —— 文件明寫「Installing Beancount using pip is no longer recommended.」

**對 image 的意義**：container 內只有一個應用程式，環境隔離由 container 本身提供，所以 pip / uv 直接裝進系統或單一 venv 都合理。文件的「不建議 pip」是針對開發者主機的全域污染問題，不是針對 container。（推論）

### 2.5 v3 與 v2 的差異：影響 image 的部分

**v3 保留的 CLI**（`[project.scripts]`）：

| 指令 | 進入點 |
|---|---|
| `bean-check` | `beancount.scripts.check:main` |
| `bean-doctor` | `beancount.scripts.doctor:main` |
| `bean-example` | `beancount.scripts.example:main` |
| `bean-format` | `beancount.scripts.format:main` |
| `treeify` | `beancount.tools.treeify:main` |

來源：<https://github.com/beancount/beancount/blob/master/pyproject.toml>

`bin/` 目錄的內容一致：`bean-check`、`bean-doctor`、`bean-example`、`bean-format`、`treeify`。
來源：<https://github.com/beancount/beancount/tree/master/bin>

**被移除或搬走的部分**：

- `bean-report`、`bean-web` 已停用。安裝文件寫「bean-report, bean-web, have been deprecated」。
- README 寫 v3「is trimmed down from v2 and most of the tools the v2 branch included have been moved to their own independent projects on Github」。
  來源：<https://github.com/beancount/beancount/blob/master/README.rst>
- `CHANGES` 記錄 2024-06-16 建立 v3 分支時，同步在 PyPI 上補齊 `beangulp`、`beanquery`、`beanprice`、`beangrow`、`beancount2ledger` 的發佈。
  來源：<https://github.com/beancount/beancount/blob/master/CHANGES>

**拆出去的套件現況**：

| 套件 | PyPI 最新版 | 上傳日期 | requires-python | 提供的 CLI | 對 beancount 的需求 |
|---|---|---|---|---|---|
| `beanquery` | 0.2.0 | 2025-03-24 | `>=3.8` | `bean-query` | `beancount>=2.3.4` |
| `beangulp` | 0.2.0 | 2025-01-20 | `>=3.7`（PyPI metadata 未填） | **無** | `beancount>=2.3.5` |
| `beanprice` | 2.1.0 | 2025-10-18 | `>=3.9` | `bean-price` | `beancount>=3.0.0` |

來源：<https://pypi.org/pypi/beanquery/json>、<https://pypi.org/pypi/beangulp/json>、<https://pypi.org/pypi/beanprice/json>，以及各自的 `pyproject.toml`：
<https://github.com/beancount/beanquery/blob/master/pyproject.toml>、
<https://github.com/beancount/beangulp/blob/master/pyproject.toml>、
<https://github.com/beancount/beanprice/blob/master/pyproject.toml>

`beangulp` 沒有 `[project.scripts]`。v2 的 `bean-identify` / `bean-extract` / `bean-file` 不存在；使用者自行撰寫 import script 並呼叫 `beangulp.Ingest`。

`beanquery` 的 master 分支版本是 `0.3.0.dev0`，尚未發佈。`beangulp` master 同樣是 `0.3.0.dev0`。
來源：同上兩個 `pyproject.toml`。

**容器內的一個陷阱與其解除**：`beangulp` 相依 `python-magic>=0.4.12`（非 Windows），而 `python-magic` 執行期需要系統的 `libmagic`。但 beangulp 對它的 import 有保護：

```python
# python-magic is an optional dependency.
try:
    import magic
except (ImportError, OSError):
    magic = None
```

來源：<https://github.com/beancount/beangulp/blob/master/beangulp/file_type.py>

所以 image 內缺少 `libmagic` 不會造成 import 失敗。`beangulp` 也相依 `lxml`，該套件有 manylinux 與 musllinux wheel。

---

## 3. 研究問題二：Fava 安裝方式

### 3.1 版本與相依

- 最新版 **1.30.16**，2026-08-18 上傳。前兩版：1.30.15（2026-08-15）、1.30.14（2026-06-16）。
  來源：<https://pypi.org/pypi/fava/json>
- `requires-python = ">=3.10"`，classifier 涵蓋 3.10 至 3.14。
- 執行期相依（來源：<https://github.com/beancount/fava/blob/v1.30.16/pyproject.toml>）：

```toml
dependencies = [
  "Babel>=2.11,<3",
  "Flask>=2.2,<4",
  "Flask-Babel>=3,<5",
  "Jinja2>=3,<4",
  "Werkzeug>=2.2,<4",
  "beancount>=3.2.0,<4",
  "beangulp>=0.2",
  "beanquery>=0.1,<0.3",
  "cheroot>=8,<12",
  "click>=7,<9",
  "markdown-it-py>=3,<5",
  "ply>=3.4",
  "simplejson>=3.16.0,<4",
  "typing-extensions>=4.5; python_version<\"3.12\"",
  "watchfiles>=0.20.0",
]
```

- 選用 extra：`fava[excel]` 加入 `pyexcel`、`pyexcel-ods3`、`pyexcel-xlsx`。

### 3.2 對 beancount v3 的支援起點

- **v1.30（2024-12-29）**：「Support for Beancount version 3 was added. Using Beancount 2 is still supported. Beancount query support is now provided by the beanquery package…」
- **v1.30.13（2026-05-19）**：「This version drops support for Beancount version 2.」

來源：<https://github.com/beancount/fava/blob/main/CHANGES>

現行 1.30.16 的相依宣告 `beancount>=3.2.0,<4`，與上述紀錄一致。所以「必須用 v3」這個硬性限制和 fava 的方向相符，不需要任何相容層。

### 3.3 PyPI wheel 是否含前端建置產物

**含。** 已實際下載驗證：

- wheel `fava-1.30.16-py3-none-any.whl`：181 個檔案，包含 `fava/static/app.js`、`fava/static/app.js.map`、`fava/static/app.css`、`fava/static/app.css.map`，以及 18 個編譯好的 `.mo` 翻譯檔。不含 `frontend/` 原始碼。
- sdist `fava-1.30.16.tar.gz`：567 個檔案，同時含 `src/fava/static/app.js` 與 `frontend/src/**`。

這與 `pyproject.toml` 的宣告一致：

```toml
[tool.hatch.build]
# The frontend and translations are generated by the build hook below
# and hence not tracked in git - include them in the sdist and wheel.
artifacts = [
  "/src/fava/static/*",
  "/src/fava/translations/**/*.mo",
]
```

來源：<https://github.com/beancount/fava/blob/v1.30.16/pyproject.toml>

**結論：`pip install fava` 不需要 Node。** image 不需要 Node build stage。

### 3.4 從原始碼建置是否需要 Node

**需要。** build hook 會呼叫 npm：

```python
npm = shutil.which("npm")
if npm is None:
    msg = "npm is missing"
    raise RuntimeError(msg)
...
subprocess.run((npm, "install", "--no-save", "--strict-allow-scripts"), cwd=frontend, check=True)
subprocess.run((npm, "run", "build"), cwd=frontend, check=True)
```

來源：<https://github.com/beancount/fava/blob/v1.30.16/hatch_build.py>

官方發佈流程也確實先跑 Node：`actions/setup-node` → `cd frontend && npm ci` → `uv build`。
來源：<https://github.com/beancount/fava/blob/main/.github/workflows/publish.yml>

同一個 hook 有 mtime 短路：若 `src/fava/static/app.js` 存在且比所有 frontend 來源新，就跳過 npm。sdist 內已含 `app.js`，**推論**從 sdist 建置在多數情況下會走短路而不需要 npm，但 tar 解壓後的 mtime 順序不保證，所以不應依賴。要避開這個不確定性，image 一律裝 wheel。

### 3.5 執行方式、CLI 選項與環境變數

進入點：`fava = "fava.cli:main"`。所有選項來自 <https://github.com/beancount/fava/blob/v1.30.16/src/fava/cli.py>。

| 選項 | 預設 | 說明 |
|---|---|---|
| `FILENAMES`（位置參數） | — | `click.Path(exists=True, dir_okay=False, resolve_path=True)` |
| `-p, --port <port>` | `5000` | 監聽埠 |
| `-H, --host <host>` | `localhost` | 監聽位址 |
| `--prefix <str>` | 無 | 設定 URL prefix |
| `--incognito` | 關 | 隱藏所有數字 |
| `--read-only` | 關 | 唯讀模式 |
| `-d, --debug` | 關 | 除錯模式 |
| `--profile` | 關 | 效能剖析，隱含 `--debug` |
| `--profile-dir <path>` | 無 | 剖析資料輸出目錄 |
| `--poll-watcher` | 關 | 改用輪詢式檔案監看 |
| `--version` | — | 顯示版本 |

**環境變數**：

1. `BEANCOUNT_FILE`：額外的 ledger 檔案清單，POSIX 用 `:` 分隔、Windows 用 `;` 分隔（實作是 `os.pathsep`）。**路徑必須是絕對路徑**，否則丟出 `NonAbsolutePathError`：

```python
for name in env_names:
    if not Path(name).is_absolute():
        raise NonAbsolutePathError(name)
```

2. 所有 CLI 選項都可用 `FAVA_` 前綴的環境變數設定。這由 click 的 `context_settings={"auto_envvar_prefix": "FAVA"}` 提供。docstring 明寫：「`--host=0.0.0.0` is equivalent to setting the environment variable `FAVA_HOST=0.0.0.0`」。

### 3.6 容器內需要注意的事

**（a）必須綁 `0.0.0.0`。** 預設 `host="localhost"`，而且程式碼會把 `localhost` 轉成 `127.0.0.1`：

```python
# ensure that cheroot does not use IP6 for localhost
host = "127.0.0.1" if host == "localhost" else host
```

綁在 `127.0.0.1` 的 container 無法從外部連入。解法：`FAVA_HOST=0.0.0.0` 或 `-H 0.0.0.0`。fava 官方的 contrib Dockerfile 正是這樣做（見 3.7）。

**（b）啟動時 ledger 檔案必須已存在。** `click.Path(exists=True, dir_okay=False)` 會在參數解析階段檢查。若 volume 尚未掛載或路徑打錯，container 會直接以 usage error 退出。

**（c）fava 會寫入 ledger 檔。** `json_api` 有 `put_source`、`put_source_slice`、`delete_source_slice`、`put_add_entries`、`put_add_document`、`put_move`、`delete_document`、`put_upload_import_file` 等寫入端點。所以：

- 掛載的 volume 必須對 container 內的執行使用者可寫。
- 若以非 root 使用者執行，host 端的 uid/gid 必須對得上，否則寫入失敗。
- fava 寫的是**檔案內容**，不是原子換檔（未逐一查證每個寫入路徑，標示為未知，見第 8 節）。

**（d）唯讀模式的實際行為**：非 GET 請求一律 `abort(401)`。

```python
if read_only:
    # Prevent any request that isn't a GET if read-only mode is active
    @fava_app.before_request
    def _read_only() -> None:
        if request.method != "GET":
            abort(401)
```

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/application.py>

搭配 `--read-only` 時，volume 可用 `:ro` 掛載。

**（e）檔案監看**：預設用 `watchfiles`（inotify）。CHANGES 記載「the watchfiles based watcher might not work correctly in some setups with network file systems」，此時改用 `--poll-watcher`。對 Docker Desktop 的 bind mount 或 NFS volume，可能需要這個選項。（來源：<https://github.com/beancount/fava/blob/main/CHANGES> v1.29 段落）

**（f）PID 1 與訊號處理**：fava 用 cheroot server，`server.start()` 只攔截 `KeyboardInterrupt`（SIGINT），沒有 SIGTERM handler。以 PID 1 執行時 SIGTERM 的預設處置被忽略，`docker stop` 會等到 timeout 才 SIGKILL。（此為 Linux PID 1 語意加上原始碼觀察的**推論**。）社群 image `tarioch/docker-fava` 用 `tini` 解決，其 Dockerfile 註解寫「fava does not handle SIGTERM as PID 1, tini forwards the stop signal and reaps child processes」，且 CI 有對應的 smoke test 檢查 exit code 不是 137。
來源：<https://github.com/tarioch/docker-fava/blob/master/Dockerfile>、<https://github.com/tarioch/docker-fava/blob/master/.github/workflows/dockerimage.yml>

**（g）反向代理**：若掛在子路徑下，用 `--prefix`。官方部署文件的 Apache 範例：`ProxyPass "/fava" "http://localhost:5000/fava"` 配 `fava --prefix /fava`。
來源：<https://github.com/beancount/fava/blob/main/contrib/deployment.rst>

### 3.7 Fava repo 內的官方 Dockerfile

**有。** 位置：`contrib/docker/`，含 `Dockerfile` 與 `README.md`。

```dockerfile
FROM python:slim as builder

RUN pip install --root-user-action ignore --prefix="/install" fava

FROM python:slim

COPY --from=builder /install /usr/local

ENV FAVA_HOST "0.0.0.0"
EXPOSE 5000
CMD fava
```

來源：<https://github.com/beancount/fava/blob/main/contrib/docker/Dockerfile>

`README.md` 給出的執行指令：

```
docker run --detach --name="beancount" --publish 5000:5000 \
  --volume $(pwd)/example.beancount:/input.beancount \
  --env BEANCOUNT_FILE=/input.beancount fava
```

來源：<https://github.com/beancount/fava/blob/main/contrib/docker/README.md>

**這份 Dockerfile 的定位與限制**：

- 它在官方 repo 內，但放在 `contrib/`，且**不在**發佈文件的 toctree（`docs/index.rst` 只含 `usage`、`changelog`、`development`、`api`）。來源：<https://github.com/beancount/fava/blob/main/docs/index.rst>
- 最近一次實質修改是 2025-05-07（commit「docker: use slim image as base, add pip flag」），再上一次實質修改是 2021-06-20。來源：GitHub commits API，`repos/beancount/fava/commits?path=contrib/docker`。
- 它**沒有**版本釘選（`pip install fava` 抓最新）、**沒有**非 root 使用者、**沒有** `HEALTHCHECK`、`CMD fava` 是 shell form、依賴 `BEANCOUNT_FILE` 在執行時提供。
- `README.md` 的「Advanced」章節建議用 `bitly/oauth2_proxy` 加 Let's Encrypt 做認證，但該段引用的 `bitly/oauth2_proxy` 與 `JrCs/docker-letsencrypt-nginx-proxy-companion` 都是多年前的專案，內容已過時。

**沒有官方發佈的 fava container image。** `contrib/docker` 只提供 Dockerfile，repo 內三個 workflow（`docs-pages.yml`、`publish.yml`、`test.yml`）都不建置或推送 image。
來源：<https://github.com/beancount/fava/tree/main/.github/workflows>

### 3.8 社群 image（非官方，僅供參考）

以下皆為**非官方**，列出僅作為設計參考。

| Repo | 狀態 | 觀察 |
|---|---|---|
| `yegle/fava-docker` | 最後推送 2025-07-06，119 stars | `ARG BEANCOUNT_VERSION=2.3.6`、`ARG FAVA_VERSION=v1.30.4`。**仍釘在 beancount v2**，不符合 v3 硬性限制。從 git 原始碼建置，用 Node build stage，最終用 `gcr.io/distroless/python3-debian12`。來源：<https://github.com/yegle/fava-docker/blob/master/Dockerfile> |
| `tarioch/docker-fava` | 最後推送 2026-09-20 | 目前最活躍。用 `ghcr.io/astral-sh/uv:0.12.17-python3.14-trixie-slim` 加 digest 釘選，`uv sync --locked` 從 `pyproject.toml` + `uv.lock` 安裝，`tini` 作 init，CI 有 smoke test 並推送到 Docker Hub `tarioch/fava`。來源：<https://github.com/tarioch/docker-fava> |
| `Evernight/lazy-beancount` | 最後推送 2026-06-03，174 stars | 整合型發行版，不只 fava。未細查。 |
| `DIYgod/docker-fava` | 最後推送 2026-08-24 | 未細查。 |

來源：GitHub search API `search/repositories?q=fava+docker`。

`tarioch/docker-fava` 的 metadata-action tag 策略值得參考：

```yaml
tags: |
  type=raw,value=latest,enable={{is_default_branch}}
  type=raw,value={{date 'YYYYMMDDHHmmss'}}{{sha}},enable={{is_default_branch}}
  type=semver,pattern={{version}}
```

---

## 4. 研究問題三：Image 架構建議

### 4.1 事實基礎

1. **fava 以 Python library 方式相依 beancount**。`pyproject.toml` 把 `beancount>=3.2.0,<4` 列在 `dependencies`，不是 `[project.optional-dependencies]`，也不是外部程序呼叫。
   來源：<https://github.com/beancount/fava/blob/v1.30.16/pyproject.toml>
2. **兩者必須在同一個 Python 環境**。fava 原始碼直接 `from beancount.core import flags` 這類 import（例如 `fava/core/ingest.py` 的 `from beangulp.importer import Importer`）。
3. **beancount 沒有常駐服務**。v3 的 `[project.scripts]` 全部是一次性 CLI（`bean-check`、`bean-doctor`、`bean-example`、`bean-format`、`treeify`），沒有 server。v2 的 `bean-web` 已停用。
4. **fava 是唯一的長時間執行程序**，由 cheroot WSGI server 提供服務。

### 4.2 三種方案的取捨

| 方案 | 優點 | 缺點 | 適用 |
|---|---|---|---|
| **A. 單一 image** | 最少的建置步驟；版本組合在建置時就固定；image 層數少；執行期只有一個 Python 環境，不可能版本錯配 | fava 與 beancount 任一更新都要重建整個 image | 本專案 |
| **B. base image（beancount）+ 衍生 image（fava）** | beancount 層可被其他 image 共用（例如未來的 API 服務）；beancount 更新頻率低，layer cache 命中率高 | 兩個 image 的版本矩陣要管理；CI 變成兩條 pipeline；beancount-only image 沒有可執行的服務，只能當基底；tag 策略複雜化 | 若未來確實要有第二個消費者 |
| **C. 完全分離（兩個 container）** | — | **技術上不可行**。fava 不透過網路或 IPC 呼叫 beancount，它直接 import。分離的 beancount container 沒有任何介面可供 fava 使用 | 不適用 |

### 4.3 建議：單一 image

理由：

- 方案 C 與觀察到的相依契約矛盾，直接排除。
- 方案 B 的「共用」效益在目前只有一個消費者（fava）時不存在。beancount 的三個執行期相依（click、python-dateutil、regex）都是純 Python，安裝成本極低；wheel 直接下載，沒有編譯。分層省下的建置時間有限，卻換來兩倍的版本矩陣與發佈流程。
- 若第 7 節的自建 API 服務成真，它與 fava 需要的是**同一組 Python 套件**（beancount + beanquery）。屆時最簡單的做法是**同一個 image、不同的 `CMD`**，而不是拆出 base image。

**若未來需要 base image**，觸發條件應該是：出現第二個消費者，且它的 Python 相依集合與 fava **顯著不同**（例如 API 服務要 FastAPI + uvicorn 而 fava 不要）。在那之前不要預先分層。

### 4.4 Base image 選擇

**建議：`python:3.13-slim-trixie` 這類「明確版本 + slim + 明確 Debian 代號」的 tag。**

| 候選 | 評估 |
|---|---|
| `python:<ver>`（完整版，buildpack-deps 基礎） | 含大量開發套件，體積大。beancount 與 fava 都有 wheel，不需要這些 | 
| **`python:<ver>-slim-<代號>`** | **建議**。glibc，可直接用 beancount 的 manylinux wheel。官方文件警告 slim「pip install may fail when installing a Python distribution package from a source distribution」，但本專案兩個套件都裝 wheel，不受影響 |
| `python:<ver>-alpine` | **不建議**。musl libc。beancount 沒有 musllinux wheel，必須從 sdist 建置，需要 C compiler、meson、ninja。官方 docker 文件也警告 alpine「uses musl libc instead of glibc and friends, so software will often run into issues」 |

來源：<https://github.com/docker-library/docs/blob/master/python/README.md>

**Python 版本選擇**：fava 要求 `>=3.10`，beancount wheel 涵蓋到 cp314。交集是 3.10–3.14。建議選 **3.13** 或 **3.14**：兩者 beancount 3.2.3 都有完整的 manylinux x86_64 與 aarch64 wheel，fava 的 classifier 也都涵蓋。3.14 是 fava 官方發佈 workflow 使用的版本（`python-version: "3.14"`）。

**明確指定 Debian 代號**（`-trixie` 而非只寫 `-slim`）的理由：官方文件建議「specifying these explicitly to minimize breakage during new Debian releases」。

### 4.5 Multi-stage build

即使不需要編譯，multi-stage 仍有價值：

- **builder stage**：安裝套件到獨立 prefix 或 venv，含 pip / uv 的 cache 與 metadata。
- **runtime stage**：只 `COPY` 安裝結果，不帶 pip cache、wheel 暫存、build metadata。

官方 contrib Dockerfile 就是這個結構（`--prefix="/install"` 然後 `COPY --from=builder /install /usr/local`）。

若改用 uv，可參考 `tarioch/docker-fava` 的 `UV_PROJECT_ENVIRONMENT=/opt/venv` + `uv sync --locked` 模式，好處是 lockfile 提供可重現的完整相依樹，且直接對應 Renovate 的 `pep621` manager（見第 6 節）。

### 4.6 非 root 使用者

必要，但要處理寫入權限。

- fava 會寫 ledger 檔（見 3.6c），所以掛載的 volume 對 container 內使用者必須可寫。
- 建立固定 uid/gid（例如 `1000:1000`）並以 `USER` 指定，使用者再用 `docker run --user` 或調整 host 端權限對齊。
- 若採唯讀部署（`--read-only` + volume `:ro`），非 root 沒有任何額外成本。

`EXPOSE 5000` 是非特權埠，不需要 root 綁定。

### 4.7 多架構

**建議：`linux/amd64` + `linux/arm64`。**

理由：beancount 3.2.3 對這兩個架構都有現成的 manylinux wheel（`manylinux_2_17_x86_64` / `manylinux_2_28_x86_64` 與 `manylinux2014_aarch64` / `manylinux_2_17_aarch64` / `manylinux_2_28_aarch64`）。fava 是 `py3-none-any`，不受架構限制。

其他架構（armv7、ppc64le、s390x、riscv64）beancount 沒有 wheel，會觸發原始碼建置。除非有明確需求，不要納入。

建置方式：`docker/setup-qemu-action` + `docker/setup-buildx-action` + `docker/build-push-action` 的 `platforms: linux/amd64,linux/arm64`。若要避開 QEMU 模擬的建置速度問題，可改用 native arm64 runner（GitHub 有 `ubuntu-24.04-arm`，beancount 官方 wheel workflow 就用它）。

---

## 5. 研究問題四：上游版本更新時自動更新 image

### 5.1 需要追蹤的上游

1. `fava`（PyPI）
2. `beancount`（PyPI）
3. `beanquery`、`beangulp`、`beanprice`（PyPI，若一併安裝）
4. base image `python:*-slim-*`（container registry）
5. GitHub Actions 的 action 版本

### 5.2 方案比較

#### （a）Renovate —— 建議

**`pep621` manager**：比對 `pyproject.toml`（regex `/(^|/)pyproject\.toml$/`），支援 uv 與 `uv.lock`、pdm 與 `pdm.lock`、hatch、pixi。可更新 `project.dependencies`、`optional-dependencies`、`build-system.requires` 與 tool-specific dev dependencies。datasource 用 `pypi`。
來源：<https://docs.renovatebot.com/modules/manager/pep621/>

**`pip_requirements` manager**：比對 `/(^|/)[\w-]*requirements([-._]\w+)?\.(txt|pip)$/`，datasource 支援 `pypi` 與 `git-tags`。
來源：<https://docs.renovatebot.com/modules/manager/pip_requirements/>

**`dockerfile` manager**：比對 `/(^|/|\.)([Dd]ocker|[Cc]ontainer)file$/` 與 `/(^|/)([Dd]ocker|[Cc]ontainer)file[^/]*$/`。更新 `FROM`（含 multi-stage）、`COPY --from`、`RUN --mount`、syntax directive，以及 `RUN` 內的 apk / deb 套件。datasource 為 `docker`、`apk`、`deb`。
來源：<https://docs.renovatebot.com/modules/manager/dockerfile/>

**custom regex manager**：可更新任何以註解標記的版本字串，例如 Dockerfile 內的 `ARG`：

```json
{
  "customManagers": [
    {
      "customType": "regex",
      "managerFilePatterns": ["/(^|/|\\.)Dockerfile$/"],
      "matchStrings": [
        "# renovate: datasource=(?<datasource>[a-z-]+?) packageName=(?<packageName>.+?)(?: versioning=(?<versioning>[a-z-]+?))?\\s(?:ENV|ARG) .+?_VERSION=(?<currentValue>.+?)\\s"
      ]
    }
  ]
}
```

搭配 Dockerfile：

```dockerfile
# renovate: datasource=pypi packageName=fava
ARG FAVA_VERSION=1.30.16
```

來源：<https://docs.renovatebot.com/modules/manager/regex/>

#### （b）Dependabot

- `dependabot.yml` 必要欄位：`version: 2`、`updates`、`package-ecosystem`、`directory`（或 `directories`）、`schedule.interval`（daily / weekly / monthly / quarterly / semiannually / yearly / cron）。
- 相關的 ecosystem：`pip`（支援 requirements 檔與 `pyproject.toml`）與 `docker`（更新指定目錄下 Dockerfile 的 base image）。
  來源：<https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference>

**限制**：Dependabot 沒有等同 Renovate custom regex manager 的機制，無法更新 Dockerfile 裡純粹用 `ARG` 表示的 PyPI 版本。要用 Dependabot 追蹤 PyPI 版本，就必須在 repo 內有 `requirements.txt` 或 `pyproject.toml` 形式的 manifest。

#### （c）排程 GitHub Actions 輪詢

自寫 workflow（`on: schedule`），定期呼叫 <https://pypi.org/pypi/fava/json> 與 <https://pypi.org/pypi/beancount/json>，比對 `info.version` 與目前釘選值，有差異就建置並推送。

- 優點：完全可控；可直接產生「fava 版本 × beancount 版本」的組合 tag。
- 缺點：要自行維護狀態比對、PR 或 commit 邏輯、錯誤處理與重試。等於重做 Renovate 已解決的問題。

#### 建議

**用 Renovate 為主，排程 workflow 為輔。**

1. 在 repo 放 `pyproject.toml` + `uv.lock`（釘選 fava、beancount 與其他套件），讓 Renovate 的 `pep621` manager 追蹤並同步更新 lockfile。這也讓 image 的相依完全可重現。
2. Dockerfile 的 `FROM` 交給 `dockerfile` manager，取得 base image 更新（含 Debian 安全性更新帶來的新 tag / digest）。
3. GitHub Actions 的 action 版本交給 Renovate 的 `github-actions` manager。
4. 額外加一個 **`on: schedule` 的週期性重建 workflow**，即使沒有任何版本變動，也定期用同一組釘選版本重新建置並推送。理由：base image 的同一個 tag 會在原地更新以納入 OS 安全性修補，Renovate 若不啟用 digest 釘選就不會產生 PR。若啟用 digest 釘選（`pinDigests`），則 Renovate 會在 digest 變動時發 PR，這時週期性重建可省略。二擇一即可，不要兩套都開。

### 5.3 Image tag 策略

建議同時提供三類 tag：

| Tag 形式 | 範例 | 用途 |
|---|---|---|
| 完整組合（immutable） | `1.30.16-beancount3.2.3` | 明確釘選，可重現。回應任務中提出的 `<fava版本>-beancount<版本>` 格式 |
| fava 版本（滾動） | `1.30.16`、`1.30` | 只在意 fava 版本的使用者 |
| `latest` | `latest` | 最新建置 |

若採 5.2 建議的週期性重建，同一組版本會被重建多次。此時完整組合 tag 會被覆蓋。**建議額外加上一個真正不可變的 build tag**，例如 `1.30.16-beancount3.2.3-20260921`（日期）或帶 commit sha，讓需要精確回溯的使用者有可釘選的對象。`tarioch/docker-fava` 用的 `{{date 'YYYYMMDDHHmmss'}}{{sha}}` 就是這個作用。

`docker/metadata-action` 支援的 tag type：`type=raw`、`type=semver`、`type=sha`、`type=ref` 等，並可用 `enable={{is_default_branch}}` 條件化。實例見 <https://github.com/tarioch/docker-fava/blob/master/.github/workflows/dockerimage.yml>。

> 注意：`type=semver` 讀的是 git tag。若要讓 image tag 反映 **fava 的上游版本**而非本 repo 的 git tag，需要自行把版本讀進 workflow 的 output，再用 `type=raw,value=${{ steps.xxx.outputs.fava_version }}` 產生。

### 5.4 發佈到 GHCR / Docker Hub 的官方 Actions（2026-09-21 最新版）

| Action | 最新 release | 發佈日期 |
|---|---|---|
| `docker/build-push-action` | **v7.4.0** | 2026-09-15 |
| `docker/metadata-action` | **v6.2.0** | 2026-07-02 |
| `docker/login-action` | **v4.6.0** | 2026-07-29 |
| `docker/setup-buildx-action` | **v4.4.1** | 2026-09-16 |
| `docker/setup-qemu-action` | **v4.4.0** | 2026-09-15 |

來源：GitHub API `repos/<owner>/<repo>/releases/latest`，查詢日期 2026-09-21。

GitHub 官方教學的流程（來源：<https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images>）：

1. `docker/login-action` 認證。GHCR 用 `registry: ghcr.io`、`username: ${{ github.actor }}`、`password: ${{ secrets.GITHUB_TOKEN }}`；Docker Hub 用帳密 secret。
2. `docker/metadata-action` 產生 tags 與 labels。
3. `docker/build-push-action` 建置並以 `push: true` 推送，tags / labels 取自上一步的 output。

**權限**：推送到 GHCR 需要 workflow 的 `permissions: packages: write`。Docker Hub 則靠儲存的憑證。

**同時推兩個 registry**：在 `metadata-action` 的 `images` 同時列出兩個 namespace，再用單一 build-push step 一次推送。

### 5.5 Base image 更新時的重建

兩條可行路線，擇一：

- **路線一（建議）：Renovate 啟用 `pinDigests`**。Dockerfile 的 `FROM` 寫成 `python:3.13-slim-trixie@sha256:...`。base image 在同 tag 原地更新時 digest 改變，Renovate 發 PR，合併即觸發重建。好處是每次重建都有 git 上的紀錄，可追溯。`tarioch/docker-fava` 就採用 digest 釘選。
- **路線二：`on: schedule` 週期性重建**。不釘 digest，定期以同樣的 Dockerfile 重建。好處是設定簡單，壞處是「這個 image 用的是哪個 base」無法從 git 得知。

不要兩條都開，否則會有兩組來源不同的重建觸發，難以判斷某個 image 是哪一條路線產生的。

---

## 6. 研究問題五：額外開發對外 API 的可行路線

### 6.1 Fava 既有的 JSON API

**掛載位置**：blueprint 註冊在 `/<bfile>/api`。

```python
fava_app.register_blueprint(json_api, url_prefix="/<bfile>/api")
```

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/application.py>

`<bfile>` 是 ledger 的 slug。所以完整路徑形如 `GET /<bfile>/api/journal`。

**路由產生規則**：函式名稱第一個底線之前是 HTTP method，之後是 endpoint 名稱。GET 與 DELETE 的參數取自 query string，PUT 取自 JSON body。

```python
method, _, name = func.__name__.partition("_")
if method not in {"get", "delete", "put"}:
    raise ValueError(msg)
...
@json_api.route(f"/{name}", methods=[method])
```

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py>

**endpoint 清單（v1.30.16）**：

讀取類（GET）：
`changed`、`errors`、`ledger_data`、`payee_accounts`、`query`、`extract`、`context`、`source_slice`、`payee_transaction`、`narration_transaction`、`narrations`、`source`、`journal`、`journal_page`、`events`、`imports`、`documents`、`options`、`help`、`commodities`、`income_statement`、`balance_sheet`、`trial_balance`、`account_report`、`statistics`

寫入類（PUT）：
`move`、`source`、`source_slice`、`format_source`、`add_document`、`attach_document`、`add_entries`、`upload_import_file`

刪除類（DELETE）：
`source_slice`、`document`

來源：同上檔案，函式定義位置逐一確認。

**穩定性承諾：官方沒有任何說明。** JSON HTTP API 沒有官方文件，也沒有穩定或不穩定的聲明。

`docs/api.rst` 的「There's no stability guarantee as this is just for internal purposes currently.」說的是 **Python 模組 API**。該文件的 toctree 只 glob `api/fava*`，內容由 `docs/generate.py` 以 `automodule` 從 docstring 產生。它不是 HTTP API 的聲明，不能引用為 JSON API 的穩定性依據。（2026-09-21 複核更正：本段原先把這句話當成 JSON API 的聲明。）

來源：<https://github.com/beancount/fava/blob/main/docs/api.rst>、<https://github.com/beancount/fava/blob/main/docs/generate.py>

實際變動頻率：`src/fava/json_api.py` 在 2025-07 至 2026-08 之間有約 20 個 commit，多數隨前端重構而改（例如 2026-08-25「refactor de-/serialisation using msgspec」在 v1.30.16 發佈後一週改了 `put_add_entries` 的簽章）。來源：<https://github.com/beancount/fava/commits/main/src/fava/json_api.py>。推論：這組 API 隨前端演進，跨版本不保證相容；釘選 fava 版本即可固定行為。

**認證機制：沒有。** 在 `src/` 下以 GitHub code search 查 `auth`，結果 0 筆。`application.py` 的 `_setup_filters` 只有兩個 `before_request`：檔案變更檢查與 `--read-only` 的非 GET 攔截，沒有任何身分驗證。

`contrib/docker/README.md` 承認這點，它的「Advanced」章節整段在教如何用 oauth2_proxy 從外部補上認證。

**結論**：fava JSON API 是給 fava 自己的前端用的內部介面。可以用，但要接受：沒有文件、跨版本可能改變（需釘選 fava 版本）、無認證、路徑綁 ledger slug、寫入端點權限與 UI 完全相同（沒有細分權限）。

### 6.2 Fava extension 機制可否新增 endpoint

**可以。** fava 提供 `@extension_endpoint` decorator：

```python
def extension_endpoint(
    func_or_endpoint_name: (Callable[[T], Any] | str | None) = None,
    methods: list[str] | None = None,
) -> ...:
    """Decorator to mark a function as an endpoint.

    Can be used as `@extension_endpoint` or
    `@extension_endpoint(endpoint_name, methods)`.
    """
```

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/ext/__init__.py>

對應的路由：

```python
@fava_app.route(
    "/<bfile>/extension/<extension_name>/<endpoint>",
    methods=["GET", "POST", "PUT", "DELETE"],
)
def extension_endpoint(extension_name: str, endpoint: str) -> Response:
    ext = g.ledger.extensions.get_extension(extension_name)
    key = (endpoint, request.method)
    if ext is None or key not in ext.endpoints:
        return abort(404)
    response = ext.endpoints[key](ext)
```

來源：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/application.py>

**機制要點**：

- extension 是繼承 `FavaExtensionBase` 的類別，由 ledger 檔內的 `fava-extension` 選項載入（`find_extensions` 用 `importlib.import_module`，並把 `base_path` 插入 `sys.path`）。
- 支援的 method：GET、POST、PUT、DELETE。注意 POST 在 extension 可用，但核心 `json_api` 不支援 POST。
- extension hook：`after_load_file`、`before_request`、`after_entry_modified`、`after_insert_entry`、`after_delete_entry`、`after_insert_metadata`、`after_write_source`。
- extension 可帶自己的 Jinja templates 與 JS module（`has_js_module`）。

**對 image 的影響**：extension 是使用者提供的 Python 檔，路徑由 ledger 檔指定。若要支援，image 需要讓 extension 目錄可被掛載，且該路徑在 container 內可讀。extension 若有額外的 Python 相依，image 內裝不到，使用者得自行擴充 image。這是一個**實質的設計決定**：若要對外提供 extension 支援，就得接受 image 不能是封閉的。

**限制**：extension endpoint 一樣沒有認證，一樣掛在 fava 的 Flask app 底下，繼承 `--read-only` 的非 GET 攔截（`_read_only` 是全域 `before_request`，會一併擋掉 extension 的 POST/PUT/DELETE）。

### 6.3 自建 API：`beancount.loader` + `beanquery`

**可行。**

- beancount v3 提供公開的 `beancount.loader` 模組（在 `meson.build` 的 `py.install_sources` 清單內，是正式安裝的模組）。另有 `beancount/api.py`。
  來源：<https://github.com/beancount/beancount/blob/master/meson.build>
- `beanquery` 提供 BQL 查詢，並輸出 `bean-query` CLI（`beanquery.shell:main`）。fava 自己就是這樣用的：`json_api.get_query` 轉呼 `g.ledger.query_shell.execute_query_serialised(...)`。

**beanquery 現況**：

- PyPI 最新 **0.2.0**，上傳 **2025-03-24**。master 分支是 `0.3.0.dev0`，尚未發佈。
- `requires-python = ">= 3.8"`，classifier 涵蓋 3.8–3.13（**沒有** 3.14 的 classifier）。
- 相依：`beancount>=2.3.4`、`click>7.0`、`python-dateutil>=2.6.0`、`tatsu-lts`。
- 純 Python wheel（`beanquery-0.2.0-py3-none-any.whl`）。
- fava 釘 `beanquery>=0.1,<0.3`，所以 0.2.0 在範圍內；beanquery 0.3.0 發佈時會超出 fava 的上限，屆時需要 fava 先放寬。**這是一個未來會出現的版本衝突點，值得在自動更新規則中預留。**

來源：<https://pypi.org/pypi/beanquery/json>、<https://github.com/beancount/beanquery/blob/master/pyproject.toml>

**注意**：beanquery 距今（2026-09-21）已約 18 個月未發佈新版，classifier 也未涵蓋 Python 3.14。若 image 選用 Python 3.14，beanquery 能否正常運作未經第一手來源確認，標為未知（見第 8 節）。選 Python 3.13 可避開這個不確定性。

**FastAPI 自建的技術可行性**：

- beancount 的載入是同步阻塞的。`loader.load_file()` 會解析整個 ledger。在 ASGI 框架內要放到 threadpool 或啟動時預載並快取，否則會阻塞 event loop。（推論，未驗證實際載入耗時）
- 需要自行處理檔案變更偵測（fava 用 `watchfiles`，自建服務要自己做或同樣用 `watchfiles`）。
- 需要自行設計認證。這正是相對於 fava JSON API 的主要優勢。

### 6.4 對 image 架構的影響

三種擺放方式：

| 方案 | 說明 | 優點 | 缺點 |
|---|---|---|---|
| **A. 同一個 image、同一個 container、同一個程序** | 把 API 掛進 fava 的 Flask app（用 extension endpoint，或自行 mount WSGI） | 只有一個程序；ledger 只載入一次，記憶體省 | 與 fava 生命週期綁死；fava 升級可能打破 extension；`--read-only` 會一併攔截 API 的寫入；API 與 UI 共用埠，無法分別套用網路政策 |
| **B. 同一個 image、同一個 container、兩個程序** | 用 supervisor 之類的 process manager 同時跑 fava 與 API | 部署單元只有一個 | container 內跑多程序違反單一職責；訊號處理、log 收集、健康檢查都變複雜；任一程序掛掉不會讓 container 退出 |
| **C. 同一個 image、兩個 container、共用 ledger volume** | 同一個 image 用不同 `CMD`（或不同 entrypoint 參數）啟兩個 container | 可分別 scale、分別設定網路政策與資源限制；API 可掛唯讀 volume；fava 升級不影響 API；單一 image 維護成本 | ledger 在兩個程序內各載入一份，記憶體加倍；寫入一致性要靠約定（只讓一方寫） |

**建議：方案 C。**

理由：

- 保住單一 image 的維護優勢（一份 Dockerfile、一組版本釘選、一條 CI pipeline），同時取得程序隔離。
- API 服務可用 `:ro` 掛載 ledger volume，把「不會寫壞帳本」變成執行期強制，而不是靠程式碼自律。
- 讓 fava 是唯一的寫入者，避開兩個程序同時寫同一個檔案的並行問題。beancount 的 ledger 是純文字檔，沒有鎖機制；兩個寫入者會直接互相覆蓋。
- 兩個 container 各自是 PID 1 單一程序，訊號、log、健康檢查都維持標準語意。

**記憶體代價**：ledger 載入兩份。對個人規模的帳本可接受。若帳本很大，再考慮方案 A。實際記憶體用量未測，標為未知。

**不建議 B**：多程序 container 帶來的運維複雜度，換不到方案 C 沒有的好處。

**不建議 A 作為主路線**：fava JSON API 與 extension 機制都沒有穩定性承諾（6.1），把對外 API 建在上面等於把外部契約綁在 fava 的內部介面上。

---

## 7. 建議方案彙整

### 7.1 Image

- 單一 image。
- base：`python:3.13-slim-trixie`（明確版本、明確 Debian 代號；選 3.13 以避開 beanquery 未宣告 3.14 支援的不確定性）。
- multi-stage：builder 安裝到獨立 venv/prefix，runtime 只 COPY 結果。
- 相依管理：repo 內放 `pyproject.toml` + `uv.lock`，用 `uv sync --locked` 安裝，取得可重現的完整相依樹。
- 非 root 使用者，固定 uid/gid。
- 加 init（tini 或 `docker run --init`）處理 SIGTERM。
- 預設環境：`FAVA_HOST=0.0.0.0`、`EXPOSE 5000`。
- ledger 路徑透過 `BEANCOUNT_FILE`（**必須絕對路徑**）或位置參數提供。
- 平台：`linux/amd64`、`linux/arm64`。

### 7.2 自動更新

- Renovate：`pep621`（pyproject + uv.lock）、`dockerfile`（FROM，啟用 `pinDigests`）、`github-actions`。
- 不另開週期性重建（digest 釘選已涵蓋 base image 更新）。
- 對 `beanquery` 設定額外注意：它的 0.3.0 會超出 fava 目前的 `<0.3` 上限。
- 發佈：`docker/login-action@v4`、`docker/metadata-action@v6`、`docker/build-push-action@v7`、`docker/setup-buildx-action@v4`、`docker/setup-qemu-action@v4`（2026-09-21 的最新主版本）。推 GHCR 需 `permissions: packages: write`。
- Tag：`<fava>-beancount<beancount>`、`<fava>`、`latest`，加上帶日期或 sha 的不可變 tag。

### 7.3 對外 API

- 不要把外部契約建在 fava JSON API 或 extension 上。
- 自建服務用 `beancount.loader` + `beanquery`，自行設計認證。
- 放在同一個 image、獨立 container，ledger volume 以 `:ro` 掛載。
- fava 是唯一的 ledger 寫入者。

---

## 8. 未知事項與待決問題

以下項目**無法從第一手來源確認**，或需要實測：

1. **Alpine 上建置 beancount 是否真的可行。** 已確認 `flex-bin`、`bison-bin` 有 musllinux wheel，meson-python 是純 Python，但未實際在 musl 環境執行建置。建議直接避開 alpine，不需要驗證。
2. **beanquery 0.2.0 在 Python 3.14 上是否正常。** 其 classifier 只到 3.13，PyPI 最新版已 18 個月未更新。beancount 與 fava 都支援 3.14，但 beanquery 沒有對應宣告。建議選 3.13 規避。
3. **fava 寫入 ledger 檔時是否使用原子換檔。** 未逐一檢查 `put_source` 等端點的底層寫入實作。這影響「寫入中途 container 被終止會不會毀損帳本」的評估。需要時應讀 `fava/core/file.py` 確認。
4. **fava 在 PID 1 下對 SIGTERM 的實際行為。** 由原始碼（只攔 `KeyboardInterrupt`）與 Linux PID 1 語意推論會等到 `docker stop` timeout，但未實測。社群 image 的 CI smoke test 顯示這是真實存在的問題。
5. **從 fava sdist 建置是否真的不需要 npm。** `hatch_build.py` 的 mtime 短路邏輯在 tar 解壓後的行為不確定。建議一律裝 wheel，不觸碰這個問題。
6. **beancount / fava 的實際記憶體用量。** 影響 6.4 方案 C 的「ledger 載入兩份」代價評估。需實測。
7. **是否有官方發佈的 beancount 或 fava container image。** 已確認兩個 repo 的 workflow 都不建置 image，`contrib/docker` 只提供 Dockerfile。但無法排除在 repo 之外另有官方發佈管道；未在 Docker Hub / GHCR 做窮舉搜尋。
8. **fava JSON API 未來是否會提供穩定性承諾。** 文件的措辭是「currently」，暗示可能改變，但沒有 roadmap 可查。
9. **`--prefix` 與反向代理搭配時，fava JSON API 路徑的完整行為。** `--prefix` 用 `DispatcherMiddleware` 實作，理論上 API 路徑也一併加前綴，但未驗證前端與 API 的 URL 產生是否一致。

---

## 9. 來源清單

### Beancount

- PyPI metadata：<https://pypi.org/pypi/beancount/json>
- `pyproject.toml`：<https://github.com/beancount/beancount/blob/master/pyproject.toml>
- `meson.build`：<https://github.com/beancount/beancount/blob/master/meson.build>
- `README.rst`（版本分支說明）：<https://github.com/beancount/beancount/blob/master/README.rst>
- `CHANGES`（v3 分支建立與子專案拆分）：<https://github.com/beancount/beancount/blob/master/CHANGES>
- `bin/` 目錄：<https://github.com/beancount/beancount/tree/master/bin>
- wheel 建置 workflow（`CIBW_SKIP: '*-musllinux*'`）：<https://github.com/beancount/beancount/blob/master/.github/workflows/wheels.yaml>
- 安裝驗證 workflow：<https://github.com/beancount/beancount/blob/master/.github/workflows/install.yaml>
- 官方安裝文件（Google Doc）：<https://docs.google.com/document/d/1FqyrTPwiHVLyncWTf3v5TcooCu9z5JRX8Nm41lVZi0U/edit>
- 官方文件鏡像：<https://beancount.github.io/docs/installing_beancount.html>

### Beancount 子專案

- <https://pypi.org/pypi/beanquery/json> ／ <https://github.com/beancount/beanquery/blob/master/pyproject.toml>
- <https://pypi.org/pypi/beangulp/json> ／ <https://github.com/beancount/beangulp/blob/master/pyproject.toml>
- <https://github.com/beancount/beangulp/blob/master/beangulp/file_type.py>（python-magic 的 optional import）
- <https://pypi.org/pypi/beanprice/json> ／ <https://github.com/beancount/beanprice/blob/master/pyproject.toml>

### Fava

- PyPI metadata：<https://pypi.org/pypi/fava/json>
- `pyproject.toml`：<https://github.com/beancount/fava/blob/v1.30.16/pyproject.toml>
- `hatch_build.py`（npm 需求）：<https://github.com/beancount/fava/blob/v1.30.16/hatch_build.py>
- `src/fava/cli.py`（CLI 選項與環境變數）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/cli.py>
- `src/fava/application.py`（路由、blueprint prefix、read-only）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/application.py>
- `src/fava/json_api.py`（endpoint 清單與註冊規則）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/json_api.py>
- `src/fava/ext/__init__.py`（extension 機制）：<https://github.com/beancount/fava/blob/v1.30.16/src/fava/ext/__init__.py>
- `CHANGES`（v3 支援與 v2 移除）：<https://github.com/beancount/fava/blob/main/CHANGES>
- `docs/api.rst`（無穩定性保證）：<https://github.com/beancount/fava/blob/main/docs/api.rst>
- `docs/index.rst`：<https://github.com/beancount/fava/blob/main/docs/index.rst>
- `docs/usage.rst`：<https://github.com/beancount/fava/blob/main/docs/usage.rst>
- `contrib/docker/Dockerfile`：<https://github.com/beancount/fava/blob/main/contrib/docker/Dockerfile>
- `contrib/docker/README.md`：<https://github.com/beancount/fava/blob/main/contrib/docker/README.md>
- `contrib/deployment.rst`：<https://github.com/beancount/fava/blob/main/contrib/deployment.rst>
- 發佈 workflow：<https://github.com/beancount/fava/blob/main/.github/workflows/publish.yml>
- wheel / sdist 內容：實際下載 `fava-1.30.16-py3-none-any.whl` 與 `fava-1.30.16.tar.gz` 驗證（2026-09-21）

### 建置工具

- <https://pypi.org/pypi/flex-bin/json>
- <https://pypi.org/pypi/bison-bin/json>
- 官方 python image 文件：<https://github.com/docker-library/docs/blob/master/python/README.md>

### 自動更新與發佈

- Renovate `pep621` manager：<https://docs.renovatebot.com/modules/manager/pep621/>
- Renovate `pip_requirements` manager：<https://docs.renovatebot.com/modules/manager/pip_requirements/>
- Renovate `dockerfile` manager：<https://docs.renovatebot.com/modules/manager/dockerfile/>
- Renovate custom regex manager：<https://docs.renovatebot.com/modules/manager/regex/>
- Dependabot options reference：<https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference>
- GitHub Actions 發佈 Docker image 教學：<https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images>
- Docker actions 版本：GitHub API `repos/docker/{build-push-action,metadata-action,login-action,setup-buildx-action,setup-qemu-action}/releases/latest`（2026-09-21）

### 社群 image（非官方，僅供參考）

- <https://github.com/yegle/fava-docker>（Dockerfile 仍釘 beancount 2.3.6）
- <https://github.com/tarioch/docker-fava>（uv + digest 釘選 + tini + metadata-action tag 策略）
- <https://github.com/Evernight/lazy-beancount>
- <https://github.com/DIYgod/docker-fava>
