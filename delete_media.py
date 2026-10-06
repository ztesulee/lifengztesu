"""Explicitly confirmed source deletion, with project state backup."""
import shutil
import time
from pathlib import Path
from media import fingerprint


def delete_sources(project, ids, progress=lambda _:None):
    with project.lock:
        selected=[item for item in project.data['items'] if item['id'] in ids]
        paths={str(Path(item['path']).resolve()).casefold():Path(item['path']).resolve() for item in selected}
        affected=[item for item in project.data['items'] if str(Path(item['path']).resolve()).casefold() in paths]
        affected_ids={item['id'] for item in affected}
        for task in project.data['submissions']:
            if affected_ids & {file['id'] for file in task.get('files',[])} and (task.get('inflight') or task.get('inflight_batch') or task.get('status') in ('sending','needs_review')):
                raise ValueError('文件关联任务正在发送或待核对，请先处理：'+task.get('name',''))
        for item in affected:
            path=Path(item['path'])
            if path.exists():
                if path.is_symlink() or not path.is_file():raise ValueError('无法删除非普通文件：'+str(path))
                if fingerprint(path)!={'size':item['size'],'mtime':item['mtime']}:
                    raise ValueError('源文件已变化，请先同步并核对后再删除：'+str(path))
            for suffix in ('.jpg','.webp','.mp4','.preview-v2'):
                cache=(project.cache/(item['id']+suffix)).resolve()
                cache.relative_to(project.cache.resolve())
        backup=project.folder/'backups';backup.mkdir(exist_ok=True)
        shutil.copy2(project.file,backup/('delete_sources_'+str(time.time_ns())+'.json'))
        removed_paths=set();errors=[]
        for index,(key,path) in enumerate(paths.items(),1):
            progress({'text':'删除源文件：'+path.name,'current':index-1,'total':len(paths)})
            try:
                path.unlink(missing_ok=True)
                removed_paths.add(key)
            except OSError as exc:errors.append(str(path)+'：'+str(exc))
        removed_ids=set()
        for item in affected:
            if str(Path(item['path']).resolve()).casefold() not in removed_paths:continue
            removed_ids.add(item['id'])
            for suffix in ('.jpg','.webp','.mp4','.preview-v2'):
                try:(project.cache/(item['id']+suffix)).unlink(missing_ok=True)
                except OSError as exc:errors.append('预览清理失败：'+item['name']+'：'+str(exc))
        project.data['items']=[item for item in project.data['items'] if item['id'] not in removed_ids]
        for task in project.data['submissions']:
            if task.get('status')!='submitted' and removed_ids & {file['id'] for file in task.get('files',[])}:
                task.update(status='error',error='任务源文件已删除，请删除此任务并重新创建')
        if removed_ids:
            project.unpublish();project.data['delivery_paused']=True
        project.save();project.manifests()
        return len(removed_paths),errors
