#!/usr/bin/env python3
"""프로젝트 설정 로더 — 검수 대시보드를 ROM/프로젝트 무관하게 구동.

프로젝트 루트의 `krpatch.dashboard.json` 을 읽어 편집대상 JSON·ROM·코덱·
빌드명령·스토리순서 모드를 해석한다. 파일이 없으면 관례로 자동탐지 후 생성.

코덱(codec_module)은 선택 — 있으면 글리프 미리보기/바이트예산을 프로젝트
빌드와 동일 로직으로 계산하고, 없으면 텍스트 전용으로 우아하게 강등한다.
코덱 duck-type 인터페이스(있는 것만 사용):
  load_table(rom)->(_,_,first_idx) · encoded_len(text,first_idx)->(n,bad)
  split_term(kr)->(body,term) · render_glyph(ch)->bytes · sjis_code(ch,first_idx)
  FW: dict
"""
import importlib.util
import json
import os

CONFIG_NAME = 'krpatch.dashboard.json'


def _first_existing(root, cands):
    for c in cands:
        p = os.path.join(root, c)
        if os.path.exists(p):
            return c
    return None


def autodetect(root):
    """관례 기반 설정 추정(파일 없을 때)."""
    tj = _first_existing(root, [
        'translations/extracted_texts_v2.json',
        'translations/extracted_texts.json',
        'assets/translations/review/translations.json',
        'assets/translations/complete/translations.json',
    ])
    rom = _first_existing(root, [
        'roms/original.gba', 'roms/original.sfc', 'roms/original.smc',
        'roms/original.bin', 'roms/original.nds', 'roms/original.gg',
    ])
    codec = _first_existing(root, ['tools/kr_codec.py'])
    build = None
    for c in ['tools/07_build_kr.py', 'tools/build.py']:
        if os.path.exists(os.path.join(root, c)):
            build = [c]
            break
    return {
        'name': os.path.basename(os.path.abspath(root)),
        'translations_json': tj,
        'rom': rom,
        'codec_module': codec,
        'build_cmd': build,
        'port': 5057,
        'story_order': 'offset',
    }


def load_config(root):
    """설정 로드(없으면 자동탐지→저장). 경로는 root 기준 절대경로로 해석."""
    path = os.path.join(root, CONFIG_NAME)
    if os.path.exists(path):
        cfg = json.load(open(path))
    else:
        cfg = autodetect(root)
        json.dump(cfg, open(path, 'w'), ensure_ascii=False, indent=1)
    cfg['_root'] = os.path.abspath(root)
    for k in ('translations_json', 'rom', 'codec_module'):
        if cfg.get(k):
            cfg[k + '_abs'] = os.path.join(cfg['_root'], cfg[k])
    return cfg


def load_codec(cfg):
    """codec_module 을 동적 import. 실패/부재 시 None (텍스트전용 강등)."""
    p = cfg.get('codec_module_abs')
    if not p or not os.path.exists(p):
        return None
    try:
        spec = importlib.util.spec_from_file_location('_krpatch_codec', p)
        mod = importlib.util.module_from_spec(spec)
        # 코덱이 형제 모듈을 import 할 수 있게 tools 디렉토리를 경로에 추가
        import sys
        d = os.path.dirname(p)
        if d not in sys.path:
            sys.path.insert(0, d)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None
