"""Local media analysis. Face embeddings are never sent to the web client."""
import os
import sys
import math
import hashlib
from io import BytesIO
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.deps'))
from insight_backend import InsightBackend, MODEL_ID, DIMENSION
from model_options import OPTIONS
PHOTOS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
VIDEOS = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'}
RAW = {'.nef', '.arw', '.cr2', '.cr3', '.dng', '.raf', '.orf', '.rw2'}

def collect(folder):
    root = Path(folder).resolve(strict=True)
    result, skipped = [], []
    for parent, dirs, files in os.walk(root, followlinks=False):
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

def histogram(frame):
    if frame.size == 0:
        return None
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [24, 12], [0, 180, 0, 256]).flatten()
    return hist / max(1e-8, np.linalg.norm(hist))

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

class Analyzer:
    def __init__(self, progress=lambda _:None, force_cpu=False, option='balanced'):
        cv2.setNumThreads(2)
        self.option = OPTIONS[option]
        self.appearance = None
        if self.option.get('appearance'):
            from appearance_backend import AppearanceBackend
            self.appearance = AppearanceBackend(progress, force_cpu)
            self.face_backend = InsightBackend(progress, force_cpu, option=self.option['face_option']) if self.option['face_option'] else None
            self.backend = self.face_backend or self.appearance
        else:
            self.backend = InsightBackend(progress, force_cpu, option=option)
            self.face_backend = self.backend
        self.model_id = self.option['id']
        self.people = cv2.HOGDescriptor()
        self.people.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    @property
    def status(self):
        if self.appearance and self.face_backend:
            return f'人脸 {self.face_backend.provider} + OSNet {self.appearance.provider}'
        return self.backend.status

    def entities(self, image, frame):
        image = fit(image)
        records = self.appearance.people(image)
        faces = self.face_backend.faces(image) if self.face_backend else []
        associations = {}
        unmatched = []
        for box, vector in faces:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            candidates = []
            for index, person in enumerate(records):
                x1, y1, x2, y2 = person['bbox']
                if x1 <= cx <= x2 and y1 <= cy <= y1 + .6 * (y2 - y1):
                    candidates.append(((x2 - x1) * (y2 - y1), index))
            if candidates:
                index = min(candidates)[1]
                associations.setdefault(index, []).append(vector.tolist())
            else:
                unmatched.append({'appearance': None, 'face': vector.tolist(), 'frame': frame})
        for index, person in enumerate(records):
            matches = associations.get(index, [])
            person['face'] = matches[0] if len(matches) == 1 else None
            person['frame'] = frame
        return records + unmatched

    def clothing_candidates(self, frame):
        image = fit(frame, 800)
        if image.shape[0] < 128 or image.shape[1] < 64:
            return []
        boxes, weights = self.people.detectMultiScale(image, winStride=(8, 8), padding=(8, 8), scale=1.08)
        result = []
        for x, y, w, h in boxes:
            torso = image[y + int(h * .2):y + int(h * .6), x + int(w * .15):x + int(w * .85)]
            vector = histogram(torso)
            if vector is not None:
                result.append(vector.tolist())
        return result

    def faces(self, frame):
        if self.face_backend is None:
            return []
        image = fit(frame)
        h, w = image.shape[:2]
        result = []
        for box, vector in self.face_backend.faces(image):
            x, y, x2, y2 = box[:4]
            fw, fh = x2 - x, y2 - y
            # Approximate upper-body crop; used only as supporting colour evidence.
            crop = image[max(0, int(y + fh)):min(h, int(y + 4.5 * fh)), max(0, int(x - fw)):min(w, int(x + 2 * fw))]
            cloth = histogram(crop)
            result.append({'face': vector.tolist(), 'cloth': cloth.tolist() if cloth is not None else None})
        return result

    def analyze(self, path, cache, progress, cancel):
        path = Path(path)
        ident = hashlib.sha256(str(path).encode()).hexdigest()[:20]
        info = fingerprint(path)
        cache.mkdir(parents=True, exist_ok=True)
        video = path.suffix.lower() in VIDEOS
        observations, frame_count, cuts, clothes, appearances = [], 0, 0, [], []
        first = None
        if not video:
            first = read_photo(path)
            if self.appearance:
                appearances = self.entities(first, 0)
            else:
                observations = self.faces(first)
                if not observations:
                    clothes = self.clothing_candidates(first)
            frame_count = 1
        else:
            capture = cv2.VideoCapture(str(path))
            try:
                fps = capture.get(cv2.CAP_PROP_FPS)
                total = capture.get(cv2.CAP_PROP_FRAME_COUNT)
                if not math.isfinite(fps) or not math.isfinite(total) or fps <= 0 or total <= 0:
                    raise ValueError('无法读取视频时长')
                duration = total / fps
                # Sample every second, capped at 180 across the entire clip.
                times = np.linspace(0, max(0, duration - .1), min(180, max(1, math.ceil(duration))))
                previous = None
                for index, second in enumerate(times):
                    if cancel.is_set():
                        raise InterruptedError('已停止分析')
                    capture.set(cv2.CAP_PROP_POS_MSEC, float(second) * 1000)
                    ok, frame = capture.read()
                    if not ok:
                        continue
                    frame = fit(frame)
                    if first is None:
                        first = frame.copy()
                    current = histogram(frame)
                    changed = previous is not None and float(current @ previous) < .65
                    if changed:
                        cuts += 1
                    # Analyze all samples, so a camera move doesn't rely on a single cover.
                    if self.appearance:
                        appearances.extend(self.entities(frame, index))
                    else:
                        found = self.faces(frame)
                        observations.extend(found)
                        if not found and index % 3 == 0:
                            clothes.extend(self.clothing_candidates(frame))
                    frame_count += 1
                    previous = current
                    if index % 10 == 0:
                        progress(f'视频抽帧 {index + 1}/{len(times)}：{path.name}')
            finally:
                capture.release()
        if first is None:
            raise ValueError('未能解码素材')
        rgb = Image.fromarray(cv2.cvtColor(first, cv2.COLOR_BGR2RGB))
        rgb.thumbnail((1200, 1200))
        rgb.save(cache / (ident + '.jpg'), quality=78, optimize=True)
        rgb.thumbnail((360, 360))
        rgb.save(cache / (ident + '.webp'), quality=65)
        (cache / (ident + '.preview-v2')).write_text(f'{info["size"]}:{info["mtime"]}')
        return {'id': ident, 'path': str(path), 'name': path.name, 'kind': 'video' if video else 'photo', **info,
                'groups': [], 'reviewed': False, 'observations': observations, 'clothes': clothes, 'analysis_model': self.model_id,
                'appearances': appearances,
                'note': f'视频抽取 {frame_count} 帧，检测到 {cuts} 次画面明显变化；需检查漏识别' if video else '待确认自动分组',
                'error': '', 'frames': frame_count}

def assign_groups(items, groups, model_id=MODEL_ID):
    """Conservative face matching; clothing can support but never establish identity alone."""
    appearance_option = next((o for o in OPTIONS.values() if o['id'] == model_id and o.get('appearance')), None)
    if appearance_option:
        from appearance_groups import assign_appearance_groups
        return assign_appearance_groups(items, groups, model_id, bool(appearance_option['face_option']))
    prototypes = {}
    for item in items:
        if len(item['groups']) == 1 and item.get('analysis_model') == model_id:
            prototypes.setdefault(item['groups'][0], []).extend(o for o in item.get('observations', []) if len(o['face']) == DIMENSION)
    for item in items:
        if item.get('reviewed') or item.get('error') or item.get('groups'):
            continue
        assigned = set()
        for observation in item.get('observations', []):
            if item.get('analysis_model') != model_id or len(observation['face']) != DIMENSION:
                continue
            scores = []
            vector = np.asarray(observation['face'])
            for group_id, samples in prototypes.items():
                best = -1
                for sample in samples[-30:]:
                    score = float(vector @ np.asarray(sample['face']))
                    if observation['cloth'] is not None and sample['cloth'] is not None and score > .38:
                        score += .04 * float(np.asarray(observation['cloth']) @ np.asarray(sample['cloth']))
                    best = max(best, score)
                scores.append((best, group_id))
            scores.sort(reverse=True)
            if scores and scores[0][0] >= .48 and (len(scores) == 1 or scores[0][0] - scores[1][0] >= .035):
                group_id = scores[0][1]
            elif scores and scores[0][0] >= .36:
                continue  # ambiguous similarity stays for manual review
            else:
                import secrets
                group_id = secrets.token_hex(6)
                groups[group_id] = {'name': f'人物 {len(groups) + 1:02d}', 'token': secrets.token_urlsafe(24), 'published': False}
            assigned.add(group_id)
            samples = prototypes.setdefault(group_id, [])
            if len(samples) < 30:
                samples.append(observation)
        item['groups'] = sorted(assigned)
        if not assigned:
            item['note'] += '；未可靠识别人脸，请手动指定人物'
    # Second pass lets distant/occluded subjects use already established clothing references.
    # These remain explicitly unreviewed and cannot be published without manual confirmation.
    for item in items:
        if item.get('reviewed') or item.get('error') or item['groups']:
            continue
        candidates = set()
        for vector in item.get('clothes', []):
            scores = []
            for gid, samples in prototypes.items():
                score = max((float(np.asarray(vector) @ np.asarray(s['cloth'])) for s in samples if s['cloth'] is not None), default=-1)
                scores.append((score, gid))
            scores.sort(reverse=True)
            if scores and scores[0][0] >= .9 and (len(scores) == 1 or scores[0][0] - scores[1][0] >= .08):
                candidates.add(scores[0][1])
        if candidates:
            item['groups'] = sorted(candidates)
            item['note'] = '仅服装颜色候选，未确认人脸；请重点检查，不代表已识别身份。' + item['note']

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
