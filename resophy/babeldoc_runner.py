# BabelDOC adapter: GNU AGPL v3, matching BabelDOC 0.6.3 (see licenses/).
# Modified for explicit opt-in, no automatic retries and content-free usage.
import asyncio
import os
import sys
import uuid
import threading
from babeldoc import main
from babeldoc.translator.translator import OpenAITranslator
from resophy.research_usage import record_usage


class ManualRetryTranslator(OpenAITranslator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = self.client.with_options(max_retries=0)
        self.request_context = threading.local()

    def update_token_count(self, response):
        super().update_token_count(response)
        usage = response.usage.model_dump() if response.usage else {}
        record_usage(os.environ.get('RESEARCH_USAGE_PATH'), 'response_received', self.request_context.call_id, usage)

    def _request(self, method, *args, **kwargs):
        if (args and args[0] is None) or (not args and kwargs.get('text') is None):
            return method(self, *args, **kwargs)
        self.request_context.call_id = uuid.uuid4().hex
        record_usage(os.environ.get('RESEARCH_USAGE_PATH'), 'request_started', self.request_context.call_id)
        try:
            return method(self, *args, **kwargs)
        except Exception:
            record_usage(os.environ.get('RESEARCH_USAGE_PATH'), 'request_failed', self.request_context.call_id)
            raise

    def do_translate(self, *args, **kwargs):
        return self._request(OpenAITranslator.do_translate.__wrapped__, *args, **kwargs)

    def do_llm_translate(self, *args, **kwargs):
        return self._request(OpenAITranslator.do_llm_translate.__wrapped__, *args, **kwargs)


if __name__ == '__main__':
    if '--version' not in sys.argv and os.environ.get('RESEARCH_AI_ENABLED') != 'true':
        sys.exit('External AI is disabled; no translation was started.')
    main.OpenAITranslator = ManualRetryTranslator
    try:
        asyncio.run(main.main())
    except Exception as error:
        # Exception messages may contain provider credentials or paper content.
        diagnostic = os.environ.get('RESEARCH_DIAGNOSTIC_PATH')
        if diagnostic:
            import json
            from pathlib import Path
            Path(diagnostic).write_text(json.dumps({'error_type': type(error).__name__}))
        raise SystemExit(1)
