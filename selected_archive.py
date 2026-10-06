"""Package selected originals with byte progress and no file replacement."""
import copy
import zipfile
import time
from pathlib import Path
from media import fingerprint
from project import safe_name


def compress_selected(project, ids, group=None, progress=lambda _:None):
    with project.lock:
        items=copy.deepcopy([item for item in project.data['items'] if item['id'] in ids and item.get('kind')=='photo'])
        groups=copy.deepcopy(project.data['groups'])
        title=project.data['title']
    if not items:raise ValueError('请先选中一张或多张图片')
    if group not in groups:raise ValueError('请进入单个人物文件夹后压缩图片；全部素材和未分配不支持压缩')
    if any(group not in item.get('groups',[]) for item in items):raise ValueError('选中图片不属于当前人物文件夹')
    related=set().union(*(set(item.get('groups',[])) for item in items))
    person=groups[group]['name'];folder=project.group_folder(group)
    folder.mkdir(parents=True,exist_ok=True)
    stem=safe_name(person+'_'+title)
    archive=folder/(stem+'.zip')
    index=2
    while archive.exists():archive=folder/(stem+'_'+str(index)+'.zip');index+=1
    total=sum(item['size'] for item in items)
    completed=0;last_progress=0;created=False
    try:
        with archive.open('xb') as output:
            created=True
            with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as package:
                names=set()
                for item in items:
                    path=Path(item['path']).resolve(strict=True)
                    expected={'size':item['size'],'mtime':item['mtime']}
                    if fingerprint(path)!=expected:raise ValueError('原片已变化，请先刷新素材：'+item['name'])
                    name=path.name;n=2
                    while name.casefold() in names:name=f'{path.stem}_{n}{path.suffix}';n+=1
                    names.add(name.casefold())
                    with path.open('rb') as source, package.open(name,'w',force_zip64=True) as destination:
                        while True:
                            block=source.read(1024*1024)
                            if not block:break
                            destination.write(block);completed+=len(block)
                            if time.monotonic()-last_progress>.1:
                                progress({'text':'正在压缩：'+item['name'],'current':completed,'total':max(total,1),'unit':'字节'})
                                last_progress=time.monotonic()
                    if fingerprint(path)!=expected:raise ValueError('压缩期间原片变化：'+item['name'])
    except Exception:
        if created:archive.unlink(missing_ok=True)
        raise
    with project.lock:
        project.data.setdefault('manual_archives',[]).append({'path':str(archive),'groups':sorted(related),
                    'person':person,'items':[item['id'] for item in items]})
        project.save()
    progress({'text':'压缩完成：'+archive.name,'current':max(total,1),'total':max(total,1),'unit':'字节'})
    return archive
