import json
from flask import request, jsonify, abort, Response
from resophy.annotation_store import AnnotationStore
from resophy.annotation_service import AnnotationService
from resophy.research_store import ResearchError


def register_annotations(app, research):
    service = AnnotationService(AnnotationStore(research))
    app.extensions['annotations'] = service

    @app.post('/papers/<key>/annotations/<action>')
    def annotations(key, action):
        user = request.headers['X-Research-User']
        if not research.state(key, user)['associated']:
            abort(404)
        payload = request.get_json()
        version = service.version(key, payload.get('version'))
        if action == 'mutate':
            return jsonify(service.mutate(key, user, version, payload))
        if action == 'discard_draft':
            service.archive_draft(key, user, version, payload.get('op_id'))
            return jsonify(archived=True, server_annotation_unchanged=True)
        state = service.storage.listing(key, user, version)
        if action == 'list':
            return jsonify(state)
        if action == 'check-export':
            current = {a['id']: a['revision'] for a in state['annotations'] if not a['deleted']}
            if current != payload.get('revisions'):
                raise ResearchError('annotation_conflict', '批注修订已变化，请重新加载后导出。', 409)
            return jsonify(authorized=True, annotations=[a for a in state['annotations'] if not a['deleted']])
        if action == 'summary':
            lines = ['# 私人批注摘要', '', 'PDF版本: ' + version['id'], 'SHA256: ' + version['sha256'], '坐标: PDF points, top-left, unrotated', '']
            for item in state['annotations']:
                if not item['deleted']:
                    lines += [f'## 第{item["page"]}页 · {item["type"]} · {item["id"]}', item['quote'], item['comment'],
                              '定位: ' + json.dumps(item['regions'], ensure_ascii=False), '']
            return Response('\n'.join(lines), mimetype='text/markdown')
        if action == 'export':
            return jsonify(schema_version=1, versions=[version], annotations=state['annotations'])
        if action == 'import':
            versions, records = service.validate_archive(payload.get('data'))
            if any(v['sha256'] != version['sha256'] or v['pages'] != version['pages'] for v in versions):
                raise ResearchError('annotation_version', '导入包PDF校验值不同，未迁移。', 409)
            for item in records:
                item.update(version_id=version['id'], pdf_sha256=version['sha256'])
            with research.database() as db:
                imported = service.storage.merge(db, key, user, [version], records)
            return jsonify(imported=imported, preserved_existing=True)
        abort(404)
