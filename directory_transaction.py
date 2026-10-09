"""Recoverable replacement of exported recipient directories."""
import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path

OWNER_FILE='.photo-return-owner.json'


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    with temp.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)


def identity(name,recipient):
    return {'name':name,'recipient':recipient}


def choose_directory(base,label,owner,groups,reserved):
    """Keep familiar names; suffix collisions rather than trusting a display name."""
    base=Path(base).resolve()
    digest=hashlib.sha256(json.dumps(owner,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:12]
    for suffix in ('','_'+digest):
        target=base/(label+suffix)
        key=str(target).casefold()
        if target.is_symlink() or target.resolve()!=target:
            raise ValueError('人物目录不能使用链接或重定向路径：'+str(target))
        known=reserved.get(key)
        if known is None and target.exists():
            marker=target/OWNER_FILE
            if marker.is_file():
                known=json.loads(marker.read_text(encoding='utf-8'))
            else:
                # Migrate only directories whose prior owner is recorded by this backend.
                from project import person_recipient
                owners=[]
                for group in groups.values():
                    if any(str(Path(group.get(field,'')).resolve()).casefold()==key for field in ('export_directory','archive_directory') if group.get(field)):
                        owners.append(identity(group['name'],person_recipient(group)))
                known=owner if owners and all(item==owner for item in owners) else {'unmanaged':True}
        if known is None or known==owner:
            reserved[key]=owner
            return target
    raise ValueError('人物目录归属冲突，请选择其他导出位置：'+str(target))


class DirectoryTransaction:
    def __init__(self,folder,directories):
        self.folder=Path(folder)
        self.ident=secrets.token_hex(16)
        self.path=self.folder/'directory-transactions'/(self.ident+'.json')
        self.entries=[]
        for index,(destination,owner) in enumerate(directories.items()):
            destination=Path(destination)
            self.entries.append(dict(destination=str(destination),owner=owner,existed=destination.exists(),
                stage=str(destination.parent/('.return-stage-'+self.ident+'-'+str(index))),
                backup=str(destination.parent/('.return-backup-'+self.ident+'-'+str(index)))))
        self._validate()
        write_json(self.path,{'id':self.ident,'entries':self.entries})

    def _validate(self):
        for index,entry in enumerate(self.entries):
            target=Path(entry['destination'])
            if not target.is_absolute() or target.parent==target or target.resolve()!=target or target.is_symlink():
                raise ValueError('目录恢复路径校验失败：'+str(target))
            for field,prefix in (('stage','.return-stage-'),('backup','.return-backup-')):
                path=Path(entry[field])
                if path.parent!=target.parent or path.name!=prefix+self.ident+'-'+str(index) or path.is_symlink() or path.resolve()!=path:
                    raise ValueError('目录恢复备份路径校验失败')

    def stage(self):
        for entry in self.entries:
            path=Path(entry['stage']);path.mkdir(parents=True,exist_ok=False)
            write_json(path/OWNER_FILE,entry['owner'])

    def stage_for(self,destination):
        return Path(next(e['stage'] for e in self.entries if e['destination']==str(destination)))

    def install(self):
        self._validate()
        for entry in self.entries:
            destination=Path(entry['destination'])
            if destination.exists():destination.rename(entry['backup'])
            Path(entry['stage']).rename(destination)

    def committed(self):
        file=self.folder/'project.json'
        data=json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}
        return self.ident in data.get('directory_commits',[])

    def rollback(self):
        self._validate()
        for entry in reversed(self.entries):
            destination,stage,backup=(Path(entry[k]) for k in ('destination','stage','backup'))
            if backup.exists():
                if destination.exists():shutil.rmtree(destination)
                backup.rename(destination)
            elif not entry['existed'] and not stage.exists() and destination.exists():
                # Only a directory carrying this transaction's owner marker may be removed.
                marker=destination/OWNER_FILE
                if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8'))!=entry['owner']:
                    raise ValueError('新目录归属无法核对，保留现场：'+str(destination))
                shutil.rmtree(destination)
            if stage.exists():shutil.rmtree(stage)
        self.path.unlink(missing_ok=True)

    def finish(self):
        self._validate()
        for entry in self.entries:
            for field in ('backup','stage'):
                path=Path(entry[field])
                if path.exists():shutil.rmtree(path)
        self.path.unlink(missing_ok=True)


def recover_transactions(folder):
    folder=Path(folder)
    for path in sorted((folder/'directory-transactions').glob('*.json')):
        data=json.loads(path.read_text(encoding='utf-8'))
        transaction=DirectoryTransaction.__new__(DirectoryTransaction)
        transaction.folder=folder;transaction.path=path
        transaction.ident=data['id'];transaction.entries=data['entries']
        if path.stem!=transaction.ident:raise ValueError('目录恢复记录编号不匹配')
        if transaction.committed():transaction.finish()
        else:transaction.rollback()
