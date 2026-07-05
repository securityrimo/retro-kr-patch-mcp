#!/usr/bin/env python3
"""krpatch.gfx.json 로더/검증/치환 — 그래픽 체인 매니페스트 (프로젝트/게임 무관).

프로젝트 루트의 ``krpatch.gfx.json`` 을 읽어 폰트·ROM·화면(frame 소스)·
체인(steps) 정의를 해석한다. dashboard/project.py 의 load_config/_abs 주입
패턴을 따르되, 오류를 예외가 아니라 ``cfg["errors"]`` 리스트로 누적해
호출측(runner/MCP)이 통째로 보고할 수 있게 한다.

스키마(요약):
{
  "platform": "gba",                       # 선택, 기본 "gba"
  "font": "assets/....ttf",
  "rom": "roms/original.gba",
  "deploy": "/var/www/.../roms/",          # 선택
  "screens": {"<name>": {"legacy_frames": "..."} 또는 {"capture": {...}}},
  "chains": {"<chain명>": {"base": "...", "out": "..._v{version}.gba",
    "steps": [
      {"name": "...", "type": "artifact", "path": "..."},
      {"name": "...", "type": "cmd", "cwd": "...", "inplace": false,
       "argv": ["python3", "s.py", "{next}", "{acc}"],
       "declared_spans": [["0x3cd70", "0x3d000"]]},
      {"name": "...", "type": "regions", "screen": "options", "bg": 1,
       "rom_base": "0x......",
       "items": [{"rect": "1,4,10,6", "text": "...",
                  "hi": 6, "fill": 5, "dark": 3, "ol": 1, "excl": null}]}
    ]}}
}

게임 특화값(주소·팔레트 역할·rect·텍스트)은 전부 이 매니페스트에 있고,
라이브러리 코드에는 어떤 게임 상수도 없다.
"""
import json
import os

CONFIG_NAME = "krpatch.gfx.json"

# ── 플랫폼 어댑터 레지스트리 ─────────────────────────────────────────
# 향후 어댑터 추가 절단면:
#   1) 여기 SUPPORTED_PLATFORMS 에 플랫폼 키를 추가하고
#   2) gfxlib/<platform>.py 프리미티브 모듈(gba.py 대응: load_frame/bgcnt/
#      screen_entries/enc·dec 등 동일 duck-type)을 구현한 뒤
#   3) runner._get_platform() 의 모듈 매핑에 등록한다.
# 미지원 플랫폼은 침묵 폴백 없이 명시 오류로 거절한다.
SUPPORTED_PLATFORMS = {"gba"}

STEP_TYPES = {"artifact", "cmd", "regions"}


def parse_rect(v):
    """rect 표기("x0,y0,x1,y1" 문자열 또는 4-int 시퀀스) → (x0,y0,x1,y1) 튜플.

    셀 좌표는 포함(inclusive) 사각. 형식 오류면 ValueError.
    """
    if isinstance(v, str):
        parts = [p.strip() for p in v.split(",")]
    elif isinstance(v, (list, tuple)):
        parts = list(v)
    else:
        raise ValueError(f"rect 형식 아님: {v!r}")
    if len(parts) != 4:
        raise ValueError(f"rect 는 4개 값 필요: {v!r}")
    try:
        x0, y0, x1, y1 = (int(p) for p in parts)
    except (TypeError, ValueError):
        raise ValueError(f"rect 정수 변환 실패: {v!r}")
    if x0 < 0 or y0 < 0 or x1 < x0 or y1 < y0:
        raise ValueError(f"rect 좌표 불량(x0<=x1, y0<=y1, 음수 금지): {v!r}")
    return (x0, y0, x1, y1)


def parse_int(v):
    """정수 또는 "0x.." 16진 문자열 → int. 실패 시 ValueError."""
    if isinstance(v, bool):
        raise ValueError(f"정수 아님: {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        return int(v.strip(), 0)
    raise ValueError(f"정수 아님: {v!r}")


def _validate_step(step, where, errors):
    """스텝 1개 스키마 검증 + 파생값(_rect/_rom_base/_declared_spans) 주입."""
    if not isinstance(step, dict):
        errors.append(f"{where}: 스텝은 객체여야 함")
        return
    t = step.get("type")
    if t not in STEP_TYPES:
        errors.append(f"{where}: type 불량({t!r}) — 지원: {sorted(STEP_TYPES)}")
        return
    if not step.get("name"):
        errors.append(f"{where}: name 누락")

    if t == "artifact":
        if not step.get("path"):
            errors.append(f"{where}: artifact 스텝에 path 누락")

    elif t == "cmd":
        argv = step.get("argv")
        if not isinstance(argv, list) or not argv or \
                not all(isinstance(a, str) for a in argv):
            errors.append(f"{where}: cmd 스텝의 argv 는 비어있지 않은 문자열 리스트여야 함")
        spans = step.get("declared_spans")
        if spans is not None:
            parsed = []
            if not isinstance(spans, list):
                errors.append(f"{where}: declared_spans 는 리스트여야 함")
            else:
                for k, sp in enumerate(spans):
                    try:
                        if not isinstance(sp, (list, tuple)) or len(sp) != 2:
                            raise ValueError("2-요소 [start,end] 아님")
                        s, e = parse_int(sp[0]), parse_int(sp[1])
                        if e <= s:
                            raise ValueError("end<=start")
                        parsed.append((s, e))
                    except ValueError as ex:
                        errors.append(f"{where}.declared_spans[{k}]: {ex}")
            step["_declared_spans"] = parsed

    elif t == "regions":
        if not step.get("screen"):
            errors.append(f"{where}: regions 스텝에 screen 누락")
        if "bg" not in step:
            errors.append(f"{where}: regions 스텝에 bg 누락")
        else:
            try:
                step["_bg"] = parse_int(step["bg"])
            except ValueError as ex:
                errors.append(f"{where}.bg: {ex}")
        if "rom_base" not in step:
            errors.append(f"{where}: regions 스텝에 rom_base 누락")
        else:
            try:
                step["_rom_base"] = parse_int(step["rom_base"])
            except ValueError as ex:
                errors.append(f"{where}.rom_base: {ex}")
        items = step.get("items")
        if not isinstance(items, list) or not items:
            errors.append(f"{where}: regions 스텝의 items 는 비어있지 않은 리스트여야 함")
            return
        for k, it in enumerate(items):
            iw = f"{where}.items[{k}]"
            if not isinstance(it, dict):
                errors.append(f"{iw}: 아이템은 객체여야 함")
                continue
            if "rect" not in it:
                errors.append(f"{iw}: rect 누락")
            else:
                try:
                    it["_rect"] = parse_rect(it["rect"])
                except ValueError as ex:
                    errors.append(f"{iw}.rect: {ex}")
            if not it.get("text"):
                errors.append(f"{iw}: text 누락")
            if it.get("excl") is not None:
                try:
                    it["_excl"] = parse_rect(it["excl"])
                except ValueError as ex:
                    errors.append(f"{iw}.excl: {ex}")


def load_gfx_config(project_root: str) -> dict:
    """krpatch.gfx.json 로드 + 스키마 검증 + _abs 경로 주입.

    리턴: 설정 dict. 항상 ``errors`` 키(리스트) 포함 — 비어있으면 유효.
    파일 부재/JSON 파손도 예외 대신 ``{"errors": [...]}`` 로 보고한다.
    경로 해석은 project.py 패턴: os.path.join(root, p) — p 가 절대경로면 그대로.
    """
    root = os.path.abspath(project_root)
    path = os.path.join(root, CONFIG_NAME)
    if not os.path.exists(path):
        return {"errors": [f"{CONFIG_NAME} 없음: {path}"]}
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as ex:
        return {"errors": [f"{CONFIG_NAME} 파싱 실패: {ex}"]}
    if not isinstance(cfg, dict):
        return {"errors": [f"{CONFIG_NAME} 최상위는 객체여야 함"]}

    errors = []
    cfg["_root"] = root

    # platform — 미지원이면 침묵 폴백 금지, 명시 오류(레지스트리 대조)
    platform = cfg.setdefault("platform", "gba")
    if platform not in SUPPORTED_PLATFORMS:
        errors.append(
            f"platform '{platform}' 어댑터 없음 — 현재 지원: "
            + ", ".join(sorted(SUPPORTED_PLATFORMS)))

    # 상위 경로 _abs 주입 + 존재 검사(선언했으면 있어야 함)
    for k in ("font", "rom"):
        if cfg.get(k):
            p = os.path.join(root, cfg[k])
            cfg[k + "_abs"] = p
            if not os.path.exists(p):
                errors.append(f"{k} 파일 없음: {p}")

    screens = cfg.get("screens", {})
    if not isinstance(screens, dict):
        errors.append("screens 는 객체여야 함")
        screens = {}
    for sname, sc in screens.items():
        if not isinstance(sc, dict):
            errors.append(f"screens.{sname}: 객체여야 함")
            continue
        if sc.get("legacy_frames"):
            sc["legacy_frames_abs"] = os.path.join(root, sc["legacy_frames"])

    chains = cfg.get("chains", {})
    if not isinstance(chains, dict) or not chains:
        errors.append("chains 누락 또는 비어있음")
        chains = {}
    for cname, ch in chains.items():
        cw = f"chains.{cname}"
        if not isinstance(ch, dict):
            errors.append(f"{cw}: 객체여야 함")
            continue
        if not ch.get("base"):
            errors.append(f"{cw}: base 누락")
        else:
            ch["_base_abs"] = os.path.join(root, ch["base"])
        if not ch.get("out"):
            errors.append(f"{cw}: out 누락")
        else:
            ch["_out_template_abs"] = os.path.join(root, ch["out"])
        steps = ch.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{cw}: steps 누락 또는 비어있음")
            continue
        seen = set()
        for i, step in enumerate(steps):
            _validate_step(step, f"{cw}.steps[{i}]", errors)
            n = step.get("name") if isinstance(step, dict) else None
            if n:
                if n in seen:
                    errors.append(f"{cw}.steps[{i}]: name 중복 '{n}'")
                seen.add(n)
        # regions 스텝의 screen 참조 검증
        for i, step in enumerate(steps):
            if isinstance(step, dict) and step.get("type") == "regions":
                scr = step.get("screen")
                if scr and scr not in screens:
                    errors.append(
                        f"{cw}.steps[{i}]: screen '{scr}' 이 screens 에 없음")

    cfg["errors"] = errors
    return cfg


def resolve_frames(cfg: dict, project_root: str, screen: str) -> str:
    """화면 이름 → frame_dir 절대경로.

    screens.<screen>.legacy_frames 가 있으면 그것(기존 프로젝트의 dump
    디렉터리 재사용), 아니면 프로젝트 상태 규약인 ``.krpatch/gfx/frames/<screen>``.
    디렉터리 존재 여부는 검사하지 않는다(캡처 전 경로 산출 용도 겸용).
    """
    root = os.path.abspath(project_root)
    sc = (cfg.get("screens") or {}).get(screen) or {}
    if sc.get("legacy_frames"):
        return sc.get("legacy_frames_abs") or os.path.join(root, sc["legacy_frames"])
    return os.path.join(root, ".krpatch", "gfx", "frames", screen)
