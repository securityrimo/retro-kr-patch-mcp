"""BG rect 재작화 엔진 + ROM base 역탐색 + 플러그인 로더 — 검증 스크립트 복사-추출.

원본: redraw_region.py(rect 엔진 전체), locate_bg.py(locate).
규율: 알고리즘 무변경 — 같은 입력에서 원본 스크립트와 md5 동일 산출이 회귀 기준.
차이는 I/O 경계뿐: 원본은 baseROM '파일'을 읽어 팔레트 히스토그램 계산+전체 복사 후 기록,
여기서는 전달받은 rom bytearray에서 동일 계산 후 in-place 수정(체인 시맨틱 동일).
게임 특화 상수 없음: 폰트/ROM/frame은 전부 인자.

frame 규약: {"io": bytes, "palette": bytes, "vram": bytes, ...} (gba.load_frame 산출).
"""
import struct
import importlib.util
from collections import Counter
from pathlib import Path
from PIL import Image, ImageFont, ImageDraw


def locate(frame, bg, rom):
    """BG 타일맵의 사용 타일로 ROM 무압축 원본 위치를 역탐색. 원본: locate_bg.py.

    다중 타일 교차검증 — 잉크 8px 미만 타일 제외, rom.find 후보 base에 대해
    전체 사용 타일 일치 수 ok가 max(2, checked//2) 이상이면 확정.
    반환: {"base": int|None, "ok": int, "checked": int}
    """
    vram = frame["vram"]; io = frame["io"]
    cnt = struct.unpack("<H", io[8+bg*2:8+bg*2+2])[0]
    charbase = ((cnt >> 2) & 3) * 0x4000
    scrbase = ((cnt >> 8) & 0x1F) * 0x800
    bpp8 = bool(cnt & 0x80)
    szbits = (cnt >> 14) & 3
    mapw, maph = [(32, 32), (64, 32), (32, 64), (64, 64)][szbits]
    used = {}
    for ty in range(maph):
        for tx in range(mapw):
            blk = 0; bx, by = tx, ty
            if mapw == 64 and tx >= 32: blk += 1; bx = tx-32
            if maph == 64 and ty >= 32: blk += (2 if mapw == 64 else 1); by = ty-32
            se = struct.unpack("<H", vram[scrbase+blk*0x800+(by*32+bx)*2:scrbase+blk*0x800+(by*32+bx)*2+2])[0]
            t = se & 0x3FF
            if t: used[t] = used.get(t, 0)+1
    tsz = 64 if bpp8 else 32
    found = None; ok_f = 0; chk_f = 0
    for t in sorted(used):
        tb = vram[charbase+t*tsz:charbase+t*tsz+tsz]
        if sum(1 for b in tb if b) < 8: continue
        k = rom.find(tb)
        if k >= 0:
            base = k-t*tsz; ok = 0; chk = 0
            for t2 in sorted(used):
                tb2 = vram[charbase+t2*tsz:charbase+t2*tsz+tsz]
                if sum(1 for b in tb2 if b) < 8: continue
                chk += 1
                if 0 <= base+t2*tsz <= len(rom)-tsz and rom[base+t2*tsz:base+t2*tsz+tsz] == tb2: ok += 1
            ok_f, chk_f = ok, chk
            if ok >= max(2, chk//2): found = base; break
    return {"base": found, "ok": ok_f, "checked": chk_f}


def _render(text, px, font_path):
    """멀티라인 텍스트 마스크 렌더(360x160 캔버스, fontmode='1', >127 임계).

    원본: redraw_region.py render()(:78). 리터럴 '\\n'(백슬래시+n)을 개행으로 치환
    (CLI 인자 시맨틱 보존 — 실제 개행 문자는 multiline_text가 그대로 처리).
    """
    f = ImageFont.truetype(font_path, px); W, H = 360, 160
    img = Image.new("L", (W, H), 0); d = ImageDraw.Draw(img); d.fontmode = "1"
    d.multiline_text((4, 4), text.replace("\\n", "\n"), font=f, fill=255, spacing=2, align="center")
    a = img.load()
    xs = [x for x in range(W) for y in range(H) if a[x, y] > 127]
    ys = [y for y in range(H) for x in range(W) if a[x, y] > 127]
    if not xs: return None
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    m = [[a[x0+i, y0+j] > 127 for i in range(x1-x0+1)] for j in range(y1-y0+1)]
    return m, x1-x0+1, y1-y0+1


def redraw_rect(rom, frame, bg, base, rect, text, *,
                font_path, hi=None, fill=None, dark=None, ol=None, excl=None,
                margin=0.86, preview_path=None, dry_run=False):
    """BG 셀 사각영역에 한글(멀티라인) 재작화 — 무압축 4bpp in-place. 원본: redraw_region.py.

    - 써지는 대상 = rect 내 '이미 사용중인' 타일맵 셀만(타일맵 불변), 미커버 사용셀은 투명 처리.
    - excl=(tx0,ty0,tx1,ty1)이면 그 사각 안 셀 제외(같은 행 다른 요소 보호. 원본 EXCL env).
    - 팔레트 역할(hi/fill/dark/ol)은 None이면 rom 바이트 히스토그램으로 자동판정,
      지정 시 오버라이드(원본 HI/FILL/DARK/OL env와 동일 시맨틱).
    - rom(bytearray)을 in-place 수정(dry_run=True면 계산만). preview_path 지정 시 5배 PNG 저장.
    반환: {"ok","cells","tiles","conflicts","palno","chosen":{"hi","fill","dark","ol"},"px","preview"}
    """
    rx0, ry0, rx1, ry1 = rect
    ex = tuple(excl) if excl else None
    vram = frame["vram"]; io = frame["io"]; pal = frame["palette"]
    cnt = struct.unpack("<H", io[8+bg*2:8+bg*2+2])[0]
    scrbase = ((cnt >> 8) & 0x1F)*0x800
    cells = []; palnos = Counter()
    for ty in range(ry0, ry1+1):
        for tx in range(rx0, rx1+1):
            if ex and ex[0] <= tx <= ex[2] and ex[1] <= ty <= ex[3]: continue
            se = struct.unpack("<H", vram[scrbase+(ty*32+tx)*2:scrbase+(ty*32+tx)*2+2])[0]
            t = se & 0x3FF
            if t:
                pn = (se >> 12) & 0xF
                cells.append((tx, ty, t, pn, bool(se & 0x400), bool(se & 0x800))); palnos[pn] += 1
    if not cells:
        return {"ok": False, "error": "사용 셀 없음", "cells": 0, "tiles": 0, "conflicts": 0,
                "palno": None, "chosen": None, "px": None, "preview": None}
    palno = palnos.most_common(1)[0][0]
    txs = [c[0] for c in cells]; tys = [c[1] for c in cells]
    tx0, tx1, ty0, ty1 = min(txs), max(txs), min(tys), max(tys)
    # 갭없는 최대 직사각형(구멍 방지) — 최소 폭/높이 유지 위해 bbox 폴백
    usedset = {(c[0], c[1]) for c in cells}
    _W, _H = tx1-tx0+1, ty1-ty0+1
    _mat = [[1 if (tx0+i, ty0+j) in usedset else 0 for i in range(_W)] for j in range(_H)]
    _hei = [0]*_W; _ba = 0; _best = (tx0, ty0, tx1, ty1)
    for _j in range(_H):
        for _i in range(_W): _hei[_i] = _hei[_i]+1 if _mat[_j][_i] else 0
        _st = []
        for _i in range(_W+1):
            _h = _hei[_i] if _i < _W else 0
            while _st and _hei[_st[-1]] >= _h:
                _hh = _hei[_st.pop()]; _lf = _st[-1]+1 if _st else 0; _ar = _hh*(_i-_lf)
                if _ar > _ba: _ba = _ar; _best = (tx0+_lf, ty0+_j-_hh+1, tx0+_i-1, ty0+_j)
            _st.append(_i)
    ctx0, cty0, ctx1, cty1 = _best
    bx0, by0 = ctx0*8, cty0*8; bw, bh = (ctx1-ctx0+1)*8, (cty1-cty0+1)*8
    MARGIN = float(margin)
    def rgb(i):
        b = palno*32; c = struct.unpack("<H", pal[b+i*2:b+i*2+2])[0]
        return ((c & 31) << 3, ((c >> 5) & 31) << 3, ((c >> 10) & 31) << 3)
    PAL = [rgb(i) for i in range(16)]
    def sat(c): return max(c)-min(c)
    def lum(c): return 0.3*c[0]+0.59*c[1]+0.11*c[2]
    # 이 라벨 타일이 실제 쓰는 인덱스만으로 역할 판정(팔레트 전체 아님 — 뱅크 공유 오검출 방지)
    _hist = Counter()
    for tx, ty, t, pn, hf, vf in cells:
        for bb in rom[base+t*32:base+t*32+32]:
            _hist[bb & 0xF] += 1; _hist[bb >> 4] += 1
    _tot = sum(v for i, v in _hist.items() if i)
    used_idx = [i for i in range(1, 16) if _hist[i] > _tot*0.02] or [i for i in range(1, 16) if _hist[i]]
    colors = [(i, PAL[i]) for i in used_idx]
    chroma = [(i, c) for i, c in colors if sat(c) > 40] or colors
    fill_bright = max(chroma, key=lambda z: sat(z[1])*(lum(z[1])+40))[0]
    fill_dark = min(chroma, key=lambda z: lum(z[1]))[0]
    IDX_HI = max(used_idx, key=lambda i: lum(PAL[i]))
    IDX_OL = min(used_idx, key=lambda i: lum(PAL[i]))
    # 수동 오버라이드(원본 FILL/DARK/HI/OL 환경변수와 동일 시맨틱)
    if fill is not None: fill_bright = int(fill)
    if dark is not None: fill_dark = int(dark)
    if hi is not None: IDX_HI = int(hi)
    if ol is not None: IDX_OL = int(ol)
    # 멀티라인 마스크 렌더 (px 자동스케일로 bbox 맞춤)
    best = None; px_used = None
    for px in range(48, 9, -1):
        r = _render(text, px, font_path)
        if not r: continue
        m, mw, mh = r
        if mh <= bh*MARGIN and mw <= bw*MARGIN: best = (m, mw, mh); px_used = px; break
    if not best:
        r = _render(text, 12, font_path); m, mw, mh = r
        sc = min((bw*MARGIN)/mw, (bh*MARGIN)/mh)
        nw, nh = max(1, int(mw*sc)), max(1, int(mh*sc))
        im = Image.new("L", (mw, mh))
        for j in range(mh):
            for i in range(mw): im.putpixel((i, j), 255 if m[j][i] else 0)
        im = im.resize((nw, nh), Image.NEAREST); a = im.load()
        m = [[a[i, j] > 127 for i in range(nw)] for j in range(nh)]; mw, mh = nw, nh
        best = (m, mw, mh)
    m, mw, mh = best
    canvas = [[0]*256 for _ in range(256)]
    gx = bx0+(bw-mw)//2; gy = by0+(bh-mh)//2
    def s(x, y, v):
        if 0 <= x < 256 and 0 <= y < 256: canvas[y][x] = v
    for j in range(mh):
        for i in range(mw):
            if m[j][i]:
                for dj, di in ((1, 0), (0, 1), (1, 1), (-1, 0), (0, -1), (1, -1), (-1, 1), (-1, -1)):
                    s(gx+i+di, gy+j+dj, IDX_OL)
    for j in range(mh):
        for i in range(mw):
            if m[j][i]:
                fr = j/max(1, mh-1)
                s(gx+i, gy+j, IDX_HI if fr < 0.22 else (fill_bright if fr < 0.7 else fill_dark))
    if preview_path:
        im = Image.new("RGB", (bw, bh), (60, 60, 90))
        for y in range(bh):
            for x in range(bw):
                v = canvas[by0+y][bx0+x]
                im.putpixel((x, y), PAL[v] if v else (60, 60, 90))
        im.resize((bw*5, bh*5), Image.NEAREST).save(preview_path)
    written = {}; conflict = 0
    for tx, ty, t, pn, hf, vf in cells:
        cell = [[canvas[ty*8+yy][tx*8+xx] for xx in range(8)] for yy in range(8)]
        if hf: cell = [row[::-1] for row in cell]
        if vf: cell = cell[::-1]
        enc = bytearray(32)
        for yy in range(8):
            for xx in range(0, 8, 2):
                enc[yy*4+xx//2] = (cell[yy][xx] & 0xF) | ((cell[yy][xx+1] & 0xF) << 4)
        off = base+t*32
        if t in written and written[t] != bytes(enc): conflict += 1
        written[t] = bytes(enc)
        if not dry_run: rom[off:off+32] = enc
    return {"ok": True, "cells": len(cells), "tiles": len(written), "conflicts": conflict,
            "palno": palno,
            "chosen": {"hi": IDX_HI, "fill": fill_bright, "dark": fill_dark, "ol": IDX_OL},
            "px": px_used, "preview": preview_path}


def load_plugin(path):
    """그래픽 플러그인 로드 — spec_from_file_location, PLUGIN_API==1 확인.

    플러그인 규약: PLUGIN_API = 1 상수 +
    def apply(rom: bytearray, frames: dict, params: dict, *, preview_dir, dry_run) -> dict
    """
    p = Path(path)
    spec = importlib.util.spec_from_file_location(f"krgfx_plugin_{p.stem}", str(p))
    if spec is None or spec.loader is None:
        raise ImportError(f"플러그인 로드 실패: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if getattr(mod, "PLUGIN_API", None) != 1:
        raise ValueError(f"PLUGIN_API != 1: {path}")
    if not callable(getattr(mod, "apply", None)):
        raise ValueError(f"apply(rom, frames, params, *, preview_dir, dry_run) 부재: {path}")
    return mod
