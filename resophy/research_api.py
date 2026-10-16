"""Private Paperless research API; model work is only performed by the worker."""
import base64
import hashlib
import hmac
import json
import os
import uuid
from urllib.parse import urlparse

import bibtexparser
from bibtexparser.bibdatabase import BibDatabase
from bibtexparser.bwriter import BibTexWriter
from flask import Flask, abort, jsonify, request, send_file

from resophy.core.base_paper import Paper
from resophy.research_store import ResearchError, ResearchStore

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
store = ResearchStore(os.environ.get('RESEARCH_DATA_DIR', '/data'))
secret = os.environ['RESEARCH_SHARED_SECRET']
if len(secret) < 32:
    raise RuntimeError('Research internal secret must be at least 32 characters')
BOOK_FIELDS = {'title', 'authors', 'year', 'journal', 'doi', 'abstract', 'keywords', 'arxiv_id', 'bibtex'}


def options(payload=None):
    with store.database() as db:
        saved = db.execute('SELECT settings FROM user_settings WHERE user_id=?', (request.headers['X-Research-User'],)).fetchone()
    preferences = json.loads(saved['settings']) if saved else {}
    preferences.update({name: value for name, value in (payload or {}).items() if name in {'model', 'output_language', 'analysis_template'}})
    allowed = os.environ.get('RESEARCH_ALLOWED_MODELS', 'deepseek-chat').split(',')
    model = preferences.get('model') or os.environ.get('RESEARCH_MODEL', 'deepseek-chat')
    language = preferences.get('output_language', 'zh')
    template = preferences.get('analysis_template', '')
    if model not in allowed or language not in {'zh', 'en'} or not isinstance(template, str) or len(template) > 8000:
        raise ResearchError('settings_invalid', '模型、输出语言或模板无效；未发起模型调用。')
    configured = {'model': os.environ.get('RESEARCH_MODEL', 'deepseek-chat'),
            'base_url': os.environ.get('RESEARCH_MODEL_BASE_URL', 'https://api.deepseek.com/v1'),
            'parser': os.environ.get('RESEARCH_ANALYSIS_PARSER', 'local_pdf'),
            'source_lang': 'en', 'target_lang': 'zh', 'prompt_version': 'research-v2', 'babeldoc': '0.6.3'}
    configured.update(model=model, target_lang=language, analysis_template=template)
    url = urlparse(configured['base_url'])
    if url.scheme != 'https' or url.username or url.password or url.query or url.fragment:
        raise ResearchError('provider_url', '模型地址必须是无凭证的 HTTPS 地址。')
    return configured


@app.before_request
def authenticate():
    if not hmac.compare_digest(request.headers.get('X-Research-Secret', '').encode(), secret.encode()):
        abort(401)
    if request.method == 'POST' and request.is_json and not isinstance(request.get_json(silent=True), dict):
        abort(400)
    if not request.headers.get('X-Research-User', '').isdigit() and request.path != '/health':
        abort(401)
    if request.view_args and 'key' in request.view_args:
        key = request.view_args['key']
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            abort(400)


@app.errorhandler(ResearchError)
def research_error(error):
    return jsonify(code=error.code, detail=error.detail), error.status


@app.get('/health')
def health():
    with store.database() as db:
        db.execute('SELECT 1').fetchone()
    return jsonify(status='ok', ai_enabled=os.environ.get('RESEARCH_AI_ENABLED') == 'true')


@app.route('/papers/<key>', methods=['GET', 'POST'])
def paper(key):
    if request.method == 'POST':
        store.associate(key, request.get_json())
    state = store.state(key, request.headers['X-Research-User'])
    state['pending_metadata'] = state['bibliography'].get('_pending_metadata', {})
    state['manual_fields'] = state['bibliography'].get('_manual_fields', [])
    state['bibliography'] = {name: value for name, value in state['bibliography'].items() if name in BOOK_FIELDS}
    state.update(paper_key=key, ai_enabled=os.environ.get('RESEARCH_AI_ENABLED') == 'true',
                 disabled_reason='外部 AI 调用关闭；已有结果直接读取，后台代码已接入，真实模型尚未验证。'
                 if os.environ.get('RESEARCH_AI_ENABLED') != 'true' else '仅手动确认本篇后创建任务，默认复用已有结果。')
    target = urlparse(os.environ.get('RESEARCH_MODEL_BASE_URL', 'https://api.deepseek.com/v1')).hostname or '未配置模型服务'
    state['processing_notice'] = ('翻译会向 ' + target + ' 发送论文文本；解读也会发送提取文本。'
                                  + ('当前解读解析模式还会把原始 PDF 上传至 MinerU。' if os.environ.get('RESEARCH_ANALYSIS_PARSER') in {'mineru_cloud', 'mineru_agent'}
                                     else '当前解读先在本地提取 PDF 文本。'))
    return jsonify(state)


@app.post('/papers/<key>/tasks')
def create_task(key):
    payload = request.get_json()
    encoded = payload.pop('input_pdf', None)
    payload['options'] = options(payload)
    payload['paper_key'] = key
    payload['user_id'] = int(request.headers['X-Research-User'])
    if not payload.get('force') and payload.get('kind') in {'translation', 'analysis'}:
        fingerprint = hashlib.sha256(json.dumps(payload['options'], sort_keys=True).encode()).hexdigest()
        result = store.existing_result(key, payload['kind'], fingerprint,
                                       require_model=os.environ.get('RESEARCH_AI_ENABLED') == 'true')
        if result:
            return jsonify(reused=True, artifact_id=result['id'], provenance=result['provenance'])
    if os.environ.get('RESEARCH_AI_ENABLED') != 'true':
        raise ResearchError('ai_disabled', '外部 AI 调用关闭；代码后台已接入，未创建付费任务。', 409)
    if payload.get('confirm_external') is not True:
        raise ResearchError('confirmation_required', '请手动确认对本篇论文进行一次外部处理。', 409)
    try:
        content = base64.b64decode(encoded or '', validate=True)
    except ValueError:
        raise ResearchError('input_invalid', '任务输入编码无效。') from None
    if len(content) > 10 * 1024 * 1024 or not content.startswith(b'%PDF-'):
        raise ResearchError('input_invalid', '任务仅接受不超过10MiB的PDF。')
    if hashlib.sha256(content).hexdigest() != payload.get('source_sha256'):
        raise ResearchError('input_invalid', '任务输入校验不匹配。')
    inputs = store.directory / 'inputs'
    inputs.mkdir(exist_ok=True)
    temporary = inputs / (uuid.uuid4().hex + '.tmp')
    temporary.write_bytes(content)
    temporary.replace(inputs / (key + '.pdf'))
    result, status = store.enqueue(key, payload, True)
    return jsonify(result), status


@app.post('/papers/<key>/artifacts')
def import_artifact(key):
    kind = request.headers.get('X-Artifact-Kind')
    if kind not in {'translated_pdf', 'chinese_pdf', 'reference_pdf', 'analysis_markdown', 'figure_png'}:
        abort(400)
    with store.database() as db:
        if db.execute('SELECT key FROM papers WHERE key=?', (key,)).fetchone() is None:
            abort(404)
        artifact_id = store.save_artifact(key, kind, request.get_data(), '人工导入（未调用模型）', 'manual_import_no_model', db=db)
        db.execute('INSERT INTO tasks(id,paper_key,kind,status,artifact_id) VALUES (?,?,?,?,?)',
                   (uuid.uuid4().hex, key, 'manual_import', 'completed', artifact_id))
    return jsonify(id=artifact_id, provenance='manual_import_no_model'), 201


@app.post('/papers/<key>/tasks/<task_id>/cancel')
def cancel_task(key, task_id):
    return jsonify(store.cancel(key, task_id))


@app.get('/tasks')
def all_tasks():
    offset = max(0, request.args.get('offset', default=0, type=int))
    with store.database() as db:
        rows = db.execute('SELECT p.key,p.document_id,p.instance,p.metadata,t.id,t.kind,t.status,t.stage,t.attempt,t.error_detail,t.created_at,t.provenance '
                          'FROM tasks t JOIN papers p ON p.key=t.paper_key ORDER BY t.created_at DESC LIMIT 100 OFFSET ?', (offset,)).fetchall()
    return jsonify(results=[{**dict(row), 'metadata': json.loads(row['metadata'])} for row in rows], next_offset=offset+100 if len(rows)==100 else None)


@app.get('/papers/<key>/artifacts/<artifact_id>')
def artifact(key, artifact_id):
    with store.database() as db:
        row = db.execute('SELECT * FROM artifacts WHERE paper_key=? AND id=?', (key, artifact_id)).fetchone()
    if row is None:
        abort(404)
    mime = 'image/png' if row['kind'] == 'figure_png' else 'application/pdf' if row['kind'].endswith('_pdf') else 'text/plain; charset=utf-8'
    return send_file(store.directory / row['file_name'], mimetype=mime, conditional=True)


@app.post('/papers/<key>/bibliography')
def bibliography(key):
    payload = request.get_json()
    current = store.state(key, request.headers['X-Research-User'])
    if not current['associated']:
        abort(404)
    metadata = current['bibliography']
    before = dict(metadata)
    accepted = payload.get('accept_fields')
    if accepted is not None:
        pending = metadata.get('_pending_metadata', {})
        if not isinstance(accepted, list) or not accepted or any(name not in BOOK_FIELDS for name in accepted) or not pending:
            raise ResearchError('metadata_confirmation', '请选择有效的待确认字段。')
        payload = {name: pending[name] for name in accepted if name in pending}
        metadata['_pending_metadata'] = {}
    imported = bool(payload.get('bibtex_import'))
    if payload.get('bibtex_import'):
        if not isinstance(payload['bibtex_import'], str) or len(payload['bibtex_import']) > 100000:
            raise ResearchError('bibtex_invalid', 'BibTeX格式或长度无效。')
        try:
            parsed = bibtexparser.loads(payload['bibtex_import'])
        except Exception:
            raise ResearchError('bibtex_invalid', '无法读取 BibTeX，书目未修改。') from None
        if len(parsed.entries) != 1:
            raise ResearchError('bibtex_invalid', '请选择只含一篇论文的有效 BibTeX。')
        entry = parsed.entries[0]
        payload = {name: entry.get('author' if name == 'authors' else name, '') for name in BOOK_FIELDS if name != 'bibtex'}
        payload['bibtex'] = request.get_json()['bibtex_import']
        if request.get_json().get('preview_only') is True:
            metadata['_pending_metadata'] = payload
            with store.database() as db:
                db.execute('UPDATE papers SET metadata=? WHERE key=?', (json.dumps(metadata, ensure_ascii=False), key))
            return jsonify(bibliography={name: value for name, value in metadata.items() if name in BOOK_FIELDS}, pending_metadata=payload,
                           manual_fields=metadata.get('_manual_fields', []))
    for name in BOOK_FIELDS & payload.keys():
        value = payload[name]
        if not isinstance(value, str) or len(value) > (100000 if name in {'abstract', 'bibtex'} else 2000):
            raise ResearchError('bibliography_invalid', '书目信息格式或长度无效。')
        metadata[name] = value
    metadata['_manual_fields'] = sorted(set(metadata.get('_manual_fields', [])) | (BOOK_FIELDS & payload.keys()))
    if not imported and any(metadata.get(name) != before.get(name) for name in BOOK_FIELDS - {'bibtex'}):
        metadata['bibtex'] = ''
    normalized = Paper.from_dict({name: value for name, value in metadata.items() if name != 'doi'})
    metadata['title'] = normalized.title
    with store.database() as db:
        db.execute('UPDATE papers SET metadata=? WHERE key=?', (json.dumps(metadata, ensure_ascii=False), key))
    return jsonify(bibliography={name: value for name, value in metadata.items() if name in BOOK_FIELDS}, pending_metadata=metadata.get('_pending_metadata', {}), manual_fields=metadata['_manual_fields'])


@app.get('/papers/<key>/bibtex')
def bibtex(key):
    state = store.state(key, request.headers['X-Research-User'])
    if not state['associated']:
        abort(404)
    metadata = state['bibliography']
    if metadata.get('bibtex'):
        output = metadata['bibtex']
    else:
        database = BibDatabase()
        database.entries = [{'ENTRYTYPE': 'article', 'ID': 'paper_' + key[:12],
                             **{('author' if name == 'authors' else name): metadata[name]
                                for name in ('title', 'authors', 'year', 'journal', 'doi') if metadata.get(name)}}]
        output = BibTexWriter().write(database)
    return app.response_class(output, mimetype='application/x-bibtex',
                              headers={'Content-Disposition': 'attachment; filename="paper.bib"'})


@app.post('/papers/<key>/personal')
def personal(key):
    payload = request.get_json()
    user_id = request.headers['X-Research-User']
    state = store.state(key, user_id)
    if not state['associated']:
        abort(404)
    values = {**state['personal'], **{name: payload[name] for name in ('notes', 'list_status', 'starred') if name in payload}}
    if (not isinstance(values['notes'], str) or len(values['notes']) > 20000
            or values['list_status'] not in {'none', 'inbox', 'reading', 'completed'}
            or values['starred'] not in (False, True, 0, 1)):
        raise ResearchError('personal_invalid', '笔记或阅读列表状态无效。')
    with store.database() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT note_revision FROM personal WHERE paper_key=? AND user_id=?', (key, user_id)).fetchone()
        if 'notes' in payload and payload.get('note_revision') is not None and payload['note_revision'] != (current['note_revision'] if current else 0):
            raise ResearchError('note_conflict', '笔记已在另一处更新，当前草稿保留，请先核对后再保存。', 409)
        updates = [f'{name}=excluded.{name}' for name in ('notes', 'list_status', 'starred') if name in payload]
        updates.append("updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        db.execute('INSERT INTO personal(paper_key,user_id,notes,list_status,starred) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(paper_key,user_id) DO UPDATE SET ' + ','.join(updates),
                   (key, user_id, values['notes'], values['list_status'], int(values['starred'])))
        if 'notes' in payload:
            db.execute('UPDATE personal SET note_revision=note_revision+1 WHERE paper_key=? AND user_id=?', (key, user_id))
    return jsonify(personal=store.state(key, user_id)['personal'])


@app.get('/library')
def library():
    offset = max(0, request.args.get('offset', default=0, type=int))
    with store.database() as db:
        rows = db.execute("SELECT p.key,p.document_id,p.instance,p.metadata,n.list_status,n.starred,n.last_opened,n.reader_state FROM papers p "
                          "JOIN personal n ON n.paper_key=p.key WHERE n.user_id=? AND (n.list_status!='none' OR n.starred=1 OR n.last_opened!='') "
                          'ORDER BY n.updated_at DESC,p.key LIMIT 100 OFFSET ?', (request.headers['X-Research-User'], offset)).fetchall()
        days = {row['key']: [dict(day) for day in db.execute("SELECT day,SUM(seconds) AS seconds,SUM(CASE WHEN session LIKE 'archive:%' THEN seconds ELSE 0 END) AS imported_seconds FROM reading_events WHERE paper_key=? AND user_id=? GROUP BY day",
                                                           (row['key'], request.headers['X-Research-User']))] for row in rows}
    return jsonify(results=[{**dict(row), 'metadata': json.loads(row['metadata']), 'reading_days': days[row['key']]} for row in rows],
                   next_offset=offset + 100 if len(rows) == 100 else None)


with store.database() as db:
    db.execute('CREATE TABLE IF NOT EXISTS user_settings (user_id TEXT PRIMARY KEY,settings TEXT NOT NULL)')
    db.executescript('''CREATE TABLE IF NOT EXISTS reading_events (user_id TEXT NOT NULL,paper_key TEXT NOT NULL,
      session TEXT NOT NULL,sequence INTEGER NOT NULL,day TEXT NOT NULL,seconds INTEGER NOT NULL,
      PRIMARY KEY(user_id,session,sequence));
      CREATE TABLE IF NOT EXISTS reading_clock (user_id TEXT PRIMARY KEY,tick REAL NOT NULL);''')


@app.post('/papers/<key>/reading')
def reading(key):
    import time
    import datetime
    from zoneinfo import ZoneInfo
    payload = request.get_json()
    user = request.headers['X-Research-User']
    if not store.state(key, user)['associated']:
        abort(404)
    session = payload.get('session', '')
    sequence = payload.get('sequence', 0)
    reader_state = payload.get('reader_state', {})
    if (not isinstance(session, str) or len(session) != 32 or any(c not in '0123456789abcdef' for c in session)
            or not isinstance(sequence, int) or sequence < 0 or not isinstance(reader_state, dict)
            or len(json.dumps(reader_state)) > 16000):
        raise ResearchError('reading_invalid', '阅读记录格式无效。')
    now = time.time()
    with store.database() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT tick FROM reading_clock WHERE user_id=?', (user,)).fetchone()
        delta = payload.get('seconds', 0)
        if not isinstance(delta, (int, float)) or not 0 <= delta <= 20:
            raise ResearchError('reading_invalid', '阅读时间间隔无效。')
        seconds = int(min(delta, max(0, now - previous['tick']), 20)) if previous else 0
        day = datetime.datetime.fromtimestamp(now, ZoneInfo(os.environ.get('RESEARCH_TIME_ZONE', 'Asia/Shanghai'))).date().isoformat()
        inserted = db.execute('INSERT OR IGNORE INTO reading_events VALUES(?,?,?,?,?,?)', (user, key, session, sequence, day, seconds)).rowcount
        if inserted:
            if delta > 0 or previous is None:
                db.execute('INSERT INTO reading_clock VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET tick=excluded.tick', (user, now))
            db.execute('INSERT INTO personal(paper_key,user_id) VALUES(?,?) ON CONFLICT DO NOTHING', (key, user))
            db.execute("UPDATE personal SET reader_state=?,last_opened=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE paper_key=? AND user_id=?",
                       (json.dumps(reader_state), key, user))
    return jsonify(saved=True, credited_seconds=seconds if inserted else 0)


from resophy.annotation_routes import register_annotations
register_annotations(app, store)
from resophy.research_archive import register_archives
register_archives(app, store)
from resophy.research_collections import register_collections
register_collections(app, store)


@app.route('/settings', methods=['GET', 'POST'])
def research_settings():
    if request.method == 'POST':
        payload = request.get_json()
        configured = options(payload)
        saved = {'model': configured['model'], 'output_language': configured['target_lang'], 'analysis_template': configured['analysis_template']}
        with store.database() as db:
            db.execute('INSERT INTO user_settings VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET settings=excluded.settings',
                       (request.headers['X-Research-User'], json.dumps(saved)))
    configured = options()
    return jsonify(model=configured['model'], output_language=configured['target_lang'], analysis_template=configured['analysis_template'],
                   allowed_models=os.environ.get('RESEARCH_ALLOWED_MODELS', 'deepseek-chat').split(','),
                   ai_enabled=os.environ.get('RESEARCH_AI_ENABLED') == 'true', parser=configured['parser'])
