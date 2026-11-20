# AI 模型轉發站

類似 OpenRouter 的私人練習專案：站方發 API key 給使用者，使用者在 Cherry Studio 這類工具裡
把它設成自訂的 OpenAI 相容 provider；站方依使用的模型轉發給上游，並從使用者的 USDT/USDC
儲值餘額扣款。

- 設計與資料流：[docs/設計.md](docs/設計.md)
- 已防護與尚未防護的攻擊手法：[docs/攻防清單.md](docs/攻防清單.md)
- 正式上線（Windows + win-acme HTTPS、開機自動啟動、備份）：[docs/部署.md](docs/部署.md)

## 第一次啟動

以下指令都在專案根目錄、PowerShell 執行。

**1. 安裝套件**（開發機已經裝好，可以跳過；換電腦時才需要）

```powershell
python -m venv .venv
```
```powershell
.venv\Scripts\Activate.ps1
```
```powershell
pip install -r requirements.txt
```

**2. 建立設定檔**：所有要填的東西都在 `config.ini` 這一個檔案裡。先從範本複製一份：

```powershell
Copy-Item config.example.ini config.ini
```

用記事本或 VS Code 打開 `config.ini`，至少要填：

- `[site]` 的 `invite_code`：朋友註冊時要輸入的邀請碼
- `[upstream:deepseek]` 的 `api_key`：上游給你的真實 API key
- `[model:deepseek-chat]` 的價格：你要賣給朋友的價格（美元 / 100 萬 tokens）

每個欄位的意思都寫在檔案的註解裡。要接其他上游、開放其他模型，就複製整個 `[upstream:...]`、`[model:...]` 區段改名稱。

> `config.ini` 已列在 `.gitignore`，不會進 git。裡面有上游的真實 key：**不要傳給別人、不要貼到聊天室、不要截圖**。外流了就到上游後台把 key 作廢重發。

幾個要注意的設定：

- 想開放圖片輸入，就把 `image_tokens` 設成「一張圖最多算幾 token」。數字要查上游文件的最壞情況，各家差很多；0 = 不接受圖片。
- `cached_input_price`：快取命中的輸入改收這個價格（不能高於 `input_price`）。長對話、agent 會一直重送前面的內容，上游（例如 DeepSeek、OpenAI）認出重複的部分會打折並回報命中數。留空 = 不打折，差價全歸站方；要不要讓利給朋友由你決定。
- OpenAI 的新模型要把 `max_tokens_field` 改成 `max_completion_tokens`。
- 為了避免額外計費，請求參數採白名單制（清單在 [docs/設計.md](docs/設計.md) 5.4）。朋友的工具如果送了白名單外的參數（例如 DeepSeek 的 `thinking`），會收到 400 並看到是哪個參數。確認那個參數不影響計費後，再加進 `app/relay.py` 的白名單。
- 如果上游是 OpenRouter，它可能因為 `tools`、`response_format` 改選較貴的供應商；售價要抓最貴的那家（見攻防清單 B）。
- 上游有特別要求時（opencode Go 是一個例子，`config.example.ini` 裡有整段範例）：
  - `headers`：每個請求固定附加的標頭，寫成一行 JSON，例如 `{"User-Agent": "my-relay/1.0"}`。驗證用的標頭（`Authorization`、`x-api-key` 等）由站方自己設定，不能寫在這裡。
  - `anthropic_auth = bearer`：上游的 Anthropic 入口要用 `Authorization: Bearer` 帶 key（預設是 Anthropic 官方的 `x-api-key`）。
  - `session_header`：上游要「對話 ID」時填標頭名稱（例如 `x-opencode-session`）。使用者的工具有帶同名標頭就沿用，但會先混入使用者 ID 再雜湊，不同使用者不會撞在一起，原始值也不會送到上游；沒帶就每把 key 固定一個。Claude Code、Codex 各自的原生對話標頭**不會**轉送。
- 設定檔打錯字（名稱、區段、數值格式）會在啟動時直接報錯，並指出是哪個區段的哪個名稱。改完設定要重啟伺服器才會生效。

確認設定檔讀得進來、模型和價格正確：

```powershell
python -m app.cli list-models
```

**3. 啟動**（只能用單一 worker，原因見攻防清單 C）

```powershell
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

**4. 註冊並拿 key**：瀏覽器打開 http://127.0.0.1:8000/
1. 「註冊」分頁：填 email、密碼、邀請碼
2. 登入後到「API Key」頁建立一把 `sk-` 開頭的 key（只會顯示這一次）
3. 網頁上還有：儲值（含收款 QR code）、模型與價格、用量紀錄、帳本

開發者用的 API 測試頁在 http://127.0.0.1:8000/docs 。

**5. 先給自己測試額度**（不用真的儲值）

```powershell
python -m app.cli credit --email 你的email --usd 5 --note 測試
```

**6. 在工具裡設定**：API 位址填 `http://127.0.0.1:8000/v1`，key 填 `sk-...`。
（有些工具會自己補 `/v1`，那就只填 `http://127.0.0.1:8000`。）

## 其他 API：Claude Code、Codex CLI、embeddings、圖片、語音

| 使用者打的 API | 誰會用 | 模型的 `kind` | 上游要求 | 計費 |
|---|---|---|---|---|
| `/v1/chat/completions` | Cherry Studio 等大部分工具 | `chat`（預設） | OpenAI 相容 | 輸入／快取／輸出 token |
| `/v1/responses` | Codex CLI | `chat` | `[upstream]` 設 `responses = true` | 同上 |
| `/v1/messages` | Claude Code（Anthropic 格式） | `chat` | `[upstream]` 填 `anthropic_base_url` | 同上（讀快取的 token 用快取價，寫快取照一般輸入價） |
| `/v1/embeddings` | 知識庫、RAG | `embedding` | OpenAI 相容 | 只收輸入 token |
| `/v1/images/generations` | 產生圖片 | `image` | OpenAI 相容 | `image_price` × 張數（一次只能 1 張） |
| `/v1/audio/speech` | 文字轉語音 | `speech` | OpenAI 相容 | `input_price` × 字元數 / 100 萬 |

站方不做格式轉換：上游原生支援哪種格式，才開放那種 API。語音轉文字（`/v1/audio/transcriptions`）**還沒做**。

**Claude Code**（PowerShell，網址換成你的站）：

```powershell
$env:ANTHROPIC_BASE_URL = "https://relay.example.com"; $env:ANTHROPIC_API_KEY = "sk-你的key"; $env:ANTHROPIC_MODEL = "deepseek-chat"; $env:ANTHROPIC_SMALL_FAST_MODEL = "deepseek-chat"; claude
```

- Claude Code 會自己在後面接 `/v1/messages`，所以網址**不要**加 `/v1`。
- 背景的小任務也會用一個模型，`ANTHROPIC_SMALL_FAST_MODEL` 一樣要設成站方有開放的模型，不然那些請求會收到「模型不存在」。
- `cache_control`、`context_management`、`service_tier: "auto"` 會被站方拿掉（前者避免上游收「寫入快取」的較高費用，後者要 beta 標頭才能用）；`anthropic-beta` 標頭不轉送；上游代執行的工具（web search、code execution）、PDF 文件、`speed: "fast"`（兩倍價）會被拒絕。
- 站方沒有 `/v1/messages/count_tokens`，Claude Code 打到時會收到 404（不收費）。

**Codex CLI**：在 `~/.codex/config.toml` 加一個 provider（格式以 Codex 官方文件為準）：

```toml
model = "gpt-test"
model_provider = "relay"

[model_providers.relay]
name = "AI 模型轉發站"
base_url = "https://relay.example.com/v1"
env_key = "RELAY_API_KEY"
wire_api = "responses"
```

key 放在環境變數 `RELAY_API_KEY`。Codex 的 web search 這類由上游代執行的工具會被拒絕；`store` 一律被改成 false，`previous_response_id` 不支援（Codex 預設就不用它）；`prompt_cache_retention` 會被拿掉。新版 Codex 可能改用 `shell`、`apply_patch` 這類內建工具（還沒有官方文件寫清楚格式，所以還沒開放）：遇到「不支援的工具類型」錯誤時，錯誤訊息會寫出是哪一種。

## 改用 PostgreSQL

預設用 SQLite（`dev.db`）就能跑。要改用你電腦上的 PostgreSQL 18，先用 postgres 管理員帳號建好資料庫：

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -c "CREATE USER relay WITH PASSWORD '換成你的密碼';" -c "CREATE DATABASE relay OWNER relay;"
```

再把 `config.ini` 的 `[site]` 裡的 `database_url` 改成 `postgresql+asyncpg://relay:你的密碼@localhost:5432/relay`。資料表會在啟動時自動建立。

**注意：這個專案沒有資料庫遷移（migration）工具。** 啟動時只會「建立不存在的資料表」，不會幫既有的表補新欄位。之後更新程式時如果資料表結構有變，舊資料庫啟動看起來正常，請求卻會失敗。練習階段的做法是刪掉資料庫重建（SQLite 就是刪掉 `dev.db`），**裡面的帳號和餘額會一起清空**。

## 儲值設定（方案 B）：先在 BSC 測試網練習

測試網上的幣是免費領的假錢，流程跟正式網一模一樣。`config.example.ini` 已經預先填好測試網的 RPC 和兩個測試代幣的合約，你只要補收款地址。

> 掃鏈程式已經對真的 BSC 測試網跑過：能讀到真實的轉帳紀錄，金額、合約、小數位數都正確。「付款 → 對上訂單 → 加餘額」這一整段，還要你用錢包實際轉一次才算驗證完。

1. **錢包加入 BSC 測試網**：在 MetaMask 新增網路，Chain ID `97`，RPC 用 `config.ini` 裡的 `rpc_url`，代幣符號 `tBNB`。
2. **準備兩個帳號**：在 MetaMask 再開一個帳號。
   - 收款帳號：把它的地址填進 `config.ini` 的 `[chain]` → `receive_address`。只要地址，**不要**填私鑰或助記詞。
   - 付款帳號：用來模擬朋友付款。
3. **領測試幣**：付款帳號到 [BNB Chain 官方水龍頭](https://docs.bnbchain.org/docs/bsc-faucet)（免費發測試幣的網站）領 tBNB（付手續費用）和測試 USDT／USDC。水龍頭可能有領取條件，以頁面為準。
4. **核對代幣合約**：在 MetaMask 點領到的代幣 → 代幣詳情，看它的合約地址，要跟 `config.ini` 的 `[token:USDT]`／`[token:USDC]` 的 `contract` 一樣。測試網有好幾種「USDT」，不一樣就把設定檔改成你實際拿到的那個，不然掃不到。
5. **重啟伺服器**：啟動時會自動開始掃鏈。掃鏈程式第一次成功連上 RPC 之前，建立訂單會回 503「儲值系統正在初始化」，這是刻意的。如果伺服器的紀錄一直出現「掃鏈失敗 … limit exceeded」，代表這個 RPC 不支援查轉帳紀錄，要換一個（可用的清單寫在 `config.example.ini` 的 `rpc_url` 註解裡）。
6. **試一次儲值**：在網頁的「儲值」頁建立訂單 → 從付款帳號轉「完全相同」的金額（含小數點後 4 位）到顯示的收款地址（可以用錢包掃 QR code，金額要自己輸入） → 等 15 個區塊確認（不到半分鐘）加上掃描間隔，就會到帳，網頁會自動更新狀態和餘額。

換成**正式網**：把 `[chain]` 的 `name` 和兩個 `[token:...]` 的 `contract` 換成正式網的值（寫在 `config.example.ini` 的註解裡），使用前務必到 bscscan.com 核對合約地址。`rpc_url` 要另外找：正式網的官方公開 RPC 都不支援查轉帳紀錄（實測過），要到第三方服務（例如 NodeReal、Ankr、Chainstack）申請一個自己的 RPC 網址。那個網址通常含有你的 API key，跟上游 key 一樣不能外流。

金額對不上的轉帳（例如忘了打尾數）會留給人工處理：

```powershell
python -m app.cli list-unmatched
```
```powershell
python -m app.cli assign-transfer --id 轉帳編號 --email 使用者email
```

## 站長後台與管理指令

伺服器跑起來之後，在**同一台電腦**開 http://localhost:8000/admin （不用帳密，別台電腦開會是 404）：

- 總覽：使用者餘額合計、近 30 天收入、收款錢包目前的 BNB／USDT／USDC 餘額
- 收益試算：今天（台灣時間 00:00 起）、過去 7 天、過去 30 天，依上游與模型列出「向使用者收多少、上游花多少、毛利」。
  上游有回報實際花費（opencode、OpenRouter）就用回報值；沒有就用模型的進價 × token 數估算；進價沒填或上游沒回用量的請求列為「不知道」，不算進毛利。
  opencode Go 額度內的請求另外列出用掉多少方案額度。這是新增的資料表欄位，**舊的資料庫要刪掉重建**（專案沒有 migration 工具，見攻防清單 P）
- 模型與價格：開關模型、改售價和快取價、記錄進價並一鍵「售價 = 進價 × 倍數」、從上游載入模型清單（OpenRouter 會附進價）。
  存檔會直接改寫 `config.ini` 的那幾行（註解不動，舊版存成 `config.ini.bak`），不用重啟
- 使用者：產生重設密碼連結（朋友忘記密碼時私訊給他）、手動調整餘額

其他功能用指令：

```powershell
python -m app.cli --help
```

## 測試

```powershell
python -m pytest
```

預設用暫存的 SQLite。要在 PostgreSQL 上跑，設定 `TEST_DATABASE_URL`，並指向一個**專門給測試用、可以清空**的資料庫（測試會刪光裡面的資料）：

```powershell
$env:TEST_DATABASE_URL = "postgresql+asyncpg://relay:密碼@localhost:5432/relay_test"; python -m pytest
```

## 時間

API 回傳的時間都是 UTC（結尾的 `Z`）。台灣時間 = UTC + 8 小時。
