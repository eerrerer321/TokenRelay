# AI 模型轉發站

類似 OpenRouter 的私人練習專案：站方發 API key 給使用者，使用者在 Cherry Studio 這類工具裡
把它設成自訂的 OpenAI 相容 provider；站方依使用的模型轉發給上游，並從使用者的 USDT/USDC
儲值餘額扣款。

- 設計與資料流：[docs/設計.md](docs/設計.md)
- 已防護與尚未防護的攻擊手法：[docs/攻防清單.md](docs/攻防清單.md)

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
- OpenAI 的新模型要把 `max_tokens_field` 改成 `max_completion_tokens`。
- 為了避免額外計費，請求參數採白名單制（清單在 [docs/設計.md](docs/設計.md) 5.4）。朋友的工具如果送了白名單外的參數（例如 DeepSeek 的 `thinking`），會收到 400 並看到是哪個參數。確認那個參數不影響計費後，再加進 `app/relay.py` 的白名單。
- 如果上游是 OpenRouter，它可能因為 `tools`、`response_format` 改選較貴的供應商；售價要抓最貴的那家（見攻防清單 B）。
- 設定檔打錯字（名稱、區段、數值格式）會在啟動時直接報錯，並指出是哪個區段的哪個名稱。改完設定要重啟伺服器才會生效。

確認設定檔讀得進來、模型和價格正確：

```powershell
python -m app.cli list-models
```

**3. 啟動**（只能用單一 worker，原因見攻防清單 C）

```powershell
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

**4. 註冊並拿 key**：瀏覽器打開 http://127.0.0.1:8000/docs
1. `POST /api/register` 註冊（要填邀請碼）
2. `POST /api/login` 拿到 `sess-` 開頭的 token
3. 按右上角 **Authorize**，貼上 token
4. `POST /api/keys` 拿到 `sk-` 開頭的 API key（只會顯示這一次）

**5. 先給自己測試額度**（不用真的儲值）

```powershell
python -m app.cli credit --email 你的email --usd 5 --note 測試
```

**6. 在工具裡設定**：API 位址填 `http://127.0.0.1:8000/v1`，key 填 `sk-...`。
（有些工具會自己補 `/v1`，那就只填 `http://127.0.0.1:8000`。）

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
6. **試一次儲值**：在 `/docs` 用 `POST /api/deposits` 建立訂單 → 從付款帳號轉「完全相同」的 `pay_amount`（含小數點後 4 位）到 `to_address` → 等 15 個區塊確認（不到半分鐘）加上掃描間隔，就會到帳。用 `GET /api/me` 看餘額。

換成**正式網**：把 `[chain]` 的 `name` 和兩個 `[token:...]` 的 `contract` 換成正式網的值（寫在 `config.example.ini` 的註解裡），使用前務必到 bscscan.com 核對合約地址。`rpc_url` 要另外找：正式網的官方公開 RPC 都不支援查轉帳紀錄（實測過），要到第三方服務（例如 NodeReal、Ankr、Chainstack）申請一個自己的 RPC 網址。那個網址通常含有你的 API key，跟上游 key 一樣不能外流。

金額對不上的轉帳（例如忘了打尾數）會留給人工處理：

```powershell
python -m app.cli list-unmatched
```
```powershell
python -m app.cli assign-transfer --id 轉帳編號 --email 使用者email
```

## 管理指令

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
