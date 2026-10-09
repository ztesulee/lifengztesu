import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from PIL import Image
from lightroom_bridge import Bridge, make_handler


class Sender:
    def __init__(self):self.paths=[]
    def connect(self, account):self.account=account
    def send_files(self, paths):self.paths=list(paths)
    def send_message(self, text):self.message=text


class LightroomTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.senders=[]
        def factory(task):
            sender=Sender();self.senders.append(sender);return sender
        self.bridge=Bridge(self.root/'state',factory)
        self.addCleanup(self.bridge.project.close)
        self.jpg=self.root/'成片.jpg';Image.new('RGB',(20,20),'red').save(self.jpg)
        self.video=self.root/'video.mp4';self.video.write_bytes(b'video-test')

    def batch(self, channel='qq', paths=None):
        return dict(folder='catalog|person',name='小沐',contacts={channel:'12345678' if channel=='qq' else '小沐老师'},
                    channel=channel,files=[str(p) for p in (paths or [self.jpg])])

    def test_person_directories_and_archive_locations(self):
        qq=self.batch();wx=self.batch('wechat',[self.video]);wx.update(name='另一位',folder='catalog|other')
        for batch in (qq,wx):batch['export_directory']=str(self.root/'exports')
        wx['archive_directory']=str(self.root/'archives')
        self.bridge.submit({'batches':[qq,wx]})
        qq_path=self.root/'exports'/'小沐_12345678'/self.jpg.name
        wx_path=self.root/'exports'/'另一位_小沐老师'/self.video.name
        self.assertEqual(qq_path.read_bytes(),self.jpg.read_bytes())
        self.assertEqual(wx_path.read_bytes(),self.video.read_bytes())
        self.assertTrue(self.bridge.run_next());self.assertTrue(self.bridge.run_next())
        self.assertEqual(self.senders[0].paths,[qq_path])
        self.assertEqual(self.senders[1].paths[0].parent,self.root/'archives'/'另一位_小沐老师')
        before=qq_path.read_bytes()
        (qq_path.parent/'旧照片.jpg').write_bytes(b'old')
        self.bridge.submit({'batches':[qq]})
        self.assertEqual(qq_path.read_bytes(),before)
        self.assertFalse((qq_path.parent/'成片_2.jpg').exists())
        self.assertFalse((qq_path.parent/'旧照片.jpg').exists())

    def test_replace_directory_blocks_pending_and_rolls_back_failure(self):
        batch=self.batch()
        batch['export_directory']=str(self.root/'exports')
        batch['archive_directory']=str(self.root/'exports')
        self.bridge.submit({'batches':[batch]})
        directory=self.root/'exports'/'小沐_12345678'
        with self.assertRaisesRegex(ValueError,'未完成任务'):
            self.bridge.submit({'batches':[batch]})
        self.assertTrue((directory/self.jpg.name).exists())
        self.assertTrue(self.bridge.run_next())
        old=(directory/self.jpg.name).read_bytes()
        (directory/'old.zip').write_bytes(b'old-zip')
        with patch('lightroom_bridge.shutil.copyfileobj',side_effect=OSError('copy failed')):
            with self.assertRaises(OSError):self.bridge.submit({'batches':[batch]})
        self.assertEqual((directory/self.jpg.name).read_bytes(),old)
        self.assertTrue((directory/'old.zip').exists())
        self.bridge.submit({'batches':[batch]})
        self.assertFalse((directory/'old.zip').exists())

    def test_editable_message_keeps_fixed_fields_and_persists(self):
        body='照片已提交，请查收。\n感谢配合！'
        self.bridge.save_message_body(body)
        self.assertEqual(self.bridge.project.snapshot()['lightroom_message_body'],body)
        self.bridge.submit({'batches':[self.batch()]})
        self.assertTrue(self.bridge.run_next())
        message=self.senders[0].message
        self.assertIn('QQ/微信号：12345678\n小沐老师您好，\n'+body,message)
        self.assertIn('\n日期：',message)
        self.assertTrue(message.endswith('（本消息为系统自动发送）'))
        self.assertEqual(self.bridge.project.snapshot()['submissions'][0]['completion_message_body'],body)
        for invalid in ('', ' '*3, 'a'*2001):
            with self.assertRaises(ValueError):self.bridge.save_message_body(invalid)

    def test_person_directory_default_zip_and_photo_sync(self):
        batch=self.batch('wechat',[self.video])
        batch['export_directory']=str(self.root/'exports')
        batch['archive_directory']=str(self.root/'exports')
        batch['photos']=[dict(export_path=str(self.video),source_path='original.mp4',catalog='catalog',local_id='7')]
        self.bridge.submit({'batches':[batch]})
        self.assertTrue(self.bridge.run_next())
        self.assertEqual(self.senders[0].paths[0].parent,self.root/'exports'/'小沐_小沐老师')
        updates=self.bridge.photo_updates('catalog')
        self.assertEqual(updates[0]['source_path'],'original.mp4')
        self.assertEqual(Path(updates[0]['export_path']).parent,self.root/'exports'/'小沐_小沐老师')

    def test_mixed_delivery_and_idempotency(self):
        qq=self.batch();wx=self.batch('wechat',[self.video]);wx['folder']='catalog|other'
        response=self.bridge.submit({'batches':[qq,wx]})
        self.assertEqual(len(response['tasks']),2)
        self.assertTrue(self.bridge.run_next());self.assertTrue(self.bridge.run_next())
        self.assertEqual(self.senders[0].paths,[self.jpg.resolve()])
        self.assertEqual(self.senders[1].paths[0].suffix,'.zip')
        self.assertIn('小沐老师您好',self.senders[0].message)
        self.assertEqual(self.bridge.submit({'batches':[qq,wx]}),response)
        self.assertFalse(self.bridge.run_next())
        self.assertTrue(all(r['status']=='submitted' for r in self.bridge.tasks()))
        states=self.bridge.folders()
        self.assertEqual(states[qq['folder']]['status'],'submitted')
        self.assertEqual(states[wx['folder']]['count'],1)
        for group in self.bridge.project.data['groups'].values():group.pop('lightroom_folder')
        self.assertEqual(self.bridge.folders([qq['folder']])[qq['folder']]['status'],'submitted')

    def test_invalid_batch_does_not_create_partial_queue(self):
        invalid=self.batch(paths=[self.root/'missing.jpg'])
        with self.assertRaises(OSError):self.bridge.submit({'batches':[self.batch(),invalid]})
        self.assertEqual(self.bridge.tasks(),[])
        raw=self.root/'raw.nef';raw.write_bytes(b'raw')
        with self.assertRaises(ValueError):self.bridge.submit({'batches':[self.batch(paths=[raw])]})
        self.assertEqual(self.bridge.tasks(),[])

    def test_metadata_recipients_in_one_folder_stay_separate(self):
        first=self.batch();first['person_key']='person-A'
        second=self.batch('wechat',[self.video]);second.update(person_key='person-B',name='另一位')
        self.bridge.submit({'batches':[first,second]})
        self.assertTrue(self.bridge.run_next());self.assertTrue(self.bridge.run_next())
        self.assertEqual(self.senders[0].account,'12345678')
        self.assertEqual(self.senders[1].account,'小沐老师')
        self.assertIn('小沐老师您好',self.senders[0].message)
        self.assertIn('另一位老师您好',self.senders[1].message)

    def test_restart_pauses_and_resume_is_explicit(self):
        sid=self.bridge.submit({'batches':[self.batch()]})['tasks'][0]
        reopened=Bridge(self.root/'state',lambda task:Sender())
        try:
            self.assertFalse(reopened.run_next())
            reopened.resume({'id':sid})
            self.assertTrue(reopened.run_next())
        finally:reopened.project.close()

    def test_photo_updates_only_after_submission_and_once_acknowledged(self):
        batch=self.batch()
        batch['photos']=[dict(catalog='catalog-path',local_id='12',source_path='source.nef',export_path=str(self.jpg))]
        self.bridge.submit({'batches':[batch]})
        self.assertEqual(self.bridge.photo_updates('catalog-path'),[])
        self.bridge.run_next()
        updates=self.bridge.photo_updates('catalog-path')
        self.assertEqual(len(updates),1)
        self.assertEqual(updates[0]['local_id'],'12')
        self.assertEqual(self.bridge.photo_updates('other-catalog'),[])
        self.bridge.acknowledge_updates(dict(catalog='catalog-path',ids=[updates[0]['id']]))
        self.assertEqual(self.bridge.photo_updates('catalog-path'),[])

    def test_wechat_eight_photos_direct_nine_zip(self):
        paths=[]
        for index in range(9):
            path=self.root/(str(index)+'.jpg');Image.new('RGB',(20,20),'red').save(path);paths.append(path)
        self.bridge.submit({'batches':[self.batch('wechat',paths[:8])]})
        self.bridge.run_next()
        self.assertEqual(self.senders[-1].paths,paths[:8])
        self.bridge.submit({'batches':[self.batch('wechat',paths)]})
        self.bridge.run_next()
        self.assertEqual(len(self.senders[-1].paths),1)
        self.assertEqual(self.senders[-1].paths[0].suffix,'.zip')

    def test_uncertain_requires_resolution(self):
        sid=self.bridge.submit({'batches':[self.batch()]})['tasks'][0]
        record=self.bridge.project.data['submissions'][0]
        record.update(status='needs_review',inflight=record['items'][0],inflight_batch=record['items'][:])
        with self.assertRaises(ValueError):self.bridge.resume({'id':sid})
        self.bridge.resume({'id':sid,'resolution':'skip'})
        self.assertEqual(record['sent_files'],record['items'])
        self.assertTrue(self.bridge.run_next())
        self.assertFalse(self.senders[0].paths)

    def test_old_waiting_does_not_block_new_task_and_cancelled_can_resume(self):
        sid=self.bridge.submit({'batches':[self.batch()]})['tasks'][0]
        first=self.bridge.project.data['submissions'][0]
        first['status']='waiting'
        second=self.batch('wechat',[self.video]);second['folder']='catalog|new'
        self.bridge.submit({'batches':[second]})
        self.assertTrue(self.bridge.run_next())
        self.assertEqual(first['status'],'waiting')
        self.bridge.resume({'id':sid,'resolution':'cancel'})
        self.bridge.paused=True
        self.bridge.resume({'id':sid})
        self.assertFalse(self.bridge.paused)
        self.assertTrue(self.bridge.run_next())
        self.assertEqual(first['status'],'submitted')

    def test_cancelled_resume_checks_files_and_uncertain_batch(self):
        sid=self.bridge.submit({'batches':[self.batch()]})['tasks'][0]
        first=self.bridge.project.data['submissions'][0]
        first.update(status='cancelled',inflight=first['items'][0],inflight_batch=first['items'][:])
        with self.assertRaisesRegex(ValueError,'核对客户端'):self.bridge.resume({'id':sid})
        self.bridge.resume({'id':sid,'resolution':'skip'})
        self.assertEqual(first['sent_files'],first['items'])
        first['status']='cancelled'
        self.jpg.unlink()
        with self.assertRaisesRegex(ValueError,'重新导出'):self.bridge.resume({'id':sid})

    def test_terminate_active_before_files_and_delete(self):
        sid=self.bridge.submit({'batches':[self.batch()]})['tasks'][0]
        bridge=self.bridge
        class StopSender(Sender):
            def connect(self,account):bridge.terminate(sid)
        self.bridge.sender_factory=lambda task:StopSender()
        self.assertTrue(self.bridge.run_next())
        record=self.bridge.project.data['submissions'][0]
        self.assertEqual(record['status'],'cancelled')
        self.assertEqual(record['sent_files'],[])
        self.assertTrue(self.bridge.paused)
        self.assertEqual(self.bridge.delete_tasks([sid]),1)
        self.assertTrue(self.jpg.exists())
        self.assertTrue(list((self.bridge.project.folder/'backups').glob('before_delete_tasks_*.json')))

    def test_http_auth_and_submission(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.bridge,'test-token'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(server.server_port)
            with self.assertRaises(HTTPError) as err:urlopen(url+'/tasks')
            self.assertEqual(err.exception.code,403)
            self.assertEqual(self.bridge.plugin_seen,0)
            with urlopen(Request(url+'/health',headers={'X-Return-Token':'test-token'})) as response:
                self.assertEqual(response.status,200)
            self.assertGreater(self.bridge.plugin_seen,0)
            payload=json.dumps({'batches':[self.batch()]}).encode()
            request=Request(url+'/submit',payload,{'X-Return-Token':'test-token','Content-Type':'application/json'})
            with urlopen(request) as response:self.assertEqual(len(json.load(response)['tasks']),1)
        finally:server.shutdown();server.server_close();thread.join()

    def test_export_progress(self):
        self.bridge.report_export(dict(done=2,total=10,state='running',message='2/10'))
        self.assertEqual(self.bridge.export_progress['done'],2)
        self.bridge.report_export(dict(done=2,total=10,state='error',message='cancelled'))
        self.assertEqual(self.bridge.export_progress['state'],'error')
        with self.assertRaises(ValueError):self.bridge.report_export(dict(done=11,total=10))

    def test_next_archive_prepared_during_current_send_and_reused(self):
        first=self.batch()
        second=self.batch('wechat',[self.video]);second['folder']='catalog|next'
        self.bridge.submit({'batches':[first,second]})
        bridge=self.bridge
        class OverlapSender(Sender):
            def connect(self,account):
                super().connect(account)
                self_test.assertTrue(bridge.sending)
                bridge.prefetch_future.result(timeout=5)
                self_test.assertEqual(bridge.project.snapshot()['submissions'][1]['status'],'queued')
                self_test.assertTrue(Path(bridge.project.snapshot()['submissions'][1]['archive_path']).is_file())
        self_test=self
        self.bridge.sender_factory=lambda task:OverlapSender() if task['recipient'].get('qq') else Sender()
        self.assertTrue(self.bridge.run_next())
        archive=Path(self.bridge.project.snapshot()['submissions'][1]['archive_path'])
        before=archive.stat().st_mtime_ns
        with patch('project.zipfile.ZipFile',side_effect=AssertionError('cached ZIP must not be rebuilt')):
            self.assertTrue(self.bridge.run_next())
        self.assertEqual(self.bridge.tasks()[1]['status'],'submitted')
        self.assertEqual(archive.stat().st_mtime_ns,before)

    def test_changed_cached_archive_is_rebuilt(self):
        sid=self.bridge.submit({'batches':[self.batch('wechat',[self.video])]})['tasks'][0]
        archive=self.bridge.project.prepare_archive(sid,'wechat')
        archive.write_bytes(b'changed')
        rebuilt=self.bridge.project.prepare_archive(sid,'wechat')
        import zipfile
        with zipfile.ZipFile(rebuilt) as package:
            self.assertEqual(package.read(self.video.name),self.video.read_bytes())

    def test_toolbar_commands_connected_single_dispatch_and_finish(self):
        with self.assertRaises(ValueError):self.bridge.request_toolbar('selected')
        self.assertEqual(self.bridge.poll_toolbar(),{})
        for action in ('selected','folder','bind','bind_parent','folder_status','settings'):
            ident=self.bridge.request_toolbar(action)
            with self.assertRaises(ValueError):self.bridge.request_toolbar(action)
            self.assertEqual(self.bridge.poll_toolbar(),dict(id=ident,action=action))
            self.assertEqual(self.bridge.poll_toolbar(),{})
            self.bridge.finish_toolbar(dict(id=ident,ok=True))
        self.bridge.sending=True
        with self.assertRaises(ValueError):self.bridge.request_toolbar('selected')
        self.bridge.sending=False

    def test_toolbar_idle_heartbeat_recovers_lost_completion(self):
        self.bridge.poll_toolbar()
        ident=self.bridge.request_toolbar('selected')
        self.bridge.poll_toolbar({'running':''})
        self.bridge.poll_toolbar({'running':ident})
        self.assertIsNotNone(self.bridge.toolbar_active)
        self.bridge.poll_toolbar({'running':''})
        self.assertIsNone(self.bridge.toolbar_active)

    def test_late_failure_receipt_survives_idle_heartbeat(self):
        self.bridge.poll_toolbar()
        ident=self.bridge.request_toolbar('selected')
        self.bridge.poll_toolbar({'running':''})
        self.bridge.poll_toolbar({'running':ident})
        self.bridge.poll_toolbar({'running':''})
        self.bridge.finish_toolbar({'id':ident,'ok':False,'error':'test dispatch error'})
        self.assertIn('test dispatch error',self.bridge.toolbar_message)
        self.bridge.request_toolbar('selected')
        self.bridge.poll_toolbar({'running':''})
        dispatched=self.bridge.toolbar_active['dispatched']
        with patch('lightroom_bridge.time.monotonic',return_value=dispatched+11):
            self.bridge.poll_toolbar({'running':''})
        self.assertIsNone(self.bridge.toolbar_active)


if __name__=='__main__':unittest.main()
