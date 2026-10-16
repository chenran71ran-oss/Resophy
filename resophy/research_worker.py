import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from resophy.research_providers import ResearchProviders
from resophy.research_store import ResearchError, ResearchStore
from resophy.research_usage import usage_summary


def authorize_task(payload):
    body = json.dumps({'document_id': payload['document_id'], 'user_id': payload['user_id'],
                       'paper_key': payload['paper_key']}).encode()
    request = Request(os.environ['RESEARCH_PAPERLESS_URL'].rstrip('/') + '/api/research/authorize/', data=body,
                      headers={'Content-Type': 'application/json', 'X-Research-Secret': os.environ['RESEARCH_SHARED_SECRET']})
    try:
        with urlopen(request, timeout=15) as response:
            return response.status == 200
    except (HTTPError, URLError, TimeoutError):
        return False


def run_one(store, providers, authorize):
    task = store.claim()
    if task is None:
        return False
    task_id, payload = task['id'], task['payload']
    with store.database() as db:
        db.execute('UPDATE tasks SET provenance=? WHERE id=?', (providers.provenance, task_id))
    print(f'task={task_id} stage=authorize status=running', flush=True)
    try:
        if not authorize(payload):
            raise ResearchError('permission_denied', '文档权限已撤销、文档已变化或权限服务不可用，未发起新调用。')
        source = store.directory / 'inputs' / (task['paper_key'] + '.pdf')
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != payload['source_sha256']:
            raise ResearchError('input_changed', '任务输入缺失或内容已变化。')
        work = store.directory / 'jobs' / task_id
        work.mkdir(parents=True, exist_ok=True)

        def save(stage, checkpoint):
            store.checkpoint(task_id, stage, checkpoint)
            print(f'task={task_id} stage={stage} status=running', flush=True)

        execute = providers.translate if task['kind'] == 'translation' else providers.analyze
        save('authorized', task['checkpoint'])
        outputs = execute(source, work, {**payload['options'], 'force_regenerate': bool(payload.get('force'))}, task['checkpoint'], save)
        if not authorize(payload):
            raise ResearchError('permission_revoked', '处理期间文档权限撤销或版本变化，未发布研究结果。')
        with store.database() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()['status'] == 'cancel_requested':
                raise ResearchError('cancelled', '取消已确认，未发布新产物。外部费用可能已发生。')
            artifacts, figures = [], {}
            primary = None
            for kind, content, title in outputs:
                if kind == 'analysis_markdown':
                    text = content.decode('utf-8')
                    for placeholder, artifact_id in figures.items():
                        text = text.replace(placeholder, f'/api/research/documents/{payload["document_id"]}/artifacts/{artifact_id}/')
                    content = text.encode('utf-8')
                artifact_id = store.save_artifact(task['paper_key'], kind, content,
                                                  '论文解析图片' if kind == 'figure_png' else title,
                                                  providers.provenance, task['options_hash'], db)
                artifacts.append(artifact_id)
                if kind == 'figure_png':
                    figures[title] = artifact_id
                elif primary is None:
                    primary = artifact_id
            if not artifacts:
                raise ResearchError('empty_result', '处理未产生可保存结果。')
            db.execute("UPDATE tasks SET status='completed',stage='saved',artifact_id=?,provenance=? WHERE id=?",
                       (primary or artifacts[0], providers.provenance, task_id))
        print(f'task={task_id} stage=saved status=completed provenance={providers.provenance}', flush=True)
    except ResearchError as error:
        store.fail(task_id, error)
        status = 'cancelled' if error.code == 'cancelled' else 'failed'
        print(f'task={task_id} status={status} code={error.code}', flush=True)
    except Exception as error:
        store.fail(task_id, ResearchError('processing_failed', f'处理异常（{type(error).__name__}），未自动重试。'))
        print(f'task={task_id} status=failed code=processing_failed', flush=True)
    finally:
        usage = usage_summary(store.directory / 'jobs' / task_id / 'usage.jsonl')
        with store.database() as db:
            row = db.execute('SELECT checkpoint FROM tasks WHERE id=?', (task_id,)).fetchone()
            checkpoint = json.loads(row['checkpoint'])
            checkpoint['usage'] = usage
            db.execute('UPDATE tasks SET checkpoint=? WHERE id=?', (json.dumps(checkpoint), task_id))
    return True


def main():
    store = ResearchStore(os.environ.get('RESEARCH_DATA_DIR', '/data'))
    lock = (store.directory / 'worker.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print(f'worker_started interrupted={store.recover()} external_ai_enabled={os.environ.get("RESEARCH_AI_ENABLED") == "true"}', flush=True)
    providers = ResearchProviders()
    while True:
        if os.environ.get('RESEARCH_AI_ENABLED') != 'true' or not run_one(store, providers, authorize_task):
            time.sleep(2)


if __name__ == '__main__':
    main()
