"""Repair source-path ID collisions without deleting material records."""
import hashlib
import shutil
import time
from pathlib import Path


def available_id(ident, path, used):
    index=0
    candidate=ident
    while candidate in used:
        index+=1
        candidate=hashlib.sha256((str(path)+'#'+str(index)).encode()).hexdigest()[:20]
    return candidate


def repair_ids(data, folder, cache):
    items=data['items']
    if len({item['id'] for item in items})==len(items):return 0
    backup=Path(folder)/'backups';backup.mkdir(exist_ok=True)
    shutil.copy2(Path(folder)/'project.json',backup/('repair_ids_'+str(time.time_ns())+'.json'))
    used={item['id'] for item in items}
    seen=set();changed=0
    # Keep established person and task identities whenever possible.
    ordered=sorted(items,key=lambda item:(not bool(item.get('groups')),not bool(item.get('managed_original'))))
    paths={}
    for item in ordered:
        old=item['id']
        if old in seen:
            new=available_id(old,item['path'],used)
            for suffix in ('.jpg','.webp','.mp4','.preview-v2'):
                source=Path(cache)/(old+suffix)
                if source.is_file():shutil.copy2(source,Path(cache)/(new+suffix))
            item['id']=new;used.add(new);changed+=1
        seen.add(old)
        paths[(old,str(Path(item['path']).resolve()).casefold())]=item['id']
    for task in data.get('submissions',[]):
        remap={}
        for file in task.get('files',[]):
            old=file['id'];new=paths.get((old,str(Path(file['path']).resolve()).casefold()),old)
            file['id']=new;remap[old]=new
        for field in ('items','sent_files','inflight_batch','archive_items'):
            if field in task:task[field]=[remap.get(ident,ident) for ident in task[field]]
        if task.get('inflight') in remap:task['inflight']=remap[task['inflight']]
    for group in data.get('groups',{}).values():group['published']=False
    return changed
