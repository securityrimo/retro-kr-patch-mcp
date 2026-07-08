# 번역 검수 대시보드 (ROM 무관)

레트로 한글패치의 번역을 **원문↔번역 좌우 대조 + 스토리(대본)순**으로 사람이
직접 검수·수정하는 로컬 전용 Flask 대시보드. `retro-kr-patch-tools` MCP의
`review_dashboard` 도구로 **활성 프로젝트별로** 기동한다.

## 사용
```
review_dashboard(project_dir="/…/my-rom-project", action="start")
→ {"ok":true,"url":"http://127.0.0.1:PORT", ...}
```
- action: `start` | `stop` | `status` | `restart`
- 프로세스는 프로젝트 `.krpatch/dashboard.pid` 로 **자기소유만** 관리(광역 kill 없음)
- 재부팅 생존이 필요하면 systemd 등록은 별도(수동 기동이 기본)

## 프로젝트 설정 `krpatch.dashboard.json` (프로젝트 루트, 없으면 자동생성)
| 키 | 뜻 | 예 |
|----|----|----|
| `name` | 표시명 | `"갓슈벨"` |
| `translations_json` | 편집대상 JSON(단일 원본) | `translations/extracted_texts_v2.json` |
| `rom` | 원본 ROM(스토리순·글리프검증) | `roms/original.gba` |
| `codec_module` | 코덱 모듈(선택) | `tools/kr_codec.py` |
| `build_cmd` | 리빌드 명령(선택) | `["tools/07_build_kr.py"]` |
| `port` | 포트 | `5057` |
| `story_order` | 기본 대본 정렬 | `offset` \| `pointer` \| `file` |
| `pointer_base/size/step` | 포인터 스캔(포인터형 ROM) | GBA=`134217728`(0x08000000)/4/4 |

## 뷰
- **대본(스토리순)**: 화자 이름표(짧고 부호 없이 개행종료·≤4자)+본문을 묶어
  시나리오 대본처럼. 라인마다 원문 병기 → 문맥·오역 대조. 장면 경계=offset 급간격.
- **표**: offset·원문·편집 3열, 필터(미번역/JP잔류/불일치/재검토/수동/예산초과).

## 스토리 순서 재정렬 (`story_order.py`) — ROM 무관
- `offset`: 파일 offset 순. **인라인-바이트코드형**(대사가 명령 스트림에 끼워진
  ROM, 예: 갓슈벨) 은 offset ≈ 실행순.
- `pointer`: ROM 전역을 훑어 각 문자열을 겨냥하는 최소 포인터 위치순 정렬.
  **포인터-테이블형**(대사 뱅크+포인터 참조, 다수 JRPG) 에 적합. 미참조는 offset순 뒤로.
- `file`: 추출 원본 순서.
- ⚠ **한계**: 진짜 분기(전투 분기·조건부 이벤트)까지 따라가려면 ROM별 스크립트
  VM 디스어셈블이 필요하며 범용 불가. 이 모듈은 두 근사(offset/pointer)+장면 그룹핑까지.

## 코덱 인터페이스(duck-type, 있는 것만 사용)
`load_table(rom)->(_,_,first_idx)` · `encoded_len(text,first_idx)->(n,bad)` ·
`split_term(kr)->(body,term)` · `render_glyph(ch)->bytes` ·
`sjis_code(ch,first_idx)` · `FW: dict`
코덱 없으면 글리프 미리보기·바이트예산을 숨기고 텍스트 전용으로 강등한다.
