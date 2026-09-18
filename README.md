# 台北 → 日本 機票追蹤

GitHub Actions 每 6 小時抓一次 Travelpayouts 價格，存進 `data/`，低於目標價時寄 Email；`index.html` 用 GitHub Pages 顯示走勢圖。

## 設定

1. **Travelpayouts token**：到 https://www.travelpayouts.com/ 註冊 → Profile → API token
2. **Gmail 應用程式密碼**：Google 帳戶需開啟兩步驟驗證 → https://myaccount.google.com/apppasswords 建立一組
3. 在 repo 的 Settings → Secrets and variables → Actions 新增：

| Secret | 內容 |
|---|---|
| `TP_TOKEN` | Travelpayouts token |
| `GMAIL_USER` | 寄件的 Gmail 地址 |
| `GMAIL_APP_PASSWORD` | 16 碼應用程式密碼 |
| `NOTIFY_TO` | （選填）收件地址，預設同 `GMAIL_USER` |

4. Actions → Track fares → Run workflow 手動跑第一次
5. Settings → Pages → Source 選 `main` branch / root，開啟網頁

## 調整追蹤條件

編輯 `config.json`：

- `destinations`：城市代碼（TYO 東京、OSA 大阪、NGO 名古屋、FUK 福岡、SPK 札幌、OKA 沖繩）與目標價
- `months_ahead`：追蹤未來幾個月（從下個月起算）；或在 `months` 直接指定，如 `["2027-01", "2027-02"]`
- `trip_days_min` / `trip_days_max`：來回行程天數範圍
- `one_way`：`true` 改追單程
- `direct_only`：`true` 只看直飛
- `preferred_airlines`：偏好航空（網頁會特別標註，也一定會保留），如 `{"code": "BR", "name": "長榮"}`
- `excluded_airlines`：不要的航空，抓價時直接排除（轉機行程只要有一段是它就排除）。`code` 為兩碼航空代碼、`name` 為 Google 顯示的中文名稱片段，兩者都填比對最完整

## 本機預覽

```bash
python -m http.server 8000
```

再打開 http://localhost:8000
