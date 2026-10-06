"""Review cross-path duplicates; cleanup never deletes original files."""
import hashlib
import shutil
import time
from pathlib import Path
from collections import defaultdict


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def picture_digest(path):
    from PIL import Image,ImageOps
    try:
        with Image.open(path) as image:
            image=ImageOps.exif_transpose(image).convert('RGB')
            return hashlib.sha256(str(image.size).encode()+image.tobytes()).hexdigest()
    except (OSError,ValueError):return None


def find_similar(items, groups=None, progress=lambda _:None):
    from PIL import Image,ImageOps
    import numpy as np
    photos=[item for item in items if item.get('kind')=='photo' and (groups is None or set(item.get('groups',[])) & set(groups))]
    signatures={};rows=[]
    for index,item in enumerate(photos):
        progress({'text':'核对相似画面：'+item['name'],'current':index,'total':len(photos)})
        value=pixels=None
        try:
            with Image.open(item['path']) as image:
                image=ImageOps.exif_transpose(image).convert('L')
                tiny=np.asarray(image.resize((9,8)),dtype='int16')
                bits=tiny[:,1:]>tiny[:,:-1]
                value=int(''.join('1' if bit else '0' for bit in bits.flat),2)
                pixels=np.asarray(image.resize((32,32)),dtype='float32')/255
        except (OSError,ValueError):pass
        for other,old_hash,old_pixels in signatures.values():
            if groups is not None and not set(item.get('groups',[])) & set(other.get('groups',[])):continue
            same_name=(Path(item['path']).name.casefold()==Path(other['path']).name.casefold() or
                       bool(item.get('name') and other.get('name')) and
                       item['name'].casefold()==other['name'].casefold())
            readable=value is not None and old_hash is not None
            similar=False
            if readable:
                distance=bin(value^old_hash).count('1')
                difference=float(np.abs(pixels-old_pixels).mean())
                similar=distance<=5 and difference<=.06
            if same_name or similar:
                if same_name:
                    state=('同名且画面相似，请人工确认' if similar else
                           '同名，源文件无法读取，请核对' if not readable else
                           '同名但画面有差异，请人工核对')
                else:state='画面相似，请人工确认'
                rows.append(dict(remove=item['id'],keep=other['id'],name=item['name'],state=state,old=item['path'],target=other['path'],groups=other.get('groups',[])))
        signatures[item['id']]=(item,value,pixels)
    return rows


def refresh_duplicate_previews(project, groups=None, progress=lambda _:None):
    from media import import_media,fingerprint
    data=project.snapshot()
    buckets=defaultdict(list)
    for item in data['items']:
        if groups is None or set(item.get('groups',[])) & set(groups):
            buckets[(item.get('name') or Path(item['path']).name).casefold()].append(item)
    items=[item for bucket in buckets.values() if len(bucket)>1 for item in bucket]
    for index,item in enumerate(items):
        progress({'text':'校正同名素材预览：'+item['name'],'current':index,'total':len(items)})
        try:
            if fingerprint(Path(item['path']))!={'size':item['size'],'mtime':item['mtime']}:continue
            import_media(item['path'],project.cache,'',ident=item['id'])
        except (OSError,ValueError):continue


def merge_reviewed(project, rows, delete_originals=False):
    import copy
    from media import fingerprint
    with project.lock:
        data=copy.deepcopy(project.data)
        remap={row['remove']:row['keep'] for row in rows}
        for ident in remap:
            seen={ident};target=remap[ident]
            while target in remap:
                if target in seen:raise ValueError('选择形成循环，请减少重复候选')
                seen.add(target);target=remap[target]
            remap[ident]=target
        involved=set(remap)|set(remap.values())
        for task in data['submissions']:
            if involved & {file['id'] for file in task.get('files',[])} and (task.get('inflight') or task.get('inflight_batch') or task.get('status') in ('sending','needs_review')):
                raise ValueError('关联任务待核对或正在发送，请先处理')
        by_id={item['id']:item for item in data['items']}
        deleting={}
        if delete_originals:
            for old,new in remap.items():
                if old not in by_id or new not in by_id:raise ValueError('素材记录变化，请重新检测')
                source=Path(by_id[old]['path']).resolve()
                keep_path=Path(by_id[new]['path']).resolve()
                if source==keep_path:continue
                if not keep_path.is_file():raise ValueError('保留的源文件不存在，不能合并删除')
                if fingerprint(source)!={'size':by_id[old]['size'],'mtime':by_id[old]['mtime']}:
                    raise ValueError('待删除原片已变化，请重新核对：'+str(source))
                if any(Path(item['path']).resolve()==source and item['id'] not in remap for item in data['items']):
                    raise ValueError('源文件仍被其他素材记录引用，不能删除：'+str(source))
                if any(Path(file['path']).resolve()==source and file['id'] not in remap for task in data['submissions'] for file in task.get('files',[])):
                    raise ValueError('发送任务的文件引用与素材记录不一致，请重新核对任务')
                deleting[str(source).casefold()]=source
            import json
            from recent_projects import read_recent
            root=Path(__file__).parent
            candidates=set((root/'projects').rglob('project.json'))
            candidates.update(Path(folder)/'project.json' for folder in read_recent(root/'.last-project.json'))
            for file in candidates:
                if file.resolve()==project.file.resolve() or not file.is_file():continue
                try:other=json.loads(file.read_text(encoding='utf-8'))
                except (OSError,ValueError):raise ValueError('历史项目无法核对引用，请先检查：'+str(file))
                for item in other.get('items',[]):
                    if str(Path(item['path']).resolve()).casefold() in deleting:
                        raise ValueError('源文件被其他历史项目引用，不能删除：'+str(file))
        ignored=data.setdefault('ignored_source_paths',[])
        for old,new in remap.items():
            if old not in by_id or new not in by_id:raise ValueError('素材记录变化，请重新检测')
            item,keep=by_id[old],by_id[new]
            keep['groups']=list(dict.fromkeys(keep.get('groups',[])+item.get('groups',[])))
            keep['reviewed']=bool(keep.get('reviewed') or item.get('reviewed'))
            keep['returned_to_coser']=bool(keep.get('returned_to_coser') or item.get('returned_to_coser'))
            path=str(Path(item['path']).resolve())
            if path not in ignored:ignored.append(path)
        data['items']=[item for item in data['items'] if item['id'] not in remap]
        for task in data['submissions']:
            for field in ('items','sent_files','archive_items'):
                if field in task:task[field]=list(dict.fromkeys(remap.get(ident,ident) for ident in task[field]))
            seen=set();files=[]
            for file in task.get('files',[]):
                ident=remap.get(file['id'],file['id'])
                if ident in seen:continue
                if file['id'] in remap:
                    for field in ('path','name','kind','size','mtime','error'):
                        if field in by_id[ident]:file[field]=by_id[ident][field]
                file['id']=ident;seen.add(ident);files.append(file)
            task['files']=files
        data.setdefault('merged_media_ids',{}).update(remap)
        for group in data['groups'].values():group['published']=False
        backup=project.folder/'backups';backup.mkdir(exist_ok=True)
        shutil.copy2(project.file,backup/('similar_merge_'+str(time.time_ns())+'.json'))
        project.data=data;project.save();project.manifests()
        if delete_originals:
            errors=[]
            for source in deleting.values():
                try:source.unlink()
                except OSError as exc:errors.append(str(source)+'：'+str(exc))
            for ident in remap:
                for suffix in ('.jpg','.webp','.mp4','.preview-v2'):
                    try:(project.cache/(ident+suffix)).unlink(missing_ok=True)
                    except OSError as exc:errors.append('预览清理失败：'+str(exc))
            if errors:raise ValueError('工程记录已合并，但以下文件未能删除：\n'+'\n'.join(errors))
    return len(remap)


def deduplicate_project(project, progress=lambda _:None, groups=None):
    import copy
    from media import fingerprint
    with project.lock:snapshot=copy.deepcopy(project.data)
    original=copy.deepcopy(snapshot)
    buckets=defaultdict(list)
    for item in snapshot['items']:
        if groups is None:
            buckets[('name',Path(item['path']).name.casefold())].append(item)
            if item.get('kind')=='photo':buckets[('all_photos',None)].append(item)
        else:
            for group in set(item.get('groups',[])) & set(groups):
                if item.get('kind')=='photo':buckets[('person',group)].append(item)
                else:buckets[('person_name',group,Path(item['path']).name.casefold())].append(item)
    remap={};ignored=[];hashes={};pixels={}
    protected=set()
    for task in snapshot.get('submissions',[]):
        if task.get('inflight') or task.get('inflight_batch') or task.get('status') in ('sending','needs_review'):
            protected.update(file['id'] for file in task.get('files',[]))
    candidates=[bucket for bucket in buckets.values() if len(bucket)>1]
    total=sum(len(bucket) for bucket in candidates);checked=0
    for bucket in candidates:
        by_bytes={};by_pixels={}
        for item in sorted(bucket,key=lambda item:(not bool(item.get('groups')),not bool(item.get('managed_original')))):
            progress({'text':'按内容核对：'+item['name'],'current':checked,'total':total});checked+=1
            if item['id'] in protected or item['id'] in remap:continue
            try:
                expected={'size':item['size'],'mtime':item['mtime']}
                if fingerprint(Path(item['path']))!=expected:continue
                if item['id'] not in hashes:hashes[item['id']]=digest(item['path'])
                match=by_bytes.get(hashes[item['id']])
                if item.get('kind')=='photo' and not match:
                    if item['id'] not in pixels:pixels[item['id']]=picture_digest(item['path'])
                    if pixels[item['id']]:match=by_pixels.get(pixels[item['id']])
                if fingerprint(Path(item['path']))!=expected:continue
            except OSError:continue
            if match:
                match['groups']=list(dict.fromkeys(match['groups']+item.get('groups',[])))
                match['reviewed']=bool(match.get('reviewed') or item.get('reviewed'))
                match['returned_to_coser']=bool(match.get('returned_to_coser') or item.get('returned_to_coser'))
                remap[item['id']]=match['id'];ignored.append(str(Path(item['path']).resolve()))
            else:
                by_bytes[hashes[item['id']]]=item
                if pixels.get(item['id']):by_pixels[pixels[item['id']]]=item
    for ident in remap:
        target=remap[ident]
        while target in remap:target=remap[target]
        remap[ident]=target
    if not remap:return 0
    snapshot['items']=[item for item in snapshot['items'] if item['id'] not in remap]
    canonical={item['id']:item for item in snapshot['items']}
    for task in snapshot.get('submissions',[]):
        if task.get('status')=='submitted':continue
        for field in ('items','sent_files','archive_items'):
            if field in task:task[field]=list(dict.fromkeys(remap.get(ident,ident) for ident in task[field]))
        seen=set();files=[]
        for file in task.get('files',[]):
            old=file['id']
            file['id']=remap.get(old,old)
            if old in remap and file['id'] not in task.get('sent_files',[]):
                for field in ('path','name','kind','size','mtime','error'):
                    if field in canonical[file['id']]:file[field]=canonical[file['id']][field]
            if file['id'] not in seen:seen.add(file['id']);files.append(file)
        if 'files' in task:task['files']=files
    aliases=snapshot.setdefault('merged_media_ids',{})
    aliases.update(remap)
    for ident in list(aliases):
        target=aliases[ident]
        while target in remap:target=remap[target]
        aliases[ident]=target
    snapshot['ignored_source_paths']=list(dict.fromkeys(snapshot.get('ignored_source_paths',[])+ignored))
    for group in snapshot['groups'].values():group['published']=False
    with project.lock:
        # Preserve edits made while the background comparison was running.
        if project.data!=original:
            raise ValueError('素材清单发生变化，请重新检测重复')
        backup=project.folder/'backups';backup.mkdir(exist_ok=True)
        shutil.copy2(project.file,backup/('deduplicate_'+str(time.time_ns())+'.json'))
        project.data=snapshot;project.save();project.manifests()
    return len(remap)


def find_duplicates(items, progress=lambda _:None):
    assigned=defaultdict(list)
    for item in items:
        if item.get('groups'):assigned[Path(item['path']).name.casefold()].append(item)
    candidates=[item for item in items if not item.get('groups') and Path(item['path']).name.casefold() in assigned]
    rows=[];hashes={}
    for index,item in enumerate(candidates):
        progress({'text':'核对重复素材：'+item['name'],'current':index,'total':len(candidates)})
        for other in assigned[Path(item['path']).name.casefold()]:
            state='同名不同内容，请人工核对'
            try:
                if Path(item['path']).stat().st_size==Path(other['path']).stat().st_size:
                    for record in (item,other):
                        if record['path'] not in hashes:hashes[record['path']]=digest(record['path'])
                    if hashes[item['path']]==hashes[other['path']]:state='文件内容完全相同'
            except OSError:state='源文件缺失或无法读取'
            rows.append(dict(remove=item['id'],keep=other['id'],name=item['name'],state=state,old=item['path'],target=other['path'],groups=other['groups']))
    return rows


def remove_duplicate_records(project, rows):
    ids={row['remove'] for row in rows}
    with project.lock:
        for task in project.data['submissions']:
            if ids & {file['id'] for file in task.get('files',[])}:
                raise ValueError('记录被发送任务引用，请先核对对应任务：'+task.get('name',''))
        removed=[item for item in project.data['items'] if item['id'] in ids]
        if any(item.get('groups') for item in removed):raise ValueError('素材归属已变化，请重新检测')
        backup=project.folder/'backups';backup.mkdir(exist_ok=True)
        shutil.copy2(project.file,backup/('duplicate_records_'+str(time.time_ns())+'.json'))
        ignored=project.data.setdefault('ignored_source_paths',[])
        for item in removed:
            path=str(Path(item['path']).resolve())
            if path not in ignored:ignored.append(path)
        project.data['items']=[item for item in project.data['items'] if item['id'] not in ids]
        project.unpublish();project.save();project.manifests()
    return len(removed)
