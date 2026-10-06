"""Local media previews. No recognition models are loaded."""
import os
import sys
import math
import hashlib
import time
from io import BytesIO
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.deps'))
PHOTOS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
VIDEOS = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'}
RAW = {'.nef', '.arw', '.cr2', '.cr3', '.dng', '.raf', '.orf', '.rw2'}

def collect(folder, progress=lambda _:None):
    root = Path(folder).resolve(strict=True)
    result, skipped = [], []
    checked = 0
    last_notice = 0
    def scan_error(error):raise error
    for parent, dirs, files in os.walk(root, followlinks=False, onerror=scan_error):
        checked += len(files)
        if time.monotonic()-last_notice > .2:
            progress(f'扫描文件夹：已检查 {checked} 个文件 · {parent}')
            last_notice=time.monotonic()
        dirs[:] = sorted(d for d in dirs if not linked(Path(parent) / d))
        for name in sorted(files):
            p = Path(parent) / name
            if linked(p):
                continue
            p = p.resolve(strict=True)
            p.relative_to(root)
            if p.suffix.lower() in PHOTOS | VIDEOS | RAW:
                result.append(p)
            else:
                skipped.append(str(p))
    return result, skipped

def linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)

def fingerprint(path):
    stat = path.stat()
    return {'size': stat.st_size, 'mtime': stat.st_mtime_ns}

def read_photo(path):
    if path.suffix.lower() in RAW:
        import rawpy
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    with Image.open(BytesIO(thumb.data)) as source:
                        image = ImageOps.exif_transpose(source).convert('RGB')
                        image.thumbnail((1800, 1800))
                        return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
                if thumb.format == rawpy.ThumbFormat.BITMAP:
                    return fit(cv2.cvtColor(thumb.data, cv2.COLOR_RGB2BGR), 1800)
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                pass
            rgb = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
            return fit(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), 1800)
    with Image.open(path) as source:
        source.draft('RGB', (1800, 1800))
        image = ImageOps.exif_transpose(source)
        image.thumbnail((1800, 1800))
        return cv2.cvtColor(np.array(image.convert('RGB')), cv2.COLOR_RGB2BGR)

def fit(frame, limit=1280):
    h, w = frame.shape[:2]
    ratio = min(1.0, limit / max(h, w))
    return cv2.resize(frame, (max(1, int(w * ratio)), max(1, int(h * ratio))))

def prepare_web_previews(item, cache):
    """Upgrade existing previews without repeating recognition or reading the original."""
    marker = cache / (item['id'] + '.preview-v2')
    revision = f'{item["size"]}:{item["mtime"]}'
    if marker.exists() and marker.read_text() == revision:
        return
    with Image.open(cache / (item['id'] + '.jpg')) as source:
        image = source.convert('RGB')
    image.thumbnail((1200, 1200))
    temp = cache / (item['id'] + '.preview.tmp')
    try:
        image.save(temp, format='JPEG', quality=78, optimize=True)
        temp.replace(cache / (item['id'] + '.jpg'))
        image.thumbnail((360, 360))
        image.save(temp, format='WEBP', quality=65)
        temp.replace(cache / (item['id'] + '.webp'))
        marker.write_text(revision)
    finally:
        temp.unlink(missing_ok=True)

def import_media(path, cache, gid, ident=None):
    """Create only a thumbnail and viewing image, assigning the chosen person."""
    path = Path(path).resolve(strict=True)
    info = fingerprint(path)
    ident = ident or hashlib.sha256(str(path).encode()).hexdigest()[:20]
    kind = 'video' if path.suffix.lower() in VIDEOS else 'photo'
    if kind == 'video':
        cap = cv2.VideoCapture(str(path))
        try:
            ok, frame = cap.read()
            if not ok:
                raise ValueError('无法读取视频封面')
        finally:
            cap.release()
    else:
        frame = read_photo(path)
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    image.thumbnail((1200, 1200))
    image.save(cache / (ident + '.jpg'), quality=78, optimize=True)
    image.thumbnail((360, 360))
    image.save(cache / (ident + '.webp'), quality=65)
    if fingerprint(path) != info:
        raise ValueError('导入期间原文件发生变化，请重试')
    (cache / (ident + '.preview-v2')).write_text(f'{info["size"]}:{info["mtime"]}')
    return {'id': ident, 'path': str(path), 'name': path.name, 'kind': kind,
            **info, 'groups': [gid], 'reviewed': True, 'error': '',
            'note': '手动添加到人物文件夹'}

def transcode(source, destination):
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    temp = destination.with_suffix('.part.mp4')
    try:
        result = subprocess.run([exe, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
            '-vf', "scale=960:960:force_original_aspect_ratio=decrease:force_divisible_by=2", '-r', '24',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '28', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', str(temp)],
            capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=1800)
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-600:])
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
