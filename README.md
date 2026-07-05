# retro-kr-patch-mcp

레트로 게임 한글화 자동화 MCP 서버 2종. 갓슈벨 GBA 한글화(v1.9)에서 검증된 파이프라인을 게임 무관 범용 도구로 승격한 것.

## 1. 구성

| 서버 | 파일 | 도구 수 | 역할 |
|---|---|---|---|
| tools | `tools_server.py` | 20 | 번역·검수·패치·대시보드·그래픽 파이프라인 |
| knowledge | `knowledge_server.py` | 7 | 방법론 리소스(strategy 문서 자동 등록)·체크포인트 |
| 공유 라이브러리 | `gfxlib/` (8모듈) | — | GBA 프리미티브·캡처·렌더·재작화·매니페스트 러너 |

## 2. 설치

```bash
python3 -m venv /opt/retro-kr-patch-venv
/opt/retro-kr-patch-venv/bin/pip install "mcp>=1.28" pillow
claude mcp add retro-kr-patch-tools -- /opt/retro-kr-patch-venv/bin/python /path/to/tools_server.py
claude mcp add retro-kr-patch-knowledge -- /opt/retro-kr-patch-venv/bin/python /path/to/knowledge_server.py
```

- 선택 의존: `DEEPSEEK_API_KEY`(번역 검수 — env로만, 코드/설정에 키 저장 금지), `mgba` + `Xvfb` + `xdotool` + `gdb`(라이브 캡처), `ssh root@127.0.0.1`(캡처 fallback).

## 3. 그래픽 파이프라인 (gfx_* 우산 도구 3개)

| 도구 | action | 기능 |
|---|---|---|
| `gfx_capture` | auto / status | 라이브 mgba+GDB 풀덤프 · .ss 오프라인 파싱 · 버스트. 캐시 우선, 3단 fallback(direct spawn → ssh loopback → user_savestate) |
| `gfx_analyze` | report / grid / render / objs / locate / verify | DISPCNT/BG 요약 + 합성 PNG + ROM base 역탐색 + 재작화 판정(native/plugin/blocked) |
| `gfx_build` | region / manifest / status / report / deploy | 단일 rect 재작화(프리뷰) · `krpatch.gfx.json` 체인 빌드+게이트 G1~G5 · 웹에뮬 배포 |

- 신규 화면 1개 한글화 = capture → analyze(report) → build(region) **3콜**. 전체 리빌드 = 1콜.
- 도구 리턴은 소형 JSON만. 이미지·바이트는 전부 파일로.

### 3.1 검증 게이트

| id | 게이트 | pass 기준 |
|---|---|---|
| G1 | source_guard | 엔트리별 written>0 (ROM==VRAM 타일만 기록) |
| G2 | span_bounds | 실변경 스팬 ⊆ 선언 스팬 |
| G3 | determinism | 2회 실행 md5 동일 |
| G4 | region_isolation | 허용 마스크 밖 픽셀 diff 0 |
| G5 | intended_change | 마스크 안 diff ≥ 10px |

- fail 시 산출물 격리(`runs/<id>/rejected.gba`), run report는 `.krpatch/gfx/runs/<ts>.json`.

## 4. 매니페스트 (`krpatch.gfx.json`)

- 게임 특화값(기하·오프셋·팔레트·폰트)은 **전부 매니페스트/플러그인으로 외부화** — 코드에 게임 하드코딩 0.
- 스텝 타입 3종: `artifact`(동결 산출물) / `cmd`(범용 argv, `{acc}/{next}/{version}` 치환, inplace 지원) / `regions`(네이티브 rect 엔진 배치).
- 플러그인 규약: `PLUGIN_API = 1`, `def apply(rom, frames, params, *, preview_dir, dry_run) -> dict`. 러너가 pre/post 바이트 diff로 실변경 스팬을 재계산(자기신고 불신).

## 5. 플랫폼 확장

- 현재 지원: `gba`. 미지원 플랫폼은 명시 오류(침묵 폴백 금지).
- 절단면 3점: `manifest.SUPPORTED_PLATFORMS` / `runner._PLATFORM_MODULES` / `gfxlib/<platform>.py`(어댑터 8함수 — `gfxlib/SPEC.md` 확장 가이드 참조).

## 6. 회귀 기준 (골든 테스트)

1. `.ss` 파싱 = 원본 ss_dump.py 산출 .bin과 md5 동일.
2. 컴포지트 렌더 = 원본 render_ss.py PNG와 픽셀 동일.
3. regions 체인 = 원본 build_ui.sh 신선 재실행 산출과 md5 동일.
4. release 체인 = 실배포 ROM(v1.9)과 md5 비트 동일.

## 7. 보안 규율

- API 키는 env로만. 매니페스트·리포트·로그에 키 무포함.
- 사용자 문자열은 셸에 절대 미포함 — job.json 파일 프로토콜 + 고정 argv.
- 프로세스 정리는 자기 소유 pid만(/proc cmdline 마커 검증). 광역 pkill 금지.
- ROM/게임 자산은 repo에 미포함(.gitignore).
