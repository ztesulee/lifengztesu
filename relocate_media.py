"""Reviewable source relinking without moving originals or changing person IDs."""
import copy
import shutil
import tempfile
import time
import os
from pathlib import Path
from media import fingerprint, import_media, linked


def find_relocations(items, folder, progress=lambda _:None, cancel=None):
    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise InterruptedError('已取消重新定位扫描，项目未修改')
    # Check the existing sources first; a healthy project needs no directory scan.
    pending=[]
    for index,item in enumerate(items):
        check_cancel()
        if index % 50 == 0:progress(f'检查原片位置：{index}/{len(items)}')
        try:
            if fingerprint(Path(item['path'])) == {'size':item['size'],'mtime':item['mtime']}:continue
        except OSError:pass
        pending.append(item)
    if not pending:return []
    root=Path(folder).resolve(strict=True)
    if not root.is_dir():raise ValueError('请选择原片目录')
    names={Path(item['path']).name.casefold() for item in pending}
    by_name={}
    visited=0
    last_notice=0
    def walk_error(error):raise error
    for parent,dirs,files in os.walk(root,followlinks=False,onerror=walk_error):
        check_cancel()
        dirs[:]=[name for name in dirs if not linked(Path(parent)/name)]
        visited+=len(files)
        if time.monotonic()-last_notice > .2:
            progress(f'扫描新目录：已检查 {visited} 个文件 · {parent}')
            last_notice=time.monotonic()
        for name in files:
            check_cancel()
            if name.casefold() not in names:continue
            path=Path(parent)/name
            if linked(path):continue
            try:info=fingerprint(path)
            except OSError:continue
            by_name.setdefault(name.casefold(),[]).append((path,info))
    result = []
    for index,item in enumerate(pending):
        check_cancel()
        if index % 50 == 0:progress(f'匹配新路径：{index}/{len(pending)}')
        old = Path(item['path'])
        candidates = by_name.get(old.name.casefold(), [])
        exact = [pair for pair in candidates if pair[1] == {'size':item['size'],'mtime':item['mtime']}]
        # Same-name files may belong to different people: never choose among them.
        choices = exact if len(exact) == 1 else candidates
        path = choices[0][0] if len(choices) == 1 else None
        state = ('路径匹配（大小和时间一致）' if path and exact else
                 '内容可能变化，请核对' if path else '同名候选不唯一，请指定文件' if choices else '未找到，请指定文件')
        result.append(dict(id=item['id'],name=item['name'],old=str(old),path=str(path) if path else '',state=state))
    return result


def apply_relocations(project, rows, progress=lambda _:None):
    selected = {row['id']:row for row in rows}
    if not selected:raise ValueError('请选择已指定新路径的素材')
    with project.lock:
        snapshot = copy.deepcopy(project.data)
    for task in snapshot['submissions']:
        affected = selected.keys() & {file['id'] for file in task.get('files',[])}
        if affected and (task.get('inflight') or task.get('inflight_batch') or task.get('status') in ('sending','needs_review')):
            raise ValueError('相关任务正在发送或结果待核对，请先处理发送记录：'+task.get('name',''))
    prepared = {}
    with tempfile.TemporaryDirectory(prefix='relocate-',dir=str(project.folder)) as directory:
        cache = Path(directory)
        for item in snapshot['items']:
            if item['id'] not in selected:continue
            progress({'text':'重新生成预览：'+item['name'],'current':len(prepared),'total':len(selected)})
            new = import_media(selected[item['id']]['path'],cache,'')
            prepared[item['id']] = new
        with project.lock:
            if project.data != snapshot:raise ValueError('项目发生变化，请重新检测后确认')
            backup = project.folder/'backups'
            backup.mkdir(exist_ok=True)
            shutil.copy2(project.file,backup/('relocate_'+time.strftime('%Y%m%d_%H%M%S')+'_'+str(time.time_ns())+'.json'))
            for item in project.data['items']:
                new = prepared.get(item['id'])
                if not new:continue
                for ext in ('.jpg','.webp','.preview-v2'):
                    shutil.copy2(cache/(new['id']+ext),project.cache/(item['id']+ext))
                if new['size']!=item['size'] or new['mtime']!=item['mtime']:
                    item['returned_to_coser']=False
                for key in ('path','name','kind','size','mtime','error'):item[key]=new[key]
                item.pop('managed_original',None)
                item.pop('source_missing',None)
                item['note']='原片已重新定位；人物归属保留'
            for task in project.data['submissions']:
                if task.get('status')=='submitted':continue
                changed=False
                for file in task.get('files',[]):
                    if file['id'] in prepared and file['id'] not in task.get('sent_files',[]):
                        new=prepared[file['id']]
                        for key in ('path','name','kind','size','mtime','error'):file[key]=new[key]
                        changed=True
                if changed:
                    for key in ('archive_path','archive_items','archive_names','direct_signature'):task.pop(key,None)
                    task.update(status='waiting',error='原片已重新定位，请核对后继续发送')
            project.unpublish()
            project.data['delivery_paused']=True
            project.save()
            project.manifests()
    return len(prepared)
