#!/usr/bin/env python3
"""
簽章工具 — 點選 PDF 位置，蓋上姓名與民國日期。

輸出為真正的向量文字（字型嵌入），非圖片。
字型使用全字庫正楷體 TW-Kai。

執行：
    python3 app.py
    然後瀏覽 http://<ip>
"""

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
    "或選擇還沒有蓋任何請確認後端狀態與網路連線重試產生失敗已"
    "0123456789/.:-"
)


@lru_cache(maxsize=32)
def webfont(chars: str) -> bytes:
    if not HAVE_FONTTOOLS:
        return Path(FONT_PATH).read_bytes()
    opts = SubsetOptions()
    opts.layout_features = []
    opts.notdef_outline = True
    opts.recalc_bounds = False
    opts.ignore_missing_unicodes = True
    font = TTFont(FONT_PATH)
    sub = Subsetter(options=opts)
    sub.populate(text=chars or SEED_CHARS)
    sub.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()

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
    if not req.placements:
        raise HTTPException(400, "還沒有放上任何簽章")

    doc = pymupdf.open(doc_path(req.doc))

    # 每次簽署用不重複的字型別名。若沿用固定名稱，對「已經簽過一次」的檔案
    # 再簽時，PyMuPDF 會重用頁面裡既有的（已子集化的）字型資源而忽略 fontfile，
    # 導致新字沒有對應字符，輸出變成空白方框。
    alias = "kai" + uuid.uuid4().hex[:8]

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
        name, dated = p.name.strip(), p.dateText.strip()

        def put(text: str, x: float, y: float, size: float) -> None:
            page.insert_text((x, y), text, fontname=alias,
                             fontfile=FONT_PATH, fontsize=size, color=(0, 0, 0))

        if p.layout == "inline":
            # 姓名與日期同一條基線，日期緊接在後（如：謝誠銓115/09/02）
            wn = width(p.name, p.nameSize)
            x0 = start_at(wn + width(p.dateText, p.dateSize), px, p.align)
            if name:
                put(p.name, x0, py, p.nameSize)
            if dated:
                put(p.dateText, x0 + wn, py, p.dateSize)
        else:
            if name:
                put(p.name, start_at(width(p.name, p.nameSize), px, p.align),
                    py, p.nameSize)
            if dated:
                put(p.dateText,
                    start_at(width(p.dateText, p.dateSize), px, p.align),
                    py + p.dateSize + p.gap, p.dateSize)

    # 只嵌入實際用到的字，否則整支楷體（約 50MB）會被塞進檔案
    try:
        doc.subset_fonts(verbose=False)
    except Exception:
        pass

    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True)
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

button, input, select { font: inherit; color: inherit; }

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
        <label for="fName">姓名</label>
        <input type="text" id="fName" autocomplete="off">
      </div>
      <div class="field">
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
        <div class="field">
          <label id="layoutLabel">日期位置</label>
          <div class="seg" role="group" aria-labelledby="layoutLabel" id="segLayout">
            <button data-layout="stack" aria-pressed="true">下方</button>
            <button data-layout="inline" aria-pressed="false">同行</button>
          </div>
        </div>
      </div>
      <div class="pair">
        <div class="field">
          <label for="fNS">姓名大小 <span class="size-val" id="vNS"></span></label>
          <input type="range" id="fNS" min="10" max="48" step="1">
        </div>
        <div class="field">
          <label for="fDS">日期大小 <span class="size-val" id="vDS"></span></label>
          <input type="range" id="fDS" min="7" max="28" step="1">
        </div>
      </div>
      <button id="del">刪除這個簽章</button>
    </div>
    <div class="panel-foot">
      <button class="primary" id="save">下載簽好的 PDF</button>
      <button id="reset">換一份</button>
    </div>
  </aside>
</div>

<input type="file" id="file" accept="application/pdf" hidden>

<script>
const $ = s => document.querySelector(s);
const state = {
  doc: null, pages: [], marks: [], sel: -1, roc: '', preset: 'sign', dragging: false,
  tpl: { name: '', dateText: '', nameSize: 22, dateSize: 12,
         gap: 6, align: 'center', layout: 'stack' }
};

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
}

// ---------- 頁面 ----------

function renderPages() {
  const stage = $('#stage');
  stage.innerHTML = '';
  state.pages.forEach((pg, i) => {
    const sheet = document.createElement('div');
    sheet.className = 'sheet';
    sheet.dataset.page = i;
    sheet.style.width = Math.min(pg.width * 1.35, 900) + 'px';

    const no = document.createElement('div');
    no.className = 'sheet-no';
    no.textContent = i + 1;
    sheet.appendChild(no);

    const img = document.createElement('img');
    img.src = `/api/page/${state.doc}/${i}`;
    img.alt = `第 ${i + 1} 頁`;
    img.draggable = false;
    sheet.appendChild(img);

    sheet.addEventListener('click', e => {
      // 拖曳戳章時若在空白處放開，click 會派給 sheet，必須擋掉
      if (state.dragging) { state.dragging = false; return; }
      if (e.target.closest('.mark')) return;
      const b = sheet.getBoundingClientRect();
      addMark(i, (e.clientX - b.left) / b.width, (e.clientY - b.top) / b.height);
    });

    stage.appendChild(sheet);
  });
  drawMarks();
}

// ---------- 樣式範本：新戳章沿用目前設定 ----------

const PRESETS = [
  { id: 'sign', chip: '簽名', make: () => ({
      name: state.tpl.name, dateText: state.roc,
      nameSize: 22, dateSize: 12, gap: 6, align: 'center', layout: 'stack' }) },
  { id: 'plain', chip: '僅簽名', make: () => ({
      name: state.tpl.name, dateText: '',
      nameSize: 22, dateSize: 12, gap: 6, align: 'center', layout: 'stack' }) },
  { id: 'na', chip: '無關', make: () => ({
      name: '無關', dateText: '',
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
      state.tpl = { ...state.tpl, ...preset };
      // 有選取中的戳章就一起套用，符合「按了就看到變化」的預期
      const m = state.marks[state.sel];
      if (m) {
        Object.assign(m, preset);
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
const KAI = { loaded: new Set(), queue: new Set(), pending: false,
              composing: false, timer: 0 };

// 每份 webfont 檔案的字符是固定的，沒辦法對已載入的字型追加。
// 但用 unicode-range 可以註冊多份同名字型、各自只負責特定字，
// 瀏覽器會逐字挑選——所以每次只需要下載「還沒有的字」。
function neededChars() {
  const set = new Set(SEED);
  const feed = str => { for (const ch of String(str || '')) set.add(ch); };
  state.marks.forEach(m => { feed(m.name); feed(m.dateText); });
  feed(state.tpl.name); feed(state.tpl.dateText);
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

  fetch('/font.ttf?chars=' + encodeURIComponent(chunk.join('')))
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

      if (m.layout === 'inline') {
        // 同一條基線，日期緊接在姓名之後
        const t = svgText(x, y, anchor);
        t.appendChild(span(m.name || '　', m.nameSize));
        if (m.dateText) t.appendChild(span(m.dateText, m.dateSize));
        g.appendChild(t);
      } else {
        const t1 = svgText(x, y, anchor);
        t1.appendChild(span(m.name || '　', m.nameSize));
        g.appendChild(t1);
        if (m.dateText) {
          const t2 = svgText(x, y + m.dateSize + m.gap, anchor);
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
    row.querySelector('.item-nm').textContent = m.name || '未填姓名';
    row.querySelector('.item-dt').textContent = m.dateText;
    row.onclick = () => {
      select(i);
      document.querySelector(`.sheet[data-page="${m.page}"]`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
    list.appendChild(row);
  });
}

const TPL_KEYS = ['name','dateText','nameSize','dateSize','gap','align','layout'];

function syncTpl(m) {
  TPL_KEYS.forEach(k => state.tpl[k] = m[k]);
  state.preset = null;
  renderChips();
}

function select(i) {
  state.sel = i;
  const m = state.marks[i];
  $('#editor').hidden = !m;
  if (m) {
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

$('#fName').oninput = edit((m, v) => m.name = v);
$('#fDate').oninput = edit((m, v) => m.dateText = v);

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
  if (!state.marks.length) return toast('還沒有放上任何簽章');
  const btn = $('#save');
  btn.disabled = true; btn.textContent = '產生中…';
  try {
    const r = await fetch('/api/sign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: state.doc, placements: state.marks })
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

$('#reset').onclick = () => {
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
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX.replace("__SEED__", json.dumps(SEED_CHARS, ensure_ascii=False))


if __name__ == "__main__":
    import uvicorn

    print(f"字型：{FONT_PATH}")
    print(f"啟動於 http://0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
