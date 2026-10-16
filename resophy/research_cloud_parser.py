"""Explicit MinerU modes; resume saved tasks without resubmitting uploads."""
import json
import re
import time

from resophy.research_parser import read_agent_markdown, read_parser_archive, trusted_asset_url
from resophy.research_store import ResearchError


def parse_cloud(source, mode, environment, checkpoint, save, request_bytes):
    agent = mode == 'mineru_agent'
    if agent:
        from pypdf import PdfReader
        if len(PdfReader(source).pages) > 20:
            raise ResearchError('parser_page_limit', 'MinerU轻量模式本项目限制20页；请显式配置精准解析。')
        base = 'https://mineru.net/api/v1/agent'
        endpoint = base + '/parse/file'
        payload = {'file_name': 'input.pdf', 'language': 'en', 'enable_formula': True, 'enable_table': True}
        headers = {'Content-Type': 'application/json'}
        id_name = 'task_id'
    else:
        token = environment.get('RESEARCH_MINERU_API_TOKEN', '')
        if not token:
            raise ResearchError('parser_unconfigured', '精准MinerU模式缺少RESEARCH_MINERU_API_TOKEN。')
        base = 'https://mineru.net/api/v4'
        endpoint = base + '/file-urls/batch'
        payload = {'files': [{'name': 'input.pdf', 'data_id': source.stem}],
                   'model_version': 'vlm', 'language': 'en', 'enable_formula': True, 'enable_table': True}
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        id_name = 'batch_id'
    if not checkpoint.get('mineru_batch'):
        if checkpoint.get('parser_submission_uncertain'):
            raise ResearchError('parser_submission_uncertain', '解析申请可能已提交；不自动创建重复任务，请先核对服务端。')
        checkpoint['parser_submission_uncertain'] = True
        save('parser_submit', checkpoint)
        result = json.loads(request_bytes(endpoint, 'POST', json.dumps(payload).encode(), headers))
        if result.get('code') != 0:
            checkpoint['parser_submission_uncertain'] = False
            save('parser_rejected', checkpoint)
            raise ResearchError('parser_rejected', 'MinerU拒绝申请；未自动重试。')
        identifier = result['data'][id_name]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
            raise ResearchError('parser_response', '解析任务标识无效。')
        checkpoint.update(mineru_batch=identifier, parser_submission_uncertain=False,
                          parser_mode=mode, parser_submissions=1)
        save('parser_upload', checkpoint)
        url = result['data']['file_url'] if agent else result['data']['file_urls'][0]
        request_bytes(trusted_asset_url(url), 'PUT', source.read_bytes())
        checkpoint['mineru_uploaded'] = True
        save('parser_wait', checkpoint)
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        save('parser_wait', checkpoint)
        endpoint = (base + '/parse/' if agent else base + '/extract-results/batch/') + checkpoint['mineru_batch']
        result = json.loads(request_bytes(endpoint, headers=headers))
        if result.get('code') != 0:
            raise ResearchError('parser_rejected', 'MinerU查询失败；保留原任务，不重新提交。')
        entries = [result['data']] if agent else result['data'].get('extract_result', [])
        if entries and entries[0]['state'] == 'waiting-file' and not checkpoint.get('mineru_uploaded'):
            raise ResearchError('upload_uncertain', '解析上传尚未确认；保留原任务，未重新上传。')
        if entries and entries[0]['state'] == 'failed':
            raise ResearchError('parser_failed', 'MinerU解析失败；保留原任务，不自动重试。')
        if entries and entries[0]['state'] == 'done':
            if agent:
                url = trusted_asset_url(entries[0]['markdown_url'])
                text = request_bytes(url, maximum=1024 * 1024).decode('utf-8')
                return read_agent_markdown(text, url, request_bytes)
            archive = request_bytes(trusted_asset_url(entries[0]['full_zip_url']), maximum=50 * 1024 * 1024)
            return read_parser_archive(archive)
        time.sleep(10)
    raise ResearchError('parser_timeout', '解析等待超时；原任务已保存，可手动继续查询。')
