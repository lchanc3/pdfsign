# 簽章工具 / pdfsign

在 PDF 上點一下就蓋章，輸出可選取、可搜尋的向量文字。

> A click-to-place PDF stamping tool that writes real vector text (not
> rasterised images) using a Traditional Chinese Kai font, with ROC-calendar
> dates. Single-file Python app, no build step. UI is in Traditional Chinese.

一個 Python 檔就是全部：前端 HTML/CSS/JS 都內嵌在 `pdfsign.py` 裡，沒有建置步驟。

---

## 為什麼

多數 PDF 工具的「簽名」功能是把文字畫在 canvas 上再轉成圖片貼進去，
結果放大會糊、無法選取、無法搜尋，檔案也變大。
這個工具直接把文字物件寫進 PDF，字型只嵌入實際用到的字。

---

## 安裝

### Proxmox VE 一鍵安裝

在 **PVE 節點的 shell** 執行（不是在容器裡），會自動建立 LXC 容器並裝好一切：

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/lchanc3/pdfsign/main/pve-install.sh)"
```

裝完會印出網址、容器編號和 root 密碼。

可用環境變數調整：

```bash
CTID=201 RAM=2048 IPV4=10.0.0.50/24 GATEWAY=10.0.0.1 \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/lchanc3/pdfsign/main/pve-install.sh)"
```

| 變數 | 預設 | 說明 |
|---|---|---|
| `CTID` | 自動取下一個 | 容器編號 |
| `CT_HOSTNAME` | `pdfsign` | 主機名稱 |
| `DISK` / `CORES` / `RAM` | `8` / `1` / `512` | 磁碟 GiB、核心數、記憶體 MB |
| `BRIDGE` / `IPV4` / `GATEWAY` | `vmbr0` / `dhcp` / — | 網路。靜態 IP 用 `IPV4=10.0.0.50/24` |
| `STORAGE` | 自動偵測 | 容器磁碟要放的儲存 |
| `PORT` | `80` | 服務 port |
| `PDFSIGN_PLUGINS` | 無 | 要一起裝的外掛，逗號分隔（見下方「外掛」） |

### Debian / Ubuntu / 既有的 LXC

```bash
git clone https://github.com/lchanc3/pdfsign.git && cd pdfsign
sudo bash install.sh
```

會安裝字型、建立 venv、設定 systemd 服務並啟動。
要一併裝外掛：`sudo PDFSIGN_PLUGINS=formfill bash install.sh`

### Docker

```bash
docker compose -f docker/docker-compose.yml up -d --build  
```

### 手動

```bash
sudo apt install fontconfig fonts-cns11643-kai && fc-cache -f
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python pdfsign.py
```

---

## 使用

拖一份 PDF 進去，所有頁面會列出來。在要簽的位置點一下就放上一個戳章。

- **常用戳章** — 側欄上方的快捷鍵決定「下一個蓋什麼」：簽名（姓名＋日期）、文字（自由撰寫、可換行、沒有日期）
- **日期位置** — 「下方」是日期換行排在姓名底下；「同行」是緊接在姓名後、同一條基線、字級較小
- **對齊** — 左／中／右。在表格格子中央點一下並選「中」，就會置中在格子裡
- **格式沿用** — 你調過的設定（姓名、日期、字級、對齊、排版）會成為下一個戳章的預設
- 戳章可以直接拖曳移動，選取後按 `Delete` 刪除

日期預設帶入當天的民國年，可自由改寫。

---

## 外掛

`plugins/` 底下的每個 `.py` 都是一個選用功能。**預設不裝**，核心完全不知道它存在；
裝了之後會自己掛上 API 並把介面注入首頁。

### formfill — 填表模式

在表格既有的那一列上定位並填入當下時間：

```
異常排除時間：    年    月    日    時    分
異常排除時間： 115 年 09 月 21 日 16 時 05 分
```

打開側欄的「填表模式」之後，會掃描整份文件、把符合的列框起來，
這時候點頁面不會蓋章；點框就填入現在時間，再點一次可以取消，
或按「全部填入」一次處理所有頁。填進去的數字就是一般的戳章——
可以拖、可以改、可以按 `Delete` 刪掉，字級沿用表格原本的大小，
空格太窄會自動縮小以免壓到隔壁的字。

原本就填過的列會用虛線標示並跳過（看的是空格裡有沒有數字）。
掃描檔（沒有文字層）無法偵測。時間取瀏覽器的本機時間，不受容器時區影響。

### 規則

`formfill.py` 只是引擎，**不內建任何規則**；要偵測什麼全部寫在
`plugins/formfill.rules.jsonc`。規則檔不見或寫錯會直接顯示在側欄，
不會偷偷退回別的規則。改完存檔即生效，不用重開服務。

檔案是 **JSONC**：可以寫 `//` 和 `/* */` 註解，結尾多一個逗號也沒關係，
所以規則旁邊可以直接註明這條是給哪份表單用的。錯誤訊息的行號指的是原始檔，
不會因為註解被拿掉而偏掉。

```jsonc
{
  "rules": [
    {
      "id": "abnormal-clear",
      "label": "異常排除時間",     // 錨定用的文字
      "slots": [
        { "before": "年", "value": "roc_year" },
        { "before": "月", "value": "month" },
      ],
    },
  ]
}
```

`label` 是錨定用的文字（給陣列可以當別名）。`slots` 由左到右列出每個空格，
每個空格指定**填在哪裡**和**填什麼**：

| 鍵 | 說明 |
|---|---|
| `before` | 填在這個字左邊的空白，預設右對齊 → `115 年` |
| `after` | 填在這個字右邊的空白，預設左對齊 → `：王小明` |
| 兩個都不給 | 接在標籤後面，會自動跳過緊接的冒號 |
| `align` | `left` / `center` / `right`，覆寫預設對齊 |
| `value` | `roc_year` `year` `month` `day` `hour` `minute` `second` `roc_date` `roc_short` `text` |
| `text` | `value` 是 `text` 時要填的固定字串 |
| `digits` | 數字補零到幾位（月日時分預設 2，年預設 1） |
| `optional` | `true` 代表找不到這個錨點也算命中 |

往右填的時候，後面緊接的底線或點線（`＿﹍─…`）算是畫出來的填寫欄，
字會寫在那條線上面而不是擠在它前面。

同一列必須找到全部的必要錨點才算命中，少一個就整條跳過——
這樣「異常發生時間」之類結構相同的鄰列才不會被誤填。
規則檔裡註解掉了幾種常見寫法，要用把註解拿掉就好。

舊的 `formfill.rules.json` 一樣讀得到（也吃註解），兩個都在時只會讀 `.jsonc`
並在側欄提醒。安裝腳本不會覆蓋已經存在的規則檔，也不會在旁邊多放一份新的把它蓋過去。

### 安裝與移除

```bash
# 安裝時一起裝
sudo PDFSIGN_PLUGINS=formfill bash install.sh

# 之後再裝：重跑一次就好，已經改過的規則檔不會被覆蓋
cd pdfsign && git pull && sudo PDFSIGN_PLUGINS=formfill bash install.sh

# 不想要了
sudo rm /opt/pdfsign/plugins/formfill.py && sudo systemctl restart pdfsign
```

PVE 一鍵安裝也吃同一個變數：

```bash
PDFSIGN_PLUGINS=formfill bash -c "$(curl -fsSL https://raw.githubusercontent.com/lchanc3/pdfsign/main/pve-install.sh)"
```

---

## 字型

預設依序尋找：

```
/usr/share/fonts/truetype/cns11643/TW-Kai-98_1.ttf
/usr/share/fonts/truetype/cns11643/TW-Kai-Plus-98_1.ttf
/usr/share/fonts/truetype/custom/kaiu.ttf
```

要指定其他字型：

```bash
PDFSIGN_FONT=/path/to/your.ttf python3 pdfsign.py
```

安裝腳本裝的是 **全字庫正楷體（TW-Kai）**，教育部釋出、可自由使用。

---

## 服務管理

```bash
systemctl status pdfsign
systemctl restart pdfsign        # 換了新版 pdfsign.py 之後
journalctl -u pdfsign -n 50
```

更新程式：覆蓋 `/opt/pdfsign/pdfsign.py` 再 restart。

---

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `PDFSIGN_PORT` | `80` | 監聽的 port |
| `PDFSIGN_FONT` | 自動尋找 | 指定字型檔路徑 |
| `PDFSIGN_WORK_DIR` | `/var/lib/pdfsign` | 上傳檔的暫存目錄 |
| `PDFSIGN_TTL_DAYS` | `7` | 暫存檔保留幾天 |
| `PDFSIGN_PLUGIN_DIR` | 程式旁的 `plugins/` | 外掛目錄 |

---

## 注意事項

**沒有內建認證。** 適合內網自用。要對外或多人共用，請自行加反向代理與認證。

**上傳的檔案暫存在 `/var/lib/pdfsign`**，按「刪除」再按一次「確認刪除」會立刻刪掉，
其餘超過 7 天會在下次上傳時自動清掉（`PDFSIGN_TTL_DAYS` 可調）。
不放 `/tmp` 是因為 `systemd-tmpfiles` 會連目錄一起清掉，服務跑久了上傳就會失敗。

**這不是數位簽章。** 蓋上去的是文字，跟手寫簽名或蓋章一樣沒有防竄改能力。
需要密碼學保證的話請用 X.509 憑證做 PDF digital signature。

---

## 授權

本專案採 **AGPL-3.0**，因為相依的 [PyMuPDF](https://github.com/pymupdf/PyMuPDF)
是 AGPL 授權，衍生作品必須同樣以 AGPL 釋出。

實務上的意思：自用、內部部署都沒問題；若要把它包成服務提供給他人使用，
必須一併公開你的修改版原始碼。若需要專有授權，得另外向 Artifex 取得 PyMuPDF 的商業授權。

字型 TW-Kai 由中華民國教育部以公開授權釋出，不受本專案授權條款拘束。
