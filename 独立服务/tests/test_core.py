import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import DeepSeek, ServiceError, safe_url, source_list, validate_request, validate_summary


class KernelTests(unittest.TestCase):
    def test_fragment_ids_are_backfilled_and_unknown_ids_rejected(self):
        sources = source_list([('search', [{'url': 'https://example.com/',
                                        'summary': '景区须提前预约，政策可能变化。'}])])
        value = {'sections': [{'heading': '预约', 'items': [{'text': '需提前预约',
                              'evidence_ids': ['1-1']}]}]}
        result = validate_summary(value, sources)
        self.assertEqual(result['sections'][0]['items'][0]['evidence'][0]['quote'], sources[0]['summary'])
        value['sections'][0]['items'][0]['evidence_ids'] = ['1-999']
        with self.assertRaises(ServiceError):
            validate_summary(value, sources)

    def test_destination_cannot_inject_search_operators(self):
        for city in ['杭州 site:xiaohongshu.com', 'https://bad.example', '<script>']:
            with self.assertRaises(ServiceError):
                validate_request({'mode': 'travel', 'destination': city})
        with self.assertRaises(ServiceError):
            validate_request({'mode': 'wechat'})

    def test_source_allowlist_and_deduplication(self):
        rows = [dict(url=u, summary='这是可用摘要', title='测试') for u in [
            'https://www.xiaohongshu.com/a', 'https://xhslink.com/a',
            'javascript:alert(1)', 'https://you.ctrip.com/a?x=1',
            'https://you.ctrip.com/a?x=2', 'https://you.ctrip.com.evil.test/a']]
        self.assertEqual(len(source_list([('experience', rows)])), 1)
        self.assertIsNone(safe_url('https://user:password@example.com/'))

    def test_fabricated_citation_fails_closed(self):
        sources = [{'id': '1', 'summary': '景区须提前预约，政策可能变化。'}]
        value = {'sections': [{'heading': '资料', 'items': [{'text': '需要预约',
            'evidence': [{'source_id': '1', 'quote': '景区须提前预约'}]}]}]}
        self.assertTrue(validate_summary(value, sources)['sections'])
        value['sections'][0]['items'][0]['evidence'][0]['quote'] = '门票只要十元'
        with self.assertRaises(ServiceError):
            validate_summary(value, sources)

    def test_missing_sources_never_calls_model_or_fetch(self):
        calls = []
        def transport(path, body):
            calls.append((path, body)); return {'results': []}
        result = DeepSeek('test', transport=transport).run({'mode': 'search', 'query': '展览信息'})
        self.assertEqual(result['state'], 'empty')
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][1]['need_content'])

    def test_travel_budget_allowlist_and_partial_fallback(self):
        calls = []
        def transport(path, body):
            calls.append((path, body))
            if path=='/chat/completions' and '搜索规划模型' in body['messages'][0]['content']:
                return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'searches':[
                    {'kind':'experience','query':'杭州 旅游攻略 site:you.ctrip.com'},
                    {'kind':'official','query':'杭州 预约 site:gov.cn'}]})}}]}
            if path.endswith('search'):
                self.assertLessEqual(len(body['query']), 128)
                return {'results': [{'title': '杭州路线', 'url': 'https://you.ctrip.com/a',
                                     'summary': '西湖可以慢慢步行游览。'}]}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
                'sections': [{'heading': '行程', 'items': [{'text': '凭空写的行程',
                'evidence': [{'source_id': '99', 'quote': '不存在的摘要'}]}]}]})}}]}
        result = DeepSeek('test', transport=transport).run({'mode': 'travel',
                            'destination': '杭州', 'days': 3})
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(not b['need_content'] for p, b in calls if p.endswith('search')))
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(len(result['sources']), 1)
        self.assertNotIn('summary', result)
        self.assertFalse(any('fetch' in p for p, _ in calls))

    def test_model_output_cutoff_is_not_success(self):
        def transport(path, body):
            if path.endswith('search'):
                return {'results': [{'url': 'https://example.com/a', 'summary': '这是一段摘要'}]}
            return {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}
        result = DeepSeek('test', transport=transport).run({'mode': 'search', 'query': '查询测试'})
        self.assertEqual(result['state'], 'partial')


if __name__ == '__main__':
    unittest.main()
