"""Persistent recent project paths, including the older last-project format."""
import json
from pathlib import Path


def read_recent(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        entries = data.get('recent', [])
        if not isinstance(entries, list):
            entries = []
        return normalize(([data['folder']] if data.get('folder') else []) + entries)
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def normalize(entries):
    result, seen = [], set()
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        folder = str(Path(entry).resolve())
        key = folder.casefold()
        if key not in seen:
            seen.add(key)
            result.append(folder)
    return result[:10]


def remember(path, folder):
    recent = normalize([str(Path(folder).resolve())] + read_recent(path))
    temp = Path(path).with_suffix('.tmp')
    temp.write_text(json.dumps({'folder': recent[0], 'recent': recent}, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)
    return recent
