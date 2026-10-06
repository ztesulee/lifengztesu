"""Install official InsightFace buffalo_l; keep local integrity checksums."""
import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path
from model_options import PACKS
ROOT = Path(__file__).resolve().parent / 'models' / 'buffalo_l'
URL = 'https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip'
FILES = ('det_10g.onnx', 'w600k_r50.onnx')
def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''): digest.update(block)
    return digest.hexdigest()
def install(progress=print, pack='buffalo_l'):
    if pack in ('appearance', 'hybrid'):
        if pack == 'hybrid':
            install(progress, pack='buffalo_l')
        from setup_appearance import install as install_appearance
        return install_appearance(progress)
    FILES = PACKS[pack]
    ROOT = Path(__file__).resolve().parent / 'models' / pack
    URL = f'https://github.com/deepinsight/insightface/releases/download/v0.7/{pack}.zip'
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = ROOT / 'manifest.json'
    if manifest.exists():
        hashes = json.loads(manifest.read_text(encoding='utf-8'))['sha256']
        if all((ROOT / name).exists() and sha256(ROOT / name) == hashes.get(name) for name in FILES):
            progress(f'InsightFace {pack} 模型已校验。')
            return
    progress(f'下载官方 InsightFace {pack}，照片不会上传。')
    archive_path = ROOT / 'download.zip.part'
    try:
        with urllib.request.urlopen(URL, timeout=90) as response, archive_path.open('wb') as output:
            received, last = 0, 0
            while True:
                block = response.read(1024 * 1024)
                if not block: break
                output.write(block)
                received += len(block)
                if received - last >= 25 * 1024 * 1024:
                    progress(f'已下载 {received // (1024 * 1024)} MB')
                    last = received
        with zipfile.ZipFile(archive_path) as archive:
            for name in FILES:
                entry = next(n for n in archive.namelist() if Path(n).name == name)
                temp = ROOT / (name + '.part')
                try:
                    with archive.open(entry) as source, temp.open('wb') as output: shutil.copyfileobj(source, output)
                    temp.replace(ROOT / name)
                finally: temp.unlink(missing_ok=True)
        value = {'source': URL, 'sha256': {name: sha256(ROOT / name) for name in FILES}, 'license': 'Official pretrained models: non-commercial research only; commercial use requires authorization.'}
        manifest.write_text(json.dumps(value, indent=2), encoding='utf-8')
        progress('模型已安装；首次下载通过 HTTPS 与 ZIP CRC 检查，SHA256 用于后续本地完整性校验。')
    finally: archive_path.unlink(missing_ok=True)
if __name__ == '__main__': install()
