import json
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock,patch
from lightroom_bridge import Bridge
from directory_transaction import DirectoryTransaction,OWNER_FILE
from delivery_cancel import cancellable,DeliveryStopped
from wechat_confirmation import attachment_confirmed
from wechat_visual import verified_search_contact,WeChatVisualSender,archive_label_matches


class Crash(BaseException):pass


class Sender:
    def connect(self,account):pass
    def send_files(self,paths):pass
    def send_message(self,text):pass


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.source=self.root/'image.jpg';self.source.write_bytes(b'photo')
        self.bridge=Bridge(self.root/'state',lambda _:Sender())
        self.addCleanup(self.bridge.project.close)

    def batch(self,name='A',account='wx_A',source=None):
        return dict(folder='catalog|'+name,person_key=name+account,name=name,contacts={'wechat':account},channel='wechat',
                    files=[str(source or self.source)],export_directory=str(self.root/'exports'),archive_directory=str(self.root/'exports'))

    def existing_directory(self):
        self.bridge.submit({'batches':[self.batch()]});self.bridge.run_next()
        directory=self.root/'exports'/'A_wx_A'
        (directory/'old.txt').write_text('keep')
        return directory

    def test_name_account_collision_preserves_other_owner(self):
        self.bridge.submit({'batches':[self.batch('A_B','user')]});self.bridge.run_next()
        old=self.root/'exports'/'A_B_user';(old/'keep.txt').write_text('keep')
        self.bridge.submit({'batches':[self.batch('A','B_user')]})
        tasks=self.bridge.tasks()
        self.assertNotEqual(tasks[0]['image_folders'],tasks[1]['image_folders'])
        self.assertTrue((old/'keep.txt').exists())
        owner=json.loads((Path(tasks[1]['image_folders'][0])/OWNER_FILE).read_text(encoding='utf-8'))
        self.assertEqual(owner['recipient'],{'wechat':'B_user'})

    def test_unmanaged_directory_is_not_cleared(self):
        directory=self.root/'exports'/'A_wx_A';directory.mkdir(parents=True)
        (directory/'keep.txt').write_text('private')
        self.bridge.submit({'batches':[self.batch()]})
        self.assertTrue((directory/'keep.txt').exists())
        self.assertNotEqual(self.bridge.tasks()[0]['image_folders'][0],str(directory))

    def test_save_failure_restores_directory_without_another_save(self):
        directory=self.existing_directory()
        with patch.object(self.bridge.project,'save',side_effect=OSError('disk full')) as save:
            with self.assertRaises(OSError):self.bridge.submit({'batches':[self.batch()]})
        self.assertEqual(save.call_count,1)
        self.assertTrue((directory/'old.txt').exists())
        self.assertFalse(list((self.root/'state'/'directory-transactions').glob('*.json')))
        self.assertEqual(len(self.bridge.tasks()),1)

    def test_crash_before_commit_recovers_on_restart(self):
        directory=self.existing_directory()
        with patch.object(self.bridge.project,'save',side_effect=Crash()):
            with self.assertRaises(Crash):self.bridge.submit({'batches':[self.batch()]})
        restarted=Bridge(self.root/'state',lambda _:Sender());self.addCleanup(restarted.project.close)
        self.assertTrue((directory/'old.txt').exists())
        self.assertEqual(len(restarted.tasks()),1)

    def test_crash_after_commit_keeps_new_directory(self):
        directory=self.existing_directory()
        with patch.object(DirectoryTransaction,'finish',side_effect=Crash()):
            with self.assertRaises(Crash):self.bridge.submit({'batches':[self.batch()]})
        restarted=Bridge(self.root/'state',lambda _:Sender());self.addCleanup(restarted.project.close)
        self.assertFalse((directory/'old.txt').exists())
        self.assertTrue((directory/'image.jpg').exists())
        self.assertEqual(len(restarted.tasks()),2)
        self.assertFalse(list((self.root/'exports').glob('.return-backup-*')))

    def test_cleanup_failure_does_not_report_submission_failure(self):
        self.existing_directory()
        with patch.object(DirectoryTransaction,'finish',side_effect=OSError('busy')):
            result=self.bridge.submit({'batches':[self.batch()]})
        self.assertEqual(len(result['tasks']),1)
        self.assertTrue(self.bridge.import_notice)
        restarted=Bridge(self.root/'state',lambda _:Sender());self.addCleanup(restarted.project.close)
        self.assertEqual(len(restarted.tasks()),2)

    def test_task_list_contains_more_than_fifty_tasks(self):
        self.bridge.project.data['submissions']=[dict(id=str(i),name='test',status='queued',items=[],group='g') for i in range(65)]
        self.assertEqual(len(self.bridge.tasks()),65)
        self.assertEqual(self.bridge.tasks()[0]['id'],'0')

    def test_save_failure_releases_sending_flag(self):
        self.bridge.submit({'batches':[self.batch()]})
        with patch.object(self.bridge.project,'save',side_effect=OSError('disk full')):
            self.bridge.run_next()
        self.assertFalse(self.bridge.sending)
        self.assertIsNone(self.bridge.current_task)
        self.assertTrue(self.bridge.paused)

    def test_copy_does_not_block_tasks_or_heartbeat(self):
        entered=threading.Event();release=threading.Event();errors=[]
        import shutil
        original=shutil.copyfileobj
        def slow(*args):
            entered.set()
            if not release.wait(5):raise RuntimeError('test timed out')
            return original(*args)
        def submit():
            try:self.bridge.submit({'batches':[self.batch()]})
            except Exception as exc:errors.append(exc)
        with patch('lightroom_import.shutil.copyfileobj',side_effect=slow):
            thread=threading.Thread(target=submit);thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.bridge.importing)
                done=threading.Event()
                def query():self.bridge.tasks();self.bridge.poll_toolbar();done.set()
                query_thread=threading.Thread(target=query);query_thread.start()
                self.assertTrue(done.wait(1),'task or heartbeat blocked behind copy')
                query_thread.join(1)
            finally:release.set();thread.join(5)
        self.assertFalse(errors)

    def test_ui_uses_cached_tasks_when_commit_lock_is_busy(self):
        self.bridge.submit({'batches':[self.batch()]})
        cached=self.bridge.tasks();locked=threading.Event();release=threading.Event()
        def hold():
            with self.bridge.project.lock:locked.set();release.wait(3)
        thread=threading.Thread(target=hold);thread.start();self.assertTrue(locked.wait(1))
        try:self.assertEqual(self.bridge.tasks(nonblocking=True),cached)
        finally:release.set();thread.join(3)

    def test_archive_cancelled_between_chunks_removes_partial_file(self):
        video=self.root/'video.mp4';video.write_bytes(b'x'*(3*1024*1024))
        sid=self.bridge.submit({'batches':[self.batch(source=video)]})['tasks'][0]
        event=threading.Event();original=zipfile._ZipWriteFile.write;writes=[]
        def write(stream,data):
            result=original(stream,data);writes.append(len(data));event.set();return result
        with patch.object(zipfile._ZipWriteFile,'write',write),cancellable(event):
            with self.assertRaises(DeliveryStopped):self.bridge.project.prepare_archive(sid,'wechat')
        self.assertEqual(writes,[1024*1024])
        self.assertFalse(list((self.root/'exports').rglob('*.zip.tmp')))
        self.assertFalse(list((self.root/'exports').rglob('*.zip')))

    def test_cancel_while_waiting_for_prefetch_does_not_wait_for_completion(self):
        sid=self.bridge.submit({'batches':[self.batch()]})['tasks'][0]
        waiting=threading.Event()
        class Pending(Future):
            def result(self,*args,**kwargs):waiting.set();return super().result(*args,**kwargs)
        self.bridge.prefetch_id=sid;self.bridge.prefetch_future=Pending()
        thread=threading.Thread(target=self.bridge.run_next);thread.start()
        try:
            self.assertTrue(waiting.wait(2));self.bridge.terminate(sid);thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(self.bridge.tasks()[0]['status'],'cancelled')
            self.assertTrue(self.bridge.prefetch_cancel.is_set())
        finally:
            self.bridge.prefetch_future.set_result(None);thread.join(2)

    def test_cancel_next_task_stops_actual_prefetch_and_removes_partial_zip(self):
        video=self.root/'video.mp4';video.write_bytes(b'x'*(3*1024*1024))
        sid=self.bridge.submit({'batches':[self.batch(source=video)]})['tasks'][0]
        entered=threading.Event();release=threading.Event();original=zipfile._ZipWriteFile.write
        def write(stream,data):
            entered.set();release.wait(3);return original(stream,data)
        with patch.object(zipfile._ZipWriteFile,'write',write):
            self.bridge.prepare_next_archive()
            try:
                self.assertTrue(entered.wait(2))
                self.bridge.resume({'id':sid,'resolution':'cancel'})
            finally:release.set();self.bridge.prefetch_future.result(timeout=3)
        self.assertEqual(self.bridge.tasks()[0]['status'],'cancelled')
        self.assertFalse(list((self.root/'exports').rglob('*.zip*')))

    def test_crash_between_directory_renames_recovers_old_files(self):
        directory=self.existing_directory()
        def partial_install(transaction):
            entry=transaction.entries[0]
            Path(entry['destination']).rename(entry['backup'])
            raise Crash()
        with patch.object(DirectoryTransaction,'install',partial_install):
            with self.assertRaises(Crash):self.bridge.submit({'batches':[self.batch()]})
        restarted=Bridge(self.root/'state',lambda _:Sender());self.addCleanup(restarted.project.close)
        self.assertTrue((directory/'old.txt').exists())
        self.assertEqual(len(restarted.tasks()),1)

    def test_unconfirmed_send_never_marks_photo_returned(self):
        batch=self.batch();batch['photos']=[dict(export_path=str(self.source),catalog='test',local_id='1',source_path='raw.cr2')]
        self.bridge.submit({'batches':[batch]})
        sender=Sender();sender.send_files=Mock(side_effect=RuntimeError('unconfirmed'))
        self.bridge.sender_factory=lambda _:sender;self.bridge.run_next()
        self.assertEqual(self.bridge.tasks()[0]['status'],'needs_review')
        self.assertEqual(self.bridge.photo_updates('test'),[])
        self.assertEqual(self.bridge.tasks()[0]['sent'],0)

    def test_confirm_sent_batch_continues_message_without_resending(self):
        self.bridge.submit({'batches':[self.batch(),self.batch('B','wx_B')]})
        failed=Sender();failed.send_files=Mock(side_effect=RuntimeError('unconfirmed'))
        self.bridge.sender_factory=lambda _:failed
        self.bridge.run_next()
        task=self.bridge.tasks()[0]
        self.assertEqual(task['status'],'needs_review')
        resumed=Sender();resumed.send_files=Mock();resumed.send_message=Mock()
        self.bridge.sender_factory=lambda _:resumed
        self.bridge.resume({'id':task['id'],'resolution':'skip'})
        self.bridge.run_next()
        resumed.send_files.assert_not_called()
        resumed.send_message.assert_called_once()
        self.assertEqual(self.bridge.tasks()[0]['status'],'submitted')
        self.bridge.run_next()
        self.assertEqual(self.bridge.tasks()[1]['status'],'submitted')

    def test_batch_sends_all_messages_before_review_and_survives_restart(self):
        events=[]
        class DeferredSender(Sender):
            def connect(self,account):self.account=account
            def send_files(self,paths):
                events.append(('send',self.account))
                self.pending_confirmation={'paths':[str(p) for p in paths],'before':{}}
            def send_message(self,text):events.append(('message',self.account))
            def verify_attachment(self,receipt):events.append(('review',self.account))
        self.bridge.sender_factory=lambda _:DeferredSender()
        self.bridge.submit({'batches':[self.batch(),self.batch('B','wx_B')]})
        self.bridge.run_next();self.bridge.run_next()
        self.assertEqual(events,[('send','wx_A'),('message','wx_A'),('send','wx_B'),('message','wx_B')])
        self.assertTrue(all(t['status']=='pending_review' for t in self.bridge.tasks()))
        self.assertEqual(self.bridge.photo_updates('test'),[])
        restored=Bridge(self.root/'state',lambda _:DeferredSender());self.addCleanup(restored.project.close)
        restored.paused=False
        with patch.object(restored.project,'deliver',side_effect=AssertionError('review must never deliver')):
            restored.run_next();restored.run_next()
        self.assertEqual(events[-2:],[('review','wx_A'),('review','wx_B')])
        self.assertTrue(all(t['status']=='submitted' for t in restored.tasks()))
        self.assertFalse(restored.run_next())

    def test_batch_review_failure_does_not_block_other_reviews(self):
        class DeferredSender(Sender):
            def connect(self,account):self.account=account
            def send_files(self,paths):self.pending_confirmation={'paths':[str(p) for p in paths],'before':{}}
            def verify_attachment(self,receipt):
                if self.account=='wx_A':raise ValueError('not visible')
        self.bridge.sender_factory=lambda _:DeferredSender()
        self.bridge.submit({'batches':[self.batch(),self.batch('B','wx_B')]})
        for _ in range(4):self.bridge.run_next()
        self.assertEqual([t['status'] for t in self.bridge.tasks()],['needs_review','submitted'])
        self.assertFalse(self.bridge.paused)

    def test_resume_many_restores_all_waiting_tasks(self):
        self.bridge.submit({'batches':[self.batch(),self.batch('B','wx_B')]})
        for record in self.bridge.project.data['submissions']:record['status']='waiting'
        ids=[r['id'] for r in self.bridge.tasks()]
        result=self.bridge.resume_many([{'id':sid} for sid in ids])
        self.assertEqual(result,{'resumed':ids,'errors':[]})
        self.assertTrue(all(r['status']=='queued' for r in self.bridge.tasks()))
        self.bridge.run_next();self.bridge.run_next()
        self.assertTrue(all(r['status']=='submitted' for r in self.bridge.tasks()))

    def test_window_resume_passes_entire_selection(self):
        from lightroom_task_window import TaskWindow
        window=Mock();window.table.selection.return_value=['a','b','c']
        window.records={sid:dict(id=sid,name=sid,status=state) for sid,state in [('a','waiting'),('b','waiting'),('c','submitted')]}
        window.bridge.resume_many.return_value={'resumed':['a','b'],'errors':[]}
        TaskWindow.resume(window)
        window.bridge.resume_many.assert_called_once_with([{'id':'a'},{'id':'b'}])


class WeChatEvidenceTests(unittest.TestCase):
    def line(self,text,x=100,y=160):return dict(text=text,words=[dict(x=x,y=y,w=180,h=20)])

    def test_ocr_requires_unique_labelled_full_account_and_nickname(self):
        name=self.line('Person',y=130);account=self.line('微信号：wx_test')
        result=verified_search_contact([name,account],'wx_test',1000,800)
        self.assertEqual(result,(account,'Person'))
        for lines in ([name,self.line('wx_test')],[name,self.line('微信号：wx_test1')],[name,account,account],[account]):
            self.assertIsNone(verified_search_contact(lines,'wx_test',1000,800))

    def test_search_enters_chat_without_labelled_account(self):
        window=Mock();window.rectangle.return_value.width.return_value=1000;window.rectangle.return_value.height.return_value=800
        sender=WeChatVisualSender(window);sender.snapshot=Mock(return_value=[]);sender.paste_text=Mock()
        sender.read_chat_title=Mock(return_value='Person');sender.check_target=Mock(return_value=True)
        with patch('wechat_visual.time.sleep'), patch('wechat_visual.wait_for',side_effect=lambda fn,*a,**k:fn()):
            sender.connect('wx_expected')
        sender.paste_text.assert_called_once_with('wx_expected')
        self.assertEqual(sum(call.args[0]=='{ENTER}' for call in window.type_keys.call_args_list),1)
        self.assertEqual(sender.nickname,'Person')

    def test_attachment_requires_new_evidence_and_no_draft(self):
        before={'files':{'a.zip':1},'draft':False}
        self.assertFalse(attachment_confirmed(['a.zip'],before,dict(before)))
        self.assertFalse(attachment_confirmed(['a.zip'],before,{'files':{'a.zip':2},'draft':True}))
        self.assertTrue(attachment_confirmed(['a.zip'],before,{'files':{'a.zip':2},'draft':False}))
        with self.assertRaises(ValueError):attachment_confirmed(['a.zip'],before,{'files':{'a.zip':2},'draft':False,'failed':1})

    def test_photo_confirmation_requires_every_attachment(self):
        before={'images':1,'draft':False}
        self.assertFalse(attachment_confirmed(['a.jpg','b.jpg'],before,{'images':2,'draft':False}))
        self.assertTrue(attachment_confirmed(['a.jpg','b.jpg'],before,{'images':3,'draft':False}))
        self.assertFalse(attachment_confirmed(['a.zip'],before,{'images':3,'draft':False}))

    def test_zip_card_confirmation_requires_new_card_and_empty_draft(self):
        before={'zip_cards':1,'draft':False}
        self.assertFalse(attachment_confirmed(['a.zip'],before,before))
        self.assertTrue(attachment_confirmed(['a.zip'],before,{'zip_cards':2,'draft':False}))
        self.assertFalse(attachment_confirmed(['a.zip'],before,{'zip_cards':2,'draft':True}))
        self.assertFalse(attachment_confirmed(['a.jpg'],before,{'zip_cards':2,'draft':False}))

    def test_visual_evidence_uses_toolbar_boundary_and_wrapped_filename(self):
        window=Mock();window.rectangle.return_value.width.return_value=1000;window.rectangle.return_value.height.return_value=800
        sender=WeChatVisualSender(window);sender.composer_top=680
        sender.snapshot=Mock(return_value=[self.line('person_',x=500,y=610),self.line('account.zip',x=500,y=635)])
        sender.zip_icons=Mock(return_value=[(750,610,790,665)])
        evidence=sender.attachment_evidence(['person_account.zip'])
        self.assertEqual(evidence['files']['person_account.zip'],1)
        self.assertEqual(evidence['zip_cards'],1)
        self.assertFalse(evidence['draft'])

    def test_similar_filename_cannot_confirm_another_attachment(self):
        self.assertFalse(archive_label_matches('11.jpg','1.jpg'))
        self.assertFalse(archive_label_matches('a.zip.bak','a.zip'))
        self.assertFalse(attachment_confirmed([],{},{}))

if __name__=='__main__':unittest.main()
