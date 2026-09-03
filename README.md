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
| `DISK` / `CORES` / `RAM` | `8` / `1` / `1024` | 磁碟 GiB、核心數、記憶體 MB |
| `BRIDGE` / `IPV4` / `GATEWAY` | `vmbr0` / `dhcp` / — | 網路。靜態 IP 用 `IPV4=10.0.0.50/24` |
| `STORAGE` | 自動偵測 | 容器磁碟要放的儲存 |
| `PORT` | `8080` | 服務 port |

### Debian / Ubuntu / 既有的 LXC

```bash
git clone https://github.com/lchanc3/pdfsign.git && cd pdfsign
sudo bash install.sh
```

會安裝字型、建立 venv、設定 systemd 服務並啟動。

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

- **常用戳章** — 側欄上方的快捷鍵決定「下一個蓋什麼」：簽名（姓名＋日期）、僅簽名、無關
- **日期位置** — 「下方」是日期換行排在姓名底下；「同行」是緊接在姓名後、同一條基線、字級較小
- **對齊** — 左／中／右。在表格格子中央點一下並選「中」，就會置中在格子裡
- **格式沿用** — 你調過的設定（姓名、日期、字級、對齊、排版）會成為下一個戳章的預設
- 戳章可以直接拖曳移動，選取後按 `Delete` 刪除

日期預設帶入當天的民國年，可自由改寫。

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
| `PDFSIGN_PORT` | `8080` | 監聽的 port |
| `PDFSIGN_FONT` | 自動尋找 | 指定字型檔路徑 |

---

## 注意事項

**沒有內建認證。** 適合內網自用。要對外或多人共用，請自行加反向代理與認證。

**上傳的檔案暫存在系統 temp 目錄**（`/tmp/pdfsign`），按「換一份」會刪除，
其餘會在重開機時隨 `/tmp` 清空。長期運行建議加個定期清理。

**這不是數位簽章。** 蓋上去的是文字，跟手寫簽名或蓋章一樣沒有防竄改能力。
需要密碼學保證的話請用 X.509 憑證做 PDF digital signature。

---

## 授權

本專案採 **AGPL-3.0**，因為相依的 [PyMuPDF](https://github.com/pymupdf/PyMuPDF)
是 AGPL 授權，衍生作品必須同樣以 AGPL 釋出。

實務上的意思：自用、內部部署都沒問題；若要把它包成服務提供給他人使用，
必須一併公開你的修改版原始碼。若需要專有授權，得另外向 Artifex 取得 PyMuPDF 的商業授權。

字型 TW-Kai 由中華民國教育部以公開授權釋出，不受本專案授權條款拘束。
