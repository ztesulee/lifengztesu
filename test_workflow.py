import json
import gzip
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from PIL import Image

from engine import Analyzer, collect, fingerprint, assign_groups, MODEL_ID
from project import Project
from server import AlbumServer

class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = Project(self.root / 'project', '测试拍摄')
        self.source = self.root / '原片.jpg'
        Image.new('RGB', (120, 100), '#d7a060').save(self.source)
        self.original = self.source.read_bytes()
        self.item = {'id': 'image1', 'name': self.source.name, 'path': str(self.source), 'kind': 'photo',
                     **fingerprint(self.source), 'groups': ['a'], 'reviewed': True, 'error': '', 'observations': []}
        self.project.data['groups'] = {
            'a': {'name': '模特A', 'token': 'token-a', 'published': True, 'contacts': {'qq': '540879838'}},
            'b': {'name': '模特B', 'token': 'token-b', 'published': True}}
        self.project.data['items'] = [self.item]
        self.project.save()
        Image.new('RGB', (90, 90), '#d7a060').save(self.project.cache / 'image1.webp')
        (self.project.cache / 'image1.jpg').write_bytes(self.original)
        self.server = AlbumServer(self.project, 0)
        self.base = f'http://127.0.0.1:{self.server.port}/a/'

    def tearDown(self):
        self.server.close()
        self.project.close()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None, method=None):
        headers = headers or {}
        if data is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(data).encode()
        return urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=headers, method=method), timeout=5)

    def wait_package(self):
        class FakeSender:
            def connect(inner, account):
                self.assertEqual(account, '540879838')
            def send_file(inner, path):
                self.assertEqual(path.read_bytes(), self.original)
        record = self.project.snapshot()['submissions'][-1]
        self.project.deliver(record['id'], FakeSender())
        return self.project.snapshot()['submissions'][-1]

    def test_compressed_selection_page(self):
        with self.request('token-a/', headers={'Accept-Encoding': 'gzip'}) as response:
            self.assertEqual(response.headers['Content-Encoding'], 'gzip')
            page = gzip.decompress(response.read()).decode()
            self.assertIn('确认提交选片', page)
            self.assertNotIn('下载 ZIP', page)

    def test_submit_pack_download_idempotence_and_revision(self):
        payload = {'ids': ['image1'], 'requestId': 'request_0000000001'}
        with self.request('token-a/submit', payload) as response:
            self.assertEqual(response.status, 202)
        result = self.wait_package()
        self.assertEqual(result['status'], 'submitted', result['error'])
        self.assertEqual(result['sent_files'], ['image1'])
        self.assertFalse(list(self.project.folder.rglob('*.zip')))
        with self.request('token-a/submit', payload) as response:
            self.assertEqual(json.load(response)['id'], result['id'])
        self.assertEqual(len(self.project.data['submissions']), 1)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('token-a/package/' + result['id'])
        self.assertEqual(error.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('token-b/package/' + result['id'])
        self.assertEqual(error.exception.code, 404)
        payload['requestId'] = 'request_0000000002'
        with self.request('token-a/submit', payload):
            pass
        second = self.wait_package()
        self.assertNotEqual(second['id'], result['id'])
        self.assertEqual(second['status'], 'submitted')

    def test_album_isolation_and_unpublished_access(self):
        with self.request('token-b/state') as response:
            self.assertEqual(json.load(response)['items'], [])
        for route in ('token-b/media/image1/view', 'token-a/../../project.json', 'invalid/state'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route)
            self.assertEqual(error.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('token-b/submit', {'ids': ['image1'], 'requestId': 'request_0000000001'})
        self.assertEqual(error.exception.code, 400)
        self.project.data['groups']['a']['published'] = False
        with self.assertRaises(urllib.error.HTTPError):
            self.request('token-a/state')

    def test_ranges_and_head(self):
        with self.request('token-a/media/image1/view', headers={'Range': 'bytes=3-15'}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.original[3:16])
        with self.request('token-a/media/image1/view', headers={'Range': 'bytes=-10'}) as response:
            self.assertEqual(response.read(), self.original[-10:])
        with self.request('token-a/media/image1/view', method='HEAD') as response:
            self.assertEqual(int(response.headers['Content-Length']), len(self.original))
            self.assertEqual(response.read(), b'')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('token-a/media/image1/view', headers={'Range': 'bytes=99999-'})
        self.assertEqual(error.exception.code, 416)

    def test_source_change_fails_without_partial_package(self):
        self.source.write_bytes(b'changed')
        self.project.submit('token-a', ['image1'], 'request_0000000001')
        result = self.wait_package()
        self.assertEqual(result['status'], 'error')
        self.assertFalse(list(self.project.folder.rglob('*.zip')))
        self.assertFalse(list(self.project.folder.rglob('*.part')))

    def test_restart_preserves_selection(self):
        self.project.submit('token-a', ['image1'], 'request_0000000001')
        result = self.wait_package()
        reopened = Project(self.project.folder)
        try:
            self.assertEqual(reopened.snapshot()['submissions'][0]['id'], result['id'])
            self.assertEqual(reopened.snapshot()['submissions'][0]['status'], 'submitted')
        finally:
            reopened.close()

    def test_uncertain_send_never_automatically_repeats(self):
        record = self.project.submit('token-a', ['image1'], 'request_0000000001')
        class InterruptedSender:
            def connect(inner, account):
                pass
            def send_file(inner, path):
                raise RuntimeError('发送结果不确定')
        self.project.deliver(record['id'], InterruptedSender())
        state = self.project.snapshot()['submissions'][0]
        self.assertEqual(state['status'], 'needs_review')
        self.assertEqual(state['inflight'], 'image1')
        class NeverSend:
            def connect(inner, account):
                self.fail('不应自动重发')
        self.project.deliver(record['id'], NeverSend())
        self.assertEqual(self.project.snapshot()['submissions'][0]['sent_files'], [])

    def test_flash_delivery_records_whole_batch(self):
        record = self.project.submit('token-a', ['image1'], 'request_0000000001')
        calls = []
        class FlashSender:
            def connect(inner, account):
                calls.append(account)
            def send_files(inner, paths):
                state = self.project.snapshot()['submissions'][0]
                self.assertEqual(state['inflight_batch'], ['image1'])
                calls.append(paths)
                self.assertEqual(len(paths), 1)
                self.assertEqual(paths[0], self.source.resolve())
                self.assertEqual(paths[0].read_bytes(), self.original)
            def send_message(inner, text):
                self.assertEqual(self.project.snapshot()['submissions'][0]['sent_files'], ['image1'])
                self.assertIn('老师您好，您的返图已全部发送完毕。', text)
                self.assertIn('\n日期：', text)
                self.assertTrue(text.endswith('\n（本消息为系统自动发送）'))
                self.assertEqual(self.project.snapshot()['submissions'][0]['completion_message_status'], 'started')
                calls.append(text)
        self.project.deliver(record['id'], FlashSender())
        state = self.project.snapshot()['submissions'][0]
        self.assertEqual(state['status'], 'submitted')
        self.assertEqual(state['sent_files'], ['image1'])
        self.assertEqual(state['inflight_batch'], [])
        self.assertEqual(state['delivery_method'], 'qq_flash')
        self.assertFalse(list(self.project.folder.rglob('*.zip')))
        self.assertEqual(len(calls), 3)
        self.assertEqual(state['completion_message_status'], 'sent')

    def test_uncertain_flash_batch_is_not_repeated(self):
        record = self.project.submit('token-a', ['image1'], 'request_0000000001')
        class FlashSender:
            def connect(inner, account):
                pass
            def send_files(inner, paths):
                raise RuntimeError('闪传结果不确定')
        self.project.deliver(record['id'], FlashSender())
        state = self.project.snapshot()['submissions'][0]
        self.assertEqual(state['status'], 'needs_review')
        self.assertEqual(state['inflight_batch'], ['image1'])
        self.assertEqual(state['sent_files'], [])

    def test_zip_duplicate_names_are_preserved_and_wechat_routes_by_bound_id(self):
        other_folder = self.root / 'other'
        other_folder.mkdir()
        other = other_folder / self.source.name
        other.write_bytes(b'another original')
        second = dict(self.item, id='image2', path=str(other), **fingerprint(other))
        self.project.data['items'].append(second)
        self.project.data['groups']['a']['contacts'] = {'wechat':'wx_test123'}
        self.project.save()
        record = self.project.queue_direct_albums(['a'])[0]
        calls = []
        class Sender:
            def connect(inner, account):
                self.assertEqual(account, 'wx_test123')
                calls.append('connect')
            def send_files(inner, paths):
                self.assertEqual(len(paths), 1)
                self.assertRegex(paths[0].name, r'^模特A\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:_\d+)?\.zip$')
                self.assertEqual(paths[0].parent, self.project.group_folder('a'))
                with zipfile.ZipFile(paths[0]) as package:
                    self.assertEqual(package.read(self.source.name), self.original)
                    self.assertEqual(package.read(self.source.stem+'_2.jpg'), b'another original')
                    self.assertIsNone(package.testzip())
                calls.append('send')
        self.project.deliver(record['id'], Sender())
        state = self.project.snapshot()['submissions'][0]
        self.assertEqual(state['status'], 'submitted', state['error'])
        self.assertEqual(state['sent_files'], ['image1','image2'])
        self.assertEqual(state['delivery_method'], 'wechat_zip')
        self.project.deliver(record['id'], Sender())
        self.assertEqual(calls, ['connect','send'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_resumed_task_zip_contains_only_unsent_originals(self):
        other = self.root / 'second.jpg'
        other.write_bytes(b'unsent original')
        self.project.data['items'].append(dict(self.item, id='image2', name=other.name, path=str(other), **fingerprint(other)))
        self.project.data['groups']['a']['contacts'] = {'wechat':'wx_test123'}
        record = self.project.submit('token-a', ['image1','image2'], 'request_resume_001')
        self.project.data['submissions'][0]['sent_files'] = ['image1']
        class Sender:
            def connect(inner, account): pass
            def send_files(inner, paths):
                with zipfile.ZipFile(paths[0]) as package:
                    self.assertEqual(package.namelist(), ['second.jpg'])
                    self.assertEqual(package.read('second.jpg'), b'unsent original')
        self.project.deliver(record['id'], Sender())
        self.assertEqual(self.project.snapshot()['submissions'][0]['sent_files'], ['image1','image2'])

    def test_folder_scope_and_analyzer(self):
        nested = self.root / 'source' / 'photos'
        nested.mkdir(parents=True)
        image = nested / '测试.jpg'
        image.write_bytes(self.original)
        (nested / 'unsupported.txt').write_bytes(b'text')
        paths, skipped = collect(nested.parent)
        self.assertEqual(paths, [image])
        self.assertEqual(len(skipped), 1)
        self.assertNotIn(self.source, paths)
        analyzer = Analyzer()
        item = analyzer.analyze(image, self.root / 'analysis', lambda _:None, threading.Event())
        self.assertEqual(item['observations'], [])
        self.assertTrue((self.root / 'analysis' / (item['id'] + '.webp')).exists())

    def test_ambiguous_grouping_remains_unreviewed(self):
        first = [1.] + [0.] * 511
        second = [0., 1.] + [0.] * 510
        items = [dict(id=str(n), groups=[], reviewed=False, error='', note='', analysis_model=MODEL_ID,
                      observations=[{'face':vector, 'cloth':None}]) for n, vector in enumerate((first, first, second))]
        groups = {}
        assign_groups(items, groups)
        self.assertEqual(items[0]['groups'], items[1]['groups'])
        self.assertNotEqual(items[0]['groups'], items[2]['groups'])
        self.assertTrue(all(not i['reviewed'] for i in items))

    def test_legacy_features_never_compared_to_insightface(self):
        groups = {'old': {'name': '旧人物'}}
        items = [dict(id='old', groups=['old'], reviewed=True, observations=[{'face':[1.] * 128, 'cloth':None}]),
                 dict(id='new', groups=[], reviewed=False, error='', note='', analysis_model=MODEL_ID,
                      observations=[{'face':[1.] + [0.] * 511, 'cloth':None}])]
        assign_groups(items, groups)
        self.assertEqual(items[0]['groups'], ['old'])
        self.assertNotIn('old', items[1]['groups'])

    def test_model_switch_isolates_same_length_embeddings(self):
        from model_options import OPTIONS
        fast = OPTIONS['fast']['id']
        vector = [1.] + [0.] * 511
        groups = {'balanced_person': {'name': '原人物'}}
        items = [dict(id='a', groups=['balanced_person'], reviewed=True, analysis_model=MODEL_ID,
                      observations=[{'face':vector, 'cloth':None}]),
                 dict(id='b', groups=[], reviewed=False, error='', note='', analysis_model=fast,
                      observations=[{'face':vector, 'cloth':None}])]
        assign_groups(items, groups, model_id=fast)
        self.assertNotIn('balanced_person', items[1]['groups'])

if __name__ == '__main__':
    unittest.main()
