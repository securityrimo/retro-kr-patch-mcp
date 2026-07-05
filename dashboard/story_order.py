#!/usr/bin/env python3
"""스토리(재생) 순서 재정렬 — ROM 무관 범용 모듈.

레트로 게임의 텍스트 배치는 크게 두 부류다:
  (A) 인라인-바이트코드형: 대사가 스크립트 명령 스트림에 끼워져 있음
      → 파일 offset 순서 ≈ 실행(저작) 순서. mode="offset".
  (B) 포인터-테이블형: 대사는 텍스트 뱅크에 모여있고 코드/테이블이 포인터로 참조
      → 포인터가 등장하는 순서 ≈ 스크립트 순서. mode="pointer".

두 메커니즘으로 지배적 아키텍처 대부분을 커버한다. 진짜 분기(전투 분기,
조건부 이벤트)까지 따라가려면 ROM별 스크립트 VM 디스어셈블이 필요하며 범용
불가 — 이 모듈은 offset/pointer 두 근사와 scene 그룹핑까지만 책임진다.

출력은 항상 "장면(scene) 리스트" — 각 scene = 연속된 문자열 묶음. 대시보드는
ROM 종류와 무관하게 이 구조만 소비한다.
"""
import struct


def _by_offset(strings):
    return sorted(strings, key=lambda s: s['offset'])


def _pointer_order(strings, rom, base=0x08000000, psize=4, step=1):
    """포인터-테이블형: ROM 전역을 훑어 각 문자열 offset 을 겨냥하는 최소 포인터
    위치를 찾고, 그 포인터 위치 순으로 정렬. 참조 안 되는 문자열은 offset 순 뒤에.

    반환: (ordered_strings, coverage_ratio)
    """
    if psize not in (2, 3, 4):
        psize = 4
    targets = {s['offset']: s for s in strings}
    # target file-offset → 가장 이른 포인터 저장위치
    first_ref = {}
    n = len(rom)
    lo, hi = base, base + n
    i = 0
    while i + psize <= n:
        v = int.from_bytes(rom[i:i + psize], 'little')
        if lo <= v < hi:
            off = v - base
            if off in targets and off not in first_ref:
                first_ref[off] = i
        i += step
    referenced = [s for s in strings if s['offset'] in first_ref]
    unref = [s for s in strings if s['offset'] not in first_ref]
    referenced.sort(key=lambda s: first_ref[s['offset']])
    unref.sort(key=lambda s: s['offset'])
    ordered = referenced + unref
    cov = len(referenced) / len(strings) if strings else 0.0
    return ordered, cov


def order_strings(strings, rom=None, mode='offset', cfg=None):
    """mode 에 따라 정렬된 문자열 리스트 + 메타 반환.

    mode: offset | pointer | file(원본 순서 유지)
    반환: {'order': [...], 'mode': mode, 'coverage': float|None, 'note': str}
    """
    cfg = cfg or {}
    if mode == 'pointer' and rom is not None:
        ordered, cov = _pointer_order(
            strings, rom,
            base=int(cfg.get('pointer_base', 0x08000000)),
            psize=int(cfg.get('pointer_size', 4)),
            step=int(cfg.get('pointer_step', 1)),
        )
        note = f'포인터 참조순 (커버리지 {cov*100:.0f}% — 미참조는 offset순 뒤에)'
        return {'order': ordered, 'mode': mode, 'coverage': cov, 'note': note}
    if mode == 'file':
        return {'order': list(strings), 'mode': mode, 'coverage': None,
                'note': '원본(추출) 순서'}
    # 기본 offset
    return {'order': _by_offset(strings), 'mode': 'offset', 'coverage': None,
            'note': 'offset 순 (인라인-바이트코드형은 실행순과 일치)'}


def group_scenes(ordered, gap_threshold=0x80):
    """정렬된 문자열을 장면 경계(offset 급간격)로 분할.

    포인터순 등 offset 이 단조롭지 않은 mode 에선 인접 offset 차가 기준을 넘으면
    새 장면으로 끊는다(느슨한 그룹핑). 반환: [[s,...], ...]
    """
    scenes = []
    cur = []
    prev = None
    for s in ordered:
        if prev is not None and abs(s['offset'] - prev) > gap_threshold:
            if cur:
                scenes.append(cur)
            cur = []
        cur.append(s)
        prev = s['offset']
    if cur:
        scenes.append(cur)
    return scenes
