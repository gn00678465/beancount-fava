# SPEC — beancount v3 + fava 單一 image、Docker Hub 發佈、自動更新 (Tier 2)

- `spec_version`: v3
- `status`: revised-pending-approval
- `tier`: 2
- `scope`: image
- `base_ref`: 4fdf55bffa078446fed8b1415e0d25a995f03428

您說的第一步是「image、自動更新和 deploy」。本 spec 涵蓋 image、發佈、自動更新。deploy（compose、cloudflared、Cloudflare Access）由 `specs/deploy/SPEC.md` 承接，Tier 3；它完成前第一步不算完成。對外 API 依您的說法，等記帳資料遷移後再確認是否需要。

## 設計

- 單一 image。理由見 `docs/research/beancount-fava-docker.md` 第 4 節：fava 直接 `import beancount`，兩者必須在同一個 Python 環境。
- fava 是您用來編輯帳本的 web UI，所以 image 必須能寫入掛載的帳本。
- Base image：`python:3.13-slim-trixie`，以 digest 釘選。beancount 沒有 musllinux wheel，所以不用 alpine。
- Python 套件釘選在 `pyproject.toml` 和 `uv.lock`：`beancount==3.2.3`、`fava==1.30.16`、`beanprice==2.1.0`。
- builder stage 執行 `uv sync --locked --no-dev`，`UV_PROJECT_ENVIRONMENT=/opt/venv`。final stage 用同一個 base image，把 `/opt/venv` 複製到相同路徑，`PATH` 以 `/opt/venv/bin` 開頭。
- apt 套件：`git`、`tini`，不釘選版本。base image digest 更新時會一起重建。final stage 移除 base image 自帶的系統 `pip`。
- 使用者 `fava`，uid 1000，Dockerfile 以數字 `USER 1000:1000` 指定。`/ledger` 由 `fava` 擁有，`WORKDIR /ledger`。`ENV FAVA_HOST=0.0.0.0`，`EXPOSE 5000`，`ENTRYPOINT ["tini","--"]`，`CMD ["fava"]`。
- 帳本以 volume 掛到 `/ledger`，用 `BEANCOUNT_FILE` 指定絕對路徑。
- 平台：`linux/amd64`、`linux/arm64`。
- Docker Hub `gn00678465/beancount-fava` 和 GitHub `gn00678465/beancount-fava`，都是 public。
- Tags：`<fava>-beancount<beancount>`、`<fava>`、`latest`、不可變的 `<fava>-beancount<beancount>-<git sha 前 7 碼>`。版本從建好的 image 讀取，交給 `docker/metadata-action`。
- `ci.yaml`：`pull_request` 觸發，執行 ruff、hadolint、actionlint、pip-audit 和完整測試，不登入 Docker Hub。Renovate 的 automerge 以它為準。
- `publish.yaml`：只由 push 到 `main` 觸發。build、測試、多架構 build、推送。`concurrency` 讓同一個 group 依序執行（`cancel-in-progress: false`）：較舊的執行先完成，`latest` 收斂到最新的 commit，也不會在推送中途被取消。tag 步驟讀不到版本時失敗，不推送。
- Renovate 追蹤 beancount、fava、beanprice、base image、uv image、GitHub Actions（釘選 commit SHA）。這比您說的「兩個基礎專案」多，目的是讓安全性更新也會重建 image。
- Renovate 頂層 `automerge: false`、`platformAutomerge: false`。只有 `patch`、`minor`、`digest`、`pin`、`pinDigest` 設為 automerge，由 Renovate 在 CI 通過後自行 merge。major 和其他類型都要您手動 merge。`lockFileMaintenance` 啟用但不 automerge，用來更新 transitive 相依。python base image 限制在 3.13；升級 Python 版本是手動決定。

## Scenarios

測試在 `tests/`，函式以 scenario 名稱命名，用 Python `subprocess` 直接呼叫 docker。fixture 是 `tests/fixtures/ledger/main.beancount`（`option "title" "Spec Ledger"`，`include "accounts.beancount"`）。掛載一律用 named volume，先在 container 內把擁有者改成 1000。

| scenario | 行為 | 通過條件 | 證據類型 |
|---|---|---|---|
| installed_packages_equal_lock | 列出 image 內 `/opt/venv` 的所有 distribution | 名稱和版本的集合等於 `uv export --locked --no-dev` 的集合；beancount 的 major 是 3；找不到 pytest；系統 Python（`/usr/local/bin/python3`）的 site-packages 沒有任何 distribution | test |
| build_fails_on_lock_drift | 在暫存複本把 `pyproject.toml` 的 fava 改成 `1.30.15`，不改 `uv.lock`，執行 `docker build` | build 的 exit code 不是 0，輸出提到 `uv.lock` | test |
| cli_tools_available | 執行 `bean-query --help`、`bean-price --help`、`bean-format --help`、`git --version` | 全部 exit 0 | test |
| bean_check_valid_ledger | `bean-check /ledger/main.beancount` | exit 0 | test |
| bean_check_unbalanced_ledger | `bean-check` 檢查含一筆不平衡交易的 `unbalanced.beancount` | exit 1，輸出含 `does not balance` | test |
| runs_as_non_root | `docker run IMAGE id -u` | 輸出 `1000` | test |
| ledger_dir_empty_and_writable | 不掛載 volume，執行 `ls -A /ledger` 再 `touch /ledger/x` | `ls` 沒有輸出，`touch` exit 0 | test |
| fava_serves_on_published_port | 發佈到隨機 host port，設定 `BEANCOUNT_FILE=/ledger/main.beancount` | 等待上限 30 秒；從 host 對 `/` 發 GET 並跟隨轉向，狀態 200，最終路徑以 `/spec-ledger/` 開頭 | test |
| fava_writes_mounted_ledger | `PUT /spec-ledger/api/add_entries`，內容是一筆 2024-02-01、narration 為 `spec-write-probe` 的平衡交易 | 回應 200；從 volume 讀回的 `main.beancount` 含 `spec-write-probe` | test |
| fava_without_ledger_fails | 不設定 `BEANCOUNT_FILE`，執行預設 CMD | exit 2，stderr 含 `No file specified` | test |
| stops_on_sigterm | 對執行中的 amd64 container 執行 `docker stop` | 5 秒內結束，exit code 143 | test |
| arm64_smoke | 以 `--platform linux/arm64` 執行 image | `platform.machine()` 是 `aarch64`；`bean-check /ledger/main.beancount` exit 0 | test |
| base_images_digest_pinned | 解析 `Dockerfile` 的 `FROM` 和 `COPY --from=` | 每個外部 image 參照都含 `@sha256:` | test |
| only_publish_workflow_pushes | 解析 `.github/workflows/*.yml` 和 `*.yaml` | `ci.yaml` 和 `publish.yaml` 都存在。以 YAML 結構檢查：`publish.yaml` 以外的 workflow 沒有任何 `secrets` 引用、`docker/login-action`、值不是 false 的 `push` 輸入、含 `docker push` 的 `run`。`publish.yaml` 的 `on` 只有 push 到 `main`；有 `concurrency.group` 且 `cancel-in-progress` 是 false；引用測試 fixture 建出的同一個 image tag。`ci.yaml` 由 `pull_request` 觸發，steps 含 `uv run pytest` 和 pip-audit。沒有 workflow 使用 `pull_request_target` | test |
| renovate_automerge_allowlist | 解析 `renovate.json` | 頂層 `automerge` 和 `platformAutomerge` 都是 false；設為 automerge 的 update type 集合恰為 `patch`、`minor`、`digest`、`pin`、`pinDigest`；`lockFileMaintenance.enabled` 是 true 且沒有 automerge；有一條規則把 `python` docker image 的 `allowedVersions` 限制在 3.13；`npx --yes --package renovate renovate-config-validator --strict` exit 0 | test |
| published_to_docker_hub | merge 到 `main` 後查詢 Docker Hub tags API 和 `imagetools inspect` | 四個 tag 都存在；`latest` 有 amd64 和 arm64 | manual，merge 之後 |
| auto_update_end_to_end | 第一個 automerge 的 Renovate PR 進入 `main` | `publish.yaml` 成功；Docker Hub 出現新的不可變 tag | manual，merge 之後 |

## Gate layers

- 測試：`uv run pytest`，隨機順序（`pytest-randomly`）。
- Suite health：以另一個固定 seed 的隨機順序再執行一次完整測試。兩個 seed 寫在 `tools/gate.sh`。
- Lint 和 format：`ruff check`、`ruff format --check`（排除 `docs/`，ruff 會改寫 Markdown 內引用的上游程式碼）、`hadolint/hadolint`、`rhysd/actionlint`。
- 多架構 build：`docker buildx build --platform linux/amd64,linux/arm64`。
- Secret 掃描：`ghcr.io/gitleaks/gitleaks` 的 git 模式。gate 執行時工作目錄是乾淨的，所以它涵蓋所有已追蹤的檔案和完整歷史。不用目錄模式，因為它會掃描 `.venv` 和 `.git`。
- 相依弱點：`pip-audit` 檢查 `uv export` 的結果，只忽略 `PYSEC-2026-2447`。其他弱點仍會讓 gate 失敗。
- 手動 mutation：移除 `USER`、移除 `FAVA_HOST`、移除 `tini`、移除 `--no-dev`、把 `--locked` 換成 `--frozen`、移除 `/ledger` 的 chown、移除 `publish.yaml` 的 `concurrency`、把 `major` 加進 Renovate allowlist、移除 `ci.yaml` 的 `uv run pytest`、移除系統 pip 的移除步驟。`tools/mutate.sh` 逐一套用。只有指定的測試以 `FAILED` 結束、且沒有任何 `ERROR` 時才算被抓到；有任何 mutant 存活就失敗。
- 宣告略過：static types 和 changed-line coverage。repo 內的 Python 只有測試程式，受測對象是 Dockerfile 和設定檔。

## Must NOT

- Must NOT：image 內安裝 beancount 3.x 以外的 major 版本。
- Must NOT：container 的預設執行身分是 root。
- Must NOT：image 或 repo 內含真實帳本資料、token 或密碼。
- Must NOT：`publish.yaml` 以外的 workflow 推送 image 或取得 Docker Hub secrets。
- Must NOT：major 版本升級在沒有人工 merge 的情況下發佈。
- Must NOT：image 內含 `uv.lock` 以外的 Python 套件，或 dev 套件。

## 已知限制

- 兩個 manual scenario 只能在 merge 且您完成手動步驟後觀察。evidence 報告會列為未執行。
- `renovate_automerge_allowlist` 檢查的是設定內容。Renovate 的實際行為由 `auto_update_end_to_end` 觀察。
- `BEANCOUNT_FILE` 指向不存在的檔案時，fava 會正常啟動並顯示錯誤，不會退出。本 spec 不加 entrypoint 包裝。
- 寫入測試使用 fava 沒有穩定性聲明的 JSON API。fava 升版若改了 `add_entries`，CI 會失敗並擋下 automerge，需要您處理。
- beanprice 2.1.0 相依的 diskcache 5.6.3 有 CVE-2025-69872（`PYSEC-2026-2447`，預設用 pickle 序列化），2026-09-21 沒有修正版。攻擊者要先能寫入 container 內 uid 1000 的快取目錄，而能寫入的人已經能以該身分執行程式。您決定保留 beanprice 並忽略這一項。公開 image 會被掃描工具標示這個 CVE，直到 diskcache 出修正版；transitive 相依的更新要靠您手動 merge Renovate 的 lock file maintenance PR。
- 發佈的 image 是 `publish.yaml` 內另一次多架構 build，不是測試過的那一個 image。apt 套件沒有釘選，兩者內容可能不同。
- 帳本 volume 以唯讀方式掛載時，fava 正常啟動，存檔時才回 HTTP 500。
- `.dockerignore` 沒有測試涵蓋。它只縮小 build context；image 不含 repo 檔案是由 `COPY` 只列出兩個檔名保證的。
- fava 沒有認證。直接發佈 port 時任何人都能改寫帳本。存取控制由 deploy spec 處理。
- fava 寫入帳本不是原子操作。在 `/ledger` 執行 `git commit` 需要設定 commit 身分；volume 由其他 uid 擁有時還需要 `safe.directory`。兩者都由 deploy spec 處理。

## 需要您手動完成的步驟

1. 在 Docker Hub 建立 public repository `gn00678465/beancount-fava`。
2. 在 Docker Hub 建立 Read + Write 權限的 personal access token。
3. 在 GitHub repo 設定 secrets `DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN`。
4. 在 GitHub repo 安裝 Renovate GitHub App。

## Setup plan

- Tools to install：無。本機已有 docker 29.5.3、buildx、uv 0.11.14、node 26.9.0、gh。
- Git isolation：branch `feat/image`，base 是 `main` 的 4fdf55b。
- Commit cadence：spec 核准一個 commit。第一個 RED commit 含測試、fixture、只有 `FROM` 的 `Dockerfile`、dev 相依的 `pyproject.toml` 和 `uv.lock`，讓測試因行為不符而失敗。之後每組 scenario 先 RED 再 GREEN。
- 核准後我會執行 `gh repo create gn00678465/beancount-fava --public` 並 push `main` 和 `feat/image`。merge 到 `main` 由您決定。
- 我會從 `feat/image` 開一個 draft PR，讓 `ci.yaml` 在 GitHub 的 runner 上實際執行。arm64 在 runner 上能否建置沒有文件可查，以這次執行的結果為準。
- `.scratch/image/` 下的 squad 紀錄和 evidence 報告會 commit 到 repo。spec-archive 在 CLOSE 時需要它們。
- Gate files by path：`tools/gate.sh`、`tools/mutate.sh`、`.gitattributes`（固定 LF；這台機器的 `core.autocrlf=true` 會讓 shell 腳本和 sed mutant 在新的 checkout 失效）、`.dockerignore`（build context 只含 `pyproject.toml` 和 `uv.lock`）、`.gitignore`（忽略 `.claude/`、`.gate/`、`.venv/`、`__pycache__/`）。
- New dependencies：
  - 執行期：`beancount`、`fava`（需求本體）、`beanprice`（提供 `bean-price`）。
  - apt：`git`；`tini`（轉送 SIGTERM。在 Docker Desktop 29.5.3 實測兩次：沒有 init 時 `docker stop` 的 exit code 是 137，有 tini 時是 143）。
  - builder：`ghcr.io/astral-sh/uv`（依 `uv.lock` 安裝並驗證 hash）。
  - dev：`pytest`、`pytest-randomly`（隨機順序）、`ruff`、`pyyaml`（解析 workflow）、`pip-audit`。

## Approval

- 2026-09-21 — approves v1 — "核准 v1"（對問題「您是否核准 specs/image/SPEC.md 的 spec_version v1（commit 886766b）？」的選擇。同一次詢問中，額外套件的解讀得到的回答是「正確，加裝 beanprice 和 git」。）
- 2026-09-21 — approves v2 — "核准 v2"（對問題「您是否核准 specs/image/SPEC.md 的 spec_version v2（commit 8a70746）？」的選擇。）

## Revisions

- 2026-09-21 — exploration round 1：repo 名稱、public、automerge 規則。額外套件一題您同時勾選「不額外安裝」、beanprice、git；本 spec 解讀為加裝 beanprice 和 git，待核准時確認。
- 2026-09-21 — after-spec squad：併入 `.scratch/image/squad/after-spec.md` 的發現。移除 `tools/image_tags.py` 和 mutmut；`--frozen` 改 `--locked --no-dev`；寫入測試改用 named volume；新增 lock drift、arm64、digest、`/ledger` 權限的 scenario；`platformAutomerge: false`；publish 限定 push 到 `main` 並加 `concurrency`。
- 2026-09-21 — v2：pip-audit 發現 diskcache 的 `PYSEC-2026-2447`；您選擇「保留 beanprice，忽略這一項」。同時記錄實作中出現、v1 沒列出的項目：`.gitattributes`、`.dockerignore`、數字 `USER`、suite-health layer、gitleaks 只用 git 模式、ruff 排除 `docs/`。scenario 和 Must NOT 沒有變動。
- 2026-09-21 — v3：併入 `.scratch/image/squad/after-implement.md` 的發現。移除系統 pip；`publish.yaml` 改為依序執行且 tag 步驟 fail closed；`ci.yaml` 的內容寫入設計並由測試斷言；workflow 的 Must NOT 改以 YAML 結構檢查；啟用 `lockFileMaintenance`；python image 限制在 3.13；mutant 清單與腳本一致並新增兩個；已知限制新增四項並修正 git 一項；開 draft PR 取得 CI 的實際結果。
