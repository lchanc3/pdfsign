#!/usr/bin/env python3
"""
簽章工具 — 點選 PDF 位置，蓋上姓名與民國日期。

輸出為真正的向量文字（字型嵌入），非圖片。
字型使用全字庫正楷體 TW-Kai。

執行：
    python3 app.py
    然後瀏覽 http://<ip>
"""

import importlib.util
import io
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import date
from functools import lru_cache
from pathlib import Path

import pymupdf
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

# ---------------------------------------------------------------- 設定

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/cns11643/TW-Kai-98_1.ttf",
    "/usr/share/fonts/truetype/cns11643/TW-Kai-Plus-98_1.ttf",
    "/usr/share/fonts/truetype/custom/kaiu.ttf",
]

PORT = int(os.environ.get("PDFSIGN_PORT", "80"))
RENDER_ZOOM = 2.0

# 自由文字換行時的行距倍率。前端 LINE_FACTOR 要跟著一樣，
# 不然畫面上看到的行距會跟輸出的 PDF 對不起來。
LINE_FACTOR = 1.3

# 上傳的 PDF 暫存在這裡。不放 /tmp：systemd-tmpfiles 會連目錄一起清掉，
# 服務跑久了就會在寫檔的時候炸出 500。
# 在 Windows 上 "/var/lib/pdfsign" 會被解析成 C:\var\lib\pdfsign 而且真的建得起來，
# 所以這個候選只在 POSIX 上放進去，開發機才會乖乖退回 temp
WORK_DIR_CANDIDATES = (["/var/lib/pdfsign"] if os.name == "posix" else []) + [
    str(Path(tempfile.gettempdir()) / "pdfsign")
]
WORK_TTL_DAYS = float(os.environ.get("PDFSIGN_TTL_DAYS", "7"))


def find_work_dir() -> Path:
    # systemd 的 StateDirectory= 會把建好的路徑放進 STATE_DIRECTORY
    override = os.environ.get("PDFSIGN_WORK_DIR") or os.environ.get("STATE_DIRECTORY")
    for candidate in [override] if override else WORK_DIR_CANDIDATES:
        path = Path(candidate)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".probe"
            probe.write_bytes(b"")
            probe.unlink()
            return path
        except OSError as e:
            if override:
                raise RuntimeError(f"暫存目錄無法寫入：{path}（{e.strerror or e}）") from e
    raise RuntimeError("找不到可寫的暫存目錄，請用 PDFSIGN_WORK_DIR 指定一個")


WORK_DIR = find_work_dir()


def find_font() -> str:
    override = os.environ.get("PDFSIGN_FONT")
    if override:
        if not Path(override).is_file():
            raise RuntimeError(f"PDFSIGN_FONT 指到的檔案不存在：{override}")
        return override
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return path
    raise RuntimeError(
        "找不到楷體字型。請執行 apt install fonts-cns11643-kai，"
        "或把 .ttf 放到 /usr/share/fonts/truetype/custom/"
    )


FONT_PATH = find_font()
FONT = pymupdf.Font(fontfile=FONT_PATH)

# 字型檔換掉、或子集化的方式改了，瀏覽器不該繼續吃快取裡的舊字型（max-age 一天）。
# 尾巴那個數字是子集化邏輯的版本，改到 webfont() 就把它加一。
_font_stat = Path(FONT_PATH).stat()
FONT_TAG = f"{int(_font_stat.st_mtime)}-{_font_stat.st_size}-2"

# 楷體全字庫超過 50MB，直接當 webfont 送給瀏覽器會拖垮渲染，
# 所以只把畫面上實際用到的字子集化後送出（通常幾十 KB）。
try:
    from fontTools.subset import Options as SubsetOptions, Subsetter
    from fontTools.ttLib import TTFont
    HAVE_FONTTOOLS = True
except ImportError:  # 沒裝也能跑，只是會退回送整支字型
    HAVE_FONTTOOLS = False

# 介面本身用得到的字，先包進預設子集
SEED_CHARS = (
    "簽章工具名日期無關僅對齊左緣置中右下方同行大小刪除這個下載好的換一份頁"
    "民國年月未填在上點一次就會放個開啟可選取搜尋文字不是圖片把檔案拖到裡"
    "內容"
    "或選擇還沒有蓋任何請確認後端狀態與網路連線重試產生失敗已"
    "0123456789/.:-"
)


def subset_font(path: str, chars: str, index: int = 0) -> bytes:
    """只留 chars 用得到的字，字符重新編號。需要 fontTools。

    網頁字型和嵌進 PDF 的字型（戳章、外掛的替代字）共用這一支。嵌進 PDF 時一定要
    先縮：整支字型嵌進去的話，MuPDF 會替整套字寫一份 ToUnicode，事後的
    subset_fonts 只縮字型本體、不縮這張表，蓋幾個字檔案就多將近 100 KB。
    """
    opts = SubsetOptions()
    opts.layout_features = []
    opts.notdef_outline = True
    opts.recalc_bounds = False
    opts.ignore_missing_unicodes = True
    opts.drop_tables += ["meta"]     # 本來就會被丟掉，先講好免得每次都印警告
    # recalcBBoxes=False 很重要：fontTools 存檔時會重算每個字的 bbox，但不會跟著
    # 更新 hmtx 的 lsb。楷體這類「bbox 記成整個 em 方框」的字型一重算就對不起來，
    # 而 FreeType / Skia 會把字形平移 (lsb - xMin) 來補，結果畫面上每個中文字都往
    # 右偏 0.4em，跟 PDF 實際輸出的位置對不上。原樣搬過去就不會有這個落差。
    font = TTFont(path, recalcBBoxes=False,
                  fontNumber=index if path.lower().endswith(".ttc") else -1)
    sub = Subsetter(options=opts)
    sub.populate(text=chars)
    sub.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


@lru_cache(maxsize=32)
def webfont(chars: str) -> bytes:
    if not HAVE_FONTTOOLS:
        return Path(FONT_PATH).read_bytes()
    return subset_font(FONT_PATH, chars or SEED_CHARS)

app = FastAPI(title="簽章工具")


# ---------------------------------------------------------------- 資料模型


class Placement(BaseModel):
    page: int
    x: float  # 頁面寬度的比例 0–1，對齊基準點
    y: float  # 頁面高度的比例 0–1，姓名基線
    name: str
    dateText: str = ""
    nameSize: float = 22.0
    dateSize: float = 12.0
    gap: float = 6.0  # 上下排時，姓名基線到日期基線的額外間距（點）
    align: str = "center"  # left | center | right
    layout: str = "stack"  # stack（日期在下）| inline（日期接在同一行）


class SignRequest(BaseModel):
    doc: str
    placements: list[Placement]
    extra: dict = {}   # 外掛附帶的資料（前端 pdfsign:collect 收集），核心不看內容


_last_sweep = 0.0


def sweep(force: bool = False) -> None:
    """清掉過期的暫存檔。離開 /tmp 之後沒有系統幫忙清，得自己來。"""
    global _last_sweep
    now = time.time()
    if not force and now - _last_sweep < 3600:
        return
    _last_sweep = now
    cutoff = now - WORK_TTL_DAYS * 86400
    for old in WORK_DIR.glob("*.pdf"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:  # 別人正在用或已經不在了，下次再說
            pass


sweep(force=True)


def doc_path(doc_id: str) -> Path:
    safe = uuid.UUID(doc_id)  # 非合法 UUID 會直接丟例外
    path = WORK_DIR / f"{safe}.pdf"
    if not path.is_file():
        raise HTTPException(404, "找不到這份文件，請重新上傳")
    return path


# ---------------------------------------------------------------- API


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception:
        raise HTTPException(400, "這不是可讀取的 PDF")

    if doc.page_count == 0:
        raise HTTPException(400, "這份 PDF 沒有任何頁面")

    sweep()

    doc_id = str(uuid.uuid4())
    try:
        # 目錄仍可能被人為刪掉或掛載點跑掉，寫之前先確保它在
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        (WORK_DIR / f"{doc_id}.pdf").write_bytes(raw)
    except OSError as e:
        raise HTTPException(500, f"暫存檔寫入失敗：{e.strerror or e}")

    pages = [
        {"width": round(p.rect.width, 2), "height": round(p.rect.height, 2)}
        for p in doc
    ]
    doc.close()

    return {"doc": doc_id, "name": file.filename, "pages": pages}


@app.get("/api/page/{doc_id}/{index}")
def page_image(doc_id: str, index: int):
    doc = pymupdf.open(doc_path(doc_id))
    if not 0 <= index < doc.page_count:
        doc.close()
        raise HTTPException(404, "頁碼超出範圍")

    pix = doc[index].get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM))
    png = pix.tobytes("png")
    doc.close()
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/sign")
def sign(req: SignRequest):
    if not req.placements and not req.extra:
        raise HTTPException(400, "還沒有放上任何簽章")

    doc = pymupdf.open(doc_path(req.doc))

    # 外掛先動原文（例如修正原文），戳章最後才蓋，才會疊在最上面
    try:
        for hook in BEFORE_SIGN:
            hook(doc, req.extra)
    except Exception:
        doc.close()
        raise

    # 每次簽署用不重複的字型別名。若沿用固定名稱，對「已經簽過一次」的檔案
    # 再簽時，PyMuPDF 會重用頁面裡既有的（已子集化的）字型資源而忽略 fontfile，
    # 導致新字沒有對應字符，輸出變成空白方框。
    alias = "kai" + uuid.uuid4().hex[:8]

    # 先把戳章用得到的字做成子集再嵌。整支楷體嵌進去的話，MuPDF 會替整套字
    # 寫一份 ToUnicode，事後的 subset_fonts 只縮字型本體、不縮這張表，
    # 蓋幾個字檔案就多將近 100 KB（TW-Kai 字更多，多更多）
    stamp_chars = "".join(sorted({ch for p in req.placements
                                  for ch in p.name + p.dateText if not ch.isspace()}))
    stamp_font = webfont(stamp_chars) if HAVE_FONTTOOLS and stamp_chars else None
    font_ready: set[int] = set()

    def width(text: str, size: float) -> float:
        return FONT.text_length(text, size) if text else 0.0

    def start_at(total: float, left: float, align: str) -> float:
        if align == "left":
            return left
        return left - total / 2 if align == "center" else left - total

    for p in req.placements:
        if not 0 <= p.page < doc.page_count:
            continue
        page = doc[p.page]
        w, h = page.rect.width, page.rect.height
        px, py = p.x * w, p.y * h
        dated = p.dateText.strip()
        # 自由文字可以換行。沒有換行時 lines 只有一行，行為跟以前一模一樣。
        # 點到的位置永遠是第一行的基線，後面的行往下長。
        lines = p.name.split("\n")
        step = p.nameSize * LINE_FACTOR
        base = py + step * (len(lines) - 1)   # 最後一行的基線，日期靠著它擺

        if stamp_font and p.page not in font_ready:
            page.insert_font(fontname=alias, fontbuffer=stamp_font)
            font_ready.add(p.page)

        def put(text: str, x: float, y: float, size: float) -> None:
            page.insert_text((x, y), text, fontname=alias,
                             fontfile=None if stamp_font else FONT_PATH,
                             fontsize=size, color=(0, 0, 0))

        def put_line(text: str, y: float) -> None:
            if text.strip():
                put(text, start_at(width(text, p.nameSize), px, p.align),
                    y, p.nameSize)

        for i, line in enumerate(lines[:-1]):
            put_line(line, py + step * i)

        last = lines[-1]
        if p.layout == "inline":
            # 姓名與日期同一條基線，日期緊接在後（如：謝誠銓115/09/02）
            wn = width(last, p.nameSize)
            x0 = start_at(wn + width(p.dateText, p.dateSize), px, p.align)
            if last.strip():
                put(last, x0, base, p.nameSize)
            if dated:
                put(p.dateText, x0 + wn, base, p.dateSize)
        else:
            put_line(last, base)
            if dated:
                put(p.dateText,
                    start_at(width(p.dateText, p.dateSize), px, p.align),
                    base + p.dateSize + p.gap, p.dateSize)

    # 只嵌入實際用到的字，否則整支楷體（約 50MB）會被塞進檔案
    try:
        doc.subset_fonts(verbose=False)
    except Exception:
        pass

    # use_objstms：Word、Acrobat 存的檔多半把小物件包在壓縮過的物件串流裡，
    # 不照做的話每個物件攤開成純文字，光重存一次就可能大上三成
    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True, use_objstms=1)
    doc.close()

    return Response(
        out.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="signed.pdf"'},
    )


@app.delete("/api/doc/{doc_id}")
def drop(doc_id: str):
    try:
        doc_path(doc_id).unlink()
    except HTTPException:
        pass
    return JSONResponse({"ok": True})


@app.get("/font.ttf")
def font(chars: str = ""):
    # 去重排序讓相同字集命中同一份快取；沒指定就給介面用的預設字集
    wanted = "".join(sorted(set(chars or SEED_CHARS)))[:4000]
    return Response(
        webfont(wanted),
        media_type="font/ttf",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/today")
def today():
    d = date.today()
    return {"roc": f"民國{d.year - 1911}年{d.month}月{d.day}日",
            "short": f"{d.year - 1911}.{d.month:02d}.{d.day:02d}"}


# ---------------------------------------------------------------- 外掛

# plugins/*.py 是選用功能：裝了才有，沒裝核心完全不知道它存在。
# 一個外掛可以提供：
#   setup(ctx)   啟動時呼叫一次，ctx 帶著 app、doc_path、字型等核心資源
#   router       APIRouter，會掛進 app
#   CLIENT_JS    注入首頁的前端程式碼，在核心 script 之後執行
#   SEED_CHARS   外掛介面用到的字，併進 webfont 的預設子集
#
# 輸出時介入：setup 裡 ctx.before_sign.append(fn)，fn(doc, extra) 會在蓋章之前
# 拿到開好的文件。extra 是前端在 pdfsign:collect 事件裡塞進 detail.extra 的資料。
# 要嵌字型進 PDF 的話先用 ctx.subset_font 縮過（沒裝 fontTools 時是 None）。
#
# 前端事件（都掛在 document 上）：
#   pdfsign:loaded   開了一份新文件
#   pdfsign:place    點頁面要蓋章，preventDefault() 可以攔掉
#   pdfsign:drawn    戳章重畫完
#   pdfsign:collect  要輸出了，把自己的資料放進 detail.extra
#
# 會接管頁面點擊的模式（填表、修正原文…）用 enterMode / leaveMode，
# 同時只會有一個開著，戳章停用與攔截點擊都由核心處理。

PLUGIN_DIR = Path(
    os.environ.get("PDFSIGN_PLUGIN_DIR", Path(__file__).resolve().parent / "plugins")
)

PLUGIN_SCRIPTS: list[str] = []
PLUGIN_NAMES: list[str] = []
BEFORE_SIGN: list = []


class PluginContext:
    """外掛要用的核心資源。外掛不要反過來 import 主程式。"""

    def __init__(self) -> None:
        self.app = app
        self.doc_path = doc_path
        self.font = FONT
        self.font_path = FONT_PATH
        self.work_dir = WORK_DIR
        self.before_sign = BEFORE_SIGN
        self.subset_font = subset_font if HAVE_FONTTOOLS else None


def load_plugins() -> None:
    global SEED_CHARS
    if not PLUGIN_DIR.is_dir():
        return
    ctx = PluginContext()
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"pdfsign_plugin_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "setup"):
                mod.setup(ctx)
            if getattr(mod, "router", None) is not None:
                app.include_router(mod.router)
            if getattr(mod, "CLIENT_JS", ""):
                PLUGIN_SCRIPTS.append(mod.CLIENT_JS)
            SEED_CHARS += getattr(mod, "SEED_CHARS", "")
            PLUGIN_NAMES.append(path.stem)
        except Exception as e:  # 外掛壞掉不該讓整個服務起不來
            print(f"外掛 {path.name} 載入失敗：{e}")


load_plugins()


# ---------------------------------------------------------------- 前端

INDEX = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>簽章工具</title>
<style>
:root {
  --kai: 'TWKai', serif;   /* TWKai 由 JS 分批註冊，未載入的字自動退回 serif */
  --desk: #E4E5E1;
  --desk-deep: #D2D4CE;
  --ink: #1F2328;
  --ink-soft: #5A6068;
  --ink-faint: #8B9199;
  --paper: #FFFFFF;
  --seal: #B03A2E;
  --seal-wash: rgba(176, 58, 46, 0.10);
  --line: #C6C9C3;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  height: 100%;
  background: var(--desk);
  color: var(--ink);
  font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", system-ui, sans-serif;
  font-size: 15px;
  line-height: 1.6;
}

button, input, select, textarea { font: inherit; color: inherit; }

button {
  cursor: pointer;
  border: 1px solid var(--line);
  background: var(--paper);
  padding: .45rem .9rem;
  border-radius: 3px;
  transition: background .12s, border-color .12s;
}
button:hover:not(:disabled) { background: #F5F6F4; border-color: var(--ink-faint); }
button:disabled { opacity: .4; cursor: not-allowed; }
button:focus-visible, input:focus-visible, [tabindex]:focus-visible {
  outline: 2px solid var(--seal);
  outline-offset: 2px;
}

.primary {
  background: var(--ink);
  color: #fff;
  border-color: var(--ink);
}
.primary:hover:not(:disabled) { background: #000; border-color: #000; }

/* 已經「上膛」的刪除鍵：紅的，再按一下就真的刪 */
.danger {
  background: var(--seal);
  color: #fff;
  border-color: var(--seal);
}
.danger:hover:not(:disabled) { background: #94301F; border-color: #94301F; }

/* ---------- 版面 ---------- */

.shell { display: flex; height: 100vh; }

.stage {
  flex: 1;
  overflow: auto;
  padding: 2rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1.25rem;
}

.panel {
  width: 340px;
  flex-shrink: 0;
  background: var(--paper);
  border-left: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
/* display 會蓋掉 [hidden] 的預設 display:none，得自己補回來 */
.panel[hidden] { display: none; }

/* ---------- 起始畫面 ---------- */

.intro {
  margin: auto;
  text-align: center;
  max-width: 34ch;
}
.intro h1 {
  font-family: var(--kai);
  font-size: 3.4rem;
  font-weight: 400;
  margin: 0 0 .4rem;
  letter-spacing: .18em;
  text-indent: .18em;
}
.intro p { color: var(--ink-soft); margin: 0 0 1.75rem; }

.drop {
  border: 1px dashed var(--ink-faint);
  border-radius: 4px;
  padding: 2.5rem 2rem;
  background: rgba(255,255,255,.55);
  transition: border-color .15s, background .15s;
}
.drop.over { border-color: var(--seal); background: var(--seal-wash); }

/* 拖檔進來時整個視窗都是放置區，這層只是視覺提示 */
body.dropping::after {
  content: '放開以開啟';
  position: fixed;
  inset: .75rem;
  z-index: 50;
  pointer-events: none;
  border: 2px dashed var(--seal);
  border-radius: 6px;
  background: var(--seal-wash);
  display: grid;
  place-items: center;
  font-family: var(--kai);
  font-size: 1.8rem;
  letter-spacing: .18em;
  text-indent: .18em;
  color: var(--seal);
}

/* ---------- 頁面 ---------- */

.sheet {
  position: relative;
  background: var(--paper);
  box-shadow: 0 1px 2px rgba(0,0,0,.10), 0 6px 22px rgba(0,0,0,.09);
  cursor: crosshair;
  align-self: center;
}
.sheet img { display: block; width: 100%; height: auto; }

/* 圖還沒回來之前，頁面照 PDF 的長寬比先佔好位置，捲軸長度一開始就對 */
.sheet.loading::after,
.sheet.failed::after {
  content: '載入中…';
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--ink-faint);
  font-size: .9rem;
  pointer-events: none;
}
.sheet.failed::after { content: '這一頁載入失敗'; color: var(--seal); }

/* 頁面還沒載完的提示：黏在可視範圍底部，不擋點擊 */
.load-pill {
  position: sticky;
  bottom: 1rem;
  z-index: 20;
  flex-shrink: 0;
  pointer-events: none;
  background: var(--ink);
  color: #fff;
  opacity: .88;
  padding: .4rem 1rem;
  border-radius: 999px;
  font-size: .82rem;
  font-variant-numeric: tabular-nums;
}
.load-pill[hidden] { display: none; }
/* 兩個都在的時候，toast 往上讓一點，不要疊在一起 */
body.pages-loading .toast { bottom: 4.25rem; }

.sheet-no {
  position: absolute;
  top: 0; left: -3.1rem;
  font-size: .8rem;
  color: var(--ink-faint);
  font-variant-numeric: tabular-nums;
}

.overlay {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  overflow: visible;
}

.mark { pointer-events: auto; cursor: grab; }
.mark text { font-family: var(--kai); fill: #000; }
.mark rect { fill: transparent; stroke: transparent; stroke-width: 1; }
.mark:hover rect { stroke: var(--ink-faint); }
.mark.on rect { fill: var(--seal-wash); stroke: var(--seal); }
.mark:focus-visible rect { stroke: var(--seal); stroke-width: 2; }

.presets {
  padding: .8rem 1.25rem;
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  gap: .7rem;
}
.presets-hint { font-size: .8rem; color: var(--ink-faint); flex-shrink: 0; }
.chips { display: flex; gap: .4rem; flex-wrap: wrap; }
.chips button {
  padding: .3rem .7rem;
  font-family: var(--kai);
  font-size: 1rem;
  line-height: 1.3;
  border-radius: 2px;
}
.chips button[aria-pressed="true"] {
  background: var(--seal-wash);
  border-color: var(--seal);
}

.seg {
  display: flex;
  border: 1px solid var(--line);
  border-radius: 3px;
  overflow: hidden;
}
.seg button {
  flex: 1;
  border: 0;
  border-radius: 0;
  padding: .38rem 0;
  font-size: .88rem;
  background: #fff;
}
.seg button + button { border-left: 1px solid var(--line); }
.seg button[aria-pressed="true"] { background: var(--ink); color: #fff; }
.seg button[aria-pressed="true"]:hover { background: #000; }

/* ---------- 外掛 ---------- */

/* 側欄的外掛開關列：左邊開關、右邊狀態，出錯時狀態自己佔一行 */
.plugin-bar {
  padding: .7rem 1.25rem;
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  gap: .7rem;
  flex-wrap: wrap;
}
.plugin-switch {
  display: flex;
  align-items: center;
  gap: .4rem;
  cursor: pointer;
  font-size: .9rem;
  user-select: none;
  flex-shrink: 0;
}
.plugin-bar > button:not(.plugin-status) { padding: .25rem .6rem; font-size: .82rem; flex-shrink: 0; }
.plugin-status {
  margin-left: auto;
  min-width: 0;
  font-size: .78rem;
  color: var(--ink-faint);
  text-align: right;
}
.plugin-status.bad {
  color: var(--seal);
  flex: 1 0 100%;
  margin-left: 0;
  text-align: left;
  line-height: 1.4;
}

/* 外掛接管點擊的模式：常用戳章壓灰、游標改回箭頭。
   核心的空狀態寫著「點一下就會放上一個簽章」，這時候剛好相反，先藏起來 */
body.mode-on .presets { opacity: .4; }
body.mode-on .sheet { cursor: default; }
body.mode-on .empty { display: none; }

/* ---------- 側欄 ---------- */

.panel-head {
  padding: 1.1rem 1.25rem .9rem;
  border-bottom: 1px solid var(--line);
}
.doc-name {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.doc-meta { color: var(--ink-faint); font-size: .85rem; }

.list { flex: 1; overflow-y: auto; }

.empty {
  padding: 2.5rem 1.5rem;
  color: var(--ink-soft);
  text-align: center;
  font-size: .92rem;
}

.item {
  padding: .85rem 1.25rem;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
  display: flex;
  gap: .75rem;
  align-items: baseline;
}
.item:hover { background: #FAFAF9; }
.item.on { background: var(--seal-wash); box-shadow: inset 3px 0 0 var(--seal); }
.item-pg {
  color: var(--ink-faint);
  font-size: .8rem;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
  min-width: 2.4em;
}
.item-nm {
  font-family: var(--kai);
  font-size: 1.15rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.item-dt { color: var(--ink-soft); font-size: .8rem; margin-left: auto; flex-shrink: 0; }

.editor { border-top: 1px solid var(--line); padding: 1.1rem 1.25rem; background: #FAFAF9; }
.field { margin-bottom: .8rem; }
.field label { display: block; font-size: .82rem; color: var(--ink-soft); margin-bottom: .25rem; }
.field input[type=text] {
  width: 100%;
  padding: .45rem .6rem;
  border: 1px solid var(--line);
  border-radius: 3px;
  background: #fff;
}
.field input[type=range] { width: 100%; }
.field textarea {
  width: 100%;
  padding: .45rem .6rem;
  border: 1px solid var(--line);
  border-radius: 3px;
  background: #fff;
  line-height: 1.5;
  resize: vertical;
}
.field .hint { margin: .3rem 0 0; font-size: .78rem; color: var(--ink-faint); }
.pair { display: flex; gap: .75rem; }
.pair > * { flex: 1; }
.size-val { color: var(--ink-faint); font-variant-numeric: tabular-nums; }

.panel-foot {
  padding: 1rem 1.25rem;
  border-top: 1px solid var(--line);
  display: flex;
  gap: .6rem;
}
.panel-foot .primary { flex: 1; }

.toast {
  position: fixed;
  bottom: 1.5rem; left: 50%;
  transform: translateX(-50%);
  background: var(--ink);
  color: #fff;
  padding: .6rem 1.1rem;
  border-radius: 3px;
  font-size: .9rem;
  z-index: 50;
}

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; }
}
</style>
</head>
<body>

<div class="shell">
  <main class="stage" id="stage">
    <div class="intro" id="intro">
      <h1>簽章</h1>
      <p>開啟 PDF，在要簽的位置點一下。輸出是可選取的文字，不是圖片。</p>
      <div class="drop" id="drop">
        <button class="primary" id="pick">選擇 PDF</button>
        <p style="margin:.9rem 0 0;font-size:.85rem;color:var(--ink-faint)">或把檔案拖進視窗任何地方</p>
      </div>
    </div>
  </main>

  <aside class="panel" id="panel" hidden>
    <div class="panel-head">
      <div class="doc-name" id="docName"></div>
      <div class="doc-meta" id="docMeta"></div>
    </div>
    <div class="presets">
      <span class="presets-hint">下一個蓋上</span>
      <div class="chips" id="chips"></div>
    </div>
    <div class="list" id="list"></div>
    <div class="editor" id="editor" hidden>
      <div class="field">
        <label for="fName" id="nameLabel">姓名</label>
        <textarea id="fName" rows="1" autocomplete="off" spellcheck="false"></textarea>
        <p class="hint" id="nameHint" hidden>按 Enter 換行</p>
      </div>
      <div class="field" id="dateField">
        <label for="fDate">日期</label>
        <input type="text" id="fDate" autocomplete="off">
      </div>
      <div class="pair">
        <div class="field">
          <label id="alignLabel">對齊（以點擊處為基準）</label>
          <div class="seg" role="group" aria-labelledby="alignLabel" id="segAlign">
            <button data-align="left" aria-pressed="false"
                    title="文字左緣貼齊你點的位置，往右延伸">左緣</button>
            <button data-align="center" aria-pressed="true"
                    title="文字以你點的位置為中心">置中</button>
            <button data-align="right" aria-pressed="false"
                    title="文字右緣貼齊你點的位置，往左延伸">右緣</button>
          </div>
        </div>
        <div class="field" id="layoutField">
          <label id="layoutLabel">日期位置</label>
          <div class="seg" role="group" aria-labelledby="layoutLabel" id="segLayout">
            <button data-layout="stack" aria-pressed="true">下方</button>
            <button data-layout="inline" aria-pressed="false">同行</button>
          </div>
        </div>
      </div>
      <div class="pair">
        <div class="field">
          <label for="fNS"><span id="nsLabel">姓名大小</span>
            <span class="size-val" id="vNS"></span></label>
          <input type="range" id="fNS" min="10" max="48" step="1">
        </div>
        <div class="field" id="dsField">
          <label for="fDS">日期大小 <span class="size-val" id="vDS"></span></label>
          <input type="range" id="fDS" min="7" max="28" step="1">
        </div>
      </div>
      <button id="del">刪除這個簽章</button>
    </div>
    <div class="panel-foot">
      <button class="primary" id="save">下載簽好的 PDF</button>
      <button id="reset">刪除</button>
    </div>
  </aside>
</div>

<input type="file" id="file" accept="application/pdf" hidden>

<script>
const $ = s => document.querySelector(s);
const state = {
  doc: null, pages: [], marks: [], sel: -1, roc: '', preset: 'sign', dragging: false,
  // 簽名與自由文字各記各的，切換模式時不會互相蓋掉
  signName: '', freeText: '無關',
  tpl: { kind: 'sign', name: '', dateText: '', nameSize: 22, dateSize: 12,
         gap: 6, align: 'center', layout: 'stack' }
};

// 換行的行距倍率，跟後端的 LINE_FACTOR 必須一致
const LINE_FACTOR = 1.3;

// ---------- 上傳 ----------

$('#pick').onclick = () => $('#file').click();
$('#file').onchange = e => { if (e.target.files[0]) load(e.target.files[0]); };

// 放置區是整個視窗，不只那個虛線框。
// dragenter/dragleave 會隨著游標經過每個子元素連續觸發，用深度計數才不會閃。
const drop = $('#drop');
let dragDepth = 0;

const isFileDrag = e => Array.from(e.dataTransfer?.types || []).includes('Files');

function endDrag() {
  dragDepth = 0;
  document.body.classList.remove('dropping');
  drop.classList.remove('over');
}

document.addEventListener('dragenter', e => {
  if (!isFileDrag(e) || state.doc) return;
  e.preventDefault();
  if (++dragDepth === 1) {
    document.body.classList.add('dropping');
    drop.classList.add('over');
  }
});

// dragover 不擋掉的話 drop 根本不會觸發，瀏覽器會直接開啟那份 PDF 把畫面換掉
document.addEventListener('dragover', e => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = state.doc ? 'none' : 'copy';
});

document.addEventListener('dragleave', e => {
  if (!isFileDrag(e)) return;
  if (--dragDepth <= 0) endDrag();
});

document.addEventListener('drop', e => {
  if (!isFileDrag(e)) return;   // 拖文字到輸入框之類的，讓瀏覽器自己處理
  e.preventDefault();
  endDrag();
  if (state.doc) return;        // 已經開著一份，不讓拖放把它蓋掉
  const f = e.dataTransfer.files[0];
  if (!f) return;
  // 某些系統拖過來的 type 是空的，退回看副檔名
  if (f.type === 'application/pdf' || /\.pdf$/i.test(f.name)) load(f);
  else toast('只能開啟 PDF');
});

async function load(file) {
  const fd = new FormData();
  fd.append('file', file);
  let r;
  try {
    r = await fetch('/api/upload', { method: 'POST', body: fd });
  } catch { return toast('連不上服務'); }
  if (!r.ok) return toast((await r.json()).detail || '開啟失敗');

  const data = await r.json();
  if (MODE.name) leaveMode(MODE.name, true);   // 換文件，上一份開著的模式收掉
  state.doc = data.doc;
  state.pages = data.pages;
  state.marks = [];
  state.sel = -1;

  const t = await (await fetch('/api/today')).json();
  state.roc = t.roc;
  state.tpl.dateText = t.roc;
  renderChips();

  $('#docName').textContent = data.name;
  $('#docMeta').textContent = data.pages.length + ' 頁';
  $('#panel').hidden = false;
  refreshFont();
  renderPages();
  renderList();
  document.dispatchEvent(new CustomEvent('pdfsign:loaded'));
}

// ---------- 頁面 ----------

let renderGen = 0;

function renderPages() {
  const stage = $('#stage');
  stage.innerHTML = '';

  // 頁數多的時候圖要一陣子才回得來，捲到已載入的最後一頁容易以為到底了，
  // 所以在底部掛一個「後面還有」的提示，全部回來才拿掉
  const gen = ++renderGen;
  const total = state.pages.length;
  let done = 0;
  const pill = document.createElement('div');
  pill.className = 'load-pill';
  pill.setAttribute('role', 'status');
  const tick = () => {
    if (gen !== renderGen) return;      // 換了文件，舊頁的圖晚到不算
    pill.textContent = `後面還有頁面，載入中 ${done} / ${total}`;
    pill.hidden = done >= total;
    document.body.classList.toggle('pages-loading', done < total);
  };

  state.pages.forEach((pg, i) => {
    const sheet = document.createElement('div');
    sheet.className = 'sheet loading';
    sheet.dataset.page = i;
    sheet.style.width = Math.min(pg.width * 1.35, 900) + 'px';
    sheet.style.aspectRatio = `${pg.width} / ${pg.height}`;

    const no = document.createElement('div');
    no.className = 'sheet-no';
    no.textContent = i + 1;
    sheet.appendChild(no);

    const img = document.createElement('img');
    img.alt = `第 ${i + 1} 頁`;
    img.draggable = false;
    img.onload = () => { sheet.classList.remove('loading'); done++; tick(); };
    img.onerror = () => { sheet.classList.replace('loading', 'failed'); done++; tick(); };
    img.src = `/api/page/${state.doc}/${i}`;
    sheet.appendChild(img);

    sheet.addEventListener('click', e => {
      // 拖曳戳章時若在空白處放開，click 會派給 sheet，必須擋掉
      if (state.dragging) { state.dragging = false; return; }
      if (e.target.closest('.mark')) return;
      const b = sheet.getBoundingClientRect();
      const x = (e.clientX - b.left) / b.width, y = (e.clientY - b.top) / b.height;
      // 外掛的模式開著時不蓋章：先讓外掛處理，它不處理就提示為什麼點了沒反應
      if (MODE.name) {
        if (!(MODE.blocked && MODE.blocked({ page: i, x, y }))) toast(MODE.hint);
        return;
      }
      // 外掛可以攔掉這一下
      const place = new CustomEvent('pdfsign:place',
                                   { detail: { page: i, x, y }, cancelable: true });
      if (!document.dispatchEvent(place)) return;
      addMark(i, x, y);
    });

    stage.appendChild(sheet);
  });
  stage.appendChild(pill);
  tick();
  drawMarks();
}

// ---------- 外掛的模式 ----------

// 會接管頁面點擊的外掛（填表、修正原文…）同時只能開一個。開著的時候常用戳章
// 停用、點頁面不蓋章，都由這裡統一處理，外掛只要說開或關。
//   enterMode(name, { off, hint, blocked })
//     off      被別的模式擠掉、或換了一份文件時呼叫，外掛在裡面把自己的畫面收起來
//     hint     點到頁面上沒東西的地方時的提示
//     blocked  同上，但先交給外掛處理；回傳 true 就不跳提示
//   leaveMode(name)
const MODE = { name: null, off: null, hint: '', blocked: null };

function enterMode(name, opts = {}) {
  if (MODE.name && MODE.name !== name) leaveMode(MODE.name, true);
  Object.assign(MODE, { name, off: opts.off || null, hint: opts.hint || '',
                        blocked: opts.blocked || null });
  // inert 會連鍵盤與輔助技術一起擋掉，而且掛在容器上，重畫 chips 也不會弄丟
  $('.presets').inert = true;
  document.body.classList.add('mode-on');
}

function leaveMode(name, notify) {
  if (MODE.name !== name) return;
  const off = MODE.off;
  Object.assign(MODE, { name: null, off: null, hint: '', blocked: null });
  $('.presets').inert = false;
  document.body.classList.remove('mode-on');
  if (notify && off) off();
}

// ---------- 樣式範本：新戳章沿用目前設定 ----------

const PRESETS = [
  { id: 'sign', chip: '簽名', make: () => ({
      kind: 'sign', name: state.signName, dateText: state.roc,
      nameSize: 22, dateSize: 12, gap: 6, align: 'center', layout: 'stack' }) },
  // 自由文字：沒有日期、可以換行。原本的「僅簽名」與「無關」都是它的特例，
  // 預設帶上一次寫過的內容（第一次是「無關」）
  { id: 'text', chip: '文字', make: () => ({
      kind: 'text', name: state.freeText, dateText: '',
      nameSize: 20, dateSize: 12, gap: 6, align: 'center', layout: 'stack' }) }
];

function renderChips() {
  const box = $('#chips');
  box.innerHTML = '';
  PRESETS.forEach(p => {
    const b = document.createElement('button');
    b.textContent = p.chip;
    b.setAttribute('aria-pressed', String(state.preset === p.id));
    b.onclick = () => {
      state.preset = p.id;
      const preset = p.make();
      // 有選取中的戳章就一起套用，符合「按了就看到變化」的預期。
      // 但換模式不該把已經寫好的字弄丟，空白的才吃範本帶來的預設內容。
      const m = state.marks[state.sel];
      if (m && m.name) preset.name = m.name;
      state.tpl = { ...state.tpl, ...preset };
      if (m) {
        Object.assign(m, preset);
        rememberName(m);
        select(state.sel);
        state.preset = p.id;   // select() 不會動 preset，但編輯欄位會，這裡固定回來
      }
      renderChips();
      refreshFont();
      drawMarks();
    };
    box.appendChild(b);
  });
}

function addMark(page, x, y) {
  state.marks.push({ page, x, y, ...state.tpl });
  select(state.marks.length - 1);
  refreshFont();
  if (!state.marks[state.sel].name) $('#fName').focus();
}

// ---------- 楷體子集：只載入畫面上真正用到的字 ----------

const SEED = __SEED__;
const FONT_TAG = __FONTTAG__;
const KAI = { loaded: new Set(), queue: new Set(), pending: false,
              composing: false, timer: 0 };

// 每份 webfont 檔案的字符是固定的，沒辦法對已載入的字型追加。
// 但用 unicode-range 可以註冊多份同名字型、各自只負責特定字，
// 瀏覽器會逐字挑選——所以每次只需要下載「還沒有的字」。
function neededChars() {
  const set = new Set(SEED);
  const feed = str => { for (const ch of String(str || '')) if (ch >= ' ') set.add(ch); };
  state.marks.forEach(m => { feed(m.name); feed(m.dateText); });
  feed(state.tpl.name); feed(state.tpl.dateText);
  feed(state.signName); feed(state.freeText);
  return set;
}

function loadFont() {
  if (KAI.pending || KAI.composing || !KAI.queue.size) return;

  const chunk = [...KAI.queue];
  KAI.queue.clear();
  KAI.pending = true;

  const range = chunk
    .map(ch => 'U+' + ch.codePointAt(0).toString(16).toUpperCase())
    .join(',');

  fetch('/font.ttf?v=' + FONT_TAG + '&chars=' + encodeURIComponent(chunk.join('')))
    .then(r => r.ok ? r.arrayBuffer() : Promise.reject())
    .then(buf => new FontFace('TWKai', buf, { unicodeRange: range }).load())
    .then(face => {
      document.fonts.add(face);
      chunk.forEach(ch => KAI.loaded.add(ch));
      drawMarks();               // 字寬變了，選取框要重量
    })
    .catch(() => { chunk.forEach(ch => KAI.queue.add(ch)); })   // 失敗放回待抓
    .finally(() => {
      KAI.pending = false;
      if (KAI.queue.size) setTimeout(loadFont, 0);
    });
}

// 注音輸入法連組字中的符號都會觸發 input，組字期間整個跳過。
function refreshFont() {
  if (KAI.composing) return;
  let queued = false;
  for (const ch of neededChars()) {
    if (!KAI.loaded.has(ch) && !KAI.queue.has(ch)) { KAI.queue.add(ch); queued = true; }
  }
  if (!queued) return;
  clearTimeout(KAI.timer);
  KAI.timer = setTimeout(loadFont, 400);
}

const SVGNS = 'http://www.w3.org/2000/svg';
const ANCHOR = { left: 'start', center: 'middle', right: 'end' };

function svgText(x, y, anchor) {
  const t = document.createElementNS(SVGNS, 'text');
  t.setAttribute('x', x);
  t.setAttribute('y', y);              // y 就是基線，與 PDF 同義
  t.setAttribute('text-anchor', anchor);
  // SVG 預設會把前後空白吃掉，輸出的 PDF 卻會照算。不保留的話，
  // 用空白縮排的那一行在畫面上是置中的、在 PDF 裡卻往旁邊偏。
  t.setAttributeNS('http://www.w3.org/XML/1998/namespace', 'xml:space', 'preserve');
  return t;
}

function span(str, size) {
  const s = document.createElementNS(SVGNS, 'tspan');
  s.setAttribute('font-size', size);
  s.textContent = str;
  return s;
}

function drawMarks() {
  document.querySelectorAll('.overlay').forEach(n => n.remove());

  state.pages.forEach((pg, pi) => {
    const sheet = document.querySelector(`.sheet[data-page="${pi}"]`);
    if (!sheet) return;
    const mine = state.marks.map((m, i) => ({ m, i })).filter(o => o.m.page === pi);
    if (!mine.length) return;

    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'overlay');
    svg.setAttribute('viewBox', `0 0 ${pg.width} ${pg.height}`);
    svg.setAttribute('preserveAspectRatio', 'none');

    const boxes = [];
    mine.forEach(({ m, i }) => {
      const g = document.createElementNS(SVGNS, 'g');
      g.setAttribute('class', 'mark' + (i === state.sel ? ' on' : ''));
      g.setAttribute('tabindex', '0');

      const box = document.createElementNS(SVGNS, 'rect');
      g.appendChild(box);

      const x = m.x * pg.width, y = m.y * pg.height;
      const anchor = ANCHOR[m.align] || 'middle';

      // 空的戳章給一個全形空白撐住，不然選取框會縮成一條線
      const lines = (m.name || '　').split('\n');
      const step = m.nameSize * LINE_FACTOR;
      const base = y + step * (lines.length - 1);   // 最後一行的基線
      const last = lines[lines.length - 1];

      lines.slice(0, -1).forEach((ln, li) => {
        const t = svgText(x, y + step * li, anchor);
        t.appendChild(span(ln, m.nameSize));
        g.appendChild(t);
      });

      if (m.layout === 'inline') {
        // 同一條基線，日期緊接在最後一行之後
        const t = svgText(x, base, anchor);
        t.appendChild(span(last, m.nameSize));
        if (m.dateText) t.appendChild(span(m.dateText, m.dateSize));
        g.appendChild(t);
      } else {
        const t1 = svgText(x, base, anchor);
        t1.appendChild(span(last, m.nameSize));
        g.appendChild(t1);
        if (m.dateText) {
          const t2 = svgText(x, base + m.dateSize + m.gap, anchor);
          t2.appendChild(span(m.dateText, m.dateSize));
          g.appendChild(t2);
        }
      }

      g.addEventListener('click', ev => { ev.stopPropagation(); select(i); });
      g.addEventListener('mousedown', ev => startDrag(ev, i, sheet));
      svg.appendChild(g);
      boxes.push([g, box]);
    });

    sheet.appendChild(svg);

    // 進 DOM 之後才量得到 bbox，用來畫選取框
    boxes.forEach(([g, box]) => {
      const b = g.getBBox();
      box.setAttribute('x', b.x - 3);
      box.setAttribute('y', b.y - 3);
      box.setAttribute('width', b.width + 6);
      box.setAttribute('height', b.height + 6);
    });
  });

  document.dispatchEvent(new CustomEvent('pdfsign:drawn'));
}

function startDrag(ev, i, sheet) {
  ev.preventDefault(); ev.stopPropagation();
  select(i);
  let moved = false;
  const move = e => {
    moved = true;
    state.dragging = true;
    const b = sheet.getBoundingClientRect();
    const m = state.marks[i];
    m.x = Math.max(0, Math.min(1, (e.clientX - b.left) / b.width));
    m.y = Math.max(0, Math.min(1, (e.clientY - b.top) / b.height));
    drawMarks();
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    // 沒真的移動就不算拖曳，讓 click 正常處理選取
    if (!moved) state.dragging = false;
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

// ---------- 清單與編輯 ----------

function renderList() {
  const list = $('#list');
  if (!state.marks.length) {
    list.innerHTML = '<div class="empty">在頁面上點一下，就會放上一個簽章。</div>';
    $('#editor').hidden = true;
    return;
  }
  list.innerHTML = '';
  state.marks.forEach((m, i) => {
    const row = document.createElement('div');
    row.className = 'item' + (i === state.sel ? ' on' : '');
    row.innerHTML = `<span class="item-pg">p.${m.page + 1}</span>
                     <span class="item-nm"></span>
                     <span class="item-dt"></span>`;
    row.querySelector('.item-nm').textContent =
      m.name.split('\n').join(' ') || (m.kind === 'text' ? '未填內容' : '未填姓名');
    row.querySelector('.item-dt').textContent = m.dateText;
    row.onclick = () => {
      select(i);
      document.querySelector(`.sheet[data-page="${m.page}"]`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
    list.appendChild(row);
  });
}

const TPL_KEYS = ['kind','name','dateText','nameSize','dateSize','gap','align','layout'];

// 簽名與自由文字各記各的，切回去時不會撿到另一邊的字
function rememberName(m) {
  if (m.kind === 'text') state.freeText = m.name; else state.signName = m.name;
}

function syncTpl(m) {
  TPL_KEYS.forEach(k => state.tpl[k] = m[k]);
  rememberName(m);
  state.preset = null;
  renderChips();
}

// 自由文字沒有日期，相關欄位一併收起來，順手把措辭換成「內容」
function applyKind(m) {
  const free = m.kind === 'text';
  $('#nameLabel').textContent = free ? '內容' : '姓名';
  $('#nsLabel').textContent = free ? '文字大小' : '姓名大小';
  $('#nameHint').hidden = !free;
  $('#dateField').hidden = free;
  $('#layoutField').hidden = free;
  $('#dsField').hidden = free;
  growName(m);
}

// 自由文字欄位跟著行數長高，最多六行
function growName(m) {
  $('#fName').rows = m.kind === 'text'
    ? Math.min(6, Math.max(2, String(m.name || '').split('\n').length))
    : 1;
}

function select(i) {
  state.sel = i;
  const m = state.marks[i];
  $('#editor').hidden = !m;
  if (m) {
    applyKind(m);
    $('#fName').value = m.name;
    $('#fDate').value = m.dateText;
    $('#fNS').value = m.nameSize; $('#vNS').textContent = m.nameSize;
    $('#fDS').value = m.dateSize; $('#vDS').textContent = m.dateSize;
    $('#segAlign').querySelectorAll('button').forEach(b =>
      b.setAttribute('aria-pressed', String(b.dataset.align === m.align)));
    $('#segLayout').querySelectorAll('button').forEach(b =>
      b.setAttribute('aria-pressed', String(b.dataset.layout === m.layout)));
  }
  drawMarks(); renderList();
}

function segHandler(sel, key) {
  $(sel).querySelectorAll('button').forEach(btn => {
    btn.onclick = () => {
      const m = state.marks[state.sel];
      if (!m) return;
      m[key] = btn.dataset[key];
      $(sel).querySelectorAll('button').forEach(b =>
        b.setAttribute('aria-pressed', String(b === btn)));
      syncTpl(m); drawMarks();
    };
  });
}
segHandler('#segAlign', 'align');
segHandler('#segLayout', 'layout');

function edit(fn) {
  return e => {
    const m = state.marks[state.sel];
    if (!m) return;
    fn(m, e.target.value);
    syncTpl(m);
    refreshFont();
    drawMarks(); renderList();
  };
}

$('#fName').oninput = edit((m, v) => { m.name = v; growName(m); });
$('#fDate').oninput = edit((m, v) => m.dateText = v);

// 簽名只有一行，Enter 不該把它撐開；自由文字才讓 Enter 換行
$('#fName').addEventListener('keydown', e => {
  const m = state.marks[state.sel];
  if (e.key === 'Enter' && (!m || m.kind !== 'text')) e.preventDefault();
});

// 中文輸入法組字中的注音／候選字不該被當成要載入的字
['#fName', '#fDate'].forEach(sel => {
  $(sel).addEventListener('compositionstart', () => { KAI.composing = true; });
  $(sel).addEventListener('compositionend', () => {
    KAI.composing = false;
    refreshFont();
  });
});
$('#fNS').oninput = edit((m, v) => { m.nameSize = +v; $('#vNS').textContent = v; });
$('#fDS').oninput = edit((m, v) => { m.dateSize = +v; $('#vDS').textContent = v; });

$('#del').onclick = () => {
  if (state.sel < 0) return;
  state.marks.splice(state.sel, 1);
  state.sel = -1;
  $('#editor').hidden = true;
  drawMarks(); renderList();
};

document.addEventListener('keydown', e => {
  if (e.key === 'Delete' && state.sel >= 0 &&
      !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
    $('#del').click();
  }
});

// ---------- 輸出 ----------

$('#save').onclick = async () => {
  // 外掛把自己要一起輸出的東西放進 extra（例如修正原文），後端交給對應的外掛
  const collect = new CustomEvent('pdfsign:collect', { detail: { extra: {} } });
  document.dispatchEvent(collect);
  const extra = collect.detail.extra;
  if (!state.marks.length && !Object.keys(extra).length) return toast('還沒有放上任何簽章');
  const btn = $('#save');
  btn.disabled = true; btn.textContent = '產生中…';
  try {
    const r = await fetch('/api/sign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: state.doc, placements: state.marks, extra })
    });
    if (!r.ok) throw new Error((await r.json()).detail || '產生失敗');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = ($('#docName').textContent || 'document').replace(/\.pdf$/i, '') + '-已簽.pdf';
    a.click();
    URL.revokeObjectURL(a.href);
    toast('已下載');
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false; btn.textContent = '下載簽好的 PDF';
  }
};

// 刪掉就沒了，第一下只「上膛」：10 秒內再按一次才真的刪，逾時自己復原
const RESET = { timer: 0 };

function disarmReset() {
  clearTimeout(RESET.timer);
  RESET.timer = 0;
  $('#reset').textContent = '刪除';
  $('#reset').classList.remove('danger');
}

$('#reset').onclick = () => {
  if (!RESET.timer) {
    $('#reset').textContent = '確認刪除';
    $('#reset').classList.add('danger');
    RESET.timer = setTimeout(disarmReset, 10000);
    return;
  }
  disarmReset();
  if (state.doc) fetch('/api/doc/' + state.doc, { method: 'DELETE' });
  location.reload();
};

// ---------- 雜項 ----------

let toastTimer;
function toast(msg) {
  clearTimeout(toastTimer);
  document.querySelector('.toast')?.remove();
  const el = document.createElement('div');
  el.className = 'toast';
  el.setAttribute('role', 'status');
  el.textContent = msg;
  document.body.appendChild(el);
  toastTimer = setTimeout(() => el.remove(), 2600);
}

let resizeTimer;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(drawMarks, 120);
});

// 介面本身也要楷體（標題、快捷鍵、清單）
refreshFont();
</script>
__PLUGINS__
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    scripts = "\n".join(f"<script>{js}</script>" for js in PLUGIN_SCRIPTS)
    return (
        INDEX.replace("__SEED__", json.dumps(SEED_CHARS, ensure_ascii=False))
        .replace("__FONTTAG__", json.dumps(FONT_TAG))
        .replace("__PLUGINS__", scripts)
    )


if __name__ == "__main__":
    import uvicorn

    print(f"字型：{FONT_PATH}")
    if PLUGIN_NAMES:
        print(f"外掛：{'、'.join(PLUGIN_NAMES)}")
    print(f"啟動於 http://0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
