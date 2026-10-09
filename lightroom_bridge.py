"""Local Lightroom handoff: folder contacts in Lua, durable delivery in Python."""
import json
import hashlib
import secrets
import sys
import threading
import time
import re
import shutil
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.deps'))
from project import Project, person_recipient
from media import fingerprint, VIDEOS

DEFAULT_MESSAGE_BODY='您的返图已全部发送完毕。感谢您的出镜与配合，辛苦您收到后回复确认，祝您生活愉快！'


def recipient_folder(name, recipient):
    raw=name+'_'+(recipient.get('qq') or recipient.get('wechat') or '')
    safe=re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw).rstrip(' .')
    if safe != raw:
        safe+='_'+hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]
    if len(safe)>160:
        safe=safe[:140]+'_'+hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]
    return safe


class Bridge:
    def __init__(self, folder, sender_factory=None):
        from directory_transaction import recover_transactions
        recover_transactions(folder)
        self.project = Project(folder, 'Lightroom 返图')
        self.lock = threading.RLock()
        self.sender_factory = sender_factory
        self.sending = False
        self.paused = False
        self.current_task = None
        self.cancel_event = threading.Event()
        self.toolbar_pending = None
        self.toolbar_active = None
        self.toolbar_last_id = None
        self.plugin_seen = 0
        self.toolbar_message = '等待 Lightroom 插件连接'
        self.export_progress={'state':'idle','done':0,'total':0,'message':'尚未开始导出'}
        self.prefetch_future=None
        self.prefetch_id=None
        self.prefetch_message=''
        self.prefetch_cancel=threading.Event()
        self.import_lock=threading.Lock()
        self.importing=False
        self.import_notice=''
        self.task_cache=[]

    def prepare_next_archive(self):
        with self.lock:
            if self.paused or (self.prefetch_future and not self.prefetch_future.done()):return
            tasks=self.project.snapshot()['submissions']
            task=next((r for r in tasks if r['status']=='queued' and r['id']!=self.current_task),None)
            if not task or not task.get('recipient',{}).get('wechat'):return
            if len(task['files'])<9 and all(f.get('kind')=='photo' for f in task['files']):return
            remaining=[f for f in task['files'] if f['id'] not in task.get('sent_files',[])]
            if not remaining:return
            self.prefetch_id=task['id']
            self.prefetch_cancel=threading.Event()
            cancel=self.prefetch_cancel
            self.prefetch_message='提前打包：'+task['name']
            def prepare():
                try:
                    from delivery_cancel import cancellable
                    with cancellable(cancel):self.project.prepare_archive(task['id'],'wechat',remaining)
                    with self.lock:self.prefetch_message='压缩包已准备：'+task['name']
                except Exception as exc:
                    from delivery_cancel import DeliveryStopped
                    with self.lock:self.prefetch_message=('预打包已停止：' if isinstance(exc,DeliveryStopped) else '提前打包失败，发送时重试：')+task['name']+' · '+str(exc)
            self.prefetch_future=self.project.workers.submit(prepare)

    def report_export(self,value):
        done,total=value.get('done',0),value.get('total',0)
        state=value.get('state','running')
        if type(done)!=int or type(total)!=int or not 0<=done<=total or state not in ('running','done','error'):
            raise ValueError('导出进度格式不正确')
        with self.lock:
            self.export_progress={'state':state,'done':done,'total':total,'message':str(value.get('message',''))[:400]}
        return {'ok':True}

    def message_body(self):
        return self.project.snapshot().get('lightroom_message_body',DEFAULT_MESSAGE_BODY)

    def save_message_body(self,body):
        if not isinstance(body,str) or not body.strip():raise ValueError('留言正文不能为空')
        if len(body)>2000:raise ValueError('留言正文最多 2000 个字符')
        with self.project.lock:
            self.project.data['lightroom_message_body']=body.strip()
            self.project.save()

    def request_toolbar(self, action):
        if action not in ('selected', 'folder', 'bind', 'bind_parent', 'folder_status', 'settings'):raise ValueError('工具栏操作不存在')
        with self.lock:
            if self.importing:raise ValueError('正在整理导出素材，请等待任务创建完成')
            if self.sending:raise ValueError('正在操作 QQ / 微信，请等待发送结束')
            if time.monotonic()-self.plugin_seen>15:raise ValueError('等待 Lightroom 插件自动连接，请确认插件已启用并重新载入最新版本')
            if self.toolbar_active:
                raise ValueError('已有工具栏操作正在处理，请先完成或取消 Lightroom 中打开的窗口')
            command={'id':secrets.token_hex(12),'action':action,'created':time.monotonic()}
            self.toolbar_pending=command;self.toolbar_active=command
            self.toolbar_last_id=command['id']
            self.toolbar_message='等待 Lightroom 打开操作窗口'
            return command['id']

    def poll_toolbar(self, value=None):
        with self.lock:
            self.plugin_seen=time.monotonic()
            active=self.toolbar_active
            if value is not None and active and active.get('dispatched'):
                if value.get('running')==active['id']:
                    active['confirmed']=True
                elif active.get('confirmed') or time.monotonic()-active['dispatched']>10:
                    self.toolbar_active=None
                    self.toolbar_message='上一次操作已结束或未启动，可以重新点击'
            command=self.toolbar_pending
            self.toolbar_pending=None
            if not command:
                if not self.toolbar_active and self.toolbar_message=='等待 Lightroom 插件连接':
                    self.toolbar_message='Lightroom 已连接，可以使用工具栏'
                return {}
            if time.monotonic()-command['created']>30:
                self.toolbar_active=None;self.toolbar_message='请求超时，请重新点击'
                return {}
            self.toolbar_message='Lightroom 正在处理工具栏操作'
            command['dispatched']=time.monotonic()
            return {'id':command['id'],'action':command['action']}

    def finish_toolbar(self,value):
        with self.lock:
            if self.toolbar_last_id==value.get('id') and (not self.toolbar_active or self.toolbar_active['id']==value.get('id')):
                self.toolbar_active=None
                self.toolbar_message='操作窗口已结束；已创建的任务可在任务状态查看' if value.get('ok') else '操作失败：'+str(value.get('error','未知错误'))
        return {'ok':True}

    def submit(self, value):
        batches = value.get('batches', [])
        if not isinstance(batches, list) or not batches:
            raise ValueError('没有导出素材')
        prepared = []
        for batch in batches:
            if not isinstance(batch, dict):raise ValueError('人物任务格式不正确')
            name = str(batch.get('name', '')).strip()
            source = str(batch.get('folder', '')).strip()
            if not name or not source:
                raise ValueError('缺少人物名称或文件夹')
            contacts=batch.get('contacts', {})
            if not isinstance(contacts, dict) or any(not isinstance(contacts.get(k, ''), str) for k in ('qq', 'wechat')):
                raise ValueError('账号格式不正确')
            group = dict(name=name, contacts=contacts, default_channel=batch.get('channel', 'default'), published=False, lightroom_folder=source)
            folders=batch.get('folders',[source])
            if not isinstance(folders,list) or any(not isinstance(p,str) for p in folders):raise ValueError('来源文件夹格式不正确')
            group['lightroom_folders']=list(dict.fromkeys([source]+folders))
            recipient=person_recipient(group)
            person_key=batch.get('person_key', '')
            if not isinstance(person_key,str):raise ValueError('人物标识格式不正确')
            gid = hashlib.sha256((source+('\0'+person_key if person_key else '')).encode('utf-8')).hexdigest()[:24]
            files = []
            refs=batch.get('photos', [])
            if not isinstance(refs,list) or any(not isinstance(ref,dict) for ref in refs):raise ValueError('照片关联格式不正确')
            by_export={str(Path(ref['export_path']).resolve()):ref for ref in refs}
            for entry in batch.get('files', []):
                path = Path(entry).resolve(strict=True)
                if not path.is_file() or path.suffix.lower() not in {'.jpg', '.jpeg'} | VIDEOS:
                    raise ValueError('只接收导出的 JPEG 或视频：' + path.name)
                files.append(dict(id=hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:24],
                    path=str(path), name=path.name, kind='video' if path.suffix.lower() in VIDEOS else 'photo',
                    groups=[gid], reviewed=True, error='', **fingerprint(path)))
                if str(path) in by_export:files[-1]['lightroom_photo']=by_export[str(path)]
            if not files:
                raise ValueError(name + '：没有素材')
            archive_directory=batch.get('archive_directory','')
            if not isinstance(archive_directory,str):raise ValueError('压缩包保存路径格式不正确')
            archive_directory=archive_directory.strip()
            if archive_directory and not Path(archive_directory).is_absolute():raise ValueError('压缩包保存位置必须为完整路径')
            group['archive_directory']=str(Path(archive_directory).resolve()) if archive_directory else str(Path(files[0]['path']).parent)
            export_directory=batch.get('export_directory','')
            if not isinstance(export_directory,str):raise ValueError('素材保存路径格式不正确')
            if export_directory:
                if not Path(export_directory).is_absolute():raise ValueError('素材保存位置必须为完整路径')
                folder_name=recipient_folder(name,recipient)
                group['export_directory']=str(Path(export_directory).resolve()/folder_name)
                group['archive_directory']=str(Path(group['archive_directory'])/folder_name)
            prepared.append((gid, group, files))
        from lightroom_import import store_batches
        return store_batches(self,prepared)

    def tasks(self, nonblocking=False):
        if not self.project.lock.acquire(blocking=not nonblocking):return self.task_cache
        try:
            data=self.project.snapshot()
        finally:self.project.lock.release()
        self.task_cache=[{'id': r['id'], 'name': r['name'], 'status': r['status'], 'count': len(r['items']),
                 'sent': len(r.get('sent_files', [])), 'recipient': r.get('recipient', {}),
                 'phase': r.get('delivery_phase', ''),
                 'inflight': r.get('inflight',''),
                 'image_folders': list(dict.fromkeys(str(Path(f['path']).parent) for f in r.get('files',[]) if f.get('path'))),
                 'folder': data['groups'].get(r['group'], {}).get('lightroom_folder', ''),
                 'error': r.get('error', '')} for r in data['submissions']]
        return self.task_cache

    def folders(self, requested=None):
        data=self.project.snapshot();result={}
        fallback={hashlib.sha256(path.encode('utf-8')).hexdigest()[:24]:path for path in (requested or [])}
        for record in data['submissions']:
            group=data['groups'].get(record['group'],{})
            folder=group.get('lightroom_folder') or fallback.get(record['group'])
            sources=group.get('lightroom_folders') or ([folder] if folder else [])
            for source in sources:
                result[source]={'status':record['status'],'count':len(record['items']),
                    'created':record['created'],'name':record['name']}
        return result

    def photo_updates(self, catalog):
        data=self.project.snapshot();ack=set(data.get('lightroom_update_ack', []));updates=[]
        for task in data['submissions']:
            if task['status']!='submitted':continue
            sent=set(task.get('sent_files', []))
            for file in task.get('files', []):
                ref=file.get('lightroom_photo')
                ident=task['id']+':'+file['id']
                if ref and ref.get('catalog')==catalog and file['id'] in sent and ident not in ack:
                    updates.append(dict(ref,id=ident))
        return updates

    def acknowledge_updates(self, value):
        catalog=value.get('catalog')
        valid={r['id'] for r in self.photo_updates(catalog)}
        ids=value.get('ids', [])
        if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids):raise ValueError('同步确认格式不正确')
        with self.project.lock:
            self.project.data['lightroom_update_ack']=list(set(self.project.data.get('lightroom_update_ack', [])) | (set(ids)&valid))
            self.project.save()
        return {'ok':True}

    def run_next(self):
        with self.lock:
            tasks = self.project.snapshot()['submissions']
            if self.sending or self.paused or self.importing:return False
            task = next((r for r in tasks if r['status'] == 'queued'), None)
            if not task:task=next((r for r in tasks if r['status']=='pending_review'),None)
            if not task:return False
            self.sending = True
            self.current_task=task['id']
            self.cancel_event.clear()
        try:
            with self.project.lock:
                record=next(r for r in self.project.data['submissions'] if r['id']==task['id'])
                if 'completion_message_body' not in record:
                    record['completion_message_body']=self.project.data.get('lightroom_message_body',DEFAULT_MESSAGE_BODY)
                    self.project.save()
            if self.prefetch_id==task['id'] and self.prefetch_future:
                from concurrent.futures import TimeoutError
                from delivery_cancel import cancellable,check_stop
                with cancellable(self.cancel_event):
                    while True:
                        check_stop()
                        try:self.prefetch_future.result(timeout=.1);break
                        except TimeoutError:pass
            if self.sender_factory:
                sender = self.sender_factory(task)
            elif task['recipient'].get('qq'):
                from qq_sender import QQSender
                sender = QQSender()
            else:
                from wechat_sender import WeChatSender
                sender = WeChatSender()
            if task['status']=='pending_review':
                from delivery_cancel import cancellable
                try:
                    with cancellable(self.cancel_event):
                        sender.connect(task['recipient'].get('wechat') or task['recipient'].get('qq'))
                        sender.verify_attachment(task['pending_confirmation'])
                    with self.project.lock:
                        # Verification is terminal: never re-enter the delivery path.
                        record.pop('pending_confirmation',None)
                        record.update(status='submitted',delivery_phase='verified',
                                      error='本轮附件已核对；客户端交接不代表对方接收完成')
                        sent=set(record.get('sent_files',[]))
                        delivered={f['id']:f for f in record['files'] if f['id'] in sent}
                        for item in self.project.data['items']:
                            file=delivered.get(item['id'])
                            if file and (item.get('size'),item.get('mtime'))==(file.get('size'),file.get('mtime')):
                                item['returned_to_coser']=True
                        self.project.save()
                except Exception as exc:
                    with self.project.lock:
                        record.update(status='needs_review',error='本轮发送完成，附件需人工核对：'+str(exc))
                        record['inflight_batch']=list(record.get('sent_files',[]))
                        record['inflight']=next(iter(record['inflight_batch']),'')
                        self.project.save()
                return True
            sender.defer_confirmation=True
            from delivery_cancel import cancellable
            with cancellable(self.cancel_event):self.project.deliver(task['id'], sender,on_prepared=self.prepare_next_archive)
            with self.lock:
                latest=next(r for r in self.project.snapshot()['submissions'] if r['id']==task['id'])
                if latest['status'] in ('error','needs_review'):self.paused=True
            return True
        except Exception as exc:
            with self.lock:self.paused=True
            with self.project.lock:
                from delivery_cancel import DeliveryStopped
                record = next(r for r in self.project.data['submissions'] if r['id'] == task['id'])
                record.update(status='needs_review' if record.get('inflight') else 'cancelled' if isinstance(exc,DeliveryStopped) else 'error', error=str(exc))
                try:self.project.save()
                except OSError as save_error:self.import_notice='任务保存失败：'+str(save_error)
            return True
        finally:
            with self.lock:self.sending = False;self.current_task=None

    def terminate(self, sid):
        if self.importing:raise ValueError('正在提交导出任务，请稍后处理')
        with self.lock, self.project.lock:
            if sid==self.prefetch_id:self.prefetch_cancel.set()
            if sid==self.current_task:
                self.paused=True
                self.cancel_event.set()
                return
            record=next((r for r in self.project.data['submissions'] if r['id']==sid),None)
            if not record or record['status'] in ('submitted','cancelled'):raise ValueError('此任务已结束')
            record.update(status='cancelled',error='用户已终止任务')
            self.project.save()

    def delete_tasks(self, ids):
        if self.importing:raise ValueError('正在提交导出任务，请稍后删除')
        with self.lock:
            if self.current_task in set(ids):raise ValueError('正在执行，请先终止并等待任务停止后删除')
            if self.prefetch_id in set(ids) and self.prefetch_future and not self.prefetch_future.done():
                raise ValueError('正在提前打包，请等待打包完成后删除')
            return self.project.delete_delivery_tasks(ids)

    def resume_many(self, values):
        """Queue the whole selection before the sender can pick its first task."""
        errors=[]
        resumed=[]
        with self.lock:
            for value in values:
                try:
                    self.resume(value)
                    resumed.append(value['id'])
                except ValueError as exc:
                    errors.append(value['id']+'：'+str(exc))
        return {'resumed':resumed,'errors':errors}

    def resume(self, value):
        if self.importing:raise ValueError('正在提交导出任务，请稍后继续')
        with self.lock, self.project.lock:
            if self.sending:raise ValueError('正在发送，请稍后处理')
            record = next((r for r in self.project.data['submissions'] if r['id'] == value.get('id')), None)
            allowed=('error', 'waiting', 'needs_review', 'queued') if value.get('resolution')=='cancel' else ('error', 'waiting', 'needs_review', 'cancelled', 'queued')
            if not record or record['status'] not in allowed:
                raise ValueError('此任务不需要继续')
            if value.get('resolution') == 'cancel':
                if record['id']==self.prefetch_id:self.prefetch_cancel.set()
                record.update(status='cancelled', error='用户已取消任务；客户端已提交的文件不会撤回')
                self.project.save()
                return {'ok': True}
            for file in record.get('files',[]):
                path=Path(file['path'])
                if not path.is_file() or fingerprint(path)!={'size':file['size'],'mtime':file['mtime']}:
                    raise ValueError('任务素材已删除或变化，请在 Lightroom 重新导出：'+path.name)
            if record['status'] == 'needs_review' or record.get('inflight'):
                action = value.get('resolution')
                if action not in ('skip', 'retry'):raise ValueError('请先核对客户端，选择跳过已发送批次或重发')
                if action == 'skip':
                    record['sent_files'] = list(dict.fromkeys(record.get('sent_files', []) + record.get('inflight_batch', [record.get('inflight')])) )
                if action=='retry':
                    record['sent_files']=[fid for fid in record.get('sent_files',[]) if fid not in record.get('inflight_batch',[])]
                record.pop('pending_confirmation',None)
                record.update(inflight='', inflight_batch=[])
            record.update(status='queued', error='')
            self.paused=False
            self.project.save()
            return {'ok': True}


def make_handler(bridge, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, value):
            payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            return secrets.compare_digest(self.headers.get('X-Return-Token', ''), token)

        def do_GET(self):
            if not self.authorized():return self.reply(403, {'error': '连接口令不正确'})
            bridge.plugin_seen=time.monotonic()
            if self.path == '/health':return self.reply(200, {'service': 'photo-return-lightroom', 'version': 1})
            if self.path == '/tasks':return self.reply(200, bridge.tasks())
            if self.path == '/folders':return self.reply(200, bridge.folders())
            self.reply(404, {'error': '接口不存在'})

        def do_POST(self):
            if not self.authorized():return self.reply(403, {'error': '连接口令不正确'})
            if self.path not in ('/submit', '/resume', '/folder-status', '/photo-updates', '/ack-updates', '/toolbar-command', '/toolbar-done','/export-progress'):return self.reply(404, {'error': '接口不存在'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4 * 1024 * 1024:raise ValueError('请求大小不正确')
                value = json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(value, dict):raise ValueError('请求必须为对象')
                # Record authenticated plugin activity before waiting for task locks.
                bridge.plugin_seen=time.monotonic()
                if self.path=='/toolbar-command':result=bridge.poll_toolbar(value)
                elif self.path=='/export-progress':result=bridge.report_export(value)
                elif self.path=='/toolbar-done':result=bridge.finish_toolbar(value)
                elif self.path=='/photo-updates':result=bridge.photo_updates(value.get('catalog'))
                elif self.path=='/ack-updates':result=bridge.acknowledge_updates(value)
                elif self.path=='/folder-status':
                    folders=value.get('folders',[])
                    if not isinstance(folders,list) or any(not isinstance(p,str) for p in folders):raise ValueError('文件夹列表不正确')
                    result=bridge.folders(folders)
                else:result = bridge.submit(value) if self.path == '/submit' else bridge.resume(value)
                self.reply(200, result)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.reply(400, {'error': str(exc)})
    return Handler


def main():
    folder = ROOT / '.lightroom-state'
    folder.mkdir(exist_ok=True)
    config = folder / 'connection.json'
    if not config.exists():config.write_text(json.dumps({'token': secrets.token_hex(24)}), encoding='utf-8')
    token = json.loads(config.read_text(encoding='utf-8'))['token']
    # Claim the port before loading state: a second process must not reset live tasks.
    server = ThreadingHTTPServer(('127.0.0.1', 17861), BaseHTTPRequestHandler)
    bridge = Bridge(folder / 'delivery')
    # Require a manual queue action on every backend launch, before any worker runs.
    bridge.paused = True
    server.RequestHandlerClass = make_handler(bridge, token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print('Lightroom 返图后台已启动；连接口令：' + token, flush=True)
    print('插件启动后自动连接本机后台；以上口令仅供手动连接备用。', flush=True)
    stop = threading.Event()
    def worker():
        while not stop.is_set():
            try:
                if not bridge.run_next():stop.wait(.5)
            except Exception as exc:
                print('后台异常：' + str(exc), flush=True)
                stop.wait(1)
    delivery_thread = threading.Thread(target=worker, name='lightroom-delivery', daemon=True)
    delivery_thread.start()
    try:
        from lightroom_task_window import TaskWindow
        TaskWindow(bridge, token, stop).mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        bridge.cancel_event.set()
        bridge.prefetch_cancel.set()
        delivery_thread.join()
        server.shutdown()
        server.server_close()
        bridge.project.close()


if __name__ == '__main__':
    if '--self-test' in sys.argv:
        try:
            import tempfile
            import tkinter
            import cv2, numpy, PIL, pywinauto
            import win32gui, win32process, win32clipboard, pythoncom
            from wechat_visual import WeChatVisualSender
            from lightroom_task_window import TaskWindow
            with tempfile.TemporaryDirectory() as folder:
                bridge=Bridge(folder)
                bridge.report_export({'done':1,'total':1,'state':'done'})
                assert bridge.export_progress['done']==1
                bridge.project.close()
            result={'ok':True,'root':str(ROOT),'ocr':Path(__file__).with_name('windows_ocr.ps1').is_file(),
                    'wechat_icon':(Path(__file__).parent/'assets'/'wechat-file-icon.png').is_file()}
            assert result['ocr'] and result['wechat_icon']
            Path(sys.argv[sys.argv.index('--self-test')+1]).write_text(json.dumps(result),encoding='utf-8')
        except Exception:
            import traceback
            Path(sys.argv[sys.argv.index('--self-test')+1]).write_text(json.dumps({'ok':False,'error':traceback.format_exc()}),encoding='utf-8')
            sys.exit(1)
    else:
        try:main()
        except Exception as exc:
            if not getattr(sys,'frozen',False):raise
            from tkinter import messagebox
            messagebox.showerror('返图后台无法启动',str(exc)+'\n请检查是否已启动另一个返图后台，以及解压位置是否可写。')
