# Squad — after-spec — scope `image`

- cut: after-spec
- spec: `specs/image/SPEC.md` v0.1 at `92a6abe`
- lenses: scope, input space, repo reality, test mapping
- date: 2026-09-21

行號指 `92a6abe` 的 `specs/image/SPEC.md`。重複的發現已合併。

## Findings

- [HIGH] specs/image/SPEC.md:53 — Must NOT「不含真實帳本、token、密碼」沒有任何 scenario 或 gate layer — scenario 表 :29-47 沒有對應列 — class 1 — 加 gitleaks layer 和 `ledger_dir_empty_and_writable` — status: fixed
- [HIGH] specs/image/SPEC.md:56 — Must NOT「不安裝 uv.lock 以外的套件」沒有檢查，`versions_match_lock` 只比對 3 個套件 — :31 — class 1 — 以 `installed_packages_equal_lock` 比對完整套件集合 — status: fixed
- [HIGH] specs/image/SPEC.md:42 — `pr_workflow_never_pushes` 在沒有 workflow 檔時會通過，且 glob 漏掉 `.yml` — :42 — class 1 — 改為 `only_publish_workflow_pushes`，先斷言兩種 workflow 都存在 — status: fixed
- [HIGH] specs/image/SPEC.md:72,81 — mutmut 3.8.0 在 Windows 拒絕執行（`mutmut run` exit 1，訊息要求使用 WSL） — repo reality 視角實測 — class 1 — 移除 mutmut，mutation 改為手動 mutant 並逐一記錄 — status: fixed
- [HIGH] specs/image/SPEC.md:23,66-68 — `platformAutomerge` 預設 true，沒有 branch protection 時 GitHub 可能在 CI 完成前 merge — renovate docs configuration-options.md:4109-4117 — class 1 — 設 `platformAutomerge: false`，由 Renovate 在檢查通過後自行 merge — status: fixed
- [MEDIUM] specs/image/SPEC.md:61 — mutant「把 beancount 換成 2.x」無法執行：版本在 `uv.lock`，fava 要求 `beancount>=3.2.0` — :15 對 :61 — class 1 — 換成其他 mutant — status: fixed
- [MEDIUM] specs/image/SPEC.md:44-45 — `lint_clean`、`multi_arch_builds` 是 gate layer，不是行為 — :44 的證據類型寫「lint layer」 — class 1 — 移到 Gate layers 一節 — status: fixed
- [MEDIUM] specs/image/SPEC.md:72 — 沒有宣告 static types layer 的處理 — :72-81 — class 1 — 明確宣告略過和理由 — status: fixed
- [LOW] specs/image/SPEC.md:72 — `npx renovate-config-validator` 無法執行；正確指令是 `npx --yes --package renovate renovate-config-validator --strict` — repo reality 視角實測 — class 1 — 寫入完整指令 — status: fixed
- [HIGH] specs/image/SPEC.md:15 — `uv sync --frozen` 不檢查 lock 與 `pyproject.toml` 是否一致，Renovate 的 PR 可能建出舊版本 — uv docker guide 使用 `--locked` — class 2 — 改用 `--locked`，加 `build_fails_on_lock_drift`
- [HIGH] specs/image/SPEC.md:15 — `uv sync` 預設安裝 dev group，pytest 等會進到正式 image — 兩個視角各自實測 — class 2 — 加 `--no-dev`，由 `installed_packages_equal_lock` 檢查
- [HIGH] specs/image/SPEC.md:74 — 沒有 RED 的 stub 策略，base ref 沒有 Dockerfile，測試只會因找不到檔案而失敗 — `git ls-tree -r 4fdf55b` — class 2 — RED commit 帶一個只有 `FROM` 的 Dockerfile
- [HIGH] specs/image/SPEC.md:37 — 寫入測試用 bind mount：Docker Desktop 忽略擁有者所以必過，Linux runner 是 uid 1001 所以會失敗 — repo reality 視角實測 Windows 端 — class 2 — 改用 named volume，在 container 內 chown 為 1000
- [HIGH] specs/image/SPEC.md:22 — 沒有 concurrency 規則，較慢的舊 publish 會把 `latest` 蓋回舊版 — :21 只有一個不可變 tag — class 2 — publish workflow 加 `concurrency` 並斷言
- [HIGH] specs/image/SPEC.md:23,46 — 「自動更新」沒有端到端的 scenario — 契約原文「當這兩個基礎專案的版本有更新」 — class 2 — 加 manual scenario `auto_update_end_to_end`
- [HIGH] specs/image/SPEC.md:78-79,87 — 額外套件的矛盾回答被寫成「您選擇加裝」 — 使用者同時勾選「不額外安裝」、beanprice、git — class 2 — 記錄原始回答，列為核准時的確認事項
- [HIGH] specs/image/SPEC.md:38 — `BEANCOUNT_FILE` 指向不存在的檔案時 fava 正常啟動 — input space 視角實測，`fava/cli.py::_add_env_filenames` 不檢查存在 — class 2 — 決定：列為已知限制，不加 entrypoint 包裝
- [MEDIUM] specs/image/SPEC.md:42 — `workflow_dispatch` 等其他觸發方式沒有規則 — :22 — class 2 — publish workflow 的觸發條件限定為 push 到 `main`
- [MEDIUM] specs/image/SPEC.md:43 — Renovate 的 `pin`、`pinDigest`、`lockFileMaintenance` 等 update type 未分類 — :23 — class 2 — 頂層 `automerge: false`，只列舉允許的類型，測試斷言完整集合
- [MEDIUM] specs/image/SPEC.md:17 — `/ledger` 的擁有者沒有規定，`WORKDIR` 由 root 建立 — :35 只檢查 `id -u` — class 2 — 以 fava 使用者建立 `/ledger` 並斷言可寫
- [MEDIUM] specs/image/SPEC.md:15,17 — `/opt/venv` 複製成立的條件沒有寫出：兩個 stage 同一個 base、相同路徑、`UV_PROJECT_ENVIRONMENT`、`PATH` — repo reality 視角實測 — class 2 — 寫入設計
- [MEDIUM] specs/image/SPEC.md:45 — arm64 只驗證可建置，沒有任何執行期測試 — :45 — class 2 — 加 `arm64_smoke`
- [MEDIUM] specs/image/SPEC.md:18 — 沒有 scenario 使用含 `include` 的帳本 — :36-38 — class 2 — fixture 改為主檔加 include 檔
- [MEDIUM] specs/image/SPEC.md:21 — `tools/image_tags.py` 是未被要求的新工具，研究文件建議 `docker/metadata-action` — docs/research/beancount-fava-docker.md:576-583 — class 2 — 移除腳本，版本從建好的 image 讀取
- [MEDIUM] specs/image/SPEC.md:36 — 固定 host port 5000 會衝突；`GET /` 先回 302 — repo reality 視角實測 — class 2 — 改用隨機 port，斷言跟隨轉向後 200
- [MEDIUM] specs/image/SPEC.md:37 — `<slug>` 未定；沒有 `title` 時是 `beancount` — fava v1.30.16 application.py `_slug()`，實測 — class 2 — fixture 設定 `title`
- [MEDIUM] specs/image/SPEC.md:76 — Git Bash 會改寫 docker 的路徑參數 — 環境是 Windows 11 + Git Bash — class 2 — 測試以 Python subprocess 呼叫 docker；gate 設 `MSYS_NO_PATHCONV=1`
- [MEDIUM] specs/image/SPEC.md:16 — apt 的 `git`、`tini` 沒有釘選版本 — :23 — class 2 — 列為已接受的取捨
- [MEDIUM] specs/image/SPEC.md:16,32 — git 在帳本目錄需要 `safe.directory` 和 commit 身分 — git ≥2.35.2 — class 2 — 留給 deploy spec，記下依賴
- [MEDIUM] specs/image/SPEC.md:79 — tini 的依據被判為推論 — 研究文件 :346 標為推論 — class 2 — 註明為實測並寫出環境；repo reality 視角也重現了 137 和 143
- [MEDIUM] specs/image/SPEC.md:10 — deploy 屬於使用者的「第一步」、API 的前提是遷移，兩者未記錄 — 使用者原文 — class 2 — 補上
- [LOW] specs/image/SPEC.md:14 — 沒有 scenario 斷言 base image 以 digest 釘選 — :29-47 — class 2 — 加 `base_images_digest_pinned`
- [LOW] specs/image/SPEC.md:20,66-68 — 沒有建立 Docker Hub public repo 的步驟 — :66-68 — class 2 — 加入手動步驟
- [LOW] specs/image/SPEC.md:72 — actionlint 沒有 ghcr image，正確名稱是 `rhysd/actionlint` — `docker manifest inspect` — class 2 — 寫明
- [LOW] specs/image/SPEC.md:9,14 — 「單一 image」的決定沒有連到研究 — 使用者原文「是否建議合併為一個 image 或是多個 image」 — class 2 — 引用研究章節
- [LOW] specs/image/SPEC.md:23 — Renovate 追蹤範圍大於使用者說的「這兩個基礎專案」 — :47 — class 2 — 註明是刻意的超集
- [LOW] specs/image/SPEC.md:37 — 寫入探針依賴 fava 沒有穩定性聲明的 JSON API — docs/research/beancount-fava-docker.md:659 — class 2 — 列為已接受的成本：API 變動會擋下 automerge
- [LOW] specs/image/SPEC.md:81 — mutation、audit 工具由 gate 決定，不屬於本次變更 — verification-gate 擁有 layer — class 3 — 記錄，不處理

## Adjudication

- test mapping 視角認為 exit 2、143、`No file specified` 未經量測。repo reality 視角和 orchestrator 各自實測，結果一致。採用實測結果。
- scope 視角建議把寫入 scenario 移到 deploy spec。使用者要的 fava 是可編輯的 web UI，image 必須能寫入掛載的帳本。保留，並在設計寫明理由。

## Concessions（各視角自述）

- scope：沒有評估技術正確性。
- input space：多數發現是從 spec 文字推導，只有 `BEANCOUNT_FILE` 缺檔一項是實測。
- repo reality：沒有執行多架構 build；無法在 Windows 重現 Linux bind mount 的擁有者問題。
- test mapping：沒有建置 image，沒有執行 container。
