"""Durable project state and idempotent, versioned original-file packages."""
import copy
import csv
import json
import os
import re
import secrets
import threading
import time
import shutil
import hashlib
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from media import fingerprint
from debug_log import logger

def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)

def safe_name(name):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:70]
    return value or '未命名'


def person_recipient(group, channel='default'):
    contacts = group.get('contacts', {})
    available = [key for key in ('qq', 'wechat') if contacts.get(key, '').strip()]
    if not available:
        raise ValueError(group.get('name', '人物') + '：未填写接收账号，请在“绑定 QQ / 微信”中填写 QQ 号或微信号。')
    if len(available) == 1:
        channel = available[0]
    elif channel == 'default':
        channel = group.get('default_channel') or ('qq' if contacts.get('qq') else 'wechat')
    account = contacts.get(channel, '').strip()
    if not account or (channel == 'qq' and not account.isdigit()):
        raise ValueError(group.get('name', '人物') + '：请先绑定有效' + (' QQ 号' if channel == 'qq' else '微信号'))
    return {channel: account}


def task_recipient(record, group, channel='default'):
    previous = record.get('recipient', {})
    started = (record.get('status') in ('submitted', 'sending', 'needs_review')
               or record.get('sent_files') or record.get('inflight') or record.get('inflight_batch')
               or record.get('completion_message_status'))
    if started and channel == 'default' and previous:
        return dict(previous)
    recipient = person_recipient(group, channel)
    if started and recipient != previous:
        raise ValueError(record.get('name', '任务') + '：已发送、部分发送或结果待核对的任务不能切换接收渠道，请新建任务。')
    return recipient


def person_name_error(groups, name, exclude=None):
    name = name.strip()
    if not name:
        return '人物文件夹名称不能为空。'
    if len(name) > 80:
        return '人物文件夹名称不能超过 80 个字符。'
    key = safe_name(name).casefold()
    for gid, group in groups.items():
        if gid != exclude and safe_name(group['name']).casefold() == key:
            return f'已存在人物文件夹“{group["name"]}”，请修改为不同的名称。'
    return ''

class Project:
    def __init__(self, folder, title=None):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.cache = self.folder / 'previews'
        self.cache.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pack')
        self.file = self.folder / 'project.json'
        if self.file.exists():
            self.data = json.loads(self.file.read_text(encoding='utf-8'))
            from media_ids import repair_ids
            if repair_ids(self.data,self.folder,self.cache):
                self.save()
        else:
            self.data = {'version': 1, 'title': title or self.folder.name, 'groups': {}, 'items': [], 'submissions': [], 'public_url': '', 'skipped': []}
            self.save()
        for submission in self.data['submissions']:
            if submission.get('delivery_version') == 1 and submission['status'] in ('sending', 'queued'):
                submission['status'] = 'needs_review' if submission.get('inflight') else 'waiting'
                submission['error'] = '软件重启，请检查上次发送状态后继续'
            elif submission.get('delivery_version') != 1:
                submission['legacy_package'] = True
        self.save()

    def save(self):
        with self.lock:
            atomic_json(self.file, self.data)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def group_folder(self, group_id):
        # Stable ID avoids collisions, reserved Windows names, and renamed-folder data loss.
        path = self.folder / '人物' / ('人物_' + group_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def manifests(self):
        with self.lock:
            for gid, group in self.data['groups'].items():
                folder = self.group_folder(gid)
                with (folder / '素材清单.csv').open('w', encoding='utf-8-sig', newline='') as handle:
                    writer = csv.writer(handle)
                    writer.writerow(['人物', '文件名', '原片路径', '确认状态'])
                    for item in self.data['items']:
                        if gid in item['groups']:
                            writer.writerow([group['name'], item['name'], item['path'], '已确认' if item['reviewed'] else '待确认'])

    def unpublish(self):
        for group in self.data['groups'].values():
            group['published'] = False

    def move_items(self, ids, target, move_originals=False, progress=lambda _:None):
        with self.lock:
            if target not in self.data['groups']:
                raise ValueError('目标人物文件夹不存在')
            count = 0
            if move_originals and any(s['status'] in ('queued', 'packing', 'sending') and set(s['items']).intersection(ids) for s in self.data['submissions']):
                raise ValueError('这些素材正在打包，请打包完成后再移动原片')
            for item in self.data['items']:
                if item['id'] in ids and not item['error'] and item['groups'] != [target]:
                    if move_originals:
                        progress('移动原片：' + item['name'])
                        self.move_original(item, target)
                    item['groups'] = [target]
                    item['reviewed'] = True
                    item['note'] = '原片已移动到人物文件夹' if move_originals else '已手动移动到人物文件夹'
                    count += 1
            if count:
                self.unpublish()
                self.save()
                self.manifests()
            return count

    def move_original(self, item, target):
        source = Path(item['path'])
        expected = {'size': item['size'], 'mtime': item['mtime']}
        if fingerprint(source) != expected:
            raise ValueError('原片已改变，请重新导入：' + item['name'])
        folder = self.group_folder(target) / '原片'
        folder.mkdir(exist_ok=True)
        if source.parent.resolve() == folder.resolve():
            return
        destination = folder / item['name']
        if destination.exists():
            destination = folder / (item['id'] + '_' + item['name'])
        if destination.exists():
            destination = folder / (secrets.token_hex(6) + '_' + item['name'])
        old = copy.deepcopy(item)
        created = False
        # Keep the source until a verified destination and its new index are durable.
        try:
            digest = hashlib.sha256()
            with source.open('rb') as reader, destination.open('xb') as writer:
                created = True
                for chunk in iter(lambda: reader.read(1024 * 1024), b''):
                    writer.write(chunk)
                    digest.update(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            shutil.copystat(source, destination)
            check = hashlib.sha256()
            with destination.open('rb') as reader:
                for chunk in iter(lambda: reader.read(1024 * 1024), b''):
                    check.update(chunk)
            if fingerprint(source) != expected or digest.digest() != check.digest():
                raise ValueError('移动校验失败，原片保留：' + item['name'])
            item.update(path=str(destination), **fingerprint(destination), managed_original=True,
                        groups=[target], reviewed=True, note='原片已移动到人物文件夹')
            self.unpublish()
            self.save()
        except Exception:
            item.clear()
            item.update(old)
            # Only remove a destination created by this operation.
            if created and destination.exists() and item['path'] != str(destination):
                destination.unlink()
            raise
        try:
            if fingerprint(source) != expected:
                raise ValueError('原位置文件发生变化，保留该文件')
            source.unlink()
        except Exception as exc:
            item['note'] = '已存入人物文件夹；原位置文件未移除：' + str(exc)
            self.save()
            raise ValueError(item['name'] + '：' + item['note'])
        for submission in self.data['submissions']:
            for recorded in submission.get('files', []):
                if recorded['id'] == item['id'] and recorded['path'] == old['path']:
                    recorded.update(path=item['path'], size=item['size'], mtime=item['mtime'])
        self.save()

    def delete_group(self, gid):
        with self.lock:
            if gid not in self.data['groups']:
                raise ValueError('人物文件夹不存在')
            backups = self.folder / 'backups'
            backups.mkdir(exist_ok=True)
            atomic_json(backups / ('before_delete_person_' + time.strftime('%Y%m%d_%H%M%S') + '_' + secrets.token_hex(3) + '.json'), self.data)
            name = self.data['groups'][gid]['name']
            for item in self.data['items']:
                if gid in item['groups']:
                    item['groups'] = [g for g in item['groups'] if g != gid]
                    item['reviewed'] = bool(item['groups'])
                    item['note'] = '已删除人物归属；可重新分配'
            del self.data['groups'][gid]
            self.unpublish()
            self.save()
            self.manifests()
            return name

    def authenticate(self, token):
        for gid, group in self.data['groups'].items():
            if group['published'] and secrets.compare_digest(group['token'], token):
                return gid
        raise ValueError('相册不存在或已停止发布')

    def submit(self, token, ids, request_id):
        with self.lock:
            gid = self.authenticate(token)
            if not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', request_id):
                raise ValueError('无效的提交编号')
            if not isinstance(ids, list) or not ids or len(ids) > 10000 or any(not isinstance(i, str) for i in ids):
                raise ValueError('请选择至少一项素材')
            selected = set(ids)
            for old in self.data['submissions']:
                if old['group'] == gid and old['request_id'] == request_id:
                    if set(old['items']) != selected:
                        raise ValueError('提交编号已被使用，请刷新重试')
                    return copy.deepcopy(old)
            if sum(s['status'] in ('queued', 'sending') for s in self.data['submissions']) >= 100:
                raise ValueError('正在处理其他选片，请稍后提交')
            allowed = {i['id']: i for i in self.data['items'] if gid in i['groups'] and i['reviewed'] and not i['error']}
            if not selected <= allowed.keys():
                raise ValueError('素材已更新或不属于此相册，请刷新')
            files = [copy.deepcopy(allowed[i]) for i in sorted(selected)]
            sid = time.strftime('%Y%m%d_%H%M%S') + '_' + secrets.token_hex(3)
            submission = {'id': sid, 'group': gid, 'name': self.data['groups'][gid]['name'],
                          'request_id': request_id, 'items': sorted(selected), 'files': files,
                          'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'status': 'waiting', 'error': '', 'delivery_version': 1,
                          'sent_files': [], 'inflight': '',
                          'recipient': person_recipient(self.data['groups'][gid]) if self.data['groups'][gid].get('contacts') else {}}
            self.data['submissions'].append(submission)
            self.save()  # acknowledge only after the request is on disk
            return copy.deepcopy(submission)

    def queue_direct_albums(self, gids, channel='default', item_ids=None):
        """Create durable whole-album tasks without requiring web publication."""
        with self.lock:
            if item_ids is not None:
                item_ids=set(item_ids)
                selected=[i for i in self.data['items'] if i['id'] in item_ids]
                if not item_ids or len(selected)!=len(item_ids):
                    raise ValueError('请选择有效素材，压缩包预览不能作为原片发送')
                if any(not set(i.get('groups',[])) & set(gids) for i in selected):
                    raise ValueError('选中素材不属于发送人物，请重新选择')
            prepared = []
            for gid in dict.fromkeys(gids):
                group = self.data['groups'].get(gid)
                if not group:
                    raise ValueError('人物文件夹不存在')
                recipient = person_recipient(group, channel)
                if not recipient.get('qq', '').isdigit() and not recipient.get('wechat', '').strip():
                    raise ValueError(group['name'] + '：请先绑定有效 QQ 号或微信号')
                files = [copy.deepcopy(i) for i in self.data['items'] if gid in i['groups'] and (item_ids is None or i['id'] in item_ids)]
                if not files:
                    raise ValueError(group['name'] + '：人物文件夹没有素材')
                for item in files:
                    if item.get('error') or not item.get('reviewed'):
                        raise ValueError(group['name'] + '：存在未准备好的素材，请先处理')
                    if fingerprint(Path(item['path'])) != {'size':item['size'], 'mtime':item['mtime']}:
                        raise ValueError(group['name'] + '：原片缺失或已变化，请重新导入')
                routing = {'qq':recipient['qq']} if recipient.get('qq') else {'wechat':recipient['wechat']}
                signature = hashlib.sha256(json.dumps({**routing,
                    'files':sorted((i['id'],i['size'],i['mtime']) for i in files)},sort_keys=True).encode()).hexdigest()
                existing = next((s for s in self.data['submissions'] if s.get('source') == 'direct'
                                 and s['group'] == gid and s.get('direct_signature') == signature), None)
                if existing:
                    prepared.append((existing, False))
                    continue
                record = {'id':time.strftime('%Y%m%d_%H%M%S')+'_'+secrets.token_hex(3),
                    'group':gid,'name':group['name'],'request_id':'direct_'+secrets.token_hex(12),
                    'items':[i['id'] for i in files], 'files':files,
                    'created':time.strftime('%Y-%m-%d %H:%M:%S'), 'status':'queued', 'error':'',
                    'delivery_version':1,'sent_files':[],'inflight':'','recipient':recipient,
                    'source':'direct','direct_signature':signature,
                    'material_scope':'partial' if item_ids is not None else 'all'}
                prepared.append((record, True))
            for record, fresh in prepared:
                if fresh:
                    self.data['submissions'].append(record)
            self.save()
            return [copy.deepcopy(record) for record, _ in prepared]

    def delete_delivery_tasks(self, ids):
        with self.lock:
            selected = [s for s in self.data['submissions'] if s['id'] in set(ids)]
            if any(s['status'] in ('sending', 'packing') for s in selected):
                raise ValueError('正在执行的任务不能删除，请等待任务结束')
            if not selected:
                return 0
            backups = self.folder / 'backups'
            backups.mkdir(exist_ok=True)
            atomic_json(backups / ('before_delete_tasks_' + time.strftime('%Y%m%d_%H%M%S') + '_' + secrets.token_hex(3) + '.json'), self.data)
            removed = {s['id'] for s in selected}
            self.data['submissions'] = [s for s in self.data['submissions'] if s['id'] not in removed]
            self.save()
            return len(selected)

    def prepare_archive(self, sid, channel, files=None):
        from delivery import selected_files
        with self.lock:
            record = next(s for s in self.data['submissions'] if s['id'] == sid)
            account = record.get('recipient', {}).get(channel, '')
            if not account:
                raise ValueError('此任务未绑定接收账号')
            subset = copy.deepcopy(record)
            if files is not None:
                subset['files'] = copy.deepcopy(files)
            day = record.setdefault('archive_date', time.strftime('%Y年%m月%d日'))
            self.save()
        paths = selected_files(self, subset)
        if not paths:
            raise ValueError('没有需要打包的原片')
        signature = hashlib.sha256(json.dumps({'channel':channel, 'account':account,
                    'files':[(f['id'], f['size'], f['mtime']) for f in subset['files']]}, sort_keys=True).encode()).hexdigest()[:16]
        folder = self.group_folder(record['group']) if channel == 'wechat' else self.folder / '返图压缩包' / safe_name(sid) / signature
        folder.mkdir(parents=True, exist_ok=True)
        if channel == 'wechat':
            with self.lock:
                names = record.setdefault('archive_names', {})
                archive_name = names.get(signature)
                if not archive_name:
                    person = self.data['groups'][record['group']]['name']
                    stem = safe_name(person) + time.strftime('%Y-%m-%d_%H-%M-%S')
                    archive_name = stem + '.zip'
                    index = 2
                    while (folder / archive_name).exists():
                        archive_name = stem + '_' + str(index) + '.zip'
                        index += 1
                    names[signature] = archive_name
                    self.save()
            archive = folder / archive_name
        else:
            archive = folder / (safe_name('QQ号_' + account + '_' + day + '_返图') + '.zip')
        temp = archive.with_suffix('.zip.tmp')
        try:
            names = set()
            with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as package:
                for path, original in zip(paths, subset['files']):
                    name, index = path.name, 2
                    while name.casefold() in names:
                        name = f'{path.stem}_{index}{path.suffix}'
                        index += 1
                    names.add(name.casefold())
                    package.write(path, name)
                    if fingerprint(path) != {'size':original['size'], 'mtime':original['mtime']}:
                        raise ValueError('打包期间原片发生变化：' + path.name)
            os.replace(temp, archive)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        with self.lock:
            record.update(archive_path=str(archive), archive_items=[f['id'] for f in subset['files']], archive_channel=channel)
            self.save()
        logger.info('ARCHIVE prepared task=%s channel=%s originals=%s bytes=%s', sid, channel, len(paths), archive.stat().st_size)
        return archive

    def deliver(self, sid, sender):
        with self.lock:
            record = next(s for s in self.data['submissions'] if s['id'] == sid)
            if record['status'] not in ('queued', 'waiting', 'error') or record.get('inflight'):
                return
            channel = 'qq' if record.get('recipient', {}).get('qq') else 'wechat'
            account = record.get('recipient', {}).get(channel)
            if not account:
                record.update(status='error', error='此任务没有绑定 QQ 号或微信号')
                self.save()
                return
            record.update(status='sending', error='', delivery_phase='packing' if channel == 'wechat' else 'preparing_originals')
            self.save()
        try:
            logger.info('TASK start id=%s channel=%s account=%s files=%s already_sent=%s', sid, channel, account, len(record['files']), len(record['sent_files']))
            remaining = [f for f in record['files'] if f['id'] not in record['sent_files']]
            if remaining:
                if channel == 'wechat':
                    payload = [self.prepare_archive(sid, channel, remaining)]
                else:
                    if not account.isdigit():
                        raise ValueError('QQ 号必须为纯数字')
                    from delivery import selected_files
                    subset = copy.deepcopy(record)
                    subset['files'] = remaining
                    payload = selected_files(self, subset)
            with self.lock:
                record['delivery_phase'] = 'connecting'
                self.save()
            sender.connect(account)
            if remaining:
                ids = [f['id'] for f in remaining]
                with self.lock:
                    record.update(inflight=ids[0], inflight_batch=ids,
                                  delivery_method='wechat_zip' if channel == 'wechat' else 'qq_flash',
                                  delivery_phase='sending_archive' if channel == 'wechat' else 'sending_originals')
                    self.save()
                if callable(getattr(sender, 'send_files', None)):
                    sender.send_files(payload)
                else:
                    for path in payload:
                        sender.send_file(path)
                with self.lock:
                    record['sent_files'].extend(ids)
                    record.update(inflight='', inflight_batch=[])
                    self.save()
            note_error = ''
            if callable(getattr(sender, 'send_message', None)) and not record.get('completion_message_status'):
                with self.lock:
                    person = self.data['groups'].get(record['group'], {}).get('name') or record.get('name', '').split(' · ')[0]
                    text = ('QQ/微信号：' + account + '\n' + person + '老师您好，您的返图已全部发送完毕。感谢您的出镜与配合，辛苦您收到后回复确认，祝您生活愉快！\n日期：' + time.strftime('%Y年%m月%d日 %H点%M分') + '\n（本消息为系统自动发送）')
                    record.update(completion_message=text, completion_message_status='started')
                    self.save()
                try:
                    sender.send_message(text)
                    record['completion_message_status'] = 'sent'
                except Exception as exc:
                    logger.exception('TASK completion message unconfirmed id=%s', sid)
                    record['completion_message_status'] = 'uncertain'
                    note_error = '；照片已发送，留言未确认：' + str(exc)
            elif record.get('completion_message_status') in ('started', 'uncertain'):
                note_error = '；留言结果未确认，未重复留言'
            with self.lock:
                record.update(status='submitted', error='已交给'+(' QQ ' if channel == 'qq' else '微信')+'发送，请在客户端确认传输与收件状态' + note_error)
                sent=set(record.get('sent_files',[]))
                delivered={file['id']:file for file in record['files'] if file['id'] in sent}
                for item in self.data['items']:
                    file=delivered.get(item['id'])
                    if file and (item.get('size'),item.get('mtime'))==(file.get('size'),file.get('mtime')):
                        item['returned_to_coser']=True
                self.save()
            logger.info('TASK complete id=%s sent=%s', sid, len(record['sent_files']))
        except Exception as exc:
            logger.exception('TASK failed id=%s inflight=%s: %s', sid, bool(record.get('inflight')), exc)
            with self.lock:
                record.update(status='needs_review' if record.get('inflight') else 'error', error=str(exc))
                self.save()

    def close(self):
        self.workers.shutdown(wait=True)
