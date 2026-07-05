#!/usr/bin/env python3
"""그래픽 체인 실행기 — build_ui.sh 시맨틱 재현 + 게이트 G1~G5 + run report.

build_ui.sh 의 `mktemp acc/o.gba → mv` 체이닝을 라이브러리로 재현한다:
  acc = bytearray(base ROM)
  artifact → 파일 통째 교체
  regions  → 아이템 순차 gfxlib.redraw.redraw_rect (acc 바이트에서 팔레트
             hist 재계산 — 원본의 서브프로세스 체인과 시맨틱 동일)
  cmd      → acc 를 로컬 임시디렉터리(tempfile.mkdtemp(prefix="krgfx-"),
             NAS 아님)에 실체화 → argv 의 {acc}/{next}/{root}/{version}
             치환 → subprocess 실행(스텝당 timeout 300s) → 재흡수

게이트:
  G1 source_guard    — regions 아이템별 written>0 (skipped/blanked 는 warn)
  G2 span_bounds     — 스텝별 pre/post 바이트 diff(연속 구간 병합)가 선언
                       스팬(cmd: declared_spans, regions: rect 참조 타일
                       자동 스팬) 안인지
  G3 determinism     — 전체 체인 2회 실행 최종 md5 동일(determinism=False 로 off)
  G4 region_isolation— regions 스텝 frame 기준 before/after compose 재구성
                       (해당 BG를 ROM 소스로), 허용 마스크(rect 셀∪실기록 셀,
                       타일 경계) 밖 diff 픽셀 0
  G5 intended_change — 엔트리 rect 마스크 안 diff ≥ 10px

verdict fail → out 미기록, `<project>/.krpatch/gfx/runs/<ts>/rejected.gba` 격리.
pass → out 기록(기존 파일은 `.bak-<ts>` 백업 선행).
run report → `<project>/.krpatch/gfx/runs/<ts>.json`.

게임 특화값은 전부 매니페스트(krpatch.gfx.json)에서 오고, 이 모듈에는
어떤 게임 상수·경로도 없다.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from . import manifest as manifest_mod

CMD_TIMEOUT_S = 300          # cmd 스텝당 subprocess 제한
G5_MIN_PX = 10               # 의도 변경 최소 diff 픽셀
SPAN_REPORT_CAP = 200        # 리포트에 싣는 스팬 최대 개수
OFFENDER_CAP = 50            # G4 위반 셀 나열 상한
TAIL_CAP = 2000              # cmd stdout/stderr 리포트 tail 길이


# ── 지연 import 게터(플랫폼 어댑터 절단면 + 테스트 스텁 주입점) ──────
# 향후 플랫폼 추가 시: manifest.SUPPORTED_PLATFORMS 등록 후 여기 매핑에
# "snes": "gfxlib.snes" 식으로 어댑터 모듈을 잇는다. 미지원 플랫폼은
# manifest 로더가 이미 명시 오류로 거절한다.
_PLATFORM_MODULES = {"gba": "gfxlib.gba"}


def _get_platform(platform: str):
    """플랫폼 프리미티브 모듈(load_frame/bgcnt/screen_entries...)을 반환."""
    import importlib
    modname = _PLATFORM_MODULES.get(platform)
    if not modname:
        raise ValueError(f"platform '{platform}' 어댑터 없음 — 현재 지원: "
                         + ", ".join(sorted(_PLATFORM_MODULES)))
    return importlib.import_module(modname)


def _get_redraw():
    """rect 엔진 모듈(테스트에서 monkeypatch 하는 주입점)."""
    from . import redraw
    return redraw


def _get_render():
    """컴포지터 모듈(테스트에서 monkeypatch 하는 주입점)."""
    from . import render
    return render


# ── 순수 유틸 ────────────────────────────────────────────────────────

def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _utcnow_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _diff_spans(pre: bytes, post: bytes, chunk: int = 4096) -> list:
    """pre/post 바이트열의 실변경 스팬 [(start, end_exclusive)] 산출.

    연속 구간은 병합(청크 경계 넘는 run 포함). 길이가 다르면 공통 길이
    이후 전체를 하나의 변경 스팬으로 취급한다.
    """
    spans = []
    n = min(len(pre), len(post))
    i = 0
    while i < n:
        j = min(i + chunk, n)
        if pre[i:j] != post[i:j]:
            k = i
            while k < j:
                if pre[k] != post[k]:
                    s = k
                    while k < j and pre[k] != post[k]:
                        k += 1
                    if spans and spans[-1][1] == s:
                        spans[-1][1] = k
                    else:
                        spans.append([s, k])
                else:
                    k += 1
        i = j
    if len(pre) != len(post):
        s, e = n, max(len(pre), len(post))
        if spans and spans[-1][1] == s:
            spans[-1][1] = e
        else:
            spans.append([s, e])
    return [tuple(sp) for sp in spans]


def _merge_spans(spans) -> list:
    """겹치거나 맞닿은 스팬 병합(정렬 포함)."""
    out = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]


def _uncovered(spans, allowed) -> list:
    """allowed(병합된 스팬들) 어디에도 완전 포함되지 않는 스팬 목록."""
    bad = []
    for s, e in spans:
        if not any(a <= s and e <= b for a, b in allowed):
            bad.append((s, e))
    return bad


def _hex_spans(spans, cap: int = SPAN_REPORT_CAP) -> list:
    return [[hex(s), hex(e)] for s, e in spans[:cap]]


def _rect_cells(rect, excl=None) -> set:
    """포함 사각 rect 의 (tx,ty) 셀 집합. excl 사각 안 셀은 제외."""
    x0, y0, x1, y1 = rect
    cells = set()
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            if excl and excl[0] <= tx <= excl[2] and excl[1] <= ty <= excl[3]:
                continue
            cells.add((tx, ty))
    return cells


def _substitute(argv, mapping) -> list:
    out = []
    for a in argv:
        for k, v in mapping.items():
            a = a.replace("{" + k + "}", v)
        out.append(a)
    return out


# ── regions 기하(frame → 셀/타일/허용스팬) ──────────────────────────

def _regions_geometry(plat, frame, bg, base, items):
    """frame 스크린맵에서 rect 셀·참조 타일·G2 허용 스팬을 산출.

    redraw_region 시맨틱과 동일하게 타일 0(빈 타일)은 제외한다.
    리턴: {"tsize", "entries", "rect_cells", "rect_tiles", "allowed_spans"}
    """
    bc = plat.bgcnt(frame["io"], bg)
    tsize = 64 if bc["bpp8"] else 32
    entries = plat.screen_entries(frame["vram"], bc["scrbase"], bc["size"])
    rect_cells = set()
    for it in items:
        rect_cells |= _rect_cells(it["_rect"], it.get("_excl"))
    rect_tiles = {t for (tx, ty, t, pn, hf, vf) in entries
                  if (tx, ty) in rect_cells and t}
    allowed = _merge_spans(
        [(base + t * tsize, base + (t + 1) * tsize) for t in rect_tiles])
    return {"tsize": tsize, "entries": entries, "rect_cells": rect_cells,
            "rect_tiles": rect_tiles, "allowed_spans": allowed}


def _written_cells(geom, base, spans) -> set:
    """이 스텝의 실변경 스팬 → 실제 기록된 타일 → 그 타일을 참조하는 셀 전부."""
    tsize = geom["tsize"]
    written_tiles = set()
    for s, e in spans:
        if e <= base:
            continue
        t0 = max(0, (s - base) // tsize)
        t1 = (max(s, e - 1) - base) // tsize
        written_tiles.update(range(t0, t1 + 1))
    return {(tx, ty) for (tx, ty, t, pn, hf, vf) in geom["entries"]
            if t and t in written_tiles}


def _diff_pixels(img_a, img_b):
    """두 PIL 이미지의 픽셀 diff 좌표 리스트 [(x,y)] (RGB 비교)."""
    a = img_a.convert("RGB")
    b = img_b.convert("RGB")
    w = min(a.size[0], b.size[0])
    h = min(a.size[1], b.size[1])
    da = list(a.getdata())
    db = list(b.getdata())
    out = []
    for y in range(h):
        ra = y * a.size[0]
        rb = y * b.size[0]
        for x in range(w):
            if da[ra + x] != db[rb + x]:
                out.append((x, y))
    return out


# ── 체인 실행(1회분) ─────────────────────────────────────────────────

class _StepError(Exception):
    pass


def _exec_chain(root, cfg, ch, version, step_filter, wd, *,
                collect=None, previews_dir=None):
    """체인 1회 실행. acc(bytearray) 반환. collect 가 있으면 게이트 원자료 축적.

    collect: {"steps": [], "g1": {}, "g2": {...}, "g4": {...}, "g5": {...},
              "attention": [], "warnings": int}
    """
    base_path = ch["_base_abs"]
    if not os.path.exists(base_path):
        raise _StepError(f"base 없음: {base_path}")
    with open(base_path, "rb") as f:
        acc = bytearray(f.read())

    steps = ch["steps"]
    if step_filter:
        names = [s.get("name") for s in steps]
        missing = [n for n in step_filter if n not in names]
        if missing:
            raise _StepError(f"steps 필터에 없는 스텝: {missing}")
        steps = [s for s in steps if s.get("name") in step_filter]
        if not steps:
            raise _StepError("steps 필터 결과가 비어있음")

    plat = None
    for idx, step in enumerate(steps):
        name = step.get("name") or f"step{idx}"
        stype = step["type"]
        pre = bytes(acc)
        t0 = time.time()
        srep = {"name": name, "type": stype}

        if stype == "artifact":
            path = os.path.join(root, step["path"])
            if not os.path.exists(path):
                raise _StepError(f"[{name}] artifact 없음: {path}")
            with open(path, "rb") as f:
                acc = bytearray(f.read())
            srep["path"] = path

        elif stype == "cmd":
            acc_path = os.path.join(wd, "acc.gba")
            next_path = os.path.join(wd, f"next_{idx:02d}.gba")
            with open(acc_path, "wb") as f:
                f.write(acc)
            if os.path.exists(next_path):
                os.unlink(next_path)
            mapping = {"acc": acc_path, "next": next_path,
                       "root": root, "version": version}
            argv = _substitute(step["argv"], mapping)
            cwd = os.path.join(root, step.get("cwd") or ".")
            try:
                proc = subprocess.run(
                    argv, cwd=cwd, capture_output=True, text=True,
                    timeout=CMD_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                raise _StepError(f"[{name}] cmd timeout {CMD_TIMEOUT_S}s: {argv}")
            except OSError as ex:
                raise _StepError(f"[{name}] cmd 실행 실패: {ex}")
            srep.update({"argv": argv, "rc": proc.returncode,
                         "stdout_tail": proc.stdout[-TAIL_CAP:],
                         "stderr_tail": proc.stderr[-TAIL_CAP:]})
            if proc.returncode != 0:
                raise _StepError(
                    f"[{name}] cmd rc={proc.returncode}: "
                    f"{proc.stderr[-TAIL_CAP:] or proc.stdout[-TAIL_CAP:]}")
            src = acc_path if step.get("inplace") else next_path
            if not os.path.exists(src):
                raise _StepError(f"[{name}] cmd 산출물 없음: {src}")
            with open(src, "rb") as f:
                acc = bytearray(f.read())

        elif stype == "regions":
            if plat is None:
                plat = _get_platform(cfg.get("platform", "gba"))
            font = cfg.get("font_abs")
            if not font:
                raise _StepError(f"[{name}] font 미설정(매니페스트 font 필요)")
            frame_dir = manifest_mod.resolve_frames(cfg, root, step["screen"])
            try:
                frame = plat.load_frame(frame_dir)
            except (OSError, KeyError, ValueError) as ex:
                raise _StepError(f"[{name}] frame 로드 실패({frame_dir}): {ex}")
            bg = step["_bg"]
            base = step["_rom_base"]
            redraw = _get_redraw()
            items_rep = []
            for k, it in enumerate(step["items"]):
                kwargs = {"font_path": font}
                for opt in ("hi", "fill", "dark", "ol", "margin"):
                    if it.get(opt) is not None:
                        kwargs[opt] = it[opt]
                if it.get("_excl"):
                    kwargs["excl"] = it["_excl"]
                if previews_dir:
                    os.makedirs(previews_dir, exist_ok=True)
                    kwargs["preview_path"] = os.path.join(
                        previews_dir, f"{name}_{k:02d}.png")
                r = redraw.redraw_rect(acc, frame, bg, base, it["_rect"],
                                       it["text"], **kwargs)
                label = f"{name}[{k}]:{it['text']}"
                written = int(r.get("tiles") or 0)
                if not r.get("ok"):
                    status = "fail"
                elif written <= 0 or r.get("skipped") or r.get("blanked"):
                    status = "warn"
                else:
                    status = "ok"
                ent = {"written": written, "cells": r.get("cells"),
                       "conflicts": r.get("conflicts"), "status": status}
                items_rep.append({"label": label, **ent,
                                  "px": r.get("px"), "preview": r.get("preview")})
                if collect is not None:
                    collect["g1"]["entries"][label] = ent
                    if status == "fail":
                        collect["g1"]["pass"] = False
                        collect["attention"].append(f"G1 fail: {label}")
                    elif status == "warn":
                        collect["warnings"] += 1
                        collect["attention"].append(f"G1 warn(무기록/blank): {label}")
            srep["items"] = items_rep
            srep["frame_dir"] = frame_dir

        post = bytes(acc)
        spans = _diff_spans(pre, post)
        srep["seconds"] = round(time.time() - t0, 3)
        srep["changed_bytes"] = sum(e - s for s, e in spans)
        srep["span_count"] = len(spans)
        srep["spans"] = _hex_spans(spans)

        if collect is not None:
            collect["steps"].append(srep)
            # ── G2: 선언 스팬 대조 ──
            if stype == "cmd" and step.get("_declared_spans"):
                allowed = _merge_spans(step["_declared_spans"])
                bad = _uncovered(spans, allowed)
                collect["g2"]["checked"].append(name)
                if bad:
                    collect["g2"]["pass"] = False
                    collect["g2"]["violations"].append(
                        {"step": name, "spans": _hex_spans(bad),
                         "declared": _hex_spans(allowed)})
            elif stype == "regions":
                geom = _regions_geometry(plat, frame, bg, base, step["items"])
                bad = _uncovered(spans, geom["allowed_spans"])
                collect["g2"]["checked"].append(name)
                if bad:
                    collect["g2"]["pass"] = False
                    collect["g2"]["violations"].append(
                        {"step": name, "spans": _hex_spans(bad),
                         "declared": _hex_spans(geom["allowed_spans"])})
                # ── G4/G5: compose 기반 픽셀 검증 ──
                _gate_pixels(collect, name, step, geom, frame, bg, base,
                             pre, post, spans)
            elif stype == "cmd":
                collect["g2"]["skipped"].append(
                    {"step": name, "reason": "declared_spans 미선언"})

    return acc


def _gate_pixels(collect, name, step, geom, frame, bg, base, pre, post, spans):
    """G4(region_isolation)/G5(intended_change) — compose 로 before/after 재구성."""
    try:
        render = _get_render()
        img_before = render.compose(frame, layers=[(bg, pre, base)])
        img_after = render.compose(frame, layers=[(bg, post, base)])
    except (ImportError, AttributeError, KeyError, ValueError, OSError) as ex:
        reason = f"compose 불가: {ex}"
        collect["g4"]["skipped"].append({"step": name, "reason": reason})
        collect["g5"]["skipped"].append({"step": name, "reason": reason})
        collect["warnings"] += 1
        collect["attention"].append(f"G4/G5 skip({name}): {reason}")
        return

    diff = _diff_pixels(img_before, img_after)
    # 허용 마스크 = rect 셀 ∪ 실기록 셀(타일 경계)
    mask = geom["rect_cells"] | _written_cells(geom, base, spans)
    out_px = [(x, y) for x, y in diff if (x // 8, y // 8) not in mask]
    collect["g4"]["out_of_region_diff_px"] += len(out_px)
    if out_px:
        collect["g4"]["pass"] = False
        cells = sorted({(x // 8, y // 8) for x, y in out_px})[:OFFENDER_CAP]
        collect["g4"]["offenders"].append(
            {"step": name, "px": len(out_px), "cells": cells})
        collect["attention"].append(
            f"G4 fail({name}): 허용 마스크 밖 diff {len(out_px)}px")

    for k, it in enumerate(step["items"]):
        label = f"{name}[{k}]:{it['text']}"
        ent = collect["g1"]["entries"].get(label, {})
        if ent.get("status") != "ok":
            collect["g5"]["skipped"].append(
                {"entry": label, "reason": f"G1 status={ent.get('status')}"})
            continue
        cells = _rect_cells(it["_rect"], it.get("_excl"))
        n = sum(1 for x, y in diff if (x // 8, y // 8) in cells)
        collect["g5"]["per_entry_changed_px"][label] = n
        if n < G5_MIN_PX:
            collect["g5"]["pass"] = False
            collect["attention"].append(
                f"G5 fail: {label} 변경 {n}px < {G5_MIN_PX}px")


# ── 공개 API ─────────────────────────────────────────────────────────

def run_chain(project_root: str, cfg: dict, chain: str, version: str, *,
              steps: list = None, workdir: str = None,
              determinism: bool = True, preview_only: bool = False) -> dict:
    """매니페스트 체인을 실행하고 게이트 G1~G5 판정 + run report 를 남긴다.

    인자:
      project_root — 프로젝트 루트(상태는 <root>/.krpatch/gfx/ 아래)
      cfg          — manifest.load_gfx_config() 결과(errors 비어있어야 함)
      chain        — cfg["chains"] 의 체인 이름
      version      — out 경로·argv 의 {version} 치환값
      steps        — 스텝 이름 부분집합(순서는 매니페스트 정의 순서 유지)
      workdir      — cmd 실체화 디렉터리(기본: 로컬 tempfile.mkdtemp("krgfx-"))
      determinism  — G3(전체 체인 2회 실행 md5 비교) on/off
      preview_only — True 면 게이트까지 수행하되 out(및 rejected) 미기록

    리턴(요약 투영): {run_id, verdict, first_fail, out, md5, counters,
                      attention, report_path}
    """
    root = os.path.abspath(project_root)
    runs_dir = os.path.join(root, ".krpatch", "gfx", "runs")
    os.makedirs(runs_dir, exist_ok=True)
    run_id = _utcnow_id()
    n = 1
    while os.path.exists(os.path.join(runs_dir, run_id + ".json")):
        run_id = f"{_utcnow_id()}-{n}"
        n += 1
    report_path = os.path.join(runs_dir, run_id + ".json")
    run_dir = os.path.join(runs_dir, run_id)

    report = {"run_id": run_id, "chain": chain, "version": version,
              "preview_only": preview_only, "inputs": {}, "steps": [],
              "gates": {}, "verdict": "fail", "first_fail": None,
              "attention": [], "artifacts": {}, "output_md5": None}

    def _finish(summary_extra=None):
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        out = {"run_id": run_id, "verdict": report["verdict"],
               "first_fail": report["first_fail"],
               "out": report["artifacts"].get("out"),
               "md5": report["output_md5"],
               "counters": report.get("counters", {}),
               "attention": report["attention"],
               "report_path": report_path}
        if summary_extra:
            out.update(summary_extra)
        return out

    # ── 매니페스트/체인 사전 검증 ──
    errs = list(cfg.get("errors") or [])
    ch = (cfg.get("chains") or {}).get(chain)
    if ch is None:
        errs.append(f"chain '{chain}' 없음 — 정의: "
                    + ", ".join(sorted(cfg.get("chains") or {})))
    if errs:
        report["first_fail"] = "manifest"
        report["attention"] = errs
        return _finish()

    if not os.path.exists(ch["_base_abs"]):
        report["first_fail"] = "manifest"
        report["attention"] = [f"base 없음: {ch['_base_abs']}"]
        return _finish()
    with open(ch["_base_abs"], "rb") as f:
        _b = f.read()
    report["inputs"]["base"] = {"path": ch["_base_abs"], "md5": _md5(_b)}
    del _b
    if cfg.get("font_abs") and os.path.exists(cfg["font_abs"]):
        with open(cfg["font_abs"], "rb") as f:
            report["inputs"]["font"] = {"path": cfg["font_abs"],
                                        "md5": _md5(f.read())}

    collect = {
        "steps": [], "attention": [], "warnings": 0,
        "g1": {"pass": True, "entries": {}},
        "g2": {"pass": True, "violations": [], "checked": [], "skipped": []},
        "g4": {"pass": True, "out_of_region_diff_px": 0, "offenders": [],
               "skipped": []},
        "g5": {"pass": True, "per_entry_changed_px": {}, "skipped": []},
    }

    own_wd = workdir is None
    wd = workdir or tempfile.mkdtemp(prefix="krgfx-")
    os.makedirs(wd, exist_ok=True)
    previews_dir = os.path.join(run_dir, "previews")
    step_error = None
    acc = None
    try:
        try:
            acc = _exec_chain(root, cfg, ch, version, steps, wd,
                              collect=collect, previews_dir=previews_dir)
        except _StepError as ex:
            step_error = str(ex)

        report["steps"] = collect["steps"]
        report["attention"] = collect["attention"]

        # ── G3 determinism: 전체 체인 2회 실행 md5 비교 ──
        g3 = {"pass": True, "md5": _md5(acc) if acc is not None else None}
        if step_error is None and determinism:
            wd2 = tempfile.mkdtemp(prefix="krgfx-")
            try:
                acc2 = _exec_chain(root, cfg, ch, version, steps, wd2)
                g3["md5_rerun"] = _md5(acc2)
                if g3["md5_rerun"] != g3["md5"]:
                    g3["pass"] = False
                    report["attention"].append(
                        f"G3 fail: 재실행 md5 불일치 {g3['md5']} != {g3['md5_rerun']}")
            except _StepError as ex:
                g3["pass"] = False
                g3["error"] = str(ex)
                report["attention"].append(f"G3 fail: 재실행 오류 {ex}")
            finally:
                shutil.rmtree(wd2, ignore_errors=True)
        elif not determinism:
            g3["skipped"] = True
            report["attention"].append("G3 skip: determinism=False")

        gates = {
            "G1": {"pass": collect["g1"]["pass"],
                   "entries": collect["g1"]["entries"]},
            "G2": {"pass": collect["g2"]["pass"],
                   "violations": collect["g2"]["violations"],
                   "checked": collect["g2"]["checked"],
                   "skipped": collect["g2"]["skipped"]},
            "G3": g3,
            "G4": collect["g4"],
            "G5": collect["g5"],
        }
        report["gates"] = gates

        # ── verdict 합성 ──
        first_fail = None
        if step_error is not None:
            first_fail = "step"
            report["attention"].insert(0, f"스텝 오류: {step_error}")
        else:
            for gid in ("G1", "G2", "G3", "G4", "G5"):
                if not gates[gid]["pass"]:
                    first_fail = gid
                    break
        warnings = collect["warnings"] \
            + len(collect["g4"]["skipped"]) + len(collect["g5"]["skipped"])
        if first_fail:
            report["verdict"] = "fail"
            report["first_fail"] = first_fail
        elif warnings:
            report["verdict"] = "pass_with_warnings"
        else:
            report["verdict"] = "pass"

        report["counters"] = {
            "steps_run": len(collect["steps"]),
            "changed_bytes": sum(s.get("changed_bytes", 0)
                                 for s in collect["steps"]),
            "spans": sum(s.get("span_count", 0) for s in collect["steps"]),
            "region_items": len(collect["g1"]["entries"]),
            "warnings": warnings,
        }
        if os.path.isdir(previews_dir):
            report["artifacts"]["previews"] = previews_dir

        # ── out 기록 / rejected 격리 ──
        if acc is not None:
            report["output_md5"] = _md5(acc)
        if preview_only:
            report["attention"].append("preview_only: out 미기록")
        elif report["verdict"] == "fail":
            if acc is not None:
                os.makedirs(run_dir, exist_ok=True)
                rej = os.path.join(run_dir, "rejected.gba")
                with open(rej, "wb") as f:
                    f.write(acc)
                report["artifacts"]["rejected"] = rej
        else:
            out_path = ch["_out_template_abs"].replace("{version}", version)
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            if os.path.exists(out_path):
                bak = f"{out_path}.bak-{run_id}"
                shutil.copy2(out_path, bak)
                report["artifacts"]["backup"] = bak
            with open(out_path, "wb") as f:
                f.write(acc)
            report["artifacts"]["out"] = out_path
        return _finish()
    finally:
        if own_wd:
            shutil.rmtree(wd, ignore_errors=True)
