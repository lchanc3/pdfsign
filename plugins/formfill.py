#!/usr/bin/env python3
"""
填表外掛 — 在 PDF 既有的表格列裡找空格，填進當下的時間或固定文字。

典型的目標長這樣（表格原本就印好的字）：

    異常排除時間：    年    月    日    時    分

規則全部寫在同目錄的 formfill.rules.jsonc，這支程式只是引擎，不內建任何規則。
改完規則存檔即生效，不用重開服務。

規則檔是 JSONC：可以寫 // 和 /* */ 註解，物件與陣列最後面多一個逗號也沒關係。
舊的 formfill.rules.json 也讀得到（同樣吃註解），兩個都在時只會讀 .jsonc 並提醒。

規則格式
--------
{ "rules": [ {
    "id":    "abnormal-clear",        // 選填，做為命中的識別碼
    "name":  "異常排除時間",           // 選填，顯示用；沒有就用 label
    "label": "異常排除時間",           // 錨定用的文字，可以給陣列當作別名
    "slots": [ ... ]                  // 由左到右要填的空格
} ] }

每個 slot 指定「填在哪裡」和「填什麼」：

  填在哪裡（三選一，都不給就是接在標籤／上一個錨點後面，並自動跳過緊接的冒號）
    "before": "年"    填在這個字左邊的空白裡，預設右對齊 → 「115 年」
    "after":  "："    填在這個字右邊的空白裡，預設左對齊 → 「：王小明」
    "align":  "left" | "center" | "right"    想覆寫預設對齊時才給

  往右填的時候，緊接著的底線或點線（＿﹍─…）算是畫出來的填寫欄，
  字會寫在那條線上面，而不是擠在它前面。

  填什麼
    "value": roc_year year month day hour minute second roc_date roc_short text
    "text":  "..."    value 是 text 時要填的固定字串
    "digits": 2       數字補零到幾位，預設時分秒與月日為 2、年為 1

  其他
    "optional": true  找不到這個錨點也算命中（預設 false，少一個就整條跳過）

同一列必須找到全部的必要錨點才算命中——「異常發生時間」那種結構相同的鄰列
才不會被誤填。已填偵測看的是空格裡有沒有數字。
"""

import json
import re
import unicodedata
from pathlib import Path

import pymupdf
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/formfill")

CTX = None

# 先找 .jsonc；.json 是舊檔名，留著讓已經裝好的機器不會因為改版就讀不到規則
RULES_PATHS = [Path(__file__).with_name("formfill.rules.jsonc"),
               Path(__file__).with_name("formfill.rules.json")]

VALUE_KINDS = {"roc_year", "year", "month", "day", "hour", "minute", "second",
               "roc_date", "roc_short", "text"}
ALIGNS = {"left", "center", "right"}

# before 錨點的空白超過字級的這個倍數，多半是撿到同一列別的格子裡的字
MAX_BLANK = 20

# 沒指定錨點時，標籤後面緊接的這些字是裝飾不是內容，要跳過去才碰得到真正的空白
LEAD = "：:︰﹕"

# 底線、點線這類「畫出來的填寫欄」本身就是空白，字要寫在它上面而不是它後面
RULED = "＿_﹍﹏ˍ￣‾─━—–－…"


def setup(ctx) -> None:
    global CTX
    CTX = ctx


def norm(text: str) -> str:
    """把 CJK 相容字正規化回常用碼位。

    標楷體嵌進 PDF 之後，抽出來的「異」常常是 U+F962 而不是 U+7570，
    看起來一模一樣但字串比對永遠對不上。逐字做 NFC，長度不變才換，
    免得 ㈱ → (株) 這種一對多的字把位置對應搞亂。
    """
    out = []
    for ch in text:
        one = unicodedata.normalize("NFC", ch)
        out.append(one if len(one) == 1 else ch)
    return "".join(out)


# ---------------------------------------------------------------- 規則

# 字串優先比對，// 或 /* */ 長在字串裡面才不會被當成註解砍掉
COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)
DANGLING = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])', re.S)


def jsonc(text: str):
    """吃得下註解與結尾多餘逗號的 json.loads。

    註解換成等長的空白（換行留著），json 報的行號、欄號才會指到原檔的位置。
    """
    def blank(m: re.Match) -> str:
        s = m.group(0)
        return s if s[0] == '"' else re.sub(r"[^\n]", " ", s)

    def comma(m: re.Match) -> str:
        s = m.group(0)
        return s if s[0] == '"' else " "

    return json.loads(DANGLING.sub(comma, COMMENT.sub(blank, text)))


def rules_file() -> tuple[Path | None, list[str]]:
    found = [p for p in RULES_PATHS if p.is_file()]
    if not found:
        return None, [f"找不到規則檔 {RULES_PATHS[0].name}"]
    if len(found) > 1:
        return found[0], [f"{found[0].name} 和 {found[1].name} 都在，"
                          f"只會讀 {found[0].name}"]
    return found[0], []


def read_slot(raw: dict, where: str, problems: list[str]) -> dict | None:
    """把一個 slot 正規化成引擎用的樣子；有問題就記下來並丟掉。"""
    value = str(raw.get("value", ""))
    if value not in VALUE_KINDS:
        problems.append(f"{where}：value「{value}」不認得")
        return None
    if value == "text" and not str(raw.get("text", "")):
        problems.append(f"{where}：value 是 text 但沒有給 text")
        return None

    before, after = raw.get("before"), raw.get("after")
    if before and after:
        problems.append(f"{where}：before 和 after 只能給一個")
        return None

    mode = "before" if before else "after"
    anchor = norm(str(before or after or ""))[:1]

    align = str(raw.get("align", "")) or ("right" if mode == "before" else "left")
    if align not in ALIGNS:
        problems.append(f"{where}：align「{align}」不認得")
        return None

    digits = raw.get("digits")
    try:
        digits = max(1, int(digits)) if digits is not None else None
    except (TypeError, ValueError):
        problems.append(f"{where}：digits 不是數字")
        digits = None

    return {"mode": mode, "anchor": anchor, "align": align, "value": value,
            "text": str(raw.get("text", "")), "digits": digits,
            "optional": bool(raw.get("optional"))}


def load_rules() -> tuple[list[dict], list[str]]:
    """每次掃描都重讀。讀不到或寫錯就回報，不要偷偷退回別的規則。"""
    path, problems = rules_file()
    if path is None:
        return [], problems

    try:
        data = jsonc(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [], problems + [f"{path.name} 讀不動：{e}"]

    if not isinstance(data, dict):
        return [], problems + [f"{path.name} 最外層要是物件，裡面放 rules"]

    rules: list[dict] = []
    listed = data.get("rules") or []

    for i, raw in enumerate(listed):
        where = f"第 {i + 1} 條規則"
        if not isinstance(raw, dict):
            problems.append(f"{where}：不是物件")
            continue
        labels = raw.get("label") or raw.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        labels = [norm(str(s).strip()) for s in labels if str(s).strip()]
        if not labels:
            problems.append(f"{where}：沒有 label")
            continue

        slots = []
        for n, item in enumerate(raw.get("slots", []) or []):
            slot = read_slot(item, f"{where} 第 {n + 1} 個欄位", problems)
            if slot:
                slots.append(slot)
        if not slots:
            problems.append(f"{where}（{labels[0]}）：沒有可用的欄位，整條跳過")
            continue

        rules.append({
            "id": str(raw.get("id") or f"rule{i + 1}"),
            "name": str(raw.get("name") or labels[0]),
            "labels": labels,
            "slots": slots,
        })

    if not listed:
        problems.append(f"{path.name} 裡沒有任何規則")
    return rules, problems


# ---------------------------------------------------------------- 版面


def line_chars(line: dict) -> list[tuple]:
    """把一行攤平成 (字, bbox, origin, 字級)。字級要沿用原文，填進去才不突兀。"""
    out = []
    for span in line.get("spans", []):
        size = float(span.get("size", 12.0))
        for c in span.get("chars", []):
            out.append((norm(c["c"]), c["bbox"], c["origin"], size))
    return out


def prev_edge(chars: list[tuple], i: int) -> float:
    """往左找最近的非空白字的右緣——那裡才是這段空白真正的起點。"""
    for j in range(i - 1, -1, -1):
        if not chars[j][0].isspace():
            return chars[j][1][2]
    return chars[0][1][0]


def next_edge(chars: list[tuple], i: int) -> float | None:
    """往右找最近的非空白字的左緣，沒有就回 None（空白延伸到這一列結束）。"""
    for j in range(i + 1, len(chars)):
        if not chars[j][0].isspace():
            return chars[j][1][0]
    return None


def page_rows(page) -> list[list[tuple]]:
    """把整頁的字依基線分群。

    PDF 不保證「視覺上的一列」就是文字流裡的一行——Word 匯出的表格常把同一列
    切成好幾段 text object。改用基線分群，標籤和後面的單位字才不會被拆散。
    """
    rows: list[dict] = []
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            chars = line_chars(line)
            if not chars:
                continue
            ys = sorted(c[2][1] for c in chars)
            y = ys[len(ys) // 2]
            tol = max(1.0, 0.35 * max(c[3] for c in chars))
            for row in rows:
                if abs(row["y"] - y) <= tol:
                    row["chars"].extend(chars)
                    break
            else:
                rows.append({"y": y, "chars": list(chars)})

    for row in rows:
        row["chars"].sort(key=lambda c: c[1][0])
    return [row["chars"] for row in rows]


# ---------------------------------------------------------------- 比對


def place(chars: list[tuple], ci: int, slot: dict, left: float, right: float,
          w: float, h: float):
    """把一段空白換算成一個可以下筆的位置。"""
    size = chars[ci][3]
    blank = max(0.0, right - left)
    pad = min(size * 0.25, blank * 0.25)

    if slot["align"] == "right":
        x = right - pad
    elif slot["align"] == "left":
        x = left + pad
    else:
        x = (left + right) / 2

    return {
        "kind": slot["value"],
        "text": slot["text"],
        "digits": slot["digits"],
        "align": slot["align"],
        "x": x / w,
        "y": chars[ci][2][1] / h,   # origin 的 y 就是基線，與 PDF 同義
        "size": round(size, 2),
        "gap": round(max(0.0, blank - pad), 2),
    }


def match(rule: dict, chars: list[tuple], keep: list[int], compact: str,
          start: int, w: float, h: float) -> tuple[dict | None, int]:
    """從 start 開始找一次命中。回傳 (命中, 下次要從哪裡找)。"""
    at, label = -1, ""
    for one in rule["labels"]:
        pos = compact.find(one, start)
        if pos >= 0 and (at < 0 or pos < at):
            at, label = pos, one
    if at < 0:
        return None, -1

    cursor = at + len(label)
    anchor_ci = keep[cursor - 1]
    slots, filled = [], False
    boxes = [chars[keep[j]][1] for j in range(at, cursor)]

    for slot in rule["slots"]:
        explicit = bool(slot["anchor"])
        if explicit:
            pos = compact.find(slot["anchor"], cursor)
            if pos < 0:
                if slot["optional"]:
                    continue
                return None, cursor        # 少了必要的錨點，這條規則不算命中
            # 空格裡已經有數字，代表這一列先前就填過了
            if any(ch.isdigit() for ch in compact[cursor:pos]):
                filled = True
            ci = keep[pos]
            cursor = pos + 1
        else:
            ci = anchor_ci                 # 沒指定錨點就接在上一個後面

        size = chars[ci][3]
        if slot["mode"] == "before":
            left, right = prev_edge(chars, ci), chars[ci][1][0]
            if right - left > size * MAX_BLANK:
                if slot["optional"]:
                    continue
                return None, cursor        # 空白長成這樣，多半是撿到別的格子
        else:
            if not explicit:
                # 標籤後面的冒號是裝飾，跨過去才是要填的地方
                while cursor < len(compact) and compact[cursor] in LEAD:
                    ci = keep[cursor]
                    cursor += 1
            left = chars[ci][1][2]
            ruled = None
            while cursor < len(compact) and compact[cursor] in RULED:
                ruled = keep[cursor]
                cursor += 1
            if ruled is not None:
                right = chars[ruled][1][2]       # 寫在底線上面
            else:
                right = next_edge(chars, ci)
                if right is None:                # 後面沒東西了，給一段合理的空間
                    right = left + size * 12

        slots.append(place(chars, ci, slot, left, right, w, h))
        boxes.append((left, chars[ci][1][1], right, chars[ci][1][3]))
        anchor_ci = ci

    if not slots:
        return None, cursor

    pad = 2.0
    hit = {
        "rule": rule["id"],
        "label": rule["name"],
        "filled": filled,
        "rect": [
            max(0.0, (min(b[0] for b in boxes) - pad) / w),
            max(0.0, (min(b[1] for b in boxes) - pad) / h),
            min(1.0, (max(b[2] for b in boxes) + pad) / w),
            min(1.0, (max(b[3] for b in boxes) + pad) / h),
        ],
        "slots": slots,
    }
    return hit, cursor


def scan_page(page, rules: list[dict]) -> tuple[list[dict], bool]:
    w, h = page.rect.width, page.rect.height
    if not w or not h:
        return [], False

    hits, has_text = [], False
    for chars in page_rows(page):
        has_text = True
        keep = [i for i, c in enumerate(chars) if not c[0].isspace()]
        if not keep:
            continue
        compact = "".join(chars[i][0] for i in keep)
        for rule in rules:
            start = 0
            # 同一列可能出現兩次同樣的欄位，往後繼續找
            while 0 <= start < len(compact):
                hit, start = match(rule, chars, keep, compact, start, w, h)
                if hit:
                    hits.append(hit)
    return hits, has_text


# ---------------------------------------------------------------- API


@router.get("/scan/{doc_id}")
def scan(doc_id: str):
    if CTX is None:
        raise HTTPException(500, "外掛沒有初始化")

    rules, problems = load_rules()
    doc = pymupdf.open(CTX.doc_path(doc_id))
    hits, has_text = [], False
    try:
        for i, page in enumerate(doc):
            found, text_here = scan_page(page, rules)
            has_text = has_text or text_here
            for n, hit in enumerate(found):
                hit["id"] = f"{i}-{hit['rule']}-{n}"
                hit["page"] = i
                hits.append(hit)
    finally:
        doc.close()

    if problems:
        print("formfill 規則問題：" + "；".join(problems))

    return {"hits": hits, "hasText": has_text, "problems": problems,
            "rules": [r["name"] for r in rules]}


# ---------------------------------------------------------------- 前端

# 介面文字用得到的字（核心只把自己的字包進 webfont 預設子集）
SEED_CHARS = ("填表模式全部入找到處已無法偵測這份沒有文字層掃描檔可的欄位原本就過了"
              "再點一下取消現時間連不上服務失敗中規則有問題等條見紀錄")

CLIENT_JS = r"""
(() => {
// 時間取瀏覽器本機時間：容器常常是 UTC，時 / 分會整整差八小時。
function render(s, d) {
  const p = (n, w) => String(n).padStart(w, '0');
  const roc = d.getFullYear() - 1911;
  switch (s.kind) {
    case 'text':      return s.text || '';
    case 'roc_year':  return p(roc, s.digits || 1);
    case 'year':      return p(d.getFullYear(), s.digits || 1);
    case 'month':     return p(d.getMonth() + 1, s.digits || 2);
    case 'day':       return p(d.getDate(), s.digits || 2);
    case 'hour':      return p(d.getHours(), s.digits || 2);
    case 'minute':    return p(d.getMinutes(), s.digits || 2);
    case 'second':    return p(d.getSeconds(), s.digits || 2);
    case 'roc_date':  return '民國' + roc + '年' + (d.getMonth() + 1) + '月' +
                             d.getDate() + '日';
    case 'roc_short': return roc + '.' + p(d.getMonth() + 1, 2) + '.' +
                             p(d.getDate(), 2);
    default:          return '';
  }
}

const FF = { on: false, scanned: false, hits: [] };

const style = document.createElement('style');
style.textContent = `
.ff-bar { padding:.7rem 1.25rem; border-bottom:1px solid var(--line);
          display:flex; align-items:center; gap:.7rem; flex-wrap:wrap; }
.ff-sw { display:flex; align-items:center; gap:.4rem; cursor:pointer;
         font-size:.9rem; user-select:none; flex-shrink:0; }
.ff-bar button { padding:.25rem .6rem; font-size:.82rem; flex-shrink:0; }
.ff-info { margin-left:auto; font-size:.78rem; color:var(--ink-faint);
           text-align:right; min-width:0; }
/* 規則出錯的訊息很長，讓它自己佔一行，不要把開關擠扁 */
.ff-info.bad { color:var(--seal); flex:1 0 100%; margin-left:0; text-align:left;
               line-height:1.4; }
.ff-hit { position:absolute; z-index:5; cursor:pointer; border-radius:2px;
          background:var(--seal-wash); outline:1px solid var(--seal);
          outline-offset:1px; transition:background .12s; }
.ff-hit:hover { background:rgba(176,58,46,.2); }
.ff-hit.done { background:rgba(31,35,40,.06); outline-color:var(--ink-faint); }
.ff-hit.pre { background:transparent; outline:1px dashed var(--ink-faint);
              cursor:not-allowed; }
.ff-gauge { position:absolute; width:0; height:0; overflow:hidden; }
/* 填表模式下不能自己蓋章：常用戳章整排壓灰（inert 負責擋掉點擊與鍵盤），
   游標也從十字改回箭頭，免得看起來還能點 */
body.ff-on .presets { opacity:.4; }
body.ff-on .sheet { cursor:default; }
/* 核心的空狀態寫著「點一下就會放上一個簽章」，在這個模式下剛好相反 */
body.ff-on .empty { display:none; }
.ff-note { padding:.6rem 1.25rem; border-bottom:1px solid var(--line);
           background:var(--seal-wash); color:var(--seal);
           font-size:.82rem; line-height:1.5; }
.ff-note b { font-weight:600; }
`;
document.head.appendChild(style);

const bar = document.createElement('div');
bar.className = 'ff-bar';
bar.innerHTML =
  '<label class="ff-sw"><input type="checkbox" id="ffOn"><span>填表模式</span></label>' +
  '<button id="ffAll" disabled>全部填入</button>' +
  '<span class="ff-info" id="ffInfo"></span>';
const presets = document.querySelector('.presets');
presets.insertAdjacentElement('afterend', bar);

// 點了沒反應是最難猜的，所以把「現在不能蓋章」直接寫出來
const note = document.createElement('div');
note.className = 'ff-note';
note.hidden = true;
note.innerHTML = '<b>填表模式已啟用</b>：只能點頁面上框出來的欄位。<br>' +
                 '「簽名」與「文字」蓋章已停用。';
bar.insertAdjacentElement('afterend', note);

// inert 會連鍵盤與輔助技術一起擋掉，而且掛在容器上，
// 核心重畫 chips（renderChips）也不會把它弄丟
function mode() {
  note.hidden = !FF.on;
  presets.inert = FF.on;
  document.body.classList.toggle('ff-on', FF.on);
}

// 量字寬用的隱形 SVG：楷體已經在瀏覽器裡，量得到就不會讓數字撐爆空格
const gauge = document.createElementNS(SVGNS, 'svg');
gauge.setAttribute('class', 'ff-gauge');
const gtext = document.createElementNS(SVGNS, 'text');
gtext.style.fontFamily = 'TWKai, serif';
gauge.appendChild(gtext);
document.body.appendChild(gauge);

function fit(text, size, gap) {
  if (!(gap > 0)) return size;
  gtext.setAttribute('font-size', size);
  gtext.textContent = text;
  let w = 0;
  try { w = gtext.getComputedTextLength(); } catch { return size; }
  if (!w || w <= gap * 0.92) return size;
  return Math.max(6, Math.round(size * gap * 0.92 / w * 10) / 10);
}

function info(msg, bad) {
  const el = $('#ffInfo');
  el.textContent = msg;
  el.classList.toggle('bad', !!bad);
}

function paint() {
  document.querySelectorAll('.ff-hit').forEach(n => n.remove());
  if (!FF.on) return;
  FF.hits.forEach(h => {
    const sheet = document.querySelector(`.sheet[data-page="${h.page}"]`);
    if (!sheet) return;
    const d = document.createElement('div');
    d.className = 'ff-hit' + (h.filled ? ' pre' : h.done ? ' done' : '');
    d.style.left = h.rect[0] * 100 + '%';
    d.style.top = h.rect[1] * 100 + '%';
    d.style.width = (h.rect[2] - h.rect[0]) * 100 + '%';
    d.style.height = (h.rect[3] - h.rect[1]) * 100 + '%';
    d.title = h.filled ? h.label + '（原本就填過了）'
            : h.done ? h.label + '（再點一下取消）'
            : h.label + '：點一下填入';
    d.onclick = e => { e.stopPropagation(); toggle(h); };
    sheet.appendChild(d);
  });
}

function fill(h, when) {
  h.slots.forEach(s => {
    const text = render(s, when);
    if (!text) return;
    state.marks.push({
      page: h.page, x: s.x, y: s.y,
      kind: 'text', name: text, dateText: '',
      nameSize: fit(text, s.size, s.gap), dateSize: 12, gap: 6,
      align: s.align, layout: 'stack',
      ffHit: h.id,        // 後端的 Placement 會忽略這個多出來的欄位
    });
  });
  h.done = true;
}

function clear(h) {
  state.marks = state.marks.filter(m => m.ffHit !== h.id);
  h.done = false;
  state.sel = -1;                 // 刪掉之後索引全亂了
  $('#editor').hidden = true;
}

function commit() {
  refreshFont(); drawMarks(); renderList(); paint(); count();
}

function toggle(h) {
  if (h.filled) return toast('這一列原本就填過了');
  if (h.done) clear(h); else fill(h, new Date());
  commit();
}

function count() {
  const open = FF.hits.filter(h => !h.filled && !h.done).length;
  $('#ffAll').disabled = !open;
  if (!FF.hits.length) return;
  const done = FF.hits.filter(h => h.done).length;
  info('找到 ' + FF.hits.length + ' 處' + (done ? '・已填 ' + done + ' 處' : ''));
}

async function scan() {
  info('偵測中…');
  let r;
  try { r = await fetch('/api/formfill/scan/' + state.doc); }
  catch { info(''); return toast('連不上服務'); }
  if (!r.ok) { info(''); return toast('偵測失敗'); }

  const data = await r.json();
  FF.hits = data.hits;
  FF.scanned = true;

  // 規則有問題就直說，不要讓人對著「找不到欄位」猜半天
  if (data.problems && data.problems.length) {
    const more = data.problems.length > 1 ? '（等 ' + data.problems.length + ' 項）' : '';
    info('規則：' + data.problems[0] + more, true);
    console.warn('formfill 規則問題', data.problems);
    if (!FF.hits.length) return;
  } else if (!data.hasText) {
    return info('這份 PDF 沒有文字層，無法偵測', true);
  } else if (!FF.hits.length) {
    return info('找不到可填的欄位');
  }
  if (FF.hits.length) count();
}

$('#ffOn').onchange = async e => {
  FF.on = e.target.checked;
  mode();
  if (FF.on && !FF.scanned) await scan();
  else if (!FF.on) info('');
  paint();
  if (FF.on && FF.hits.length) count();
};

$('#ffAll').onclick = () => {
  const when = new Date();
  let n = 0;
  FF.hits.forEach(h => { if (!h.filled && !h.done) { fill(h, when); n++; } });
  commit();
  toast(n ? '已填入 ' + n + ' 處' : '沒有可填的欄位');
};

// 填表模式下不要順手蓋章
document.addEventListener('pdfsign:place', e => {
  if (!FF.on) return;
  e.preventDefault();
  toast('填表模式下不能自己蓋章，請點框出來的欄位，或關掉填表模式');
});

document.addEventListener('pdfsign:loaded', () => {
  FF.on = false; FF.scanned = false; FF.hits = [];
  $('#ffOn').checked = false;
  $('#ffAll').disabled = true;
  mode();
  info('');
});
})();
"""
