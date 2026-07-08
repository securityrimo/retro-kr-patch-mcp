# 라이브 검증 필수 함정 3종 (2026-07-06 Hikaru no Go 2 실전 교훈)

주입 코드를 짜기 전에 이 문서를 읽어라. 아래 3개 함정은 전부 "ROM 데이터는 완벽한데
화면은 엉망"인 상태를 만들며, 정적 분석만으로는 발견할 수 없다.
**모든 검증은 retro-kr-patch-tools MCP의 도구 호출 순서로 해결된다 — 직접 코드를 짜지 마라.**

## 함정 1: 폰트 베이스 오프셋 오진 (글리프 시프트)

- 증상: 한글 글리프를 주입했는데 화면에 **무관한 한글/한자가 뒤죽박죽**으로 나온다.
- 원인: RE로 찾은 글리프 베이스와 렌더러의 실효 베이스가 상수 글리프 수만큼
  어긋남. Hikaru2 실측 = 렌더러가 `font[TAB_gi + 223]`을 그림
  (베이스 0x7F003C가 아니라 0x7F3EF4가 실효).
- 절차 (주입 **전** 의무):
  1. `font_base_probe(rom, table_offset, entries, glyph_base, test_chars="早碁局", shifts=...)`
  2. 생성된 시트 PNG를 Read로 열어 test_chars와 모양이 일치하는 행의 shift 채택
  3. `effective_base = glyph_base + shift × bytes_per_glyph` 를 이후 모든 주입에 사용
- 이미 주입해서 깨진 경우: 화면 스크린샷의 깨진 글자와 기대 문장을
  `diagnose_glyph_shift(...)`에 넣으면 상수 시프트를 자동 판정한다.

## 함정 2: 문자열 추출 경계 누락 (제어코드 분절)

- 증상: 대사가 **일본어+한글 혼재**로 나온다. 앞부분은 일본어, 뒷부분은 한글.
- 원인: 추출기가 이름코드(①②=0x8740/41)·숫자코드(③~⑥) 같은 제어코드를
  문자열 경계로 오인 → 문장 앞부분이 추출/번역에서 통째로 빠짐
  (Hikaru2에서 616조각 13.6KB).
- 절차 (주입 **후** 의무):
  1. `coverage_gap_scan(original_rom, patched_rom, region_start, region_end)`
  2. 결과 JSON의 `jp_untranslated`를 번역해 각 항목에 `ko` 필드 추가
     (①②↓③~⑥은 그대로 보존, 인명은 기존 번역 용어집과 통일)
  3. `inject_budgeted_text(...)` 로 주입 (예산 자동 처리)
  4. gaps의 실텍스트가 0이 될 때까지 1~3 반복. `・・・`/`！？` 등
     구두점-전용 세그먼트는 언어중립이라 남겨도 된다.

## 함정 3: 화면 실측 없는 "완료" 선언

- 증상: "ROM 검증 5534/5534 통과"인데 사용자는 "여전히 엉망"이라고 한다.
- 원인: ROM 바이트 검증은 함정 1·2를 못 잡는다. 렌더러가 어디를 읽고
  어떤 글리프를 쓰는지는 **화면**만이 정본이다.
- 절차 (배포 **전** 의무): emucap-control(mGBA Lua 어댑터)로 자율 주행 검증
  1. `launch(content_path=패치롬)` → 5~8초 대기 → `status` connected 확인
  2. `run_frames(300)` → `tap(["Start"])`/`tap(["A"])` 로 대사 화면까지 진행
     (tap의 after_frames로 입력+대기 한 콜에)
  3. `screenshot()` 결과를 기대 번역문과 문장 단위로 대조 — 최소 3대사
  4. 불일치 시: 함정 1이면 diagnose_glyph_shift, 함정 2이면 coverage_gap_scan
- 브라우저 검증 병행 시: EmulatorJS 캔버스는 preserveDrawingBuffer=false라
  canvas 복사 캡처가 빈 화면이 된다 — `EJS_emulator.gameManager.screenshot()`
  API를 쓸 것. OCR은 픽셀 폰트에 무용지물이니 금지, 이미지를 직접 봐라.

## 권장 전체 순서 (저능력 에이전트용 체크리스트)

```
1. scan_strings / 기존 추출물 확보
2. font_base_probe        → 실효 베이스 확정   [함정 1 예방]
3. (번역) + 주입 스크립트 or inject_budgeted_text
4. coverage_gap_scan      → 잔존 일본어 0 확인 [함정 2 예방]
5. emucap 자율주행 3대사 화면 대조              [함정 3 예방]
6. build_patch로 배포판 생성
```
