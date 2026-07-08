#!/usr/bin/env python3
"""장면-인지 2단계 번역 파이프라인 — ROM/프로젝트 무관 제네릭(MCP 편입판).

프로젝트-로컬 tools/{10_translate_pipeline,build_glossary,resolve_labels}.py 를
codec-driven + config-driven 으로 일반화. 어떤 한글패치 프로젝트든 krpatch.dashboard.json +
codec_module(encoded_len) 만 있으면 동작한다.

3단계:
  1) Context Aggregation : 연속 문자열을 대화 창(장면)으로 묶어 한 프롬프트로
  2) Stage A (화자/맥락 초벌): 창 전체 + 화자 인지 + 말투 일관 + 이름표 고정(glossary)
  3) Stage B (자수 최적화 루프): 줄별 바이트예산·불가문자·제어토큰 기계검증 → 위반 시 피드백 재시도

이름 고정: 프로젝트별 glossary.json(웹지식+ROM 이름표 grounding). 예산 초과 이름표는
한국 통용 별칭으로 축약하되 불확실하면 label_questions.json 으로 사용자 확인 대상 분리.

CLI(= MCP translate_pipeline 도구가 subprocess 로 호출):
  python translate.py <project_dir> glossary [--game T] [--force]
  python translate.py <project_dir> labels
  python translate.py <project_dir> run [--scope all|--start 0xADDR --lines N] [--apply]
env: DEEPSEEK_API_KEY (로컬 .env.master, 키 출력 안 함)
"""
import json, os, re, sys, time, urllib.request, shutil, threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import project as prj  # noqa

API = 'https://api.deepseek.com/v1/chat/completions'
KEY = os.environ.get('DEEPSEEK_API_KEY', '')

# ── 제어토큰(프로젝트가 cfg.ctrl_tokens 로 재정의 가능, 기본=이 ROM-텍스트 계열 공통) ──
DEFAULT_CTRL = r'＠|￥|＊|＄|＿|&&|&[0-9A-F]|[x+\-][0-3]'
_JUNK = re.compile(r'[A-Za-z0-9Ａ-Ｚａ-ｚ０-９★☆／・：\-&]')

class Ctx:
    """설정 + 코덱 + 문자열 로딩 캐시."""
    def __init__(self, project_dir):
        self.cfg = prj.load_config(project_dir)
        self.codec = prj.load_codec(self.cfg)
        self.jpath = self.cfg.get('translations_json_abs')
        self.data = json.load(open(self.jpath))
        self.strings = self.data['strings'] if isinstance(self.data, dict) else self.data
        self.first_idx = 0
        if self.codec and hasattr(self.codec, 'load_table') and self.cfg.get('rom_abs'):
            try:
                _, _, self.first_idx = self.codec.load_table(open(self.cfg['rom_abs'], 'rb').read())
            except Exception:
                pass
        self.ctrl = re.compile(self.cfg.get('ctrl_tokens', DEFAULT_CTRL))
        self.dir = os.path.dirname(self.jpath)

    def enc_len(self, text):
        """(바이트, 불가문자목록). 코덱 없으면 한글·기호 2바이트 근사."""
        if self.codec and hasattr(self.codec, 'encoded_len'):
            try:
                return self.codec.encoded_len(text, self.first_idx)
            except Exception:
                pass
        return len(text) * 2, []

    def ms(self, t):
        return sorted(self.ctrl.findall(t or ''))

def name_of(jp):
    """진짜 이름표 = '이름＠'(＠개행 필수)."""
    m = re.fullmatch(r'([^＿＠￥＊＄]+)＠', jp)
    if not m:
        return None
    c = m.group(1).strip()
    if not c or any(x in c for x in '＿。、！？!?「」…　'):
        return None
    if _JUNK.search(c):
        return None
    return c if len(c) <= 8 else None

def max_chars(jp, budget):
    reserve = 2 if jp.endswith('＠') else 0
    return max(1, (budget - reserve) // 2)

# 스테이지별 모델 분배 — 글로서리(세계지식)·라벨(축약판단)은 상위, 초벌·자수교정은 경량 가능
_MODEL_DEFAULT = os.environ.get('KRPATCH_MODEL_DEFAULT', 'deepseek-chat')
def _model_for(stage):
    return os.environ.get(f'KRPATCH_MODEL_{stage.upper()}', _MODEL_DEFAULT)

def chat(messages, temp=0.3, max_tokens=1800, stage='draft'):
    body = json.dumps({'model': _model_for(stage), 'messages': messages,
                       'temperature': temp, 'max_tokens': max_tokens}).encode()
    req = urllib.request.Request(API, data=body, headers={
        'Content-Type': 'application/json', 'Authorization': f'Bearer {KEY}'})
    for att in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())['choices'][0]['message']['content'].strip()
        except Exception:
            if att == 3:
                raise
            time.sleep(2 * (att + 1))

def _loads_lenient(out):
    out = re.sub(r'^```(json)?|```$', '', out.strip(), flags=re.M).strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return [json.loads(m.group()) for m in re.finditer(r'\{[^{}]*\}', out)]

# ── 글로서리 부트스트랩 ─────────────────────────────────────────────
def build_glossary(ctx, game=None, force=False):
    out = os.path.join(ctx.dir, 'glossary.json')
    if os.path.exists(out) and not force:
        return {'status': 'exists', 'path': out}
    game = game or ctx.cfg.get('name') or os.path.basename(ctx.cfg.get('_root', ''))
    cnt = Counter()
    for s in ctx.strings:
        nm = name_of(s['jp'])
        if nm and not re.fullmatch(r'[？?]+', nm):
            cnt[nm] += 1
    labels = [n for n, _ in cnt.most_common(60)]
    sysmsg = (
        '너는 일본 게임/애니 한국 로컬라이징 전문가다. 게임 등장인물 이름표와 핵심 고유명사를 '
        '한국 정발/통용 표기로 매핑한다. names=화자 이름표(3~4자 짧게), terms=기타 고유명사. '
        '한자·가나 금지 순한글, ？？？류 제외, 확신없으면 제외. '
        'JSON만: {"names":{"<JP>":"<KR>"},"terms":{"<JP>":"<KR>"}}')
    usr = f'게임:『{game}』\nROM 실제 이름표(빈도순):\n{json.dumps(labels, ensure_ascii=False)}'
    m = _loads_lenient(chat([{'role': 'system', 'content': sysmsg}, {'role': 'user', 'content': usr}],
                            stage='glossary'))
    m = m[0] if isinstance(m, list) and m else (m if isinstance(m, dict) else {})
    names = {k: v for k, v in m.get('names', {}).items() if v and k}
    terms = {k: v for k, v in m.get('terms', {}).items() if v and k}
    for s in ctx.strings:
        nm = name_of(s['jp'])
        if nm and re.fullmatch(r'[？?]+', nm):
            names.setdefault(nm, '？？？')
    gl = {'game': game, 'names': names, 'terms': terms,
          '_note': 'translate.py 자동생성. 사람/에이전트가 WebSearch로 교정 가능.'}
    json.dump(gl, open(out, 'w'), ensure_ascii=False, indent=1)
    return {'status': 'created', 'path': out, 'names': len(names), 'terms': len(terms),
            'unmapped': [n for n in labels if n not in names][:20]}

# ── 이름표 예산-맞춤 해석 ───────────────────────────────────────────
def resolve_labels(ctx):
    glp = os.path.join(ctx.dir, 'glossary.json')
    if not os.path.exists(glp):
        return {'status': 'no_glossary', 'hint': 'glossary 먼저 실행'}
    gl = json.load(open(glp)); names = gl.get('names', {})
    order = sorted(ctx.strings, key=lambda s: s['offset'])
    hit, allc = Counter(), Counter()
    for i, s in enumerate(order):
        nm = name_of(s['jp'])
        if not nm:
            continue
        allc[nm] += 1
        nxt = order[i + 1]['jp'] if i + 1 < len(order) else ''
        if nxt.startswith('＿'):
            hit[nm] += 1
    is_spk = lambda nm: allc[nm] and hit[nm] >= allc[nm] * 0.5
    budget = {}
    for s in ctx.strings:
        nm = name_of(s['jp'])
        if not nm or re.fullmatch(r'[？?]+', nm) or not is_spk(nm):
            continue
        budget[nm] = min(budget.get(nm, 99), max_chars(s['jp'], s['byte_budget']))
    need = []
    for jp, mc in budget.items():
        cur = names.get(jp)
        if cur and len(cur) <= mc and cur[0] >= '가':
            continue
        need.append({'jp': jp, 'glossary': cur or '', 'max_chars': mc})
    questions = []
    if need:
        sysmsg = ('너는 일본 게임 한국 로컬라이징 전문가다. 좁은 글자수 안에서 한국인이 흔히 부르는 '
                  '별칭/약칭으로 축약한다. max_chars 이하 순한글. 정발/팬덤 통용이면 sure=true, '
                  '임의축약이면 sure=false. JSON배열만: [{"jp":..,"kr":..,"sure":bool,"reason":..}]')
        usr = (f'게임:『{gl.get("game","")}』\n확정명 참고:{json.dumps(dict(list(names.items())[:30]), ensure_ascii=False)}\n'
               f'해석대상:\n{json.dumps(need, ensure_ascii=False)}')
        for it in _loads_lenient(chat([{'role': 'system', 'content': sysmsg}, {'role': 'user', 'content': usr}],
                                      max_tokens=4000, stage='labels')):
            jp, kr, sure = it.get('jp'), (it.get('kr') or '').strip(), it.get('sure')
            mc = budget.get(jp, 3)
            if kr and len(kr) <= mc and sure:
                names[jp] = kr
            else:
                questions.append({'jp': jp, 'glossary': names.get(jp, ''), 'max_chars': mc,
                                  'candidate': kr, 'reason': it.get('reason', '')})
    gl['names'] = names
    json.dump(gl, open(glp, 'w'), ensure_ascii=False, indent=1)
    json.dump(questions, open(os.path.join(ctx.dir, 'label_questions.json'), 'w'), ensure_ascii=False, indent=1)
    return {'status': 'ok', 'resolved': len(need) - len(questions), 'questions': questions}

# ── 번역 파이프라인 ─────────────────────────────────────────────────
def _glossary(ctx):
    glp = os.path.join(ctx.dir, 'glossary.json')
    gl = json.load(open(glp)) if os.path.exists(glp) else {'names': {}, 'terms': {}}
    names = dict(gl.get('names', {}))
    terms = gl.get('terms', {})
    gtext = ' / '.join(f'{k}={v}' for k, v in {**names, **terms}.items()) or '(없음)'
    return names, gtext

CTRL_RULE = ('제어토큰(원문과 같은 위치·개수 100% 보존, 번역·삭제·이동 금지): '
             '＠=개행 ￥=대기 ＊=문장끝 ＄=종료 ＿=행머리공백 &0~&F/&&=색 x/+/- 0~3=자간·위치')

def stage_a(ctx, window, names, gtext):
    sysmsg = (f'너는 게임 한국어 번역가다. 대화 한 장면(여러 줄)을 통째로 받는다. 이름표로 화자를 파악하고 '
              f'화자별 말투를 일관되게 유지하며 자연스러운 한국어 구어체로 번역하라.\n용어집: {gtext}\n{CTRL_RULE}\n'
              '한자·가나 금지(용어집 이름은 정해진 한글로, 같은 화자명은 장면 내내 동일). '
              '각 줄 max 글자수 이하(넘치면 짧게 의역). JSON배열만: [{"id":"<offset_hex>","kr":".."}]')
    payload = [{'id': s['offset_hex'], 'jp': s['jp'], 'max': s['byte_budget'] // 2} for s in window]
    arr = _loads_lenient(chat([{'role': 'system', 'content': sysmsg},
                               {'role': 'user', 'content': '장면(줄순서=대사순서):\n' + json.dumps(payload, ensure_ascii=False)}],
                              stage='draft'))
    return {d['id']: d['kr'] for d in arr if isinstance(d, dict) and 'id' in d}

def pin_names(ctx, window, draft, names, name_seen):
    for i, s in enumerate(window):
        nm = name_of(s['jp'])
        if nm is None:
            continue
        if nm in names:
            fixed = names[nm]
        else:
            nxt = window[i + 1]['jp'] if i + 1 < len(window) else ''
            if not nxt.startswith('＿'):
                continue
            fixed = name_seen.setdefault(nm, draft.get(s['offset_hex'], nm))
        cand = fixed + ('＠' if s['jp'].endswith('＠') else '')
        n, bad = ctx.enc_len(cand)
        if n <= s['byte_budget'] and not bad:
            draft[s['offset_hex']] = cand

def stage_b(ctx, jp, kr, budget, src_ctrl):
    sysmsg = f'너는 게임 대사 자수 최적화기다. 뜻 유지하되 더 짧게 의역.\n{CTRL_RULE}\n번역문만 한 줄 출력.'
    for _ in range(3):
        n, bad = ctx.enc_len(kr)
        cur = ctx.ms(kr)
        if n <= budget and not bad and cur == src_ctrl:
            return kr, True, None
        fb = []
        if n > budget:
            fb.append(f'현재 {n // 2}자→최대 {budget // 2}자 이하로 짧게')
        if bad:
            fb.append(f'사용불가문자 {"".join(sorted(set(bad)))} 제거')
        if cur != src_ctrl:
            fb.append(f'제어토큰 원문={src_ctrl} 위치·개수 맞출 것')
        kr = chat([{'role': 'system', 'content': sysmsg},
                   {'role': 'user', 'content': f'원문:{jp}\n현재:{kr}\n최대 {budget // 2}자.\n' + '. '.join(fb) + '. 번역문만'}],
                  temp=0.4, max_tokens=200, stage='refine').splitlines()[-1].strip().strip('"「」')
    n, bad = ctx.enc_len(kr)
    ok = n <= budget and not bad and ctx.ms(kr) == src_ctrl
    return kr, ok, (None if ok else 'budget/char/ctrl')

def make_windows(strings, cap=24, soft=18, gap=0x80):
    wins, cur, prev = [], [], None
    for s in strings:
        newscene = prev is not None and s['offset'] - prev > gap
        boundary = len(cur) >= soft and name_of(s['jp']) is not None
        if cur and (newscene or boundary or len(cur) >= cap):
            wins.append(cur); cur = []
        cur.append(s); prev = s['end']
    if cur:
        wins.append(cur)
    return wins

def run_pipeline(ctx, scope='all', start=None, lines=24, apply=False, workers=6):
    names, gtext = _glossary(ctx)
    strings = sorted(ctx.strings, key=lambda s: s['offset'])
    if scope != 'all' and start is not None:
        i0 = next(i for i, s in enumerate(strings) if s['offset'] == start)
        wins = [strings[i0:i0 + lines]]
    else:
        wins = make_windows(strings)
    lock = threading.Lock()
    drafts = [None] * len(wins)

    def sa(iw):
        i, w = iw
        try:
            return i, stage_a(ctx, w, names, gtext)
        except Exception:
            return i, {s['offset_hex']: (s.get('kr') or s['jp']) for s in w}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, d in ex.map(sa, enumerate(wins)):
            drafts[i] = d
    name_seen, merged = {}, {}
    for w, d in zip(wins, drafts):
        pin_names(ctx, w, d, names, name_seen)
        merged.update(d)

    pool = [s for w in wins for s in w]
    report = [None] * len(pool)

    def sb(iw):
        i, s = iw
        oid = s['offset_hex']
        try:
            kr, ok, fail = stage_b(ctx, s['jp'], merged.get(oid, s.get('kr') or ''), s['byte_budget'], ctx.ms(s['jp']))
        except Exception as e:
            kr, ok, fail = (s.get('kr') or ''), False, str(e)[:60]
        return i, {'id': oid, 'jp': s['jp'], 'budget': s['byte_budget'], 'baseline': s.get('kr'),
                   'pipeline': kr, 'changed': s.get('kr') != kr, 'ok': ok, 'fail': fail}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, r in ex.map(sb, enumerate(pool)):
            report[i] = r

    outp = os.path.join(ctx.dir, 'pipeline_report.json')
    json.dump(report, open(outp, 'w'), ensure_ascii=False, indent=1)
    res = {'windows': len(wins), 'lines': len(report),
           'changed': sum(r['changed'] for r in report),
           'fails': sum(not r['ok'] for r in report), 'report': outp, 'applied': 0}
    if apply:
        bak = ctx.jpath + '.bak-pre-pipeline'
        if not os.path.exists(bak):
            shutil.copy(ctx.jpath, bak)
        by = {r['id']: r for r in report if r['ok']}
        n = 0
        for s in ctx.strings:
            r = by.get(s['offset_hex'])
            if r and r['pipeline'] != s.get('kr'):
                s['kr'] = r['pipeline']; s.pop('needs_review', None); s['kr_source'] = 'pipeline_v1'; n += 1
        json.dump(ctx.data, open(ctx.jpath, 'w'), ensure_ascii=False, indent=1)
        res['applied'] = n; res['backup'] = bak
    return res

# ── CLI ─────────────────────────────────────────────────────────────
def main():
    a = sys.argv
    if len(a) < 3:
        print('usage: translate.py <project_dir> glossary|labels|run [flags]'); sys.exit(2)
    pdir, action = a[1], a[2]
    if not KEY:
        print(json.dumps({'error': 'DEEPSEEK_API_KEY 없음'})); sys.exit(1)
    ctx = Ctx(pdir)
    if action == 'glossary':
        game = a[a.index('--game') + 1] if '--game' in a else None
        r = build_glossary(ctx, game, '--force' in a)
    elif action == 'labels':
        r = resolve_labels(ctx)
    elif action == 'run':
        scope = 'all' if '--scope' in a and a[a.index('--scope') + 1] == 'all' else ('all' if '--all' in a else 'scene')
        start = int(a[a.index('--start') + 1], 16) if '--start' in a else None
        if start is not None:
            scope = 'scene'
        lines = int(a[a.index('--lines') + 1]) if '--lines' in a else 24
        r = run_pipeline(ctx, scope, start, lines, '--apply' in a)
    else:
        print('unknown action'); sys.exit(2)
    print(json.dumps(r, ensure_ascii=False, indent=1))

if __name__ == '__main__':
    main()
