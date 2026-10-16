"""Private discovery/import candidates; IDs are never Paperless document IDs."""
import io
import json
import re
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from flask import abort, jsonify, request, send_file
from resophy.research_store import ResearchError


def register_collections(app, store):
    with store.database() as db:
        db.execute('CREATE TABLE IF NOT EXISTS collections (id TEXT PRIMARY KEY,user_id TEXT NOT NULL,source TEXT NOT NULL,metadata TEXT NOT NULL,'
                   "file_name TEXT NOT NULL DEFAULT '',arxiv_id TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'candidate',"
                   "task_id TEXT NOT NULL DEFAULT '',document_id INTEGER,to_read INTEGER NOT NULL DEFAULT 0)")

    def row_for(identifier):
        if not re.fullmatch('[a-f0-9]{32}', identifier):
            abort(404)
        with store.database() as db:
            row = db.execute('SELECT * FROM collections WHERE id=? AND user_id=?', (identifier, request.headers['X-Research-User'])).fetchone()
        if not row:
            abort(404)
        return dict(row)

    def public(row):
        return {name: row[name] for name in ('id', 'source', 'arxiv_id', 'status', 'task_id', 'document_id', 'to_read')} | {
            'metadata': json.loads(row['metadata']), 'has_pdf': bool(row['file_name'])}

    def add(metadata, source, content=None, arxiv_id=''):
        identifier = uuid.uuid4().hex
        file_name = ''
        if content:
            if not content.startswith(b'%PDF-') or len(content) > 10 * 1024 * 1024:
                raise ResearchError('import_pdf', '附件不是允许大小的PDF。')
            directory = store.directory / 'imports'
            directory.mkdir(exist_ok=True)
            file_name = identifier + '.pdf'
            (directory / file_name).write_bytes(content)
        with store.database() as db:
            existing = db.execute('SELECT id FROM collections WHERE user_id=? AND arxiv_id=? AND arxiv_id!=\'\'',
                                  (request.headers['X-Research-User'], arxiv_id)).fetchone()
            if existing:
                return existing['id']
            db.execute('INSERT INTO collections(id,user_id,source,metadata,file_name,arxiv_id) VALUES(?,?,?,?,?,?)',
                       (identifier, request.headers['X-Research-User'], source, json.dumps(metadata, ensure_ascii=False), file_name, arxiv_id))
        return identifier

    @app.route('/collections', methods=['GET', 'POST'])
    def collections():
        if request.method == 'POST':
            payload = request.get_json()
            items = payload.get('items', [])
            if not isinstance(items, list) or len(items) > 20:
                raise ResearchError('candidate_invalid', '单次候选最多20篇。')
            identifiers = []
            for item in items:
                if not isinstance(item.get('title'), str) or len(json.dumps(item)) > 30000:
                    raise ResearchError('candidate_invalid', '候选资料格式无效。')
                identifiers.append(add(item, 'arxiv', arxiv_id=item.get('arxiv_id', '')))
            return jsonify(ids=identifiers), 201
        with store.database() as db:
            rows = db.execute('SELECT * FROM collections WHERE user_id=? ORDER BY rowid DESC', (request.headers['X-Research-User'],)).fetchall()
        return jsonify(results=[public(dict(row)) for row in rows])

    @app.route('/collections/<identifier>', methods=['GET', 'POST'])
    def candidate(identifier):
        row = row_for(identifier)
        if request.method == 'POST':
            payload = request.get_json()
            with store.database() as db:
                for name in ('status', 'task_id', 'document_id', 'to_read'):
                    if name in payload:
                        db.execute(f'UPDATE collections SET {name}=? WHERE id=?', (payload[name], identifier))
            row = row_for(identifier)
        return jsonify(public(row))

    @app.get('/collections/<identifier>/source')
    def source(identifier):
        row = row_for(identifier)
        if not row['file_name']:
            abort(404)
        return send_file(store.directory / 'imports' / row['file_name'], mimetype='application/pdf')

    @app.post('/collections/<identifier>/reserve')
    def reserve(identifier):
        row_for(identifier)
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE collections SET status='preparing' WHERE id=? AND user_id=? AND status='candidate' AND task_id='' AND document_id IS NULL",
                                 (identifier, request.headers['X-Research-User'])).rowcount
        if not changed:
            raise ResearchError('archive_reserved', '此候选已提交或正在归档，请核对任务；不会重复提交。', 409)
        return jsonify(public(row_for(identifier)))

    @app.post('/imports/zotero')
    def import_zotero():
        try:
            with zipfile.ZipFile(io.BytesIO(request.get_data())) as zipped:
                entries = zipped.infolist()
                if len(entries) > 100 or sum(item.file_size for item in entries) > 20 * 1024 * 1024:
                    raise ValueError()
                files = [entry.filename for entry in entries if not entry.is_dir()]
                if len(files) != len(set(files)):
                    raise ValueError()
                for name in files:
                    path = PurePosixPath(name)
                    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
                        raise ValueError()
                rdf = [name for name in files if name.lower().endswith(('.rdf', '.xml'))]
                if len(rdf) != 1 or zipped.getinfo(rdf[0]).file_size > 2 * 1024 * 1024:
                    raise ValueError()
                xml = zipped.read(rdf[0])
                if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                    raise ValueError()
                root = ET.fromstring(xml)
                if len(list(root.iter())) > 10000:
                    raise ValueError()
                resource = '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource'
                about = '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about'
                nodes = {node.get(about): node for node in root if node.get(about)}
                pdfs = [name for name in files if name.lower().endswith('.pdf')]
                candidates = []
                for node in root:
                    tag = node.tag.split('}')[-1]
                    types = [child.get(resource, '').rsplit('#', 1)[-1] for child in node if child.tag.split('}')[-1] == 'type']
                    if tag in {'Attachment', 'Memo', 'Collection', 'Person', 'Seq', 'Journal'} or set(types) & {'Attachment', 'Memo', 'Collection', 'Person', 'Journal'}:
                        continue
                    fields = {child.tag.split('}')[-1]: (child.text or '').strip() for child in node}
                    if not fields.get('title'):
                        continue
                    all_nodes, visited = [node], {id(node)}
                    for parent in all_nodes:
                        for child in parent.iter():
                            target = nodes.get(child.get(resource))
                            if target is not None and id(target) not in visited:
                                visited.add(id(target))
                                all_nodes.append(target)
                    paths = [el.get(resource, '') for parent in all_nodes for el in parent.iter() if el.get(resource, '').lower().endswith('.pdf')]
                    matches = [name for name in pdfs if any(name == path or PurePosixPath(name).name == PurePosixPath(path).name for path in paths)]
                    if len(matches) > 1:
                        raise ResearchError('attachment_ambiguous', '附件名称不唯一，请整理ZIP后再导入。')
                    authors = []
                    for parent in all_nodes:
                        for person in parent.iter():
                            person_types = [child.get(resource, '').rsplit('#', 1)[-1] for child in person if child.tag.split('}')[-1] == 'type']
                            if person.tag.split('}')[-1] == 'Person' or 'Person' in person_types:
                                parts = {field.tag.split('}')[-1]: (field.text or '').strip() for field in person}
                                authors.append(' '.join(filter(None, [parts.get('givenname') or parts.get('firstName'), parts.get('surname') or parts.get('lastName')])) )
                    metadata = {'title': fields['title'], 'authors': ' and '.join(filter(None, authors)), 'year': fields.get('date', '')[:4],
                                'abstract': fields.get('abstract', ''), 'journal': fields.get('publicationTitle', ''),
                                'doi': fields.get('DOI', '') or fields.get('doi', ''), 'keywords': fields.get('subject', '')}
                    for parent in all_nodes[1:]:
                        if parent.tag.split('}')[-1] == 'Journal':
                            metadata['journal'] = next((el.text for el in parent if el.tag.split('}')[-1] == 'title'), '')
                    candidates.append((metadata, zipped.read(matches[0]) if matches else None))
                    if len(candidates) > 20:
                        raise ValueError()
                if not candidates:
                    raise ResearchError('zotero_empty', '未识别到文献条目；请提供Zotero RDF及其PDF附件的ZIP。')
        except ResearchError:
            raise
        except (ValueError, KeyError, RuntimeError, zipfile.BadZipFile, ET.ParseError):
            raise ResearchError('zotero_invalid', 'ZIP/RDF格式、大小或路径不符合边界，未导入。') from None
        ids = [add(metadata, 'zotero', content) for metadata, content in candidates]
        return jsonify(ids=ids, preview_only=True, documents_created=0), 201
