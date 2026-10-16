import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path


class ResearchError(Exception):
    def __init__(self, code, detail, status=400):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(detail)


class ResearchStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.database() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS papers (key TEXT PRIMARY KEY, metadata TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
              CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, paper_key TEXT NOT NULL REFERENCES papers(key),
                kind TEXT NOT NULL, title TEXT NOT NULL, file_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
              CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, paper_key TEXT NOT NULL REFERENCES papers(key),
                kind TEXT NOT NULL, status TEXT NOT NULL, artifact_id TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
              CREATE TABLE IF NOT EXISTS personal (paper_key TEXT NOT NULL REFERENCES papers(key), user_id TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '', list_status TEXT NOT NULL DEFAULT 'none', starred INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(paper_key,user_id));
            ''')
            migrations = {
                'papers': {'document_id': 'INTEGER', 'instance': "TEXT NOT NULL DEFAULT ''"},
                'personal': {'reader_state': "TEXT NOT NULL DEFAULT '{}'", 'last_opened': "TEXT NOT NULL DEFAULT ''", 'note_revision': 'INTEGER NOT NULL DEFAULT 0'},
                'artifacts': {'provenance': "TEXT NOT NULL DEFAULT 'manual_import_no_model'",
                              'options_hash': "TEXT NOT NULL DEFAULT ''", 'checksum': "TEXT NOT NULL DEFAULT ''"},
                'tasks': {'options_hash': "TEXT NOT NULL DEFAULT ''", 'payload': "TEXT NOT NULL DEFAULT '{}'",
                          'checkpoint': "TEXT NOT NULL DEFAULT '{}'", 'attempt': 'INTEGER NOT NULL DEFAULT 0',
                          'stage': "TEXT NOT NULL DEFAULT ''", 'error_code': "TEXT NOT NULL DEFAULT ''",
                          'error_detail': "TEXT NOT NULL DEFAULT ''", 'provenance': "TEXT NOT NULL DEFAULT 'manual_import_no_model'"},
            }
            db.execute('BEGIN IMMEDIATE')
            for table, columns in migrations.items():
                existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
                for column, declaration in columns.items():
                    if column not in existing:
                        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_research_task ON tasks(paper_key,kind,options_hash) "
                       "WHERE status IN ('queued','running')")

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.directory / 'research.sqlite3', timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def associate(self, key, payload):
        with self.database() as db:
            existing = db.execute('SELECT metadata FROM papers WHERE key=?', (key,)).fetchone()
            metadata = json.loads(existing['metadata']) if existing else {'title': str(payload['title'])[:512]}
            db.execute('INSERT INTO papers(key,metadata,document_id,instance) VALUES(?,?,?,?) '
                       'ON CONFLICT(key) DO UPDATE SET document_id=excluded.document_id,instance=excluded.instance',
                       (key, json.dumps(metadata, ensure_ascii=False), payload['document_id'], payload['instance']))

    def state(self, key, user_id, db=None):
        with (nullcontext(db) if db is not None else self.database()) as db:
            row = db.execute('SELECT metadata FROM papers WHERE key=?', (key,)).fetchone()
            artifacts = [dict(r) for r in db.execute('SELECT id,kind,title,provenance,created_at FROM artifacts '
                                                    'WHERE paper_key=? ORDER BY created_at', (key,))]
            tasks = [dict(r) for r in db.execute('SELECT id,kind,status,artifact_id,attempt,stage,error_code,error_detail,'
                                                'provenance,created_at,checkpoint FROM tasks WHERE paper_key=? ORDER BY created_at DESC', (key,))]
            for task in tasks:
                task['usage'] = json.loads(task.pop('checkpoint')).get('usage', {})
            personal = db.execute('SELECT notes,list_status,starred,reader_state,last_opened,note_revision FROM personal WHERE paper_key=? AND user_id=?',
                                  (key, user_id)).fetchone()
        return {'associated': row is not None, 'bibliography': json.loads(row['metadata']) if row else {},
                'personal': {**dict(personal), 'reader_state': json.loads(personal['reader_state'])} if personal else {'notes': '', 'list_status': 'none', 'starred': False, 'reader_state': {}, 'last_opened': '', 'note_revision': 0},
                'artifacts': artifacts, 'tasks': tasks}

    def save_artifact(self, key, kind, content, title, provenance, options_hash='', db=None):
        if not content or len(content) > 10 * 1024 * 1024:
            raise ResearchError('result_size', '产物为空或超过 10 MiB。')
        if kind == 'figure_png':
            import struct
            if len(content) < 24 or not content.startswith(b'\x89PNG\r\n\x1a\n') or max(struct.unpack('>II', content[16:24])) > 4096:
                raise ResearchError('invalid_image', '只支持尺寸不超过4096的PNG图片。')
            suffix = '.png'
        elif kind.endswith('_pdf'):
            from pypdf import PdfReader
            import io
            try:
                if not content.startswith(b'%PDF-') or not PdfReader(io.BytesIO(content)).pages:
                    raise ValueError()
            except Exception:
                raise ResearchError('invalid_pdf', '产物不是可读取的 PDF。') from None
            suffix = '.pdf'
        else:
            if len(content) > 1024 * 1024:
                raise ResearchError('result_size', 'Markdown 超过 1 MiB。')
            try:
                content.decode('utf-8')
            except UnicodeDecodeError:
                raise ResearchError('invalid_markdown', 'Markdown 必须为 UTF-8。') from None
            suffix = '.md'
        artifact_id = uuid.uuid4().hex
        file_name = artifact_id + suffix
        temporary = self.directory / (file_name + '.tmp')
        temporary.write_bytes(content)
        temporary.replace(self.directory / file_name)
        values = (artifact_id, key, kind, title, file_name, provenance, options_hash, hashlib.sha256(content).hexdigest())
        sql = 'INSERT INTO artifacts(id,paper_key,kind,title,file_name,provenance,options_hash,checksum) VALUES(?,?,?,?,?,?,?,?)'
        if db is not None:
            db.execute(sql, values)
        else:
            with self.database() as connection:
                connection.execute(sql, values)
        return artifact_id

    def existing_result(self, key, kind, options_hash, require_model=False):
        artifact_kind = 'translated_pdf' if kind == 'translation' else 'analysis_markdown'
        with self.database() as db:
            rows = db.execute('SELECT * FROM artifacts WHERE paper_key=? AND kind=? AND '
                              '(options_hash=? OR options_hash=\'\') ORDER BY created_at DESC',
                              (key, artifact_kind, options_hash)).fetchall()
        for row in rows:
            if require_model and (row['provenance'] != 'model_generated' or row['options_hash'] != options_hash):
                continue
            path = self.directory / row['file_name']
            if path.is_file() and (not row['checksum'] or hashlib.sha256(path.read_bytes()).hexdigest() == row['checksum']):
                return dict(row)
        return None

    def enqueue(self, key, payload, enabled):
        kind = payload.get('kind')
        if kind not in {'translation', 'analysis'}:
            raise ResearchError('invalid_kind', '请选择翻译或解读。')
        options_hash = hashlib.sha256(json.dumps(payload['options'], sort_keys=True).encode()).hexdigest()
        if not payload.get('force'):
            result = self.existing_result(key, kind, options_hash, require_model=enabled)
            if result:
                return {'reused': True, 'artifact_id': result['id'], 'provenance': result['provenance']}, 200
        if not enabled:
            raise ResearchError('ai_disabled', '外部 AI 调用关闭；代码后台已接入，未创建付费任务。', 409)
        if payload.get('confirm_external') is not True:
            raise ResearchError('confirmation_required', '请手动确认对本篇论文进行一次外部处理。', 409)
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute("SELECT id,status FROM tasks WHERE paper_key=? AND kind=? AND options_hash=? "
                                "AND status IN ('queued','running','cancel_requested')", (key, kind, options_hash)).fetchone()
            if active:
                return {'reused': True, 'task_id': active['id'], 'status': active['status']}, 200
            if payload.get('retry_task_id'):
                row = db.execute('SELECT * FROM tasks WHERE id=? AND paper_key=? AND kind=? AND options_hash=?',
                                 (payload['retry_task_id'], key, kind, options_hash)).fetchone()
                if not row or row['status'] not in {'failed', 'interrupted', 'cancelled'}:
                    raise ResearchError('retry_invalid', '只可手动重试同参数的失败、中断或已取消任务。', 409)
                db.execute("UPDATE tasks SET status='queued',error_code='',error_detail='',stage='queued',payload=? WHERE id=?",
                           (json.dumps(payload), row['id']))
                return {'task_id': row['id'], 'status': 'queued', 'reused': False}, 202
            previous = db.execute('SELECT id,status FROM tasks WHERE paper_key=? AND kind=? AND options_hash=? '
                                  'ORDER BY created_at DESC LIMIT 1', (key, kind, options_hash)).fetchone()
            if previous and previous['status'] in {'failed', 'interrupted'} and not payload.get('force'):
                return {'task_id': previous['id'], 'status': previous['status'], 'reused': True,
                        'detail': '任务未完成，请查看原因并手动重试；不会自动重复调用。'}, 200
            task_id = uuid.uuid4().hex
            db.execute("INSERT INTO tasks(id,paper_key,kind,status,options_hash,payload,stage,provenance) "
                       "VALUES(?,?,?,'queued',?,?,'queued','model_generated')",
                       (task_id, key, kind, options_hash, json.dumps(payload)))
        return {'task_id': task_id, 'status': 'queued', 'reused': False}, 202

    def claim(self):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET status='running',stage='authorize',attempt=attempt+1 WHERE id=?", (row['id'],))
            task = dict(row)
            task['payload'] = json.loads(task['payload'])
            task['checkpoint'] = json.loads(task['checkpoint'])
            return task

    def checkpoint(self, task_id, stage, checkpoint):
        with self.database() as db:
            status = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()['status']
            if status in {'cancel_requested', 'cancelled'}:
                raise ResearchError('cancelled', '任务已取消；已发出的外部请求不能保证立即停止或退费。')
            db.execute('UPDATE tasks SET stage=?,checkpoint=? WHERE id=?', (stage, json.dumps(checkpoint), task_id))

    def fail(self, task_id, error):
        with self.database() as db:
            db.execute("UPDATE tasks SET status=?,error_code=?,error_detail=? WHERE id=?",
                       ('cancelled' if error.code == 'cancelled' else 'failed', error.code, error.detail, task_id))

    def cancel(self, key, task_id):
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM tasks WHERE paper_key=? AND id=?', (key, task_id)).fetchone()
            if not row:
                raise ResearchError('task_missing', '任务不存在。', 404)
            if row['status'] not in {'queued', 'running', 'cancel_requested', 'cancelled'}:
                raise ResearchError('cancel_invalid', '已完成或失败的任务不能取消。', 409)
            status = 'cancel_requested' if row['status'] in {'running', 'cancel_requested'} else 'cancelled'
            db.execute('UPDATE tasks SET status=? WHERE id=?', (status, task_id))
        return {'task_id': task_id, 'status': status}

    def recover(self):
        with self.database() as db:
            db.execute("UPDATE tasks SET status='cancelled',error_code='cancelled',error_detail='取消期间后台重启，未自动恢复外部调用。' WHERE status='cancel_requested'")
            return db.execute("UPDATE tasks SET status='interrupted',error_code='worker_interrupted',"
                              "error_detail='后台中断，可能已发生外部调用；请确认后手动重试。' WHERE status='running'").rowcount
