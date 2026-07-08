# 그래픽 재작화 레시피 — 출처가드 · 폰트 프리미티브 · 인젝션 철칙 · 플러그인 규약

> rect 엔진(native)으로 안 되는 화면(8bpp·affine·합성 로고 등)을 다루는 실전 패턴.
> 게임 무관 — 주소·팔레트 역할·rect 는 전부 매니페스트/인자. 갓슈벨 수치는 예시일 뿐.

## 1. 출처가드 (source guard) — 필수 패턴

- 원칙: **ROM 바이트 == 캡처 VRAM 바이트인 타일만 기록**한다.
- 이유: locate 로 base 를 확정해도, 런타임에 코드가 덧그린 타일(동적 합성)은 ROM 원본과 다르다. 그 위치에 쓰면 다른 화면이 깨진다.
- 구현: 타일별로 `rom[base+t*tsz : +tsz] == vram[charbase+t*tsz : +tsz]` 검사 → 불일치 타일은 skip 하고 리포트에 `skipped` 로 남긴다(침묵 금지).
- 게이트 연동: 러너 G1 이 `written>0` 을 요구하고 skipped 는 warn — 전량 skip 이면 base 오인이다.

## 2. 공유타일 충돌 → 블랭크

- 같은 타일 인덱스를 rect 안 여러 셀이 다른 글자로 요구하면 **충돌**.
- 처방: 충돌 타일은 재사용하지 말고 **블랭크(공백) 처리 후 별도 타일에 재배치**하거나, rect 를 쪼개 충돌 셀을 `excl` 로 제외한다.
- rect 엔진 리턴의 `conflicts` 카운터가 0 이 아니면 결과를 신뢰하지 말 것.

## 3. 폰트 프리미티브 사용법 (gfxlib.text)

| 함수 | 역할 | 실전 파라미터 예시 |
|---|---|---|
| `glyph_mask(text, px, font_path, adv_ratio, shear, bold)` | 비트 마스크 렌더 | 타이틀 로고: `px=30, adv_ratio=0.92, shear=0.10, bold=1` |
| `paint(canvas, mask, mw, mh, gx, gy, fills, outline, outer, ol_w)` | 채움+외곽선 페인트 | 그라데이션 fills 리스트 + `ol_w=2` 이중 외곽 |
| `mark_erase(...)` → `inpaint(canvas, x0,y0,x1,y1)` | 기존 글자 소거 + 행최근접 배경 복원 | 소거 영역 누적 후 inpaint 1회 |
| `bbox(cv)` / `transplant(cv, dst_bb, src_bb, src_canvas, lut)` | 원본 로고 영역 계측/이식 | 최근접 리샘플, `lut=None`=항등 |

- 순서: 원본 캔버스 확보 → `mark_erase`+`inpaint` 로 원문 제거 → `glyph_mask`+`paint` 로 한글 페인트 → 타일 재인코딩(enc4/enc8).
- 마스크는 이진(>127) — 안티앨리어싱 없음이 정상(팔레트 인덱스 기록이므로).

## 4. crisp 폰트 렌더 (ppem 결정)

- 증상: 특정 px 에서 획이 뭉개져 오독(예: "서"→"시").
- 원인: 비트맵계 TTF 는 좌표가 격자에 정렬돼 있어 **정수 배율 ppem 에서만 crisp**.
- 공식: `crisp ppem = unitsPerEm ÷ (글리프 좌표 그리드의 gcd)` — fontTools 로 좌표 gcd 를 구해 검증.
- 필수: PIL `draw.fontmode = "1"`(안티앨리어싱 off). 임의 px 로 내려 그리지 말고 crisp ppem 으로 그린 뒤 필요 시 마스크를 다운스케일.

## 5. 인젝션 철칙 (ROM 기록)

1. **스팬 꽉채움**: 확보한 스팬(구간)은 정확히 그 길이만큼 채운다 — 남는 꼬리를 방치하면 잔상 바이트가 다른 해석을 만든다.
2. **0x00 종단 보존**: 종단/패딩 바이트(0x00)를 밀어내지 않는다. 텍스트·타일 스트림의 종단 시맨틱은 엔진이 소비한다.
3. **END 경계 불침범**: 선언 스팬의 끝(END)을 1바이트도 넘지 않는다. 러너 G2(span_bounds)가 declared_spans 밖 실변경을 잡아 fail 시킨다.
4. 모든 기록은 게이트 경유(runner) 또는 최소 preview(dry_run) 선행 — ROM 직기록 후 육안 확인은 금지 순서다.

## 6. 팔레트 역할 자동판정과 수동 오버라이드

- 원리: rect 안 셀들이 쓰는 팔레트 뱅크(palno 최빈값)에서 **ROM 바이트 히스토그램**으로 역할을 추정 — 최다 사용=fill(본문), 밝음=hi(하이라이트), 어두움=dark(그림자), 외곽=ol.
- 자동판정을 쓰는 경우: 단색 본문+외곽 구조의 일반 메뉴 텍스트.
- **수동 오버라이드가 필요한 시점**: ①그라데이션/장식 팔레트라 히스토그램 최빈이 본문이 아닐 때 ②같은 뱅크를 여러 UI 요소가 공유할 때 ③프리뷰에서 색이 뒤집혀 보일 때. 매니페스트 items 에 `"hi":6,"fill":5,"dark":3,"ol":1` 처럼 고정한다(예시 수치).
- 판정 결과는 리턴 `chosen` 으로 노출 — 오버라이드 값과 다르면 원인 조사(팔레트가 프레임마다 다른 화면일 수 있음).

## 7. PLUGIN_API=1 규약 (rect 엔진 범위 밖 화면)

```python
PLUGIN_API = 1

def apply(rom: bytearray, frames: dict, params: dict, *,
          preview_dir: str, dry_run: bool) -> dict:
    """rom 을 in-place 수정. frames={"<screen>": frame dict}, params=매니페스트 전달값.
    리턴 report(dict): {"ok", "written", "skipped", "spans": [[s,e],...], ...}"""
```

- 로드: `gfxlib.redraw.load_plugin(path)` — `PLUGIN_API == 1` 확인 후 `apply` 반환.
- 규율: 출처가드(§1) 내장 필수, `dry_run=True` 면 ROM 무수정+프리뷰만, 변경 스팬을 report 로 신고(G2 대조).
- 매니페스트 등록: 플러그인 실행은 `cmd` 스텝으로 감싼다(체인 시맨틱 동일).

```json
{"name": "title-logo", "type": "cmd", "cwd": "plugins",
 "argv": ["python3", "run_plugin.py", "title_logo.py", "{acc}", "{next}"],
 "declared_spans": [["0x1c8d5c", "0x1cc75c"]]}
```

- 단순 rect 는 플러그인 말고 `regions` 스텝(엔진 내장)이 정본 — 플러그인은 8bpp·affine·다중 BG 합성·OBJ 재배치처럼 rect 엔진이 못 하는 것만.
