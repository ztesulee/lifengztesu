"""Stage exports without holding the task/UI lock, then commit their directories."""
import hashlib
import json
import shutil
from pathlib import Path
from directory_transaction import DirectoryTransaction,choose_directory,identity,recover_transactions
from media import fingerprint
from project import person_recipient


def store_batches(bridge,prepared):
    if not bridge.import_lock.acquire(blocking=False):raise ValueError('正在整理上一批素材，请稍候')
    transaction=None
    try:
        with bridge.lock:
            if bridge.sending:raise ValueError('当前任务正在操作 QQ / 微信，请等待发送结束后再提交')
            if bridge.prefetch_future and not bridge.prefetch_future.done():raise ValueError('正在结束预打包，请稍后重试')
            bridge.importing=True
            bridge.import_notice=''
        recover_transactions(bridge.project.folder)
        data=bridge.project.snapshot()
        directories={};reserved={}
        for _,group,_ in prepared:
            if not group.get('export_directory'):continue
            owner=identity(group['name'],person_recipient(group))
            for field in ('export_directory','archive_directory'):
                original=Path(group[field])
                target=choose_directory(original.parent,original.name,owner,data['groups'],reserved)
                group[field]=str(target);directories[target]=owner
        for directory in directories:
            if any(other!=directory and other in directory.parents for other in directories):
                raise ValueError('人物素材目录与压缩包目录不能互相嵌套')
            for task in data['submissions']:
                if task['status'] not in ('queued','waiting','sending','error','needs_review','pending_review'):continue
                paths=[f['path'] for f in task.get('files',[]) if f.get('path')]
                if task.get('archive_path'):paths.append(task['archive_path'])
                if any(directory in Path(p).resolve().parents for p in paths):
                    raise ValueError('人物目录仍被未完成任务使用，请先取消旧任务：'+task['name'])
        if directories:
            transaction=DirectoryTransaction(bridge.project.folder,directories)
            transaction.stage()
        for _,group,files in prepared:
            if not group.get('export_directory'):continue
            destination=Path(group['export_directory']);stage=transaction.stage_for(destination)
            for file in files:
                source=Path(file['path']);target=stage/source.name;index=2
                while target.exists():
                    target=stage/(source.stem+'_'+str(index)+source.suffix);index+=1
                with target.open('xb') as output,source.open('rb') as input_file:
                    shutil.copyfileobj(input_file,output,1024*1024)
                if fingerprint(source)!={'size':file['size'],'mtime':file['mtime']}:
                    raise ValueError('整理期间素材发生变化：'+source.name)
                final=destination/target.name
                file.update(path=str(final),name=final.name,id=hashlib.sha256(str(final).encode()).hexdigest()[:24],**fingerprint(target))
                if file.get('lightroom_photo'):file['lightroom_photo']=dict(file['lightroom_photo'],export_path=str(final))
        # Only directory renames and a single metadata commit hold the project lock.
        with bridge.project.lock:
            old=bridge.project.snapshot()
            records=[]
            try:
                if transaction:transaction.install()
                for gid,group,files in prepared:
                    bridge.project.data['groups'][gid]=group
                    ids={f['id'] for f in files}
                    bridge.project.data['items']=[i for i in bridge.project.data['items'] if i['id'] not in ids]+files
                    records.extend(bridge.project.queue_direct_albums([gid],group['default_channel'],list(ids),persist=False))
                if transaction:bridge.project.data.setdefault('directory_commits',[]).append(transaction.ident)
                bridge.project.save()
            except Exception:
                # A failed write must not prevent restoring the original directories.
                if transaction and transaction.committed():
                    bridge.project.data=json.loads(bridge.project.file.read_text(encoding='utf-8'))
                else:
                    bridge.project.data=old
                    if transaction:transaction.rollback();transaction=None
                    raise
        if transaction:
            try:transaction.finish()
            except OSError:
                bridge.import_notice='任务已创建；旧目录备份暂未清理，重启后会自动处理'
        return {'tasks':[record['id'] for record in records]}
    except Exception:
        if transaction and not transaction.committed():transaction.rollback()
        raise
    finally:
        bridge.importing=False
        bridge.import_lock.release()
