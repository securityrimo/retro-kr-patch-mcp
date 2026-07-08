#!/usr/bin/env python3
"""레트로 한글패치 번역 검수 대시보드 (Flask, 로컬 전용) — ROM/프로젝트 무관.

`PROJECT_DIR` 환경변수의 프로젝트를 `krpatch.dashboard.json` 설정대로 연다.
- 원문 ↔ 한글 좌우 대조, 코덱 있으면 실제 글리프 미리보기 + 바이트예산 게이지
- 대본(스토리순) 뷰: story_order 로 offset/pointer 재정렬 + 장면 그룹핑
- 필터/검색/인라인편집/리빌드
코덱 부재 프로젝트는 텍스트 전용으로 우아하게 강등(미리보기·예산 숨김).
"""
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

from flask import Flask, jsonify, render_template, request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import project as prj  # noqa: E402
import story_order as so  # noqa: E402

try:
    from PIL import Image
except Exception:
    Image = None

ROOT = os.environ.get('PROJECT_DIR') or os.getcwd()
CFG = prj.load_config(ROOT)
CODEC = prj.load_codec(CFG)
EXTRACT = CFG.get('translations_json_abs')
ROM_PATH = CFG.get('rom_abs')

# 코덱 있고 ROM 있으면 sjis 검증용 first_idx 확보
FIRST_IDX = None
_ROM = None
if ROM_PATH and os.path.exists(ROM_PATH):
    _ROM = bytearray(open(ROM_PATH, 'rb').read())
    if CODEC and hasattr(CODEC, 'load_table'):
        try:
            _, _, FIRST_IDX = CODEC.load_table(_ROM)
        except Exception:
            FIRST_IDX = None

HAS_PREVIEW = bool(CODEC and hasattr(CODEC, 'render_glyph') and Image is not None)
HAS_BUDGET = bool(CODEC and hasattr(CODEC, 'encoded_len'))

app = Flask(__name__)

CJK = re.compile(r'[぀-ヿ一-鿿ｦ-ﾟ]')   # 히라/가타/한자/반각가나
PUNCT = '!?！？。…～!?'


# ── 데이터 IO ──────────────────────────────────────────────────
def load():
    return json.load(open(EXTRACT))


def save(data):
    """저장 전 롤링 백업(.bak-YYYYmmdd-HHMMSS), 최근 20개 유지."""
    ts = time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(EXTRACT, f'{EXTRACT}.bak-{ts}')
    d = os.path.dirname(EXTRACT)
    base = os.path.basename(EXTRACT)
    baks = sorted(f for f in os.listdir(d) if f.startswith(base + '.bak-2'))
    for old in baks[:-20]:
        os.remove(os.path.join(d, old))
    json.dump(data, open(EXTRACT, 'w'), ensure_ascii=False, indent=1)


# ── 계산 헬퍼(코덱 유무에 따라 강등) ────────────────────────────
def _enc_len(text):
    if HAS_BUDGET:
        return CODEC.encoded_len(text, FIRST_IDX)
    return len(text) * 2, []   # 근사: 코덱 없으면 문자당 2B 가정


def _split_term(kr):
    if CODEC and hasattr(CODEC, 'split_term'):
        return CODEC.split_term(kr)
    return kr, ''


def is_name_tag(s, kr):
    """화자 이름표 휴리스틱: 짧고 부호 없이 개행으로 끝나며 가시글자 ≤4자."""
    budget = s.get('byte_budget', len(kr or '') * 2)
    if not kr or budget > 10 or kr.startswith('＿'):
        return False
    body, term = _split_term(kr)
    if CODEC and hasattr(CODEC, 'split_term'):
        if '＠' not in term:
            return False
    if any(p in body for p in PUNCT):
        return False
    vis = [c for c in body if c not in ' 　＿']
    return 1 <= len(vis) <= 4


def annotate(s):
    kr = s.get('kr') or ''
    nbytes, bad = _enc_len(kr)
    budget = s.get('byte_budget', 0)
    body, term = _split_term(kr)
    bb, _ = _enc_len(body)
    tb, _ = _enc_len(term)
    pad = budget - bb - tb
    fits = (pad >= 0 and pad % 2 == 0 and not bad) if HAS_BUDGET else True
    return {
        'offset': s.get('offset', 0), 'offset_hex': s['offset_hex'],
        'jp': s.get('jp', ''), 'kr': kr,
        'byte_budget': budget, 'enc_bytes': nbytes, 'n_chars': s.get('n_chars', 0),
        'fits': fits, 'bad': bad, 'pad': pad,
        'has_jp': bool(CJK.search(kr)),
        'jp_src': bool(CJK.search(s.get('jp', ''))),
        'manual': bool(s.get('manual')),
        'needs_review': bool(s.get('needs_review')),
        'untranslated': (not kr) or bool(s.get('needs_review')),
        'is_name': is_name_tag(s, kr),
    }


def inconsistencies(data):
    by_jp = {}
    for s in data['strings']:
        jp = s.get('jp', '')
        if jp:
            by_jp.setdefault(jp, set()).add(s.get('kr') or '')
    bad_jp = {jp for jp, krs in by_jp.items() if len(krs) > 1}
    return {s['offset_hex'] for s in data['strings']
            if s.get('jp') in bad_jp and s.get('jp')}


# ── 라우트 ─────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/config')
def api_config():
    return jsonify({
        'name': CFG.get('name'),
        'has_preview': HAS_PREVIEW,
        'has_budget': HAS_BUDGET,
        'has_rom': _ROM is not None,
        'story_modes': (['offset', 'pointer', 'file'] if _ROM is not None
                        else ['file']),
        'default_mode': CFG.get('story_order', 'offset'),
        'can_rebuild': bool(CFG.get('build_cmd')),
    })


def _match(r, flt, q):
    """annotate된 행이 필터+검색어에 부합하는지."""
    if flt == 'untranslated':
        ok = r['untranslated']
    elif flt == 'jp_residue':
        ok = r['has_jp']
    elif flt == 'needs_review':
        ok = r['needs_review']
    elif flt == 'manual':
        ok = r['manual']
    elif flt == 'overbudget':
        ok = not r['fits']
    elif flt == 'inconsistent':
        ok = r['inconsistent']
    else:
        ok = True
    if ok and q:
        ok = q in r['jp'] or q in r['kr'] or q in r['offset_hex']
    return ok


def _rows(data, flt, q):
    inc = inconsistencies(data)
    rows = []
    for s in data['strings']:
        a = annotate(s)
        a['inconsistent'] = a['offset_hex'] in inc
        rows.append(a)
    return [r for r in rows if _match(r, flt, q)]


@app.route('/api/strings')
def api_strings():
    data = load()
    flt = request.args.get('filter', 'all')
    q = request.args.get('q', '').strip()
    rows = _rows(data, flt, q)
    return jsonify({'count': len(rows), 'total': len(data['strings']), 'rows': rows})


@app.route('/api/scenes')
def api_scenes():
    """대본(스토리순) 뷰: 재정렬 + 장면 그룹핑."""
    data = load()
    mode = request.args.get('mode', CFG.get('story_order', 'offset'))
    flt = request.args.get('filter', 'all')
    q = request.args.get('q', '').strip()
    if mode != 'file' and _ROM is None:
        mode = 'file'
    res = so.order_strings(data['strings'], rom=_ROM, mode=mode, cfg=CFG)
    inc = inconsistencies(data)
    scenes_raw = so.group_scenes(res['order'])
    active = flt != 'all' or bool(q)
    scenes = []
    for grp in scenes_raw:
        annotated = []
        for s in grp:
            a = annotate(s)
            a['inconsistent'] = a['offset_hex'] in inc
            annotated.append(a)
        if active:
            # 필터 시: 매칭 행이 하나도 없는 장면은 제외.
            # 남기는 장면 안에서는 매칭 행 + 화자 이름표(문맥 보존)만 노출.
            if not any(_match(a, flt, q) for a in annotated):
                continue
            annotated = [a for a in annotated if _match(a, flt, q) or a['is_name']]
            # 이름표만 남고 본문 매칭이 사라진 경우 제외
            if not any(_match(a, flt, q) for a in annotated):
                continue
        scenes.append({'start': grp[0].get('offset_hex', ''), 'rows': annotated})
    return jsonify({'mode': res['mode'], 'note': res['note'],
                    'coverage': res['coverage'], 'scenes': scenes})


@app.route('/api/string', methods=['POST'])
def api_string():
    body = request.get_json()
    off, kr = body['offset_hex'], body.get('kr', '')
    force = body.get('force', False)
    nbytes, bad = _enc_len(kr)
    b, t = _split_term(kr)
    bb, _ = _enc_len(b)
    tb, _ = _enc_len(t)
    data = load()
    s = next((x for x in data['strings'] if x['offset_hex'] == off), None)
    if s is None:
        return jsonify({'ok': False, 'err': 'offset 미발견'}), 404
    budget = s.get('byte_budget', 0)
    pad = budget - bb - tb
    fits = (pad >= 0 and pad % 2 == 0 and not bad) if HAS_BUDGET else True
    if HAS_BUDGET and not fits and not force:
        msg = (f'예산초과/정렬불가 (필요 {bb+tb}B, 예산 {budget}B, pad {pad})'
               if not bad else f'인코딩 불가 문자: {"".join(bad)}')
        return jsonify({'ok': False, 'err': msg, 'fits': False})
    s['kr'] = kr
    s['manual'] = True
    s.pop('needs_review', None)
    save(data)
    return jsonify({'ok': True, 'enc_bytes': nbytes, 'fits': fits})


def _parse_lines(text):
    lines = [[]]
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '&' and i + 1 < len(text) and (text[i+1] == '&' or text[i+1] in '0123456789ABCDEF'):
            i += 2; continue
        if ch in 'x+-' and i + 1 < len(text) and text[i+1] in '0123':
            i += 2; continue
        if ch == '＠':
            lines.append([]); i += 1; continue
        if ch in '￥＄＊':
            i += 1; continue
        if ch in ('＿', ' ', '　'):
            lines[-1].append(None); i += 1; continue
        lines[-1].append(ch); i += 1
    return lines


@app.route('/api/preview')
def api_preview():
    if not HAS_PREVIEW:
        return jsonify({'png': None, 'disabled': True})
    text = request.args.get('text', '')
    lines = _parse_lines(text)[:3]
    scale = 4
    cols = max((len(l) for l in lines), default=1) or 1
    cols = min(max(cols, 6), 18)
    W = cols * 12 * scale
    H = len(lines) * 14 * scale
    img = Image.new('RGB', (W, H), (24, 52, 120))
    fw = getattr(CODEC, 'FW', {})
    sjis = getattr(CODEC, 'sjis_code', lambda c, f: None)
    for ly, line in enumerate(lines):
        for cx, ch in enumerate(line):
            if ch is None:
                continue
            ok = ('가' <= ch <= '힣') or sjis(ch, FIRST_IDX) or ch in fw
            if not ok:
                continue
            g = CODEC.render_glyph(ch)
            for y in range(12):
                for x in range(12):
                    nib = (g[y*8 + x//2] >> (4 if (x & 1) else 0)) & 0xF
                    if nib:
                        px0 = (cx*12 + x) * scale
                        py0 = (ly*14 + y) * scale
                        for dy in range(scale):
                            for dx in range(scale):
                                if px0+dx < W and py0+dy < H:
                                    img.putpixel((px0+dx, py0+dy), (245, 245, 245))
    buf = io.BytesIO(); img.save(buf, 'PNG')
    return jsonify({'png': 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()})


@app.route('/api/rebuild', methods=['POST'])
def api_rebuild():
    build = CFG.get('build_cmd')
    if not build:
        return jsonify({'ok': False, 'err': '이 프로젝트에 build_cmd 미설정'})
    cmd = [sys.executable] + [os.path.join(CFG['_root'], c) if c.endswith('.py') else c
                              for c in build]
    env = dict(os.environ, PYTHONPATH=os.path.join(CFG['_root'], 'tools'))
    try:
        p = subprocess.run(cmd, cwd=CFG['_root'], env=env,
                           capture_output=True, text=True, timeout=300)
        return jsonify({'ok': p.returncode == 0, 'out': p.stdout, 'err': p.stderr})
    except Exception as e:
        return jsonify({'ok': False, 'err': str(e)})


if __name__ == '__main__':
    port = int(os.environ.get('DASH_PORT') or CFG.get('port', 5057))
    # 인증 없는 로컬 검수용 서버 — 기본은 localhost만. LAN 노출은 명시적 opt-in.
    host = os.environ.get('FLASK_RUN_HOST', '127.0.0.1')
    app.run(host=host, port=port)
