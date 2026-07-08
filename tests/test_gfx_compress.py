#!/usr/bin/env python3
"""gfx 압축 파이프라인 회귀 테스트 — 코덱/역탐색/8bpp/압축 재작화/runner 게이트.

합성 프레임·합성 ROM 만 사용(외부 게임 자산 불필요, 폰트만 시스템/NAS 후보).
실행: /opt/retro-kr-patch-venv/bin/python tests/test_gfx_compress.py
"""
import json
import os
import random
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gfxlib import compress as C, redraw, gba, manifest, runner  # noqa: E402

FONT_CANDIDATES = [
    "/mnt/synology_devdata/projects/kor-trans/gashbell-gba/assets/NeoDunggeunmo.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT = next((f for f in FONT_CANDIDATES if os.path.isfile(f)), None)

_pass = _fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail += 1
        print(f"[FAIL] {name} {detail}")


random.seed(20260708)


def mkframe(bpp8, tiles_n=96):
    """합성 프레임: BG0 mode0, charbase 0x4000, scrbase 0x1000, t=1..48 사용."""
    tsz = 64 if bpp8 else 32
    io = bytearray(0x400)
    struct.pack_into("<H", io, 0, 0x0100)
    struct.pack_into("<H", io, 8, (1 << 2) | (2 << 8) | (0x80 if bpp8 else 0))
    pal = bytearray(0x400)
    for i in range(1, 9):
        v = i * 3
        struct.pack_into("<H", pal, i * 2, v | (v << 5) | (v << 10))
    vram = bytearray(0x18000)
    tiles = bytes(random.choice(b"\x01\x02\x03\x04\x05")
                  for _ in range(tiles_n * tsz))
    vram[0x4000 + tsz:0x4000 + tsz + len(tiles)] = tiles
    for ty in range(4, 8):
        for tx in range(2, 14):
            t = 1 + (ty - 4) * 12 + (tx - 2)
            struct.pack_into("<H", vram, 0x1000 + (ty * 32 + tx) * 2, t)
    return ({"io": bytes(io), "palette": bytes(pal), "vram": bytes(vram),
             "oam": bytes(0x400)}, tiles, tsz)


# ── 1. 코덱 라운드트립 ───────────────────────────────────────────────
cases = [b"\x00" * 512, bytes(range(256)) * 4, os.urandom(1024),
         bytes(random.choice(b"ABCD") for _ in range(4096)),
         b"ABCABCABCABC" * 100 + os.urandom(37), os.urandom(1), b"\x11" * 3]
ok = True
for raw in cases:
    for vs in (True, False):
        comp = C.lz77_compress(raw, vram_safe=vs)
        dec, _, _ = C.decompress(comp, 0)
        ok &= dec == raw and len(comp) % 4 == 0
    dec, _, k = C.decompress(C.rle_compress(raw), 0)
    ok &= dec == raw and k == "rle"
check("1 코덱 라운드트립(lz77 vs/nvs + rle)", ok)

# vram_safe: disp>=2 전수 파스
raw = bytes(random.choice(b"AB") for _ in range(4096))
comp = C.lz77_compress(raw, vram_safe=True)
p, outn = 4, 0
size = struct.unpack_from("<I", comp)[0] >> 8
mind = 9999
while outn < size:
    f = comp[p]; p += 1
    for b in range(7, -1, -1):
        if outn >= size:
            break
        if f & (1 << b):
            b1, b2 = comp[p], comp[p + 1]; p += 2
            mind = min(mind, (((b1 & 0xF) << 8) | b2) + 1)
            outn += (b1 >> 4) + 3
        else:
            p += 1; outn += 1
check("2 vram_safe disp>=2", mind >= 2, f"min disp={mind}")

# ── 2. scan + locate_compressed ─────────────────────────────────────
fr4, tiles4, _ = mkframe(False)
comp4 = C.lz77_compress(tiles4)
rom = bytearray(os.urandom(0x40000))
rom[0x2000:0x2000 + len(comp4)] = comp4
loc = redraw.locate(fr4, 0, bytes(rom))
check("3 무압축 locate 실패(압축본만 존재)", loc["base"] is None)
lc = C.locate_compressed(fr4, 0, bytes(rom))
check("4 locate_compressed 확정", lc.get("base") is not None
      and lc.get("comp_off") == 0x2000 and lc.get("kind") == "lz77", lc)

# ── 3. 압축 재작화 == 무압축 rect 엔진 비트동일 ─────────────────────
if FONT:
    rom2 = bytearray(rom)
    r = redraw.redraw_rect_compressed(
        rom2, fr4, 0, lc["comp_off"], lc["base"], (2, 4, 13, 7), "난이도",
        font_path=FONT)
    check("5 압축 재작화 ok+예산 내", bool(r.get("ok"))
          and r["compress"]["ok"], r.get("compress"))
    buf_after, _, _ = C.decompress(bytes(rom2), lc["comp_off"])
    ref = bytearray(tiles4)
    redraw.redraw_rect(ref, fr4, 0, lc["base"], (2, 4, 13, 7), "난이도",
                       font_path=FONT)
    check("6 압축경로 산출 == 무압축 rect 엔진", bytes(buf_after) == bytes(ref))
    rom3 = bytearray(rom2)
    r2 = redraw.redraw_rect_compressed(
        rom3, fr4, 0, lc["comp_off"], lc["base"], (2, 4, 13, 7), "다른글",
        font_path=FONT, dry_run=True)
    check("7 dry_run ROM 불변", bytes(rom3) == bytes(rom2)
          and r2["compress"].get("skipped"))

    # ── 4. 8bpp 무압축 재작화 ───────────────────────────────────────
    fr8, tiles8, tsz8 = mkframe(True)
    rom8 = bytearray(os.urandom(0x40000))
    rom8[0x3000:0x3000 + len(tiles8)] = tiles8
    base8 = 0x3000 - tsz8
    r8 = redraw.redraw_rect(rom8, fr8, 0, base8, (2, 4, 13, 7), "옵션",
                            font_path=FONT)
    changed = sum(1 for t in range(1, 49)
                  if rom8[base8 + t * 64:base8 + t * 64 + 64]
                  != tiles8[(t - 1) * 64:t * 64])
    check("8 8bpp redraw(48타일 실변경)", r8.get("ok") and r8.get("bpp8")
          and changed == 48, {"changed": changed})

    # ── 5. runner 압축 스텝 + 게이트 ────────────────────────────────
    proj = Path(tempfile.mkdtemp(prefix="krgfx_test_"))
    (proj / "frames").mkdir()
    for nm in ("io", "palette", "vram", "oam"):
        (proj / "frames" / f"{nm}.bin").write_bytes(fr4[nm])
    (proj / "base.gba").write_bytes(bytes(rom))
    mf = {"font": FONT, "rom": "base.gba", "platform": "gba",
          "screens": {"synth": {"legacy_frames": "frames"}},
          "chains": {"t": {"base": "base.gba", "out": "out_{version}.gba",
                           "steps": [{"name": "logo", "type": "regions",
                                      "screen": "synth", "bg": 0,
                                      "rom_base": hex(lc["base"]),
                                      "comp_off": hex(lc["comp_off"]),
                                      "items": [{"rect": "2,4,13,7",
                                                 "text": "난이도"}]}]}}}
    (proj / "krpatch.gfx.json").write_text(
        json.dumps(mf, ensure_ascii=False, indent=1))
    cfg = manifest.load_gfx_config(str(proj))
    check("9 매니페스트 comp_off 검증 통과", not cfg.get("errors"),
          cfg.get("errors"))
    res = runner.run_chain(str(proj), cfg, "t", "v1")
    check("10 runner 압축 스텝 verdict pass",
          res.get("verdict") in ("pass", "pass_with_warnings"),
          {k: res.get(k) for k in ("verdict", "first_fail", "attention")})
    if res.get("out"):
        out_rom = Path(res["out"]).read_bytes()
        buf_out, _, _ = C.decompress(out_rom, lc["comp_off"])
        check("11 체인 산출 == 압축경로 산출", buf_out == bytes(ref))

    # 예산 초과 → 미기록: 원본 블록이 단색(극압축)이라 슬롯이 극소 →
    # 글리프 기록으로 엔트로피 증가 → 재압축이 슬롯 초과
    uni = b"\x11" * (49 * 32)
    compU = C.lz77_compress(uni)
    romT = bytearray(os.urandom(0x40000))
    romT[0x2000:0x2000 + len(compU)] = compU
    snap = bytes(romT)
    rr = redraw.redraw_rect_compressed(
        romT, fr4, 0, 0x2000, 0, (2, 4, 13, 7), "난이도", font_path=FONT)
    check("12 예산 초과 시 미기록+ok=False",
          rr.get("ok") is False and rr["compress"].get("ok") is False
          and bytes(romT) == snap,
          {"slot": ((len(compU) + 3) // 4) * 4,
           "need": rr["compress"].get("need")})
else:
    print("[SKIP] 폰트 없음 — 재작화 계열 테스트 생략")

print(f"\n{_pass}/{_pass + _fail} 통과")
sys.exit(1 if _fail else 0)
