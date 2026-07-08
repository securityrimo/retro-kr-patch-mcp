# GBA 라이브 캡처 운용 — mgba GDB stub · .ss 오프라인 파싱 · 3단 fallback

> 그래픽 한글화용 화면 프레임(io/palette/vram/oam) 확보 전략. 게임 무관 —
> 게임 특화값(키시퀀스·주소)은 전부 프로젝트 `krpatch.gfx.json`에 둔다.

## 1. 라이브 백엔드: mgba -3 -g + GDB stub

| 항목 | 값 | 비고 |
|---|---|---|
| 기동 | `mgba -3 -g <rom>` | `-3` 3배창, `-g` GDB stub 대기 |
| 포트 | 2345 (기본) | 프로젝트당 파일락으로 직렬화 |
| 응답 조건 | **halt(0x03) 상태에서만 m 패킷 응답** | 실행 중 read는 무응답/타임아웃 |
| 청크 | 0x200 바이트/요청 | m 패킷 반복 |
| 풀덤프 소요 | io+palette+oam+vram(0x18000) ≈ **1분** | 폴링 예산에 반영 |
| 프로토콜 | RSP `$<cmd>#<cksum2hex>`, ack `+` | halt=0x03 → settle → drain |

- 순서 철칙: **halt → settle(0.15~0.3s) → drain → read → resume**. drain 생략 시 stop-reply 패킷이 데이터에 섞인다.
- 에뮬 기동 직후 `drain(); cont()` 후 부팅 대기(boot 초) — stub 이 초기 halt 상태로 뜨기 때문.

## 2. 헤드리스 입력: Xvfb + xdotool

1. `Xvfb :99 -screen 0 640x480x24` — 이미 떠 있으면 재사용(소켓 `/tmp/.X11-unix/X99`).
2. `xdotool search --class mgba` 로 창 획득 → `windowfocus`.
3. 키 시퀀스 문법: `key[:hold[:wait]],...` (기본 hold 0.35s / wait 0.8s).

| GBA 버튼 | X 키 | GBA 버튼 | X 키 |
|---|---|---|---|
| A | `x` | B | `z` |
| L | `a` | R | `s` |
| Start | `Return` | Select | `BackSpace` |
| 방향 | `Up/Down/Left/Right` | — | — |

## 3. 버스트 덤프 (애니메이션 프레임 수집)

- 매 간격: **halt(settle 0.15) → 스크린샷 + 전영역 덤프 → resume** 을 N회.
- 산출: `bd_NN.png` + `bd_NN_<region>.bin`, 마지막 프레임이 대표 프레임(capture.json).
- 용도: 스핀/페이드 인트로처럼 특정 프레임에만 원하는 타일맵이 뜨는 장면.

## 4. .ss 세이브스테이트 오프라인 파싱 (에뮬 불필요)

mGBA `.ss` = PNG 래핑, `gbAs` 청크 = zlib 압축 상태(GBA 0x61000 바이트).
헤더 0x400 뒤 메모리 블록 연속:

| 영역 | 오프셋 | 크기 | 영역 | 오프셋 | 크기 |
|---|---|---|---|---|---|
| io | 0x400 | 0x400 | oam | 0x18C00 | 0x400 |
| palette | 0x800 | 0x400 | iwram | 0x19000 | 0x8000 |
| vram | 0xC00 | 0x18000 | wram | 0x21000 | 0x40000 |

- PNG 아니면 원시 상태로 간주(앞 0x61000B). GDB 덤프와 **바이트 호환** — 파이프라인 하류 동일.
- 화면 스크린샷은 .ss PNG 자체를 변환(`screenshot.png`).

## 5. 샌드박스 3단 fallback (gfx_capture 내장)

1. **① MCP direct spawn** — detached 워커(`gfxlib.worker --job job.json`). 3초 내 사망/포트 미오픈이면 ②.
2. **② ssh root@127.0.0.1 loopback** — 고정 argv, 사용자 문자열은 전부 job.json 안(인젝션 봉쇄). 샌드박스가 GUI 프로세스를 SIGKILL 하는 환경 우회.
3. **③ offline .ss** — 둘 다 불가 시 `{ok:false, need:"user_savestate"}`. 사용자가 에뮬에서 해당 화면 .ss 를 만들어 `ss_path` 로 넘기면 §4 로 처리.

- 완료 마커: `frame_dir/capture.json` (임시명→rename 원자 기록). 실패는 `FAILED.json`.
- 캐시 키: live/burst=`rom_md5+mode+keyseq`, ss=`ss_sha1` — 동일 키면 재캡처 생략.
- 정리 규율: 워커는 자기가 띄운 pid 만 `.pids`+cmdline 검증 후 kill. 광역 pkill 금지.

## 6. 워치포인트 → 정적 역추적

- VRAM 쓰기 주체 추적은 GDB 워치포인트로 가능하나, **세이브스테이트 로드가 워치포인트를 무효화**한다.
- 따라서 워치포인트 세션은 **풀부트(전원 on → 목표 화면까지 실입력)만** 유효. 스테이트 로드로 단축 금지.
- 대안: 라이브 불가 환경에서는 정적 역추적 — 캡처 VRAM 타일을 `rom.find` 로 역탐색(locate)해 무압축 원본 base 를 확정.

## 7. MCP 도구 3콜 워크플로

```
gfx_capture(project_dir, screen)            # ① 프레임 확보(캐시 우선, 3단 fallback)
  → gfx_analyze(frame_dir, action="report", base_rom=...)   # ② BG 판정(native/plugin/blocked)
    → gfx_build(project_dir, action="manifest"|"region")    # ③ 재작화+게이트 G1~G5
```

- report 판정: **native**=무압축 역탐색 확정(mode0·4bpp, rect 엔진 직행) / **plugin**=base 확정이나 8bpp·affine(플러그인 필요) / **blocked**=rom.find 실패(압축·동적 생성 — 워치포인트 추적 대상).
- 장시간 캡처는 `pending:true` 반환 → `action="status"` 재폴링.
