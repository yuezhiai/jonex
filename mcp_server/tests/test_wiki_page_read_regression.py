"""Small offline regression for the bounded MCP article reader."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp_server import tools


class WikiPageReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_page_is_scoped_and_paginated(self):
        auth = SimpleNamespace(tenant_id='tenant', key_id='key')
        gateway = AsyncMock(return_value={'content': 'abcdefghij', 'engine_path': 'articles/a.md',
                                         'type': 'article', 'bundle_sha': 'sha',
                                         'references': [{'document_id': 'doc', 'raw_url': 'private'}]})
        with patch.object(tools, 'require_mcp_auth', return_value=auth), \
             patch.object(tools, '_check_service_scope', AsyncMock()) as scope, \
             patch.object(tools, '_resolve_wiki_kb_ids', AsyncMock(return_value=['kb'])) as resolve, \
             patch.object(tools, '_call_wiki_action_single', gateway), \
             patch.object(tools, 'touch_service_call_async'), \
             patch.object(tools, '_WIKI_BROWSE_ENABLED', True):
            result = await tools.read_wiki_page('service', 'kb', 'articles/a.md', 3, 4)
        self.assertEqual(result['content'], 'defg')
        self.assertEqual(result['next_offset'], 7)
        self.assertEqual(result['references'], [{'document_id': 'doc'}])
        scope.assert_awaited_once()
        resolve.assert_awaited_once_with(auth, 'service', 'kb', None)
        self.assertEqual(gateway.await_args.args[0], 'get_wiki_page')

    def test_page_filter_accepts_article(self):
        result = tools._project_wiki_pages('kb', {'pages': [
            {'path': 'article.md', 'type': 'article'}, {'path': 'concept.md', 'type': 'concept'}]},
            topic='', page_type='article', page=1, page_size=20)
        self.assertEqual([p['path'] for p in result['items']], ['article.md'])


if __name__ == '__main__':
    unittest.main()
