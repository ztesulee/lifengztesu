"""Reviewed folder synchronization; source files are never deleted."""
import copy
import shutil
import tempfile
import time
from pathlib import Path
from media import collect, fingerprint, import_media


def scan_folder(items, folder, progress=lambda _:None, ignored=()):
    root=Path(folder).resolve(strict=True)
    paths,_=collect(root,progress=progress)
    key=lambda path:str(Path(path).resolve()).casefold()
    known={key(item['path']):item for item in items}
    excluded={key(path) for path in ignored}
    found={key(path):path for path in paths if key(path) not in excluded}
    rows=[]
    for ident,path in found.items():
        item=known.get(ident)
        if item is None:
            rows.append(dict(action='新增',id='',path=str(path),name=path.name))
        elif item.get('source_missing') or fingerprint(path)!={'size':item['size'],'mtime':item['mtime']}:
            rows.append(dict(action='内容变化',id=item['id'],path=str(path),name=item['name']))
    for ident,item in known.items():
        path=Path(item['path']).resolve()
        try:path.relative_to(root)
        except ValueError:continue
        if ident not in found and not path.is_file() and not item.get('source_missing'):
            rows.append(dict(action='缺失',id=item['id'],path=str(path),name=item['name']))
    return rows


def apply_sync(project, rows, gid, progress=lambda _:None):
    with project.lock:snapshot=copy.deepcopy(project.data)
    if gid != '?' and gid not in snapshot['groups']:raise ValueError('请先选择新增素材所属人物')
    affected={row['id'] for row in rows if row['id']}
    removed={row['id'] for row in rows if row['action']=='缺失'}
    for task in snapshot['submissions']:
        if affected & {file['id'] for file in task.get('files',[])} and (task.get('inflight') or task.get('inflight_batch') or task.get('status') in ('sending','needs_review')):
            raise ValueError('相关任务正在发送或待核对，请先处理任务：'+task.get('name',''))
    prepared={}
    with tempfile.TemporaryDirectory(prefix='sync-',dir=str(project.folder)) as directory:
        cache=Path(directory)
        for index,row in enumerate(rows):
            if row['action']=='缺失':
                if Path(row['path']).exists():raise ValueError('缺失文件已恢复，请重新扫描：'+row['name'])
                continue
            progress({'text':'生成预览：'+row['name'],'current':index,'total':len(rows)})
            target=row.get('group',gid)
            if target not in snapshot['groups']:target='?'
            new=import_media(row['path'],cache,target)
            if target=='?':new.update(groups=[],reviewed=False,note='未分配到人物文件夹')
            prepared[index]=new
        with project.lock:
            if snapshot!=project.data:raise ValueError('项目已变化，请重新扫描')
            backup=project.folder/'backups';backup.mkdir(exist_ok=True)
            shutil.copy2(project.file,backup/('sync_'+str(time.time_ns())+'.json'))
            changed={}
            for index,new in prepared.items():
                row=rows[index]
                old=next((item for item in project.data['items'] if item['id']==row['id']),None)
                if old:
                    ident=old['id']
                else:
                    from media_ids import available_id
                    ident=available_id(new['id'],new['path'],{item['id'] for item in project.data['items']})
                for ext in ('.jpg','.webp','.preview-v2'):
                    shutil.copy2(cache/(new['id']+ext),project.cache/(ident+ext))
                (project.cache/(ident+'.mp4')).unlink(missing_ok=True)
                if old:
                    if old['size']!=new['size'] or old['mtime']!=new['mtime']:old['returned_to_coser']=False
                    old.pop('source_missing',None)
                    for field in ('path','name','kind','size','mtime','error'):old[field]=new[field]
                    old['note']='源文件已同步更新；人物归属保留'
                    changed[ident]=old
                else:
                    new['id']=ident
                    project.data['items'].append(new)
            for item in project.data['items']:
                if item['id'] in removed:
                    item.update(source_missing=True,error='该文件已删除',note='源文件缺失，预览保留；可重新定位原片')
            for task in project.data['submissions']:
                if task.get('status')=='submitted':continue
                dirty=False
                for file in task.get('files',[]):
                    if file['id'] in changed and file['id'] not in task.get('sent_files',[]):
                        for field in ('path','name','kind','size','mtime','error'):file[field]=changed[file['id']][field]
                        dirty=True
                if dirty:
                    for field in ('archive_path','archive_items','archive_names','direct_signature'):task.pop(field,None)
                    task.update(status='waiting',error='素材已更新，请核对后继续发送')
                if removed & {file['id'] for file in task.get('files',[])}:
                    task.update(status='error',error='任务包含已删除的源文件，请恢复或重新定位原片后继续')
            project.unpublish();project.data['delivery_paused']=True
            project.save();project.manifests()
    return len(rows)
