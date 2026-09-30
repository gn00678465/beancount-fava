# 瀏覽器認證方式比較：better-auth、Cloudflare Access、來源 IP、HTTP Basic

查詢日期：2026-09-30。

本文回答一個問題：在 image 內以 WSGI middleware 包住整個 fava（方案 B）之後，瀏覽器要用什麼憑證通過。Agent 用 Bearer token，只能呼叫 fava `/api/` 的 GET 與 extension 的寫入端點。這一部分不在本文範圍。背景見 `docs/plans/agent-write-api.md` 與 `docs/research/agent-write-api-and-public-exposure.md` 第 8 至 12 節。

每項主張附第一手來源。「推論」代表由已查證事實推導，沒有直接證據。「未知」代表第一手來源沒有答案。本文沒有實測，沒有連線到 `192.168.2.11`。

部署現況（使用者提供，本文沒有查證）：

- fava 在 Unraid 的 Docker 執行，區網位址 `http://192.168.2.11:5656`（純 HTTP、以 IP 存取），`--user 99:100`。
- cloudflared 也在同一台 Unraid 上（container），對外發佈 fava，前面有 Cloudflare Access。
- 使用者要保留區網 5656 port。

引用的版本：

| 對象 | 版本 | 固定點 |
|---|---|---|
| better-auth | `v1.7.6`（npm `latest`，2026-09-24 發佈） | commit `229a02a652185ed32e87eab0c77c09d58532e0f1` |
| `@better-auth/utils`（密碼雜湊實作） | `0.4.2`（better-auth `v1.7.6` 的 `pnpm-workspace.yaml` catalog 指定） | tag `v0.4.2`，commit `b20329a32d78f1f9bcc088bbd6f982b28c4192f1` |
| W3C WebAuthn Level 3 | W3C Recommendation，2026-08-25 | `https://www.w3.org/TR/2026/REC-webauthn-3-20260825/` |
| W3C Secure Contexts | Candidate Recommendation Draft，2023-11-10 | `https://www.w3.org/TR/secure-contexts/` |
| WHATWG URL | Living Standard，2026-09-10 更新 | `https://url.spec.whatwg.org/` |
| WHATWG Fetch | Living Standard，2026-09-21 更新 | `https://fetch.spec.whatwg.org/` |
| RFC | 6265、6749、7617、9700；Internet-Draft `draft-ietf-httpbis-rfc6265bis-22`（尚未成為 RFC） | rfc-editor.org、ietf.org |
| Docker 文件 | `docker/docs` | commit `3633800c79c473180d51ff64f7dffb05ccfb92c9` |
| moby | `master` | commit `367ff5729a24b630d0072c6dc3e0c7cb4481e4de` |
| Cloudflare、Unraid 文件 | 頁面沒有版本號 | 2026-09-30 抓取 |
| fava | `v1.30.16` | tag `v1.30.16` |

下文連結的縮寫：

- `BA` = `https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1`
- `BAD` = `BA/docs/content/docs`（better-auth 官方文件原始檔）
- `BAU` = `https://github.com/better-auth/utils/blob/b20329a32d78f1f9bcc088bbd6f982b28c4192f1`
- `WA` = `https://www.w3.org/TR/2026/REC-webauthn-3-20260825/`
- `DD` = `https://github.com/docker/docs/blob/3633800c79c473180d51ff64f7dffb05ccfb92c9/content/manuals/engine`
- `CF1` = `https://developers.cloudflare.com/cloudflare-one`

---

## 1. 摘要

### 1.1 關鍵事實

| 主題 | 事實 | 來源 |
|---|---|---|
| better-auth 的形態 | TypeScript 函式庫，不是獨立的認證伺服器。要自己寫一個 Node 應用程式與登入頁。 | `BAD/introduction.mdx#L7`、`BAD/installation.mdx#L257-L261` |
| passkey 在 `http://192.168.2.11:5656` | **不能用**。WebAuthn 只在 secure context 提供，而 `http` + 私有 IP 不是 potentially trustworthy origin。另外 RP ID 必須是 domain，IP 位址會被拒絕。 | `WA#sctn-api`、`WA#sctn-createCredential`、Secure Contexts §3.1、WHATWG URL `#valid-domain` |
| passkey 在 Cloudflare 網址 | 可以用。`https` + domain 符合 RP ID 規則。 | `WA#rp-id` |
| SSO 在 `http://192.168.2.11` | 不能用。RFC 9700 規定 authorization server MUST NOT 允許 `http` redirect URI（native app 的 loopback 除外）。 | RFC 9700 §2.6 |
| better-auth 密碼雜湊 | scrypt，`N=16384`、`r=16`、`p=1`、`dkLen=64`，16 byte 隨機 salt。 | `BAU/src/password.node.ts#L3-L8` |
| better-auth 登入速率限制 | `/sign-in*` 預設每 IP 10 秒 3 次。只在 `NODE_ENV=production` 時預設啟用。預設存在記憶體。 | `BA/packages/better-auth/src/api/rate-limiter/index.ts#L439-L452`、`BA/packages/better-auth/src/context/create-context.ts#L359` |
| better-auth 帳號鎖定 | 密碼登入沒有帳號鎖定。只有 two-factor plugin 有（預設 10 次、15 分鐘）。 | `BA/packages/better-auth/src/plugins/two-factor/constant.ts#L8-L11` |
| better-auth 安全公告 | GitHub 上公開 34 則，其中 `@better-auth/sso` 5 則、`@better-auth/passkey` 1 則。`v1.7.6` 已包含到 2026-09-29 為止公告的修正版本。 | `https://github.com/better-auth/better-auth/security/advisories` |
| fava 前端的 fetch | 沒有覆寫 `credentials`，所以用預設值 `same-origin`。同源請求會帶 cookie 與 HTTP authentication 憑證。 | fava `frontend/src/lib/fetch.ts`、Fetch `#concept-request-credentials-mode` |
| Cloudflare Access 的 WebAuthn | Independent MFA 支援 security key 與 biometrics（WebAuthn）作為第二因素。方案限制：未知。 | `CF1/access-controls/access-settings/independent-mfa/` |
| Docker 容器的 raw socket | Docker 預設保留 `CAP_NET_RAW`。Docker 文件把「拒絕 raw socket」描述為防止封包偽造的手段。 | moby `daemon/pkg/oci/caps/defaults.go#L11`、`DD/security/_index.md#L184` |
| HTTP Basic | 每個請求都以明文傳送密碼。RFC 7617 說沒有 HTTPS 時 SHOULD NOT 用來保護敏感資料。 | RFC 7617 §4 |

### 1.2 結論

- **最安全**：d（只信任 Cloudflare Access，middleware 以密碼學方式驗證 `Cf-Access-Jwt-Assertion`），並在 Access 端加上 WebAuthn 第二因素（Independent MFA）或用支援 phishing-resistant MFA 的 IdP。b（better-auth passkey）的憑證強度相同，但只能在 Cloudflare 網址使用，而且要多一個 Node container、資料庫與 better-auth 的程式面。第 10 節有推導。
- **最適合使用者現況**：d。image 內只多一段 JWT 驗證程式與一個 Bearer token。瀏覽器改走 Cloudflare 網址。區網 5656 只給 Agent。
- **d 的代價是方便性**：區網瀏覽器不能再直接開 `http://192.168.2.11:5656`。對外網路或 Cloudflare 中斷時，瀏覽器無法使用 fava。
- **區網瀏覽器一定要能用時**：純 HTTP + IP 的條件下，passkey 與 SSO 都不能用。剩下的 password（a）與 Basic（f）在區網都以明文傳送憑證。f 的部署成本最低。a 多了 Node container 與資料庫，但在區網這條路徑上沒有解決明文問題。要在區網得到強認證，前提是區網也改成 HTTPS + domain（推論，見 10.3）。

---

## 2. 問題的限制條件

1. **fava 前端與 Agent 打同一組 `/api/`。** fava 前端以 `fetch(input, init)` 呼叫 API，沒有設定 `credentials`（fava `v1.30.16` `frontend/src/lib/fetch.ts` 第 36、59 行；`frontend/src/api/index.ts` 第 199 行只設 `Content-Type`）。Fetch 規格中 request 的 credentials mode 預設是 `"same-origin"`，意義是同源請求會帶憑證。憑證的定義是「HTTP cookies, TLS client certificates, and authentication entries (for HTTP authentication)」。
   來源：<https://github.com/beancount/fava/blob/v1.30.16/frontend/src/lib/fetch.ts>、<https://fetch.spec.whatwg.org/#concept-request-credentials-mode>、<https://fetch.spec.whatwg.org/#credentials>
   推論：瀏覽器端的憑證只要是 cookie 或 HTTP Basic，fava 前端不必改程式就會自動帶上。Bearer header 不會自動帶上，要改 fava 前端才能用。
2. **兩條路徑的 origin 不同。** 區網是 `http://192.168.2.11:5656`，Cloudflare 是 `https://<hostname>`。cookie 以 host 為範圍，兩條路徑各自要登入一次（推論，依 RFC 6265 的 host-only cookie 規則）。
3. **同一台主機的 cookie 不以 port 隔離。** RFC 6265 §8.5：「Cookies do not provide isolation by port.」並說 servers SHOULD NOT 在同一 host 的不同 port 跑互不信任的服務又用 cookie 存敏感資料。
   來源：<https://www.rfc-editor.org/rfc/rfc6265#section-8.5>
   推論：在 `192.168.2.11` 上以 cookie 保存 fava 的 session，同一 IP 上的其他服務（Unraid webGUI、其他 container 的 web UI）收到請求時也會收到這個 cookie。

---

## 3. better-auth 是什麼（研究問題 1）

### 3.1 語言、授權、版本、維護

- 語言與定位：「a framework-agnostic, universal authentication and authorization framework for TypeScript」。
  來源：`BAD/introduction.mdx#L7`
- 執行環境：官方安裝文件以 `better-auth/node` 的 `toNodeHandler` 掛到 Node HTTP 伺服器，也有 Hono、Express 等範例。Cloudflare Workers 要開 `nodejs_compat`。密碼雜湊在 Node.js、Bun、Deno 用 `node:crypto` 的 scrypt，其他環境改用 `@noble/hashes`。
  來源：`BAD/installation.mdx#L257-L261`、`BAD/installation.mdx#L346-L356`、`BA/packages/better-auth/src/crypto/password.ts#L1-L6`
- 授權：MIT（GitHub API `license.spdx_id`）。
  來源：<https://github.com/better-auth/better-auth>
- 最新版本：npm `latest` 為 `1.7.6`（2026-09-24）。另有維護線 `release-1.6` 為 `1.6.33`（2026-09-14）。
  來源：<https://registry.npmjs.org/better-auth>、<https://github.com/better-auth/better-auth/releases/tag/v1.7.6>
- 維護狀態：repo 沒有封存，最後 push 為 2026-09-30。2026-08-26 到 2026-09-24 之間發佈了 `v1.7.2` 到 `v1.7.6` 與 `v1.6.31` 到 `v1.6.33`。
  來源：<https://github.com/better-auth/better-auth/releases>
- 安全公告：公開 34 則。2025-10-01 之後公告的 29 則中，critical 2、high 19、medium 6、low 2。依套件分：`better-auth` 21、`@better-auth/sso` 5、`@better-auth/oauth-provider` 4、`@better-auth/scim` 2、`@better-auth/passkey` 1、`@better-auth/stripe` 1。最新一則 `GHSA-mx9r-x6ww-qjw9`（2026-09-29，`@better-auth/sso`，high）的修正版本是 `1.7.3`。
  來源：<https://github.com/better-auth/better-auth/security/advisories>、<https://github.com/better-auth/better-auth/security/advisories/GHSA-mx9r-x6ww-qjw9>
- 登入頁：官方文件的範例都由應用程式自己寫登入頁（例：`BAD/basic-usage.mdx#L187` 的 `router.push("/login")`）。文件沒有提供內建登入頁（推論：在 `docs/content/docs` 中找不到內建 UI 的說明）。

### 3.2 登入方式與設定

| 方式 | 套件 | 設定 | 需要資料庫 | 來源 |
|---|---|---|---|---|
| email/password | 內建 | `emailAndPassword: { enabled: true }`，預設 `false` | 是（`user`、`account` 表） | `BAD/authentication/email-password.mdx#L17-L27`、`#L470-L552` |
| passkey | `@better-auth/passkey`（以 SimpleWebAuthn 實作） | `plugins: [passkey()]`，再 `npx auth migrate` 建 `passkey` 表 | 是 | `BAD/plugins/passkey.mdx#L9-L72` |
| SSO（OIDC、OAuth2、SAML 2.0） | `@better-auth/sso` | `plugins: [sso()]`，再以 `registerSSOProvider` 註冊 IdP | 是 | `BAD/plugins/sso.mdx#L10`、`#L82-L160` |
| 社群登入 | 內建 `socialProviders` | 例：`socialProviders.cloudflare: { clientId, clientSecret }`。文件列出約 38 個 provider | 可不用（stateless 模式） | `BAD/authentication/`、`BAD/authentication/cloudflare.mdx#L28-L44`、`BAD/concepts/session-management.mdx#L383-L403` |
| 其他 | two-factor（TOTP、OTP、backup codes）、magic link、email OTP、Have I Been Pwned 檢查、captcha | 各自的 plugin | 大多需要 | `BAD/plugins/2fa.mdx`、`BAD/plugins/have-i-been-pwned.mdx` |

設定細節：

- email/password：`minPasswordLength` 預設 8，`maxPasswordLength` 預設 128，`disableSignUp` 預設 `false`，`requireEmailVerification` 預設 `false`。
  來源：`BAD/authentication/email-password.mdx#L470-L530`
  推論：單人使用時應設 `disableSignUp: true`，否則任何能連到登入頁的人都能註冊帳號。
- passkey：預設 `registration.requireSession` 為 `true`，也就是要先以其他方式登入才能新增 passkey。設為 `false` 時要提供 `resolveUser`。
  來源：`BAD/plugins/passkey.mdx#L84-L100`、`#L134`、`BA/packages/passkey/src/routes.ts#L74-L105`
  推論：單人部署要另有一個「第一次登入」的方式（例如先開 password 再加 passkey，或自己簽發一次性註冊 token 給 `resolveUser`）。
- passkey 的 `rpID` 預設取 `baseURL` 的 hostname，沒有 `baseURL` 時是 `localhost`。`origin` 預設為 `null`，驗證時退回使用請求的 `Origin` header。
  來源：`BA/packages/passkey/src/utils.ts#L3-L5`、`BA/packages/passkey/src/index.ts#L34-L36`、`BA/packages/passkey/src/routes.ts#L593`、`#L836`
  推論：退回 `Origin` header 不會讓釣魚網站取得有效的 assertion，因為瀏覽器只允許 RP ID 等於或為 origin 的 registrable domain suffix（見 5.2）。仍建議明確設定 `origin`。
- SSO：OIDC 的 redirect URL 由 provider ID 產生，格式為 `{baseURL}/api/auth/sso/callback/<providerId>`。註冊時自動抓 `{issuer}/.well-known/openid-configuration`。抓取的 URL 必須在 `trustedOrigins` 內，否則回 `discovery_untrusted_origin`。伺服器端的 OIDC 請求不跟隨 redirect。
  來源：`BAD/plugins/sso.mdx#L86`、`#L169-L175`、`#L249-L253`、`#L294`
- SSO 的 `/sso/register` 需要已登入的 session（文件標 `requireSession`）。
  來源：`BAD/plugins/sso.mdx#L179`

### 3.3 session 的形式

- 主要的 cookie 是 `session_token`：「an opaque, secret-signed session identifier」。session 本身存在資料庫或 secondary storage。
  來源：`BAD/concepts/cookies.mdx#L9`、`BAD/reference/security.mdx#L25`
- cookie 名稱為 `better-auth.<name>`，Secure 模式時加上 `__Secure-` 前綴。屬性預設 `sameSite: "lax"`、`path: "/"`、`httpOnly: true`。
  來源：`BA/packages/better-auth/src/cookies/index.ts#L92-L115`
- `Secure` 屬性的決定順序（原始碼）：`advanced.useSecureCookies` > 動態 `baseURL` 的 `protocol` > 靜態 `baseURL` 是否以 `https://` 開頭 > `NODE_ENV === "production"`。
  來源：`BA/packages/better-auth/src/cookies/index.ts#L53-L75`
  注意：`BAD/concepts/cookies.mdx#L33`、`#L96` 寫「secure only when the server is running in production mode」，與原始碼的順序不完全一致。`BAD/reference/security.mdx#L136` 寫「when the base URL uses `https`」，與原始碼一致。本文以原始碼為準。
- 有效期：預設 7 天。每次使用時若超過 `updateAge`（預設 1 天）就延長。`freshAge` 預設 1 天。
  來源：`BAD/reference/security.mdx#L25`、`BAD/concepts/session-management.mdx#L25-L36`、`#L74`
- 資料庫：核心 schema 需要 `user`、`session`、`account`、`verification` 四張表。支援 SQLite、PostgreSQL、MySQL 等。
  來源：`BAD/concepts/database.mdx#L4`、`#L328-L330`、`BAD/installation.mdx#L74-L90`
- 沒有設定資料庫時進入 stateless 模式，session 放在簽章或加密的 cookie。文件說「most plugins will require a database」。
  來源：`BAD/installation.mdx#L77`、`BAD/concepts/session-management.mdx#L383-L389`
  推論：password 與 passkey 都要存使用者資料，所以需要資料庫。單人部署用 SQLite 檔案即可。
- 秘密：`BETTER_AUTH_SECRET`。`NODE_ENV=production` 時使用預設值會拋錯。支援版本化輪替（`BETTER_AUTH_SECRETS`）。
  來源：`BA/packages/better-auth/src/context/create-context.ts#L61-L80`、`BAD/reference/security.mdx#L13-L19`

### 3.4 給 agent 用的 plugin

| plugin | 做什麼 | 對本案的限制 | 來源 |
|---|---|---|---|
| API Key（`@better-auth/api-key`） | 建立、驗證、限速、設權限、到期 | `/api-key/verify` 是 **server-only** 端點：只能在 Node 程式內以 `auth.api.*` 呼叫，不註冊在 HTTP router 上。預設以 SHA-256 雜湊保存 key | `BAD/plugins/api-key/index.mdx#L175`、`BA/packages/api-key/src/routes/verify-api-key.ts#L514`、`BA/packages/core/src/api/index.ts#L183-L195`、`BA/packages/api-key/src/index.ts#L27-L28` |
| Bearer | 以 `Authorization: Bearer <session token>` 取代 cookie | 文件警告「Use this cautiously」，範例把 token 存在 `localStorage` | `BAD/plugins/bearer.mdx#L11`、`#L43` |
| JWT | `/api/auth/token` 簽發 JWT，`/api/auth/jwks` 公開公鑰，外部服務可離線驗證 | 預設 EdDSA / Ed25519，預設效期 `15m`。文件說它「not meant as a replacement for the session」 | `BAD/plugins/jwt.mdx#L1-L16`、`#L138-L145`、`#L436`、`BA/packages/better-auth/src/plugins/jwt/sign.ts#L282` |

推論：Python 的 fava middleware 不能直接透過 HTTP 驗證 better-auth 的 API key。要嘛在 Node 應用程式另開一個端點包裝 `auth.api.verifyApiKey`，要嘛讀同一個資料庫自行比對雜湊。兩者都比計畫中的單一 `AGENT_API_TOKEN` 環境變數多一層。

---

## 4. better-auth 保護 fava（WSGI）的整合方式（研究問題 2）

### 4.1 官方文件記載的內容

- better-auth 的文件沒有 forward auth、nginx `auth_request`、Traefik 或 Caddy 整合的說明。`reverse proxy` 只出現在 IP header、cookie 跨網域與 OAuth callback 錯誤排除的段落。
  來源：在 `BAD` 全文搜尋 `forward auth`、`nginx`、`traefik`、`caddy`、`python`、`flask`，只命中 `BAD/reference/errors/*.mdx`、`BAD/concepts/cookies.mdx#L134-L137`、`BAD/concepts/rate-limit.mdx#L80`
- 官方給「非 better-auth 服務」的途徑是 JWT plugin：外部服務以 JWKS 驗證，「without the need for an additional verify call or database check」。
  來源：`BAD/plugins/jwt.mdx#L138-L141`
- session 查詢端點 `GET /api/auth/get-session` 註冊在 HTTP router 上。預設 GET 也會寫資料庫以延長 session，可用 `session.deferSessionRefresh` 改成唯讀。
  來源：`BA/packages/better-auth/src/api/routes/session.ts#L31`、`BAD/concepts/session-management.mdx#L58-L70`
- 若啟用 `session.cookieCache.strategy = "jwt"` 並在 `jwt()` 設 `sessionCookieCache: true`，`session_data` cookie 可用 JWKS 驗證。`session_data` 的預設 `maxAge` 是 5 分鐘。
  來源：`BAD/plugins/jwt.mdx#L14-L16`、`BA/packages/better-auth/src/cookies/index.ts#L126-L131`

### 4.2 可行的整合形態（皆為推論）

| 形態 | middleware 做什麼 | 問題 |
|---|---|---|
| 每個請求轉問 `get-session` | 把瀏覽器的 `Cookie` header 轉送到 Node 的 `/api/auth/get-session`，依結果放行 | fava 前端每 5 秒輪詢 `api/changed`（既有研究 1.1 節），每次都多一個 HTTP 呼叫。Node 停止時 fava 全部 401 |
| 驗證 JWT cookie cache | 以 JWKS 驗證 `session_data` cookie | cookie 預設 5 分鐘過期。更新要由 better-auth client 呼叫 `get-session`，但 fava 前端不會呼叫。middleware 要在過期時把瀏覽器導到登入頁或 Node 端的更新端點 |
| 反向代理加 forward auth | 前面加 nginx 或 Traefik，以 `get-session` 判斷 | 多一個 container。區網 5656 要改由代理監聽 |

部署影響（推論）：

1. 多一個 Node container（better-auth 應用程式與登入頁）。這是要自己寫、自己維護、隨 better-auth 升版的程式碼。
2. 多一個資料庫（SQLite 檔案或另一個資料庫 container）。
3. 登入頁與 `/api/auth/*` 要和 fava 同一個 host，cookie 才會送到 fava。Cloudflare 路徑可用 tunnel ingress 的 path 路由（既有研究 11.1 節）。區網路徑只有 5656 一個 port 時，要由 middleware 轉送 `/api/auth/*` 到 Node，或另加反向代理。也可以讓 Node 監聽 `192.168.2.11` 的另一個 port，因為 cookie 不以 port 隔離（第 2 節第 3 點）。
4. 兩條路徑的 origin 不同。一個 better-auth 實例要服務兩者，要用動態 `baseURL`（`allowedHosts`）。靜態 `baseURL` 為 `https://` 時所有 cookie 都帶 `Secure` 與 `__Secure-` 前綴，區網的 `http` 頁面無法設定這些 cookie（rfc6265bis-22 第 5.7 節步驟 13、20）。
   來源：`BAD/guides/dynamic-base-url.mdx#L14-L30`、`#L145-L151`、<https://www.ietf.org/archive/id/draft-ietf-httpbis-rfc6265bis-22.txt>
5. 速率限制以 IP 為鍵，預設讀 `X-Forwarded-For`。文件建議在代理後改用單一可信 header（例如 `cf-connecting-ip`）或設定 `trustedProxies`，並警告 client 可以直接送 `X-Forwarded-For` 偽造位址。
   來源：`BAD/concepts/rate-limit.mdx#L55-L110`、`BAD/reference/security.mdx#L150-L188`
   推論：區網路徑沒有 Cloudflare，區網 client 可以自己送 `cf-connecting-ip`。兩條路徑共用一個 better-auth 實例時，IP header 的設定只能對其中一條正確。

---

## 5. 各登入方式的安全性質（研究問題 3）

### 5.1 password（better-auth）

| 項目 | 值 | 來源 |
|---|---|---|
| 演算法 | scrypt。文件引用 OWASP：沒有 argon2id 時建議 scrypt。可自訂 `password.hash` / `verify`（例：Argon2id） | `BAD/authentication/email-password.mdx#L425-L466` |
| 參數 | `N: 16384`、`r: 16`、`p: 1`、`dkLen: 64`；salt 16 byte 隨機；輸入先 `normalize("NFKC")` | `BAU/src/password.node.ts#L3-L32` |
| 比對 | `targetKey.toString("hex") === key`（字串比較，不是 `timingSafeEqual`） | `BAU/src/password.node.ts#L45` |
| 記憶體成本 | `128 * N * r` = 32 MiB（推論，依 scrypt 的定義計算） | 同上 |
| 全域速率限制 | 60 秒 100 次 | `BAD/concepts/rate-limit.mdx#L9-L10` |
| 登入速率限制 | `/sign-in*`、`/sign-up*`、`/change-password*`、`/change-email*`：10 秒 3 次 | `BA/packages/better-auth/src/api/rate-limiter/index.ts#L439-L452` |
| 啟用條件 | `rateLimit.enabled` 預設等於 `isProduction`，也就是 `NODE_ENV === "production"` | `BA/packages/better-auth/src/context/create-context.ts#L359`、`BA/packages/core/src/env/env-impl.ts#L52` |
| 儲存 | 預設在記憶體；可改資料庫或 secondary storage | `BAD/concepts/rate-limit.mdx#L216` |
| 限制的鍵 | IP + path；取不到可信 IP 時所有 client 共用一個鍵 | `BA/packages/better-auth/src/api/rate-limiter/index.ts#L355-L359` |
| 帳號鎖定 | 密碼登入沒有。只有 two-factor 驗證有 `accountLockout`（預設 10 次、900 秒） | `BA/packages/better-auth/src/plugins/two-factor/constant.ts#L8-L11`、`BAD/plugins/2fa.mdx#L630-L648` |
| CSRF | 檢查 `Origin`、Fetch Metadata；session cookie `SameSite=Lax` | `BAD/reference/security.mdx#L33-L70` |

推論：

- 10 秒 3 次是每個 IP 的上限。沒有帳號層的鎖定，所以分散來源 IP 的線上猜測不受這條規則限制。
- 在 `http://192.168.2.11` 路徑上，密碼在登入時以明文送出，之後的 session cookie 在每個請求中以明文送出。RFC 6265 §8.3：「Unless sent over a secure channel (such as TLS), the information in the Cookie and Set-Cookie headers is transmitted in the clear.」
  來源：<https://www.rfc-editor.org/rfc/rfc6265#section-8.3>
- password 不具備 phishing resistance。使用者在假網站輸入密碼，密碼就外洩。這是與 5.2 的主要差別。

### 5.2 passkey（WebAuthn Level 3）

規格事實：

1. **只在 secure context 提供。** `PublicKeyCredential` 介面標註 `[SecureContext, Exposed=Window]`。規格說：「user agents only expose this API to callers in secure contexts.」§13.4.8 又說「WebAuthn Clients only expose the WebAuthn API in secure contexts」。
   來源：`WA#sctn-api`、`WA#sctn-code-injection`
2. **effective domain 必須是 valid domain。** `[[Create]]` 的步驟：「If effective domain is not a valid domain, then throw a "SecurityError" DOMException.」附註：「Only the domain format of host is allowed here. This is for simplification and also is in recognition of various issues with using direct IP address identification in concert with PKI-based security.」`[[DiscoverFromExternalSource]]` 有同樣的步驟。
   來源：`WA#sctn-createCredential`、`WA#sctn-discover-from-external-source`
3. **RP ID 規則。** RP ID 是「a valid domain string」。必須等於 origin 的 effective domain，或是它的 registrable domain suffix。另外 origin 的 scheme 必須是 `https`，或 host 是 `localhost` 且 scheme 是 `http`。
   來源：`WA#rp-id`
4. **WHATWG URL 的 valid domain 排除 IP。** 「A string input is a valid domain if these steps return true」，其中一步是「If running the ends in a number checker on domain returns true, then return false.」`192.168.2.11` 以數字結尾。
   來源：<https://url.spec.whatwg.org/#valid-domain>
5. **Secure Contexts 的判定。** 「Is origin potentially trustworthy?」只對 `https`/`wss`、`127.0.0.0/8`、`::1/128`、`localhost` 與 `.localhost`、`file`、使用者代理視為已認證的 scheme、以及開發者手動設定的 origin 回傳 potentially trustworthy。規格附註：「Neither origin's domain nor port has any effect on whether or not it is considered to be a secure context.」
   來源：<https://www.w3.org/TR/secure-contexts/#is-origin-trustworthy>

套用到本案：

| 路徑 | secure context | RP ID | passkey 可用 |
|---|---|---|---|
| `http://192.168.2.11:5656` | 否（`http` + 非 loopback IP） | 無（IP 不是 valid domain） | **否**。兩個條件各自足以拒絕 |
| `https://<Cloudflare hostname>` | 是 | hostname 或其 registrable domain suffix | **是** |

推論：

- 使用者在瀏覽器手動把 `http://192.168.2.11:5656` 設為可信 origin（Secure Contexts 7.2 節的開發環境例外）也只解決第 1 點。第 2 點的 IP 限制仍然成立。
- 要在區網用 passkey，區網也要用 `https` + domain，而且 RP ID 要與 Cloudflare 網址相容（例如同一個 registrable domain）。這需要區網 DNS 與 TLS 憑證。不在本文查證範圍。
- passkey 的安全性質：私鑰不離開 authenticator，伺服器只存公鑰，憑證綁定 RP ID（`WA#rp-id`）。所以伺服器資料庫外洩不會洩漏可重用的秘密，也不能在其他網域的假網站使用。

### 5.3 SSO

- 安全性取決於 IdP。better-auth 只負責 OIDC/SAML 協定的 client 端。
- **redirect URI 必須是 HTTPS。** RFC 9700 §2.6：「Authorization responses MUST NOT be transmitted over unencrypted network connections. To this end, authorization servers MUST NOT allow redirection URIs that use the http scheme except for native clients that use loopback interface redirection」。RFC 6749 §3.1.2.1 原本只是 SHOULD。
  來源：<https://www.rfc-editor.org/rfc/rfc9700#section-2.6>、<https://www.rfc-editor.org/rfc/rfc6749#section-3.1.2.1>
- redirect URI 比對：RFC 9700 §2.1 要求 authorization server 用 exact string matching。
  來源：<https://www.rfc-editor.org/rfc/rfc9700#section-2.1>
- better-auth 的 Cloudflare 社群登入文件也是本機用 `http://localhost:3000/...`，正式環境用 `https://example.com/...`。
  來源：`BAD/authentication/cloudflare.mdx#L19`
- better-auth 的 OAuth 流程以 state 與 PKCE 防 CSRF 與 code injection。
  來源：`BAD/reference/security.mdx#L128-L132`
- `@better-auth/sso` 有 5 則公開的 high 或 critical 公告，其中多則是帳號接管類（例：`GHSA-prpr-5gj3-qqhg`「Four SSO flaws let an attacker sign in as another user」、`GHSA-5rr4-8452-hf4v`「Any signed-in user can make the server fetch and read internal URLs」）。
  來源：<https://github.com/better-auth/better-auth/security/advisories>

推論：

- 區網 `http://192.168.2.11` 的 better-auth 不能當 SSO 的 redirect URI。SSO 只能在 Cloudflare 網址使用。
- SSO plugin 是給多租戶或企業 IdP 用的。單人只接一個 IdP 時，內建的 `socialProviders` 設定較少、程式面較小。
- 使用者已經在 Cloudflare Access 前面登入一次。better-auth 的 SSO 再接一個 IdP，瀏覽器會經過兩次登入（Access 一次、better-auth 一次），除非把 Access 對 fava 設成 Bypass。

---

## 6. Cloudflare Access 本身的登入方式

| 方式 | 事實 | 來源 |
|---|---|---|
| Cloudflare identity provider | 建立 Zero Trust 組織時自動加入。以 Cloudflare 帳號成員資格判斷。可限制為「只有本帳號成員」 | `CF1/integrations/identity-providers/cloudflare/` |
| One-time PIN | 寄到信箱。PIN 在請求後 10 分鐘過期、只能用一次、重新申請會使舊的失效。寄件者 `noreply@notify.cloudflare.com` | `CF1/integrations/identity-providers/one-time-pin/` |
| 外部 IdP | Entra ID、Google、GitHub、Okta、Keycloak、Generic OIDC、Generic SAML 等。官方建議 OIDC 優於 SAML | `CF1/integrations/identity-providers/` |
| Independent MFA | 在 Access 內要求第二因素，不依賴 IdP 的 MFA。支援 TOTP app、security key（WebAuthn）、biometrics（WebAuthn：Touch ID、Face ID、Windows Hello）。可在組織、application、policy 三層設定。可用 AAGUID 限制可註冊的 authenticator | `CF1/access-controls/access-settings/independent-mfa/`、<https://developers.cloudflare.com/changelog/post/2026-04-15-independent-mfa/>、<https://developers.cloudflare.com/changelog/post/2026-04-23-independent-mfa-aaguid-amr/> |
| Cloudflare 帳號本身的 2FA | security key（WebAuthn）、TOTP、email | <https://developers.cloudflare.com/fundamentals/user-profiles/2fa/> |
| `CF_Authorization` cookie | 預設 HttpOnly。SameSite 預設 `None`，可改 Lax 或 Strict。可開 Binding Cookie（`CF_Binding`），使被盜的 `CF_Authorization` 無法單獨使用。效期依 policy 或 application 的 session duration，都沒設時 24 小時 | `CF1/access-controls/applications/http-apps/authorization-cookie/` |

- Access 的 identity providers 頁沒有把 passkey 列為主要登入方式。WebAuthn 只出現在 Independent MFA（第二因素）與 Cloudflare 帳號的 2FA。
  來源：`CF1/integrations/identity-providers/`
- Independent MFA 在哪些方案可用：**未知**。文件與 changelog 都沒有寫。
- Cloudflare identity provider 是否強制套用 Cloudflare 帳號的 2FA：**未知**。
- origin 端驗證 `Cf-Access-Jwt-Assertion` 的做法見既有研究 9.5 節，2026-09-30 重新抓取仍一致：驗 header 而不是 cookie；公鑰從 `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` 取；以 `kid` 比對 `public_certs`；驗 `aud`、`iss`、`exp`；官方有 Python（PyJWT）範例；簽章金鑰每 6 週輪替。
  來源：`CF1/access-controls/applications/http-apps/authorization-cookie/validating-json/`
- application-token 頁：「Validation of the header alone is not sufficient — the JWT and signature must be confirmed to avoid identity spoofing.」
  來源：`CF1/access-controls/applications/http-apps/authorization-cookie/application-token/`

---

## 7. 以來源 IP 信任 cloudflared（選項 e）

### 7.1 Docker 文件的事實

- 同一個 user-defined bridge 上的 container 互相開放所有 port：「Containers connected to the same user-defined bridge network effectively expose all ports to each other.」預設 bridge 會接上所有沒有指定 `--network` 的 container。
  來源：`DD/network/drivers/bridge.md`
- Docker 預設保留的 capability 包含 `CAP_NET_RAW`。
  來源：<https://github.com/moby/moby/blob/367ff5729a24b630d0072c6dc3e0c7cb4481e4de/daemon/pkg/oci/caps/defaults.go#L11>
- Docker 安全文件把「Deny access to raw sockets (to prevent packet spoofing)」列為縮減 capability 的效果。
  來源：`DD/security/_index.md#L184`
- 從遠端主機直接連 container IP：預設不允許，只能連到 publish 到 host IP 的 port。可用 `allow-direct-routing` 或 `trusted_host_interfaces` 放寬。舊於 28.0.0 的版本，同一 L2 網段的主機可以連到 publish 在 localhost 的 port。
  來源：`DD/network/port-publishing.md#L81-L96`、<https://docs.docker.com/engine/network/port-publishing/>
- `DOCKER-USER` chain 看到的封包已經過 DNAT。
  來源：`DD/network/firewall-iptables.md#L81-L86`
- macvlan：「Containers attached to a macvlan network cannot communicate with the host directly, this is a restriction in the Linux kernel.」
  來源：`DD/network/drivers/macvlan.md#L47-L48`
- 經由 published port 或 userland proxy 進來的連線，container 看到的來源 IP 是什麼：Docker 文件沒有明寫。**未知**。

### 7.2 Unraid 文件的事實

- Bridge（預設）：「The container is placed on an internal Docker network. Only ports you explicitly map will be accessible from your Unraid server or LAN.」
- Host：「The container shares the Unraid server's network stack.」
- Custom（macvlan/ipvlan）：「The container is assigned its own IP address on your LAN, making it appear as a separate device.」
  來源：<https://docs.unraid.net/unraid-os/using-unraid-to/run-docker-containers/managing-and-customizing-containers/>
- 自 6.11.5 起 custom network 預設為 ipvlan。host 與 custom network 的 container 互通要開「Host access to custom networks」。
  來源：<https://docs.unraid.net/unraid-os/release-notes/6.12.4/>、<https://docs.unraid.net/unraid-os/troubleshooting/common-issues/docker-troubleshooting/>
- Unraid 7.2.x 更新到 Docker 29。
  來源：<https://docs.unraid.net/unraid-os/release-notes/7.2.5/>
- 使用者的 Unraid 版本、cloudflared 用哪一種網路模式、cloudflared 以什麼位址連 fava：**未知**，要由使用者確認。

### 7.3 各網路模式下「信任 cloudflared 的 IP」代表信任誰（推論）

| cloudflared 的模式 | fava 看到的來源 IP（推論） | 能送出同一來源 IP 的對象（推論） |
|---|---|---|
| 與 fava 同一個 user-defined bridge，以 container 名稱連線 | cloudflared 的 bridge IP | 同一 bridge 上任何有 `CAP_NET_RAW` 的 container（Docker 預設有） |
| custom br0（ipvlan/macvlan），連 `192.168.2.11:5656` | cloudflared 的區網 IP | 區網上任何把自己設成該 IP 的裝置 |
| host 模式，連 `127.0.0.1:5656` 或 `192.168.2.11:5656` | bridge gateway 位址或 host 位址（未知，要實測） | Unraid 主機上的所有 process 與所有 host 模式的 container |

推論：

- 來源 IP 不是密碼學證明。三種模式下被信任的範圍都比「cloudflared 這個 process」大。
- 即使 IP 信任精確，e 仍然只是把判斷交給 cloudflared。cloudflared 轉送的每一個請求都被放行，所以安全性完全取決於 Cloudflare 端的 Access 設定正確、沒有同帳號內的繞過（既有研究 9.6 節列出的途徑）。Cloudflare 官方要求 origin 驗證 token 的理由就是「any requests which bypass Cloudflare Access (for example, due to a network misconfiguration) are rejected」（既有研究 9.6 節引文）。
- e 與 d 的差別：d 驗證的是 Cloudflare 私鑰簽出的 JWT，攻擊者在區網或 bridge 上無法偽造。e 驗證的是封包的來源欄位。

---

## 8. HTTP Basic（選項 f，RFC 7617）

- 「The most serious flaw of Basic authentication is that it results in the cleartext transmission of the user's password over the physical network.」
- 「Because Basic authentication involves the cleartext transmission of passwords, it SHOULD NOT be used (without enhancements such as HTTPS [RFC2818]) to protect sensitive or valuable information.」
- 「Basic authentication is also vulnerable to spoofing by counterfeit servers.」
- 伺服器應以難以還原的形式保存密碼，不要存明文或無 salt 的 digest。
  來源：<https://www.rfc-editor.org/rfc/rfc7617#section-4>

推論：

- f 與 fava 前端相容。瀏覽器登入一次後，同源 fetch 自動帶 `Authorization: Basic`（第 2 節第 1 點）。
- 區網路徑每個請求都帶明文密碼。Cloudflare 路徑在瀏覽器到 Cloudflare 之間有 TLS，cloudflared 到 fava 是 host 內的 HTTP。
- 沒有速率限制、沒有登出、沒有 phishing resistance。速率限制要 middleware 自己做。
- 走 Cloudflare 路徑時，使用者會先過 Access，再過 Basic，共兩次。

---

## 9. 比較表（研究問題 4）

「區網」指 `http://192.168.2.11:5656`。「CF」指 Cloudflare 網址。「強度」欄只看瀏覽器憑證本身。

| 選項 | 瀏覽器憑證的強度 | 區網瀏覽器 | CF 瀏覽器 | 新增元件 | 要維護的秘密 | 主要弱點 |
|---|---|---|---|---|---|---|
| a. better-auth + password | 中。scrypt（N=16384,r=16,p=1）；每 IP 10 秒 3 次；無帳號鎖定；可被釣魚 | 可用，但密碼與 cookie 明文（RFC 6265 §8.3） | 可用；前面還有 Access，要登入兩次 | Node container、資料庫、登入頁、session 驗證整合 | `BETTER_AUTH_SECRET`、使用者密碼、資料庫檔、Agent token | 區網明文；better-auth 公告多；cookie 在同 IP 的其他 port 可見 |
| b. better-auth + passkey | 高。私鑰不離開裝置、綁定 RP ID、抗釣魚（`WA#rp-id`） | **不可用**（非 secure context；IP 不是 valid domain） | 可用；前面還有 Access | 同 a，另要一個首次登入或註冊方式 | `BETTER_AUTH_SECRET`、資料庫檔（公鑰不是秘密）、首次註冊憑證、Agent token | 只在 CF 可用，而 CF 已有 Access；部署成本最高 |
| c. better-auth + SSO | 取決於 IdP | **不可用**（RFC 9700 §2.6 禁止 `http` redirect URI） | 可用；前面還有 Access | 同 a，另要 IdP 設定 | `BETTER_AUTH_SECRET`、OAuth client secret、IdP 帳號、Agent token | `@better-auth/sso` 有多則帳號接管類公告；與 Access 重複 |
| d. 只信任 Access（驗 JWT） | 取決於 Access 的登入方式。加 Independent MFA 的 WebAuthn 可達到 b 的等級。origin 以 Cloudflare 簽章驗證 | **不可用**（設計上只給 Agent） | 可用，只登入一次 | middleware 內的 JWT 驗證；`pyjwt` 與 `cryptography`（目前不在 `uv.lock`） | Agent token。AUD tag 與 team 名稱不是秘密。簽章金鑰由 Cloudflare 管理 | 區網瀏覽器失去直連；對外網路中斷時瀏覽器不能用；fava 要能連到 `<team>.cloudflareaccess.com` 取公鑰 |
| e. 信任 cloudflared 的 IP | 同 d 的 Access 登入，但 origin 不驗證任何密碼學證明 | 不可用 | 可用 | middleware 內的 IP 判斷 | Agent token | 信任範圍大於 cloudflared（7.3）；依賴 Cloudflare 端設定沒有繞過 |
| f. HTTP Basic | 低至中。依密碼強度；無速率限制；可被釣魚 | 可用，但每個請求都帶明文密碼 | 可用；前面還有 Access | middleware 內的 Basic 驗證 | Basic 密碼（以雜湊保存）、Agent token | 區網明文；RFC 7617 §4 說沒有 HTTPS 時 SHOULD NOT 用於敏感資料 |

補充：

- a、b、c 在 CF 路徑上都疊在 Access 之後。若要避免兩次登入，要把 Access 對 fava 設為 Bypass，這會拿掉 Cloudflare 端的保護（推論）。
- d 與 e 在區網都不給瀏覽器使用。差別只在 origin 的驗證方式。
- 所有選項的 Agent 都用同一個 Bearer token，秘密數量相同。

---

## 10. 結論（研究問題 5）

### 10.1 安全性

排序（推論，依上列事實）：

1. **d + Access 的 WebAuthn 第二因素**。瀏覽器憑證可抗釣魚（Independent MFA 的 security key 或 biometrics 是 WebAuthn）。origin 以 Cloudflare 的簽章驗證，區網與 Docker 網路上的攻擊者無法偽造（第 6 節）。image 內不存任何瀏覽器用的秘密，也不多一個認證服務。
2. **b**。憑證強度與 1 相同。但只能在 CF 路徑使用（5.2），而 CF 路徑前面已經有 Access。b 額外帶來 Node 服務、資料庫、自寫登入頁與 better-auth 的程式面（34 則公告，第 3.1 節）。多出來的元件不增加憑證強度，只增加可能出錯的地方。
3. **d（Access 只用 One-time PIN 或 IdP 密碼）**。強度等於信箱或 IdP 帳號的強度。origin 驗證仍是密碼學的。
4. **c**。取決於 IdP，只能在 CF 路徑使用，SSO plugin 的公告歷史較差。
5. **e**。Access 端的登入強度與 d 相同，但 origin 不驗證 token。信任範圍取決於網路模式（7.3）。
6. **a**。在 CF 路徑尚可；在區網路徑密碼與 cookie 都是明文，而且可被釣魚。
7. **f**。在區網路徑每個請求都是明文密碼。

關鍵事實：在 `http://192.168.2.11:5656` 這條路徑上，**沒有任何選項能提供強的瀏覽器認證**。passkey 與 SSO 被規格排除（5.2、5.3）。password 與 Basic 都以明文傳送（RFC 6265 §8.3、RFC 7617 §4）。better-auth 不改變這個事實。

### 10.2 方便性

| 需求 | 最方便的選項 | 理由 |
|---|---|---|
| 只在外部或透過 Cloudflare 網址使用瀏覽器 | d | 只登入一次（Access）。不多任何服務 |
| 區網瀏覽器一定要能直連 5656 | f | 瀏覽器內建登入框。middleware 只需比對一個 header（推論）。fava 前端不必改 |
| 想要 passkey 體驗 | d + Independent MFA | 不必自架 better-auth。方案可用性未知 |

### 10.3 建議

**建議採用 d。** 理由：

1. 使用者已經在用 Access。d 讓 Access 成為瀏覽器唯一的入口，middleware 用 Cloudflare 官方的方式驗證 JWT（第 6 節）。
2. 在使用者的條件下，better-auth 的三種方式中安全的兩種（b、c）只能在 CF 路徑使用，那條路徑已經有 Access。剩下能在區網用的 a 沒有解決明文問題。
3. d 不需要 Node container、資料庫、登入頁，也不需要在 image 內保存瀏覽器用的秘密。
4. 想要 passkey 時，在 Access 開 Independent MFA 並限制為 security key 或 biometrics。

d 的實作要點（引用自既有研究 9.5 節與本文第 6 節）：

- 只讀 `Cf-Access-Jwt-Assertion` header，不讀 cookie。
- 驗簽章、`aud`、`iss`、`exp`。以 `kid` 比對 `public_certs`，不要寫死公鑰。
- 沒有有效 JWT 時，只接受 Agent 的 Bearer token，且只放行 `/api/` 的 GET 與 extension 的寫入端點。
- 可以同時開 tunnel 的 Protect with Access（既有研究 9.6 節 (a)）與帳號層的 Require Access protection。

推論的剩餘風險：

- 取得有效 JWT 的人（例如從瀏覽器記錄或日誌）在 `exp` 之前可以直接送到區網 5656。縮短 Access 的 session duration 可以縮小這個視窗。Binding Cookie 保護的是 `CF_Authorization` cookie 在 Cloudflare 端的重用，不涵蓋直接打 origin 的情況。
- fava 要能連到 `<team>.cloudflareaccess.com` 取公鑰。連不到時 middleware 的行為要設計成拒絕（fail closed）。

**若區網瀏覽器必須保留**：短期用 f 並接受區網明文的風險，或把區網也改成 HTTPS + domain 再評估 b。後者需要區網 DNS 與 TLS 憑證，不在本文查證範圍。

---

## 11. 未知事項

| 項目 | 狀態 |
|---|---|
| Cloudflare Access Independent MFA 在免費方案是否可用 | 未知。文件與 changelog 都沒寫 |
| Cloudflare identity provider 是否強制套用 Cloudflare 帳號的 2FA | 未知 |
| 經由 published port、userland proxy 或 host 模式連入時，fava container 看到的來源 IP | 未知。Docker 文件沒寫，要實測 |
| 使用者 Unraid 的版本、Docker 版本、cloudflared 的網路模式與連 fava 的位址 | 未知。要由使用者確認 |
| better-auth 是否有官方的 forward auth 或反向代理整合 | 文件沒有記載（第 4.1 節的搜尋結果）。視為沒有 |
| better-auth 是否提供內建登入頁 | 文件沒有記載，視為沒有（推論） |
| scrypt 參數（N=16384、r=16、p=1）與外部建議值的比較 | 不在本文的來源範圍（本文只用指定的第一手來源）。未比較 |
| RFC 6265bis 何時成為 RFC | 未知。datatracker 顯示最新為 `-22`，狀態包含 RFC Ed Queue |

---

## 12. 來源清單

### better-auth（commit `229a02a`，tag `v1.7.6`）

- 文件：`BAD/introduction.mdx`、`BAD/installation.mdx`、`BAD/basic-usage.mdx`、`BAD/reference/security.mdx`、`BAD/concepts/cookies.mdx`、`BAD/concepts/session-management.mdx`、`BAD/concepts/database.mdx`、`BAD/concepts/rate-limit.mdx`、`BAD/authentication/email-password.mdx`、`BAD/authentication/cloudflare.mdx`、`BAD/plugins/passkey.mdx`、`BAD/plugins/sso.mdx`、`BAD/plugins/jwt.mdx`、`BAD/plugins/bearer.mdx`、`BAD/plugins/api-key/index.mdx`、`BAD/plugins/2fa.mdx`、`BAD/plugins/have-i-been-pwned.mdx`、`BAD/guides/dynamic-base-url.mdx`
  （`BAD` = <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/docs/content/docs>）
- 原始碼：
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/better-auth/src/cookies/index.ts#L45-L118>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/better-auth/src/api/rate-limiter/index.ts#L355-L468>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/better-auth/src/context/create-context.ts#L61-L80>、`#L359`
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/better-auth/src/crypto/password.ts>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/better-auth/src/plugins/two-factor/constant.ts#L8-L11>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/better-auth/src/plugins/jwt/sign.ts#L282>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/better-auth/src/api/routes/session.ts#L31>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/core/src/env/env-impl.ts#L52>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/core/src/api/index.ts#L169-L195>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/passkey/src/utils.ts#L3-L5>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/passkey/src/index.ts#L32-L40>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/passkey/src/routes.ts#L74-L105>、`#L593`、`#L836`
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/api-key/src/index.ts#L27-L28>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/packages/api-key/src/routes/verify-api-key.ts#L514>
  - <https://github.com/better-auth/better-auth/blob/229a02a652185ed32e87eab0c77c09d58532e0f1/pnpm-workspace.yaml#L67>
- `@better-auth/utils` `v0.4.2`：<https://github.com/better-auth/utils/blob/b20329a32d78f1f9bcc088bbd6f982b28c4192f1/src/password.node.ts>
- 版本與授權：<https://registry.npmjs.org/better-auth>、<https://github.com/better-auth/better-auth/releases>
- 安全公告：<https://github.com/better-auth/better-auth/security/advisories>

### W3C / WHATWG

- WebAuthn Level 3（W3C Recommendation 2026-08-25）：<https://www.w3.org/TR/2026/REC-webauthn-3-20260825/#rp-id>、`#sctn-api`、`#sctn-createCredential`、`#sctn-discover-from-external-source`、`#sctn-code-injection`
- Secure Contexts：<https://www.w3.org/TR/secure-contexts/#is-origin-trustworthy>
- URL Standard：<https://url.spec.whatwg.org/#valid-domain>
- Fetch Standard：<https://fetch.spec.whatwg.org/#credentials>、<https://fetch.spec.whatwg.org/#concept-request-credentials-mode>

### IETF

- RFC 6265 §8.3、§8.5：<https://www.rfc-editor.org/rfc/rfc6265#section-8>
- draft-ietf-httpbis-rfc6265bis-22（Internet-Draft）：<https://www.ietf.org/archive/id/draft-ietf-httpbis-rfc6265bis-22.txt>、<https://datatracker.ietf.org/doc/draft-ietf-httpbis-rfc6265bis/>
- RFC 6749 §3.1.2.1：<https://www.rfc-editor.org/rfc/rfc6749#section-3.1.2.1>
- RFC 7617 §4：<https://www.rfc-editor.org/rfc/rfc7617#section-4>
- RFC 9700 §2.1、§2.6：<https://www.rfc-editor.org/rfc/rfc9700>

### Cloudflare（2026-09-30 抓取）

- Identity providers：<https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/>
- Cloudflare IdP：<https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/cloudflare/>
- One-time PIN：<https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/>
- Independent MFA：<https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/independent-mfa/>、<https://developers.cloudflare.com/changelog/post/2026-04-15-independent-mfa/>、<https://developers.cloudflare.com/changelog/post/2026-04-23-independent-mfa-aaguid-amr/>
- 帳號 2FA：<https://developers.cloudflare.com/fundamentals/user-profiles/2fa/>
- Authorization cookie：<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/>
- 驗證 JWT：<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/>
- Application token：<https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/>

### Docker（`docker/docs` commit `3633800`）

- Bridge：<https://github.com/docker/docs/blob/3633800c79c473180d51ff64f7dffb05ccfb92c9/content/manuals/engine/network/drivers/bridge.md>
- Macvlan：<https://github.com/docker/docs/blob/3633800c79c473180d51ff64f7dffb05ccfb92c9/content/manuals/engine/network/drivers/macvlan.md#L47-L48>
- Port publishing：<https://github.com/docker/docs/blob/3633800c79c473180d51ff64f7dffb05ccfb92c9/content/manuals/engine/network/port-publishing.md#L64-L96>
- iptables：<https://github.com/docker/docs/blob/3633800c79c473180d51ff64f7dffb05ccfb92c9/content/manuals/engine/network/firewall-iptables.md#L81-L86>
- Security：<https://github.com/docker/docs/blob/3633800c79c473180d51ff64f7dffb05ccfb92c9/content/manuals/engine/security/_index.md#L184>
- moby 預設 capability：<https://github.com/moby/moby/blob/367ff5729a24b630d0072c6dc3e0c7cb4481e4de/daemon/pkg/oci/caps/defaults.go>

### Unraid（2026-09-30 抓取）

- Managing and customizing containers：<https://docs.unraid.net/unraid-os/using-unraid-to/run-docker-containers/managing-and-customizing-containers/>
- Docker troubleshooting：<https://docs.unraid.net/unraid-os/troubleshooting/common-issues/docker-troubleshooting/>
- 6.12.4 release notes：<https://docs.unraid.net/unraid-os/release-notes/6.12.4/>
- 7.2.5 release notes：<https://docs.unraid.net/unraid-os/release-notes/7.2.5/>

### fava（tag `v1.30.16`）

- <https://github.com/beancount/fava/blob/v1.30.16/frontend/src/lib/fetch.ts>
- <https://github.com/beancount/fava/blob/v1.30.16/frontend/src/api/index.ts>
