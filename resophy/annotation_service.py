import copy
import json
import math
import re
from resophy.research_store import ResearchError

IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,80}')
TYPES = {'highlight', 'underline', 'rectangle', 'ink', 'text'}


def validate_version(value):
    if (not isinstance(value, dict) or not isinstance(value.get('id'), str) or len(value['id']) > 100
            or not re.fullmatch(r'(original:[a-f0-9]{64}|[a-f0-9]{32})', value['id'])
            or not isinstance(value.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', value['sha256']) or value.get('kind') not in {'original', 'translated_pdf', 'chinese_pdf', 'reference_pdf'}):
        raise ResearchError('annotation_version', 'PDF版本关联无效。')
    pages = value.get('pages')
    if not isinstance(pages, list) or not 1 <= len(pages) <= 1000:
        raise ResearchError('annotation_pages', 'PDF页数超出批注范围。')
    for page in pages:
        if not isinstance(page, dict) or any(not isinstance(page.get(k), (int, float)) or not math.isfinite(page[k]) or not 0 < page[k] <= 20000 for k in ('width', 'height')):
            raise ResearchError('annotation_pages', 'PDF页面尺寸无效。')
    return value


def validate_annotation(value, version, deletion=False):
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not IDENTIFIER.fullmatch(value['id']):
        raise ResearchError('annotation_invalid', '批注稳定ID无效。')
    if deletion:
        return {'id': value['id']}
    if (value.get('schema_version') != 1 or value.get('type') not in TYPES
            or value.get('coordinate_system') != 'pdf-points-top-left-unrotated'
            or type(value.get('page')) is not int or not 1 <= value['page'] <= len(version['pages'])):
        raise ResearchError('annotation_invalid', '批注类型、页码或坐标体系无效。')
    for name, limit in [('quote', 20000), ('comment', 20000)]:
        if not isinstance(value.get(name, ''), str) or len(value.get(name, '')) > limit:
            raise ResearchError('annotation_invalid', '批注文字超过限制。')
    if not isinstance(value.get('color'), str) or not re.fullmatch(r'#[a-fA-F0-9]{6}', value['color']):
        raise ResearchError('annotation_color', '批注颜色无效。')
    regions = value.get('regions', [])
    if not isinstance(regions, list) or not 1 <= len(regions) <= 500:
        raise ResearchError('annotation_geometry', '批注区域无效。')
    for rect in regions:
        if not isinstance(rect, dict) or any(type(rect.get(k)) not in (int, float) or not math.isfinite(rect[k]) or abs(rect[k]) > 20000 for k in ('x', 'y', 'width', 'height')) or rect['width'] < 0 or rect['height'] < 0:
            raise ResearchError('annotation_geometry', '批注坐标无效。')
    sdk = value.get('sdk', {})
    if not isinstance(sdk, dict) or sdk.get('format') != 'embedpdf-v2' or sdk.get('version') != '2.15.1' or not isinstance(sdk.get('data'), dict):
        raise ResearchError('annotation_sdk', '批注SDK格式不支持。')
    raw = sdk['data']
    expected = {'text': 1, 'rectangle': 5, 'highlight': 9, 'underline': 10, 'ink': 15}[value['type']]
    if raw.get('type') != expected or raw.get('id') != value['id'] or raw.get('pageIndex') != value['page'] - 1:
        raise ResearchError('annotation_sdk', 'SDK数据与批注契约不一致。')
    if len(json.dumps(value)) > 256000:
        raise ResearchError('annotation_size', '单条批注超过256KB。')
    # Binary stamps/attachments/actions are not accepted as arbitrary SDK extension data.
    forbidden = {'ctx', 'appearance', 'data', 'imageData', 'attachment', 'action', 'target', 'uri', 'javascript'}
    if forbidden.intersection(raw):
        raise ResearchError('annotation_attachment', '本轮不支持图片/印章/附件或活动脚本。')
    def rectangle(rect):
        if not isinstance(rect, dict) or not isinstance(rect.get('origin'), dict) or not isinstance(rect.get('size'), dict):
            raise ResearchError('annotation_geometry', 'SDK矩形无效。')
        if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 20000 for v in [rect['origin'].get('x'), rect['origin'].get('y'), rect['size'].get('width'), rect['size'].get('height')]):
            raise ResearchError('annotation_geometry', 'SDK矩形坐标无效。')
        if rect['size']['width'] < 0 or rect['size']['height'] < 0:
            raise ResearchError('annotation_geometry', 'SDK矩形尺寸无效。')
    rectangle(raw.get('rect'))
    if 'unrotatedRect' in raw:
        rectangle(raw['unrotatedRect'])
    if value['type'] in {'highlight', 'underline'}:
        segments = raw.get('segmentRects')
        if not isinstance(segments, list) or len(segments) != len(regions):
            raise ResearchError('annotation_geometry', '跨行高亮区域不完整。')
        for segment, region in zip(segments, regions):
            rectangle(segment)
            if segment != {'origin': {'x': region['x'], 'y': region['y']}, 'size': {'width': region['width'], 'height': region['height']}}:
                raise ResearchError('annotation_geometry', 'SDK文本区域与契约不一致。')
    if value['type'] == 'ink':
        strokes = raw.get('inkList')
        if not isinstance(strokes, list) or not 1 <= len(strokes) <= 1000:
            raise ResearchError('annotation_geometry', '手绘路径无效。')
        for stroke in strokes:
            points = stroke.get('points') if isinstance(stroke, dict) else None
            if not isinstance(points, list) or not 1 <= len(points) <= 10000:
                raise ResearchError('annotation_geometry', '手绘点数无效。')
            for point in points:
                if not isinstance(point, dict) or any(type(point.get(k)) not in (int, float) or not math.isfinite(point[k]) or abs(point[k]) > 20000 for k in ('x', 'y')):
                    raise ResearchError('annotation_geometry', '手绘坐标无效。')
    for name in ('opacity', 'strokeWidth', 'rotation', 'blendMode', 'strokeStyle'):
        if name in raw and (type(raw[name]) not in (int, float) or not math.isfinite(raw[name]) or abs(raw[name]) > 10000):
            raise ResearchError('annotation_sdk', 'SDK数值无效。')
    if 'flags' in raw and (not isinstance(raw['flags'], list) or any(flag not in {'print', 'locked', 'lockedContents', 'noZoom', 'noRotate', 'readOnly', 'hidden', 'invisible', 'noView', 'toggleNoView'} for flag in raw['flags'])):
        raise ResearchError('annotation_sdk', 'SDK标志无效。')
    if 'fillColor' in raw and (not isinstance(raw['fillColor'], str) or not re.fullmatch(r'#[a-fA-F0-9]{6}|transparent', raw['fillColor'])):
        raise ResearchError('annotation_color', '填充颜色无效。')
    if 'color' in raw and (not isinstance(raw['color'], str) or not re.fullmatch(r'#[a-fA-F0-9]{6}([a-fA-F0-9]{2})?|transparent', raw['color'])):
        raise ResearchError('annotation_color', 'SDK颜色无效。')
    if 'strokeDashArray' in raw and (not isinstance(raw['strokeDashArray'], list) or len(raw['strokeDashArray']) > 64 or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 20000 for v in raw['strokeDashArray'])):
        raise ResearchError('annotation_geometry', '描边样式无效。')
    if 'rectangleDifferences' in raw and (not isinstance(raw['rectangleDifferences'], dict) or any(type(raw['rectangleDifferences'].get(k)) not in (int, float) or not math.isfinite(raw['rectangleDifferences'][k]) or abs(raw['rectangleDifferences'][k]) > 20000 for k in ('left', 'top', 'right', 'bottom'))):
        raise ResearchError('annotation_geometry', '矩形边距无效。')
    allowed = {'id', 'type', 'pageIndex', 'rect', 'unrotatedRect', 'rotation', 'segmentRects', 'inkList', 'contents', 'strokeColor', 'color', 'opacity', 'strokeWidth', 'fillColor', 'strokeStyle', 'strokeDashArray', 'rectangleDifferences', 'flags', 'blendMode', 'created', 'modified'}
    for name in ('created', 'modified'):
        if name in raw and (not isinstance(raw[name], str) or len(raw[name]) > 40):
            raise ResearchError('annotation_sdk', 'SDK日期无效。')
    result = {name: copy.deepcopy(value.get(name, '')) for name in ('id', 'schema_version', 'type', 'coordinate_system', 'page', 'quote', 'comment', 'color', 'regions')}
    result['sdk'] = {'format': 'embedpdf-v2', 'version': '2.15.1', 'data': {k: copy.deepcopy(v) for k, v in raw.items() if k in allowed}}
    result['sdk']['data']['contents'] = result['comment']
    result['sdk']['data']['strokeColor'] = result['color']
    if result['type'] == 'rectangle':
        result['sdk']['data'].setdefault('color', 'transparent')
    return result


class AnnotationService:
    def __init__(self, storage):
        self.storage = storage

    def version(self, key, version):
        validate_version(version)
        if not version['id'].startswith('original:'):
            row = self.storage.artifact(key, version['id'])
            if not row or row['kind'] != version['kind'] or row['checksum'] != version['sha256']:
                raise ResearchError('annotation_version', 'PDF产物不属于当前文章或文件校验值变化。', 404)
        elif version['id'] != 'original:' + version['sha256']:
            raise ResearchError('annotation_version', '原文版本与校验值不一致。')
        self.storage.register(key, version)
        return version

    def mutate(self, key, user, version, payload):
        if (payload.get('action') not in {'create', 'update', 'delete'} or type(payload.get('base_revision')) is not int or payload['base_revision'] < 0
                or not isinstance(payload.get('op_id'), str) or not re.fullmatch('[a-f0-9-]{32,36}', payload['op_id'])):
            raise ResearchError('annotation_operation', '操作ID、修订或操作类型无效。')
        record = validate_annotation(payload.get('annotation'), version, payload['action'] == 'delete')
        return self.storage.mutate(key, user, version, payload, record)

    def export(self, key, user, db):
        return self.storage.export(key, user, db)

    def archive_draft(self, key, user, version, operation):
        if not isinstance(operation, str) or not re.fullmatch('[a-f0-9-]{32,36}', operation):
            raise ResearchError('annotation_operation', '草稿操作ID无效。')
        self.storage.archive_draft(key, user, version, operation)

    def validate_archive(self, data):
        if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('versions'), list) or not isinstance(data.get('annotations'), list) or len(data['annotations']) > 10000 or len(data['versions']) > 100:
            raise ResearchError('annotations_archive', '批注包格式或数量超限。')
        versions = {}
        for value in data['versions']:
            version = validate_version(value)
            if version['id'] in versions:
                raise ResearchError('annotations_archive', '重复PDF版本。')
            versions[version['id']] = version
        result = []
        seen = set()
        for item in data['annotations']:
            if not isinstance(item, dict) or type(item.get('deleted')) is not bool or any(not isinstance(item.get(k), str) or len(item[k]) > 40 for k in ('created_at', 'updated_at')):
                raise ResearchError('annotations_archive', '批注历史字段无效。')
            version = versions.get(item.get('version_id'))
            if not version or item.get('pdf_sha256') != version['sha256'] or type(item.get('revision')) is not int or item['revision'] < 1:
                raise ResearchError('annotations_archive', '批注版本校验不匹配。')
            record = validate_annotation(item, version)
            identity = (version['id'], version['sha256'], record['id'])
            if identity in seen:
                raise ResearchError('annotations_archive', '包内重复批注ID。')
            seen.add(identity)
            record.update({k: item[k] for k in ('version_id', 'pdf_sha256', 'revision', 'created_at', 'updated_at', 'deleted')})
            result.append(record)
        return list(versions.values()), result
