"""Publish independent person albums, isolating failures between people."""
from pathlib import Path
from media import fingerprint, prepare_web_previews, transcode


def publish_people(project, gids, progress=lambda _:None):
    results = []
    for number, gid in enumerate(dict.fromkeys(gids), 1):
        data = project.snapshot()
        group = data['groups'][gid]
        items = [i for i in data['items'] if gid in i['groups']]
        status, reason = '已发布', ''
        with project.lock:
            project.data['groups'][gid]['published'] = False
            project.save()
        try:
            if not items:
                status, reason = '已跳过', '人物文件夹为空'
            else:
                for index, item in enumerate(items, 1):
                    progress(f'发布人物 {number}/{len(gids)} · {group["name"]} · 素材 {index}/{len(items)}')
                    if not item['reviewed'] or item['error']:
                        raise ValueError('素材未分配完成或读取失败：' + item['name'])
                    if fingerprint(Path(item['path'])) != {'size':item['size'], 'mtime':item['mtime']}:
                        raise ValueError('原片发生变化，请重新导入：' + item['name'])
                    prepare_web_previews(item, project.cache)
                    if item['kind'] == 'video':
                        destination = project.cache / (item['id'] + '.mp4')
                        if not destination.exists():
                            transcode(item['path'], destination)
        except Exception as exc:
            status, reason = '发布失败', str(exc)
        with project.lock:
            current = project.data['groups'][gid]
            current.update(published=status == '已发布', last_publish_status=status, last_publish_error=reason)
            project.save()
        results.append({'gid':gid, 'status':status, 'reason':reason})
    return results
