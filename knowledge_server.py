#!/usr/bin/env python3
"""retro-kr-patch MCP — 지식 서버 (Tier 1)

create-retro-game-kr-patch Agent Skill의 방법론 문서를 MCP 리소스로 노출.
모든 MCP 클라이언트(Claude Code, pi, Gemini 등)가 참조할 수 있는 범용 지식 베이스.

설계 원칙:
- 외부 API/키/네트워크 0 — 순수 로컬 문서 서빙
- cng-ui/shapr-mcp와 동일 패턴: FastMCP + @mcp.tool()/@mcp.resource()
- 체크포인트 시스템으로 FCC 세션 재개 지원
"""
from __future__ import annotations
import json
import re
import os
from pathlib import Path
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

# ── 경로 ──────────────────────────────────────────────────────────────────────
# 기본은 리포에 내장된 knowledge/ (자체완결, 다른 호스트에서도 그대로 동작).
# KRPATCH_SKILL_ROOT 환경변수로 외부 스킬 디렉토리를 대신 가리킬 수 있음(사설 확장용).
REPO_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = Path(os.environ.get("KRPATCH_SKILL_ROOT", str(REPO_ROOT / "knowledge")))
STRATEGY_DIR = SKILL_ROOT / "references" / "strategy"
PLATFORMS_DIR = SKILL_ROOT / "references" / "platforms"
SKILL_MD = SKILL_ROOT / "SKILL.md"
CHECKPOINT_DIR = Path(os.environ.get("KRPATCH_CHECKPOINT_DIR", str(REPO_ROOT / "checkpoints")))
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("retro-kr-patch-knowledge")


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────
def _read_md(path: Path) -> str | None:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _list_strategy_docs() -> dict[str, str]:
    """strategy 디렉토리의 모든 문서 목록 (이름 → 첫 줄)"""
    docs = {}
    for f in sorted(STRATEGY_DIR.glob("*.md")):
        content = f.read_text(encoding="utf-8")
        first_line = content.strip().split("\n")[0].lstrip("#").strip()
        docs[f.stem] = first_line
    return docs


def _list_platform_docs() -> dict[str, str]:
    """platforms 디렉토리의 모든 문서 목록 (이름 → 첫 줄)"""
    docs = {}
    for f in sorted(PLATFORMS_DIR.glob("*.md")):
        content = f.read_text(encoding="utf-8")
        first_line = content.strip().split("\n")[0].lstrip("#").strip()
        docs[f.stem] = first_line
    return docs


# ── 리소스: SKILL.md ─────────────────────────────────────────────────────────
@mcp.resource("skill://overview")
def get_skill_overview() -> str:
    """SKILL.md — 한글 패치 Agent Skill 개요·라우팅·핵심 불변식"""
    return _read_md(SKILL_MD) or "(SKILL.md not found)"


@mcp.resource("skill://pipeline")
def get_pipeline() -> str:
    """전체 파이프라인 단계 요약 (SKILL.md에서 추출)"""
    skill = _read_md(SKILL_MD)
    if not skill:
        return "N/A"
    # 파이프라인 도표 + 워크플로우 설명 부분 추출
    lines = skill.split("\n")
    in_pipeline = False
    out = []
    for line in lines:
        if "## 전체 워크플로우" in line:
            in_pipeline = True
            continue
        if in_pipeline:
            if line.startswith("## ") and "전체 워크플로우" not in line:
                break
            out.append(line)
    return "\n".join(out)


# ── 리소스: strategy 문서 ────────────────────────────────────────────────────
@mcp.resource("strategy://list")
def get_strategy_list() -> str:
    """전략 문서 목록 (이름 + 설명)"""
    docs = _list_strategy_docs()
    lines = ["# Strategy Documents", ""]
    for name, desc in docs.items():
        lines.append(f"- **{name}**: {desc}")
    return "\n".join(lines)


@mcp.resource("strategy://{name}")
def get_strategy_doc(name: str) -> str:
    """특정 전략 문서 전체 내용"""
    path = STRATEGY_DIR / f"{name}.md"
    content = _read_md(path)
    if content is None:
        return json.dumps({"error": f"strategy 문서 없음: {name}", "available": list(_list_strategy_docs().keys())}, ensure_ascii=False)
    return content


# ── 리소스: platforms 문서 ───────────────────────────────────────────────────
@mcp.resource("platforms://list")
def get_platform_list() -> str:
    """플랫폼 문서 목록 (이름 + 설명)"""
    docs = _list_platform_docs()
    lines = ["# Platform Documents", ""]
    for name, desc in docs.items():
        lines.append(f"- **{name}**: {desc}")
    return "\n".join(lines)


@mcp.resource("platforms://{name}")
def get_platform_doc(name: str) -> str:
    """특정 플랫폼 문서 전체 내용"""
    path = PLATFORMS_DIR / f"{name}.md"
    content = _read_md(path)
    if content is None:
        return json.dumps({"error": f"platforms 문서 없음: {name}", "available": list(_list_platform_docs().keys())}, ensure_ascii=False)
    return content


# ── 툴: 지식 검색 ────────────────────────────────────────────────────────────
def _search_in_docs(query: str) -> list[dict]:
    """모든 문서에서 쿼리 검색"""
    results = []
    q = query.lower()
    all_docs = []

    # SKILL.md
    skill = _read_md(SKILL_MD)
    if skill:
        all_docs.append(("skill://overview", "SKILL.md", skill))

    # Strategy docs
    for f in sorted(STRATEGY_DIR.glob("*.md")):
        all_docs.append((f"strategy://{f.stem}", f"strategy/{f.name}", f.read_text(encoding="utf-8")))

    # Platform docs
    for f in sorted(PLATFORMS_DIR.glob("*.md")):
        all_docs.append((f"platforms://{f.stem}", f"platforms/{f.name}", f.read_text(encoding="utf-8")))

    # 검색: 제목 + 본문 청크
    for uri, label, content in all_docs:
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if q in line.lower():
                # 전후 컨텍스트 추출
                start = max(0, i - 2)
                end = min(len(lines), i + 3)
                context = "\n".join(f"  L{j+1}: {l}" for j, l in enumerate(lines[start:end], start))
                results.append({
                    "uri": uri,
                    "doc": label,
                    "line": i + 1,
                    "context": context
                })

    return results


@mcp.tool()
def search_knowledge(query: str) -> str:
    """모든 strategy·platform 문서에서 키워드 검색. URI, 줄번호, 컨텍스트 반환.

    검색 대상: SKILL.md + strategy/ 12종 + platforms/ 9종.
    용례: "포인터 테이블 검색 방법", "SNES 체크섬 재계산", "완성형 vs 조합형"
    """
    if not query.strip():
        return json.dumps({"error": "query 필요"}, ensure_ascii=False)

    results = _search_in_docs(query)
    if not results:
        # 부분 일치 재시도
        words = query.split()
        for word in words:
            if len(word) >= 2:
                results.extend(_search_in_docs(word))
        # 중복 제거
        seen = set()
        unique = []
        for r in results:
            key = (r["uri"], r["line"])
            if key not in seen:
                seen.add(key)
                unique.append(r)
        results = unique

    return json.dumps({
        "query": query,
        "count": len(results),
        "results": results[:30]  # 최대 30개
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def get_workflow_step(step: str) -> str:
    """특정 파이프라인 단계에 해당하는 모든 관련 문서를 모아 반환.

    step: survey|font|extract|poc|reinsert|translate|build|debug|graphics|compression|conventions|tips
    """
    step_map = {
        "survey": "initial-survey",
        "font": "font-strategy",
        "extract": "text-extraction",
        "poc": "poc",
        "reinsert": "reinsertion",
        "translate": "translation-workflow",
        "build": "build-and-verify",
        "debug": "debugging",
        "graphics": "graphics-text",
        "compression": "compression",
        "conventions": "project-conventions",
        "tips": "tips",
    }

    name = step_map.get(step.lower())
    if not name:
        return json.dumps({
            "error": f"알 수 없는 단계: {step}",
            "valid_steps": list(step_map.keys())
        }, ensure_ascii=False)

    path = STRATEGY_DIR / f"{name}.md"
    content = _read_md(path)
    if not content:
        return json.dumps({"error": f"문서 없음: {name}"}, ensure_ascii=False)

    # SKILL.md에서 이 단계 관련 라우팅 정보도 추출
    skill = _read_md(SKILL_MD) or ""
    routing_lines = []
    in_table = False
    for line in skill.split("\n"):
        if name in line.lower() and "|" in line:
            routing_lines.append(line)

    return json.dumps({
        "step": step,
        "doc": name,
        "content": content,
        "routing": routing_lines if routing_lines else None
    }, ensure_ascii=False)


# ── 툴: 체크포인트 시스템 (FCC 세션 재개) ───────────────────────────────────
def _checkpoint_path(project: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_-]+", "_", project)[:40]
    return CHECKPOINT_DIR / f"{safe}.json"


@mcp.tool()
def checkpoint_save(project: str, phase: str, sub_step: str = "",
                    resume_prompt: str = "", artifacts: str = "{}") -> str:
    """현재 작업 상태를 체크포인트로 저장. FCC 세션 재개에 사용.

    project: 프로젝트명 (예: "chrono_trigger")
    phase: 현재 단계 (survey|font|extract|poc|reinsert|translate|build)
    sub_step: 세부 단계 설명
    resume_prompt: 재개 시 AI에게 전달할 프롬프트
    artifacts: JSON — 현재까지의 산출물 경로/해시 (예: {"rom":"/path/to/rom.sfc","hash":"abcd"})
    """
    path = _checkpoint_path(project)
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))

    now = datetime.now(timezone.utc).isoformat()

    if "checkpoints" not in existing:
        existing["checkpoints"] = {}

    existing["checkpoints"][phase] = {
        "status": "in_progress",
        "sub_step": sub_step,
        "updated_at": now,
    }

    # 이전 in_progress 단계들 정리
    for p, ck in existing["checkpoints"].items():
        if p != phase and ck.get("status") == "in_progress":
            ck["status"] = "interrupted"

    existing["last_phase"] = phase
    existing["last_updated"] = now
    existing["resume_prompt"] = resume_prompt
    existing["artifacts"] = json.loads(artifacts) if isinstance(artifacts, str) else artifacts

    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.dumps({
        "saved": True,
        "project": project,
        "phase": phase,
        "sub_step": sub_step,
        "path": str(path)
    }, ensure_ascii=False)


@mcp.tool()
def checkpoint_load(project: str) -> str:
    """마지막 체크포인트를 불러와 재개 프롬프트와 상태 반환.

    project: 프로젝트명
    """
    path = _checkpoint_path(project)
    if not path.exists():
        return json.dumps({
            "found": False,
            "project": project,
            "message": "체크포인트 없음. checkpoint_save로 먼저 저장하세요."
        }, ensure_ascii=False)

    data = json.loads(path.read_text(encoding="utf-8"))
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def checkpoint_list() -> str:
    """모든 프로젝트의 체크포인트 상태 요약"""
    projects = []
    for f in sorted(CHECKPOINT_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        projects.append({
            "project": f.stem,
            "last_phase": data.get("last_phase", "unknown"),
            "last_updated": data.get("last_updated", ""),
            "phases": {k: v.get("status") for k, v in data.get("checkpoints", {}).items()}
        })
    return json.dumps({"projects": projects, "count": len(projects)}, ensure_ascii=False, indent=2)


@mcp.tool()
def checkpoint_done(project: str, phase: str) -> str:
    """특정 단계 완료 표시

    project: 프로젝트명
    phase: 완료할 단계
    """
    path = _checkpoint_path(project)
    if not path.exists():
        return json.dumps({"error": f"프로젝트 없음: {project}"}, ensure_ascii=False)

    data = json.loads(path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).isoformat()

    if "checkpoints" not in data:
        data["checkpoints"] = {}

    data["checkpoints"][phase] = {
        "status": "done",
        "completed_at": now,
    }
    data["last_updated"] = now

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.dumps({"marked_done": True, "project": project, "phase": phase}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
