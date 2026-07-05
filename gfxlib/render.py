"""GBA 프레임 컴포지터/렌더러 — BG 레이어 렌더, mode0 합성, 그리드 오버레이, OBJ 분석.

갓슈벨 gfx_re 검증 스크립트(render_ss/bg_render/grid_bg/analyze/verify_title)에서
복사-추출. 알고리즘 무변경 — 원본과 픽셀 동일 산출이 회귀 기준.

주의: 원본 스크립트마다 팔레트 0번(투명) 처리·배경색이 다르며, 각 함수는
자기 원본의 처리를 그대로 유지한다:
  - render_bg   (bg_render.py): 인덱스 0 = 완전 투명 RGBA
  - compose 기본(render_ss.py): 배경 검정, 인덱스 0 스킵(타일 0은 그림)
  - compose layers(verify_title.py): 배경 = 팔레트[0], 타일 0 스킵
  - grid_overlay(grid_bg.py): 배경 (40,40,40), 타일 0 스킵
  - obj_sheet   (analyze.py): 배경 (40,40,40,255), 인덱스 0 = 투명
"""
import struct

from PIL import Image, ImageDraw

# (shape, size) → (w, h) 타일 단위 (analyze.py 동일)
OBJ_SIZES = {
    (0, 0): (1, 1), (0, 1): (2, 2), (0, 2): (4, 4), (0, 3): (8, 8),
    (1, 0): (2, 1), (1, 1): (4, 1), (1, 2): (4, 2), (1, 3): (8, 4),
    (2, 0): (1, 2), (2, 1): (1, 4), (2, 2): (2, 4), (2, 3): (4, 8),
}

# OBJ 타일 영역 시작(vram 내 오프셋, 0x06010000 상당) — analyze.py 동일
OBJ_TILE_BASE = 0x10000


def render_bg(frame: dict, n: int, rom: bytes = None, rom_base: int = None) -> Image.Image:
    """BG n 레이어를 맵 전체 크기(mapw*8 x maph*8) RGBA로 렌더.

    타일 소스: rom+rom_base 지정 시 ROM 무압축 타일(verify_title 방식, 타일 0 스킵),
    아니면 VRAM charbase(bg_render.py 방식, 타일 0 포함). 인덱스 0은 투명.
    """
    vram = frame["vram"]
    pal = frame["palette"]
    io = frame["io"]
    cnt = struct.unpack("<H", io[8 + n * 2:8 + n * 2 + 2])[0]
    charbase = ((cnt >> 2) & 3) * 0x4000
    scrbase = ((cnt >> 8) & 0x1F) * 0x800
    bpp8 = bool(cnt & 0x80)
    szbits = (cnt >> 14) & 3
    mapw, maph = [(32, 32), (64, 32), (32, 64), (64, 64)][szbits]

    def col(idx, palbase):
        if idx == 0:
            return (0, 0, 0, 0)
        c = struct.unpack("<H", pal[palbase + idx * 2:palbase + idx * 2 + 2])[0]
        return ((c & 31) << 3, ((c >> 5) & 31) << 3, ((c >> 10) & 31) << 3, 255)

    img = Image.new("RGBA", (mapw * 8, maph * 8), (0, 0, 0, 0))
    for ty in range(maph):
        for tx in range(mapw):
            # 스크린엔트리(4096 char 넘어가는 32x32 블록 처리 간략화)
            blk = 0
            bx, by = tx, ty
            if mapw == 64 and tx >= 32:
                blk += 1
                bx = tx - 32
            if maph == 64 and ty >= 32:
                blk += (2 if mapw == 64 else 1)
                by = ty - 32
            se_off = scrbase + blk * 0x800 + (by * 32 + bx) * 2
            se = struct.unpack("<H", vram[se_off:se_off + 2])[0]
            tile = se & 0x3FF
            hflip = bool(se & 0x400)
            vflip = bool(se & 0x800)
            palno = (se >> 12) & 0xF
            if rom is not None and not tile:
                continue  # ROM 소스에서 타일 0은 무의미(verify_title/grid_bg 방식)
            src = vram if rom is None else rom
            base = charbase if rom is None else rom_base
            # 타일 픽셀
            if bpp8:
                off = base + tile * 64
                pb = 0
                pix = []
                for y in range(8):
                    for x in range(8):
                        pix.append(col(src[off + y * 8 + x], pb))
            else:
                off = base + tile * 32
                pb = palno * 32
                pix = []
                for y in range(8):
                    for x in range(0, 8, 2):
                        b = src[off + y * 4 + x // 2]
                        pix.append(col(b & 0xF, pb))
                        pix.append(col(b >> 4, pb))
            t = Image.new("RGBA", (8, 8))
            t.putdata(pix)
            if hflip:
                t = t.transpose(Image.FLIP_LEFT_RIGHT)
            if vflip:
                t = t.transpose(Image.FLIP_TOP_BOTTOM)
            img.paste(t, (tx * 8, ty * 8), t)
    return img


def compose(frame: dict, layers: list = None) -> Image.Image:
    """mode0 우선순위 컴포지터 → 240x160 RGB.

    layers=None: render_ss.py 동등 — DISPCNT 활성 BG를 우선순위 큰 것부터 그려
    낮은 prio가 위에 덮음. 배경 검정, VRAM 타일 소스.

    layers=[(bg, rom|None, base|None), ...]: verify_title.py 일반화 — 주어진 순서
    그대로(뒤→앞) 합성, 각 레이어 타일을 ROM(base) 또는 VRAM charbase에서 취함.
    배경 = 팔레트[0] 색, 타일 0 스킵.
    """
    io = frame["io"]
    pal = frame["palette"]
    vram = frame["vram"]

    if layers is None:
        # ── render_ss.py 그대로 ──
        disp = struct.unpack("<H", io[0:2])[0]

        def col(bank, idx):
            o = bank * 32 + idx * 2
            c = struct.unpack("<H", pal[o:o + 2])[0]
            return ((c & 31) << 3, ((c >> 5) & 31) << 3, ((c >> 10) & 31) << 3)

        img = Image.new("RGB", (240, 160), (0, 0, 0))
        px = img.load()
        # 우선순위 높은(숫자 큰) BG 먼저 → 낮은 BG가 위에 덮음
        bgs = []
        for bg in range(4):
            if not (disp & (0x100 << bg)):
                continue
            c = struct.unpack("<H", io[8 + bg * 2:8 + bg * 2 + 2])[0]
            bgs.append((c & 3, bg, c))
        for prio, bg, c in sorted(bgs, key=lambda z: -z[0]):
            charB = ((c >> 2) & 3) * 0x4000
            scrB = ((c >> 8) & 0x1f) * 0x800
            c256 = bool(c & 0x80)
            for ty in range(20):
                for tx in range(30):
                    se = struct.unpack(
                        "<H", vram[scrB + (ty * 32 + tx) * 2:scrB + (ty * 32 + tx) * 2 + 2])[0]
                    t = se & 0x3ff
                    pn = (se >> 12) & 0xf
                    hf = bool(se & 0x400)
                    vf = bool(se & 0x800)
                    if c256:
                        base = charB + t * 64
                        for yy in range(8):
                            for xx in range(8):
                                v = vram[base + yy * 8 + xx]
                                if v == 0:
                                    continue
                                sx = xx if not hf else 7 - xx
                                sy = yy if not vf else 7 - yy
                                px[tx * 8 + sx, ty * 8 + sy] = col(0, v)
                    else:
                        base = charB + t * 32
                        for yy in range(8):
                            for xx in range(0, 8, 2):
                                b = vram[base + yy * 4 + xx // 2]
                                lo = b & 0xf
                                hi = b >> 4
                                for k, v in ((0, lo), (1, hi)):
                                    if v == 0:
                                        continue
                                    X = xx + k
                                    sx = X if not hf else 7 - X
                                    sy = yy if not vf else 7 - yy
                                    px[tx * 8 + sx, ty * 8 + sy] = col(pn, v)
        return img

    # ── verify_title.py 일반화 ──
    def rgb16(c):
        return ((c & 31) << 3, ((c >> 5) & 31) << 3, ((c >> 10) & 31) << 3)

    def prgb(pn, i):
        return rgb16(struct.unpack("<H", pal[pn * 32 + i * 2:pn * 32 + i * 2 + 2])[0])

    def prgb8(i):
        return rgb16(struct.unpack("<H", pal[i * 2:i * 2 + 2])[0])

    img = Image.new("RGB", (240, 160), rgb16(struct.unpack("<H", pal[0:2])[0]))
    # layers는 뒤(아래)에서 앞(위) 순으로 그려짐
    for N, rom, base in layers:
        cnt = struct.unpack("<H", io[8 + N * 2:10 + N * 2])[0]
        charbase = ((cnt >> 2) & 3) * 0x4000
        scrbase = ((cnt >> 8) & 0x1F) * 0x800
        bpp8 = bool(cnt & 0x80)
        tsz = 64 if bpp8 else 32
        for ty in range(21):
            for tx in range(30):
                se = struct.unpack(
                    "<H", vram[scrbase + (ty * 32 + tx) * 2:scrbase + (ty * 32 + tx) * 2 + 2])[0]
                t = se & 0x3FF
                if not t:
                    continue
                pn = (se >> 12) & 0xF
                hf = bool(se & 0x400)
                vf = bool(se & 0x800)
                td = (rom[base + t * tsz:base + t * tsz + tsz] if rom is not None
                      else vram[charbase + t * tsz:charbase + t * tsz + tsz])
                for yy in range(8):
                    for xx in range(8):
                        sx = 7 - xx if hf else xx
                        sy = 7 - yy if vf else yy
                        if bpp8:
                            v = td[sy * 8 + sx]
                        else:
                            b = td[sy * 4 + sx // 2]
                            v = (b >> 4) if sx & 1 else (b & 0xF)
                        if not v:
                            continue
                        px, py = tx * 8 + xx, ty * 8 + yy
                        if px < 240 and py < 160:
                            img.putpixel((px, py), prgb8(v) if bpp8 else prgb(pn, v))
    return img


def grid_overlay(frame: dict, n: int, rom: bytes = None, rom_base: int = None) -> Image.Image:
    """BG n을 6배 확대 렌더 + 8px 그리드/셀좌표 오버레이(grid_bg.py 동등).

    타일 소스: rom+rom_base 지정 시 ROM(grid_bg.py 원형), 아니면 VRAM charbase.
    4bpp/32x32 맵 전제(원본과 동일). 타일 0 스킵, 인덱스 0은 배경색 (40,40,40).
    """
    vram = frame["vram"]
    io = frame["io"]
    pal = frame["palette"]
    cnt = struct.unpack("<H", io[8 + n * 2:8 + n * 2 + 2])[0]
    charbase = ((cnt >> 2) & 3) * 0x4000
    scrbase = ((cnt >> 8) & 0x1F) * 0x800

    def rgb(i, pn):
        b = pn * 32
        c = struct.unpack("<H", pal[b + i * 2:b + i * 2 + 2])[0]
        return ((c & 31) << 3, ((c >> 5) & 31) << 3, ((c >> 10) & 31) << 3)

    Z = 6
    img = Image.new("RGB", (32 * 8 * Z, 32 * 8 * Z), (40, 40, 40))
    d = ImageDraw.Draw(img)
    for ty in range(32):
        for tx in range(32):
            se = struct.unpack(
                "<H", vram[scrbase + (ty * 32 + tx) * 2:scrbase + (ty * 32 + tx) * 2 + 2])[0]
            t = se & 0x3FF
            if not t:
                continue
            pn = (se >> 12) & 0xF
            hf = bool(se & 0x400)
            vf = bool(se & 0x800)
            src = vram if rom is None else rom
            off = (charbase if rom is None else rom_base) + t * 32
            cell = Image.new("RGB", (8, 8), (40, 40, 40))
            for yy in range(8):
                for xx in range(0, 8, 2):
                    b = src[off + yy * 4 + xx // 2]
                    lo, hi = b & 0xF, b >> 4
                    if lo:
                        cell.putpixel((xx, yy), rgb(lo, pn))
                    if hi:
                        cell.putpixel((xx + 1, yy), rgb(hi, pn))
            if hf:
                cell = cell.transpose(Image.FLIP_LEFT_RIGHT)
            if vf:
                cell = cell.transpose(Image.FLIP_TOP_BOTTOM)
            img.paste(cell.resize((8 * Z, 8 * Z), Image.NEAREST), (tx * 8 * Z, ty * 8 * Z))
    for i in range(33):
        d.line([(i * 8 * Z, 0), (i * 8 * Z, 32 * 8 * Z)], fill=(0, 80, 0), width=1)
        d.line([(0, i * 8 * Z), (32 * 8 * Z, i * 8 * Z)], fill=(0, 80, 0), width=1)
    for ty in range(0, 32, 2):
        for tx in range(0, 32, 2):
            d.text((tx * 8 * Z + 1, ty * 8 * Z + 1), f"{tx},{ty}", fill=(255, 255, 0))
    return img


def obj_list(frame: dict) -> list:
    """OAM 파싱 → 활성 OBJ 리스트(analyze.py 동등).

    disable 비트(affine 아님+hidden) 및 미정의 (shape,size) 조합 제외.
    리턴: [{"index","x","y","w","h","tile","palno","prio"}] — w/h는 타일 단위.
    """
    oam = frame["oam"]
    objs = []
    for i in range(128):
        a0, a1, a2 = struct.unpack("<HHH", oam[i * 8:i * 8 + 6])
        if (a0 >> 8) & 3 == 2:  # disable bit (not affine, hidden)
            continue
        y = a0 & 0xFF
        shape = (a0 >> 14) & 3
        x = a1 & 0x1FF
        size = (a1 >> 14) & 3
        tile = a2 & 0x3FF
        palno = (a2 >> 12) & 0xF
        prio = (a2 >> 10) & 3
        if (shape, size) not in OBJ_SIZES:
            continue
        w, h = OBJ_SIZES[(shape, size)]
        # 화면 밖/투명 자잘한 것 제외 안 함 — 전부 기록
        objs.append({"index": i, "x": x, "y": y, "w": w, "h": h,
                     "tile": tile, "palno": palno, "prio": prio})
    return objs


def obj_sheet(frame: dict) -> Image.Image:
    """OBJ VRAM 타일 시트(팔레트 0 기준, 식별용) 렌더 — analyze.py objsheet_pal0 동등.

    OBJ 타일 영역 = vram[0x10000:], 32열 격자, 배경 (40,40,40,255).
    """
    vram = frame["vram"]
    pal = frame["palette"]

    def color(idx, palbase):
        if idx == 0:
            return (0, 0, 0, 0)
        c = struct.unpack("<H", pal[palbase + idx * 2:palbase + idx * 2 + 2])[0]
        r = (c & 31) << 3
        g = ((c >> 5) & 31) << 3
        b = ((c >> 10) & 31) << 3
        return (r, g, b, 255)

    def tile4(base_off, tile_no, palno, obj=True):
        """4bpp 8x8 타일 → RGBA list. OBJ palette=0x200+, BG=0x000+."""
        palbase = (0x200 if obj else 0) + palno * 32
        off = base_off + tile_no * 32
        px = []
        for y in range(8):
            for x in range(0, 8, 2):
                b = vram[off + y * 4 + x // 2]
                px.append(color(b & 0xF, palbase))
                px.append(color(b >> 4, palbase))
        return px

    ntiles = (len(vram) - OBJ_TILE_BASE) // 32
    cols = 32
    rows = (ntiles + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * 8, rows * 8), (40, 40, 40, 255))
    for t in range(ntiles):
        px = tile4(OBJ_TILE_BASE, t, 0)  # pal 0 기준(식별용)
        im = Image.new("RGBA", (8, 8))
        im.putdata(px)
        sheet.paste(im, ((t % cols) * 8, (t // cols) * 8), im)
    return sheet
