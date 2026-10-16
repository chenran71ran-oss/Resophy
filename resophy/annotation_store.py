import json
import hashlib
from resophy.research_store import ResearchError


class AnnotationStore:
    def __init__(self, research):
        self.research = research
        with research.database() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS annotation_schema (schema_version INTEGER PRIMARY KEY);
              INSERT OR IGNORE INTO annotation_schema VALUES(1);
              CREATE TABLE IF NOT EXISTS annotation_versions (
                paper_key TEXT NOT NULL REFERENCES papers(key),version_id TEXT NOT NULL,checksum TEXT NOT NULL,
                kind TEXT NOT NULL,pages TEXT NOT NULL,PRIMARY KEY(paper_key,version_id,checksum));
              CREATE TABLE IF NOT EXISTS annotations (
                paper_key TEXT NOT NULL,user_id TEXT NOT NULL,version_id TEXT NOT NULL,checksum TEXT NOT NULL,
                id TEXT NOT NULL,record TEXT NOT NULL,revision INTEGER NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(paper_key,user_id,version_id,checksum,id));
              CREATE TABLE IF NOT EXISTS annotation_operations (
                user_id TEXT NOT NULL,op_id TEXT NOT NULL,fingerprint TEXT NOT NULL,response TEXT NOT NULL,
                PRIMARY KEY(user_id,op_id));
              CREATE TABLE IF NOT EXISTS annotation_drafts (
                user_id TEXT NOT NULL,op_id TEXT NOT NULL,paper_key TEXT NOT NULL,version_id TEXT NOT NULL,
                checksum TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY(user_id,op_id));
            ''')

    def register(self, key, version):
        with self.research.database() as db:
            db.execute('INSERT OR IGNORE INTO annotation_versions VALUES(?,?,?,?,?)',
                       (key, version['id'], version['sha256'], version['kind'], json.dumps(version['pages'])))

    def artifact(self, key, identifier):
        with self.research.database() as db:
            row = db.execute('SELECT kind,checksum FROM artifacts WHERE paper_key=? AND id=?', (key, identifier)).fetchone()
            return dict(row) if row else None

    def export(self, key, user, db):
        rows = [json.loads(r[0]) for r in db.execute('SELECT record FROM annotations WHERE paper_key=? AND user_id=?', (key, user))]
        versions = [{'id': r['version_id'], 'sha256': r['checksum'], 'kind': r['kind'], 'pages': json.loads(r['pages'])}
                    for r in db.execute('SELECT * FROM annotation_versions WHERE paper_key=?', (key,))]
        return {'schema_version': 1, 'versions': versions, 'annotations': rows}

    def archive_draft(self, key, user, version, operation):
        with self.research.database() as db:
            db.execute("UPDATE annotation_drafts SET status='archived' WHERE user_id=? AND op_id=? AND paper_key=? AND version_id=? AND checksum=?",
                       (user, operation, key, version['id'], version['sha256']))

    def listing(self, key, user, version):
        with self.research.database() as db:
            records = [json.loads(row[0]) for row in db.execute(
                'SELECT record FROM annotations WHERE paper_key=? AND user_id=? AND version_id=? AND checksum=? ORDER BY rowid',
                (key, user, version['id'], version['sha256']))]
            drafts = [dict(row) for row in db.execute("SELECT op_id,payload,status FROM annotation_drafts WHERE paper_key=? AND user_id=? AND version_id=? AND checksum=? AND status='pending'",
                                                     (key, user, version['id'], version['sha256']))]
            other = [dict(row) for row in db.execute('''SELECT a.version_id,a.checksum,COUNT(*) AS count FROM annotations a JOIN papers p ON a.paper_key=p.key
              JOIN papers current ON current.key=? AND current.instance=p.instance AND current.document_id=p.document_id
              WHERE a.user_id=? AND a.deleted=0 AND NOT(a.paper_key=? AND a.version_id=? AND a.checksum=?) GROUP BY a.version_id,a.checksum''',
                                                     (key, user, key, version['id'], version['sha256']))]
        return {'schema_version': 1, 'version': version, 'annotations': records,
                'drafts': [{**d, 'payload': json.loads(d['payload'])} for d in drafts], 'other_versions': other}

    def mutate(self, key, user, version, payload, record):
        context = (key, user, version['id'], version['sha256'], record['id'])
        fingerprint = hashlib.sha256(json.dumps([context, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.research.database() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT fingerprint,response FROM annotation_operations WHERE user_id=? AND op_id=?', (user, payload['op_id'])).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ResearchError('operation_conflict', '操作ID已用于不同内容，未覆盖。', 409)
                return json.loads(prior['response'])
            draft = db.execute('SELECT paper_key,version_id,checksum,payload,status FROM annotation_drafts WHERE user_id=? AND op_id=?', (user, payload['op_id'])).fetchone()
            if draft and (draft['status'] == 'archived' or (draft['paper_key'], draft['version_id'], draft['checksum']) != (key, version['id'], version['sha256']) or json.loads(draft['payload']) != payload):
                raise ResearchError('operation_conflict', '操作已归档或操作ID对应其他草稿；未覆盖。', 409)
            db.execute('INSERT OR IGNORE INTO annotation_drafts(user_id,op_id,paper_key,version_id,checksum,payload) VALUES(?,?,?,?,?,?)',
                       (user, payload['op_id'], key, version['id'], version['sha256'], json.dumps(payload, ensure_ascii=False)))
        # Keep a server-side recovery draft even if the subsequent revision check fails.
        with self.research.database() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT fingerprint,response FROM annotation_operations WHERE user_id=? AND op_id=?', (user, payload['op_id'])).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ResearchError('operation_conflict', '操作ID冲突。', 409)
                return json.loads(prior['response'])
            current = db.execute('SELECT record,revision,deleted FROM annotations WHERE paper_key=? AND user_id=? AND version_id=? AND checksum=? AND id=?', context).fetchone()
            if db.execute('SELECT status FROM annotation_drafts WHERE user_id=? AND op_id=?', (user, payload['op_id'])).fetchone()['status'] == 'archived':
                raise ResearchError('operation_conflict', '草稿已归档，未继续写入。', 409)
            expected = current['revision'] if current else 0
            if payload['base_revision'] != expected:
                raise ResearchError('annotation_conflict', '批注已有新修订；草稿已保留，请刷新比较或另存副本。', 409)
            if payload['action'] in {'update', 'delete'} and (not current or current['deleted']):
                raise ResearchError('annotation_missing', '批注不存在或已删除。', 404)
            if payload['action'] == 'create' and current and not current['deleted']:
                raise ResearchError('annotation_exists', '稳定ID已存在；未重复创建。', 409)
            now = db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
            if payload['action'] == 'delete':
                record = json.loads(current['record'])
            record.update(schema_version=1, user_id=user, paper_key=key, version_id=version['id'], pdf_sha256=version['sha256'],
                          revision=expected + 1, created_at=json.loads(current['record'])['created_at'] if current else now,
                          updated_at=now, deleted=payload['action'] == 'delete')
            db.execute('INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(paper_key,user_id,version_id,checksum,id) DO UPDATE SET record=excluded.record,revision=excluded.revision,deleted=excluded.deleted',
                       (*context, json.dumps(record, ensure_ascii=False), record['revision'], int(record['deleted'])))
            result = {'schema_version': 1, 'annotation': record, 'saved': True, 'op_id': payload['op_id']}
            db.execute('INSERT INTO annotation_operations VALUES(?,?,?,?)', (user, payload['op_id'], fingerprint, json.dumps(result, ensure_ascii=False)))
            db.execute("UPDATE annotation_drafts SET status='applied' WHERE user_id=? AND op_id=?", (user, payload['op_id']))
        return result

    def merge(self, db, key, user, versions, annotations):
        imported = 0
        for version in versions:
            db.execute('INSERT OR IGNORE INTO annotation_versions VALUES(?,?,?,?,?)', (key, version['id'], version['sha256'], version['kind'], json.dumps(version['pages'])))
        for record in annotations:
            record = {**record, 'paper_key': key, 'user_id': user}
            imported += db.execute('INSERT OR IGNORE INTO annotations VALUES(?,?,?,?,?,?,?,?)',
                                   (key, user, record['version_id'], record['pdf_sha256'], record['id'], json.dumps(record, ensure_ascii=False), record['revision'], int(record.get('deleted', False)))).rowcount
        return imported
