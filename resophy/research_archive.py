"""Logical per-document export and merge restore; no Paperless database replacement."""
import hashlib
import io
import json
import re
import zipfile
import datetime
from pypdf import PdfReader
from pathlib import PurePosixPath
from flask import abort, jsonify, request, send_file
from resophy.research_store import ResearchError


def register_archives(app, store):
    @app.get('/papers/<key>/export')
    def export_research(key):
        buffer, size = io.BytesIO(), 0
        with store.database() as db, zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zipped:
            db.execute('BEGIN')
            state = store.state(key, request.headers['X-Research-User'], db=db)
            if not state['associated']:
                abort(404)
            manifest = {'schema': 2, 'document_checksum': request.headers.get('X-Document-Checksum', ''),
                        'bibliography': state['bibliography'], 'personal': state['personal'], 'artifacts': [],
                        'tasks': state['tasks'], 'scope': 'research-only; originals remain in Paperless'}
            manifest['annotations'] = app.extensions['annotations'].export(key, request.headers['X-Research-User'], db)
            manifest['reading_days'] = [dict(row) for row in db.execute('SELECT day,SUM(seconds) AS seconds FROM reading_events WHERE paper_key=? AND user_id=? GROUP BY day',
                                                                     (key, request.headers['X-Research-User']))]
            for row in db.execute('SELECT * FROM artifacts WHERE paper_key=?', (key,)):
                content = (store.directory / row['file_name']).read_bytes()
                size += len(content)
                if size > 10 * 1024 * 1024:
                    raise ResearchError('archive_size', '研究包超过10MiB，请减少单包产物。')
                zipped.writestr('artifacts/' + row['file_name'], content)
                manifest['artifacts'].append({'id': row['id'], 'file': 'artifacts/' + row['file_name'], 'kind': row['kind'],
                                              'sha256': hashlib.sha256(content).hexdigest(), 'provenance': row['provenance']})
            zipped.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        buffer.seek(0)
        return send_file(buffer, mimetype='application/zip', as_attachment=True, download_name='research-data.zip')

    @app.post('/papers/<key>/restore')
    def restore_research(key):
        user = request.headers['X-Research-User']
        if not store.state(key, user)['associated']:
            abort(404)
        try:
            with zipfile.ZipFile(io.BytesIO(request.get_data())) as zipped:
                entries = zipped.infolist()
                if len(entries) > 100 or sum(item.file_size for item in entries) > 20 * 1024 * 1024:
                    raise ValueError()
                names = [item.filename for item in entries]
                if len(names) != len(set(names)):
                    raise ValueError()
                for name in names:
                    path = PurePosixPath(name)
                    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
                        raise ValueError()
                manifest = json.loads(zipped.read('manifest.json'))
                checksum = request.headers.get('X-Document-Checksum', '')
                if manifest['schema'] not in {1, 2} or not checksum or manifest.get('document_checksum') != checksum:
                    raise ResearchError('archive_document_mismatch', '原文校验值与当前文章不一致，未导入。', 409)
                artifacts = manifest['artifacts']
                if len(artifacts) > 99 or not isinstance(manifest.get('bibliography'), dict):
                    raise ValueError()
                contents, seen_ids = [], set()
                for artifact in artifacts:
                    if not re.fullmatch('[a-f0-9]{32}', artifact['id']) or artifact['id'] in seen_ids:
                        raise ValueError()
                    seen_ids.add(artifact['id'])
                    if not re.fullmatch(r'artifacts/' + artifact['id'] + r'\.(pdf|md|png)', artifact['file']):
                        raise ValueError()
                    if artifact['kind'] not in {'reference_pdf', 'translated_pdf', 'chinese_pdf', 'analysis_markdown', 'figure_png'}:
                        raise ValueError()
                    content = zipped.read(artifact['file'])
                    if hashlib.sha256(content).hexdigest() != artifact['sha256']:
                        raise ValueError()
                    contents.append((artifact, content))
                annotation_versions, annotation_records = [], []
                if 'annotations' in manifest:
                    annotation_versions, annotation_records = app.extensions['annotations'].validate_archive(manifest['annotations'])
                    for version in annotation_versions:
                        if version['kind'] == 'original':
                            if version['id'] != 'original:' + checksum or version['sha256'] != checksum:
                                raise ValueError()
                        elif not any(a['id'] == version['id'] and a['sha256'] == version['sha256'] and a['kind'] == version['kind'] for a in artifacts):
                            raise ValueError()
                        else:
                            pdf = PdfReader(io.BytesIO(next(content for artifact, content in contents if artifact['id'] == version['id'])))
                            pages = [{'width': float(p.cropbox.width), 'height': float(p.cropbox.height)} for p in pdf.pages]
                            if pages != version['pages']:
                                raise ValueError()
                if set(names) != {'manifest.json', *(item['file'] for item in artifacts)}:
                    raise ValueError()
                notes = manifest.get('personal', {}).get('notes', '')
                if not isinstance(notes, str) or len(notes) > 20000:
                    raise ValueError()
                personal = manifest.get('personal', {})
                reader_state = personal.get('reader_state', {})
                if (personal.get('list_status', 'none') not in {'none', 'inbox', 'reading', 'completed'}
                        or not isinstance(reader_state, dict) or len(json.dumps(reader_state)) > 16000):
                    raise ValueError()
                days = manifest.get('reading_days', [])
                if not isinstance(days, list) or len(days) > 3660:
                    raise ValueError()
                for day in days:
                    datetime.date.fromisoformat(day['day'])
                    if not isinstance(day['seconds'], int) or not 0 <= day['seconds'] <= 86400:
                        raise ValueError()
        except ResearchError:
            raise
        except (ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile, UnicodeDecodeError):
            raise ResearchError('archive_invalid', '研究包格式、大小、路径或校验无效，未导入。') from None
        reused, imported, mapping = 0, 0, {}
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            paper = db.execute('SELECT document_id,metadata FROM papers WHERE key=?', (key,)).fetchone()
            for artifact, content in sorted(contents, key=lambda item: item[0]['kind'] == 'analysis_markdown'):
                existing = db.execute('SELECT id FROM artifacts WHERE paper_key=? AND checksum=? AND kind=?',
                                      (key, artifact['sha256'], artifact['kind'])).fetchone()
                if existing:
                    mapping[artifact['id']] = existing['id']
                    reused += 1
                    continue
                if artifact['kind'] == 'analysis_markdown':
                    text = content.decode('utf-8')
                    for old_id, new_id in mapping.items():
                        text = re.sub(r'/api/research/documents/\d+/artifacts/' + re.escape(old_id) + '/',
                                      f'/api/research/documents/{paper["document_id"]}/artifacts/{new_id}/', text)
                    content = text.encode()
                mapping[artifact['id']] = store.save_artifact(key, artifact['kind'], content, '导入存档（来源未重新验证）', 'restored_unverified', db=db)
                imported += 1
            for version in annotation_versions:
                version['id'] = mapping.get(version['id'], version['id'])
            for record in annotation_records:
                record['version_id'] = mapping.get(record['version_id'], record['version_id'])
            annotations_imported = app.extensions['annotations'].storage.merge(db, key, user, annotation_versions, annotation_records)
            metadata = json.loads(paper['metadata'])
            for name in ('title', 'authors', 'year', 'journal', 'doi', 'arxiv_id', 'keywords', 'abstract', 'bibtex'):
                value = manifest['bibliography'].get(name)
                if not metadata.get(name) and isinstance(value, str) and len(value) <= 100000:
                    metadata[name] = value
            db.execute('UPDATE papers SET metadata=? WHERE key=?', (json.dumps(metadata, ensure_ascii=False), key))
            current = db.execute('SELECT notes FROM personal WHERE paper_key=? AND user_id=?', (key, user)).fetchone()
            if not current:
                db.execute('INSERT INTO personal(paper_key,user_id,list_status,starred,reader_state) VALUES(?,?,?,?,?)',
                           (key, user, personal.get('list_status', 'none'), int(bool(personal.get('starred'))), json.dumps(reader_state)))
            if notes and (not current or not current['notes']):
                db.execute('INSERT INTO personal(paper_key,user_id,notes,note_revision) VALUES(?,?,?,1) ON CONFLICT(paper_key,user_id) DO UPDATE SET notes=excluded.notes,note_revision=personal.note_revision+1', (key, user, notes))
            for index, day in enumerate(days):
                previous = db.execute('SELECT COALESCE(SUM(seconds),0) FROM reading_events WHERE paper_key=? AND user_id=? AND day=?', (key, user, day['day'])).fetchone()[0]
                extra = max(0, day['seconds'] - previous)
                if extra:
                    db.execute('INSERT OR IGNORE INTO reading_events VALUES(?,?,?,?,?,?)', (user, key, 'archive:' + hashlib.sha256(request.get_data()).hexdigest()[:32], index, day['day'], extra))
        return jsonify(imported=imported, reused=reused, annotations_imported=annotations_imported, preserved_existing=True, tasks_restarted=0)
