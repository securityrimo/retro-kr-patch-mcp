"""GBA 하드웨어 프리미티브 — 프레임 덤프 로드, IO 레지스터 파싱, 타일 인코딩/디코딩.

갓슈벨 gfx_re 검증 스크립트(locate_bg/ss_dump/bg_render/redraw_region)에서
복사-추출. 알고리즘 무변경 — 비트 동일성이 회귀 기준.

frame_dir 형식(기존 바이트호환):
  io.bin(0x400) palette.bin(0x400) vram.bin(0x18000) oam.bin(0x400)
  + 선택 iwram.bin(0x8000) wram.bin(0x40000)
"""
import os
import struct

# BGxCNT size 비트 → (mapw, maph) 타일 단위 (bg_render.py / locate_bg.py 동일)
MAP_SIZES = [(32, 32), (64, 32), (32, 64), (64, 64)]


def load_frame(frame_dir: str) -> dict:
    """frame_dir에서 메모리 리전 .bin들을 읽어 dict로 반환.

    필수: io/palette/vram/oam. 선택: iwram/wram(존재 시에만 포함).
    리턴: {"io": bytes, "palette": bytes, "vram": bytes, "oam": bytes[, "iwram", "wram"]}
    """
    frame = {}
    for name in ("io", "palette", "vram", "oam"):
        with open(os.path.join(frame_dir, name + ".bin"), "rb") as f:
            frame[name] = f.read()
    for name in ("iwram", "wram"):
        p = os.path.join(frame_dir, name + ".bin")
        if os.path.exists(p):
            with open(p, "rb") as f:
                frame[name] = f.read()
    return frame


def dispcnt(io: bytes) -> dict:
    """DISPCNT(io[0:2]) 파싱.

    리턴: {"mode", "bg": [bool]*4, "obj", "forced_blank", "obj_1d", "raw"}
    """
    d = struct.unpack("<H", io[0:2])[0]
    return {
        "mode": d & 7,
        "bg": [bool(d & (0x100 << n)) for n in range(4)],
        "obj": bool(d & 0x1000),
        "forced_blank": bool(d & 0x80),
        "obj_1d": bool(d & 0x40),
        "raw": d,
    }


def bgcnt(io: bytes, n: int) -> dict:
    """BGnCNT(io[8+n*2:]) 파싱. charbase/scrbase는 바이트 오프셋으로 환산해 반환.

    리턴: {"prio", "charbase"(*0x4000), "scrbase"(*0x800), "bpp8", "size", "raw"}
    """
    c = struct.unpack("<H", io[8 + n * 2:8 + n * 2 + 2])[0]
    return {
        "prio": c & 3,
        "charbase": ((c >> 2) & 3) * 0x4000,
        "scrbase": ((c >> 8) & 0x1F) * 0x800,
        "bpp8": bool(c & 0x80),
        "size": (c >> 14) & 3,
        "raw": c,
    }


def pal_rgb(pal: bytes, idx: int, palno: int = 0, bpp8: bool = False) -> tuple:
    """BG 팔레트 엔트리 → RGB888 튜플. RGB555 각 성분 <<3 (원본 스크립트 공통 방식).

    bpp8=True면 256색 단일 뱅크(팔레트 선두), False면 palno*32 뱅크의 16색.
    """
    base = 0 if bpp8 else palno * 32
    c = struct.unpack("<H", pal[base + idx * 2:base + idx * 2 + 2])[0]
    return ((c & 31) << 3, ((c >> 5) & 31) << 3, ((c >> 10) & 31) << 3)


def screen_entries(vram: bytes, scrbase: int, size: int) -> list:
    """텍스트 BG 스크린엔트리 전수 파싱(32/64 맵의 0x800 블록 처리 포함).

    bg_render.py / locate_bg.py의 blk 산식 그대로.
    리턴: [(tx, ty, tile, palno, hf, vf)] — 타일 0 포함 전수(필터는 호출측).
    """
    mapw, maph = MAP_SIZES[size]
    out = []
    for ty in range(maph):
        for tx in range(mapw):
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
            out.append((tx, ty, se & 0x3FF, (se >> 12) & 0xF,
                        bool(se & 0x400), bool(se & 0x800)))
    return out


def dec4(data: bytes, off: int) -> list:
    """4bpp 8x8 타일(32B) 디코드 → [[int]*8]*8 (팔레트 인덱스)."""
    cell = [[0] * 8 for _ in range(8)]
    for y in range(8):
        for x in range(0, 8, 2):
            b = data[off + y * 4 + x // 2]
            cell[y][x] = b & 0xF
            cell[y][x + 1] = b >> 4
    return cell


def dec8(data: bytes, off: int) -> list:
    """8bpp 8x8 타일(64B) 디코드 → [[int]*8]*8."""
    return [[data[off + y * 8 + x] for x in range(8)] for y in range(8)]


def enc4(cell: list) -> bytes:
    """8x8 팔레트 인덱스 → 4bpp 32B. redraw_region.py의 비트 배치 그대로
    (짝수 픽셀=하위 니블, 홀수 픽셀=상위 니블)."""
    enc = bytearray(32)
    for yy in range(8):
        for xx in range(0, 8, 2):
            enc[yy * 4 + xx // 2] = (cell[yy][xx] & 0xF) | ((cell[yy][xx + 1] & 0xF) << 4)
    return bytes(enc)


def enc8(cell: list) -> bytes:
    """8x8 팔레트 인덱스 → 8bpp 64B."""
    enc = bytearray(64)
    for yy in range(8):
        for xx in range(8):
            enc[yy * 8 + xx] = cell[yy][xx] & 0xFF
    return bytes(enc)


def flip(cell: list, hf: bool, vf: bool) -> list:
    """8x8 셀 좌우/상하 반전(redraw_region.py 순서: hf 먼저, vf 다음). 새 리스트 반환."""
    out = [row[:] for row in cell]
    if hf:
        out = [row[::-1] for row in out]
    if vf:
        out = out[::-1]
    return out
