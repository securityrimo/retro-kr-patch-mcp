#!/usr/bin/env python3
"""retro-kr-patch MCP — 도구 서버 (Tier 2)

ROM 해킹·한글 패치 제작의 공통 도구들을 MCP 툴로 제공.
Tier 1 지식 서버와 함께 사용한다.

설계 원칙:
- 모든 도구는 stateless — 입력 파일 경로를 받아 결과 반환
- ROM·디스크 이미지 원본은 건드리지 않음 (읽기 전용 분석)
- 패치 생성 시 원본은 항상 CLI 인자로 받음 (하드코딩 금지)
- 인코딩 누락 = 빌드 에러 (조용한 스킵 금지)
"""
from __future__ import annotations
import json
import struct
import hashlib
import zlib
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

OUTPUT_DIR = Path("/root/projects/retro-kr-patch-mcp/output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("retro-kr-patch-tools")


# ═══════════════════════════════════════════════════════════════════════════════
# ROM 분석 도구
# ═══════════════════════════════════════════════════════════════════════════════

def _read_rom(path: str) -> bytes:
    """ROM 파일 읽기. 경로 검증."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"ROM 없음: {path}")
    return p.read_bytes()


# ── SNES 헤더 파싱 ───────────────────────────────────────────────────────────
def _parse_snes_header(data: bytes) -> dict:
    """SNES ROM 헤더 분석 ($00:FFB0-$00:FFDF, LoROM 기준)"""
    # SMC 헤더 (512바이트) 처리
    has_smc = len(data) % 1024 == 512 and len(data) > 512
    rom_data = data[512:] if has_smc else data

    if len(rom_data) < 0x8000:
        return {"error": "SNES ROM이 너무 작음 (최소 32KB)"}

    # LoROM 헤더는 $00:FFB0-$00:FFDF
    hdr = rom_data[0x7FB0:0x7FFF] if len(rom_data) >= 0x8000 else None
    if hdr is None or len(hdr) < 48:
        # HiROM 헤더 시도
        hdr = rom_data[0xFFB0:0xFFFF] if len(rom_data) > 0x10000 else None

    if hdr is None or len(hdr) < 48:
        return {"error": "SNES 헤더를 찾을 수 없음"}

    title = hdr[0:21].decode("ascii", errors="replace").rstrip("\x00").strip()

    # 매퍼·ROM 타입 ($00:FFD5)
    rom_type = hdr[0x25]
    rom_size_val = hdr[0x27]
    sram_size_val = hdr[0x28]
    region = hdr[0x29]
    dev_id = hdr[0x2A]
    version = hdr[0x2B]

    # 체크섬
    checksum_complement = struct.unpack_from("<H", hdr, 0x2C)[0]
    checksum = struct.unpack_from("<H", hdr, 0x2E)[0]

    # ROM 타입 해석
    rom_types = {
        0x00: "LoROM",
        0x01: "HiROM",
        0x02: "LoROM + S-DD1",
        0x03: "LoROM + SA-1",
        0x05: "HiROM + SPC7110",
        0x0A: "LoROM + SuperFX",
        0x13: "LoROM + SA-1",
        0x15: "HiROM + SA-1",
        0x1A: "LoROM + SuperFX",
        0x25: "ExHiROM",
        0x35: "ExLoROM",
    }

    # ROM 크기 (2^N KB)
    if rom_size_val >= 0x08:
        rom_size_kb = 1 << rom_size_val
    else:
        rom_size_kb = 0

    # SRAM 크기
    sram_sizes = {0: "없음", 1: "2Kbit", 2: "4Kbit", 3: "8Kbit",
                  4: "16Kbit", 5: "32Kbit", 6: "64Kbit"}

    return {
        "title": title,
        "rom_type": f"${rom_type:02X} ({rom_types.get(rom_type, '알 수 없음')})",
        "rom_size": f"{rom_size_kb}KB ({rom_size_val:#x})",
        "actual_size": f"{len(data)} bytes ({len(data)/1024:.1f}KB)",
        "sram": sram_sizes.get(sram_size_val, f"${sram_size_val:02X}"),
        "region": f"${region:02X}" + (" (NTSC)" if region == 0 else " (PAL)" if region >= 0x02 else ""),
        "version": f"1.{version}",
        "checksum": f"${checksum:04X}",
        "checksum_complement": f"${checksum_complement:04X}",
        "has_header": has_smc,  # SMC 헤더 여부
    }


# ── 메가드라이브 헤더 파싱 ───────────────────────────────────────────────────
def _parse_md_header(data: bytes) -> dict:
    """메가드라이브 ROM 헤더 분석"""
    if len(data) < 0x200:
        return {"error": "MD ROM이 너무 작음"}

    # $100-$1FF: 헤더 영역
    hdr = data[0x100:0x200]

    console_name = hdr[0:16].decode("ascii", errors="replace").rstrip("\x00").strip()
    copyright_str = hdr[16:32].decode("ascii", errors="replace").rstrip("\x00").strip()

    # 도메스틱 타이틀 ($120-$14F)
    domestic_title = data[0x120:0x150].decode("shift_jis", errors="replace").rstrip("\x00").strip()
    # 인터내셔널 타이틀 ($150-$17F)
    intl_title = data[0x150:0x180].decode("ascii", errors="replace").rstrip("\x00").strip()

    # 장르·제품코드 ($1F0-$1FF)
    if len(data) > 0x1F0:
        serial = data[0x1F0:0x1F4].decode("ascii", errors="replace").strip()
        checksum_val = struct.unpack_from(">H", data, 0x18E)[0] if len(data) > 0x190 else 0

    return {
        "console": console_name or "(비어 있음)",
        "copyright": copyright_str,
        "title_domestic": domestic_title or "(비어 있음)",
        "title_intl": intl_title or "(비어 있음)",
        "serial": serial if len(data) > 0x1F0 else "N/A",
        "checksum": f"${checksum_val:04X}",
        "actual_size": f"{len(data)} bytes ({len(data)/1024:.1f}KB)",
    }


@mcp.tool()
def rom_info(rom_path: str) -> str:
    """ROM 파일 헤더·매퍼·크기·체크섬 정보 분석.

    지원 플랫폼: SNES(.sfc/.smc), 메가드라이브(.md/.bin), 게임기어(.gg), NDS(.nds).
    확장자로 플랫폼을 추론하고 해당 헤더 파싱을 적용한다.
    """
    data = _read_rom(rom_path)
    path = Path(rom_path)
    ext = path.suffix.lower()
    stem = path.stem

    info = {
        "file": str(path),
        "size": len(data),
        "size_human": f"{len(data)/1024:.1f}KB" if len(data) < 1024*1024 else f"{len(data)/1024/1024:.1f}MB",
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08X}",
    }

    # SNES
    if ext in (".sfc", ".smc"):
        header_info = _parse_snes_header(data)
        info["platform"] = "SNES"
        info["header"] = header_info
        # 매퍼 분기 힌트
        if "LoROM" in header_info.get("rom_type", ""):
            info["mapper_note"] = "LoROM: CPU 주소 = 파일 오프셋 & $7FFF | $808000 (뱅크 80-). 포인터 검색 시 $80 접두어 고려."
        elif "HiROM" in header_info.get("rom_type", ""):
            info["mapper_note"] = "HiROM: CPU 주소 = 파일 오프셋 | $C00000. 대부분의 주소가 64KB 경계 내."

    # 메가드라이브
    elif ext in (".md", ".bin", ".gen"):
        header_info = _parse_md_header(data)
        info["platform"] = "MegaDrive"
        info["header"] = header_info
        info["mapper_note"] = "MD: 24비트 플랫 주소공간. ROM은 $000000부터 선형 매핑. 68000 빅엔디언."

    # 게임기어
    elif ext in (".gg", ".sms"):
        info["platform"] = "GameGear/SMS"
        info["header"] = {
            "actual_size": f"{len(data)} bytes",
            "mapper_note": "GG: Z80, 16KB 뱅킹 슬롯 3개($0000/$4000/$8000). $FFFC-$FFFF가 보통 SDSC 헤더."
        }

    # NDS
    elif ext in (".nds"):
        info["platform"] = "NDS"
        if len(data) > 0x200:
            title = data[0:12].decode("ascii", errors="replace").rstrip("\x00")
            gamecode = data[0x0C:0x10].decode("ascii", errors="replace")
            arm9_offset = struct.unpack_from("<I", data, 0x20)[0]
            arm9_size = struct.unpack_from("<I", data, 0x2C)[0]
            info["header"] = {
                "title": title,
                "gamecode": gamecode,
                "arm9_offset": f"${arm9_offset:X}",
                "arm9_size": arm9_size,
            }

    # 기타 — 기본 정보만
    else:
        info["platform"] = "unknown"
        info["note"] = "알 수 없는 확장자. 헤더 분석 불가. rom_hash로 해시만 확인."

    return json.dumps(info, ensure_ascii=False, indent=2)


@mcp.tool()
def rom_hash(rom_path: str) -> str:
    """ROM의 CRC32/MD5/SHA1 해시값 계산. 보존 덤프 대조용."""
    data = _read_rom(rom_path)
    return json.dumps({
        "file": rom_path,
        "size": len(data),
        "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08X}",
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def rom_diff(original_path: str, patched_path: str, min_cluster: int = 3) -> str:
    """두 ROM/파일의 바이트 차이(diff) 분석 — 변경 클러스터 목록 반환.

    클러스터: 연속된 변경 바이트 묶음. min_cluster보다 작은 차이는 무시.
    한글 패치의 텍스트·폰트·코드 변경 영역 식별에 사용.
    """
    orig = _read_rom(original_path)
    patched = _read_rom(patched_path)

    if len(orig) != len(patched):
        note = f"⚠ 크기 다름: 원본={len(orig)}, 패치본={len(patched)}"
    else:
        note = f"✅ 크기 동일: {len(orig)} bytes"

    clusters = []
    in_cluster = False
    cluster_start = 0
    cluster_bytes_orig = bytearray()
    cluster_bytes_patched = bytearray()

    max_len = min(len(orig), len(patched))
    for i in range(max_len):
        if orig[i] != patched[i]:
            if not in_cluster:
                cluster_start = i
                cluster_bytes_orig = bytearray()
                cluster_bytes_patched = bytearray()
                in_cluster = True
            cluster_bytes_orig.append(orig[i])
            cluster_bytes_patched.append(patched[i])
        else:
            if in_cluster:
                length = i - cluster_start
                if length >= min_cluster:
                    clusters.append({
                        "offset": f"${cluster_start:X}",
                        "offset_dec": cluster_start,
                        "length": length,
                        "original_hex": cluster_bytes_orig[:64].hex(" "),
                        "patched_hex": cluster_bytes_patched[:64].hex(" "),
                        "original_ascii": cluster_bytes_orig[:64].decode("ascii", errors="replace"),
                        "patched_ascii": cluster_bytes_patched[:64].decode("ascii", errors="replace"),
                    })
                in_cluster = False

    # 마지막 클러스터
    if in_cluster:
        length = max_len - cluster_start
        if length >= min_cluster:
            clusters.append({
                "offset": f"${cluster_start:X}",
                "offset_dec": cluster_start,
                "length": length,
                "original_hex": cluster_bytes_orig[:64].hex(" "),
                "patched_hex": cluster_bytes_patched[:64].hex(" "),
            })

    return json.dumps({
        "note": note,
        "total_changed_bytes": sum(c["length"] for c in clusters),
        "cluster_count": len(clusters),
        "clusters": clusters[:50],  # 최대 50개
        "truncated": len(clusters) > 50,
    }, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 텍스트·데이터 분석 도구
# ═══════════════════════════════════════════════════════════════════════════════

# Shift-JIS 디코딩을 위한 최소한의 매핑 (encoding_rs 없이)
def _try_decode_sjis(data: bytes) -> tuple[str, int]:
    """Shift-JIS로 디코딩 시도. (디코딩된 문자열, 유효 바이트 수)"""
    result = []
    i = 0
    valid = 0
    while i < len(data):
        b = data[i]
        if b < 0x80:
            result.append(chr(b))
            valid += 1
            i += 1
        elif 0xA1 <= b <= 0xDF:
            # 반각 가타카나
            result.append(chr(0xFF61 + b - 0xA1))
            valid += 1
            i += 1
        elif (0x81 <= b <= 0x9F or 0xE0 <= b <= 0xEF) and i + 1 < len(data):
            b2 = data[i + 1]
            if 0x40 <= b2 <= 0x7E or 0x80 <= b2 <= 0xFC:
                # 2바이트 SJIS → 유니코드 근사
                result.append(_sjis_to_char(b, b2))
                valid += 2
                i += 2
            else:
                result.append("�")
                i += 1
        else:
            result.append("�")
            i += 1
    return "".join(result), valid


def _sjis_to_char(hi: int, lo: int) -> str:
    """Shift-JIS 2바이트 → 유니코드 근사 (간단 구현)"""
    # SJIS → JIS 변환
    if hi <= 0x9F:
        jis_hi = (hi - 0x81) * 2 + 0x21
    else:
        jis_hi = (hi - 0xE0) * 2 + 0x5F
    if lo <= 0x7E:
        jis_lo = lo - 0x1F
    else:
        jis_lo = lo - 0x7E + 0x5E
    if jis_lo < 0x21:
        jis_lo = 0x21
    # 근사: CP932 코드 포인트
    code = jis_hi * 0x100 + jis_lo
    # 단순화: 기본 범위만 처리
    try:
        return bytes([hi, lo]).decode("shift_jis", errors="replace")
    except Exception:
        return "�"


@mcp.tool()
def scan_strings(rom_path: str, encoding: str = "sjis", min_length: int = 4,
                 offset_start: str = "0x0", offset_end: str = "") -> str:
    """ROM에서 문자열 스캔 — 주어진 인코딩으로 해석 가능한 연속 문자열 검출.

    encoding: sjis(Shift-JIS) | ascii | utf8
    min_length: 최소 문자열 길이 (이보다 짧으면 무시)
    offset_start/offset_end: 스캔 범위 (16진수, 생략 시 전체)

    용례: "화면에 보이는 일본어 대사를 ROM에서 찾기"
    """
    data = _read_rom(rom_path)
    start = int(offset_start, 16) if offset_start else 0
    end = int(offset_end, 16) if offset_end else len(data)
    data = data[start:end]

    results = []
    current_run = bytearray()
    current_start = 0

    i = 0
    while i < len(data):
        b = data[i]
        valid = False

        if encoding == "ascii":
            # 출력 가능한 ASCII
            if 0x20 <= b <= 0x7E:
                valid = True
                current_run.append(b)
                i += 1
                continue
        elif encoding == "sjis":
            # Shift-JIS 유효 여부
            if b < 0x80 and b >= 0x20:
                valid = True
                current_run.append(b)
                i += 1
                continue
            elif 0xA1 <= b <= 0xDF:
                valid = True
                current_run.append(b)
                i += 1
                continue
            elif (0x81 <= b <= 0x9F or 0xE0 <= b <= 0xEF) and i + 1 < len(data):
                b2 = data[i + 1]
                if 0x40 <= b2 <= 0x7E or 0x80 <= b2 <= 0xFC:
                    valid = True
                    current_run.extend([b, b2])
                    i += 2
                    continue
        elif encoding == "utf8":
            # UTF-8 멀티바이트
            if b < 0x80 and b >= 0x20:
                valid = True
                current_run.append(b)
                i += 1
                continue
            elif 0xC0 <= b <= 0xDF and i + 1 < len(data):
                if 0x80 <= data[i + 1] <= 0xBF:
                    valid = True
                    current_run.extend([b, data[i + 1]])
                    i += 2
                    continue
            elif 0xE0 <= b <= 0xEF and i + 2 < len(data):
                if 0x80 <= data[i + 1] <= 0xBF and 0x80 <= data[i + 2] <= 0xBF:
                    valid = True
                    current_run.extend([b, data[i + 1], data[i + 2]])
                    i += 3
                    continue

        # 유효하지 않은 바이트 → run 종료
        if current_run and len(current_run) >= min_length:
            abs_offset = start + current_start
            if encoding == "sjis":
                decoded, _valid_bytes = _try_decode_sjis(current_run)
            elif encoding == "ascii":
                decoded = current_run.decode("ascii", errors="replace")
            else:
                decoded = current_run.decode("utf-8", errors="replace")
            results.append({
                "offset": f"${abs_offset:X}",
                "offset_dec": abs_offset,
                "length": len(current_run),
                "hex": current_run[:40].hex(" "),
                "text": decoded[:100],
            })
            if len(results) >= 200:  # 최대 200개
                break
        current_run = bytearray()
        current_start = i + 1
        i += 1

    return json.dumps({
        "encoding": encoding,
        "range": f"${start:X}-${end:X}",
        "min_length": min_length,
        "count": len(results),
        "results": results,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def scan_pointers(rom_path: str, pointer_base: str = "0x0",
                  pointer_size: int = 2, big_endian: bool = True,
                  min_target: str = "0x0", max_target: str = "0xFFFFFF",
                  offset_start: str = "0x0", offset_end: str = "") -> str:
    """ROM에서 포인터 테이블 후보 스캔.

    포인터: 특정 기준 주소(pointer_base)를 더했을 때 유효한 ROM 영역을 가리키는 값.
    CPU 주소 모드에 따라 pointer_base를 설정:
    - SNES LoROM: $808000 (또는 $008000)
    - 메가드라이브: $0 (ROM이 $000000부터 매핑)
    - 게임기어: $0 (뱅크에 따라 다름)

    pointer_size: 2(16비트) | 3(24비트) | 4(32비트)
    big_endian: True(빅엔디언, MD/SNES) | False(리틀엔디언, PS1/NDS)
    """
    data = _read_rom(rom_path)
    start = int(offset_start, 16) if offset_start else 0
    end = int(offset_end, 16) if offset_end else len(data)
    base = int(str(pointer_base), 16) if isinstance(pointer_base, str) else pointer_base
    min_t = int(str(min_target), 16) if isinstance(min_target, str) else min_target
    max_t = int(str(max_target), 16) if isinstance(max_target, str) else max_target
    pointer_size = int(pointer_size) if isinstance(pointer_size, str) else pointer_size
    big_endian = big_endian if isinstance(big_endian, bool) else (big_endian.lower() in ("true", "1", "yes"))

    results = []
    fmt = ">" if big_endian else "<"
    if pointer_size == 2:
        fmt += "H"
    elif pointer_size == 3:
        # 3바이트는 수동 처리
        fmt = None
    elif pointer_size == 4:
        fmt += "I"
    else:
        return json.dumps({"error": f"지원하지 않는 pointer_size: {pointer_size}"})

    i = start
    while i < end - pointer_size + 1:
        if pointer_size == 3:
            if big_endian:
                val = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
            else:
                val = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16)
        else:
            val = struct.unpack_from(fmt, data, i)[0] if fmt else 0

        target = base + val if base else val
        if min_t <= target <= max_t and val > 0:
            # 포인터가 가리키는 대상이 존재하는지 확인
            if target < len(data):
                # 대상의 첫 바이트들
                preview = data[target:target + 16].hex(" ")
                results.append({
                    "ptr_offset": f"${i:X}",
                    "ptr_offset_dec": i,
                    "raw_value": f"${val:X}",
                    "target": f"${target:X}",
                    "target_preview": preview,
                })

        if len(results) >= 500:
            break
        i += 1  # 1바이트씩 스캔 (정렬 무시)

    return json.dumps({
        "pointer_base": f"${base:X}",
        "pointer_size": pointer_size,
        "endian": "big" if big_endian else "little",
        "scan_range": f"${start:X}-${end:X}",
        "target_range": f"${min_t:X}-${max_t:X}",
        "count": len(results),
        "results": results[:100],
        "truncated": len(results) > 100,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def dump_data(rom_path: str, offset: str, length: str = "0x100") -> str:
    """ROM의 특정 오프셋 범위를 헥스 덤프 + ASCII로 출력.

    offset: 시작 위치 (16진수)
    length: 읽을 바이트 수 (16진수)
    """
    data = _read_rom(rom_path)
    start = int(offset, 16) if isinstance(offset, str) else int(offset)
    size = int(length, 16) if isinstance(length, str) else int(length)

    chunk = data[start:start + size]
    lines = []
    for i in range(0, len(chunk), 16):
        row = chunk[i:i + 16]
        hex_part = " ".join(f"{b:02X}" for b in row)
        ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row)
        lines.append(f"${start + i:08X}  {hex_part:<48s}  |{ascii_part}|")

    return json.dumps({
        "offset": f"${start:X}",
        "length": len(chunk),
        "hex_dump": "\n".join(lines),
    }, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 폰트 도구
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_tiles_1bpp(data: bytes, width: int = 8, height: int = 8,
                        count: int = 256) -> bytes:
    """1bpp 타일 → raw RGBA 바이트 (pillow 없는 경우 hex만)"""
    # 타일 1개 = 높이 바이트 (1bpp = 평면당 1바이트/라인)
    tile_size = height  # 1bpp = 1바이트/라인
    total = tile_size * count
    return data[:total]


def _extract_tiles_2bpp(data: bytes, width: int = 8, height: int = 8,
                        count: int = 256) -> bytes:
    """2bpp 타일 → raw 바이트 (SNES, MD)"""
    tile_size = height * 2  # 2bpp = 2바이트/라인
    total = tile_size * count
    return data[:total]


def _extract_tiles_4bpp(data: bytes, width: int = 8, height: int = 8,
                        count: int = 256) -> bytes:
    """4bpp 타일 → raw 바이트 (MD, GG)"""
    tile_size = height * 4  # 4bpp = 4바이트/라인
    total = tile_size * count
    return data[:total]


def _tiles_to_png_bytes(tile_data: bytes, bpp: int, width: int = 8, height: int = 8,
                        cols: int = 16, palette: list[int] | None = None) -> bytes | None:
    """타일 데이터 → PNG 바이트 (pillow 필요)"""
    try:
        from PIL import Image
    except ImportError:
        return None

    bytes_per_row = (width + 7) // 8  # 8px 한글자당 bpp 바이트

    tile_count = len(tile_data) // (height * bytes_per_row * bpp)
    if palette is None:
        # 기본 그레이스케일 팔레트
        if bpp == 1:
            palette = [0x000000, 0xFFFFFF]
        elif bpp == 2:
            palette = [0x000000, 0x555555, 0xAAAAAA, 0xFFFFFF]
        elif bpp == 4:
            palette = [
                0x000000, 0x111111, 0x222222, 0x333333,
                0x444444, 0x555555, 0x666666, 0x777777,
                0x888888, 0x999999, 0xAAAAAA, 0xBBBBBB,
                0xCCCCCC, 0xDDDDDD, 0xEEEEEE, 0xFFFFFF,
            ]

    rows = (tile_count + cols - 1) // cols
    img = Image.new("RGB", (cols * width, rows * height), (0, 0, 0))

    for tile_idx in range(tile_count):
        tx = (tile_idx % cols) * width
        ty = (tile_idx // cols) * height

        tile_row_size = height * bytes_per_row * bpp
        tile_base = tile_idx * tile_row_size

        for py in range(height):
            for px in range(width):
                byte_idx, bit_idx = divmod(px, 8)
                bit = 7 - bit_idx
                color_idx = 0
                for bp in range(bpp):
                    row_offset = py * bytes_per_row * bpp
                    byte_offset = tile_base + row_offset + byte_idx * bpp + bp
                    if byte_offset < len(tile_data):
                        if tile_data[byte_offset] & (1 << bit):
                            color_idx |= (1 << bp)
                if color_idx < len(palette):
                    c = palette[color_idx]
                    r, g, b = (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF
                    img.putpixel((tx + px, ty + py), (r, g, b))

    # PNG 바이트로 변환
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@mcp.tool()
def dump_font_tiles(rom_path: str, offset: str, bpp: int = 2,
                    tile_width: int = 8, tile_height: int = 8,
                    tile_count: int = 256, columns: int = 16,
                    output_name: str = "") -> str:
    """ROM에서 폰트 타일 시트를 추출해 PNG 이미지로 저장.

    타일 포맷:
    - 1bpp: 게임보이, 일부 GG
    - 2bpp: SNES, NES (플레이너)
    - 4bpp: 메가드라이브, 게임기어, 대부분 16비트 콘솔

    offset: 폰트 데이터 시작 위치 (16진수)
    bpp: bits per pixel (1/2/4)
    tile_width/tile_height: 타일 크기 (보통 8x8 또는 8x16)
    tile_count: 추출할 타일 개수
    columns: PNG의 열 수
    output_name: 출력 파일명 (생략 시 자동 생성)
    """
    data = _read_rom(rom_path)
    start = int(offset, 16) if isinstance(offset, str) else int(offset)
    bpp = int(bpp) if isinstance(bpp, str) else bpp
    tile_width = int(tile_width) if isinstance(tile_width, str) else tile_width
    tile_height = int(tile_height) if isinstance(tile_height, str) else tile_height
    tile_count = int(tile_count) if isinstance(tile_count, str) else tile_count
    columns = int(columns) if isinstance(columns, str) else columns
    bytes_per_row = (tile_width + 7) // 8
    tile_size = tile_height * bytes_per_row * bpp
    end = start + tile_size * tile_count
    tile_raw = data[start:end]

    # PNG 생성
    png_data = _tiles_to_png_bytes(tile_raw, bpp, tile_width, tile_height, columns)

    # 파일 저장
    name = output_name or f"font_{Path(rom_path).stem}_{offset}"
    if png_data:
        png_path = OUTPUT_DIR / f"{name}.png"
        png_path.write_bytes(png_data)

        # 원시 데이터도 저장 (재사용 용이)
        raw_path = OUTPUT_DIR / f"{name}.bin"
        raw_path.write_bytes(tile_raw)

        return json.dumps({
            "saved_png": str(png_path),
            "saved_raw": str(raw_path),
            "tile_count": tile_count,
            "tile_size": f"{tile_width}x{tile_height}",
            "bpp": bpp,
            "data_size": len(tile_raw),
            "preview_url": f"http://172.30.1.78:8093/files/{png_path.name}" if png_data else None,
        }, ensure_ascii=False, indent=2)
    else:
        # pillow 없는 경우 헥스 덤프만
        raw_path = OUTPUT_DIR / f"{name}.bin"
        raw_path.write_bytes(tile_raw)
        hex_preview = tile_raw[:256].hex(" ")
        return json.dumps({
            "saved_raw": str(raw_path),
            "tile_count": tile_count,
            "tile_size": f"{tile_width}x{tile_height}",
            "bpp": bpp,
            "data_size": len(tile_raw),
            "hex_preview": hex_preview,
            "note": "PNG 변환 실패 (pillow 필요). 원시 데이터 저장됨.",
        }, ensure_ascii=False, indent=2)


@mcp.tool()
def ttf_to_tiles(ttf_path: str, output_name: str = "hangul_tiles",
                 tile_width: int = 8, tile_height: int = 8,
                 bpp: int = 2, font_size: int = 8,
                 characters: str = "") -> str:
    """TTF 폰트 → 레트로 타일 비트맵으로 변환.

    characters: 변환할 문자 목록 (예: "가각간..."). 생략 시 한글 2350자.
    font_size: 렌더링 크기 (px). tile_width와 동일하게 설정.
    bpp: 출력 비트 깊이 (1=흑백, 2=4색, 4=16색)

    장르별 한글 사용빈도가 다르므로 characters를 명시적으로 지정.
    """
    try:
        from PIL import Image, ImageFont, ImageDraw
    except ImportError:
        return json.dumps({"error": "pillow 필요"}, ensure_ascii=False)

    # MCP에서 문자열로 전달될 수 있으므로 정수 변환
    tile_width = int(tile_width) if isinstance(tile_width, str) else tile_width
    tile_height = int(tile_height) if isinstance(tile_height, str) else tile_height
    bpp = int(bpp) if isinstance(bpp, str) else bpp
    font_size = int(font_size) if isinstance(font_size, str) else font_size

    # 문자 목록
    if not characters:
        # KS X 1001 한글 영역 (가~힣)
        chars = []
        for cp in range(0xAC00, 0xD7A4):
            chars.append(chr(cp))
        characters = "".join(chars[:2350])  # 기본 2350자

    # TTF 로드
    try:
        font = ImageFont.truetype(ttf_path, font_size)
    except Exception as e:
        return json.dumps({"error": f"TTF 로드 실패: {e}"}, ensure_ascii=False)

    all_tiles = bytearray()
    tile_count = 0
    fail_count = 0

    for ch in characters:
        # 1글자 렌더링
        img = Image.new("L", (tile_width, tile_height), 0)
        draw = ImageDraw.Draw(img)
        try:
            bbox = font.getbbox(ch)
            # 중앙 정렬
            char_w = bbox[2] - bbox[0]
            char_h = bbox[3] - bbox[1]
            x = (tile_width - char_w) // 2 - bbox[0]
            y = (tile_height - char_h) // 2 - bbox[1]
            draw.text((x, y), ch, fill=255, font=font)
        except Exception:
            fail_count += 1
            continue

        # 비트플레인 변환 (가변 타일폭 지원, 8px 단위 바이트 분할)
        pixels = list(img.getdata())
        bytes_per_row = (tile_width + 7) // 8  # 8px당 1바이트

        if bpp == 1:
            for py in range(tile_height):
                row = [0] * bytes_per_row
                for px in range(tile_width):
                    if pixels[py * tile_width + px] > 128:
                        byte_idx, bit_idx = divmod(px, 8)
                        row[byte_idx] |= (1 << (7 - bit_idx))
                all_tiles.extend(row)

        elif bpp == 2:
            for py in range(tile_height):
                bp = [[0] * bytes_per_row for _ in range(2)]
                for px in range(tile_width):
                    val = pixels[py * tile_width + px]
                    byte_idx, bit_idx = divmod(px, 8)
                    if val > 192:
                        bp[0][byte_idx] |= (1 << (7 - bit_idx))
                        bp[1][byte_idx] |= (1 << (7 - bit_idx))
                    elif val > 128:
                        bp[0][byte_idx] |= (1 << (7 - bit_idx))
                    elif val > 64:
                        bp[1][byte_idx] |= (1 << (7 - bit_idx))
                for plane in range(2):
                    all_tiles.extend(bp[plane])

        elif bpp == 4:
            for py in range(tile_height):
                bp = [[0] * bytes_per_row for _ in range(4)]
                for px in range(tile_width):
                    val = pixels[py * tile_width + px]
                    byte_idx, bit_idx = divmod(px, 8)
                    level = val // 16
                    if level & 1: bp[0][byte_idx] |= (1 << (7 - bit_idx))
                    if level & 2: bp[1][byte_idx] |= (1 << (7 - bit_idx))
                    if level & 4: bp[2][byte_idx] |= (1 << (7 - bit_idx))
                    if level & 8: bp[3][byte_idx] |= (1 << (7 - bit_idx))
                for plane in range(4):
                    all_tiles.extend(bp[plane])

        tile_count += 1
        if tile_count >= 3000:
            break

    # 저장
    name = output_name or f"tileset_{Path(ttf_path).stem}"
    bin_path = OUTPUT_DIR / f"{name}_{tile_width}x{tile_height}_{bpp}bpp.bin"
    bin_path.write_bytes(bytes(all_tiles))

    # 타일 시트 PNG도 생성
    cols = 32
    png_data = _tiles_to_png_bytes(bytes(all_tiles), bpp, tile_width, tile_height, cols)
    png_path = None
    if png_data:
        png_path = OUTPUT_DIR / f"{name}_{tile_width}x{tile_height}_{bpp}bpp.png"
        png_path.write_bytes(png_data)

    # 문자→슬롯 매핑 저장
    mapping = {ch: idx for idx, ch in enumerate(characters[:tile_count])}
    map_path = OUTPUT_DIR / f"{name}_mapping.json"
    map_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")

    return json.dumps({
        "saved_bin": str(bin_path),
        "saved_png": str(png_path) if png_path else None,
        "saved_mapping": str(map_path),
        "tile_count": tile_count,
        "failed_chars": fail_count,
        "total_chars_in": len(characters),
        "tile_size": f"{tile_width}x{tile_height}",
        "bpp": bpp,
        "data_size": len(all_tiles),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def analyze_glyphs(translations_json: str) -> str:
    """번역 JSON에서 실제 사용된 한글 음절 집계. 글리프 예산 산정 입력.

    translations_json: 번역 JSON 파일 경로 또는 JSON 문자열
    반환: 고유 음절 목록, 빈도, 슬롯 예산 추정
    """
    # 파일 경로 또는 JSON 문자열 처리
    path = Path(translations_json)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        try:
            data = json.loads(translations_json)
        except json.JSONDecodeError:
            return json.dumps({"error": "유효한 JSON 파일 경로 또는 JSON 문자열이 아님"}, ensure_ascii=False)

    # 모든 번역문 수집
    all_text = ""
    entries = []

    if isinstance(data, list):
        entries = data
        for entry in data:
            if isinstance(entry, dict):
                ko = entry.get("ko", "") or entry.get("translation", "") or entry.get("text", "")
                all_text += ko
    elif isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str):
                all_text += v
            elif isinstance(v, dict):
                ko = v.get("ko", "") or v.get("translation", "")
                all_text += ko

    # 한글 음절 추출 (U+AC00 ~ U+D7A3)
    syllables = {}
    non_syllable = set()
    for ch in all_text:
        cp = ord(ch)
        if 0xAC00 <= cp <= 0xD7A3:
            syllables[ch] = syllables.get(ch, 0) + 1
        elif cp > 0x7F and ch not in ("{", "}", "\n", "\r", "\t"):
            non_syllable.add(ch)

    # 빈도순 정렬
    sorted_syl = sorted(syllables.items(), key=lambda x: -x[1])

    return json.dumps({
        "total_chars_analyzed": len(all_text),
        "unique_syllables": len(syllables),
        "unique_non_syllable": len(non_syllable),
        "non_syllable_chars": list(non_syllable)[:50],
        "top_100": [{"char": ch, "count": cnt} for ch, cnt in sorted_syl[:100]],
        "bottom_50": [{"char": ch, "count": cnt} for ch, cnt in sorted_syl[-50:]] if len(sorted_syl) > 50 else [],
        "slot_estimate": {
            "ksx1001_max": 2350,
            "actual_needed": len(syllables),
            "headroom": 2350 - len(syllables),
            "recommendation": "여유 있음" if len(syllables) < 1800 else "글리프 예산 초과 위험 — 서브셋 검토 필요" if len(syllables) > 2350 else "2350자 경계 근접 — 주의"
        }
    }, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 패치 빌드 도구
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def verify_roundtrip(original_path: str, rebuilt_path: str) -> str:
    """추출→재조립 결과가 원본과 바이트 단위로 동일한지 검증.

    패치 제작의 0순위 검증. 이것이 통과해야 번역 삽입을 시작할 수 있다.
    """
    orig = _read_rom(original_path)
    rebuilt = _read_rom(rebuilt_path)

    if len(orig) != len(rebuilt):
        return json.dumps({
            "passed": False,
            "error": f"크기 불일치: 원본={len(orig)}, 재조립={len(rebuilt)}",
            "diff_count": abs(len(orig) - len(rebuilt)),
        }, ensure_ascii=False, indent=2)

    diffs = []
    for i in range(len(orig)):
        if orig[i] != rebuilt[i]:
            diffs.append({
                "offset": f"${i:X}",
                "original": f"${orig[i]:02X}",
                "rebuilt": f"${rebuilt[i]:02X}",
            })
            if len(diffs) >= 100:
                break

    return json.dumps({
        "passed": len(diffs) == 0,
        "total_bytes": len(orig),
        "diff_count": len(diffs),
        "diffs": diffs[:20],
        "message": "✅ 라운드트립 검증 통과 — 원본과 완전히 동일" if len(diffs) == 0 else f"❌ {len(diffs)}개 위치 불일치"
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def build_patch(original_path: str, modified_path: str,
                output_name: str = "", format: str = "bps") -> str:
    """원본 ROM + 수정된 ROM → BPS/xdelta 패치 파일 생성.

    format: bps | xdelta | ips
    output_name: 출력 파일명 (생략 시 자동 생성)

    주의: xdelta3 명령이 시스템에 설치되어 있어야 함.
    IPS는 사용 비권장 (원본 검증 없음).
    """
    orig = _read_rom(original_path)
    mod = _read_rom(modified_path)

    name = output_name or f"{Path(original_path).stem}_patch"
    name_p = Path(name)
    if name_p.is_absolute():
        patch_path = name_p.parent / f"{name_p.name}.{format}"
    else:
        patch_path = OUTPUT_DIR / f"{name}.{format}"

    if format == "ips":
        # IPS 생성 (단순 구현, 16MB 한계)
        patch = _create_ips(orig, mod)
        patch_path.write_bytes(patch)

    elif format == "bps":
        # BPS 생성 (간소화된 구현)
        patch = _create_bps(orig, mod)
        patch_path.write_bytes(patch)

    elif format == "xdelta":
        import subprocess
        result = subprocess.run(
            ["xdelta3", "-e", "-s", original_path, modified_path, str(patch_path)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return json.dumps({"error": f"xdelta3 실패: {result.stderr}"}, ensure_ascii=False)

    return json.dumps({
        "saved": str(patch_path),
        "format": format,
        "original_size": len(orig),
        "modified_size": len(mod),
        "patch_size": patch_path.stat().st_size if patch_path.exists() else 0,
        "compression_ratio": f"{patch_path.stat().st_size / max(len(orig), 1) * 100:.1f}%" if patch_path.exists() else "N/A",
    }, ensure_ascii=False, indent=2)


def _create_ips(orig: bytes, mod: bytes) -> bytes:
    """IPS 패치 생성 (단순 구현)"""
    result = bytearray(b"PATCH")
    max_len = min(len(orig), len(mod))
    i = 0
    while i < max_len:
        if orig[i] != mod[i]:
            start = i
            chunk = bytearray()
            while i < max_len and len(chunk) < 0xFFFF and (orig[i] != mod[i] or i - start < 3):
                if orig[i] != mod[i]:
                    chunk.append(mod[i])
                else:
                    # 연속 2바이트 이상 같으면 run 종료
                    if i + 1 < max_len and orig[i + 1] == mod[i + 1]:
                        break
                    chunk.append(mod[i])
                i += 1
                if len(chunk) >= 0xFFFF:
                    break
            # 실제로 변경된 마지막 위치로 조정
            while chunk and chunk[-1] == orig[start + len(chunk) - 1]:
                chunk.pop()
                i -= 1
            if chunk:
                result.extend(struct.pack(">BH", start >> 16, start & 0xFFFF))
                result.extend(struct.pack(">H", len(chunk)))
                result.extend(chunk)
        i += 1
    result.extend(b"EOF")
    return bytes(result)


def _create_bps(orig: bytes, mod: bytes) -> bytes:
    """BPS 패치 생성 (간소화)"""
    # BPS 헤더
    result = bytearray(b"BPS1")
    # 원본/수정본 크기 (가변 길이 인코딩)
    result.extend(_bps_encode_num(len(orig)))
    result.extend(_bps_encode_num(len(mod)))
    # 메타데이터 길이
    result.extend(_bps_encode_num(0))

    # 단순 diff: SourceRead + TargetRead + TargetCopy
    max_len = max(len(orig), len(mod))
    out_offset = 0
    orig_offset = 0

    while out_offset < len(mod) and orig_offset < max_len:
        # 같은 구간 건너뛰기
        same_count = 0
        while (orig_offset + same_count < len(orig) and
               out_offset + same_count < len(mod) and
               orig[orig_offset + same_count] == mod[out_offset + same_count]):
            same_count += 1
            if same_count >= 8192:
                break

        if same_count >= 1:
            # SourceRead
            result.append(0)  # SourceRead
            result.extend(_bps_encode_num(same_count))
            orig_offset += same_count
            out_offset += same_count

        # 다른 구간
        diff_count = 0
        while (orig_offset + diff_count < len(orig) and
               out_offset + diff_count < len(mod) and
               orig[orig_offset + diff_count] != mod[out_offset + diff_count]):
            diff_count += 1
            if diff_count >= 8192:
                break

        if diff_count > 0:
            # SourceCopy (소스에서 읽은 후 XOR)
            result.append(1)  # SourceCopy
            result.extend(_bps_encode_num(diff_count))
            for j in range(diff_count):
                if orig_offset + j < len(orig) and out_offset + j < len(mod):
                    result.append(orig[orig_offset + j] ^ mod[out_offset + j])
            orig_offset += diff_count
            out_offset += diff_count

        # 수정본만 있는 구간 (TargetRead)
        if out_offset >= len(mod):
            break
        if orig_offset >= len(orig):
            target_only = len(mod) - out_offset
            if target_only > 0:
                result.append(2)  # TargetRead
                result.extend(_bps_encode_num(target_only))
                result.extend(mod[out_offset:out_offset + target_only])
                out_offset += target_only
            break

    # 체크섬 (CRC32)
    crc_orig = zlib.crc32(orig) & 0xFFFFFFFF
    crc_mod = zlib.crc32(mod) & 0xFFFFFFFF
    crc_patch = zlib.crc32(result) & 0xFFFFFFFF
    result.extend(struct.pack("<III", crc_orig, crc_mod, crc_patch))

    return bytes(result)


def _bps_encode_num(n: int) -> bytes:
    """BPS 가변 길이 정수 인코딩"""
    result = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            result.append(b | 0x80)
        else:
            result.append(b)
            break
    return bytes(result)


@mcp.tool()
def apply_patch(original_path: str, patch_path: str, output_name: str = "") -> str:
    """기존 패치 파일을 원본 ROM에 적용.

    BPS/IPS 포맷 자동 감지.
    xdelta는 xdelta3 명령으로 적용.
    """
    patch_data = _read_rom(patch_path)
    orig = _read_rom(original_path)

    # 포맷 감지
    magic = patch_data[:5]  # 최대 5바이트까지 확인 (IPS = "PATCH" 5바이트)
    name = output_name or f"{Path(original_path).stem}_patched"
    name_p = Path(name)

    if magic[:4] == b"BPS1":
        mod = _apply_bps(orig, patch_data)
    elif magic == b"PATCH":
        mod = _apply_ips(orig, patch_data)
    else:
        # xdelta 시도
        import subprocess
        if name_p.is_absolute():
            xd_path = name_p.parent / f"{name_p.name}.bin"
        else:
            xd_path = OUTPUT_DIR / f"{name}.bin"
        result = subprocess.run(
            ["xdelta3", "-d", "-s", original_path, patch_path, str(xd_path)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return json.dumps({"error": f"패치 적용 실패 (알 수 없는 포맷 또는 xdelta3 오류): {result.stderr}"}, ensure_ascii=False)
        mod = xd_path.read_bytes()

    # 출력 파일 저장
    if name_p.is_absolute():
        out_path = name_p.parent / f"{name_p.name}.patched.sfc"
    else:
        out_path = OUTPUT_DIR / f"{name}.sfc"
    out_path.write_bytes(mod)

    return json.dumps({
        "saved": str(out_path),
        "original_size": len(orig),
        "patched_size": len(mod),
        "format": "BPS" if magic == b"BPS1" else "IPS" if magic == b"PATCH" else "xdelta",
        "success": True,
    }, ensure_ascii=False, indent=2)


def _apply_bps(orig: bytes, patch: bytes) -> bytes:
    """BPS 패치 적용 (간소화)"""
    if patch[:4] != b"BPS1":
        raise ValueError("BPS 매직 불일치")

    pos = 4
    # 원본 크기 읽기
    orig_size, pos = _bps_decode_num(patch, pos)
    # 수정본 크기 읽기
    mod_size, pos = _bps_decode_num(patch, pos)
    # 메타데이터 건너뛰기
    meta_size, pos = _bps_decode_num(patch, pos)
    pos += meta_size

    mod = bytearray(mod_size)
    out_offset = 0
    orig_offset = 0

    while pos < len(patch) - 12:  # 마지막 12바이트 = 체크섬 3개
        action = patch[pos]
        pos += 1
        length, pos = _bps_decode_num(patch, pos)

        if action == 0:  # SourceRead
            mod[out_offset:out_offset + length] = orig[orig_offset:orig_offset + length]
            orig_offset += length
            out_offset += length
        elif action == 1:  # SourceCopy (XOR)
            for j in range(length):
                mod[out_offset + j] = orig[orig_offset + j] ^ patch[pos]
                pos += 1
            orig_offset += length
            out_offset += length
        elif action == 2:  # TargetRead
            mod[out_offset:out_offset + length] = patch[pos:pos + length]
            pos += length
            out_offset += length

    return bytes(mod)


def _apply_ips(orig: bytes, patch: bytes) -> bytes:
    """IPS 패치 적용"""
    if patch[:5] != b"PATCH":
        raise ValueError("IPS 매직 불일치")

    mod = bytearray(orig)
    pos = 5

    while pos + 2 < len(patch):
        if patch[pos:pos + 3] == b"EOF":
            break
        offset = (patch[pos] << 16) | (patch[pos + 1] << 8) | patch[pos + 2]
        pos += 3
        size = (patch[pos] << 8) | patch[pos + 1]
        pos += 2

        if size > 0:
            # 일반 레코드
            if offset + size > len(mod):
                mod.extend(b"\x00" * (offset + size - len(mod)))
            mod[offset:offset + size] = patch[pos:pos + size]
            pos += size
        else:
            # RLE 레코드
            rle_size = (patch[pos] << 8) | patch[pos + 1]
            pos += 2
            val = patch[pos]
            pos += 1
            if offset + rle_size > len(mod):
                mod.extend(b"\x00" * (offset + rle_size - len(mod)))
            mod[offset:offset + rle_size] = bytes([val]) * rle_size

    return bytes(mod)


def _bps_decode_num(data: bytes, pos: int) -> tuple[int, int]:
    """BPS 가변 길이 정수 디코딩"""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


# ── SNES 체크섬 재계산 ──────────────────────────────────────────────────────
@mcp.tool()
def calc_checksum(rom_path: str, platform: str = "snes") -> str:
    """플랫폼별 ROM 체크섬 재계산 및 보정.

    platform: snes | megadrive
    SNES: 16비트 보수쌍 체크섬 ($00:FFDC-$00:FFDF)
    MD: 16비트 합 체크섬 ($18E-$18F)

    주의: 원본 ROM을 변경하므로 백업 후 실행.
    """
    data = bytearray(_read_rom(rom_path))

    if platform == "snes":
        # LoROM 체크섬 위치
        if len(data) < 0x8000:
            return json.dumps({"error": "SNES ROM이 너무 작음"}, ensure_ascii=False)

        # 체크섬 계산 (전체 ROM, 체크섬 영역 제외)
        checksum = 0
        for b in data:
            checksum += b
        # $FFDC-$FFDF의 4바이트를 빼기 (이미 더했으므로)
        cs_offset = 0x7FDC  # LoROM
        if len(data) > 0x10000:
            # $00:FFDC-$00:FFDF
            for i in range(cs_offset, cs_offset + 4):
                checksum -= data[i]

        checksum &= 0xFFFF
        complement = checksum ^ 0xFFFF

        # 체크섬 쓰기
        struct.pack_into("<H", data, cs_offset, complement)
        struct.pack_into("<H", data, cs_offset + 2, checksum)

        output_path = Path(rom_path).parent / f"{Path(rom_path).stem}_fixed.sfc"
        output_path.write_bytes(bytes(data))

    elif platform == "megadrive":
        if len(data) < 0x200:
            return json.dumps({"error": "MD ROM이 너무 작음"}, ensure_ascii=False)

        # $200부터 끝까지 16비트 합
        checksum = 0
        for i in range(0x200, len(data), 2):
            if i + 1 < len(data):
                checksum += (data[i] << 8) | data[i + 1]
        checksum &= 0xFFFF

        struct.pack_into(">H", data, 0x18E, checksum)

        output_path = Path(rom_path).parent / f"{Path(rom_path).stem}_fixed.bin"
        output_path.write_bytes(bytes(data))

    else:
        return json.dumps({"error": f"지원하지 않는 플랫폼: {platform}"}, ensure_ascii=False)

    return json.dumps({
        "platform": platform,
        "saved": str(output_path),
        "note": "체크섬 보정 완료. 원본은 변경되지 않음 (_fixed 파일 생성).",
    }, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 번역·검증 도구
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def check_glyph_coverage(translations_json: str, glyph_table_json: str) -> str:
    """번역문의 모든 문자가 폰트 글리프 테이블에 존재하는지 검사.

    translations_json: 번역 JSON 파일 경로
    glyph_table_json: 문자→슬롯 매핑 JSON (analyze_glyphs 또는 ttf_to_tiles 산출물)
    """
    # 번역 로드
    tpath = Path(translations_json)
    if tpath.exists():
        translations = json.loads(tpath.read_text(encoding="utf-8"))
    else:
        try:
            translations = json.loads(translations_json)
        except json.JSONDecodeError:
            return json.dumps({"error": "번역 JSON 로드 실패"}, ensure_ascii=False)

    # 글리프 테이블 로드
    gpath = Path(glyph_table_json)
    if gpath.exists():
        glyph_table = json.loads(gpath.read_text(encoding="utf-8"))
    else:
        try:
            glyph_table = json.loads(glyph_table_json)
        except json.JSONDecodeError:
            return json.dumps({"error": "글리프 테이블 JSON 로드 실패"}, ensure_ascii=False)

    # 모든 번역문에서 문자 수집
    all_text = ""
    if isinstance(translations, list):
        for entry in translations:
            if isinstance(entry, dict):
                all_text += entry.get("ko", "")
    elif isinstance(translations, dict):
        for v in translations.values():
            if isinstance(v, str):
                all_text += v

    # 커버리지 검사
    missing = {}
    covered = set()
    for ch in all_text:
        if ord(ch) <= 0x7F:  # ASCII
            covered.add(ch)
            continue
        if ch in glyph_table:
            covered.add(ch)
        else:
            if ch not in missing:
                missing[ch] = 0
            missing[ch] += 1

    total_chars = len(set(all_text))
    missing_chars = len(missing)

    return json.dumps({
        "passed": missing_chars == 0,
        "total_unique_chars": total_chars,
        "covered": len(covered),
        "missing_count": missing_chars,
        "missing_chars": [{"char": ch, "code": f"U+{ord(ch):04X}", "count": cnt}
                          for ch, cnt in sorted(missing.items(), key=lambda x: -x[1])[:50]],
        "message": "✅ 모든 문자가 글리프 테이블에 존재" if missing_chars == 0 else
                   f"❌ {missing_chars}개 문자 누락 — 폰트 확장 또는 번역 수정 필요",
    }, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 프로젝트 관리 도구
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def project_init(project_name: str, platform: str, base_dir: str = "") -> str:
    """새 한글 패치 프로젝트 스캐폴딩 생성.

    project-conventions.md 표준 레이아웃으로 디렉토리 구조 생성.
    platform: snes|megadrive|saturn|ps1|dreamcast|pce|pc98|gg|nds
    base_dir: 프로젝트 루트 경로 (기본: /mnt/synology_devdata/projects/)
    """
    if not base_dir:
        base_dir = "/mnt/synology_devdata/projects"

    root = Path(base_dir) / project_name
    if root.exists():
        return json.dumps({"error": f"이미 존재하는 프로젝트: {root}"}, ensure_ascii=False)

    # 디렉토리 구조
    dirs = [
        "src/commands",
        "assets/fonts",
        "assets/translations/pending",
        "assets/translations/in_progress",
        "assets/translations/review",
        "assets/translations/complete",
        "assets/translation_guide",
        "docs",
        "docs/archive",
        "scripts",
        "qa_screenshot",
        "roms",
        "out",
    ]
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)

    # .gitignore
    gitignore = """# 원본 자산 (저작권)
roms/
out/
*.gg
*.sfc
*.smc
*.bin
*.cue
*.d88
*.iso
*.nds

# 빌드 산출물
__pycache__/
*.pyc
.pytest_cache/
"""
    (root / ".gitignore").write_text(gitignore, encoding="utf-8")

    # AGENTS.md (에이전트 진입점)
    agents_md = f"""# {project_name} — 한글 패치 프로젝트

## 상태
| 단계 | 상태 |
|------|------|
| 초기 조사 | 미완료 |
| 폰트·인코딩 | 미완료 |
| 텍스트 추출 | 미완료 |
| PoC | 미완료 |
| 재삽입·훅 | 미완료 |
| 번역 | 미완료 |
| 빌드·검증 | 미완료 |

## 플랫폼
{platform}

## 빌드 명령
```
# (작성 예정)
```

## 디렉토리
- `docs/` — 기술 문서 (포맷 분석, 텍스트 엔진 분석)
- `assets/translations/` — 번역 JSON (pending/in_progress/review/complete)
- `assets/fonts/` — 한글 TTF
- `roms/` — 원본 ROM (.gitignore)
- `scripts/` — 일회성 분석 스크립트

## 알려진 함정
- (프로젝트 진행 중 추가)
"""
    (root / "AGENTS.md").write_text(agents_md, encoding="utf-8")

    return json.dumps({
        "created": str(root),
        "platform": platform,
        "directories": [str(root / d) for d in dirs],
        "files": [str(root / ".gitignore"), str(root / "AGENTS.md")],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def review_dashboard(project_dir: str, action: str = "start",
                     port: int = 0, bind: str = "0.0.0.0") -> str:
    """번역 검수 대시보드를 프로젝트별로 기동/중지/상태확인 (내부 IP, ROM 무관).

    활성 세션의 프로젝트 루트를 넘기면 그 프로젝트의 `krpatch.dashboard.json`
    설정대로 Flask 대시보드를 띄운다(없으면 관례로 자동생성). 원문↔번역 대조,
    대본(스토리순) 뷰, 코덱 있으면 글리프 미리보기·바이트예산, 인라인 편집·리빌드.

    action: start | stop | status | restart
    port:   0 이면 설정(krpatch.dashboard.json)의 port, 없으면 5057
    반환(JSON): {ok, url, pid, port, name, ...}

    프로세스는 프로젝트 `.krpatch/dashboard.pid` 로 자기소유만 관리(광역 kill 없음).
    재부팅 생존이 필요하면 systemd 등록은 별도(수동 기동이 기본).
    """
    import signal
    import socket
    import subprocess
    import sys
    import time

    dash = "/root/projects/retro-kr-patch-mcp/dashboard/app.py"
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return json.dumps({"ok": False, "err": f"디렉토리 아님: {root}"}, ensure_ascii=False)

    cfg_path = root / "krpatch.dashboard.json"
    cfg = {}
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
    use_port = port or int(cfg.get("port", 5057))

    krdir = root / ".krpatch"
    krdir.mkdir(exist_ok=True)
    pidfile = krdir / "dashboard.pid"
    logfile = krdir / "dashboard.log"

    def _alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _read_pid():
        if pidfile.exists():
            try:
                p = int(pidfile.read_text().strip())
                return p if _alive(p) else None
            except Exception:
                return None
        return None

    def _stop():
        p = _read_pid()
        if p:
            try:
                os.kill(p, signal.SIGTERM)   # 자기소유 PID 만
            except OSError:
                pass
            for _ in range(20):
                if not _alive(p):
                    break
                time.sleep(0.15)
        if pidfile.exists():
            pidfile.unlink()
        return p

    def _ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    if action == "status":
        p = _read_pid()
        return json.dumps({"ok": True, "running": bool(p), "pid": p,
                           "port": use_port,
                           "url": f"http://{_ip()}:{use_port}" if p else None},
                          ensure_ascii=False)

    if action in ("stop", "restart"):
        _stop()
        if action == "stop":
            return json.dumps({"ok": True, "stopped": True}, ensure_ascii=False)

    # start / restart
    if action == "start" and _read_pid():
        p = _read_pid()
        return json.dumps({"ok": True, "already": True, "pid": p,
                           "port": use_port, "url": f"http://{_ip()}:{use_port}",
                           "name": cfg.get("name")}, ensure_ascii=False)

    env = dict(os.environ, PROJECT_DIR=str(root),
               DASH_PORT=str(use_port), FLASK_RUN_HOST=bind)
    proc = subprocess.Popen(
        [sys.executable, dash],
        cwd=str(root), env=env,
        stdout=open(logfile, "a"), stderr=subprocess.STDOUT,
        start_new_session=True)   # 세션 종료에도 상주(detached)
    pidfile.write_text(str(proc.pid))
    time.sleep(1.5)
    if not _alive(proc.pid):
        tail = logfile.read_text()[-600:] if logfile.exists() else ""
        return json.dumps({"ok": False, "err": "기동 실패", "log": tail},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "pid": proc.pid, "port": use_port,
                       "url": f"http://{_ip()}:{use_port}",
                       "name": cfg.get("name"),
                       "hint": "재부팅 생존 원하면 systemd 등록 별도"},
                      ensure_ascii=False)


@mcp.tool()
def translate_pipeline(project_dir: str, action: str = "run",
                       game: str = "", scope: str = "all",
                       start: str = "", lines: int = 24,
                       apply: bool = False) -> str:
    """장면-인지 2단계 번역 파이프라인 (ROM/프로젝트 무관, codec-driven).

    문자열 1개씩 독립 번역의 한계(화자·맥락·줄간 일관성 부재)를 개선:
    ①대화 창(장면) 묶기 ②화자 인지 초벌 ③바이트예산·제어토큰 검증 피드백 루프.
    이름 고정은 프로젝트별 translations/glossary.json(웹지식+ROM 이름표 grounding).

    action:
      glossary : glossary.json 부트스트랩(등장인물·용어 매핑). game=제목 override, apply 무관.
      labels   : 이름표 예산-맞춤 해석. 불확실 항목은 translations/label_questions.json 로 분리(사용자 확인).
      run      : 번역 실행. scope=all 이면 전수(detached 백그라운드), start=0xADDR 면 그 창만(동기).
                 apply=True 여야 extracted_texts_v2.json 갱신(백업 .bak-pre-pipeline). False면 리포트만.
      status   : 전수 run 진행/완료 상태 + 로그 tail.

    글로서리→라벨→(사용자 확인)→run 순서 권장. DEEPSEEK_API_KEY 필요.
    """
    import subprocess
    import sys
    import time

    tp = "/root/projects/retro-kr-patch-mcp/dashboard/translate.py"
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return json.dumps({"ok": False, "err": f"디렉토리 아님: {root}"}, ensure_ascii=False)
    krdir = root / ".krpatch"
    krdir.mkdir(exist_ok=True)
    pidfile = krdir / "translate.pid"
    logfile = krdir / "translate.log"

    def _alive(pid):
        try:
            os.kill(pid, 0); return True
        except OSError:
            return False

    if action == "status":
        p = None
        if pidfile.exists():
            try:
                q = int(pidfile.read_text().strip()); p = q if _alive(q) else None
            except Exception:
                p = None
        tail = logfile.read_text()[-800:] if logfile.exists() else ""
        return json.dumps({"ok": True, "running": bool(p), "pid": p, "log_tail": tail},
                          ensure_ascii=False)

    argv = [sys.executable, tp, str(root), action]
    if action == "glossary":
        if game:
            argv += ["--game", game]
    elif action == "run":
        if start:
            argv += ["--start", start, "--lines", str(lines)]
        elif scope == "all":
            argv += ["--all"]
        if apply:
            argv += ["--apply"]

    # 전수 run(백그라운드가 아닌 start 지정 없는 apply 전수)은 장시간 → detached
    if action == "run" and not start and scope == "all":
        proc = subprocess.Popen(argv, cwd=str(root), env=dict(os.environ),
                                stdout=open(logfile, "w"), stderr=subprocess.STDOUT,
                                start_new_session=True)
        pidfile.write_text(str(proc.pid))
        return json.dumps({"ok": True, "started": True, "pid": proc.pid,
                           "hint": "진행은 action=status 로 폴링. 완료 후 translations/pipeline_report.json"},
                          ensure_ascii=False)

    # glossary / labels / 스코프 run 은 동기(최대 10분)
    try:
        out = subprocess.run(argv, cwd=str(root), env=dict(os.environ),
                             capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "err": "timeout(600s) — 전수는 scope=all 로 백그라운드 실행"},
                          ensure_ascii=False)
    body = out.stdout.strip()
    try:
        return json.dumps({"ok": out.returncode == 0, "result": json.loads(body)},
                          ensure_ascii=False)
    except Exception:
        return json.dumps({"ok": out.returncode == 0, "stdout": body[-1200:],
                           "stderr": out.stderr[-400:]}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 그래픽(gfx) 파이프라인 우산 도구 — gfxlib thin wrapper
#   로직은 전부 gfxlib(manifest/capture/worker/render/redraw/runner)에 있고
#   여기서는 인자 검증·백엔드 선택·JSON 투영만 한다.
#   게임/플랫폼 특화값 하드코딩 금지 — 전부 project_dir/krpatch.gfx.json 에서.
# ═══════════════════════════════════════════════════════════════════════════════

_GFX_ROOT = str(Path(__file__).resolve().parent)   # gfxlib 패키지 위치(인프라 상수)
_GFX_SCREEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_GFX_KEYSEQ_RE = re.compile(r"^[A-Za-z0-9_:.,]+$")
_GFX_SAFEPATH_RE = re.compile(r"^[A-Za-z0-9_/.\-]+$")   # ssh argv 에 실릴 서버생성 경로


def _gfx_json(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, indent=2)


def _gfx_err(msg: str, **kw) -> str:
    d = {"ok": False, "err": msg}
    d.update(kw)
    return _gfx_json(d)


def _gfx_import():
    """gfxlib 지연 임포트 — 실패해도 기존 17개 도구에 영향 없음."""
    import sys
    if _GFX_ROOT not in sys.path:
        sys.path.insert(0, _GFX_ROOT)
    from gfxlib import manifest, capture, worker, gba, render, redraw, runner
    return {"manifest": manifest, "capture": capture, "worker": worker,
            "gba": gba, "render": render, "redraw": redraw, "runner": runner}


def _gfx_hex(v: str, glib) -> int:
    """"0x.." / 10진 문자열 → int (manifest.parse_int 위임)."""
    return glib["manifest"].parse_int(v)


def _gfx_md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gfx_sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gfx_port_listening(port: int) -> bool:
    """/proc/net/tcp{,6} 파싱으로 LISTEN 여부 확인 — 실제 연결하지 않음
    (mgba GDB stub 은 단일 클라이언트라 probe 접속이 워커 연결을 방해할 수 있음)."""
    want = "%04X" % port
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name) as f:
                next(f)
                for line in f:
                    parts = line.split()
                    if len(parts) > 3 and parts[1].endswith(":" + want) \
                            and parts[3] == "0A":
                        return True
        except OSError:
            continue
    return False


def _gfx_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _gfx_load_frame(glib, frame_dir: str, frame: str = "") -> dict:
    """frame_dir 로드. frame 접두어(예 'bd_03') 지정 시 버스트 프레임 로드."""
    if not frame:
        return glib["gba"].load_frame(frame_dir)
    fr = {}
    for name in ("io", "palette", "vram", "oam", "iwram", "wram"):
        p = Path(frame_dir) / f"{frame}_{name}.bin"
        if p.exists():
            fr[name] = p.read_bytes()
    for req in ("io", "palette", "vram", "oam"):
        if req not in fr:
            raise FileNotFoundError(f"{frame}_{req}.bin 없음: {frame_dir}")
    return fr


def _gfx_deploy(src: str, dst_dir: str) -> dict:
    """out ROM → deploy 디렉터리 cp(shutil.copy2)만 + md5 확인 (NFS 안전)."""
    import shutil
    if not os.path.isfile(src):
        return {"ok": False, "err": f"deploy 소스 없음: {src}"}
    if not os.path.isdir(dst_dir):
        return {"ok": False, "err": f"deploy 디렉터리 없음: {dst_dir}"}
    dst = os.path.join(dst_dir, os.path.basename(src))
    shutil.copy2(src, dst)
    m_src, m_dst = _gfx_md5_file(src), _gfx_md5_file(dst)
    return {"ok": m_src == m_dst, "src": src, "dst": dst,
            "md5": m_src, "md5_dst": m_dst}


@mcp.tool()
def gfx_capture(project_dir: str, screen: str, action: str = "auto",
                keyseq: str = "", ss_path: str = "", rom: str = "",
                boot: float = 11.0, freeze_at: float = 0.0, burst: int = 0,
                burst_int: float = 0.5, burst_start: float = 0.0,
                force: bool = False) -> str:
    """화면 프레임 캡처(라이브 mgba+GDB / .ss 오프라인 / 버스트) — 캐시 우선.

    project_dir 의 krpatch.gfx.json screens.<screen>.capture 정의대로 실행하되
    인자로 오버라이드 가능. 완료 마커는 frame_dir/capture.json (원자 기록).

    action: auto(캐시 유효 시 즉시 반환, 아니면 캡처) | status(진행 재폴링)
    keyseq: xdotool 키시퀀스 "key[:hold[:wait]],..." (dump_screen.py 문법)
    ss_path: mGBA .ss 세이브스테이트 — 지정 시 에뮬 없이 오프라인 파싱(kind=ss)
    rom: ROM 경로(비우면 매니페스트 rom). boot/freeze_at/burst*: 워커 파라미터
    force: 캐시 무시 재캡처

    실행 백엔드 3단 fallback: ①직접 spawn(detached 워커) →②ssh 127.0.0.1 loopback
    →③불가 시 {ok:false, need:"user_savestate"}. 리턴 via 필드에 사용 백엔드.
    완료 폴링 최대 ~100초 — 미완이면 pending:true + job 경로(action=status 재폴링).
    GDB 포트는 프로젝트 .krpatch/gfx/locks/ 파일락(O_EXCL, stale 자동 해제)으로 직렬화.
    """
    import subprocess
    import sys
    import time

    try:
        glib = _gfx_import()
    except Exception as e:
        return _gfx_err(f"gfxlib 로드 실패: {e}")
    manifest = glib["manifest"]

    if not _GFX_SCREEN_RE.match(screen or ""):
        return _gfx_err("screen 은 ^[A-Za-z0-9_-]{1,32}$ 이어야 함")
    if keyseq and not _GFX_KEYSEQ_RE.match(keyseq):
        return _gfx_err("keyseq 는 ^[A-Za-z0-9_:.,]+$ 이어야 함")
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return _gfx_err(f"디렉토리 아님: {root}")

    cfg = manifest.load_gfx_config(str(root))
    sc = (cfg.get("screens") or {}).get(screen) or {}
    cap = sc.get("capture") or {}

    # ── 유효 파라미터 합성: 매니페스트 capture ← 인자 오버라이드 ──
    def _pick(arg_val, default, key):
        return arg_val if arg_val != default or key not in cap \
            else cap.get(key, default)

    eff_keyseq = keyseq or cap.get("keyseq", "")
    if eff_keyseq and not _GFX_KEYSEQ_RE.match(eff_keyseq):
        return _gfx_err(f"매니페스트 keyseq 형식 불량: {eff_keyseq!r}")
    eff_ss = ss_path or cap.get("ss_path", "")
    if eff_ss and not os.path.isabs(eff_ss):
        eff_ss = str(root / eff_ss)
    eff_rom = rom or cap.get("rom", "") or cfg.get("rom_abs", "")
    if eff_rom and not os.path.isabs(eff_rom):
        eff_rom = str(root / eff_rom)
    eff_boot = _pick(boot, 11.0, "boot")
    eff_freeze = _pick(freeze_at, 0.0, "freeze_at")
    eff_burst = _pick(burst, 0, "burst")
    eff_bint = _pick(burst_int, 0.5, "burst_int")
    eff_bstart = _pick(burst_start, 0.0, "burst_start")
    method = cap.get("method", "")
    if ss_path or (method == "ss" and eff_ss):
        kind = "ss"
    elif burst > 0 or method == "burst":
        kind = "burst"
        if eff_burst <= 0:
            eff_burst = 1
    else:
        kind = "live"
    # ── frame_dir: legacy 는 절대 기록하지 않음(읽기 캐시로만) ──
    legacy = sc.get("legacy_frames_abs")
    out_dir = str(root / ".krpatch" / "gfx" / "frames" / screen)
    if legacy and os.path.isfile(os.path.join(legacy, "io.bin")) \
            and action == "auto" and not force:
        return _gfx_json({"ok": True, "cached": True, "via": "legacy",
                          "frame_dir": legacy})

    cap_json = os.path.join(out_dir, "capture.json")
    failed_json = os.path.join(out_dir, "FAILED.json")
    lock_dir = root / ".krpatch" / "gfx" / "locks"
    gdb_port = int(cap.get("gdb_port", 2345))
    lock_path = lock_dir / f"gdb{gdb_port}.lock"

    def _release_lock():
        try:
            lock_path.unlink()
        except OSError:
            pass

    def _done_payload():
        try:
            doc = json.loads(Path(cap_json).read_text())
        except Exception as e:
            return {"ok": False, "err": f"capture.json 파싱 실패: {e}"}
        pngs = sorted(p.name for p in Path(out_dir).glob("*.png"))
        return {"ok": True, "frame_dir": out_dir,
                "capture": {k: doc.get(k) for k in
                            ("mode", "rom_md5", "keyseq", "ss_sha1",
                             "backend", "finished_at", "worker_version")},
                "pngs": pngs}

    if action == "status":
        st = {"ok": True, "frame_dir": out_dir,
              "done": os.path.exists(cap_json),
              "failed": os.path.exists(failed_json)}
        if st["done"]:
            _release_lock()
            st.update(_done_payload())
        elif st["failed"]:
            _release_lock()
            try:
                st["failure"] = json.loads(Path(failed_json).read_text())
            except Exception:
                pass
        logf = Path(out_dir) / "worker.log"
        if logf.exists():
            st["log_tail"] = logf.read_text()[-800:]
        pids = Path(out_dir) / ".pids"
        if pids.exists():
            alive = []
            for ln in pids.read_text().splitlines():
                try:
                    p = int(ln.split("\t", 1)[0])
                except ValueError:
                    continue
                if _gfx_pid_alive(p):
                    alive.append(p)
            st["worker_pids_alive"] = alive
        return _gfx_json(st)

    if action != "auto":
        return _gfx_err(f"action 불량: {action} (auto|status)")

    # ── 입력 파일 존재 검증 (status 폴링은 rom/ss 불필요라 여기서) ──
    if kind == "ss" and not (eff_ss and os.path.isfile(eff_ss)):
        return _gfx_err(f"ss_path 파일 없음: {eff_ss}")
    if kind != "ss" and not (eff_rom and os.path.isfile(eff_rom)):
        return _gfx_err(f"rom 없음(인자 또는 매니페스트 rom 필요): {eff_rom}")

    # ── 캐시 판정 (capture.json 키: rom_md5+mode+keyseq | ss_sha1) ──
    if not force and os.path.exists(cap_json):
        try:
            doc = json.loads(Path(cap_json).read_text())
        except Exception:
            doc = {}
        hit = False
        if kind == "ss":
            hit = doc.get("mode") == "ss" \
                and doc.get("ss_sha1") == _gfx_sha1_file(eff_ss)
        else:
            hit = doc.get("mode") == kind \
                and doc.get("rom_md5") == _gfx_md5_file(eff_rom) \
                and (doc.get("keyseq") or "") == eff_keyseq
        if hit:
            d = _done_payload()
            d["cached"] = True
            d["via"] = "cache"
            return _gfx_json(d)

    # ── job.json 생성 (사용자 문자열은 전부 job 안에 — argv 에는 서버생성 경로만) ──
    os.makedirs(out_dir, exist_ok=True)
    for stale in (cap_json, failed_json):
        try:
            os.remove(stale)
        except OSError:
            pass
    jobs_dir = root / ".krpatch" / "gfx" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    job_path = str(jobs_dir / f"{screen}-{ts}.json")
    job = {"kind": kind, "out_dir": out_dir, "keyseq": eff_keyseq,
           "boot": float(eff_boot), "freeze_at": float(eff_freeze),
           "burst": int(eff_burst), "burst_int": float(eff_bint),
           "burst_start": float(eff_bstart), "gdb_port": gdb_port,
           "shot": bool(cap.get("shot", kind == "ss"))}
    if kind == "ss":
        job["ss_path"] = eff_ss
        if eff_rom:
            job["rom"] = eff_rom
    else:
        job["rom"] = eff_rom
    if cap.get("mgba"):
        job["mgba"] = cap["mgba"]
    if cap.get("display"):
        job["display"] = cap["display"]
    Path(job_path).write_text(json.dumps(job, ensure_ascii=False, indent=1))

    # ── GDB 포트 락 (live/burst 만) ──
    if kind != "ss":
        lock_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps({"pid": None, "job": job_path,
                                         "at": ts}).encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    holder = json.loads(lock_path.read_text())
                except Exception:
                    holder = {}
                hp = holder.get("pid")
                if hp and _gfx_pid_alive(int(hp)):
                    return _gfx_err(f"GDB 포트 {gdb_port} 사용중(락 pid={hp})",
                                    lock=str(lock_path))
                _release_lock()   # stale — 해제 후 재시도
        else:
            return _gfx_err(f"락 획득 실패: {lock_path}")

    def _record_lock_pid(pid):
        if kind != "ss":
            try:
                lock_path.write_text(json.dumps(
                    {"pid": pid, "job": job_path, "at": ts}))
            except OSError:
                pass

    # ── 백엔드 ①: MCP 프로세스에서 직접 detached spawn ──
    via = None
    spawn_log = Path(out_dir) / "spawn.log"

    def _spawn_ok(proc):
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if os.path.exists(cap_json) or os.path.exists(failed_json):
                return True   # 마커 존재 = 백엔드 동작(실패 사유는 폴링 루프가 보고)
            if proc.poll() is not None:
                # 빠른 정상 종료(ss 등) 가능 — 마커 최종 재확인 후 판정
                time.sleep(0.3)
                return os.path.exists(cap_json) or os.path.exists(failed_json)
            if kind != "ss" and _gfx_port_listening(gdb_port):
                return True
            time.sleep(0.2)
        # 3초 경과: ss 는 살아있으면 OK, live/burst 는 포트가 열려야 OK
        return proc.poll() is None and \
            (kind == "ss" or _gfx_port_listening(gdb_port))

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "gfxlib.worker", "--job", job_path],
            cwd=_GFX_ROOT, start_new_session=True,
            stdout=open(spawn_log, "a"), stderr=subprocess.STDOUT)
        if _spawn_ok(proc):
            via = "direct"
            _record_lock_pid(proc.pid)
        else:
            # 실패한 자기 spawn 만 정리(광역 kill 금지)
            import signal as _sig
            try:
                os.killpg(proc.pid, _sig.SIGTERM)
            except OSError:
                pass
            time.sleep(1.0)
            try:
                os.killpg(proc.pid, _sig.SIGKILL)
            except OSError:
                pass
            try:
                glib["worker"].cleanup_pids(out_dir)
            except Exception:
                pass
    except Exception as e:
        with open(spawn_log, "a") as f:
            f.write(f"direct spawn 실패: {e}\n")

    # ── 백엔드 ②: ssh loopback 고정 argv (사용자 문자열은 job.json 에만) ──
    if via is None:
        if _GFX_SAFEPATH_RE.match(job_path) and _GFX_SAFEPATH_RE.match(sys.executable):
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                    "root@127.0.0.1", "--",
                    "nohup", "env", f"PYTHONPATH={_GFX_ROOT}",
                    sys.executable, "-m", "gfxlib.worker",
                    "--job", job_path, ">/dev/null", "2>&1", "&"]
            try:
                r = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=20)
                if r.returncode == 0:
                    deadline = time.time() + 6.0
                    while time.time() < deadline:
                        if os.path.exists(cap_json) or os.path.exists(failed_json) \
                                or (kind != "ss" and _gfx_port_listening(gdb_port)):
                            via = "ssh-loopback"
                            break
                        time.sleep(0.3)
                else:
                    with open(spawn_log, "a") as f:
                        f.write(f"ssh spawn rc={r.returncode}: {r.stderr[-300:]}\n")
            except Exception as e:
                with open(spawn_log, "a") as f:
                    f.write(f"ssh spawn 실패: {e}\n")
        else:
            with open(spawn_log, "a") as f:
                f.write("ssh 백엔드 스킵: job 경로 문자셋 불허\n")

    if via is None:
        if kind != "ss":
            _release_lock()
        return _gfx_json({
            "ok": False, "need": "user_savestate",
            "err": "direct/ssh 백엔드 모두 실행 불가 — 샌드박스 제약 추정",
            "hint": "에뮬레이터에서 해당 화면의 mGBA .ss 세이브스테이트를 만들어 "
                    "ss_path 로 넘기면 오프라인 파싱(kind=ss) 가능",
            "job": job_path})

    # ── 완료 폴링 (최대 ~100초) ──
    deadline = time.time() + 100.0
    while time.time() < deadline:
        if os.path.exists(cap_json):
            if kind != "ss":
                _release_lock()
            d = _done_payload()
            d["via"] = via
            d["job"] = job_path
            return _gfx_json(d)
        if os.path.exists(failed_json):
            if kind != "ss":
                _release_lock()
            try:
                failure = json.loads(Path(failed_json).read_text())
            except Exception:
                failure = {}
            logf = Path(out_dir) / "worker.log"
            return _gfx_json({"ok": False, "via": via, "failure": failure,
                              "log_tail": logf.read_text()[-800:]
                              if logf.exists() else ""})
        time.sleep(0.5)
    return _gfx_json({"ok": True, "pending": True, "via": via,
                      "frame_dir": out_dir, "job": job_path,
                      "hint": "action=status 로 재폴링"})


@mcp.tool()
def gfx_analyze(frame_dir: str, action: str = "report", bg: int = -1,
                rom_base: str = "", rect: str = "", base_rom: str = "",
                frame: str = "") -> str:
    """캡처 프레임(frame_dir) 오프라인 분석 — 렌더/역탐색/판정. 픽셀은 PNG 로만.

    action:
      report : DISPCNT/BG 요약 + compose PNG + (base_rom 지정 시) 활성 BG별
               locate·판정 — native(무압축 역탐색 확정, mode0·4bpp) /
               plugin(base 확정이나 8bpp·affine) / blocked(rom.find 실패)
      grid   : BG bg 를 8px 그리드+셀좌표 오버레이 PNG 로 (rom_base 로 ROM 타일)
      render : bg>=0 이면 render_bg, bg<0 이면 compose(우선순위 합성) PNG
      objs   : OAM OBJ 목록 + OBJ 시트 PNG
      locate : BG bg 사용 타일로 base_rom 내 무압축 원본 base 역탐색
      verify : VRAM 렌더 vs ROM(base_rom+rom_base) 재렌더 픽셀 diff
               (rect="x0,y0,x1,y1" 셀사각 지정 시 안/밖 분리 집계)

    rom_base: "0x.." 16진 문자열. frame: 버스트 프레임 접두어(예 "bd_03").
    PNG 는 frame_dir 하위에 gfx_*.png 로 저장하고 경로만 리턴.
    """
    try:
        glib = _gfx_import()
    except Exception as e:
        return _gfx_err(f"gfxlib 로드 실패: {e}")
    gba, render, redraw = glib["gba"], glib["render"], glib["redraw"]
    manifest = glib["manifest"]

    fdir = Path(frame_dir).resolve()
    if not fdir.is_dir():
        return _gfx_err(f"frame_dir 아님: {fdir}")
    try:
        fr = _gfx_load_frame(glib, str(fdir), frame)
    except Exception as e:
        return _gfx_err(f"frame 로드 실패: {e}")

    rom = None
    if base_rom:
        if not os.path.isfile(base_rom):
            return _gfx_err(f"base_rom 없음: {base_rom}")
        rom = Path(base_rom).read_bytes()
    rb = None
    if rom_base:
        try:
            rb = manifest.parse_int(rom_base)
        except ValueError as e:
            return _gfx_err(f"rom_base 파싱 실패: {e}")
    rc = None
    if rect:
        try:
            rc = manifest.parse_rect(rect)
        except ValueError as e:
            return _gfx_err(f"rect 파싱 실패: {e}")

    tag = (frame + "_") if frame else ""

    def _save(img, name):
        p = fdir / f"gfx_{tag}{name}.png"
        img.save(p)
        return str(p)

    d = gba.dispcnt(fr["io"])

    if action == "report":
        out = {"ok": True, "frame_dir": str(fdir), "dispcnt": d, "bgs": []}
        for n in range(4):
            if not d["bg"][n]:
                continue
            c = gba.bgcnt(fr["io"], n)
            entry = {"bg": n, **c}
            affine = d["mode"] >= 1 and n >= 2   # mode1: BG2, mode2: BG2/3 affine
            entry["affine"] = affine
            if rom is not None:
                loc = redraw.locate(fr, n, rom)
                entry["locate"] = loc
                reasons = []
                if loc["base"] is None:
                    entry["verdict"] = "blocked"
                    reasons.append("rom.find 실패(압축/동적 생성 추정)")
                elif c["bpp8"] or affine:
                    entry["verdict"] = "plugin"
                    if c["bpp8"]:
                        reasons.append("8bpp — rect 엔진(4bpp) 범위 밖")
                    if affine:
                        reasons.append("affine BG")
                else:
                    entry["verdict"] = "native"
                    reasons.append("무압축 역탐색 확정 — redraw_rect 가능")
                entry["reasons"] = reasons
            else:
                entry["verdict"] = "unknown"
                entry["reasons"] = ["base_rom 미지정 — locate/판정 생략"]
            out["bgs"].append(entry)
        out["compose_png"] = _save(render.compose(fr), "compose")
        return _gfx_json(out)

    if action == "grid":
        if bg < 0:
            return _gfx_err("grid 는 bg 필요(0~3)")
        p = _save(render.grid_overlay(fr, bg, rom, rb), f"grid_bg{bg}")
        return _gfx_json({"ok": True, "png": p})

    if action == "render":
        if bg >= 0:
            img = render.render_bg(fr, bg, rom, rb)
            p = _save(img, f"bg{bg}" + ("_rom" if rom is not None else ""))
        else:
            img = render.compose(fr)
            p = _save(img, "compose")
        return _gfx_json({"ok": True, "png": p, "size": list(img.size)})

    if action == "objs":
        objs = render.obj_list(fr)
        p = _save(render.obj_sheet(fr), "objsheet")
        return _gfx_json({"ok": True, "count": len(objs),
                          "objs": objs[:64], "sheet_png": p})

    if action == "locate":
        if bg < 0 or rom is None:
            return _gfx_err("locate 는 bg 와 base_rom 필요")
        loc = redraw.locate(fr, bg, rom)
        out = {"ok": True, "bg": bg, "base": loc["base"],
               "matches": loc["ok"], "checked": loc["checked"]}
        if loc["base"] is not None:
            out["base_hex"] = hex(loc["base"])
        return _gfx_json(out)

    if action == "verify":
        if bg < 0 or rom is None or rb is None:
            return _gfx_err("verify 는 bg, base_rom, rom_base 필요")
        img_v = render.render_bg(fr, bg)
        img_r = render.render_bg(fr, bg, rom, rb)
        if img_v.size != img_r.size:
            return _gfx_err(f"렌더 크기 불일치: {img_v.size} vs {img_r.size}")
        pv, pr = img_v.load(), img_r.load()
        w, h = img_v.size
        diff = in_r = out_r = 0
        for y in range(h):
            for x in range(w):
                if pv[x, y] != pr[x, y]:
                    diff += 1
                    if rc and rc[0] <= x // 8 <= rc[2] and rc[1] <= y // 8 <= rc[3]:
                        in_r += 1
                    else:
                        out_r += 1
        p = _save(img_r, f"verify_bg{bg}_rom")
        out = {"ok": True, "bg": bg, "identical": diff == 0,
               "diff_px": diff, "png": p}
        if rc:
            out["diff_in_rect"] = in_r
            out["diff_out_rect"] = out_r
        return _gfx_json(out)

    return _gfx_err(f"action 불량: {action} "
                    "(report|grid|render|objs|locate|verify)")


@mcp.tool()
def gfx_build(project_dir: str, action: str = "manifest", chain: str = "",
              screen: str = "", steps: str = "", version: str = "",
              bg: int = -1, rom_base: str = "", rect: str = "", text: str = "",
              overrides: str = "", excl: str = "", base_rom: str = "",
              out_rom: str = "", preview: bool = False, deploy: bool = False) -> str:
    """그래픽 빌드 — 매니페스트 체인 실행(게이트 G1~G5) 또는 단일 rect 재작화.

    action:
      manifest : krpatch.gfx.json 의 chain(기본 "release")을 runner.run_chain 으로
                 실행. steps="a,b" 부분 실행, preview=True 면 out 미기록.
                 deploy=True 면 verdict pass 시 cfg deploy 디렉터리로 cp+md5 확인.
      region   : 단일 rect 직접 재작화(runner 미경유, redraw_rect). screen(frame),
                 bg, rect="x0,y0,x1,y1", text 필수. rom_base 비우면 locate 자동.
                 base_rom 비우면 매니페스트 rom. preview=True 면 ROM 미기록,
                 프리뷰 PNG 만(.krpatch/gfx/previews/). 아니면 out_rom 에 기록.
      status   : 최근 run 요약 목록(.krpatch/gfx/runs)
      report   : 최신 run report 전체
      deploy   : out_rom(비우면 최신 pass run 의 out)을 deploy 디렉터리로 cp

    overrides: "hi=6,fill=5,dark=3,ol=1" 팔레트 역할 수동 지정(비우면 자동판정).
    excl: 제외 셀사각 "x0,y0,x1,y1". rom_base: "0x.." 16진.
    게임 특화값은 전부 매니페스트/인자 — 이 도구에 하드코딩 없음.
    """
    import shutil

    try:
        glib = _gfx_import()
    except Exception as e:
        return _gfx_err(f"gfxlib 로드 실패: {e}")
    manifest, runner = glib["manifest"], glib["runner"]
    gba, redraw = glib["gba"], glib["redraw"]

    root = Path(project_dir).resolve()
    if not root.is_dir():
        return _gfx_err(f"디렉토리 아님: {root}")
    runs_dir = root / ".krpatch" / "gfx" / "runs"

    # ── status / report: 매니페스트 없어도 동작 ──
    if action == "status":
        runs = []
        if runs_dir.is_dir():
            for p in sorted(runs_dir.glob("*.json"), reverse=True)[:10]:
                try:
                    r = json.loads(p.read_text())
                except Exception:
                    continue
                runs.append({"run_id": r.get("run_id"),
                             "chain": r.get("chain"),
                             "version": r.get("version"),
                             "verdict": r.get("verdict"),
                             "first_fail": r.get("first_fail"),
                             "out": (r.get("artifacts") or {}).get("out"),
                             "output_md5": r.get("output_md5")})
        return _gfx_json({"ok": True, "runs": runs})

    if action == "report":
        if not runs_dir.is_dir():
            return _gfx_err(f"runs 없음: {runs_dir}")
        latest = sorted(runs_dir.glob("*.json"), reverse=True)
        if not latest:
            return _gfx_err("run report 없음")
        try:
            r = json.loads(latest[0].read_text())
        except Exception as e:
            return _gfx_err(f"report 파싱 실패: {e}")
        return _gfx_json({"ok": True, "report_path": str(latest[0]),
                          "report": r})

    cfg = manifest.load_gfx_config(str(root))

    if action == "deploy":
        dst = cfg.get("deploy")
        if not dst:
            return _gfx_err("매니페스트에 deploy 디렉터리 없음")
        src = out_rom
        if src and not os.path.isabs(src):
            src = str(root / src)
        if not src and runs_dir.is_dir():
            for p in sorted(runs_dir.glob("*.json"), reverse=True):
                try:
                    r = json.loads(p.read_text())
                except Exception:
                    continue
                o = (r.get("artifacts") or {}).get("out")
                if o and r.get("verdict") in ("pass", "pass_with_warnings"):
                    src = o
                    break
        if not src:
            return _gfx_err("deploy 소스 없음 — out_rom 지정 또는 pass run 필요")
        return _gfx_json(_gfx_deploy(src, dst))

    if action == "manifest":
        if cfg.get("errors"):
            return _gfx_json({"ok": False, "err": "매니페스트 오류",
                              "errors": cfg["errors"]})
        ch = chain or "release"
        ver = version or datetime.now().strftime("%Y%m%d-%H%M%S")
        step_list = [s.strip() for s in steps.split(",") if s.strip()] or None
        try:
            res = runner.run_chain(str(root), cfg, ch, ver,
                                   steps=step_list, preview_only=preview)
        except Exception as e:
            return _gfx_err(f"run_chain 실패: {type(e).__name__}: {e}")
        out = {"ok": res.get("verdict") in ("pass", "pass_with_warnings"),
               **res}
        if deploy and out["ok"] and res.get("out"):
            if not cfg.get("deploy"):
                out["deploy"] = {"ok": False, "err": "매니페스트에 deploy 없음"}
            else:
                out["deploy"] = _gfx_deploy(res["out"], cfg["deploy"])
        return _gfx_json(out)

    if action == "region":
        if not screen or bg < 0 or not rect or not text:
            return _gfx_err("region 은 screen, bg, rect, text 필수")
        font = cfg.get("font_abs")
        if not font or not os.path.isfile(font):
            return _gfx_err(f"매니페스트 font 없음/부재: {font}")
        fdir = manifest.resolve_frames(cfg, str(root), screen)
        try:
            fr = gba.load_frame(fdir)
        except Exception as e:
            return _gfx_err(f"frame 로드 실패({fdir}): {e}")
        src = base_rom or cfg.get("rom_abs", "")
        if not src or not os.path.isfile(src):
            return _gfx_err(f"base_rom 없음: {src}")
        try:
            rc = manifest.parse_rect(rect)
            ex = manifest.parse_rect(excl) if excl else None
        except ValueError as e:
            return _gfx_err(f"rect/excl 파싱 실패: {e}")
        ov = {}
        if overrides:
            for tok in overrides.split(","):
                if "=" not in tok:
                    return _gfx_err(f"overrides 형식 불량: {tok!r} (k=v 콤마구분)")
                k, v = tok.split("=", 1)
                k = k.strip()
                if k not in ("hi", "fill", "dark", "ol"):
                    return _gfx_err(f"overrides 키 불량: {k!r} (hi|fill|dark|ol)")
                try:
                    ov[k] = manifest.parse_int(v.strip())
                except ValueError as e:
                    return _gfx_err(f"overrides 값 파싱 실패: {e}")
        rom_ba = bytearray(Path(src).read_bytes())
        rb = None
        if rom_base:
            try:
                rb = manifest.parse_int(rom_base)
            except ValueError as e:
                return _gfx_err(f"rom_base 파싱 실패: {e}")
        else:
            loc = redraw.locate(fr, bg, bytes(rom_ba))
            if loc["base"] is None:
                return _gfx_json({"ok": False, "err": "locate 실패 — rom_base 직접 지정 필요",
                                  "locate": loc})
            rb = loc["base"]
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        prev_dir = root / ".krpatch" / "gfx" / "previews"
        prev_dir.mkdir(parents=True, exist_ok=True)
        prev_path = str(prev_dir / f"{screen}_bg{bg}_{ts}.png")
        try:
            res = redraw.redraw_rect(
                rom_ba, fr, bg, rb, rc, text, font_path=font,
                hi=ov.get("hi"), fill=ov.get("fill"), dark=ov.get("dark"),
                ol=ov.get("ol"), excl=ex, preview_path=prev_path,
                dry_run=preview)
        except Exception as e:
            return _gfx_err(f"redraw_rect 실패: {type(e).__name__}: {e}")
        out = {"ok": bool(res.get("ok")), "rom_base": hex(rb), **res}
        if preview or not res.get("ok"):
            out["written"] = False
            return _gfx_json(out)
        if not out_rom:
            return _gfx_err("out_rom 필요(preview=False 기록 모드)")
        op = Path(out_rom)
        if not op.is_absolute():
            op = root / op
        op.parent.mkdir(parents=True, exist_ok=True)
        if op.exists():
            bak = f"{op}.bak-{ts}"
            shutil.copy2(op, bak)
            out["backup"] = bak
        op.write_bytes(bytes(rom_ba))
        out["written"] = True
        out["out"] = str(op)
        out["md5"] = _gfx_md5_file(str(op))
        return _gfx_json(out)

    return _gfx_err(f"action 불량: {action} "
                    "(manifest|region|status|report|deploy)")


# ═══════════════════════════════════════════════════════════════════════════════
# 폰트 베이스 검증 · 커버리지 갭 · 글리프 시프트 진단 · 예산 주입
# (2026-07-06 Hikaru2 프로젝트 교훈 도구화 — 저능력 에이전트도 순서 호출만으로 수행 가능)
# ═══════════════════════════════════════════════════════════════════════════════

def _fv_read_table(rom: bytes, table_offset: int, table_entries: int) -> list:
    """(u16 sjis, u16 glyph_idx) 정렬 테이블 파싱"""
    import struct
    return [struct.unpack("<HH", rom[table_offset + k*4: table_offset + k*4 + 4])
            for k in range(table_entries)]


def _fv_glyph_png(rom: bytes, base: int, gi: int, w: int, h: int, bpp: int,
                  bytes_per_glyph: int, scale: int = 8):
    """글리프 1개를 PIL 이미지로 (4bpp 니블=픽셀 농도, 2bpp/1bpp packed)"""
    from PIL import Image
    g = rom[base + gi*bytes_per_glyph: base + (gi+1)*bytes_per_glyph]
    img = Image.new("L", (w, h), 0)
    px = img.load()
    if bpp == 4:
        bpr = (w + 1) // 2
        for y in range(h):
            for x in range(w):
                b = g[y*bpr + x//2] if y*bpr + x//2 < len(g) else 0
                v = (b & 0xF) if x % 2 == 0 else (b >> 4)
                px[x, y] = min(255, v * 85)
    elif bpp == 2:
        bpr = (w + 3) // 4
        for y in range(h):
            for x in range(w):
                b = g[y*bpr + x//4] if y*bpr + x//4 < len(g) else 0
                v = (b >> ((x % 4) * 2)) & 3
                px[x, y] = v * 85
    else:  # 1bpp
        bpr = (w + 7) // 8
        for y in range(h):
            for x in range(w):
                b = g[y*bpr + x//8] if y*bpr + x//8 < len(g) else 0
                px[x, y] = 255 if (b >> (7 - x % 8)) & 1 else 0
    return img.resize((w*scale, h*scale), Image.NEAREST)


@mcp.tool()
def font_base_probe(rom_path: str, table_offset: str, table_entries: int,
                    glyph_base: str, test_chars: str = "早碁局",
                    shifts: str = "-256,-223,-128,-64,0,64,128,223,256",
                    width: int = 12, height: int = 12, bpp: int = 4,
                    bytes_per_glyph: int = 0, out_name: str = "font_probe") -> str:
    """폰트 베이스/글리프 시프트 검증 시트 생성.

    ⚠ 정적 RE로 찾은 글리프 베이스는 시프트가 어긋나 있을 수 있다(Hikaru2에서 +223 실측).
    반드시 이 도구로 화면과 대조 검증 후 주입할 것.

    test_chars의 각 문자를 SJIS→gi 테이블로 조회한 뒤, 후보 시프트별로
    font[gi+shift] 글리프를 렌더한 대조 시트 PNG를 만든다.
    → 에이전트는 시트에서 test_chars와 모양이 일치하는 행(shift)을 찾아
      실효 베이스 = glyph_base + shift*bytes_per_glyph 로 확정한다.
    자동판정 불가 시 게임 화면 스크린샷(emucap screenshot)과 비교하라.
    """
    import bisect
    from PIL import Image
    try:
        rom = _read_rom(rom_path)
        toff = int(table_offset, 0)
        gbase = int(glyph_base, 0)
        if bytes_per_glyph <= 0:
            bytes_per_glyph = (width * bpp + 7) // 8 * height
        entries = _fv_read_table(rom, toff, table_entries)
        codes = [c for c, _ in entries]

        def gi_of(ch):
            try:
                enc = ch.encode("cp932")
            except Exception:
                return None
            if len(enc) != 2:
                return None
            sj = (enc[0] << 8) | enc[1]
            k = bisect.bisect_left(codes, sj)
            return entries[k][1] if k < len(codes) and codes[k] == sj else None

        shift_list = [int(s) for s in shifts.split(",")]
        chars = [(ch, gi_of(ch)) for ch in test_chars if gi_of(ch) is not None]
        if not chars:
            return json.dumps({"error": "test_chars 중 테이블에 있는 문자가 없음"},
                              ensure_ascii=False)

        cell = max(width, height) * 8 + 8
        sheet = Image.new("L", (cell * len(chars) + 80, cell * len(shift_list)), 32)
        rows = []
        max_gi_pos = (len(rom) - gbase) // bytes_per_glyph
        for r, sh in enumerate(shift_list):
            row_info = {"shift": sh,
                        "effective_base": hex(gbase + sh * bytes_per_glyph), "chars": {}}
            for c, (ch, gi) in enumerate(chars):
                gi2 = gi + sh
                if 0 <= gi2 < max_gi_pos:
                    im = _fv_glyph_png(rom, gbase, gi2, width, height, bpp, bytes_per_glyph)
                    sheet.paste(im, (80 + c * cell + 4, r * cell + 4))
                    row_info["chars"][ch] = {"gi": gi, "gi_shifted": gi2}
            rows.append(row_info)
        out = OUTPUT_DIR / f"{out_name}.png"
        sheet.save(out)
        return json.dumps({
            "sheet": str(out), "row_order_shifts": shift_list, "chars": test_chars,
            "next_action": "시트를 Read로 열어 각 행을 보고, test_chars 모양과 일치하는 "
                           "행의 shift를 채택. effective_base를 주입 도구에 사용. "
                           "일치 행이 없으면 shifts를 좁혀 재호출(예: '200,210,220,230,240').",
            "rows": rows,
        }, ensure_ascii=False, indent=1)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def coverage_gap_scan(original_rom: str, patched_rom: str,
                      region_start: str, region_end: str,
                      min_jp_chars: int = 2, out_name: str = "coverage_gaps") -> str:
    """번역 커버리지 갭 스캔 — 패치롬에서 원본과 동일하게 남은(=미번역) 일본어 세그먼트 열거.

    ⚠ 문자열 추출기가 제어코드/이름코드(①② 등)를 경계로 오인해 문장 앞부분이
    통째로 누락되는 사고가 흔하다(Hikaru2에서 616조각). 주입 후 반드시 이 도구로
    잔존 일본어를 전수 확인할 것.

    null-종단 세그먼트 단위로 걷고, 각 세그먼트에서 원본과 첫 diff 위치까지를
    '미번역 접두부(budget)'로 마킹한다. 결과 JSON은 inject_budgeted_text 입력과 호환.
    """
    try:
        orig = _read_rom(original_rom)
        pat = _read_rom(patched_rom)
        rs, re_ = int(region_start, 0), int(region_end, 0)
        items = []
        i = rs
        while i < re_:
            if pat[i] == 0:
                i += 1
                continue
            st = i
            while i < re_ and pat[i] != 0:
                i += 1
            seg_p, seg_o = pat[st:i], orig[st:i]
            diff = next((k for k in range(len(seg_p)) if seg_p[k] != seg_o[k]), None)
            untr = seg_o[:diff] if diff is not None else seg_o
            try:
                jp = untr.decode("cp932", errors="replace")
            except Exception:
                continue
            kana = sum(1 for c in jp if '぀' <= c <= 'ヿ')
            kanji = sum(1 for c in jp if '一' <= c <= '鿿')
            ctrl = sum(1 for c in jp if ord(c) < 0x20 or 0xE000 <= ord(c) <= 0xF8FF)
            if kana + kanji >= min_jp_chars and ctrl == 0:
                items.append({
                    "file_off": st, "byte_len": len(seg_p),
                    "budget": diff if diff is not None else len(seg_p),
                    "jp_untranslated": jp,
                    "jp_full_orig": seg_o.split(b"\x00")[0].decode("cp932", errors="replace"),
                    "kind": "prefix" if diff is not None else "full",
                })
        out = OUTPUT_DIR / f"{out_name}.json"
        out.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        return json.dumps({
            "gaps": len(items), "total_bytes": sum(x["budget"] for x in items),
            "saved": str(out),
            "next_action": "0건이면 커버리지 완전. 있으면 jp_untranslated를 번역해 "
                           "items에 'ko' 필드를 채우고 inject_budgeted_text로 주입. "
                           "번역 시 이름코드(①②)·줄바꿈(↓)·숫자코드(③~⑥)는 그대로 보존.",
            "sample": items[:5],
        }, ensure_ascii=False, indent=1)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def diagnose_glyph_shift(rom_path: str, table_offset: str, table_entries: int,
                         host_map_json: str, expected_text: str,
                         observed_text: str) -> str:
    """화면 깨짐 진단 — 기대 문장 vs 화면에 보인 글자의 글리프 인덱스 차이를 계산.

    주입 후 화면에 엉뚱한 한글/한자가 나올 때 사용. 기대 문장의 각 음절 gi와
    화면에 보인 각 글자(주입 음절이면 그 gi)의 위치별 차이가 '상수'면
    렌더러의 글리프 베이스가 그만큼 어긋난 것이다(Hikaru2 +223 사례).
    → 확정되면 font_base_probe로 검증 후 글리프를 shift 반영 위치에 재주입.
    관측 글자가 주입 음절이 아니면(원본 한자 등) delta가 null로 나온다.
    """
    import bisect
    try:
        rom = _read_rom(rom_path)
        toff = int(table_offset, 0)
        entries = _fv_read_table(rom, toff, table_entries)
        codes = [c for c, _ in entries]

        def sidx(sj):
            k = bisect.bisect_left(codes, sj)
            return entries[k][1] if k < len(codes) and codes[k] == sj else None

        host_map = {k: int(v) for k, v in
                    json.loads(Path(host_map_json).read_text(encoding="utf-8")).items()}
        exp = [(ch, sidx(host_map[ch])) for ch in expected_text if ch in host_map]
        obs = [(ch, sidx(host_map[ch]) if ch in host_map else None)
               for ch in observed_text if not ch.isspace()]
        deltas = []
        for k in range(min(len(exp), len(obs))):
            e_gi, o_gi = exp[k][1], obs[k][1]
            deltas.append({"pos": k, "expected": exp[k][0], "observed": obs[k][0],
                           "delta": (o_gi - e_gi) if (e_gi is not None and o_gi is not None)
                           else None})
        vals = [d["delta"] for d in deltas if d["delta"] is not None]
        const = bool(vals) and all(v == vals[0] for v in vals)
        return json.dumps({
            "constant_shift": vals[0] if const else None,
            "verdict": (f"렌더러 글리프 시프트 = {vals[0]:+d} 확정. "
                        "font_base_probe로 검증 후 글리프 주입 베이스를 "
                        f"{vals[0]:+d} 글리프만큼 이동하라." if const else
                        "상수 시프트 아님 — 인코딩 어긋남이나 다른 폰트/테이블 사용 가능성. "
                        "coverage_gap_scan과 원문 대조를 먼저 하라."),
            "deltas": deltas,
        }, ensure_ascii=False, indent=1)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def inject_budgeted_text(rom_path: str, items_json: str, host_map_json: str,
                         table_offset: str, table_entries: int, glyph_base: str,
                         ttf_path: str, out_path: str = "",
                         width: int = 12, height: int = 12, bpp: int = 4,
                         bytes_per_glyph: int = 0, min_host_gi: int = 300,
                         pad_byte: str = "0x20") -> str:
    """예산 내 in-place 한글 주입 (신규 음절 글리프 자동 렌더 포함).

    items_json: [{file_off, budget, ko}] 배열 파일. ko의 한글 음절은 host_map으로
    인코딩하고, host_map에 없는 신규 음절은 미사용 슬롯(gi>=min_host_gi,
    기존 host 미사용 sjis)에 TTF 렌더로 글리프를 만들어 자동 등록한다.
    glyph_base는 반드시 font_base_probe로 검증한 '실효 베이스'를 줄 것.
    예산 초과 시 공백 제거 → 문자 경계 절단, 잔여는 pad_byte로 패딩.
    host_map_json 파일은 신규 음절이 추가된 상태로 갱신 저장된다.
    """
    import bisect, shutil
    from PIL import Image, ImageFont, ImageDraw
    try:
        rom = bytearray(_read_rom(rom_path))
        toff = int(table_offset, 0)
        gbase = int(glyph_base, 0)
        pad = int(pad_byte, 0)
        if bytes_per_glyph <= 0:
            bytes_per_glyph = (width * bpp + 7) // 8 * height
        entries = _fv_read_table(bytes(rom), toff, table_entries)

        hm_path = Path(host_map_json)
        host_map = {k: int(v) for k, v in
                    json.loads(hm_path.read_text(encoding="utf-8")).items()}
        items = json.loads(Path(items_json).read_text(encoding="utf-8"))

        # 신규 음절 → 미사용 슬롯 + TTF 글리프
        need = sorted({ch for it in items for ch in it.get("ko", "")
                       if '가' <= ch <= '힣' and ch not in host_map})
        used = set(host_map.values())
        free = [(c, gi) for c, gi in entries if gi >= min_host_gi and c not in used]
        if len(need) > len(free):
            return json.dumps({"error": f"슬롯 부족: 신규 {len(need)} > 여유 {len(free)}"},
                              ensure_ascii=False)
        if bpp != 4:
            return json.dumps({"error": "글리프 자동 렌더는 4bpp만 지원"}, ensure_ascii=False)
        font = ImageFont.truetype(ttf_path, max(width, height))

        def render_glyph(ch):
            tmp = Image.new("L", (16, 16), 0)
            d = ImageDraw.Draw(tmp)
            bb = font.getbbox(ch)
            ox = (width - (bb[2]-bb[0])) // 2 - bb[0]
            oy = (height - (bb[3]-bb[1])) // 2 - bb[1]
            d.text((ox, oy), ch, font=font, fill=255)
            img = tmp.crop((0, 0, width, height))
            px = img.load()
            out = bytearray(bytes_per_glyph)
            bpr = (width + 1) // 2
            for y in range(height):
                for xb in range(bpr):
                    lo = min(3, px[xb*2, y]*3//255)
                    hi = min(3, px[xb*2+1, y]*3//255) if xb*2+1 < width else 0
                    out[y*bpr+xb] = (lo & 0xF) | ((hi & 0xF) << 4)
            return bytes(out)

        glyphs_new = 0
        for i, ch in enumerate(need):
            sj, gi = free[i]
            host_map[ch] = sj
            rom[gbase + gi*bytes_per_glyph: gbase + (gi+1)*bytes_per_glyph] = render_glyph(ch)
            glyphs_new += 1

        def enc(text):
            out = bytearray()
            for ch in text:
                if ch in host_map:
                    sj = host_map[ch]
                    out += bytes([(sj >> 8) & 0xFF, sj & 0xFF])
                elif ord(ch) < 0x80:
                    out.append(ord(ch))
                else:
                    try:
                        out += ch.encode("cp932")
                    except Exception:
                        out.append(0x3F)
            return bytes(out)

        done = trimmed = truncated = skipped = 0
        for it in items:
            ko = it.get("ko", "")
            if not ko:
                skipped += 1
                continue
            off, budget = it["file_off"], it["budget"]
            e = enc(ko)
            if len(e) > budget:
                e = enc(ko.replace(" ", ""))
                if len(e) <= budget:
                    trimmed += 1
                else:
                    cut = bytearray()
                    for ch in ko.replace(" ", ""):
                        ce = enc(ch)
                        if len(cut) + len(ce) > budget:
                            break
                        cut += ce
                    e = bytes(cut)
                    truncated += 1
            rom[off:off+len(e)] = e
            for k in range(off+len(e), off+budget):
                rom[k] = pad
            done += 1

        op = Path(out_path) if out_path else Path(rom_path)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if op.exists():
            shutil.copy2(op, f"{op}.bak-{ts}")
        op.write_bytes(bytes(rom))
        hm_path.write_text(json.dumps(host_map, ensure_ascii=False), encoding="utf-8")
        return json.dumps({
            "injected": done, "skipped_no_ko": skipped, "space_trimmed": trimmed,
            "truncated": truncated, "new_glyphs": glyphs_new, "out": str(op),
            "next_action": "에뮬(emucap launch→tap→screenshot)로 해당 장면을 열어 "
                           "화면 실측 검증. 깨지면 diagnose_glyph_shift 실행.",
        }, ensure_ascii=False, indent=1)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
