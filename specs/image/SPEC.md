# SPEC — beancount v3 + fava 單一 image、Docker Hub 發佈、自動更新 (Tier 2)

- `spec_version`: v0.1
- `status`: draft
- `tier`: 2
- `scope`: image
- `base_ref`: 4fdf55bffa078446fed8b1415e0d25a995f03428

範圍：一個 image、它的 CI 發佈流程、Renovate 自動更新。
不在範圍：deploy（compose、cloudflared、Cloudflare Access）另立 `specs/deploy/SPEC.md`，Tier 3。對外 API 暫緩。

## 設計

- Base image：`python:3.13-slim-trixie`，以 digest 釘選。beancount 沒有 musllinux wheel，所以不用 alpine。
- 版本釘選在 `pyproject.toml` 和 `uv.lock`：`beancount==3.2.3`、`fava==1.30.16`、`beanprice==2.1.0`。builder stage 用 `uv sync --frozen` 裝進 `/opt/venv`，final stage 只複製 venv。
- apt 套件：`git`、`tini`。
- 執行身分：uid 1000 的使用者 `fava`。`WORKDIR /ledger`，`ENV FAVA_HOST=0.0.0.0`，`EXPOSE 5000`，`ENTRYPOINT ["tini","--"]`，`CMD ["fava"]`。
- 帳本由使用者以 volume 掛到 `/ledger`，用 `BEANCOUNT_FILE` 指定絕對路徑。
- 平台：`linux/amd64`、`linux/arm64`。
- Docker Hub：`gn00678465/beancount-fava`（public）。GitHub：`gn00678465/beancount-fava`（public）。
- Tags：`<fava>-beancount<beancount>`、`<fava>`、`latest`、不可變的 `<fava>-beancount<beancount>-<git sha 前 7 碼>`。由 `tools/image_tags.py` 從 `uv.lock` 計算。
- CI：`pull_request` 只 build 和測試。push 到 `main` 才 build、測試、發佈。
- Renovate：`pep621`、`dockerfile`（`pinDigests`）、`github-actions`（釘選 commit SHA）。patch、minor、digest 在 CI 通過後自動 merge。major 不自動 merge。

## Scenarios

測試在 `tests/`，測試函式以 scenario 名稱命名。image 測試透過 docker CLI 對實際建置的 image 執行。

| scenario | 行為 | 通過條件 | 證據類型 |
|---|---|---|---|
| versions_match_lock | 在 image 內讀取 `importlib.metadata` | beancount、fava、beanprice 的版本等於 `uv.lock` 的版本，且 beancount major 是 3 | test |
| cli_tools_available | 執行 `bean-check`、`bean-query --help`、`bean-price --help`、`bean-format --help`、`git --version` | 全部 exit 0 | test |
| bean_check_valid_ledger | `bean-check` 檢查 `tests/fixtures/valid.beancount` | exit 0 | test |
| bean_check_unbalanced_ledger | `bean-check` 檢查含一筆不平衡交易的 `tests/fixtures/unbalanced.beancount` | exit 1 | test |
| runs_as_non_root | `docker run IMAGE id -u` | 輸出 `1000` | test |
| fava_serves_on_published_port | 以 `-p` 發佈 5000、掛載 fixture、設定 `BEANCOUNT_FILE=/ledger/valid.beancount` | 30 秒內從 host 對 `/` 發 GET，最終回應是 200 | test |
| fava_writes_mounted_ledger | 對執行中的 container 發 `PUT /<slug>/api/add_entries`，內容是一筆 2024-02-01、narration 為 `spec-write-probe` 的平衡交易 | 回應 200，且 host 上掛載的檔案含 `spec-write-probe` | test |
| fava_without_ledger_fails | 不設定 `BEANCOUNT_FILE` 執行預設 CMD | exit 2，stderr 含 `No file specified` | test |
| stops_on_sigterm | 對執行中的 container 執行 `docker stop` | 5 秒內結束，exit code 143 | test |
| image_tags_from_lock | `image_tags.py` 輸入 fava 1.30.16、beancount 3.2.3、sha `abcdef1234` | 輸出恰為 `1.30.16-beancount3.2.3`、`1.30.16`、`latest`、`1.30.16-beancount3.2.3-abcdef1` | test |
| image_tags_missing_package | `uv.lock` 缺少 fava 或 beancount | exit 非 0，訊息指出缺少的套件名稱 | test |
| pr_workflow_never_pushes | 解析 `.github/workflows/*.yaml` | 由 `pull_request` 觸發的 job 沒有 push 步驟，也沒有引用 `secrets.DOCKERHUB_*`；沒有 workflow 使用 `pull_request_target` | test |
| renovate_major_not_automerged | 解析 `renovate.json` | major 更新的 `automerge` 是 false；patch、minor、digest 是 true；`renovate-config-validator --strict` exit 0 | test |
| lint_clean | hadolint 檢查 `Dockerfile`，actionlint 檢查 workflows，ruff 檢查 Python | 全部沒有錯誤 | lint layer |
| multi_arch_builds | `docker buildx build --platform linux/amd64,linux/arm64` | 建置成功 | real execution |
| published_to_docker_hub | merge 到 `main` 後執行 `docker buildx imagetools inspect gn00678465/beancount-fava:latest` | 列出 amd64 和 arm64，四個 tag 都存在 | manual，merge 之後 |
| renovate_detects_dependencies | Renovate 首次執行後查看 Dependency Dashboard | 列出 beancount、fava、beanprice、python base image、uv、GitHub Actions | manual，merge 之後 |

## Must NOT

- Must NOT：image 內安裝 beancount 3.x 以外的 major 版本。
- Must NOT：container 的預設執行身分是 root。
- Must NOT：image 或 repo 內含任何真實帳本資料、token 或密碼。測試只用 `tests/fixtures/` 的假資料。
- Must NOT：`pull_request` 觸發的 workflow 推送 image 或取得 Docker Hub secrets。
- Must NOT：major 版本升級在沒有人工 merge 的情況下發佈。
- Must NOT：Dockerfile 在 build 時安裝未經 `uv.lock` 釘選的 Python 套件。

## 已知限制

- `published_to_docker_hub` 和 `renovate_detects_dependencies` 只能在 merge 且您完成下列手動步驟後觀察。evidence 報告會把它們列為未執行。
- 涵蓋率和 mutation 工具只適用於 `tools/image_tags.py`。Dockerfile 以手動 mutation 驗證：移除 `USER`、移除 `FAVA_HOST`、移除 `tini`、把 beancount 換成 2.x，每個 mutant 都必須被測試抓到。
- fava 寫入帳本不是原子操作（見研究文件）。本 spec 不處理，deploy spec 以 git 備份處理。

## 需要您手動完成的步驟

1. 在 Docker Hub 建立 Read + Write 權限的 personal access token。
2. 在 GitHub repo 設定 secrets `DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN`。
3. 在 GitHub repo 安裝 Renovate GitHub App。

## Setup plan

- Tools to install：無。本機已有 docker 29.5.3、buildx、uv 0.11.14、node 26.9.0、gh。hadolint 和 actionlint 以官方 docker image 執行，`renovate-config-validator` 以 `npx` 執行。
- Git isolation：branch `feat/image`。base 是 `main` 上的 4fdf55b。
- Commit cadence：spec 核准時一個 commit；每個 scenario 群組先 RED commit（只有測試），再 GREEN commit（只有實作）。
- 核准後我會用 `gh repo create gn00678465/beancount-fava --public` 建立遠端並 push。merge 到 `main` 由您決定。
- Gate files by path：`tools/gate.sh`、`tools/mutate_dockerfile.sh`。另加 `.gitignore`（忽略 `.claude/`、`.gate/`、`.venv/`、`__pycache__/`）。
- New dependencies：
  - 執行期：`beancount`（需求本體）、`fava`（需求本體）、`beanprice`（您選擇加裝，提供 `bean-price`）。
  - apt：`git`（您選擇加裝）、`tini`（轉送 SIGTERM；實測沒有 init 時 exit code 是 137）。
  - builder：`ghcr.io/astral-sh/uv`（依 `uv.lock` 安裝，含 hash 驗證）。
  - dev：`pytest`（測試）、`pytest-randomly`（suite health）、`ruff`（lint）、`pyyaml`（解析 workflow 檔）、`pip-audit`（相依弱點掃描）、`coverage`、`mutmut`（只用於 `image_tags.py`）。

## Approval

## Revisions

- 2026-09-21 — exploration round 1：Docker Hub 與 GitHub repo 名稱、public、automerge 規則、加裝 beanprice 和 git。
