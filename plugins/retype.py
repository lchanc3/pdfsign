#!/usr/bin/env python3
"""
修正原文外掛 — 改寫 PDF 上原本印好的字，盡量沿用原本的字型。

PDF 沒辦法原地改字，做法是：
  1. 用 redaction 只拿掉那段字（框線、底色、圖片都不動）
  2. 在同一條基線上用新的字重寫

重寫時直接引用頁面上既有的字型物件，用原本的字碼寫，字型一個位元組都不動，
所以字形、字寬跟原文一模一樣。代價是嵌入的字型通常只是子集，只有文件裡用過的字；
原字型沒有的字才退回替代字型，介面上會逐字標出來。替代字型依序找：

  系統裡同名的字型 → 同風格的中文字型（楷／明／黑）→ 核心的楷體 → PDF 內建字型

要讓某份文件的缺字也一模一樣，把同名的 .ttf 放進 plugins/fonts/ 就會優先使用。

實測過的眉角
------------
- Word 會把同一套字拆成兩個字型物件：中文走 Type0（Identity-H），英數走 WinAnsi
  的 TrueType，而且可能有好幾套同名的子集。同名的字形一定一樣，所以合起來查字。
- 有些產生器會把子集裡的 cmap 拿掉，從字型本身查不回字；
  改用 PDF 的 ToUnicode 反查字碼就不受影響。
- Word 的假粗體是同一串字畫兩次：填色一次、描邊一次（texttrace 的 type 0 / 1），
  重寫也要照做，不然會變細。
- 標楷體嵌進 PDF 後，抽出來的字常是相容字（U+F962 而不是 U+7570），
  查字碼時兩種都要認。
- 字型資源要在 redaction 之後才掛上頁面，redaction 會把「沒用到」的資源清掉。
"""

import base64
import os
import re
import statistics
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pymupdf
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/retype")

CTX = None

PREVIEW_ZOOM = 3.0
MAX_TEXT = 500
ALIGNS = {"left", "center", "right"}

# 使用者自己放的字型優先，其次是系統字型
FONT_DIRS = [Path(__file__).with_name("fonts"),
             Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
if os.name == "nt":   # 開發機
    FONT_DIRS.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")

# 同風格的中文字型，依序找第一個裝了的
STYLE_FONTS = {
    "kai": ["DFKaiShu-SB-Estd-BF", "標楷體", "TW-Kai", "全字庫正楷體", "AR PL UKai TW"],
    "ming": ["PMingLiU", "新細明體", "MingLiU", "細明體", "TW-Sung", "全字庫正宋體",
             "AR PL UMing TW"],
    "hei": ["Microsoft JhengHei", "微軟正黑體", "Noto Sans TC", "WenQuanYi Zen Hei"],
}

BASE14 = {
    "Helvetica": "helv", "Helvetica-Bold": "hebo", "Helvetica-Oblique": "heit",
    "Helvetica-BoldOblique": "hebi", "Times-Roman": "tiro", "Times-Bold": "tibo",
    "Times-Italic": "tiit", "Times-BoldItalic": "tibi", "Courier": "cour",
    "Courier-Bold": "cobo", "Courier-Oblique": "coit", "Courier-BoldOblique": "cobi",
}


def setup(ctx) -> None:
    global CTX
    CTX = ctx
    ctx.before_sign.append(before_sign)


# ---------------------------------------------------------------- 名稱與字元

SUBSET = re.compile(r"^[A-Z]{6}\+")


def family_of(base: str) -> str:
    return SUBSET.sub("", base or "")


def readable(name: str) -> str:
    """字型名稱給人看的樣子。中文字型名常是 Big5 位元組被當成 latin-1（¼Ð·¢Åé = 標楷體）"""
    try:
        raw = name.encode("latin-1")
    except UnicodeEncodeError:
        return name
    if raw.isascii():
        return name
    for enc in ("utf-8", "big5", "gbk", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return name


def squash(name: str) -> str:
    return re.sub(r"[\s\-_,]", "", name).lower()


def nfc(ch: str) -> str:
    """相容字（U+F900 區）轉回常用碼位，一對多的不動"""
    one = unicodedata.normalize("NFC", ch)
    return one if len(one) == 1 else ch


def is_cjk(ch: str) -> bool:
    return ord(ch) >= 0x2E80


# ---------------------------------------------------------------- PDF 物件


def value(doc, xref: int, key: str) -> str:
    """字典裡的值；間接參照就順著拿到物件本身的原始碼"""
    t, v = doc.xref_get_key(xref, key)
    if t == "xref":
        return doc.xref_object(int(v.split()[0]))
    return "" if t == "null" else v


def ref(doc, xref: int, key: str) -> int | None:
    t, v = doc.xref_get_key(xref, key)
    m = re.search(r"(\d+) 0 R", v) if t in ("xref", "array") else None
    return int(m.group(1)) if m else None


def numbers(src: str) -> list[float]:
    return [float(x) for x in re.findall(r"-?\d*\.?\d+", src or "")]


def to_unicode(doc, xref: int) -> dict[int, str]:
    """ToUnicode CMap → {字碼: 字}"""
    t, v = doc.xref_get_key(xref, "ToUnicode")
    if t != "xref":
        return {}
    try:
        data = doc.xref_stream(int(v.split()[0])).decode("latin-1")
    except Exception:
        return {}

    def text(h: str) -> str:
        return bytes.fromhex(h).decode("utf-16-be", "replace")

    out: dict[int, str] = {}
    for sect in re.findall(r"beginbfchar(.*?)endbfchar", data, re.S):
        for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", sect):
            out[int(a, 16)] = text(b)
    for sect in re.findall(r"beginbfrange(.*?)endbfrange", data, re.S):
        for a, b, rest in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", sect):
            lo, hi = int(a, 16), int(b, 16)
            if rest.startswith("["):
                for i, h in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", rest)):
                    out[lo + i] = text(h)
            elif hi - lo < 0x10000:
                start = bytearray(bytes.fromhex(rest[1:-1]) or b"\0\0")
                for i in range(hi - lo + 1):
                    cur = bytearray(start)
                    cur[-1] = (cur[-1] + i) & 0xFF
                    out[lo + i] = bytes(cur).decode("utf-16-be", "replace")
    return out


def parse_w(src: str) -> tuple[dict[int, float], list[tuple[int, int, float]]]:
    """CID 字型的 /W：[c [w1 w2 …]  c1 c2 w …] → 個別字寬與區段字寬"""
    toks = re.findall(r"\[|\]|-?\d*\.?\d+", src or "")
    one: dict[int, float] = {}
    spans: list[tuple[int, int, float]] = []
    if not toks or toks[0] != "[":
        return one, spans
    i = 1
    try:
        while i < len(toks) and toks[i] != "]":
            first = int(float(toks[i]))
            i += 1
            if toks[i] == "[":
                i += 1
                c = first
                while toks[i] != "]":
                    one[c] = float(toks[i])
                    c += 1
                    i += 1
                i += 1
            else:
                spans.append((first, int(float(toks[i])), float(toks[i + 1])))
                i += 2
    except (IndexError, ValueError):
        pass
    return one, spans


# ---------------------------------------------------------------- 原字型


class Face:
    """文件裡一個既有的字型物件：寫得出哪些字、字碼、字寬"""

    def __init__(self, doc, xref: int, base: str):
        self.xref = xref
        self.family = family_of(base)
        # texttrace 報的是子字型（CIDFont）的 BaseFont，不一定跟外層 Type0 同名：
        # PyMuPDF 嵌的字外層叫「DFKai-SB Regular」、裡面叫「DFKaiShu-SB-Estd-BF」
        self.names = {self.family}
        desc = ref(doc, xref, "DescendantFonts")
        if desc:
            t, v = doc.xref_get_key(desc, "BaseFont")
            if t == "name":
                raw = v.lstrip("/").encode("utf-8", "surrogateescape").decode("latin-1")
                raw = re.sub(r"#([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), raw)
                self.names.add(family_of(raw))
        self.code: dict[str, int] = {}
        self.widths: dict[int, float] = {}
        self.spans: list[tuple[int, int, float]] = []
        self.default = 0.0
        self.two_byte = False
        sub = doc.xref_get_key(xref, "Subtype")[1].lstrip("/")
        try:
            self.buffer = doc.extract_font(xref)[3] or b""
        except Exception:
            self.buffer = b""
        try:
            if sub == "Type0":
                self._cid(doc)
            elif sub in ("TrueType", "Type1", "MMType1"):
                self._simple(doc, base)
        except Exception as e:   # 看不懂的字型就當作寫不出任何字，改走替代字型
            print(f"retype：字型 {xref} {base} 無法解析：{e}")
            self.code.clear()
        self.buffer = b""        # 只在判斷字符時用得到，不要一直抱著

    def _add(self, u: str, code: int) -> None:
        if len(u) != 1 or not u.isprintable():
            return
        self.code.setdefault(u, code)
        self.code.setdefault(nfc(u), code)

    def _cid(self, doc) -> None:
        # 只處理 Identity-H：字碼就是 CID，不用另外解 CMap
        if not self.buffer or doc.xref_get_key(self.xref, "Encoding")[1] != "/Identity-H":
            return
        self.two_byte = True
        for code, u in to_unicode(doc, self.xref).items():
            self._add(u, code)
        desc = ref(doc, self.xref, "DescendantFonts")
        self.default = 1000.0
        if desc:
            dw = numbers(value(doc, desc, "DW"))
            if dw:
                self.default = dw[0]
            self.widths, self.spans = parse_w(value(doc, desc, "W"))

    def _simple(self, doc, base: str) -> None:
        t, enc = doc.xref_get_key(self.xref, "Encoding")
        tu = to_unicode(doc, self.xref)
        first = numbers(value(doc, self.xref, "FirstChar"))
        widths = numbers(value(doc, self.xref, "Widths"))
        name = family_of(base)

        if widths:
            start = int(first[0]) if first else 0
            self.widths = {start + i: w for i, w in enumerate(widths)}
            codes = [c for c, w in self.widths.items() if w > 0]
        elif not self.buffer and name in BASE14:
            # 沒嵌入、也沒給字寬的標準字型，字寬照 Base14 的
            metric = pymupdf.Font(BASE14[name])
            codes = list(range(32, 256))
            for c in codes:
                try:
                    self.widths[c] = metric.glyph_advance(ord(bytes([c]).decode("cp1252"))) * 1000
                except UnicodeDecodeError:
                    pass
        else:
            return

        winansi = enc == "/WinAnsiEncoding"
        font = None
        if self.buffer:
            try:
                font = pymupdf.Font(fontbuffer=self.buffer)
            except Exception:
                font = None

        for c in codes:
            u = tu.get(c)
            if u is None and (winansi or (t == "null" and 32 <= c < 127)):
                try:
                    u = bytes([c]).decode("cp1252")
                except UnicodeDecodeError:
                    u = None
            if not u:
                continue
            # 子集常把字寬整排留著、字符卻只留用到的，要確認字符真的在
            if font is not None and not (font.has_glyph(ord(u[0])) or font.has_glyph(c)
                                         or font.has_glyph(0xF000 + c)):
                continue
            self._add(u, c)

    def has(self, ch: str) -> bool:
        return ch in self.code

    def advance(self, ch: str) -> float:
        """字寬，單位 em"""
        code = self.code[ch]
        w = self.widths.get(code)
        if w is None:
            w = next((sw for lo, hi, sw in self.spans if lo <= code <= hi), self.default)
        return w / 1000

    def hex(self, ch: str) -> str:
        return f"{self.code[ch]:04X}" if self.two_byte else f"{self.code[ch]:02X}"


# ---------------------------------------------------------------- 替代字型


class Substitute:
    """替代字型：系統字型檔（可能在 .ttc 裡），或 PDF 內建的 Base14"""

    def __init__(self, label: str, *, base14: str = "", path: str = "", index: int = 0,
                 font=None):
        self.label = label
        self.base14 = base14
        self.path = path
        self.index = index
        self._font = font
        self._buffer = None

    def buffer(self) -> bytes:
        if self._buffer is None:
            from fontTools.ttLib import TTCollection   # 只有 .ttc 才會走到這裡
            import io
            out = io.BytesIO()
            TTCollection(self.path).fonts[self.index].save(out)
            self._buffer = out.getvalue()
        return self._buffer

    @property
    def collection(self) -> bool:
        return self.path.lower().endswith(".ttc")

    @property
    def font(self):
        if self._font is None:
            if self.base14:
                self._font = pymupdf.Font(self.base14)
            elif self.collection:
                self._font = pymupdf.Font(fontbuffer=self.buffer())
            else:
                self._font = pymupdf.Font(fontfile=self.path)
        return self._font

    def has(self, ch: str) -> bool:
        if self.base14:
            try:
                ch.encode("cp1252")
            except UnicodeEncodeError:
                return False
        try:
            return self.font.has_glyph(ord(ch)) > 0
        except Exception:
            return False

    def advance(self, ch: str) -> float:
        return self.font.glyph_advance(ord(ch))

    def hex(self, ch: str) -> str:
        if self.base14:
            return f"{ch.encode('cp1252')[0]:02X}"
        return f"{self.font.has_glyph(ord(ch)):04X}"   # 嵌入後是 Identity-H，字碼就是字符編號

    def embed(self, page, name: str) -> int:
        if self.base14:
            return page.insert_font(fontname=self.base14)
        if self.collection:
            return page.insert_font(fontname=name, fontbuffer=self.buffer())
        return page.insert_font(fontname=name, fontfile=self.path)


@lru_cache(maxsize=1)
def installed() -> dict[str, tuple[str, int, str]]:
    """裝在系統裡的字型：各種名稱 → (檔案, .ttc 裡第幾個, 顯示名)

    只收 TrueType 外框（有 glyf 表）的字型：CFF 的 CID 字型嵌進 PDF 之後
    字碼不等於字符編號，寫出來會錯字。
    """
    try:
        from fontTools.ttLib import TTCollection, TTFont
    except ImportError:
        return {}

    out: dict[str, tuple[str, int, str]] = {}
    for root in FONT_DIRS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            ext = path.suffix.lower()
            if ext not in (".ttf", ".ttc"):
                continue
            try:
                faces = (TTCollection(str(path), lazy=True).fonts if ext == ".ttc"
                         else [TTFont(str(path), lazy=True)])
            except Exception:
                continue
            for i, face in enumerate(faces):
                try:
                    if "glyf" not in face:
                        continue
                    recs = face["name"].names
                except Exception:
                    continue
                names, label = [], ""
                for rec in recs:
                    if rec.nameID not in (1, 4, 6):
                        continue
                    try:
                        s = rec.toUnicode()
                    except Exception:
                        continue
                    names.append(s)
                    # 顯示名優先用中文的完整名稱
                    if rec.nameID == 4 and (not label or rec.langID == 0x404):
                        label = s
                for s in names:
                    out.setdefault(squash(s), (str(path), i, label or s))
    return out


def find_installed(name: str) -> Substitute | None:
    fonts = installed()
    tries = [name, re.sub(r"(MT|PS)$", "", name), name.replace(",", "-")]
    for t in tries:
        hit = fonts.get(squash(t))
        if hit:
            return Substitute(hit[2], path=hit[0], index=hit[1])
    return None


def style_of(name: str) -> str:
    n = name.lower()
    if re.search(r"kai|楷", n):
        return "kai"
    if re.search(r"ming|sung|song|宋|明|mincho", n):
        return "ming"
    if re.search(r"hei|黑|gothic|jheng|yahei", n):
        return "hei"
    return ""


def base14_for(name: str, bold: bool, italic: bool) -> str:
    n = name.lower()
    if re.search(r"courier|mono|consol", n):
        kind = "co"
    elif re.search(r"times|roman|serif|georgia|garamond|cambria|book|minion|palatino|"
                   r"century|明|宋|ming|song|sung|kai|楷", n):
        kind = "ti"
    else:
        kind = "he"
    names = {"co": ("cour", "cobo", "coit", "cobi"),
             "ti": ("tiro", "tibo", "tiit", "tibi"),
             "he": ("helv", "hebo", "heit", "hebi")}[kind]
    return names[(1 if bold else 0) + (2 if italic else 0)]


def core_substitute() -> Substitute | None:
    if CTX is None:
        return None
    return Substitute(readable(CTX.font.name), path=CTX.font_path, font=CTX.font)


# ---------------------------------------------------------------- 分析


def census(page) -> Counter:
    """頁面上每個看得見的字（含描邊那一次），用來確認 redaction 拿掉的剛好是要的"""
    out: Counter = Counter()
    for s in page.get_texttrace():
        if s["type"] not in (0, 1):
            continue
        for c in s["chars"]:
            if not chr(c[0]).isspace():
                out[(c[0], round(c[2][0], 1), round(c[2][1], 1))] += 1
    return out


def split(chars: list, size: float) -> list[list]:
    """一個 span 可能橫跨好幾個表格格子，中間空很大就拆開；頭尾的空白不算"""
    pieces, cur = [], []
    for c in chars:
        if cur:
            prev = cur[-1]
            gap = c[3][0] - prev[3][2]
            if gap > size * 1.2 or abs(c[2][1] - prev[2][1]) > size * 0.3:
                pieces.append(cur)
                cur = []
        cur.append(c)
    if cur:
        pieces.append(cur)

    out = []
    for p in pieces:
        while p and chr(p[0][0]).isspace():
            p = p[1:]
        while p and chr(p[-1][0]).isspace():
            p = p[:-1]
        if p:
            out.append(p)
    return out


class Analysis:
    """一份文件的字型與可以改的字段。只存資料，不抱著 doc"""

    def __init__(self, doc):
        self.faces: dict[str, list[Face]] = {}
        seen: set[int] = set()
        for page in doc:
            for f in page.get_fonts(full=True):
                if f[0] in seen:
                    continue
                seen.add(f[0])
                face = Face(doc, f[0], f[3])
                for name in face.names:
                    self.faces.setdefault(name, []).append(face)
        # 同一套字裡，先用中文那個物件、再用英數那個
        for faces in self.faces.values():
            faces.sort(key=lambda f: (not f.two_byte, f.xref))

        self.pages: list[list[dict]] = []
        self.sizes: list[tuple[float, float]] = []
        seen_w: dict[str, dict[str, list[float]]] = {}
        for pno, page in enumerate(doc):
            self.sizes.append((page.rect.width, page.rect.height))
            self.pages.append(self._runs(page, pno, seen_w))
        # 每套字裡每個字實際畫出來的寬度（em）。排版優先用它：從字型表查的字寬
        # 只要解錯一次（例如半形數字查成全形），接縫就會多出或少掉一段空白
        self.adv: dict[str, dict[str, float]] = {
            fam: {ch: statistics.median(ws) for ch, ws in chars.items()}
            for fam, chars in seen_w.items()}
        self._subs: dict[tuple, list[Substitute]] = {}

    def _runs(self, page, pno: int, seen_w: dict) -> list[dict]:
        rows: dict[tuple, dict] = {}
        for s in page.get_texttrace():
            # 0 填色、1 描邊；3 是隱形文字（掃描檔的 OCR 層），看不到就不給改
            if s["type"] not in (0, 1) or not s["chars"]:
                continue
            if s["type"] == 0 and s["dir"] == (1.0, 0.0) and s["size"] > 0:
                fam = seen_w.setdefault(s["font"], {})
                for c in s["chars"]:
                    fam.setdefault(nfc(chr(c[0])), []).append((c[3][2] - c[3][0]) / s["size"])
            text = "".join(chr(c[0]) for c in s["chars"])
            key = (s["font"], round(s["size"], 2), tuple(round(v, 1) for v in s["bbox"]), text)
            one = {"mode": s["type"], "width": s["linewidth"] or 0.0,
                   "color": tuple(s["color"] or (0,)), "opacity": s.get("opacity", 1)}
            if key in rows:           # Word 的假粗體：同一串字又描了一次邊
                rows[key]["passes"].append(one)
                continue
            rows[key] = {"font": s["font"], "size": s["size"], "dir": tuple(s["dir"]),
                         "wmode": s["wmode"], "flags": s["flags"], "chars": s["chars"],
                         "passes": [one]}

        # 同一條基線上緊鄰、字型與畫法都一樣的片段合成一段（Word 常把「115」拆成
        # 「11」「5」）。中間夾了別種字的就不併，不然清原文時會連那個字一起清掉
        def style(row: dict) -> tuple:
            return (row["font"], round(row["size"], 1), row["wmode"],
                    tuple((p["mode"], round(p["width"], 2), p["color"]) for p in row["passes"]))

        def along(c) -> float:
            return c[2][0]

        baselines: dict[tuple, list[dict]] = {}
        for row in rows.values():
            flat = abs(row["dir"][0] - 1) < 1e-3 and abs(row["dir"][1]) < 1e-3
            key = ("flat", round(row["chars"][0][2][1], 1)) if flat else ("other", id(row))
            baselines.setdefault(key, []).append(row)

        lines: list[dict] = []
        for key, group in baselines.items():
            if key[0] != "flat":
                lines.extend(group)
                continue
            group.sort(key=lambda r: along(r["chars"][0]))
            cur = None
            for row in group:
                if (cur is not None and style(cur) == style(row)
                        and along(row["chars"][0]) >= along(cur["chars"][-1])):
                    cur["chars"] = cur["chars"] + sorted(row["chars"], key=along)
                else:
                    cur = dict(row, chars=sorted(row["chars"], key=along))
                    lines.append(cur)
        # 照原本在內容裡出現的先後排，字段編號才穩定、好對照
        order = {id(c): i for i, r in enumerate(rows.values()) for c in r["chars"][:1]}
        lines.sort(key=lambda r: min(order.get(id(c), 1 << 30) for c in r["chars"]))

        # texttrace 連註解、表單欄位的外觀也算進來，但 redaction 只清得掉頁面內容，
        # 那些字清不掉又會被重寫一次，疊成兩層
        annots = [a.rect for a in page.annots()] + [w.rect for w in page.widgets()]

        runs = []
        for row in lines:
            flat = abs(row["dir"][0] - 1) < 1e-3 and abs(row["dir"][1]) < 1e-3
            pieces = split(row["chars"], row["size"]) if flat else [row["chars"]]
            for chars in pieces:
                x0 = min(c[3][0] for c in chars)
                y0 = min(c[3][1] for c in chars)
                x1 = max(c[3][2] for c in chars)
                y1 = max(c[3][3] for c in chars)
                mid = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
                why = ""
                if any(mid in r for r in annots):
                    why = "這是註解或表單欄位裡的字，請用 PDF 閱讀器改"
                elif not flat or row["wmode"]:
                    why = "直書或旋轉的字還不能改"
                runs.append({**row, "chars": chars, "id": f"{pno}:{len(runs)}", "page": pno,
                             "text": "".join(nfc(chr(c[0])) for c in chars),
                             "bbox": (x0, y0, x1, y1), "ok": not why, "why": why})
        return runs

    def run(self, run_id: str) -> dict | None:
        try:
            pno, n = (int(x) for x in run_id.split(":"))
            return self.pages[pno][n]
        except (ValueError, IndexError):
            return None

    def substitutes(self, run: dict) -> list[Substitute]:
        family = run["font"]
        bold = bool(run["flags"] & 16) or bool(re.search(r"bold|black|heavy", family, re.I))
        italic = bool(run["flags"] & 2) or bool(re.search(r"italic|oblique", family, re.I))
        key = (family, bold, italic)
        if key in self._subs:
            return self._subs[key]

        name = readable(family)
        faces = self.faces.get(family, [])
        cjk = (not name.isascii() or bool(style_of(name))
               or any(is_cjk(ch) for f in faces for ch in f.code))

        chain: list[Substitute] = []

        def add(sub: Substitute | None) -> None:
            if sub is not None and all((s.path, s.index, s.base14) != (sub.path, sub.index, sub.base14)
                                       for s in chain):
                chain.append(sub)

        add(find_installed(name))
        latin = Substitute(base14_for(name, bold, italic), base14=base14_for(name, bold, italic))
        latin.label = {"tiro": "Times", "tibo": "Times 粗體", "tiit": "Times 斜體",
                       "tibi": "Times 粗斜體", "helv": "Helvetica", "hebo": "Helvetica 粗體",
                       "heit": "Helvetica 斜體", "hebi": "Helvetica 粗斜體", "cour": "Courier",
                       "cobo": "Courier 粗體", "coit": "Courier 斜體",
                       "cobi": "Courier 粗斜體"}[latin.base14]
        if cjk:
            for candidate in STYLE_FONTS.get(style_of(name), []):
                hit = find_installed(candidate)
                if hit:
                    add(hit)
                    break
            add(core_substitute())
            add(latin)
        else:
            add(latin)
            add(core_substitute())
        self._subs[key] = chain
        return chain


@lru_cache(maxsize=8)
def _analysis(path: str, mtime: float) -> Analysis:
    doc = pymupdf.open(path)
    try:
        return Analysis(doc)
    finally:
        doc.close()


def analysis(path: Path) -> Analysis:
    return _analysis(str(path), path.stat().st_mtime)


# ---------------------------------------------------------------- 排版


class Plan:
    def __init__(self):
        self.glyphs: list[tuple] = []   # (寫字用的字型, 字, 原點 x，頁面座標)
        self.chars: list[dict] = []     # 給介面看的逐字狀態
        self.x0 = 0.0                   # 新字佔的範圍（頁面座標），預覽要蓋住它
        self.x1 = 0.0


def lay_out(an: Analysis, run: dict, text: str, align: str) -> Plan:
    """決定每個字寫在哪裡。

    Word 左右對齊時是逐字微調間距，照字寬重排整行會跑位。所以新舊相同的
    開頭與結尾保留原本每個字的位置，只有中間真的改掉的那段重新排，
    結尾那段整體跟著平移。原樣重寫的結果就跟原文一模一樣。
    """
    faces = an.faces.get(run["font"], [])
    subs = an.substitutes(run)
    size = run["size"]
    orig = run["chars"]
    old = run["text"]                 # 跟 orig 一字對一字

    measured = an.adv.get(run["font"], {})

    def natural(ch: str) -> float | None:
        """字寬（em）：文件裡畫過的照實際量到的，沒畫過的才查字型表"""
        if ch in measured:
            return measured[ch]
        for f in faces:
            if f.has(ch):
                return f.advance(ch)
        return None

    def pick(ch: str):
        for f in faces:
            if f.has(ch):
                return f, "orig"
        for s in subs:
            if s.has(ch):
                return s, "sub"
        return None, "none"

    def advance(ch: str) -> float:
        w = natural(ch)
        if w is not None:
            return w * size
        if ch == "　":
            return size
        if ch.isspace():
            w = natural(" ")
            return (w if w is not None else 0.25) * size
        writer = pick(ch)[0]
        return writer.advance(ch) * size if writer else size   # 缺字留個空位

    def char_end(k: int) -> float:
        return orig[k][3][2]           # 字實際畫出來的右緣，不用自己推算

    # 原文的字距：下一個字的起點減掉這個字的右緣取中位數。
    # 表格裡「公 司 名 稱」這種拉開的字，接字時才會保持一樣的間距
    extra = [b[2][0] - a[3][2] for a, b in zip(orig, orig[1:])]
    track = statistics.median(extra) if extra else 0.0
    track = max(-0.5 * size, min(track, 3 * size))

    # 新舊相同的開頭 p 個字、結尾 s 個字，中間 old[p:n-s] 換成 text[p:m-s]
    n, m = len(old), len(text)
    p = 0
    while p < min(n, m) and old[p] == text[p]:
        p += 1
    s = 0
    while s < min(n, m) - p and old[n - 1 - s] == text[m - 1 - s]:
        s += 1

    start = orig[0][2][0]
    old_end = char_end(n - 1)
    slots = [(text[i], orig[i][2][0]) for i in range(p)]

    x = orig[p][2][0] if p < n else old_end + track
    middle = text[p:m - s]
    for ch in middle:
        slots.append((ch, x))
        x += advance(ch) + track

    if s:
        # 結尾那段整體平移到中間段後面，保持原本跟前一個字的間距
        shift = x - orig[n - s][2][0]
        slots += [(text[m - s + i], orig[n - s + i][2][0] + shift) for i in range(s)]
        end = old_end + shift
    elif middle:
        end = x - track
    else:
        end = char_end(p - 1) if p else start

    grow = end - old_end
    move = {"left": 0.0, "center": -grow / 2, "right": -grow}.get(align, 0.0)

    plan = Plan()
    for ch, at in slots:
        writer, how = (None, "orig") if ch.isspace() else pick(ch)
        item = {"c": ch, "how": how}
        if how == "sub":
            item["font"] = writer.label
        plan.chars.append(item)
        if writer is not None:
            plan.glyphs.append((writer, ch, at + move))
    plan.x0 = start + move
    plan.x1 = end + move
    return plan


# ---------------------------------------------------------------- 寫入


def redact_rect(run: dict) -> pymupdf.Rect:
    """只框住這段字的中段：鄰行的字框常會跟這一行疊一點點，框整個 bbox 會誤傷"""
    ch = run["chars"]
    size = run["size"]
    a, b = ch[0][3], ch[-1][3]
    x0 = a[0] + 0.3 * (a[2] - a[0])
    x1 = b[2] - 0.3 * (b[2] - b[0])
    y0, y1 = run["bbox"][1], run["bbox"][3]
    mid, band = (y0 + y1) / 2, max(0.5, 0.15 * size)
    return pymupdf.Rect(min(x0, x1 - 0.2), mid - band, max(x1, x0 + 0.2), mid + band)


def own_resources(doc, page) -> None:
    """頁面的 Resources 若是從上層繼承來的，先複製一份到頁面上，再往裡面加字型"""
    if doc.xref_get_key(page.xref, "Resources")[0] != "null":
        return
    node = page.xref
    while True:
        t, v = doc.xref_get_key(node, "Parent")
        if t != "xref":
            break
        node = int(v.split()[0])
        t2, v2 = doc.xref_get_key(node, "Resources")
        if t2 != "null":
            doc.xref_set_key(page.xref, "Resources", v2)
            return
    doc.xref_set_key(page.xref, "Resources", "<<>>")


class Embedder:
    """把替代字型掛上頁面。整份文件每個替代字型只嵌一次。

    輸出時（compact）先把用得到的字全部登記好，嵌入時用核心的 subset_font 只放那些字
    （整支嵌的話 MuPDF 會替整套字寫一份 ToUnicode，兩個字也要多將近 100 KB）；
    預覽時直接用整支字型，打字時才不用每次重新子集化。
    """

    def __init__(self, compact: bool):
        self.compact = compact
        self.need: dict[int, set[str]] = {}
        self.xrefs: dict[int, int] = {}
        self.fonts: dict[int, pymupdf.Font] = {}   # 子集化後的字型，查新的字符編號用

    def want(self, plan: "Plan") -> None:
        for writer, ch, _ in plan.glyphs:
            if isinstance(writer, Substitute) and not writer.base14:
                self.need.setdefault(id(writer), set()).add(ch)

    def attach(self, doc, page, writer) -> str:
        """把字型掛到頁面資源上，回傳資源名稱"""
        if isinstance(writer, Face):
            xref = writer.xref
        else:
            key = id(writer)
            xref = self.xrefs.get(key)
            if xref is None:
                name = f"RTS{len(self.xrefs)}"
                xref = None
                if self.compact and not writer.base14 and CTX and CTX.subset_font:
                    try:
                        buf = CTX.subset_font(writer.path, "".join(sorted(self.need.get(key, ()))),
                                              writer.index)
                        xref = page.insert_font(fontname=name, fontbuffer=buf)
                        self.fonts[key] = pymupdf.Font(fontbuffer=buf)
                    except Exception as e:   # 子集化失敗就退回整支嵌入，大一點但字是對的
                        print(f"retype：{writer.label} 子集化失敗，改嵌整支字型：{e}")
                if xref is None:
                    xref = writer.embed(page, name)
                self.xrefs[key] = xref
        name = f"RT{xref}"
        own_resources(doc, page)
        doc.xref_set_key(page.xref, f"Resources/Font/{name}", f"{xref} 0 R")
        return name

    def hex(self, writer, ch: str) -> str:
        font = self.fonts.get(id(writer))
        if font is not None:
            return f"{font.has_glyph(ord(ch)):04X}"
        return writer.hex(ch)


def color_op(one: dict) -> str:
    c = one["color"]
    op = {1: "g", 4: "k"}.get(len(c), "rg")
    if one["mode"] == 1:
        op = op.upper()
    return " ".join(f"{v:.4f}" for v in c) + " " + op


def append_stream(doc, page, data: bytes) -> None:
    page.wrap_contents()      # 原本的內容包進 q/Q，殘留的座標變換才不會吃到新寫的字
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, data)
    t, v = doc.xref_get_key(page.xref, "Contents")
    if t == "array":
        new = v.rstrip()[:-1] + f" {xref} 0 R]"
    elif t == "xref":
        new = f"[{v} {xref} 0 R]"
    else:
        new = f"{xref} 0 R"
    doc.xref_set_key(page.xref, "Contents", new)


def apply(doc, page, an: Analysis, pairs: list, embed: Embedder) -> list[dict]:
    """把一頁的修改寫進去。pairs 是 [(字段, 修改)]；回傳每段的預覽範圍與逐字狀態"""
    plans = [(run, edit, lay_out(an, run, edit.text, edit.align)) for run, edit in pairs]
    for _, _, plan in plans:
        embed.want(plan)

    before = census(page)
    for run, _, _ in plans:
        page.add_redact_annot(redact_rect(run))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    removed = before - census(page)
    expected: Counter = Counter()
    for run, _, _ in plans:
        for c in run["chars"]:
            if not chr(c[0]).isspace():
                expected[(c[0], round(c[2][0], 1), round(c[2][1], 1))] += len(run["passes"])
    warnings = []
    if removed - expected:
        warnings.append("旁邊的字也被清掉了，請看一下預覽")
    if expected - removed:
        warnings.append("原文沒有完全清掉")

    inv = ~page.transformation_matrix
    ops = []
    for run, _, plan in plans:
        base = run["chars"][0][2][1]
        o = pymupdf.Point(plan.x0, base)
        vec = (o + pymupdf.Point(1, 0)) * inv - o * inv
        a, b = vec.x / abs(vec), vec.y / abs(vec)
        for writer, ch, x in plan.glyphs:
            name = embed.attach(doc, page, writer)
            at = pymupdf.Point(x, base) * inv
            for one in run["passes"]:
                ops.append(
                    f"q {color_op(one)} {one['width']:.3f} w BT /{name} {run['size']:.3f} Tf "
                    f"{one['mode']} Tr {a:.5f} {b:.5f} {-b:.5f} {a:.5f} {at.x:.3f} {at.y:.3f} Tm "
                    f"<{embed.hex(writer, ch)}> Tj ET Q")
    if ops:
        append_stream(doc, page, "\n".join(ops).encode())

    out = []
    for run, edit, plan in plans:
        x0, y0, x1, y1 = run["bbox"]
        pad = run["size"] * 0.25
        rect = pymupdf.Rect(min(x0, plan.x0) - pad, y0 - pad,
                            max(x1, plan.x1) + pad, y1 + pad) & page.rect
        mine = list(warnings)
        if plan.x1 > page.rect.x1 + 0.5 or plan.x0 < page.rect.x0 - 0.5:
            mine.append("新字超出頁面邊界，超出的部分印不出來")
        out.append({"run": run["id"], "rect": rect, "chars": plan.chars, "warnings": mine})
    return out


# ---------------------------------------------------------------- API


class Edit(BaseModel):
    run: str
    orig: str
    text: str
    align: str = "left"


class RenderRequest(BaseModel):
    doc: str
    page: int
    edits: list[Edit]


def pair_up(an: Analysis, edits: list[Edit]) -> dict[int, list]:
    """核對每筆修改對應的字段還在不在，照頁分組"""
    pages: dict[int, list] = {}
    for e in edits:
        run = an.run(e.run)
        if run is None or run["text"] != "".join(nfc(c) for c in e.orig):
            raise HTTPException(409, "文件內容對不上，請關掉修正原文再重開一次")
        if not run["ok"]:
            raise HTTPException(400, f"第 {run['page'] + 1} 頁「{run['text']}」{run['why']}")
        e.text = e.text.replace("\r", "").replace("\n", "")[:MAX_TEXT]
        if e.align not in ALIGNS:
            e.align = "left"
        pages.setdefault(run["page"], []).append((run, e))
    return pages


@router.get("/runs/{doc_id}")
def runs(doc_id: str):
    if CTX is None:
        raise HTTPException(500, "外掛沒有初始化")
    an = analysis(CTX.doc_path(doc_id))
    out = []
    for pno, page_runs in enumerate(an.pages):
        w, h = an.sizes[pno]
        for r in page_runs:
            x0, y0, x1, y1 = r["bbox"]
            out.append({"id": r["id"], "page": pno, "text": r["text"],
                        "rect": [x0 / w, y0 / h, x1 / w, y1 / h],
                        "ok": r["ok"], "why": r["why"]})
    return {"runs": out}


@router.post("/render")
def render(req: RenderRequest):
    if CTX is None:
        raise HTTPException(500, "外掛沒有初始化")
    path = CTX.doc_path(req.doc)
    an = analysis(path)
    pages = pair_up(an, req.edits)
    pairs = pages.get(req.page, [])
    if not pairs or len(pages) > 1:
        raise HTTPException(400, "一次只預覽一頁")

    doc = pymupdf.open(path)
    try:
        page = doc[req.page]
        items = apply(doc, page, an, pairs, Embedder(compact=False))
        page = doc.reload_page(page)
        w, h = page.rect.width, page.rect.height
        out = []
        for it in items:
            rect = it["rect"]
            pix = page.get_pixmap(matrix=pymupdf.Matrix(PREVIEW_ZOOM, PREVIEW_ZOOM), clip=rect)
            png = base64.b64encode(pix.tobytes("png")).decode("ascii")
            out.append({"run": it["run"], "chars": it["chars"], "warnings": it["warnings"],
                        "rect": [rect.x0 / w, rect.y0 / h, rect.x1 / w, rect.y1 / h],
                        "png": "data:image/png;base64," + png})
    finally:
        doc.close()
    return {"items": out}


def before_sign(doc, extra: dict) -> None:
    """輸出時先改原文，核心之後才蓋章，戳章會疊在最上面"""
    raw = extra.get("retype") if isinstance(extra, dict) else None
    if not raw:
        return
    try:
        edits = [Edit(**e) for e in raw]
    except Exception:
        raise HTTPException(400, "修正原文的資料格式不對")

    an = Analysis(doc)
    pages = pair_up(an, edits)

    # 先全部排過一次：有寫不出來的字就整個擋下，不要輸出半套；
    # 順便把每個替代字型要用到的字登記好，嵌入時一次只放這些字
    embed = Embedder(compact=True)
    for pairs in pages.values():
        for run, e in pairs:
            plan = lay_out(an, run, e.text, e.align)
            missing = [c["c"] for c in plan.chars if c["how"] == "none"]
            if missing:
                raise HTTPException(
                    400, f"第 {run['page'] + 1} 頁「{e.text}」裡的「{''.join(missing)}」"
                         "找不到任何字型寫得出來")
            embed.want(plan)

    for pno, pairs in pages.items():
        apply(doc, doc[pno], an, pairs, embed)


# ---------------------------------------------------------------- 前端

CLIENT_JS = r"""
(() => {
const RT = {
  on: false, scanned: false, runs: [], byId: {}, edits: {}, align: {}, sel: null,
  prev: {}, info: {}, timers: {}, seq: {}, composing: false,
  listOpen: false, detail: false
};

const style = document.createElement('style');
style.textContent = `
.rt-count { border:0; background:none; padding:.1rem .3rem; }
.rt-count:hover:not(:disabled) { background:none; color:var(--ink); }
.rt-count:disabled { opacity:1; cursor:default; }
.rt-panel { padding:.7rem 1.25rem .75rem; border-bottom:1px solid var(--line); background:#FAFAF9; }
.rt-panel[hidden] { display:none; }
.rt-head { display:flex; align-items:center; gap:.5rem; margin-bottom:.35rem; min-height:1.5em; }
.rt-orig { flex:1; min-width:0; font-size:.78rem; color:var(--ink-faint);
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rt-badge { flex-shrink:0; border:0; border-radius:999px; padding:.05rem .55rem;
            font-size:.72rem; line-height:1.6; background:#FFF1D6; color:#8A5A12; }
.rt-badge:hover:not(:disabled) { background:#FDE6B8; border:0; }
.rt-badge:disabled { opacity:1; cursor:default; }
.rt-badge[hidden] { display:none; }
.rt-badge.ok { background:none; color:var(--ink-faint); padding-right:0; }
.rt-badge.bad { background:var(--seal-wash); color:var(--seal); }
.rt-panel input[type=text] { width:100%; padding:.45rem .6rem; border:1px solid var(--line);
                             border-radius:3px; background:#fff; }
.rt-detail { margin-top:.5rem; font-size:.78rem; color:var(--ink-soft); line-height:1.6; }
.rt-detail[hidden] { display:none; }
.rt-detail .txt { font-size:.95rem; color:var(--ink); word-break:break-all; }
.rt-detail mark { background:#FFF1D6; color:inherit; border-bottom:2px dotted #B7791F; }
.rt-detail mark.none { background:var(--seal-wash); border-color:var(--seal); color:var(--seal); }
.rt-detail .bad { color:var(--seal); }
.rt-foot { display:flex; align-items:center; gap:.5rem; margin-top:.55rem; }
.rt-foot .seg { width:9rem; }
.rt-foot .seg[hidden] { display:none; }
.rt-foot .seg button { padding:.25rem 0; font-size:.8rem; }
.rt-link { margin-left:auto; border:0; background:none; padding:.25rem .3rem;
           font-size:.82rem; color:var(--ink-faint); }
.rt-link:hover:not(:disabled) { background:none; color:var(--seal); }
.rt-link[hidden] { display:none; }
.rt-foot .primary { padding:.28rem .8rem; font-size:.85rem; }
.rt-foot .rt-link[hidden] + .primary { margin-left:auto; }
.rt-list { max-height:9.5rem; overflow-y:auto; border-bottom:1px solid var(--line); }
.rt-list[hidden] { display:none; }
.rt-item { padding:.45rem 1.25rem; display:flex; gap:.5rem; align-items:baseline;
           font-size:.85rem; cursor:pointer; border-bottom:1px solid #EEF0EC; }
.rt-item:last-child { border-bottom:0; }
.rt-item:hover { background:#FAFAF9; }
.rt-item.on { background:var(--seal-wash); }
.rt-item .from { color:var(--ink-faint); text-decoration:line-through;
                 overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:35%; }
.rt-item .to { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; }
.rt-item .flag { width:.45rem; height:.45rem; border-radius:50%; flex-shrink:0;
                 align-self:center; background:#D69E2E; }
.rt-item .flag.bad { background:var(--seal); }
.rt-item button { border:0; background:none; padding:0 .2rem; color:var(--ink-faint);
                  font-size:1rem; line-height:1; }
.rt-item button:hover { color:var(--seal); background:none; }
.rt-run { position:absolute; z-index:5; cursor:text; border-radius:1px; }
.rt-run:hover { background:rgba(176,58,46,.08); outline:1px solid var(--seal); }
.rt-run.edited { outline:1px dashed var(--ink-faint); }
.rt-run.on { outline:2px solid var(--seal); background:transparent; }
.rt-run.bad { cursor:not-allowed; }
.rt-run.bad:hover { outline-color:var(--ink-faint); background:rgba(0,0,0,.04); }
.sheet img.rt-prev { position:absolute; z-index:0; pointer-events:none; }
`;
document.head.appendChild(style);

// 側欄只放開關和計數；怎麼用在打開時用 toast 講一次，修改清單點計數才展開
const bar = document.createElement('div');
bar.className = 'plugin-bar';
bar.innerHTML =
  '<label class="plugin-switch"><input type="checkbox" id="rtOn"><span>修正原文</span></label>' +
  '<button class="plugin-status rt-count" id="rtCount" disabled aria-expanded="false"></button>';
document.querySelector('.presets').insertAdjacentElement('afterend', bar);

const list = document.createElement('div');
list.className = 'rt-list';
list.hidden = true;
bar.insertAdjacentElement('afterend', list);

const panel = document.createElement('div');
panel.className = 'rt-panel';
panel.hidden = true;
panel.innerHTML =
  '<div class="rt-head">' +
  '  <span class="rt-orig" id="rtOrig"></span>' +
  '  <button class="rt-badge" id="rtBadge" hidden aria-expanded="false"></button>' +
  '</div>' +
  '<input type="text" id="rtText" autocomplete="off" spellcheck="false" aria-label="改成">' +
  '<div class="rt-detail" id="rtDetail" hidden aria-live="polite"></div>' +
  '<div class="rt-foot">' +
  '  <div class="seg" role="group" aria-label="字數變了以後怎麼對齊原文" id="rtAlign" hidden>' +
  '    <button data-align="left" title="新字從原文的左緣開始">靠左</button>' +
  '    <button data-align="center" title="新字以原文的中心對齊">置中</button>' +
  '    <button data-align="right" title="新字的右緣對齊原文的右緣">靠右</button>' +
  '  </div>' +
  '  <button class="rt-link" id="rtRevert" hidden>還原</button>' +
  '  <button class="primary" id="rtDone" title="也可以按 Enter">完成</button>' +
  '</div>';
list.insertAdjacentElement('afterend', panel);

// 右上角那一格：沒改東西時是淡淡的提示，有改就變成可以展開清單的按鈕
function info(msg, bad) {
  const el = $('#rtCount');
  el.textContent = msg;
  el.disabled = true;
  el.classList.toggle('bad', !!bad);
}

function count() {
  if (!RT.scanned || !RT.runs.length) return;
  const n = Object.keys(RT.edits).length;
  if (!n) {
    RT.listOpen = false;
    info(RT.on ? '點字來改寫' : '');
  } else {
    const el = $('#rtCount');
    el.disabled = false;
    el.classList.remove('bad');
    el.textContent = '已改 ' + n + ' 處 ' + (RT.listOpen ? '▴' : '▾');
    el.setAttribute('aria-expanded', String(RT.listOpen));
  }
  list.hidden = !(n && RT.listOpen);
}

$('#rtCount').onclick = () => { RT.listOpen = !RT.listOpen; count(); listEdits(); };

// 蓋章停用、點頁面不蓋章都交給核心；被別的模式擠掉時把自己收起來
function mode() {
  if (!RT.on) return leaveMode('retype');
  enterMode('retype', {
    hint: '修正原文模式下不能蓋章，請點頁面上的字，或關掉修正原文',
    blocked: () => { if (RT.sel) { select(null); return true; } },   // 改字時點空白處＝改完了
    off: () => setOn(false),
  });
}

// ---------- 頁面上的字段框 ----------

function paintRuns() {
  document.querySelectorAll('.rt-run').forEach(n => n.remove());
  if (!RT.on) return;
  RT.runs.forEach(run => {
    const sheet = document.querySelector(`.sheet[data-page="${run.page}"]`);
    if (!sheet) return;
    const d = document.createElement('div');
    d.className = 'rt-run';
    d.dataset.id = run.id;
    // 字框貼著字，稍微放大一點比較好點
    const [x0, y0, x1, y1] = run.rect;
    d.style.left = (x0 * 100 - .15) + '%';
    d.style.top = (y0 * 100 - .1) + '%';
    d.style.width = ((x1 - x0) * 100 + .3) + '%';
    d.style.height = ((y1 - y0) * 100 + .2) + '%';
    d.title = run.ok ? run.text : run.why;
    d.onclick = e => {
      e.stopPropagation();
      if (!run.ok) return toast(run.why);
      select(run.id);
    };
    sheet.appendChild(d);
  });
  markRuns();
}

function markRuns() {
  document.querySelectorAll('.rt-run').forEach(d => {
    const run = RT.byId[d.dataset.id];
    d.classList.toggle('bad', !run.ok);
    d.classList.toggle('edited', !!RT.edits[run.id]);
    d.classList.toggle('on', RT.sel === run.id);
  });
}

// ---------- 預覽：伺服器照實際輸出的方式畫好，疊在原頁上 ----------

function paintPrev() {
  document.querySelectorAll('.rt-prev').forEach(n => n.remove());
  Object.entries(RT.prev).forEach(([page, items]) => {
    const base = document.querySelector(`.sheet[data-page="${page}"] img`);
    if (!base) return;
    items.forEach(it => {
      const im = document.createElement('img');
      im.className = 'rt-prev';
      im.alt = '';
      im.draggable = false;
      im.src = it.png;
      im.style.left = it.rect[0] * 100 + '%';
      im.style.top = it.rect[1] * 100 + '%';
      im.style.width = (it.rect[2] - it.rect[0]) * 100 + '%';
      im.style.height = (it.rect[3] - it.rect[1]) * 100 + '%';
      // 放在原頁圖片正後面：蓋得住原文，但戳章的 SVG 還是在最上面
      base.insertAdjacentElement('afterend', im);
    });
  });
}

function schedule(page) {
  clearTimeout(RT.timers[page]);
  RT.timers[page] = setTimeout(() => render(page), 250);
}

async function render(page) {
  const edits = Object.values(RT.edits).filter(e => e.page === page);
  const seq = RT.seq[page] = (RT.seq[page] || 0) + 1;
  if (!edits.length) {
    delete RT.prev[page];
    paintPrev();
    return;
  }
  let r;
  try {
    r = await fetch('/api/retype/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: state.doc, page, edits: edits.map(pack) })
    });
  } catch { return toast('連不上服務'); }
  if (seq !== RT.seq[page]) return;      // 打字很快的時候，只認最後一次
  if (!r.ok) {
    let msg = '預覽失敗';
    try { const d = (await r.json()).detail; if (typeof d === 'string') msg = d; } catch {}
    return toast(msg);
  }
  const data = await r.json();
  if (seq !== RT.seq[page]) return;
  RT.prev[page] = data.items;
  data.items.forEach(it => { RT.info[it.run] = it; });
  paintPrev(); status(); listEdits();
}

const pack = e => ({ run: e.run, orig: e.orig, text: e.text, align: e.align });

// ---------- 編輯 ----------

function select(id) {
  RT.sel = id;
  RT.detail = false;
  const run = RT.byId[id];
  panel.hidden = !run;
  if (run) {
    $('#rtOrig').textContent = '原文　' + run.text;
    $('#rtOrig').title = run.text;
    const ed = RT.edits[id];
    $('#rtText').value = ed ? ed.text : run.text;
    setAlign(RT.align[id] || 'left');
    status();
    $('#rtText').focus();
    $('#rtText').select();
  }
  markRuns(); listEdits();
}

function setAlign(a) {
  $('#rtAlign').querySelectorAll('button').forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.align === a)));
}

function change() {
  const run = RT.byId[RT.sel];
  if (!run) return;
  const text = $('#rtText').value;
  if (text === run.text) { delete RT.edits[run.id]; delete RT.info[run.id]; }
  else RT.edits[run.id] = { run: run.id, page: run.page, orig: run.text, text,
                            align: RT.align[run.id] || 'left' };
  // 逐字狀態先沿用上一次的，新的預覽回來再換，打字時標籤才不會一直閃
  status();
  schedule(run.page);
  markRuns(); listEdits(); count();
}

function revert(id) {
  const run = RT.byId[id];
  delete RT.edits[id];
  delete RT.info[id];
  if (RT.sel === id) { $('#rtText').value = run.text; status(); }
  schedule(run.page);
  markRuns(); listEdits(); count();
}

// 編輯區的狀態：平常只有右上角一個小標籤，點了才展開逐字說明；
// 寫不出來的字、誤傷旁邊的字這種要處理的問題，才自動展開
function status() {
  const run = RT.byId[RT.sel];
  if (!run) return;
  const ed = RT.edits[run.id];
  const it = ed && RT.info[run.id];
  const badge = $('#rtBadge'), detail = $('#rtDetail');

  $('#rtRevert').hidden = !ed;
  $('#rtAlign').hidden = !ed || [...ed.text].length === [...run.text].length;

  if (!it) {
    badge.hidden = true;
    detail.hidden = true;
    return;
  }

  const subs = it.chars.filter(c => c.how === 'sub');
  const none = it.chars.filter(c => c.how === 'none');
  const warns = it.warnings || [];
  const bad = none.length > 0 || warns.length > 0;
  const more = bad || subs.length > 0;

  badge.hidden = false;
  badge.disabled = !more;
  badge.className = 'rt-badge' + (bad ? ' bad' : more ? '' : ' ok');
  badge.textContent =
    none.length ? none.length + ' 個字寫不出來' :
    warns.length ? '請看一下預覽' :
    subs.length ? subs.length + ' 個替代字' :
    !ed.text ? '整段拿掉' : '✓ 原字型';
  if (more) badge.textContent += RT.detail || bad ? ' ▴' : ' ▾';

  detail.hidden = !(more && (RT.detail || bad));
  badge.setAttribute('aria-expanded', String(!detail.hidden));
  if (detail.hidden) return;

  detail.innerHTML = '';
  const txt = document.createElement('div');
  txt.className = 'txt';
  it.chars.forEach(c => {
    const el = document.createElement(c.how === 'orig' ? 'span' : 'mark');
    el.textContent = c.c;
    if (c.how === 'none') el.className = 'none';
    txt.appendChild(el);
  });
  detail.appendChild(txt);

  const say = (msg, cls) => {
    const p = document.createElement('div');
    p.textContent = msg;
    if (cls) p.className = cls;
    detail.appendChild(p);
  };
  if (subs.length) say('標底線的字原字型裡沒有，改用' +
                       [...new Set(subs.map(c => c.font))].join('、') + '。');
  if (none.length) say('紅色的字找不到任何字型寫得出來，要換掉才能輸出。', 'bad');
  warns.forEach(w => say(w + '。', 'bad'));
}

$('#rtBadge').onclick = () => { RT.detail = !RT.detail; status(); };

function listEdits() {
  const eds = Object.values(RT.edits).sort((a, b) =>
    a.page - b.page || RT.byId[a.run].rect[1] - RT.byId[b.run].rect[1]);
  list.hidden = !(eds.length && RT.listOpen);
  list.innerHTML = '';
  if (list.hidden) return;
  eds.forEach(e => {
    const row = document.createElement('div');
    row.className = 'rt-item' + (e.run === RT.sel ? ' on' : '');
    row.innerHTML = '<span class="item-pg"></span><span class="from"></span>' +
                    '<span class="to"></span>' +
                    '<button title="還原這一處" aria-label="還原">×</button>';
    row.querySelector('.item-pg').textContent = 'p.' + (e.page + 1);
    row.querySelector('.from').textContent = e.orig;
    row.querySelector('.to').textContent = e.text || '（拿掉）';
    // 有替代字或寫不出來的字，只用一個小圓點提醒，細節點進去看
    const it = RT.info[e.run];
    if (it && it.chars.some(c => c.how !== 'orig')) {
      const dot = document.createElement('span');
      const none = it.chars.some(c => c.how === 'none');
      dot.className = 'flag' + (none ? ' bad' : '');
      dot.title = none ? '有寫不出來的字' : '有替代字';
      row.querySelector('.to').insertAdjacentElement('afterend', dot);
    }
    row.onclick = () => {
      if (!RT.on) setOn(true);
      select(e.run);
      document.querySelector(`.sheet[data-page="${e.page}"]`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
    row.querySelector('button').onclick = ev => { ev.stopPropagation(); revert(e.run); };
    list.appendChild(row);
  });
}

$('#rtText').addEventListener('input', () => { if (!RT.composing) change(); });
$('#rtText').addEventListener('compositionstart', () => { RT.composing = true; });
$('#rtText').addEventListener('compositionend', () => { RT.composing = false; change(); });
$('#rtText').addEventListener('keydown', e => {
  if (e.isComposing) return;
  if (e.key === 'Enter' || e.key === 'Escape') { e.preventDefault(); select(null); }
});
$('#rtAlign').querySelectorAll('button').forEach(btn => {
  btn.onclick = () => {
    const run = RT.byId[RT.sel];
    if (!run) return;
    RT.align[run.id] = btn.dataset.align;
    setAlign(btn.dataset.align);
    const ed = RT.edits[run.id];
    if (ed) { ed.align = btn.dataset.align; schedule(run.page); }
  };
});
$('#rtRevert').onclick = () => { if (RT.sel) revert(RT.sel); };
$('#rtDone').onclick = () => select(null);

// ---------- 開關 ----------

async function scan() {
  info('讀取中…');
  let r;
  try { r = await fetch('/api/retype/runs/' + state.doc); }
  catch { info(''); return toast('連不上服務'); }
  if (!r.ok) { info(''); return toast('讀取失敗'); }
  const data = await r.json();
  RT.runs = data.runs;
  RT.byId = {};
  data.runs.forEach(run => { RT.byId[run.id] = run; });
  RT.scanned = true;
  if (!RT.runs.length) info('這份 PDF 沒有文字層，沒有字可以改', true);
}

async function setOn(on) {
  RT.on = on;
  $('#rtOn').checked = on;
  mode();
  if (on && !RT.scanned) await scan();
  if (!on) select(null);
  paintRuns(); count();
  // 打開時講一次怎麼用；沒有文字層的話，右上角已經寫了原因，不用再跳
  if (on && RT.on && RT.runs.length) toast('修正原文已開啟：點頁面上的字就能改寫，這段期間不能蓋章');
}

$('#rtOn').onchange = e => setOn(e.target.checked);

// 輸出時附上修改，核心會交給後端的 before_sign
document.addEventListener('pdfsign:collect', e => {
  const eds = Object.values(RT.edits);
  if (eds.length) e.detail.extra.retype = eds.map(pack);
});

document.addEventListener('pdfsign:loaded', () => {
  Object.values(RT.timers).forEach(clearTimeout);
  Object.assign(RT, { on: false, scanned: false, runs: [], byId: {}, edits: {}, align: {},
                      sel: null, prev: {}, info: {}, timers: {}, seq: {},
                      listOpen: false, detail: false });
  $('#rtOn').checked = false;
  panel.hidden = true;
  mode(); listEdits(); info('');
});
})();
"""
