"""텍스트 마스크/페인트/인페인트 프리미티브 — 검증 스크립트 복사-추출.

원본: redraw_title.py(glyph_mask:135 / paint:167 / inpaint:108),
      redraw_intro.py(bbox:74 / transplant:85).
규율: 알고리즘 무변경 — 같은 인자에서 원본 스크립트와 비트 동일 산출이 회귀 기준.
게임 특화 상수 없음: 폰트 경로·캔버스·소스 캔버스는 전부 인자.

캔버스 규약: canvas[y][x] 팔레트 인덱스(0=투명), 크기는 len(canvas)×len(canvas[0]).
원본은 256x160(BG 화면)·256x256(타일맵 평면) 고정이었고, 여기서는 캔버스 크기에서 유도
(같은 크기 입력이면 동작 동일).
"""
from PIL import Image, ImageFont, ImageDraw


def glyph_mask(text, px, font_path, adv_ratio=1.0, shear=0.0, bold=0):
    """개별 글리프 이어붙임(자간 조절) + 굵히기 + 이탤릭 시어 마스크 렌더.

    원본: redraw_title.py:135 (NEO 클로저 → font_path 인자 승격).
    반환: (mask, w, h) — mask는 bool 2D(행 우선), 글리프 없으면 (None, 0, 0).
    """
    f = ImageFont.truetype(font_path, px)
    W, H = 600, 120
    img = Image.new("L", (W, H), 0); d = ImageDraw.Draw(img); d.fontmode = "1"
    cx = 4
    for ch in text:
        if ch == " ":
            cx += int(px*0.45); continue
        d.text((cx, 4), ch, font=f, fill=255)
        bb = d.textbbox((cx, 4), ch, font=f)
        cx += max(4, int((bb[2]-bb[0])*adv_ratio)) + max(1, px//12)
    a = img.load()
    for _ in range(bold):     # 획 굵히기(우+하 1px 딜레이트)
        img2 = img.copy(); b = img2.load()
        for y in range(H-1):
            for x in range(W-1):
                if a[x, y] > 127: b[x+1, y] = 255; b[x, y+1] = 255
        img, a = img2, img2.load()
    if shear:
        img2 = Image.new("L", (W, H), 0); b = img2.load()
        for y in range(H):
            off = int(shear*(H-y))
            for x in range(W):
                if a[x, y] > 127 and 0 <= x+off < W: b[x+off, y] = 255
        img, a = img2, img2.load()
    xs = [x for x in range(W) for y in range(H) if a[x, y] > 127]
    ys = [y for y in range(H) for x in range(W) if a[x, y] > 127]
    if not xs: return None, 0, 0
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return [[a[x0+i, y0+j] > 127 for i in range(x1-x0+1)] for j in range(y1-y0+1)], x1-x0+1, y1-y0+1


def paint(canvas, mask, mw, mh, gx, gy, fills, outline, outer=None, ol_w=1):
    """마스크를 캔버스 (gx,gy)에 페인트 — 세로 그라디언트 채움 + 체비쇼프 외곽선.

    원본: redraw_title.py:167 (canvas 클로저 → 인자 승격). canvas를 in-place 수정.
    fills=[(frac상한, idx), ...] 세로 그라디언트, outline=외곽선 idx(두께 ol_w),
    outer=그 바깥 1px idx(None이면 미적용).
    """
    CH = len(canvas); CW = len(canvas[0])
    def on(i, j): return 0 <= i < mw and 0 <= j < mh and mask[j][i]
    def setpx(x, y, v):
        if 0 <= x < CW and 0 <= y < CH: canvas[y][x] = v
    R2 = ol_w + 1
    for j in range(-R2, mh+R2):
        for i in range(-R2, mw+R2):
            if on(i, j): continue
            dmin = 99
            for dj in range(-R2, R2+1):
                for di in range(-R2, R2+1):
                    if on(i+di, j+dj):
                        dmin = min(dmin, max(abs(di), abs(dj)))
            if dmin <= ol_w: setpx(gx+i, gy+j, outline)
            elif outer is not None and dmin <= ol_w+1: setpx(gx+i, gy+j, outer)
    for j in range(mh):
        fr = j/max(1, mh-1); v = fills[-1][1]
        for lim, idx in fills:
            if fr < lim: v = idx; break
        for i in range(mw):
            if mask[j][i]: setpx(gx+i, gy+j, v)


def mark_erase(canvas, erase, x0, y0, x1, y1, pred=None):
    """사각 [x0,x1)×[y0,y1) 안의 유색(≠0) 픽셀을 erase 마스크에 마킹.

    원본: redraw_title.py erase()(:58) — pred(x, y, v) 참인 픽셀만(None이면 전부).
    inpaint()에 넘길 마스크를 여러 영역에 걸쳐 누적할 때 사용.
    """
    for y in range(y0, y1):
        for x in range(x0, x1):
            v = canvas[y][x]
            if v and (pred is None or pred(x, y, v)): erase[y][x] = True


def inpaint(canvas, x0, y0, x1, y1, pred=None, erase=None):
    """사각 영역 소거 후 행 최근접(양측) 인페인트, 폴백 열 최근접.

    원본: redraw_title.py:108 (canvas/E 클로저 → 인자 승격).
    - [x0,x1)×[y0,y1) 안의 유색 픽셀 중 pred(x, y, v) 참(None이면 전부)을 소거 마킹.
    - erase(bool 2D)를 주면 그 기존 마킹에 누적 — 여러 영역을 한 번에 인페인트하려면
      mark_erase()로 마스크를 만들어 넘기고 마지막 호출에서 채운다.
    - 투명(0) 소스보다 유색 소스 우선(배경 한가운데 '미표기' 구멍 방지).
    canvas를 in-place 수정. 반환: 사용한 erase 마스크(전부 False로 소진됨).
    """
    H = len(canvas); W = len(canvas[0])
    E = erase if erase is not None else [[False]*W for _ in range(H)]
    mark_erase(canvas, E, x0, y0, x1, y1, pred)
    for y in range(H):
        x = 0
        while x < W:
            if E[y][x]:
                x0_ = x
                while x < W and E[y][x]: x += 1
                lv = canvas[y][x0_-1] if x0_ > 0 and not E[y][x0_-1] else None
                rv = canvas[y][x] if x < W and not E[y][x] else None
                # 투명(0) 소스보다 유색 소스 우선 — 배경 한가운데 '미표기' 구멍 방지
                if lv == 0 and rv: lv = None
                if rv == 0 and lv: rv = None
                for i in range(x0_, x):
                    if lv is not None and rv is not None:
                        canvas[y][i] = lv if (i-x0_) <= (x-1-i) else rv
                    elif lv is not None: canvas[y][i] = lv
                    elif rv is not None: canvas[y][i] = rv
                    else:
                        for d in range(1, 60):
                            if y-d >= 0 and not E[y-d][i]: canvas[y][i] = canvas[y-d][i]; break
                            if y+d < H and not E[y+d][i]: canvas[y][i] = canvas[y+d][i]; break
                    E[y][i] = False
            else:
                x += 1
    return E


def bbox(cv):
    """캔버스의 유색(≠0) 픽셀 바운딩 박스. 원본: redraw_intro.py:74.

    반환: (x0, y0, x1, y1) 포함 범위. 유색 픽셀이 없으면 ValueError(원본 동일).
    """
    xs = [x for y in range(len(cv)) for x in range(len(cv[0])) if cv[y][x]]
    ys = [y for y in range(len(cv)) for x in range(len(cv[0])) if cv[y][x]]
    return min(xs), min(ys), max(xs), max(ys)


def transplant(cv, dst_bb, src_bb, src_canvas, lut=None):
    """src_canvas의 src_bb 영역을 최근접 리샘플로 cv의 dst_bb에 이식.

    원본: redraw_intro.py:85 (static/LUT 클로저 → src_canvas/lut 인자 승격).
    bb는 (x0, y0, x1, y1) 포함 범위. lut=None이면 항등 매핑(원본 LUT=range(256)).
    투명(0)은 lut을 거치지 않고 0 유지. cv를 in-place 수정.
    """
    SH = len(src_canvas); SW = len(src_canvas[0])
    dx0, dy0, dx1, dy1 = dst_bb
    sx0, sy0, sx1, sy1 = src_bb
    dw, dh = dx1-dx0+1, dy1-dy0+1
    sw, sh = sx1-sx0+1, sy1-sy0+1
    for j in range(dh):
        for i in range(dw):
            sx = sx0 + int(i*sw/dw); sy = sy0 + int(j*sh/dh)
            v = src_canvas[sy][sx] if 0 <= sy < SH and 0 <= sx < SW else 0
            cv[dy0+j][dx0+i] = (lut[v] if lut is not None else v) if v else 0
