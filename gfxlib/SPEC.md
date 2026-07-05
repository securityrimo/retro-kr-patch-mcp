# gfxlib — 그래픽 한글화 파이프라인 공유 라이브러리 API 스펙 (v1)

> 구현 규율: 갓슈벨 gfx_re 검증 스크립트에서 **복사-추출, 알고리즘 무변경**.
> 원본 소스는 읽기 전용(`/mnt/synology_devdata/projects/kor-trans/gashbell-gba/` 절대 수정 금지).
> 비트 동일성이 회귀 기준: 같은 입력 → 원본 스크립트와 같은 출력(md5/픽셀).
> 실행 인터프리터: `/opt/retro-kr-patch-venv/bin/python` (3.13.5, pillow 12.2.0 — 시스템 python3와 동일 버전 실측).

## 파일 배치·소유

| 파일 | 내용 | 원본 |
|---|---|---|
| `gba.py` | 하드웨어 프리미티브 | locate_bg/ss_dump/bg_render/redraw_region |
| `render.py` | 컴포지터/렌더러 | render_ss/bg_render/grid_bg/analyze/verify_title |
| `capture.py` | .ss 파서, GDB RSP 클라, frame_dir 기록 | ss_dump/dump_screen |
| `worker.py` | detached 캡처 워커 CLI | dump_screen |
| `text.py` | 폰트/마스크/페인트 프리미티브 | redraw_title/redraw_region/redraw_intro |
| `redraw.py` | rect 엔진 + locate + 플러그인 로더 | redraw_region/locate_bg |
| `manifest.py` | krpatch.gfx.json 로더/검증/치환 | 신규 (dashboard/project.py 패턴) |
| `runner.py` | 체인 실행 + 게이트 G1~G5 + run report | build_ui.sh 시맨틱 |

## 공통 규약

- frame_dir 형식(기존 바이트호환): `io.bin(0x400) palette.bin(0x400) vram.bin(0x18000) oam.bin(0x400)` + 선택 `iwram.bin(0x8000) wram.bin(0x40000)` + `*.png` + **`capture.json`(완료 마커, 임시명→os.rename 원자 기록)**.
- capture.json: `{"mode","rom","rom_md5","keyseq","ss_sha1","boot","backend","started_at","finished_at","regions":{"io":"<sha1>",...},"worker_version"}`
- 모든 공개 함수 리턴은 JSON 직렬화 가능 dict(경로·수치만) 또는 PIL.Image/bytes(내부용). 픽셀/바이트 데이터를 MCP 리턴에 싣지 않음.
- NAS 경로 하드코딩 금지 — 폰트/ROM/frame 경로는 전부 인자.

## gba.py

```python
def load_frame(frame_dir: str) -> dict          # {"io","palette","vram","oam"[,"iwram","wram"]}: bytes
def dispcnt(io: bytes) -> dict                  # {"mode":int,"bg":[bool]*4,"obj":bool,"forced_blank":bool}
def bgcnt(io: bytes, n: int) -> dict            # {"prio","charbase","scrbase","bpp8","size"}  (charbase*0x4000, scrbase*0x800)
def pal_rgb(pal: bytes, idx: int, palno: int = 0, bpp8: bool = False) -> tuple  # RGB555→RGB888 (<<3)
def screen_entries(vram: bytes, scrbase: int, size: int) -> list  # [(tx,ty,tile,palno,hf,vf)] 32/64맵 블록 처리
def dec4(data: bytes, off: int) -> list         # 8x8 [[int]*8]*8
def dec8(data: bytes, off: int) -> list
def enc4(cell: list) -> bytes                   # 32B — redraw_region.py:134-137과 동일 비트 배치
def enc8(cell: list) -> bytes                   # 64B
def flip(cell: list, hf: bool, vf: bool) -> list
```

## render.py

```python
def render_bg(frame: dict, n: int, rom: bytes = None, rom_base: int = None) -> "PIL.Image"
    # 타일 소스: rom+rom_base 지정 시 ROM에서(=verify_title 방식), 아니면 VRAM charbase
def compose(frame: dict, layers: list = None) -> "PIL.Image"
    # mode0 우선순위 컴포지터(render_ss.py 동등). layers=[(bg, rom|None, base|None)] 로 ROM/VRAM 혼합(verify_title 일반화)
def grid_overlay(frame: dict, n: int, rom: bytes = None, rom_base: int = None) -> "PIL.Image"  # 8px 그리드+셀좌표
def obj_list(frame: dict) -> list[dict]         # OAM 파싱(analyze.py)
def obj_sheet(frame: dict) -> "PIL.Image"
```

자가검증: `compose(dump_options)` == 기존 `render_ss.py` PNG (픽셀 동일), `render_bg` == `bg_render.py`.

## capture.py

```python
def parse_ss(path: str) -> dict                 # mGBA .ss PNG래핑 gbAs zlib 0x61000 →
    # io@0x400 palette@0x800 vram@0xC00 oam@0x18C00 iwram@0x19000 wram@0x21000 (ss_dump.py 바이트호환)
def write_frame(out_dir: str, regions: dict, meta: dict) -> str   # .bin들 기록 후 capture.json 원자 기록
class GdbClient:                                 # dump_screen.py RSP 함수 승격
    def __init__(self, port=2345, timeout=...)  # connect 재시도
    def read_mem(self, addr, length) -> bytes   # 0x200 청크
    def halt(self); def resume(self); def regs(self) -> str
REGIONS = (("io",0x04000000,0x400),("palette",0x05000000,0x400),("oam",0x07000000,0x400),("vram",0x06000000,0x18000))
```

자가검증: `parse_ss(out/gashbell_kr_v1.0.ss1)` 산출 .bin == 기존 `ss_dump.py` 산출 md5 전부 동일.

## worker.py — `python3 -m gfxlib.worker --job <job.json>`

- job.json: `{"kind":"live"|"ss"|"burst","rom","out_dir","keyseq","boot":11.0,"freeze_at":0,"burst":0,"burst_int":0.5,"burst_start":0,"ss_path","deadline_s"}`
- live/burst: Xvfb :99(부재 시 기동) + `/usr/games/mgba -3 -g <rom>` + xdotool 키시퀀스(`key[:hold[:wait]]` 콤마구분, dump_screen.py 문법) → GdbClient 덤프 → write_frame. burst는 `bd_NN.png`+`bd_NN_*.bin` 후 대표 프레임 meta 기록.
- 자기 pid만 정리(자기가 띄운 mgba/Xvfb pid를 `<out_dir>/.pids`에 기록, /proc cmdline 검증 후 TERM→KILL). 광역 pkill 금지.
- deadline_s 초과 시 자식 정리 후 `FAILED.json`(사유) 기록. 성공 시 capture.json이 완료 마커.
- stdout 최소화(로그는 `<out_dir>/worker.log`).

## text.py (redraw_title.py에서 시그니처 그대로 승격)

```python
def glyph_mask(text, px, font_path, adv_ratio=1.0, shear=0.0, bold=0) -> (mask, w, h)   # :135
def paint(canvas, mask, mw, mh, gx, gy, fills, outline, outer=None, ol_w=1)             # :167
def inpaint(canvas, x0, y0, x1, y1, ...)   # :108 행최근접(투명0보다 유색 우선) — 원본 시그니처 유지
def bbox(cv) -> (x0,y0,x1,y1)                                                            # redraw_intro.py:74
def transplant(cv, dst_bb, src_bb, src_canvas, lut=None)                                 # redraw_intro.py:85
```

## redraw.py

```python
def locate(frame: dict, bg: int, rom: bytes) -> dict   # {"base":int|None,"ok":int,"checked":int}
    # locate_bg.py: rom.find 다중타일 교차검증, 확정 기준 ok >= max(2, checked//2)
def redraw_rect(rom: bytearray, frame: dict, bg: int, base: int, rect: tuple, text: str, *,
                font_path: str, hi=None, fill=None, dark=None, ol=None, excl: tuple = None,
                margin: float = 0.86, preview_path: str = None, dry_run: bool = False) -> dict
    # redraw_region.py와 비트 동일. rom(누적 bytearray)을 in-place 수정(원본은 baseROM 파일 → 여기선 전달 바이트).
    # 팔레트 히스토그램은 rom 바이트에서 계산(원본과 동일 시맨틱). hi/fill/dark/ol=None이면 자동판정.
    # 리턴: {"ok","cells","tiles","conflicts","palno","chosen":{"hi","fill","dark","ol"},"px","preview"}
def load_plugin(path: str):   # spec_from_file_location, PLUGIN_API==1 확인
    # 플러그인: def apply(rom: bytearray, frames: dict, params: dict, *, preview_dir, dry_run) -> dict
```

자가검증(필수): v1.2 사본에 `redraw_rect(..., rect=(1,4,10,6), text="난이도", hi=6, fill=5, dark=3, ol=1)`
→ 원본 `HI=6 FILL=5 DARK=3 OL=1 python3 redraw_region.py dump_options 1 0x179cc0 "1,4,10,6" "난이도" <out> <v1.2>` 출력과 **md5 동일**.

## manifest.py — krpatch.gfx.json

```json
{"font":"assets/NeoDunggeunmo.ttf","rom":"roms/original.gba","deploy":"/var/www/gba-test/roms/",
 "screens":{"<name>":{"legacy_frames":"analysis/gfx_re/dump_options"} 또는 {"capture":{"method":"live","keyseq":"...","boot":11}}},
 "chains":{"<chain명>":{"base":"out/....gba","out":"out/..._v{version}.gba","steps":[
   {"name":"...","type":"artifact","path":"..."},
   {"name":"...","type":"cmd","cwd":"analysis/gfx_re","inplace":false,"argv":["python3","script.py","...","{next}","{acc}"],"declared_spans":[["0x..","0x.."]]},
   {"name":"...","type":"regions","screen":"options","bg":1,"rom_base":"0x179cc0",
    "items":[{"rect":"1,4,10,6","text":"난이도","hi":6,"fill":5,"dark":3,"ol":1,"excl":null}]}]}}}
```

```python
def load_gfx_config(project_root: str) -> dict    # 검증 + _abs 경로 주입 + 오류 리스트({"errors":[...]})
def resolve_frames(cfg, project_root, screen) -> str   # legacy_frames 우선, 아니면 .krpatch/gfx/frames/<screen>
```

## runner.py

```python
def run_chain(project_root: str, cfg: dict, chain: str, version: str, *, steps: list = None,
              workdir: str = None, determinism: bool = True, preview_only: bool = False) -> dict
```
- acc = bytearray(base 파일). artifact→교체, regions→아이템 순차 redraw_rect(**acc 바이트에서 팔레트 hist 재계산 — build_ui.sh 서브프로세스 체인과 시맨틱 동일**), cmd→acc를 로컬 임시파일로 실체화(`tempfile.mkdtemp(prefix="krgfx-")`, NAS 아님)→argv의 {acc}/{next} 치환 실행→결과 재흡수. inplace=true면 {acc} 파일 직접 수정 후 재흡수.
- 스텝마다 pre/post 바이트 diff → 실변경 스팬 산출(G2).
- 게이트: G1 source_guard(regions: written>0; skipped/blanked는 warn) / G2 span_bounds(declared_spans 있는 스텝+regions 자동 스팬) / G3 determinism(전체 체인 2회 md5 동일, 기본 on) / G4 region_isolation(frame 있는 regions 스텝: before/after compose diff, 허용마스크=rect셀∪실기록셀 타일경계, 밖 픽셀 0) / G5 intended_change(엔트리 마스크 안 diff ≥ 10px).
- verdict fail → out 미기록, `<project>/.krpatch/gfx/runs/<ts>/rejected.gba` 격리. pass → out 기록(기존 파일은 `.bak-<ts>` 백업 선행).
- run report: `<project>/.krpatch/gfx/runs/<ts>.json` — {run_id, chain, version, inputs(md5), steps[], gates{}, verdict, attention[], artifacts{}, output_md5}.
- 리턴은 요약 투영: {run_id, verdict, first_fail, out, md5, counters, attention, report_path}.

## 게이트 요약 리포트 스키마

`{"gates":{"G1":{"pass":bool,"entries":{...}},"G2":{"pass":bool,"violations":[]},"G3":{"pass":bool,"md5":".."},
  "G4":{"pass":bool,"out_of_region_diff_px":0,"offenders":[]},"G5":{"pass":bool,"per_entry_changed_px":{}}},
 "verdict":"pass|pass_with_warnings|fail","first_fail":null|"G4"}`

## 플랫폼 확장 가이드 (어댑터 절단면)

범용성 원칙: **게임 특화값(주소·팔레트 역할·rect·텍스트·keyseq)은 전부 krpatch.gfx.json 매니페스트/인자에, 플랫폼 특화 로직은 `gfxlib/<platform>.py` 어댑터 모듈에** 둔다. 라이브러리·MCP 도구 코드에는 어떤 게임 상수도 두지 않는다(2026-07-05 전수 감사로 실증).

절단면 3점(현재 GBA 단독 등록, 미지원 platform 은 침묵 폴백 없이 명시 오류):

1. `manifest.SUPPORTED_PLATFORMS` — 매니페스트 `platform` 키 검증(로드 시점 거절).
2. `runner._PLATFORM_MODULES` — `{"gba": "gfxlib.gba"}` 매핑. `runner._get_platform()` 이 지연 import(미등록 → ValueError).
3. `gfxlib/<platform>.py` — 프리미티브 모듈(gba.py duck-type).

새 플랫폼(예: snes) 어댑터가 구현해야 할 함수(= gba.py 공개 API, runner/redraw/render 가 이 이름으로 호출):

| 함수 | 역할 | 비고 |
|---|---|---|
| `load_frame(frame_dir) -> dict` | frame_dir → 리전 bytes dict | 리전 구성은 플랫폼 재량, `io`/`palette`/`vram`/`oam` 상당 필요 |
| `dispcnt(io) -> dict` | 활성 레이어/모드 요약 | `{"mode","bg":[bool],"obj","forced_blank"}` 형태 유지 |
| `bgcnt(io, n) -> dict` | 레이어 n 파라미터 | `{"prio","charbase","scrbase","bpp8","size"}` 상당 |
| `pal_rgb(pal, idx, palno, bpp8) -> tuple` | 팔레트 → RGB888 | |
| `screen_entries(vram, scrbase, size) -> list` | 타일맵 엔트리 `[(tx,ty,tile,palno,hf,vf)]` | |
| `dec4/dec8(data, off) -> list` | 타일 디코드(8x8 셀) | 플랫폼 bpp 체계에 맞게 |
| `enc4/enc8(cell) -> bytes` | 타일 인코드 — dec 와 왕복 비트 동일 | rect 엔진 기록 경로 |
| `flip(cell, hf, vf) -> list` | 셀 플립 | |

주의(현재 GBA 결합점 — 어댑터 추가 시 함께 일반화할 대상):

- `capture.py`: `parse_ss`(mGBA .ss 포맷)·`REGIONS`(GBA IO/VRAM 주소)·GDB RSP 는 GBA/mGBA 전용. 타 플랫폼은 별도 캡처 백엔드(에뮬별 세이브스테이트 파서/디버거 클라)를 어댑터 곁에 둔다. frame_dir + `capture.json` 완료 마커 규약은 플랫폼 무관 공통.
- `worker.py`: mgba/xdotool 구동부가 GBA 전용(기본 `/usr/games/mgba` 는 매니페스트 `capture.mgba`/`display` 로 오버라이드 가능). 타 플랫폼은 job.json `kind` 를 확장하거나 별도 워커.
- `render.py`/`redraw.py`: 프리미티브를 전부 어댑터에서 받도록 작성돼 있으나 mode0 우선순위 합성·4bpp rect 엔진 시맨틱은 GBA 기준 — 타 플랫폼은 동등 시맨틱 검증(원본 스크립트 md5/픽셀 회귀) 후 편입.
- `text.py`/`manifest.py`/`runner.py` 게이트(G1~G5): 플랫폼 무관 — 수정 없이 재사용.
