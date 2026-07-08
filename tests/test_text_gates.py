#!/usr/bin/env python3
"""텍스트 경로 정합성 게이트(T1~T5) 회귀 테스트.

인메모리 가짜 ROM(인접 null-종단 SJIS 문자열 3개 + 가짜 host 테이블)으로
2026-07-07 병합 사고("정답입니다.오답입니다.") 재발 방지 불변식을 검증한다.
실행: /opt/retro-kr-patch-venv/bin/python tests/test_text_gates.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tools_server as ts  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="krpatch_gates_"))

# ── 가짜 ROM 구성 ────────────────────────────────────────────────────────────
# 레이아웃: [0x00~0x0F 여백] [S1] [S2(토큰 0x41 포함)] [S3] [테이블 @0x100]
S1_OFF = 0x10
S1 = "せいかい".encode("cp932") + b"\x00"          # 8 + 1
S2_OFF = S1_OFF + len(S1)
S2 = b"\x41" + "です".encode("cp932") + b"\x00"    # 1 + 4 + 1 (0x41 = 제어 토큰)
S3_OFF = S2_OFF + len(S2)
S3 = "おわり".encode("cp932") + b"\x00"            # 6 + 1

# 가짜 host 테이블 (u16 sjis, u16 gi) LE — sjis 정렬. gi>=300은 탈취 가능 슬롯.
TABLE_OFF = 0x100
TABLE = [
    (0x8840, 100), (0x8841, 101), (0x8842, 102), (0x8843, 103),
    (0x88EE, 300),  # 탈취 대상 슬롯 (audit 테스트)
    (0x88EF, 301),
]
HOST_MAP = {"정": 0x8840, "답": 0x8841, "입": 0x8842, "니": 0x8843}
CTRL_TOKENS = json.dumps([{"bytes": "41", "name": "name_sub", "atomic": True}])


def make_rom() -> bytearray:
    rom = bytearray(0x200)
    rom[S1_OFF:S1_OFF + len(S1)] = S1
    rom[S2_OFF:S2_OFF + len(S2)] = S2
    rom[S3_OFF:S3_OFF + len(S3)] = S3
    import struct
    for k, (sj, gi) in enumerate(TABLE):
        rom[TABLE_OFF + k * 4: TABLE_OFF + k * 4 + 4] = struct.pack("<HH", sj, gi)
    return rom


def setup_files(rom: bytes, items: list, tag: str):
    rp = TMP / f"rom_{tag}.bin"
    rp.write_bytes(rom)
    ip = TMP / f"items_{tag}.json"
    ip.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    hp = TMP / f"hostmap_{tag}.json"
    hp.write_text(json.dumps(HOST_MAP, ensure_ascii=False), encoding="utf-8")
    return rp, ip, hp


def inject(rom, items, tag, **kw):
    rp, ip, hp = setup_files(rom, items, tag)
    op = TMP / f"out_{tag}.bin"
    res = json.loads(ts.inject_budgeted_text(
        rom_path=str(rp), items_json=str(ip), host_map_json=str(hp),
        table_offset=hex(TABLE_OFF), table_entries=len(TABLE),
        glyph_base="0x1F0", ttf_path="", out_path=str(op), **kw))
    return res, op


PASS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    PASS.append(bool(cond))


# ── (1) 예산 정확 충전이 종단자를 보존 (사고 재현 회귀) ──────────────────────
# S1 예산: byte_len_with_null = 9 (내용 8 + null). "정답입니" 인코딩 = 8바이트
# = 예산 꽉 채움 → 종전 코드라면 null을 덮어 S2와 병합됐다.
rom = make_rom()
res, op = inject(rom, [{"file_off": S1_OFF, "budget": 9, "ko": "정답입니"}], "exactfill")
out = op.read_bytes()
check("1a 게이트 통과", res.get("verdict") == "pass", json.dumps(res, ensure_ascii=False))
nul = out[S1_OFF:S1_OFF + 9].find(b"\x00")
check("1b 예산 윈도우 안에 종단자 존재", nul >= 0)
check("1c 인접 문자열 S2 무손상", out[S2_OFF:S2_OFF + len(S2)] == S2)
check("1d 절단으로 내용이 예산-1 이하", nul <= 8)

# ── (2) pad_byte 기본값이 0x00 ───────────────────────────────────────────────
rom = make_rom()
res, op = inject(rom, [{"file_off": S1_OFF, "budget": 9, "ko": "정답"}], "paddefault")
out = op.read_bytes()
check("2a 게이트 통과", res.get("verdict") == "pass")
check("2b 잔여 패딩 전부 0x00", all(b == 0 for b in out[S1_OFF + 4:S1_OFF + 9]),
      out[S1_OFF:S1_OFF + 9].hex(" "))

# ── (3) T4가 고의 병합(종단자 소실)을 검출 ───────────────────────────────────
rom = make_rom()
corrupt = bytearray(rom)
# S1 영역을 null 없이 꽉 채워 S2와 병합시킨다 (사고 상태 재현)
enc = ts._tp_encode("정답입니", HOST_MAP, [])
corrupt[S1_OFF:S1_OFF + 9] = enc + b"\x81"  # 9바이트, 종단자 없음
rp = TMP / "rom_merge.bin"
rp.write_bytes(bytes(corrupt))
ip = TMP / "items_merge.json"
ip.write_text(json.dumps([{"file_off": S1_OFF, "budget": 9, "ko": "정답입니"}]),
              encoding="utf-8")
hp = TMP / "hostmap_merge.json"
hp.write_text(json.dumps(HOST_MAP, ensure_ascii=False), encoding="utf-8")
vres = json.loads(ts.verify_injection(str(rp), str(ip), str(hp)))
check("3a verify_injection 전체 verdict=fail", vres.get("verdict") == "fail")
check("3b T4(no_merge) 실패 검출", vres["gates"]["T4"]["pass"] is False,
      json.dumps(vres.get("gates", {}), ensure_ascii=False))

# ── (4) 절단이 절름 선두 바이트를 남기지 않음 ────────────────────────────────
# 예산 6(윈도우 6, 내용 5): 2바이트 음절만이라 5바이트 절단 유혹 → 4바이트(2음절)여야 함
rom = make_rom()
res, op = inject(rom, [{"file_off": S1_OFF, "budget": 6, "ko": "정답입니"}], "trunclead")
out = op.read_bytes()
check("4a 게이트 통과(T3 포함)", res.get("verdict") == "pass",
      json.dumps(res.get("gates", {}), ensure_ascii=False))
win = out[S1_OFF:S1_OFF + 6]
nul = win.find(b"\x00")
check("4b 내용 길이 짝수(2바이트 쌍 보존)", nul >= 0 and nul % 2 == 0, win.hex(" "))
check("4c 말미가 절름 선두 아님",
      nul == 0 or not ts._tp_is_lead(win[nul - 1]) or (nul >= 2 and ts._tp_is_lead(win[nul - 2])))

# ── (5) 제어 토큰 0x41 바이트 보존 + T5가 손실을 검출 ────────────────────────
# S2 재주입: "A정답" (A = name_sub 토큰). 예산 = len(S2) = 6 (내용 5 + null)
rom = make_rom()
res, op = inject(rom, [{"file_off": S2_OFF, "budget": 6, "ko": "A정답"}],
                 "token", ctrl_tokens=CTRL_TOKENS)
out = op.read_bytes()
check("5a 게이트 통과", res.get("verdict") == "pass",
      json.dumps(res, ensure_ascii=False))
check("5b 토큰 0x41 바이트 원형 보존", out[S2_OFF] == 0x41, out[S2_OFF:S2_OFF + 6].hex(" "))
# 토큰 바이트를 고의로 파괴 → T5 실패해야 함
broken = bytearray(out)
broken[S2_OFF] = 0x42  # 'B'로 치환 = name_sub 토큰 손실
rp = TMP / "rom_tokenloss.bin"
rp.write_bytes(bytes(broken))
ip = TMP / "items_tokenloss.json"
ip.write_text(json.dumps([{"file_off": S2_OFF, "budget": 6, "ko": "A정답"}],
                         ensure_ascii=False), encoding="utf-8")
vres = json.loads(ts.verify_injection(str(rp), str(ip),
                                      str(TMP / "hostmap_token.json"),
                                      ctrl_tokens=CTRL_TOKENS))
check("5c T5(ctrl_token_guard)가 토큰 손실 검출", vres["gates"]["T5"]["pass"] is False,
      json.dumps(vres.get("gates", {}), ensure_ascii=False))

# ── (6) glyph_slot_audit — 탈취 슬롯 참조 검출 ───────────────────────────────
# 슬롯 gi=300(sjis 0x88EE)을 한글로 탈취했다고 가정, S3 뒤 원시 영역과
# S1 문자열 안에 0x88EE 참조를 심는다.
rom = make_rom()
rom[S1_OFF:S1_OFF + 2] = bytes([0x88, 0xEE])          # 문자열 내 참조
rom[0x80:0x82] = bytes([0x88, 0xEE])                   # 문자열 밖 원시 참조 (이름 템플릿 후보)
rp = TMP / "rom_audit.bin"
rp.write_bytes(bytes(rom))
# 알려진 문자열 목록을 명시 → 0x80 원시 참조는 '문자열 밖'으로 분류돼야 함
strings = json.dumps([
    {"file_off": S1_OFF, "byte_len_with_null": len(S1)},
    {"file_off": S2_OFF, "byte_len_with_null": len(S2)},
    {"file_off": S3_OFF, "byte_len_with_null": len(S3)},
])
ares = json.loads(ts.glyph_slot_audit(
    rom_path=str(rp), table_offset=hex(TABLE_OFF), table_entries=len(TABLE),
    glyph_slots_used=json.dumps([300]),
    region_start="0x0", region_end="0x100", strings_json=strings))
kinds = {c["kind"] for c in ares.get("collisions", [])}
check("6a 문자열 내 참조(string_ref) 검출", "string_ref" in kinds,
      json.dumps(ares, ensure_ascii=False))
check("6b 문자열 밖 원시 참조(raw_ref) 검출", "raw_ref" in kinds)
check("6c sjis 코드 0x88EE로 보고",
      any(c["sjis_code"] == "0x88EE" for c in ares.get("collisions", [])))
# 토큰 충돌 케이스: 0x88EE를 토큰으로 등록하면 ctrl_token 충돌
ares2 = json.loads(ts.glyph_slot_audit(
    rom_path=str(rp), table_offset=hex(TABLE_OFF), table_entries=len(TABLE),
    glyph_slots_used=json.dumps(["0x88EE"]),
    ctrl_tokens=json.dumps([{"bytes": "88ee", "name": "digit_sub"}])))
check("6d 토큰-슬롯 충돌(ctrl_token) 검출",
      any(c["kind"] == "ctrl_token" for c in ares2.get("collisions", [])))

# ── (보너스) 안전 재산정 — 틀린 budget이 클램프되어 다음 필드 불침범 ─────────
rom = make_rom()
res, op = inject(rom, [{"file_off": S1_OFF, "budget": 30, "ko": "정답입니 정답입니"}],
                 "clamp")
out = op.read_bytes()
check("7a 과대 예산 경고 발생", len(res.get("warnings", [])) >= 1)
check("7b S2 무손상(클램프 동작)", out[S2_OFF:S2_OFF + len(S2)] == S2)
check("7c 게이트 통과", res.get("verdict") == "pass")

n_pass = sum(PASS)
print(f"\n{n_pass}/{len(PASS)} 통과")
sys.exit(0 if n_pass == len(PASS) else 1)
