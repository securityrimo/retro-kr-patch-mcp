"""GBA BIOS 압축 코덱 + ROM 압축블록 스캔 + 압축 그래픽 역탐색.

목적: locate(무압축 rom.find)가 실패하는 "blocked" 그래픽(타이틀 로고 등
LZ77/RLE 압축 타일)의 재작화 경로 개방.

포맷(GBA BIOS SWI 규약):
  LZ77 (0x10): u32 헤더 = 0x10 | decomp_size<<8. 이후 flag byte(MSB first),
    bit=1 → 2바이트 참조: b1=(len-3)<<4 | disp_hi, b2=disp_lo, disp=((b1&0xF)<<8|b2)+1
    bit=0 → 리터럴 1바이트.
  RLE  (0x30): u32 헤더 = 0x30 | decomp_size<<8. flag bit7=1 → run len=(f&0x7F)+3
    반복 1바이트, bit7=0 → 리터럴 len=(f&0x7F)+1.

VRAM-safe: LZ77UnCompVram은 16비트 단위 기록 — disp>=2 로 압축해야 VRAM 대상
디코드가 안전하다(compress 기본값 vram_safe=True).

게임 특화 상수 없음. 모든 함수는 JSON 직렬화 가능 dict 또는 bytes 반환.
"""
import struct

LZ77_TYPE = 0x10
RLE_TYPE = 0x30

# 스캔 시 허용 decomp_size 기본 범위(타일 그래픽 상식선)
_SCAN_MIN = 0x20
_SCAN_MAX = 0x20000


class CompressError(ValueError):
    pass


# ── 해제 ─────────────────────────────────────────────────────────────


def lz77_decompress(data, off=0):
    """off의 LZ77(0x10) 블록 해제 → (bytes, comp_size). 불량이면 CompressError.

    comp_size = 헤더 4B 포함 소비 바이트 수(패딩 미포함).
    strict: disp가 기존 산출 범위를 벗어나면(자기참조 이전) 오류.
    """
    if off + 4 > len(data) or data[off] != LZ77_TYPE:
        raise CompressError("LZ77 헤더 아님")
    size = struct.unpack_from("<I", data, off)[0] >> 8
    if size == 0:
        raise CompressError("decomp_size 0")
    out = bytearray()
    p = off + 4
    n = len(data)
    while len(out) < size:
        if p >= n:
            raise CompressError("입력 소진(flag)")
        flags = data[p]
        p += 1
        for bit in range(7, -1, -1):
            if len(out) >= size:
                break
            if flags & (1 << bit):
                if p + 2 > n:
                    raise CompressError("입력 소진(ref)")
                b1, b2 = data[p], data[p + 1]
                p += 2
                length = (b1 >> 4) + 3
                disp = (((b1 & 0xF) << 8) | b2) + 1
                if disp > len(out):
                    raise CompressError("disp 범위 초과")
                for _ in range(length):
                    out.append(out[-disp])
            else:
                if p >= n:
                    raise CompressError("입력 소진(lit)")
                out.append(data[p])
                p += 1
    if len(out) != size:
        raise CompressError("초과 산출")
    return bytes(out), p - off


def rle_decompress(data, off=0):
    """off의 RLE(0x30) 블록 해제 → (bytes, comp_size)."""
    if off + 4 > len(data) or data[off] != RLE_TYPE:
        raise CompressError("RLE 헤더 아님")
    size = struct.unpack_from("<I", data, off)[0] >> 8
    if size == 0:
        raise CompressError("decomp_size 0")
    out = bytearray()
    p = off + 4
    n = len(data)
    while len(out) < size:
        if p >= n:
            raise CompressError("입력 소진(flag)")
        f = data[p]
        p += 1
        if f & 0x80:
            ln = (f & 0x7F) + 3
            if p >= n:
                raise CompressError("입력 소진(run)")
            out.extend(data[p:p + 1] * ln)
            p += 1
        else:
            ln = (f & 0x7F) + 1
            if p + ln > n:
                raise CompressError("입력 소진(lit)")
            out.extend(data[p:p + ln])
            p += ln
    if len(out) != size:
        raise CompressError("초과 산출")
    return bytes(out), p - off


def decompress(data, off=0):
    """헤더 타입 자동 판별 해제 → (bytes, comp_size, kind:"lz77"|"rle")."""
    t = data[off] if off < len(data) else -1
    if t == LZ77_TYPE:
        buf, sz = lz77_decompress(data, off)
        return buf, sz, "lz77"
    if t == RLE_TYPE:
        buf, sz = rle_decompress(data, off)
        return buf, sz, "rle"
    raise CompressError(f"미지원 헤더 타입: {t:#x}" if t >= 0 else "범위 밖")


# ── 압축 ─────────────────────────────────────────────────────────────


def _longest_matches(raw, min_disp, max_tries=256):
    """모든 위치의 최장일치 (maxlen[i], disp[i]) — 해시체인.

    매치 비용은 disp 무관 고정 2B라 위치별 최장 길이만 있으면 optimal parse 가능
    (최장매치의 접두 길이는 같은 disp로 항상 유효).
    """
    n = len(raw)
    maxlen = [0] * n
    disps = [0] * n
    head = {}
    for pos in range(n):
        if pos + 3 <= n:
            key = raw[pos:pos + 3]
            best_len = 0
            best_disp = 0
            tries = 0
            for cand in reversed(head.get(key, ())):
                disp = pos - cand
                if disp > 0x1000:
                    break
                if disp < min_disp:
                    continue
                tries += 1
                if tries > max_tries:
                    break
                ln = 0
                limit = min(18, n - pos)
                # 오버랩 매치: 산출 바이트 = out[pos+ln-disp] = raw[pos+ln-disp]
                while ln < limit and raw[pos + ln - disp] == raw[pos + ln]:
                    ln += 1
                if ln > best_len:
                    best_len, best_disp = ln, disp
                    if ln >= 18:
                        break
            maxlen[pos], disps[pos] = best_len, best_disp
            head.setdefault(key, []).append(pos)
    return maxlen, disps


def lz77_compress(raw, vram_safe=True):
    """LZ77(0x10) 압축 — optimal parse(DP, 비트비용 lit=9/match=17).

    in-place 슬롯 예산이 빡빡한 재압축 용도라 greedy 대신 최적 파스를 쓴다.
    vram_safe=True면 disp>=2 (LZ77UnCompVram 16비트 기록 안전).
    반환 bytes는 4바이트 정렬 패딩(0x00) 포함. 라운드트립 비트동일 보장.
    """
    n = len(raw)
    if n == 0 or n > 0xFFFFFF:
        raise CompressError(f"크기 불가: {n}")
    maxlen, disps = _longest_matches(raw, 2 if vram_safe else 1)
    # DP 후방: cost[i] = i부터 끝까지 최소 비트, choice[i] = 채택 길이(1=리터럴)
    INF = float("inf")
    cost = [0] * (n + 1)
    choice = [1] * (n + 1)
    for i in range(n - 1, -1, -1):
        best = cost[i + 1] + 9
        ch = 1
        ml = maxlen[i]
        if ml >= 3:
            for ln in range(3, ml + 1):
                c = cost[i + ln] + 17
                if c < best:
                    best = c
                    ch = ln
        cost[i] = best
        choice[i] = ch
    # 토큰 방출
    out = bytearray(struct.pack("<I", LZ77_TYPE | (n << 8)))
    pos = 0
    while pos < n:
        flag_at = len(out)
        out.append(0)
        flags = 0
        for bit in range(7, -1, -1):
            if pos >= n:
                break
            ln = choice[pos]
            if ln >= 3:
                disp = disps[pos]
                flags |= (1 << bit)
                out.append(((ln - 3) << 4) | (((disp - 1) >> 8) & 0xF))
                out.append((disp - 1) & 0xFF)
                pos += ln
            else:
                out.append(raw[pos])
                pos += 1
        out[flag_at] = flags
    while len(out) % 4:
        out.append(0)
    return bytes(out)


def rle_compress(raw):
    """RLE(0x30) 압축. 반환 bytes는 4바이트 정렬 패딩 포함."""
    n = len(raw)
    if n == 0 or n > 0xFFFFFF:
        raise CompressError(f"크기 불가: {n}")
    out = bytearray(struct.pack("<I", RLE_TYPE | (n << 8)))
    pos = 0
    lit_start = pos

    def _flush_lit(end):
        s = lit_start
        while s < end:
            ln = min(0x80, end - s)
            out.append(ln - 1)
            out.extend(raw[s:s + ln])
            s += ln

    while pos < n:
        run = 1
        while pos + run < n and run < 0x82 and raw[pos + run] == raw[pos]:
            run += 1
        if run >= 3:
            _flush_lit(pos)
            out.append(0x80 | (run - 3))
            out.append(raw[pos])
            pos += run
            lit_start = pos
        else:
            pos += run
    _flush_lit(pos)
    while len(out) % 4:
        out.append(0)
    return bytes(out)


# ── ROM 스캔 · 역탐색 ────────────────────────────────────────────────


def scan_blocks(rom, kinds=("lz77", "rle"), min_size=_SCAN_MIN,
                max_size=_SCAN_MAX, align=4, limit=0):
    """ROM 전체에서 유효 압축블록 후보 스캔.

    판정 = 헤더 타입/size 범위 + strict 해제 성공. align 정렬 오프셋만 검사
    (GBA 압축 데이터는 관행상 4바이트 정렬). limit>0 이면 그 수에서 중단.
    반환: [{"off","kind","comp_size","decomp_size"}] (off 오름차순)
    """
    want = set()
    if "lz77" in kinds:
        want.add(LZ77_TYPE)
    if "rle" in kinds:
        want.add(RLE_TYPE)
    res = []
    n = len(rom)
    off = 0
    while off + 8 <= n:
        t = rom[off]
        if t in want:
            size = struct.unpack_from("<I", rom, off)[0] >> 8
            if min_size <= size <= max_size:
                try:
                    buf, csz, kind = decompress(rom, off)
                except CompressError:
                    buf = None
                if buf is not None:
                    res.append({"off": off, "kind": kind, "comp_size": csz,
                                "decomp_size": size})
                    if limit and len(res) >= limit:
                        break
                    off += ((csz + align - 1) // align) * align
                    continue
        off += align
    return res


def _used_tiles(frame, bg, plat):
    """locate()와 동일한 사용-타일 수집(잉크 8px 미만 제외).

    반환: (tiles:{t: bytes}, tsz, charbase)
    """
    io = frame["io"]
    vram = frame["vram"]
    c = plat.bgcnt(io, bg)
    tsz = 64 if c["bpp8"] else 32
    used = {}
    for tx, ty, t, pn, hf, vf in plat.screen_entries(
            vram, c["scrbase"], c["size"]):
        if t:
            used[t] = None
    tiles = {}
    for t in sorted(used):
        tb = vram[c["charbase"] + t * tsz: c["charbase"] + t * tsz + tsz]
        if sum(1 for b in tb if b) < 8:
            continue
        tiles[t] = tb
    return tiles, tsz, c["charbase"]


def locate_compressed(frame, bg, rom, plat=None, blocks=None,
                      min_size=_SCAN_MIN, max_size=_SCAN_MAX):
    """압축블록 안에서 BG 사용 타일의 원본 위치 역탐색(locate의 압축판).

    blocks 미지정 시 scan_blocks 수행(수 MB ROM에서 수 초). 확정 기준은
    locate와 동일: ok >= max(2, checked//2).
    반환: {"comp_off","kind","comp_size","decomp_size","base","ok","checked"}
          | {"base": None, "ok", "checked", "blocks_scanned"}
    base = 해제 버퍼 내 타일0 기준 오프셋(locate의 base와 동일 시맨틱).
    """
    if plat is None:
        from . import gba as plat
    tiles, tsz, _ = _used_tiles(frame, bg, plat)
    if blocks is None:
        blocks = scan_blocks(rom, min_size=max(min_size, tsz),
                             max_size=max_size)
    best = {"base": None, "ok": 0, "checked": 0,
            "blocks_scanned": len(blocks)}
    for blk in blocks:
        try:
            buf, _, _ = decompress(rom, blk["off"])
        except CompressError:
            continue
        for t, tb in tiles.items():
            k = buf.find(tb)
            if k < 0:
                continue
            base = k - t * tsz
            ok = chk = 0
            for t2, tb2 in tiles.items():
                chk += 1
                o = base + t2 * tsz
                if 0 <= o <= len(buf) - tsz and buf[o:o + tsz] == tb2:
                    ok += 1
            if ok >= max(2, chk // 2):
                return {"comp_off": blk["off"], "kind": blk["kind"],
                        "comp_size": blk["comp_size"],
                        "decomp_size": blk["decomp_size"],
                        "base": base, "ok": ok, "checked": chk}
            if ok > best["ok"]:
                best.update({"ok": ok, "checked": chk})
            break   # 이 블록의 앵커 매칭 1회로 충분(locate와 동일 전략)
    return best


def patch_compressed(rom, comp_off, new_raw, kind="lz77", vram_safe=True):
    """해제 버퍼 new_raw를 재압축해 rom의 comp_off 블록에 in-place 패치.

    예산 = 기존 블록 comp_size(패딩 4정렬 포함 슬롯). 초과 시 기록하지 않고
    {"ok": False, "need", "budget"} 반환. 성공 시 여분은 0x00 패딩.
    rom은 bytearray(in-place 수정).
    """
    old_raw, old_csz, old_kind = decompress(bytes(rom), comp_off)
    slot = ((old_csz + 3) // 4) * 4
    if kind == "rle":
        comp = rle_compress(new_raw)
    else:
        comp = lz77_compress(new_raw, vram_safe=vram_safe)
    # 라운드트립 자가검증(게이트 이전 최후방어)
    rt, _, _ = decompress(comp, 0)
    if rt != bytes(new_raw):
        raise CompressError("재압축 라운드트립 불일치")
    if len(comp) > slot:
        return {"ok": False, "need": len(comp), "budget": slot,
                "old_kind": old_kind,
                "err": f"압축 결과 {len(comp)}B > 슬롯 {slot}B"}
    rom[comp_off:comp_off + len(comp)] = comp
    for i in range(comp_off + len(comp), comp_off + slot):
        rom[i] = 0
    return {"ok": True, "written": len(comp), "budget": slot,
            "slack": slot - len(comp)}
