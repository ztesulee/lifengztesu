"""Read-only cross-project contact detection; never instantiate Project."""
import hashlib
import json
import os
import stat
from pathlib import Path
from collections import defaultdict


def scan_history(project_root, recent=(), current=None):
    files = set()
    root = Path(project_root)
    if root.is_dir():
        for parent, dirs, names in os.walk(root, followlinks=False):
            safe_dirs = []
            for name in dirs:
                try:
                    info = (Path(parent)/name).lstat()
                    if not stat.S_ISLNK(info.st_mode) and not getattr(info,'st_file_attributes',0) & 0x400:
                        safe_dirs.append(name)
                except OSError:
                    pass
            dirs[:] = safe_dirs
            if 'project.json' in names:
                files.add((Path(parent)/'project.json').resolve())
                dirs[:] = []
    for folder in list(recent) + ([current] if current else []):
        files.add((Path(folder)/'project.json').resolve())
    contacts, errors = defaultdict(list), []
    for file in sorted(files, key=lambda p:str(p).casefold()):
        try:
            data = json.loads(file.read_text(encoding='utf-8'))
            if not isinstance(data.get('groups'),dict) or not isinstance(data.get('items'),list):
                raise ValueError('项目格式无效')
            for gid, group in data['groups'].items():
                accounts = {**group.get('legacy_contacts',{}), **group.get('contacts',{})}
                accounts = {key:str(value).strip() for key,value in accounts.items()
                            if key in ('qq','wechat') and value and str(value).strip()}
                items = [item for item in data['items'] if gid in item.get('groups',[])]
                missing = 0
                for item in items:
                    path = Path(item.get('path',''))
                    if not path.is_absolute():path = file.parent/path
                    missing += not path.is_file()
                entry = dict(project=data.get('title') or file.parent.name, folder=str(file.parent),
                             person=group.get('name',gid), group=gid, accounts=accounts,
                             count=len(items), missing=missing)
                for channel, account in accounts.items():
                    if channel == 'qq' and not account.isdigit():continue
                    contacts[(channel,account)].append(entry)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append(str(file)+'：'+str(exc))
    duplicates=[]
    for (channel,account), entries in sorted(contacts.items()):
        if len({entry['folder'].casefold() for entry in entries}) < 2:continue
        other = 'wechat' if channel == 'qq' else 'qq'
        conflict = len({entry['accounts'][other] for entry in entries if entry['accounts'].get(other)}) > 1
        duplicates.append(dict(channel=channel,account=account,conflict=conflict,entries=entries))
    digest = hashlib.sha256(json.dumps(duplicates,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return dict(duplicates=duplicates,errors=errors,digest=digest,projects=len(files)-len(errors))
