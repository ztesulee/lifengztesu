"""Evidence required before treating a WeChat attachment as submitted."""
from collections import Counter
from pathlib import Path


def attachment_confirmed(paths,before,after):
    if not paths:return False
    if after.get('failed',0)>before.get('failed',0):
        raise ValueError('微信显示发送失败，请核对客户端')
    if after.get('draft',True):return False
    needed=Counter(Path(path).name for path in paths)
    labels=all(after.get('files',{}).get(name,0)-before.get('files',{}).get(name,0)>=count for name,count in needed.items())
    photos=all(Path(path).suffix.lower() in ('.jpg','.jpeg','.png','.bmp','.tif','.tiff','.webp') for path in paths)
    images=photos and after.get('images',0)-before.get('images',0)>=len(paths)
    # ZIP cards can truncate or wrap their names beyond OCR recognition.
    # Only a newly appearing card counts; an existing card never confirms a send.
    archives=all(Path(path).suffix.lower()=='.zip' for path in paths)
    cards=archives and after.get('zip_cards',0)-before.get('zip_cards',0)>=len(paths)
    return labels or images or cards
