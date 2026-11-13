# -*- coding: utf-8 -*-
"""
MSDS 관리요령 · 경고표지 생성기 (V9) - 핵심 로직
 - 원본 MSDS(PDF)를 '위치 기준'으로 읽어 줄/항목을 복원 (텍스트 순서가 뒤섞인 PDF 대응)
 - MSDS 원문 문장은 자르거나 합치지 않고 '선택'만 한다
 - 외국어는 기계번역을 쓰지 않고 검수된 사전(msds_dictionary.json)에서만 가져온다
 - 사전에 없으면 비워 두고 '번역 필요'로 알린다 (추측 번역 금지)
"""
from __future__ import annotations
import base64, html, io, json, os, re, shutil, subprocess, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DICT_PATH = HERE / "msds_dictionary.json"
USER_DICT_PATH = HERE / "user_dictionary.json"

FOREIGN = ["en", "zh", "vi", "km", "ru", "si"]
LANG_NAMES = {"en": "영어", "zh": "중국어", "vi": "베트남어", "km": "캄보디아어(크메르어)", "ru": "러시아어", "si": "싱할라어(스리랑카)"}
LANG_ATTR = {"ko": "ko", "en": "en", "zh": "zh-CN", "vi": "vi", "km": "km", "ru": "ru", "si": "si"}

# 항목 개수: (기본, 최대)  - 요청하신 기준
LIMITS = {"handling": (4, 4), "storage": (4, 4), "eye": (2, 2), "skin": (2, 2),
          "inhale": (2, 2), "ingest": (2, 2), "incident": (4, 4)}

# ───────────────────────── 1. PDF → 칸/항목 (서식이 달라도 읽도록 일반화) ─────────────────────────
HEAD = re.compile(r"^\s*(?:제\s*|섹션\s*|Section\s*|SECTION\s*)?(1[0-6]|[1-9])\s*(?:[.．:：]|항\b)\s*([^\d\s].*)$")
EN_KEYS = {1: r"IDENTIF", 2: r"HAZARD", 3: r"COMPOSIT", 4: r"FIRST", 5: r"FIRE", 6: r"ACCIDENT|RELEASE", 7: r"HANDLING",
           8: r"EXPOSURE", 9: r"PHYSICAL", 10: r"STABIL", 11: r"TOXIC", 12: r"ECOLOG", 13: r"DISPOS", 14: r"TRANSPORT",
           15: r"REGULAT", 16: r"OTHER"}
SEC_KEYS = {1: r"화학제품|회사에", 2: r"유해|위험", 3: r"구성|성분", 4: r"응급|조치", 5: r"폭발|화재|소화", 6: r"누출|유출", 7: r"취급|저장",
            8: r"노출|보호구", 9: r"물리|화학적", 10: r"안정|반응", 11: r"독성", 12: r"환경", 13: r"폐기", 14: r"운송",
            15: r"법적|규제", 16: r"참고|기타"}
MARK = re.compile(r"^\s*(?:[-–—•·ㆍ○◎●■□▪※*▶]+|[①-⑳⑴-⒇]|\(?\d{1,2}\)|\d{1,2}\.\d{1,2}\.?|[가-하a-z]\s*[.)])\s*")
SUBMARK = re.compile(r"^\s*(?:[가-하]\s*[.．)]\s*)?")
SUBKW = {
    4: [(r"눈에\s*들어갔을\s*때|눈\s*접촉|눈에\s*접촉|안구\s*접촉|눈\s*(?=[:：])|(?:화학물질이\s*)?눈에\s*들어간\s*경우(?=\s*[:：]?\s*$)", "눈"),
        (r"피부에\s*접촉했을\s*때|피부\s*접촉|피부에\s*접촉|피부\s*(?=[:：])|(?:화학물질이\s*)?피부에\s*(?:닿은|묻은|접촉한)\s*경우(?=\s*[:：]?\s*$)", "피부"),
        (r"흡입했을\s*때|흡입\s*시|흡입\s*(?=[:：])|호흡기\s*(?=[:：])|흡입(?:한|하였을|했을)\s*경우(?=\s*[:：]?\s*$)", "흡입"),
        (r"먹었을\s*때|삼켰을\s*때|섭취\s*시|섭취\s*(?=[:：])|경구\s*(?=[:：])|삼켰을\s*경우|(?:삼킨|먹은|섭취한|삼켰을)\s*경우(?=\s*[:：]?\s*$)", "섭취"),
        (r"기타\s*의사|의사의\s*주의\s*사항|급성\s*및\s*지연성(?:의)?\s*가장\s*중요한\s*증상|즉각적인\s*의료\s*조치\s*및\s*특별\s*치료", "의사")],
    5: [(r"적절한\s*\(?\s*및?\s*부적절한\s*\)?\s*소화제|적절한.{0,10}소화제", "소화제"), (r"화학물질(?:로부터\s*생기는|에서\s*기인하는)\s*특정\s*유해성|특정\s*유해성", "특정유해성"),
        (r"화재\s*진압\s*시\s*착용할\s*보호구[^\n]*|화재\s*진압\s*시|소방관을\s*위한\s*특별\s*보호\s*조치|소방\s*시\s*보호", "진압")],
    6: [(r"인체를\s*보호하기\s*위해\s*필요한\s*조치\s*사항\s*및\s*보호구|인체를\s*보호|개인\s*예방\s*조치(?:\s*,\s*보호\s*장비\s*및\s*비상\s*절차)?", "인체"), (r"환경을\s*보호하기\s*위해\s*필요한\s*조치사항|환경을\s*보호|환경\s*예방\s*조치", "환경"),
        (r"정화\s*또는\s*제거\s*방법|봉쇄\s*및\s*정화(?:\s*방법\s*및\s*재료)?", "정화")],
    7: [(r"안전\s*취급\s*요령", "취급"), (r"안전한\s*저장\s*방법(?:\s*\([^)]*\))?|저장\s*방법\s*(?=[:：])|비호환성을\s*포함한\s*안전한\s*보관(?:을\s*위한\s*조건)?|안전한\s*보관(?:을\s*위한\s*조건)?|보관\s*조건", "저장")],
    8: [(r"화학물질의\s*노출기준", "노출기준"), (r"적절한\s*공학적\s*관리", "공학"), (r"개인\s*보호구|개인\s*보호\s*장비|개인\s*보호\s*조치", "보호구")],
}
SUBKW = {n: [(re.compile(p), name) for p, name in v] for n, v in SUBKW.items()}
CODE = re.compile(r"^\(?\s*([HP]\d{3}[A-Za-z]?(?:\s*\+\s*[HP]?\d{3}[A-Za-z]?)*)\s*\)?\s*[:：.\-–]?\s*(.*)$")
PAGEPAT = re.compile(r"^(?:Page|페이지|PAGE)\s*[:：]?\s*\d+|^\d+\s*/\s*\d+$|^-\s*\d+\s*-$|^\d+\s*$", re.I)
TERM = re.compile(r"(?:[.。!?]|시오|하라|할 것|할것|있음|없음|함|음|됨|임|것|다|요)\s*\.?\s*$")


def chars_to_words(lines):
    """글자 목록(줄 단위)을 단어로 묶는다. 글꼴이 달라 글자 상자 높이가 달라도 줄이 갈라지지 않도록
    각 글자의 '기준선(baseline)'을 세로 위치로 쓴다. lines: [[{c,x0,x1,base,size}, ...], ...]"""
    words = []
    for ln in lines:
        cur = []
        def flush():
            if cur:
                size = max(ch["size"] for ch in cur)
                base = sum(ch["base"] for ch in cur) / len(cur)
                text = "".join(ch["c"] for ch in cur)
                words.append((min(ch["x0"] for ch in cur), base - size * 0.8, max(ch["x1"] for ch in cur), base + size * 0.2, text))
        for ch in ln:
            if ch["c"].isspace():
                flush(); cur = []
                continue
            if cur and (ch["x0"] - cur[-1]["x1"] > 0.35 * max(ch["size"], 1) or abs(ch["base"] - cur[-1]["base"]) > 2.5):
                flush(); cur = []
            cur.append(ch)
        flush()
    return words


def read_pdf_plumber(raw: bytes):
    """pdfplumber로 읽는다(권장). 글자 위치는 기준선으로 줄을 묶고, 그림문자 후보는 쪽을 144dpi로 그려 잘라낸다."""
    import pdfplumber
    pages = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for pg in pdf.pages:
            chs = []
            for c in pg.chars:
                try:
                    base = pg.height - c["matrix"][5]
                except Exception:
                    base = c["bottom"] - c.get("size", 10) * 0.2
                chs.append({"c": c["text"], "x0": c["x0"], "x1": c["x1"], "base": base, "size": c.get("size", 10)})
            chs.sort(key=lambda c: (round(c["base"] / 2), c["x0"]))
            lines, cur = [], []
            for c in chs:
                if cur and (abs(c["base"] - cur[-1]["base"]) > 2.5 or c["x0"] - cur[-1]["x1"] > 60):
                    lines.append(cur); cur = []
                cur.append(c)
            if cur:
                lines.append(cur)
            words = chars_to_words(lines)
            cands = [im for im in pg.images
                     if 14 < (im["bottom"] - im["top"]) < 130 and 14 < (im["x1"] - im["x0"]) < 420
                     and 0.7 < (im["x1"] - im["x0"]) / max(im["bottom"] - im["top"], 1e-6) < 5.5]
            imgs = []
            if cands:
                try:
                    full = pg.to_image(resolution=144).original.convert("RGB")
                    sc = 144 / 72.0
                    for im in cands:
                        crop = full.crop((int(im["x0"] * sc), int(im["top"] * sc), int(im["x1"] * sc), int(im["bottom"] * sc)))
                        bio = io.BytesIO()
                        crop.save(bio, "PNG")
                        imgs.append(((im["x0"], im["top"], im["x1"], im["bottom"]), bio.getvalue()))
                except Exception:
                    pass
            pages.append({"words": words, "images": imgs})
    return pages


def read_pdf(raw: bytes):
    """pdfplumber가 있으면 그것으로(테스트와 같은 방식), 없으면 PyMuPDF로 읽는다."""
    try:
        import pdfplumber  # noqa: F401
    except Exception:
        return read_pdf_fitz(raw)
    return read_pdf_plumber(raw)


def read_pdf_fitz(raw: bytes):
    """PyMuPDF로 단어(위치 포함)와 그림문자 후보 이미지를 읽는다."""
    import fitz  # PyMuPDF
    pages = []
    with fitz.open(stream=raw, filetype="pdf") as doc:
        for p in doc:
            lines = []
            try:
                d = p.get_text("rawdict")
                for blk in d.get("blocks", []):
                    if blk.get("type") != 0:
                        continue
                    for ln in blk.get("lines", []):
                        chs = []
                        for sp in ln.get("spans", []):
                            for ch in sp.get("chars", []):
                                bb = ch["bbox"]
                                chs.append({"c": ch["c"], "x0": bb[0], "x1": bb[2], "base": ch["origin"][1], "size": sp.get("size", 10)})
                        if chs:
                            lines.append(chs)
                words = chars_to_words(lines)
            except Exception:
                words = []
            if not words:
                words = [(w[0], w[1], w[2], w[3], w[4]) for w in p.get_text("words")]
            imgs = []
            try:
                infos = p.get_image_info()
            except Exception:
                infos = []
            for info in infos:
                bb = fitz.Rect(info["bbox"])
                w, h = bb.width, bb.height
                if h > 0 and 14 < h < 130 and 14 < w < 420 and 0.7 < w / h < 5.5:
                    pix = p.get_pixmap(clip=bb, dpi=144, alpha=False)
                    imgs.append(((bb.x0, bb.y0, bb.x1, bb.y1), pix.tobytes("png")))
            pages.append({"words": words, "images": imgs})
    return pages


def undouble_cell(t):
    """겹쳐 찍혀 글자가 두 번씩 읽힌 줄(예: 'HH331177 알알레레르르기기')은 두 글자씩 하나로 복원한다."""
    ns = re.sub(r"\s+", "", t)
    if len(ns) < 6:
        return t
    pairs = len(re.findall(r"(.)\1", ns))
    if pairs >= 0.35 * len(ns) / 1.0 * 1.0 and pairs * 2 >= 0.6 * len(ns):
        return re.sub(r"(.)\1", r"\1", t)
    return t


def build_cells(words, tol=3.5, gap=16):
    """같은 높이의 단어를 한 줄로 묶되, 칸(표의 열) 사이의 넓은 간격에서는 나눈다."""
    ws = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows = []
    for w in ws:
        cy = (w[1] + w[3]) / 2
        if rows and abs(rows[-1][0] - cy) <= tol:
            rows[-1][1].append(w)
        else:
            rows.append([cy, [w]])
    out = []
    for cy, grp in rows:
        grp.sort(key=lambda w: w[0])
        cur = [grp[0]]
        for w in grp[1:]:
            if w[0] - cur[-1][2] > gap:
                out.append(cur); cur = [w]
            else:
                cur.append(w)
        out.append(cur)
        # y를 맞추기 위해 같은 줄 표시
    cells = []
    for c in out:
        text = re.sub(r"\s+", " ", " ".join(w[4] for w in c)).strip()
        text = undouble_cell(text)
        if text:
            cells.append({"y": (c[0][1] + c[0][3]) / 2, "x": c[0][0], "x1": c[-1][2], "text": text})
    merged = []
    for cell in cells:     # '(분진·흄…스프레이' + ')의 흡입을 피하시오.' 처럼 괄호가 칸 사이에서 끊긴 경우
        if merged and abs(merged[-1]["y"] - cell["y"]) <= tol and cell["x"] - merged[-1]["x1"] < 90 \
                and (merged[-1]["text"].count("(") > merged[-1]["text"].count(")") or re.match(r"^\)", cell["text"])):
            merged[-1]["text"] = (merged[-1]["text"] + " " + cell["text"]).strip()
            merged[-1]["x1"] = cell["x1"]
        else:
            merged.append(cell)
    for cell in merged:        # 원본의 늘어난 글자 간격 때문에 생긴 '씻으시오 .' ' ( 관련' 같은 공백 정리(글자는 그대로)
        cell["text"] = re.sub(r"([(\[（])\s+", r"\1", re.sub(r"\s+([.,;:)\]）])", r"\1", cell["text"]))
    return merged


def _undouble_heading(text):
    """글자가 겹쳐 두 번씩 찍힌 제목(예: '66.. 누누출출사사고고시시')을 복원한다."""
    if len(re.findall(r"([가-힣])\1", text)) < 2:
        return text
    t = re.sub(r"([가-힣])\1", r"\1", text)
    m = re.match(r"^\s*(\d)\1(\d)\2(.*)$", t)
    if m:
        return m.group(1) + m.group(2) + ". " + re.sub(r"^[\s.]*", "", m.group(3)).lstrip("0123456789")
    m = re.match(r"^\s*(\d)\1{0,2}[\s.]*(.*)$", t)
    if m:
        return m.group(1) + ". " + m.group(2).lstrip("0123456789 .")
    return t


def _is_heading(text, cur):
    text = _undouble_heading(text)
    m = HEAD.match(text)
    if not m:
        return 0
    n = int(m.group(1))
    if n <= cur or n > cur + 3:
        return 0
    rest = re.sub(r"\s+", "", m.group(2))
    if re.search(SEC_KEYS[n], rest) or re.search(EN_KEYS[n], m.group(2), re.I):
        return n
    if n == cur + 1 and len(text) < 70 and re.search(r"[가-힣]", rest[:30]):   # 글자가 겹쳐 깨진 제목 대비
        return n
    return 0


def parse_sections(pages):
    """항(1~16)별 항목 리스트. 항목: {kind: sub|grp|item, text, sub, grp, page, y}"""
    # 머리글/바닥글(여러 쪽에 반복되는 줄) 제거 준비
    per_page = []
    freq = {}
    for pg in pages:
        cs = build_cells(pg["words"])
        per_page.append(cs)
        for t in {re.sub(r"[\d\s]+", "", c["text"]) for c in cs}:
            freq[t] = freq.get(t, 0) + 1
    rep = max(3, int(len(pages) * 0.6))
    allx1 = sorted(c["x1"] for cs in per_page for c in cs)
    right = allx1[int(len(allx1) * 0.93)] if allx1 else 540
    sections = {n: [] for n in range(1, 17)}
    secpos, cur, sub, grp, ckey = {}, 0, "", "", ""
    for pi, cs in enumerate(per_page):
        prev = None
        for c in cs:
            t = c["text"]
            key = re.sub(r"[\d\s]+", "", t)
            if (PAGEPAT.match(t) and not (cur == 1 and re.fullmatch(r"\d{1,6}", t))) or (freq.get(key, 0) >= rep and len(pages) >= 3 and cur >= 2 and not HEAD.match(t)):
                continue
            n = _is_heading(t, cur)
            if n:
                cur, sub, grp, ckey = n, "", "", ""
                secpos[n] = (pi, c["y"])
                prev = None
                continue
            if cur == 0:
                continue
            ents = sections[cur]
            base = {"page": pi, "y": c["y"]}
            # 소제목(키워드 기준) — 왼쪽 칸 제목과 오른쪽 칸 내용이 한 칸으로 합쳐져도 분리
            hit = None
            t = re.sub(r"^\s*[●○◎■□▪•·ㆍ※*▶▷◆◇-]+\s*", "", t) if cur in SUBKW else t
            for rx, name in SUBKW.get(cur, []):
                m = re.match(r"^\s*(?:(?:\d{1,2}\.\d{1,2}\.?|[가-하a-z]\s*[.．)]|\(\d{1,2}\))\s*)?(" + rx.pattern + r")\s*(.*)$", t)
                if m:
                    hit = (name, m.group(1).strip(), (m.group(m.lastindex) or "").strip())
                    break
            if hit:
                sub, grp, ckey = hit[1], "", hit[0]
                ents.append({**base, "kind": "sub", "text": hit[1], "sub": sub, "grp": "", "key": hit[0]})
                rest = MARK.sub("", hit[2].lstrip(" :：-")).strip() if hit[2] else ""
                if len(rest) >= 4 and re.search(r"[가-힣A-Za-z]", rest):
                    ents.append({**base, "kind": "item", "text": rest, "sub": sub, "grp": "", "x1": c["x1"], "key": hit[0]})
                prev = ents[-1] if ents[-1]["kind"] == "item" else None
                continue
            ms = re.match(r"^\s*([가-하])\s*[.．]\s*(\S.*)$", t)
            if ms and cur not in SUBKW:
                sub, grp, ckey = ms.group(2).strip(), "", ""
                ents.append({**base, "kind": "sub", "text": sub, "sub": sub, "grp": "", "key": ""})
                prev = None
                continue
            mg = re.match(r"^\s*[○〇◎●■□▪]\s*(\S.*)$", t)
            if mg and len(mg.group(1)) < 40 and not CODE.match(mg.group(1)):
                grp = mg.group(1).strip()
                ents.append({**base, "kind": "grp", "text": grp, "sub": sub, "grp": grp})
                prev = None
                continue
            has_mark = bool(MARK.match(t)) or bool(CODE.match(t))
            body = MARK.sub("", t).strip() if MARK.match(t) and not CODE.match(t) else t
            frag = bool(re.match(r"^(?:시오|십시오|오\W|시\s?오)", body))          # 줄바꿈으로 잘린 어미('제거하' + '시오.')
            dang = (prev is not None and 0 <= c["y"] - prev["y"] <= 16
                    and not re.search(r"[.다음함임것오요)\]]\s*$", prev["text"])
                    and (frag or re.search(r"[하시으이를을에로고며는은의과와]\s*$", prev["text"])))
            if (prev is not None and not has_mark and prev["page"] == pi and prev["sub"] == sub
                    and not TERM.search(prev["text"])
                    and (prev.get("x1", 0) >= right - 120 or re.match(r"^[을를이가은는의에도로과와]\s", body) or dang)):
                prev["text"] = (prev["text"] + ("" if (re.match(r"^[을를이가은는의에도로과와]\s", body) or frag) else " ") + body).strip()
                prev["x1"] = c["x1"]
                prev["joined"] = True
                continue
            if len(body) < 2 or not re.search(r"[가-힣A-Za-z0-9]", body):
                continue
            e = {**base, "kind": "item", "text": body, "sub": sub, "grp": grp, "x1": c["x1"], "key": ckey}
            ents.append(e)
            prev = e
    for n in (4, 5, 6, 7, 8):          # '문장. 문장.' 로 이어진 한 칸은 문장 경계에서만 나눈다(글자는 그대로)
        out = []
        for e in sections[n]:
            if e["kind"] == "item" and len(e["text"]) > 50:
                parts = re.split(r"(?<=[다요음함임것오됨])\.\s+(?=[가-힣A-Za-z(])", e["text"])
                if len(parts) > 1:
                    for j, ptxt in enumerate(parts):
                        ptxt = ptxt.strip()
                        if j < len(parts) - 1 and not ptxt.endswith("."):
                            ptxt += "."
                        out.append({**e, "text": ptxt})
                    continue
            out.append(e)
        sections[n] = out
    return sections, secpos


# ───────────────────────── 2. 항목 추출 ─────────────────────────
def norm(t: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t or "")


def damaged(t):
    """줄바꿈·겹침으로 문장이 뒤섞였을 가능성이 있는 문장(예: '배치하시 용기는')."""
    if re.search(r"(?:하|되|으|리)시 (?=[가-힣])", t):
        return True
    t2 = t.strip()
    return t2.startswith(")") or t2.count("(") != t2.count(")") or t2.count("[") != t2.count("]")


def clean_items(texts):
    out, seen = [], set()
    for t in texts:
        t = re.sub(r"\s+", " ", t).strip()
        t = re.sub(r"([가-힣]) (시오|십시오)(?=[\s.,]|$)", r"\1\2", t)
        t = re.sub(r"시 오(?=[\s.,\[\]]|$)", "시오", t)
        if len(t) < 4 or re.fullmatch(r"(자료\s*없음|해당\s*없음|없음)\.?", t) or norm(t) in seen:
            continue
        seen.add(norm(t))
        out.append(t)
    return out


def sub_items(ents, pat):
    return clean_items([e["text"] for e in ents if e["kind"] == "item" and e["sub"] and (re.search(pat, e["sub"]) or re.search(pat, e.get("key", "") or ""))])


PHONE = re.compile(r"((?:0\d{1,2}\s*[-).]?\s*\d{3,4}\s*[-.]\s*\d{4})|(?<!\d)(?:1[5-9]\d{2}\s*-\s*\d{4})(?!\d))")


LEAD = re.compile(r"^\s*(?:(?:\d{1,2}\.\d{1,2}\.?|[가-하a-z]\s*[.)]|\(\s*\d{1,2}\s*\)|\d{1,2}\))\s*)+")
LABELS = re.compile(r"^(?:제\s*품\s*명|제품의|회사명|업체명|공급자|제조자|제조사|수입자|유통|주\s*소|소\s*재\s*지|긴급|전화|연락|용도|사용상|홈페이지|이메일|e-?mail|팩스|FAX|TEL|기타\s*식별|관련\s*용도|금지된)", re.I)
NAMELBL = re.compile(r"^(?:회\s*사\s*명|업\s*체\s*명|공급자\s*명|제조자\s*명|제조사\s*명|수입자\s*명|상\s*호|제조/?공급자\s*명)\s*[:：]?\s*")
ADDRLBL = re.compile(r"^(?:주\s*소|소\s*재\s*지|본\s*사|공\s*장|사업장)\s*[:：]?\s*")
COREG = re.compile(r"(주식회사|\(주\)|㈜|유한회사|\(유\)|Co\.?,?\s*Ltd|Corp|Inc|코리아|케미칼|페인트|화학|산업|공업|상사)", re.I)


def extract_info(s1):
    info = {"product": "", "company": "", "address": "", "phone": ""}
    texts = [LEAD.sub("", e["text"]).strip() for e in s1 if e["kind"] != "grp" or True]
    n = len(texts)

    def nextval(i):
        if i + 1 < n:
            nx = texts[i + 1]
            if nx and not LABELS.match(nx) and not NAMELBL.match(nx) and not ADDRLBL.match(nx):
                return nx.strip(" :：-")
        return ""

    raw = [e["text"] for e in s1]
    for i, t0 in enumerate(raw):          # '제품 이름 앞부분이 제목 칸에 붙어 나온' 경우: 'KR-400(L)a. 제품명' + 'NEOFORCE'
        m = re.match(r"^(\S.*?)\s*[a-z가-하]\s*[.)]\s*제\s*품\s*명\s*$", t0)
        if m and not info["product"] and i + 1 < len(raw):
            nv = LEAD.sub("", raw[i + 1]).strip(" :：-")
            if nv and not LABELS.match(nv):
                info["product"] = f"{nv} {m.group(1).strip()}"
    for i, t in enumerate(texts):
        if not info["product"]:
            m = re.match(r"^제\s*품\s*명\s*[:：]?\s*", t)
            if m:
                v = t[m.end():].strip() or nextval(i)
                if v and not re.match(r"^(기타\s*식별|제품의)", v):
                    info["product"] = v
        if not info["company"]:
            m = NAMELBL.match(t)
            if m:
                v = t[m.end():].strip() or nextval(i)
                if v:
                    info["company"] = v
        if not info["address"]:
            m = ADDRLBL.match(t)
            if m:
                v = t[m.end():].strip() or nextval(i)
                if v:
                    info["address"] = v
        if not info["phone"] and re.search(r"긴급|전화|TEL|연락", t, re.I):
            m = PHONE.search(t) or (PHONE.search(texts[i + 1]) if i + 1 < n else None)
            if m:
                info["phone"] = re.sub(r"\s+", "", m.group(1))
    if not info["company"]:      # '공급자 정보' 제목 바로 뒤에 회사명이 오는 서식
        for i, t in enumerate(texts):
            if re.match(r"^(?:제조자|공급자|제조/?공급자|공급자\s*/\s*유통|수입)", t) and re.search(r"정보", t):
                for nx in texts[i + 1:i + 4]:
                    if COREG.search(nx) and not LABELS.match(nx) and len(nx) < 60:
                        info["company"] = nx; break
                if info["company"]:
                    break
    if not info["company"]:
        for t in texts:
            if COREG.search(t) and not LABELS.match(t) and len(t) < 45 and re.search(r"주식회사|\(주\)|㈜|유한회사|Co\.", t):
                info["company"] = t; break
    if not info["phone"]:
        for t in texts:
            m = PHONE.search(t)
            if m:
                info["phone"] = re.sub(r"\s+", "", m.group(1)); break
    if not info["phone"]:
        for t in texts:
            m = re.search(r"(\+82[\s-]*\d{1,2}[\s-]*\d{3,4}[\s-]*\d{4})", t)
            if m:
                info["phone"] = m.group(1).strip(); break
    if not info["address"]:
        for t in texts:
            if re.search(r"[가-힣]+(?:시|군|구)\s.*(?:로|길|동|읍|면)\s*\d", t) and len(t) < 90:
                info["address"] = t; break
    c0 = info["company"]          # '마페이코리아 : (우) 31251 충청남도 … 46 전화: … 팩스: …' 처럼 한 칸에 붙은 경우 나눈다
    mm = re.search(r"\s*[:：]?\s*(?=\(우\)|\(?\d{5}\)\s|\d{5}\s+[가-힣]+(?:시|도|군|구))", c0)
    if mm and mm.start() > 0:
        rest = c0[mm.end():]
        info["company"] = c0[:mm.start()].strip(" :：")
        if not info["address"]:
            info["address"] = re.sub(r"\s*(?:전화|팩스|TEL|FAX|Tel|Fax).*$", "", rest).strip()
    info["company"] = re.sub(r"\s*(?:전화|팩스|TEL|FAX)\s*[:：].*$", "", info["company"], flags=re.I).strip(" :：")
    if re.match(r"^\d{2,4}\)", info["phone"]):
        info["phone"] = "(" + info["phone"]
    info["address"] = re.sub(r"\s*(?:전화|팩스|TEL|FAX)\s*[:：].*$", "", info["address"], flags=re.I).strip()
    info["company"] = re.sub(r"\s*(?:주\s*소|본사\s*및\s*지방영업소|긴급\s*전화.*)\s*$", "", info["company"]).strip()
    info["address"] = re.sub(r"\s*(?:긴급\s*전화\s*번호?|전화\s*번호?|TEL).*$", "", info["address"], flags=re.I).strip()
    for k in info:
        info[k] = re.sub(r"\s+", " ", info[k]).strip()
    return info


CLS_VAL = re.compile(r"(구분|등급|액화가스|압축가스|용해가스|심냉|해당\s*없음|분류되지|분류\s*불가)")


def _rev_lookup(D, kind, body):
    """코드 없이 문장만 인쇄된 경우: 사전의 표준 문장과 대조해 코드를 찾는다(없으면 '')."""
    if D is None:
        return ""
    nb = norm(body)
    for code, v in D.codes.items():
        if code.startswith(kind) and norm(v.get("ko", "")) == nb:
            return code
    return ""


def extract_sec2(s2, D=None):
    classes, hs, ps, signal = [], [], [], ""
    region, mode, pgrp, pend = "pre", "", "prev", None
    blob = " ".join(e["text"] for e in s2)
    m = re.search(r"신호\s*어\s*[:：]?\s*(위험|경고|없음|해당\s*없음)", blob)
    if m:
        signal = re.sub(r"\s+", "", m.group(1))
    else:
        for i, e in enumerate(s2):
            if re.search(r"신호\s*어", e["text"]):
                nxt = " ".join(x["text"] for x in s2[i:i + 3])
                m2 = re.search(r"(위험|경고|없음)", nxt)
                if m2:
                    signal = m2.group(1)
                break
    for e in s2:
        t = e["text"]
        if re.search(r"예방조치문구를\s*포함한\s*경고\s*표지|경고\s*표지\s*(요소|항목)", t):
            region = "label"
            continue
        if re.match(r"^\W*유해\W*위험\s*문구", t):
            mode = "H"; region = "label"
            rest = re.sub(r"^\W*유해\W*위험\s*문구\s*[:：]?", "", t).strip()
            if not rest:
                continue
            t = rest
        elif re.match(r"^\W*예방\s*조치\s*문구", t):
            mode = "P"; pgrp = "prev"; region = "label"
            rest = re.sub(r"^\W*예방\s*조치\s*문구\s*[:：]?", "", t).strip()
            if not rest:
                continue
            t = rest
        elif re.match(r"^\W*(?:그림문자|신호\s*어)", t):
            mode = ""
            continue
        elif re.search(r"분류\s*기준에\s*포함되지|분류되지\s*않은\s*기타\s*위험|NFPA|^\W*[가-하a-z]\s*[.．)]\s*\S", t) and mode:
            mode = ""
            continue
        mp = re.match(r"^\W*(예방|대응|저장|폐기)\s*[-–:：]\s*(\S.*)$", t)
        if mp and CODE.match(mp.group(2)):
            pgrp = {"예방": "prev", "대응": "resp", "저장": "stor", "폐기": "disp"}[mp.group(1)]
            t = mp.group(2)
        mg = re.match(r"^\W*\(?\s*(예방|대응|저장|폐기)\s*\)?\W*$", t)
        if mg:
            pgrp = {"예방": "prev", "대응": "resp", "저장": "stor", "폐기": "disp"}[mg.group(1)]
            continue
        if pend and e["kind"] == "item" and not CODE.match(t):
            body = MARK.sub("", t).strip()
            (hs if pend.startswith("H") else ps).append({"code": pend, "ko": body, "text": f"{pend} {body}"})
            pend = None
            continue
        mc = CODE.match(t)
        if mc:
            code = re.sub(r"\s+", "", mc.group(1)).upper()
            body = mc.group(2).strip()
            mp2 = re.match(r"^\+\s*([HP]?\d{2,3}[A-Za-z]?)\s+(.*)$", body)      # 'P305+P351 +P33' + 줄바꿈된 '8'
            if mp2:
                part = mp2.group(1).upper()
                code += "+" + (part if part[0] in "HP" else code[0] + part)
                body = mp2.group(2).strip()
            if D is not None and code not in D.codes:
                cand = [k for k in D.codes if k.startswith(code) and len(k) == len(code) + 1]
                if len(cand) == 1:
                    code = cand[0]
            if body:
                (hs if code.startswith("H") else ps).append({"code": code, "ko": body, "text": f"{code} {body}"})
                pend = None
            else:
                pend = code
            continue
        pend = None
        mt = re.search(r"^(.*?)\s*\(\s*([HP]\d{3}[A-Za-z]?)\s*\)\s*$", t)
        if mt and mt.group(1):
            code = mt.group(2).upper()
            (hs if code.startswith("H") else ps).append({"code": code, "ko": mt.group(1).strip(), "text": f"{code} {mt.group(1).strip()}"})
            continue
        if mode and region == "label" and e["kind"] in ("item", "grp") and len(t) >= 6:
            body = MARK.sub("", t).strip()
            if mode == "H":
                code = _rev_lookup(D, "H", body) or "H999"
                hs.append({"code": code, "ko": body, "text": body if code == "H999" else f"{code} {body}", "nocode": True})
            else:
                if not re.search(r"(오|것|다|라|요|음|함|임|시오|바람)\W*$", body):
                    continue        # 제품명·NFPA 표 등 문장이 아닌 줄은 예방조치문구로 보지 않는다
                code = _rev_lookup(D, "P", body) or "P%d99" % {"prev": 1, "resp": 3, "stor": 4, "disp": 5}[pgrp]
                ps.append({"code": code, "ko": body, "text": body if code.endswith("99") else f"{code} {body}", "nocode": True})
            continue
        if region == "pre" and e["kind"] == "item" and CLS_VAL.search(t) and len(t) < 120:
            classes.append(re.sub(r"\s*[:：]\s*", " : ", t))

    def dedup(lst):
        seen, out = set(), []
        for x in lst:
            key = x["code"] if not x["code"].endswith("99") else norm(x["ko"])
            if key not in seen:
                seen.add(key); out.append(x)
        return out
    return {"classes": clean_items(classes), "H": dedup(hs), "P": dedup(ps), "signal": signal}


# ── 그림문자: 원본에 인쇄된 그림을 기준 그림 9종과 대조 ──
def _arr(img):
    import numpy as np
    from PIL import Image
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[3])
        img = bg
    from PIL import ImageChops
    rgb = img.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, "white")).convert("L").point(lambda v: 255 if v > 40 else 0)
    bb = diff.getbbox()
    if bb:                               # 여백 제거 후 정사각형으로
        rgb = rgb.crop(bb)
        w, h = rgb.size
        side = max(w, h)
        sq = Image.new("RGB", (side, side), "white")
        sq.paste(rgb, ((side - w) // 2, (side - h) // 2))
        rgb = sq
    from PIL import ImageFilter
    g = rgb.resize((64, 64)).convert("L")
    full = np.asarray(g.resize((32, 32)).filter(ImageFilter.GaussianBlur(1.0)), dtype=float) / 255.0
    inner = np.asarray(g.crop((14, 14, 50, 50)).resize((32, 32)).filter(ImageFilter.GaussianBlur(0.8)), dtype=float) / 255.0
    return (full - full.mean(), inner - inner.mean())


def _corr1(a, b):
    import numpy as np
    return float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-9))


def _corr(a, b):
    """전체 모양과 안쪽 그림을 함께 비교한 유사도(0~1)."""
    return 0.5 * _corr1(a[0], b[0]) + 0.5 * _corr1(a[1], b[1])


def _squares(png):
    """이미지를 정사각형 조각들로 만든다(가로로 이어붙인 그림문자 띠는 개수만큼 분할, 비정방형은 흰 여백으로 정사각형화)."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    r = w / h
    parts = []
    if r >= 1.6:
        import numpy as np
        from PIL import ImageChops
        d = np.asarray(ImageChops.difference(im, Image.new("RGB", im.size, "white")).convert("L")) > 40
        cols = d.any(axis=0)
        segs, start = [], None
        for x, on in enumerate(list(cols) + [False]):
            if on and start is None:
                start = x
            elif not on and start is not None:
                if segs and start - segs[-1][1] <= 2:          # 아주 좁은 틈은 같은 그림으로 본다
                    segs[-1] = (segs[-1][0], x)
                else:
                    segs.append((start, x))
                start = None
        segs = [sg for sg in segs if sg[1] - sg[0] >= 0.5 * h]
        if len(segs) >= 2:
            parts = [im.crop((a, 0, b, h)) for a, b in segs]
        else:
            n = max(2, round(r))
            pw = w / n
            parts = [im.crop((int(i * pw), 0, int((i + 1) * pw), h)) for i in range(n)]
    else:
        parts = [im]
    out = []
    for p in parts:
        pw, ph = p.size
        side = max(pw, ph)
        bg = Image.new("RGB", (side, side), "white")
        bg.paste(p, ((side - pw) // 2, (side - ph) // 2))
        out.append(bg)
    return out


def _load_refs():
    """공식 그림 + 실제 MSDS에서 모은 견본들. 그림문자별로 여러 견본의 배열을 돌려준다."""
    from PIL import Image
    from msds_assets import GHS_ASSETS
    try:
        from msds_assets import GHS_EXTRA
    except Exception:
        GHS_EXTRA = {}
    refs = {k: [_arr(Image.open(io.BytesIO(base64.b64decode(v))))] for k, v in GHS_ASSETS.items()}
    for k, lst in GHS_EXTRA.items():
        for v in lst:
            refs[k].append(_arr(Image.open(io.BytesIO(base64.b64decode(v)))))
    return refs


def _rank(a, refs):
    return sorted(((max(_corr(a, r) for r in lst), k) for k, lst in refs.items()), reverse=True)


def _strip_match(png, refs):
    """가로로 이어붙은 그림문자 띠: 정사각 창을 밀어 가며 기준 그림과 대조한다."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    import numpy as np
    from PIL import ImageChops
    ink = np.asarray(ImageChops.difference(im, Image.new("RGB", im.size, "white")).convert("L")) > 40
    xs = np.where(ink.any(axis=0))[0]
    xa, xb = (int(xs[0]), int(xs[-1]) + 1) if len(xs) else (0, w)
    n = max(2, round((xb - xa) / (0.97 * h)))
    found = []
    for i in range(n):                       # 그림 수만큼 같은 간격으로 나눠 가운데를 정사각형으로 자른다
        cx = xa + (i + 0.5) * (xb - xa) / n
        x0 = int(max(0, min(w - h, cx - h / 2)))
        a = _arr(im.crop((x0, 0, x0 + h, h)))
        sc = _rank(a, refs)
        (s1, k1), (s2, _) = sc[0], sc[1]
        found.append(k1 if (s1 >= 0.62 and s1 - s2 >= 0.10) else None)
    if all(found):
        return found
    picks = []
    for x in range(0, max(1, w - h + 1), max(2, h // 20)):
        a = _arr(im.crop((x, 0, min(w, x + h), h)))
        sc = _rank(a, refs)
        (s1, k1), (s2, _) = sc[0], sc[1]
        if s1 >= 0.62 and s1 - s2 >= 0.10:
            picks.append((s1, x, k1))
    chosen = []
    for s1, x, k1 in sorted(picks, reverse=True):
        if all(abs(x - cx) > 0.6 * h for _, cx, _ in chosen):
            chosen.append((s1, x, k1))
    return [k for _, x, k in sorted(chosen, key=lambda t: t[1])]


def match_pictograms(imgs):
    """imgs: [(bbox, png_bytes)] → (['GHS02',...], [미식별 개수])"""
    from PIL import Image
    refs = _load_refs()
    found, unknown = [], 0
    for bbox, png in sorted(imgs, key=lambda t: (round(t[0][1] / 25), t[0][0])):
        w0, h0 = Image.open(io.BytesIO(png)).size
        if w0 / h0 >= 1.6:
            for k in _strip_match(png, refs):
                if k not in found:
                    found.append(k)
            continue
        im = _squares(png)[0]
        a = _arr(im)
        sc = _rank(a, refs)
        (s1, k1), (s2, _) = sc[0], sc[1]
        if s1 >= 0.62 and s1 - s2 >= 0.10:
            if k1 not in found:
                found.append(k1)
        elif s1 >= 0.45:
            unknown += 1
    return found, unknown


H_TO_GHS = {"H201": "GHS01", "H220": "GHS02", "H221": "GHS02", "H222": "GHS02", "H223": "GHS02", "H224": "GHS02",
            "H225": "GHS02", "H226": "GHS02", "H228": "GHS02", "H280": "GHS04", "H281": "GHS04", "H290": "GHS05",
            "H300": "GHS06", "H301": "GHS06", "H310": "GHS06", "H311": "GHS06", "H330": "GHS06", "H331": "GHS06",
            "H314": "GHS05", "H318": "GHS05", "H334": "GHS08", "H340": "GHS08", "H341": "GHS08", "H350": "GHS08",
            "H351": "GHS08", "H360": "GHS08", "H361": "GHS08", "H370": "GHS08", "H371": "GHS08", "H372": "GHS08",
            "H373": "GHS08", "H304": "GHS08", "H410": "GHS09", "H400": "GHS09"}


def ghs_consistency(hcodes, ghs):
    """H코드로 예상되는 주요 그림문자가 원본에 없으면 경고 문구를 돌려준다."""
    msgs = []
    for code in hcodes:
        g = H_TO_GHS.get(code.split("+")[0])
        if g and g not in ghs:
            msgs.append(f"{code}는 보통 {g} 그림문자에 해당하지만 원본 MSDS 2항에는 인쇄되어 있지 않습니다. 원본 확인 필요")
    return list(dict.fromkeys(msgs))


# ───────────────────────── 3. 항목 선택 (원문 그대로 선택만) ─────────────────────────
def actionable(t):
    return bool(re.search(r"시오|할 ?것|말 ?것|하라|[을를] ?것\W*$|것\W*$|하십시오|바랍니다", t))


def pick(cands, n, skip=None):
    good = [c for c in cands if actionable(c) and not (skip and re.search(skip, c))]
    good = [c for c in good if not damaged(c)] + [c for c in good if damaged(c)]   # 훼손 의심 문장은 뒤로
    if not good:
        good = [c for c in cands if not (skip and re.search(skip, c))] or cands
    return good[:n]


def build_candidates(sections):
    s4, s5, s6, s7, s8 = (sections[i] for i in (4, 5, 6, 7, 8))
    inc_primary = sub_items(s6, r"인체")
    inc_pool = clean_items(inc_primary + sub_items(s5, r"진압") + sub_items(s5, r"소화제")
                           + sub_items(s6, r"정화|제거") + sub_items(s6, r"환경"))
    cands = {
        "handling": sub_items(s7, r"취급"), "storage": sub_items(s7, r"저장"),
        "eye": sub_items(s4, r"눈"), "skin": sub_items(s4, r"피부"),
        "inhale": sub_items(s4, r"흡입"), "ingest": sub_items(s4, r"먹|삼|섭취"),
        "incident": inc_pool,
    }
    defaults = {k: pick(v, LIMITS[k][0], skip=(r"토하게|삼켰" if k == "inhale" else None)) for k, v in cands.items()}
    prim = pick(inc_primary, LIMITS["incident"][0])
    if len(prim) < LIMITS["incident"][0]:
        prim = pick(inc_pool, LIMITS["incident"][0])
    defaults["incident"] = prim
    return cands, defaults


def detect_ppe(s8):
    """8항 '개인 보호구'(호흡기 보호) 문구 기준: 방독→방독마스크, 방진→방진마스크, 그 외(공단 인증 호흡용보호구 등)→방진마스크."""
    txt = " ".join(e["text"] for e in s8 if e["kind"] == "item" and e["sub"] and re.search(r"보호구|보호\s*장비|보호\s*조치", e["sub"]))
    if not txt:
        txt = " ".join(e["text"] for e in s8 if e["kind"] == "item")
    if "방독" in txt:
        return "gas", "8항 호흡기 보호에 '방독마스크' 기재 → 방독마스크 그림"
    if re.search(r"방진|분진\s*마스크", txt):
        return "dust", "8항 호흡기 보호에 '방진마스크' 기재 → 방진마스크 그림"
    if re.search(r"호흡용\s*보호구|호흡\s*보호구|호흡기\s*보호", txt):
        return "dust", "8항에 방독/방진 구분 없이 '공단 인증 호흡용보호구'만 기재 → 방진마스크 그림(규칙)"
    return "dust", "8항에서 호흡 보호구 문구를 찾지 못해 방진마스크 그림 적용 → 확인 필요"


def fix_joined(sections, D):
    """줄바꿈으로 이어 붙인 문장은 단어 중간에서 끊겼을 수 있다. 사전의 표준 문구와 같으면 표준 표기(공백)로 복원."""
    for ents in sections.values():
        for e in ents:
            if not e.get("joined"):
                continue
            m = CODE.match(e["text"])
            body = m.group(2) if m else e["text"]
            hit = D.sent.get(norm(body))
            if hit and hit.get("ko") and re.sub(r"\s+", "", hit["ko"]) == re.sub(r"\s+", "", body):   # 공백만 다를 때만 표준 표기 사용(문자·부호는 원본 유지)
                e["text"] = (m.group(1) + " " if m else "") + hit["ko"]



# ── 경고표지: 유해·위험문구 최대 4개, 예방/대응/저장/폐기 각 최대 3개 (원본에 더 적으면 있는 만큼만) ──
LABEL_LIMITS = {"H": 4, "prev": 3, "resp": 3, "stor": 3, "disp": 3}
LABEL_GROUPS = [("prev", "예방"), ("resp", "대응"), ("stor", "저장"), ("disp", "폐기")]
P_PRIORITY = {
    "prev": ["P210", "P202", "P280", "P261", "P201"],
    "resp": ["P301+P310", "P305+P351+P338", "P303+P361+P353", "P304+P340", "P302+P352", "P308+P313"],
    "stor": ["P403+P233", "P410+P403", "P401", "P405"],
    "disp": ["P501"],
}


def p_group(code):
    return {"1": "prev", "2": "prev", "3": "resp", "4": "stor", "5": "disp"}.get(code[1], "prev")


def h_score(code):
    c = code.split("+")[0]
    if c in ("H300", "H310", "H330"):
        return 100
    if c in ("H304", "H314", "H318", "H334", "H340", "H350", "H360", "H370", "H372"):
        return 90
    if c in ("H200", "H201", "H202", "H203", "H204", "H205", "H220", "H222", "H224"):
        return 85
    if c in ("H225", "H229", "H280", "H281", "H290", "H301", "H311", "H331", "H341", "H351", "H361", "H371", "H373"):
        return 65
    if c in ("H302", "H312", "H332", "H226", "H228"):
        return 50
    if c.startswith("H4"):
        return 30
    return 40


def default_label_selection(H, P):
    """(H 인덱스 목록, {그룹: P 인덱스 목록}) - 위험도/우선순위 기준, 문서 순서로 반환."""
    n = LABEL_LIMITS["H"]
    rank = lambda i: (-h_score(H[i]["code"]), i)
    phys = [i for i, h in enumerate(H) if h["code"].startswith("H2")]
    oth = [i for i in range(len(H)) if i not in phys]
    chosen = sorted(phys, key=rank)[:2] + sorted(oth, key=rank)[: n - 2]
    if len(chosen) < n:
        rest = sorted((i for i in range(len(H)) if i not in chosen), key=rank)
        chosen += rest[: n - len(chosen)]
    hsel = sorted(chosen)[:n]
    psel = {}
    for g, _ in LABEL_GROUPS:
        idx = [i for i, p in enumerate(P) if p_group(p["code"]) == g]
        pr = P_PRIORITY[g]
        idx.sort(key=lambda i: (pr.index(P[i]["code"]) if P[i]["code"] in pr else 99, i))
        psel[g] = sorted(idx[: LABEL_LIMITS[g]])
    return hsel, psel


def analyze(pages, D=None):
    sections, secpos = parse_sections(pages)
    fix_joined(sections, D or Dictionary())
    info = extract_info(sections[1])
    DD = D or Dictionary()
    s2 = extract_sec2(sections[2], DD)
    for h in s2["H"] + s2["P"]:      # 표준 문구와 글자가 같고 띄어쓰기·괄호만 다르면 표준 표기로 복원
        std = DD.codes.get(h["code"], {}).get("ko")
        strip = lambda x: re.sub(r"[\s()（）]", "", x or "")
        if std and strip(std) == strip(h["ko"]) and std != h["ko"]:     # 띄어쓰기·괄호만 다를 때만(글자·부호는 원본 그대로)
            std = re.sub(r"([:.])(?=[가-힣])", r"\1 ", std)
            h["ko"] = std
            h["text"] = f"{h['code']} {std}"
    # 2항 범위 안의 그림문자 이미지
    imgs = []
    if 2 in secpos:
        start = secpos[2]
        end = secpos.get(3, (10 ** 6, 0))
        for pi, pg in enumerate(pages):
            for bbox, png in pg["images"]:
                pos = (pi, bbox[1])
                if start <= pos < end:
                    imgs.append((bbox, png))
    ghs, unknown = match_pictograms(imgs) if imgs else ([], 0)
    ghs_est = False
    if not ghs and s2["H"]:      # 그림 파일을 못 찾았을 때만 H코드로 추정(화면에 '추정' 표시)
        est = []
        for h in s2["H"]:
            for part in re.split(r"\+", h["code"]):
                g = H_TO_GHS.get(part if part.startswith("H") else "H" + part)
                if g and g not in est:
                    est.append(g)
        ghs, ghs_est = est, bool(est)
    cands, defaults = build_candidates(sections)
    ppe, ppe_reason = detect_ppe(sections[8])
    hsel, psel = default_label_selection(s2["H"], s2["P"])
    return {"label_default": {"H": hsel, **psel}, "info": info, "sec2": s2, "ghs": ghs, "ghs_unknown": unknown, "ghs_estimated": ghs_est,
            "ghs_warn": ghs_consistency([h["code"] for h in s2["H"]], ghs),
            "cands": cands, "defaults": defaults, "ppe": ppe, "ppe_reason": ppe_reason}


# ───────────────────────── 4. 번역 사전 ─────────────────────────
class Dictionary:
    def __init__(self):
        base = json.loads(DICT_PATH.read_text("utf-8"))
        self.meta = base["meta"]
        self.codes = {**base["H"], **base["P"]}
        self.sent = {norm(e["ko"]): e for e in base["S"]}
        for e in self.codes.values():
            self.sent.setdefault(norm(e["ko"]), e)
        self.terms = {norm(k): v for k, v in base["terms"].items()}
        for k, v in list(self.terms.items()):          # '급성독성-경피' 같은 표기 차이를 같은 용어로
            self.terms.setdefault(k.replace("급성독성", "급성독성"), v)
        for a, b in (("급성독성경피", "급성독성경피"),):
            pass
        self.values = {norm(k): v for k, v in base["values"].items()}
        self.ui_ = base["ui"]
        self.user = {"codes": {}, "sent": {}}
        if USER_DICT_PATH.exists():
            try:
                self.user = json.loads(USER_DICT_PATH.read_text("utf-8"))
            except Exception:
                pass
        for c, e in self.user.get("codes", {}).items():
            self.codes.setdefault(c, {}).update(e)
        for k, e in self.user.get("sent", {}).items():
            self.sent.setdefault(k, {}).update(e)

    def ui(self, key, lang):
        return self.ui_[key].get(lang, "")

    def tr(self, text, lang):
        """MSDS 원문 문장 1개의 번역. 사전에 없으면 None."""
        if lang == "ko":
            return ""
        m = CODE.match(text.strip())
        if m:
            code = re.sub(r"\s+", "", m.group(1)).upper()
            e = self.codes.get(code)
            if e and e.get(lang):
                return e[lang]
            e = self.sent.get(norm(m.group(2)))
            return e.get(lang) if e and e.get(lang) else None
        e = self.sent.get(norm(text))
        return e.get(lang) if e and e.get(lang) else None

    def tr_class(self, line, lang):
        """'폭발성 물질 : 등급1.1' 같은 분류 줄 번역. 구분자는 ':' '-' 공백 어느 것이든 읽는다."""
        if lang == "ko":
            return ""
        line = line.strip()
        e0 = self.sent.get(norm(line))
        if e0 and e0.get(lang):
            return e0[lang]
        # 용어가 뒤에 붙은 줄(": 구분 4 심한 눈 손상성/눈 자극성")과 한 줄에 여러 항목이 붙은 줄을 항목별로 번역
        line = re.sub(r"(구분\s*[0-9][0-9A-Za-z]*)\s*-\s*(?=[가-힣])", r"\1(", line) if re.search(r"구분\s*[0-9][0-9A-Za-z]*\s*-[가-힣]+(?:\s|$)", line) else line
        line = re.sub(r"(구분\s*[0-9][0-9A-Za-z]*\([가-힣 ]+?)(\s+(?=[가-힣]+\s*[:：-])|$)", lambda m: m.group(1) + ")" + m.group(2), line) if "(" in line and ")" not in line else line
        m0 = re.match(r"^[:：]\s*(구분|등급)\s*([0-9][0-9A-Za-z.]*)\s+(.+)$", line)
        if m0:
            line = f"{m0.group(3)} : {m0.group(1)} {m0.group(2)}"
        parts = re.findall(r"(.+?)\s*(?:[:：]|\s-\s)\s*((?:구분|등급)\s*[0-9][0-9A-Za-z.]*\s*(?:[(（][^)）]*[)）])?)\s*", line)
        if len(parts) > 1 and "".join(a + b for a, b in parts).replace(" ", "") != "" :
            outs = []
            for a, b in parts:
                t = self.tr_class(f"{a.strip()} : {b.strip()}", lang)
                if not t:
                    return None
                outs.append(t)
            return "; ".join(outs)
        m = re.match(r"^(.+?)\s*(?:[:：]|\s-\s|\s|-)\s*(구분|등급)\s*([0-9][0-9A-Za-z.]*)\s*(?:[(（](.+?)[)）])?\s*[,.]?\s*$", line)
        if m:
            term = self.terms.get(norm(m.group(1)))
            if not term or not term.get(lang):
                return None
            w = self.values[norm(m.group(2))].get(lang)
            num, par = m.group(3), m.group(4)
            if not w:
                return None
            val = f"类别{num}" if (lang == "zh" and m.group(2) == "구분") else (f"{num}项" if lang == "zh" else f"{w} {num}")
            if par:
                if re.match(r"^\s*ATE", par):
                    val += f" ({par.strip()})"
                else:
                    s = self.values.get(norm(par))
                    if not s or not s.get(lang):
                        return None
                    val += f" ({s[lang]})" if lang != "zh" else f"（{s[lang]}）"
            return f"{term[lang]}{'：' if lang == 'zh' else ': '}{val}"
        m = re.match(r"^(.+?)\s*[:：]\s*(.+)$", line)
        if not m:
            return None
        term = self.terms.get(norm(m.group(1)))
        if not term or not term.get(lang):
            return None
        e = self.values.get(norm(m.group(2)))
        if not e or not e.get(lang):
            return None
        return f"{term[lang]}{'：' if lang == 'zh' else ': '}{e[lang]}"

    def add_user(self, text, lang, translation):
        """사용자가 검수해 입력한 번역을 user_dictionary.json에 저장 (H/P코드는 코드 기준, 그 외는 문장 기준)."""
        m = CODE.match(text.strip())
        if m:
            code = re.sub(r"\s+", "", m.group(1)).upper()
            e = self.user.setdefault("codes", {}).setdefault(code, {"ko": m.group(2).strip()})
            e[lang] = translation
        else:
            e = self.user.setdefault("sent", {}).setdefault(norm(text), {"ko": text})
            e[lang] = translation
        USER_DICT_PATH.write_text(json.dumps(self.user, ensure_ascii=False, indent=1), "utf-8")


def find_missing(D: Dictionary, data, langs):
    """선택된 모든 문구 중 사전에 없는 것: [(종류, 한국어원문, 언어)]"""
    miss = []
    for lang in langs:
        for line in data["classes"]:
            if D.tr_class(line, lang) is None:
                miss.append(("분류", line, lang))
        texts = [h["text"] for h in data["H"]] + [p["text"] for p in data["P"]]
        for k in ("handling", "storage", "eye", "skin", "inhale", "ingest", "incident"):
            texts += data[k]
        for t in texts:
            if D.tr(t, lang) is None:
                miss.append(("문장", t, lang))
    seen, out = set(), []
    for k, t, l in miss:
        if (t, l) not in seen:
            seen.add((t, l))
            out.append((k, t, l))
    return out


# ───────────────────────── 5. HTML 생성 ─────────────────────────
def esc(x):
    return html.escape(str(x), quote=True)


CSS = """
@page{size:A4 portrait;margin:7mm}
*{box-sizing:border-box}
html,body{margin:0;padding:0;color:#111;background:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact;
 font-family:"Malgun Gothic","Noto Sans CJK KR","Noto Sans KR","Apple SD Gothic Neo","Segoe UI",Arial,sans-serif;font-size:10.5px;line-height:1.3}
.doc{width:196mm;margin:0 auto;break-after:page;page-break-after:always}
.doc:last-child{break-after:auto;page-break-after:auto}
[lang=zh-CN]{font-family:"Microsoft YaHei","Noto Sans CJK SC","PingFang SC","SimSun",sans-serif}
[lang=vi],[lang=ru],[lang=en]{font-family:"Segoe UI","Malgun Gothic","Noto Sans","Arial",sans-serif}
[lang=si]{font-family:"Nirmala UI","Iskoola Pota","Noto Sans Sinhala","Noto Serif Sinhala",sans-serif}
[lang=km]{font-family:"Khmer UI","Leelawadee UI","Noto Sans Khmer","DaunPenh","Khmer OS",sans-serif}
.lg{font-weight:700;font-size:12px;margin:0 0 .5mm}
h1{font-size:25px;text-align:center;margin:0 0 1.5mm;line-height:1.25}
h1 small{display:block;font-size:14px;color:#24467a;font-weight:700}
table{border-collapse:collapse;width:100%}
th,td{border:1.5px solid #222;padding:1.6mm 2.2mm;vertical-align:middle}
th{text-align:center;font-weight:700;font-size:12.5px;background:#fff}
th.sub{font-size:11.5px;width:11%}
td.ic{text-align:center;width:18%}
.it{break-inside:avoid;page-break-inside:avoid;margin:0 0 1.2mm}
.k{color:#111}
.g{font-size:12px}
.f{color:#24467a;font-size:9px}
.cls{}
.pic{display:flex;flex-direction:column;align-items:center;gap:1mm}
.pic img{display:block}
.shead{font-weight:700;font-size:12.5px;margin:0 0 1mm}
.foot{margin-top:2mm;font-size:11px}
.disc{margin-top:1.5mm;font-size:8px;color:#444}
.lab{border:3px solid #111;padding:4mm 4mm 3mm}
.lt{text-align:center;font-weight:800;font-size:23px;line-height:1.25;margin:0 0 2.5mm}
.lt small{display:block;font-size:11px;color:#24467a;font-weight:700}
.pbox{border:3px solid #e03131;border-radius:9mm;text-align:center;font-weight:800;font-size:36px;line-height:1.2;padding:5mm 3mm;margin:0 0 4mm}
.pbox small{display:block;font-size:12px;color:#24467a;font-weight:700}
.sigpics{display:flex;align-items:center;justify-content:space-between;margin:1mm 0 3mm}
.pics{display:flex;gap:1mm;align-items:center}
.sig{color:#e03131;font-weight:800;font-size:38px;letter-spacing:3px;white-space:nowrap;text-align:center;line-height:1.1}
.sig small{display:block;font-size:14px;letter-spacing:0}
.banner{background:#e60012;color:#fff;font-weight:800;font-size:22px;text-align:center;padding:1.5mm;border-radius:2.5mm;margin:0 0 1.5mm;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.banner small{display:block;font-size:10px;font-weight:700}
.lbl th{width:15%;font-size:14.5px}
.lbl th.sub{width:10%;font-size:14.5px}
.lbl .k{font-weight:700;font-size:14px}
.lbl .it{margin:0 0 2.2mm}
.lbl .f{font-size:10px}
.supp{margin-top:3mm}
.supp th,.supp td{background:#fff9c4;font-weight:700;font-size:14px;text-align:center}
.supp td.v{background:#fff;font-size:15px}
.ppe{display:block;margin:0 auto;max-width:100%;max-height:48mm}
"""

FIT_JS = """<script>
window.addEventListener('load',function(){
 var H=1045;
 document.querySelectorAll('.doc.fit').forEach(function(d){
  var h=d.scrollHeight; if(h>H){d.style.zoom=Math.max(0.5,H/h);}
 });
});
</script>"""


def _img(b64, style=""):
    return f'<img src="data:image/png;base64,{b64}" style="{style}">'


def _items(texts, D, lang, tag="•"):
    out = []
    for s in texts:
        fo = D.tr(s, lang) if lang != "ko" else ""
        out.append(f'<div class="it"><div class="k">{tag} {esc(s)}</div>'
                   + (f'<div class="f" lang="{LANG_ATTR[lang]}">{esc(fo)}</div>' if fo else "") + "</div>")
    return "".join(out)


def _bi(D, key, lang):
    ko = esc(D.ui(key, "ko"))
    if lang == "ko":
        return ko
    return f'{ko} / <span class="f" lang="{LANG_ATTR[lang]}" style="font-size:inherit;color:#24467a">{esc(D.ui(key, lang))}</span>'


def _fo(D, key, lang):
    return "" if lang == "ko" else f'<span lang="{LANG_ATTR[lang]}">{esc(D.ui(key, lang))}</span>'


def _small(ko, fo):
    return esc(ko) + (f"<small>{fo}</small>" if fo else "")


def _bk(ko, key, D, lang):
    """한국어 라벨(ko) / 외국어(사전 key)"""
    if lang == "ko":
        return esc(ko)
    return f'{esc(ko)}<br><span class="f" lang="{LANG_ATTR[lang]}" style="font-size:9.5px">{esc(D.ui(key, lang))}</span>'


def guide_doc(data, D, lang, assets):
    ghs = data["ghs"]
    gw = "24mm" if len(ghs) <= 4 else "17mm"
    pics = "".join(_img(assets["GHS"][g], f"width:{gw}") for g in ghs if g in assets["GHS"])
    cls = "".join(
        f'<div class="it"><div class="k">• {esc(c)}</div>'
        + (f'<div class="f" lang="{LANG_ATTR[lang]}">{esc(D.tr_class(c, lang))}</div>' if lang != "ko" and D.tr_class(c, lang) else "")
        + "</div>" for c in data["classes"])
    rows_fa = ""
    for i, k in enumerate(["eye", "skin", "inhale", "ingest"]):
        th = f'<th rowspan="4">{_bi(D, "first_aid", lang)}</th>' if i == 0 else ""
        rows_fa += f'<tr>{th}<th class="sub">{_bi(D, k, lang)}</th><td colspan="2">{_items(data[k], D, lang)}</td></tr>'
    foot = esc(D.ui("see_msds", "ko")) + ("" if lang == "ko" else f' <span class="f" lang="{LANG_ATTR[lang]}">/ {esc(D.ui("see_msds", lang))}</span>')
    disc = "" if lang == "ko" else f'<div class="disc f" lang="{LANG_ATTR[lang]}">▶ {esc(D.ui("disclaimer", lang))}</div>'
    return f"""<section class="doc fit">
<div class="lg">산업안전보건법 제114조 규정에 의거</div>
<h1>{_small(D.ui("g_title", "ko"), _fo(D, "g_title", lang))}</h1>
<table>
<colgroup><col style="width:19%"><col style="width:11%"><col style="width:52%"><col style="width:18%"></colgroup>
<tr><th>{_bi(D, "site", lang)}</th><td colspan="3">{esc(data["site"])}</td></tr>
<tr><th>{_bi(D, "product", lang)}</th><td colspan="3"><b>{esc(data["product"])}</b></td></tr>
<tr><th>{_bi(D, "hazards", lang)}</th><td colspan="2">{cls}</td><td class="ic" rowspan="2"><div class="pic">{pics}</div></td></tr>
<tr><th>{_bi(D, "handling", lang)}</th><td colspan="2">
 <div class="shead">{_bi(D, "sub_handling", lang)}</div>{_items(data["handling"], D, lang)}
 <div class="shead" style="margin-top:1.5mm">{_bi(D, "sub_storage", lang)}</div>{_items(data["storage"], D, lang)}</td></tr>
<tr><th>{_bi(D, "ppe", lang)}</th><td colspan="3">{_img(assets["PPE"][data["ppe"]], "display:block;margin:0 auto;max-width:100%;max-height:46mm")}</td></tr>
{rows_fa}
<tr><th>{_bi(D, "incident", lang)}</th><td colspan="3">{_items(data["incident"], D, lang)}</td></tr>
</table>
<div class="foot">▶ {foot}</div>{disc}
</section>"""


def label_doc(data, D, lang, assets):
    ghs = [g for g in data["ghs"] if g in assets["GHS"]]
    pw = min(40, int(152 / max(1, len(ghs))) - 2)
    pics = "".join(_img(assets["GHS"][g], f"width:{pw}mm;height:{pw}mm") for g in ghs)
    sig_ko = (data["signal"] or "").strip()
    key = "signal_danger" if "위험" in sig_ko else ("signal_warning" if "경고" in sig_ko else None)
    sig = ""
    if sig_ko:
        sig = esc(" ".join(sig_ko)) if len(sig_ko) <= 3 else esc(sig_ko)
        if lang != "ko" and key:
            sig += f'<small lang="{LANG_ATTR[lang]}">{esc(D.ui(key, lang))}</small>'

    def block(items):
        out = []
        for it in items:
            fo = D.tr(it["text"], lang) if lang != "ko" else ""
            out.append(f'<div class="it"><div class="k">• {esc(it["ko"])}</div>'
                       + (f'<div class="f" lang="{LANG_ATTR[lang]}">{esc(fo)}</div>' if fo else "") + "</div>")
        return "".join(out)

    P = data["P"]
    pg = [("prev", "예방", [p for p in P if p["code"][1] in "12"]), ("resp", "대응", [p for p in P if p["code"][1] == "3"]),
          ("stor", "저장", [p for p in P if p["code"][1] == "4"]), ("disp", "폐기", [p for p in P if p["code"][1] == "5"])]
    pg = [g for g in pg if g[2]]
    rows = f'<tr><th colspan="2">{_bk("유해·위험문구", "hstmt", D, lang)}</th><td>{block(data["H"])}</td></tr>'
    for i, (k, ko, items) in enumerate(pg):
        th = f'<th rowspan="{len(pg)}">{_bk("예방조치문구", "precaut", D, lang)}</th>' if i == 0 else ""
        rows += f'<tr>{th}<th class="sub">{_bk(ko, k, D, lang)}</th><td>{block(items)}</td></tr>'
    foot = esc(D.ui("see_msds", "ko")) + ("" if lang == "ko" else f' <span class="f" lang="{LANG_ATTR[lang]}">/ {esc(D.ui("see_msds", lang))}</span>')
    disc = "" if lang == "ko" else f'<div class="disc f" lang="{LANG_ATTR[lang]}">▶ {esc(D.ui("disclaimer", lang))}</div>'
    return f"""<section class="doc fit"><div class="lab">
<div class="lt">{_small("산업안전보건법 제115조 규정에 의한 경고표지", _fo(D, "l_title", lang))}</div>
<div class="pbox">{_small(data["product"], "")}</div>
<div class="sigpics"><div class="pics">{pics}</div><div class="sig">{sig}</div></div>
<div class="banner">{_small("유해·위험성에 따른 조치사항", _fo(D, "banner", lang))}</div>
<table class="lbl"><colgroup><col style="width:14%"><col style="width:10%"><col></colgroup>{rows}</table>
<table class="supp"><colgroup><col style="width:14%"><col style="width:13%"><col><col style="width:14%"><col style="width:22%"></colgroup>
<tr><th rowspan="2">{_bk("공급자 정보", "supplier", D, lang)}</th><th>{_bk("업체명", "company", D, lang)}</th><td class="v">{esc(data["company"])}</td>
<th>{_bk("전화번호", "phone", D, lang)}</th><td class="v">{esc(data["phone"])}</td></tr>
<tr><th>{_bk("주 소", "address", D, lang)}</th><td class="v" colspan="3">{esc(data["address"])}</td></tr></table>
<div class="foot">▶ {foot}</div>{disc}
</div></section>"""


def make_html(kind, data, D, langs, assets):
    fn = guide_doc if kind == "guide" else label_doc
    body = "\n".join(fn(data, D, lang, assets) for lang in langs)
    title = "화학물질 작업공정별 관리요령" if kind == "guide" else "경고표지"
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{title} - {esc(data["product"])}</title>'
            f"<style>{CSS}</style></head><body>{body}{FIT_JS}</body></html>")


# ───────────────────────── 6. HTML → PDF (Edge/Chrome 헤드리스) ─────────────────────────
def find_browser():
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    for n in ("msedge", "google-chrome", "chrome", "chromium", "chromium-browser"):
        p = shutil.which(n)
        if p:
            return p
    extra = os.environ.get("MSDS_BROWSER")
    return extra if extra and os.path.exists(extra) else None


def html_to_pdf(html_text: str):
    """성공하면 (pdf_bytes, ''), 실패하면 (None, 사유)"""
    exe = find_browser()
    if not exe:
        return None, "Edge 또는 Chrome을 찾지 못했습니다."
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.html"
        out = Path(td) / "out.pdf"
        src.write_text(html_text, "utf-8")
        last = ""
        for head in (["--headless=new", "--no-pdf-header-footer"], ["--headless", "--print-to-pdf-no-header"]):
            cmd = [exe, *head, "--disable-gpu", f"--user-data-dir={Path(td) / 'prof'}",
                   f"--print-to-pdf={out}", "--run-all-compositor-stages-before-draw", src.as_uri()]
            if os.name != "nt":
                cmd.insert(1, "--no-sandbox")
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=60)
                last = (r.stderr or b"").decode("utf-8", "ignore")[-300:]
            except Exception as ex:
                last = str(ex)
                continue
            if out.exists() and out.stat().st_size > 1000:
                return out.read_bytes(), ""
        return None, "PDF 변환 실패: " + last
