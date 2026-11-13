# -*- coding: utf-8 -*-
"""
MSDS 관리요령 · 경고표지 자동 생성기 (V9)
실행:  python -m streamlit run MSDS_V9.py
필요한 파일(같은 폴더): msds_core.py, msds_assets.py, msds_dictionary.json
설치:  python -m pip install streamlit pdfplumber pillow numpy   (PDF 변환은 PC에 설치된 Edge/Chrome 사용)
"""
import base64
import hashlib
import re
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import msds_core as C
from msds_assets import GHS_ASSETS, PPE_ASSETS

_HERE = Path(__file__).resolve().parent
_LOGO = _HERE / "ci_logo.png"
_FAV = _HERE / "favicon.png"
_SITE = "전주 오성대우 주택재건축 정비사업"
st.set_page_config(page_title="MSDS 관리요령·경고표지", page_icon=str(_FAV) if _FAV.exists() else None, layout="wide")
st.markdown("""<style>
.block-container{padding-top:1.6rem}
.hdr-site{font-size:1.05rem;font-weight:700;color:#212121;margin:.35rem 0 0}
.hdr-bar{height:4px;background:#F01428;margin:.5rem 0 1rem;border-radius:2px}
</style>""", unsafe_allow_html=True)


def _header(title):
    if _LOGO.exists():
        st.image(str(_LOGO), width=340)
    st.markdown(f'<div class="hdr-site">{_SITE}</div><div class="hdr-bar"></div>', unsafe_allow_html=True)
    st.title(title)

try:
    import pdfplumber  # noqa: F401
except Exception:
    st.warning("pdfplumber가 설치되어 있지 않습니다. 명령 창에서  python -m pip install pdfplumber  를 실행한 뒤 다시 시작하면 읽기 정확도가 높아집니다.")


def _gate():
    """Secrets에 APP_PASSWORD가 설정되어 있으면 비밀번호를 입력해야 사용할 수 있다."""
    import hmac
    try:
        pw = st.secrets.get("APP_PASSWORD", "")
    except Exception:
        pw = ""
    if not pw or st.session_state.get("_auth_ok"):
        return
    _header("MSDS 관리요령 · 경고표지")
    x = st.text_input("비밀번호", type="password")
    if x and hmac.compare_digest(x.encode(), str(pw).encode()):
        st.session_state["_auth_ok"] = True
        st.rerun()
    elif x:
        st.error("비밀번호가 맞지 않습니다.")
    st.stop()


_gate()

with st.sidebar:        # 서버가 다시 시작되면 입력한 번역이 지워질 수 있으므로 백업/불러오기 제공
    st.subheader("입력한 번역 백업")
    st.caption("서버가 다시 시작되면 입력창에 넣은 번역이 사라질 수 있습니다. 가끔 내려받아 보관하고, 필요하면 아래에서 불러오세요.")
    _p = C.USER_DICT_PATH
    if _p.exists():
        st.download_button("입력한 번역 내려받기 (user_dictionary.json)", _p.read_bytes(), "user_dictionary.json", "application/json")
    _up = st.file_uploader("백업 불러오기 (기존 내용과 합칩니다)", type="json", key="udict_up")
    if _up is not None and st.session_state.get("_udict_done") != _up.file_id:
        try:
            import json as _json
            _new = _json.loads(_up.getvalue().decode("utf-8"))
            _cur = _json.loads(_p.read_text("utf-8")) if _p.exists() else {}
            for _grp in ("codes", "sent"):
                for _k, _v in (_new.get(_grp) or {}).items():
                    _cur.setdefault(_grp, {}).setdefault(_k, {}).update(_v)
            _p.write_text(_json.dumps(_cur, ensure_ascii=False, indent=1), "utf-8")
            st.session_state["_udict_done"] = _up.file_id
            st.success("백업을 불러왔습니다.")
        except Exception as _ex:
            st.error(f"불러오지 못했습니다: {_ex}")

_header("MSDS → 화학물질 관리요령 · 경고표지 (PDF)")
st.caption("원본 MSDS 문장은 줄이거나 고치지 않고 '선택'만 합니다. 외국어는 검수된 사전에서만 가져오며, 사전에 없으면 비워 둡니다.")

D = C.Dictionary()


@st.cache_data(show_spinner="MSDS를 읽는 중...")
def analyze_pdf(raw: bytes):
    return C.analyze(C.read_pdf(raw))


def multi(label, options, default, key, limit=None):
    """원문 항목 중에서만 고르는 다중선택 (문서 순서 유지)."""
    kw = {"max_selections": limit} if limit else {}
    try:
        sel = st.multiselect(label, options, default=default, key=key, **kw)
    except TypeError:                      # 구버전 Streamlit
        sel = st.multiselect(label, options, default=default, key=key)
        if limit and len(sel) > limit:
            st.warning(f"최대 {limit}개까지만 사용됩니다.")
            sel = sel[:limit]
    return sorted(sel, key=options.index)


def multi_idx(label, texts, default_idx, key, limit, base=None):
    """인덱스로 선택하고 (원본 리스트 기준) 인덱스 목록을 돌려준다."""
    base = base if base is not None else list(range(len(texts)))
    opts = list(range(len(texts)))
    kw = {"max_selections": limit} if limit else {}
    try:
        sel = st.multiselect(label, opts, default=default_idx, key=key, format_func=lambda i: texts[i], **kw)
    except TypeError:
        sel = st.multiselect(label, opts, default=default_idx, key=key, format_func=lambda i: texts[i])
        sel = sel[:limit] if limit else sel
    return [base[i] for i in sorted(sel)]


# ───────── 1. 입력 ─────────
c1, c2 = st.columns([1, 1])
with c1:
    site = st.text_input("사업장명", value="", placeholder="예) 전주 오성대우 주택재건축 정비사업")
    up = st.file_uploader("원본 MSDS (PDF)", type=["pdf"])
with c2:
    langs = st.multiselect(
        "외국어 선택 (선택한 언어마다 별도 페이지로 출력)", C.FOREIGN, default=["en"],
        format_func=lambda k: f"{C.LANG_NAMES[k]}  ·  신뢰도: {D.meta['reliability'][k]}")
    if "km" in langs:
        st.warning("크메르어는 원어민 검수 전 번역입니다. 현장 게시 전에 반드시 원어민 확인을 받으세요.")
    if "si" in langs:
        st.warning("싱할라어는 화면 제목 등 고정 문구만 번역되어 있고(원어민 검수 전), 위험·예방조치 문장은 비어 있습니다. 아래 입력창에 검수된 번역을 넣거나, 현장 게시 전에 반드시 원어민 확인을 받으세요.")
    if "vi" in langs:
        st.info("베트남어는 현지 근로자 확인을 권장합니다.")

if not up:
    st.info("MSDS PDF를 올려 주세요.")
    st.stop()

raw = up.getvalue()
try:
    A = analyze_pdf(raw)
except Exception as ex:
    st.error(f"MSDS를 읽지 못했습니다: {ex}")
    st.stop()

dg = hashlib.md5(raw).hexdigest()[:10]
K = lambda name: f"{dg}_{name}"

# ───────── 2. 기본 정보 ─────────
st.subheader("1. 제품·공급자 정보 (MSDS 1항 · 2항)")
a, b = st.columns(2)
with a:
    product = st.text_input("제품명", A["info"]["product"], key=K("product"))
    company = st.text_input("회사명", A["info"]["company"], key=K("company"))
    signal = st.text_input("신호어", A["sec2"]["signal"], key=K("signal"))
with b:
    address = st.text_input("주소", A["info"]["address"], key=K("address"))
    phone = st.text_input("긴급전화", A["info"]["phone"], key=K("phone"))

# ───────── 3. 그림문자 ─────────
st.subheader("2. GHS 그림문자 (원본 MSDS 2항에 인쇄된 그림을 자동 판별)")
ghs_all = sorted(GHS_ASSETS)
ghs = st.multiselect("그림문자", ghs_all, default=[g for g in A["ghs"] if g in ghs_all], key=K("ghs"))
st.image([base64.b64decode(GHS_ASSETS[g]) for g in ghs], width=70) if ghs else None
if A.get("ghs_estimated"):
    st.warning("⚠ 원본 PDF에서 그림문자 이미지를 찾지 못해 H코드로 추정한 값입니다. 반드시 원본 MSDS와 대조해 수정하세요.")
if not A["sec2"]["H"] and A["sec2"]["signal"] in ("", "없음", "해당없음"):
    st.info("이 MSDS는 유해·위험문구와 신호어가 없는 제품(비유해성)으로 읽혔습니다. 경고표지 대상인지 원본을 확인하세요.")
if A["ghs_unknown"]:
    st.warning(f"판별하지 못한 그림 {A['ghs_unknown']}개가 있습니다. 위 목록을 원본과 대조해 직접 확인하세요.")
for w in A["ghs_warn"]:
    st.warning("⚠ " + w)

# ───────── 4. 보호구 ─────────
st.subheader("3. 적절한 보호구 (MSDS 8항)")
ppe = st.radio("보호구 그림", ["gas", "dust"], index=0 if A["ppe"] == "gas" else 1, horizontal=True, key=K("ppe"),
               format_func=lambda k: "방독마스크 세트" if k == "gas" else "방진마스크 세트")
st.caption("자동 판정 근거: " + A["ppe_reason"])
st.image(base64.b64decode(PPE_ASSETS[ppe]), width=420)

# ───────── 5. 관리요령 문장 선택 ─────────
st.subheader("4. 관리요령 문장 선택 (원문 중에서만 선택 · 수정 불가)")
cands, defaults = A["cands"], A["defaults"]
TITLES = {"handling": "가. 안전취급요령 (MSDS 7항)", "storage": "나. 안전한 저장방법 (MSDS 7항)",
          "eye": "응급조치 - 눈 (MSDS 4항)", "skin": "응급조치 - 피부 (MSDS 4항)",
          "inhale": "응급조치 - 흡입 (MSDS 4항)", "ingest": "응급조치 - 섭취 (MSDS 4항)",
          "incident": "사고 시 대처방법 (MSDS 5·6항)"}
chosen = {}
for k in ["handling", "storage", "eye", "skin", "inhale", "ingest", "incident"]:
    n_def, n_max = C.LIMITS[k]
    chosen[k] = multi(f"{TITLES[k]}  ·  기본 {n_def}개 / 최대 {n_max}개", cands[k], defaults[k], K(k), n_max) if cands[k] else []
    if not cands[k]:
        st.warning(f"{TITLES[k]}: 원본에서 항목을 찾지 못했습니다.")
    for t in chosen[k]:
        if C.damaged(t):
            st.warning(f"⚠ 줄바꿈으로 문장이 섞였을 수 있습니다. 원본과 대조하세요: {t[:60]}…")

# ───────── 6. 분류 · H/P문구 ─────────
st.subheader("5. 유해·위험성 분류 / H·P문구 (MSDS 2항)")
classes = multi("유해성·위험성 분류", A["sec2"]["classes"], A["sec2"]["classes"], K("classes"))
HS, PS = A["sec2"]["H"], A["sec2"]["P"]
st.caption("경고표지: 유해·위험문구 최대 4개, 예방·대응·저장·폐기 각 최대 3개 (원본에 그보다 적으면 있는 만큼만). 기본값은 위험도·중요도 순으로 골랐습니다.")
LD = A["label_default"]
hi = multi_idx("유해·위험문구 (H)  ·  최대 4개", [h["text"] for h in HS], LD["H"], K("h"), C.LABEL_LIMITS["H"])
pi = []
for g, gname in C.LABEL_GROUPS:
    gi = [i for i, p in enumerate(PS) if C.p_group(p["code"]) == g]
    if not gi:
        st.caption(f"{gname}: 원본 MSDS에 해당 문구가 없습니다.")
        continue
    pi += multi_idx(f"{gname} (P)  ·  최대 {C.LABEL_LIMITS[g]}개", [PS[i]["text"] for i in gi],
                    [gi.index(i) for i in LD[g]], K("p_" + g), C.LABEL_LIMITS[g], base=gi)

data = {"site": site, "product": product, "company": company, "address": address, "phone": phone,
        "signal": signal, "ghs": ghs, "ppe": ppe, "classes": classes,
        "H": [HS[i] for i in sorted(hi)], "P": [PS[i] for i in sorted(pi)], **chosen}

# ───────── 7. 번역 점검 ─────────
st.subheader("6. 번역 점검")
miss = C.find_missing(D, data, langs) if langs else []
allow_partial = False
if langs and not miss:
    st.success("선택한 모든 문장이 검수된 사전에 있습니다.")
elif miss:
    st.error(f"사전에 없는 문장 {len(miss)}건 — 추측 번역을 하지 않고 비워 둡니다. 아래에 검수된 번역을 입력하면 사용자 사전에 저장되어 다음부터 자동 적용됩니다.")
    with st.expander("번역 입력", expanded=True):
        inputs = []
        for n, (kind, text, lang) in enumerate(miss):
            v = st.text_input(f"[{C.LANG_NAMES[lang]}] {text}", key=K(f"m{n}"))
            inputs.append((text, lang, v))
        if st.button("입력한 번역을 사용자 사전에 저장"):
            for text, lang, v in inputs:
                if v.strip():
                    D.add_user(text, lang, v.strip())
            st.rerun()
    allow_partial = st.checkbox("번역이 없는 문장은 한국어만 출력하고 진행", value=False)

# ───────── 8. 생성 ─────────
st.subheader("7. PDF 생성")
if not site.strip():
    st.warning("사업장명을 입력해 주세요.")
if st.button("PDF 만들기", type="primary", disabled=not site.strip() or (bool(miss) and not allow_partial)):
    assets = {"GHS": GHS_ASSETS, "PPE": PPE_ASSETS}
    order = langs or ["ko"]
    out = {}
    with st.spinner("PDF 변환 중..."):
        for kind in ("guide", "label"):
            h = C.make_html(kind, data, D, order, assets)
            pdf, err = C.html_to_pdf(h)
            out[kind] = {"html": h, "pdf": pdf, "err": err}
    st.session_state["out_" + dg] = out

out = st.session_state.get("out_" + dg)
if out:
    safe = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", product).strip("_") or "MSDS"
    names = {"guide": "관리요령", "label": "경고표지"}
    cols = st.columns(2)
    for col, kind in zip(cols, ("guide", "label")):
        with col:
            o = out[kind]
            if o["pdf"]:
                st.download_button(f"{names[kind]} PDF 다운로드", o["pdf"], f"{names[kind]}_{safe}.pdf", "application/pdf", key=f"dl_{kind}")
            else:
                st.error(f"{names[kind]}: {o['err']}")
                st.download_button(f"{names[kind]} HTML 다운로드 (열어서 Ctrl+P → PDF로 저장)", o["html"].encode("utf-8"),
                                   f"{names[kind]}_{safe}.html", "text/html", key=f"dh_{kind}")
    st.caption("※ 외국어 번역은 보조용입니다. 게시 전 원본 MSDS 2·4·5·6·7·8항과 대조하세요.")
