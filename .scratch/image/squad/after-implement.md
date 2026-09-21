# Squad — after-implement — scope `image`

- cut: after-implement
- spec: `specs/image/SPEC.md` v2 at `35d3d75`
- lenses: contract vs implementation, live evidence, diff hygiene and automation correctness
- date: 2026-09-21
- quick checks before the cut: 18 tests passed; ruff, hadolint, actionlint clean; gitleaks git mode clean; pip-audit clean with one ignore; 8/8 manual mutants killed; multi-arch build rc 0

## Findings

- [MEDIUM] Dockerfile:15 — final image 內有 base image 的系統 `pip` 26.2.1，不在 `uv.lock`；`installed_packages_equal_lock` 只掃描 `/opt/venv` — `docker run --rm bf-squad:live pip --version` — class 1 — final stage 移除系統 pip，測試同時檢查系統 site-packages
- [HIGH] renovate.json:3-15 — spec 已知限制承諾 transitive 更新靠 lock file maintenance PR，但 `lockFileMaintenance` 預設停用，`config:recommended` 不含它 — renovate docs configuration-options.md:2754 — class 1 — 啟用 `lockFileMaintenance`，不加入 automerge
- [MEDIUM] tests/test_automation.py:13 — Must NOT「只有 publish.yaml 能推送」只靠三個字串；`run: docker push`、`push: yes`、`secrets: inherit` 都能繞過 — SPEC.md scenario `only_publish_workflow_pushes` — class 1 — 以 YAML 結構檢查 steps
- [MEDIUM] tools/mutate.sh:51,56 — 兩個 mutant 與 spec 宣告不一致：spec 寫移除 `FAVA_HOST`、移除 `concurrency`，腳本改寫數值 — SPEC.md Gate layers — class 1 — 依 spec 實作
- [HIGH] .github/workflows/ci.yaml:25 — 沒有測試斷言 `ci.yaml` 會執行 `uv run pytest`；刪掉它所有測試仍通過，Renovate 會在空的 CI 上 automerge — tests/test_automation.py:27-43 — class 2 — 加斷言和 mutant
- [HIGH] .github/workflows/ci.yaml:24 — pip-audit 的 pipeline 沒有 `pipefail`，`uv export` 失敗時 pip-audit 讀到空輸入並 exit 0 — GitHub Actions 預設 shell 是 `bash -e {0}` — class 2 — 拆成兩個步驟
- [HIGH] .github/workflows/publish.yaml:33-39 — `installed()` 內的 `docker run` 失敗時版本為空，步驟仍成功，會推送錯誤的 tag 和 `latest` — command substitution 的失敗不觸發 `-e` — class 2 — `set -euo pipefail`，先取值再檢查非空
- [MEDIUM] tools/mutate.sh:41-45 — 任何 pytest rc 1 都記為 killed，build 錯誤或 fixture 錯誤也算 — tests/conftest.py `_built` 的 assert — class 2 — 要求指定測試出現 `FAILED` 且沒有 `ERROR`
- [MEDIUM] .github/workflows/publish.yaml:35 — 版本步驟使用 `beancount-fava:test`，這個 tag 只是 pytest fixture 的副作用，兩處字串沒有測試確認一致 — tests/conftest.py `image` fixture — class 2 — 測試斷言 `publish.yaml` 引用同一個常數
- [MEDIUM] .github/workflows/publish.yaml:8-10 — `cancel-in-progress: true` 會在推送中途取消執行，被取消的 commit 沒有不可變 tag，四個 tag 可能不一致 — SPEC.md:25 — class 2 — 改為 `cancel-in-progress: false`：同一 group 依序執行，較舊的先完成，`latest` 仍收斂到最新
- [MEDIUM] tests/conftest.py:30 — ubuntu runner 上 `docker build --platform linux/arm64` 在 `setup-qemu-action` 之後能否成功，沒有第一手文件說明（視角標為推論） — docs.docker.com/build/building/multi-platform — class 2 — 開 draft PR 讓 `ci.yaml` 實際執行，以結果為準
- [MEDIUM] renovate.json:12-14 — `python:3.14-slim-trixie` 在 docker versioning 下是 minor，會進 automerge；`requires-python = "==3.13.*"` 讓 build 失敗，CI fail closed，但 PR 永遠是紅的 — dockerfile manager readme Versioning — class 2 — 加 packageRule 把 python image 限制在 3.13
- [MEDIUM] .github/workflows/publish.yaml:54-62 — 發佈的 image 是另一次 build，apt 套件沒有釘選，內容可能與測試過的不同 — Dockerfile apt 步驟 — class 2 — 列入已知限制
- [MEDIUM] .scratch/image/squad/after-spec.md — squad 紀錄已 commit 且公開，spec 的檔案清單沒有列出 `.scratch/` — commit 886766b — class 2 — spec 列為預期的產出（spec-archive 要求 commit）
- [MEDIUM] `-v vol:/ledger:ro` — 唯讀掛載時 fava 正常啟動，存檔時回 HTTP 500 和 traceback — 實測 `OSError: [Errno 30] Read-only file system` — class 2 — 列入已知限制
- [LOW] tests/test_image.py:78 — `build_fails_on_lock_drift` 只斷言 rc 非 0，無關的 build 失敗也會通過 — SPEC.md scenario 列 — class 2 — 同時斷言輸出提到 `uv.lock`
- [LOW] tools/gate.sh:112,114 — `tests` 和 `suite-health` 指令相同，沒有記錄 seed — pytest-randomly — class 2 — 傳入兩個不同的固定 seed
- [LOW] .github/workflows/ci.yaml:24 — `--ignore-vuln` 沒有註解，忽略規則有兩個擁有者 — tools/gate.sh:77-78 — class 2 — 兩處加上互相指向的註解
- [LOW] .github/workflows/ci.yaml:20-24 — `ci.yaml` 執行 lint 和 pip-audit，spec 的 CI 描述沒有提到 — SPEC.md:24 — class 2 — 寫入 spec
- [LOW] .dockerignore:1 — 沒有 scenario 或 mutant 涵蓋；Must NOT 由 `COPY` 只列兩個檔名來保證 — Dockerfile `COPY pyproject.toml uv.lock` — class 2 — 接受：它只縮小 build context，不是受測行為
- [LOW] tests/test_automation.py:52-58 — `extends` 的 preset 沒有被解析，preset 若啟用 major automerge 不會被發現 — SPEC.md 已知限制 — class 2 — 接受已記錄的限制，由 `auto_update_end_to_end` 觀察
- [LOW] renovate.json:12 — `pin` 在這個 repo 不會發生，allowlist 比實際需要寬 — pyproject.toml 都是 `==` — class 2 — 保留
- [LOW] specs/image/SPEC.md:80 — git 的已知限制不精確：`git init`、`git add` 成功，只有 `commit` 因缺少身分而失敗；`safe.directory` 只在 volume 由其他 uid 擁有時需要 — 實測 `commit exit=128` — class 2 — 修正句子
- [MEDIUM] 直接發佈 port — 不經認證的 `PUT add_entries` 可以改寫帳本 — 實測回應 200 — class 3 — deploy spec 必須以 Cloudflare Access 把關
- [LOW] fava 寫入的格式 — 新交易的金額欄位對齊位置與既有交易不同 — 實測前後對照 — class 3 — 記錄
- [LOW] `--read-only` 根檔案系統 — image 完全正常，不需要可寫的 HOME 或 tmp — 實測 GET 200、PUT 200 — class 3 — deploy spec 可設為預設

## Adjudication

- diff hygiene 視角建議在「記錄限制」和「只用 group 依序執行」之間二選一。採用後者：它解決原本的問題（較舊的執行覆蓋 `latest`），又不會在推送中途取消。
- contract 視角沒有執行任何指令；live evidence 視角手動重現了 13 項 scenario，全部成立。

## Concessions（各視角自述）

- contract vs implementation：沒有執行 docker、pytest 或 gate，結論來自檔案和 git 歷史。
- live evidence：只建置 amd64；沒有檢查 arm64、lock drift、workflow、Renovate 和兩個 manual scenario。
- diff hygiene：無法執行 GitHub Actions、Docker 或 Renovate；arm64 在 runner 上的建置和 python 3.14 的分類是推論。
