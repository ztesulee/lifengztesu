"""Fetch upstream person detector and OSNet ReID weights; export locally to ONNX."""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEST = ROOT / 'models' / 'appearance'
WEIGHT = 'osnet_x0_5_msmt17_combineall_256x128_amsgrad_ep150_stp60_lr0.0015_b64_fb10_softmax_labelsmooth_flip_jitter.pth'
REID_URL = 'https://huggingface.co/kaiyangzhou/osnet/resolve/main/' + WEIGHT
DETECT_REL = 'models/object_detection_yolox/object_detection_yolox_2022nov.onnx'

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()

def download(url, path, progress):
    if path.exists():
        return
    progress('下载：' + path.name)
    temp = path.with_suffix('.part')
    try:
        with urllib.request.urlopen(url, timeout=90) as response, temp.open('wb') as output:
            for block in iter(lambda: response.read(1024 * 1024), b''):
                output.write(block)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)

def install(progress=print):
    DEST.mkdir(parents=True, exist_ok=True)
    manifest = DEST / 'manifest.json'
    if manifest.exists():
        value = json.loads(manifest.read_text(encoding='utf-8'))
        if all((DEST / n).exists() and digest(DEST / n) == h for n, h in value['sha256'].items()):
            progress('YOLOX + OSNet 外观模型已校验。')
            return
    detector = DEST / 'yolox.onnx'
    download('https://media.githubusercontent.com/media/opencv/opencv_zoo/main/' + DETECT_REL, detector, progress)
    with urllib.request.urlopen('https://raw.githubusercontent.com/opencv/opencv_zoo/main/' + DETECT_REL, timeout=45) as response:
        pointer = response.read().decode()
    expected = next(line.split('sha256:')[1] for line in pointer.splitlines() if line.startswith('oid '))
    if digest(detector) != expected:
        raise ValueError('YOLOX 模型 SHA256 校验失败，请删除 models/appearance/yolox.onnx 后重试')
    weights = DEST / WEIGHT
    download(REID_URL, weights, progress)
    target = DEST / 'osnet_x0_5.onnx'
    if not target.exists():
        # Export is a separate process so build-only torch dependencies never affect GUI inference.
        import subprocess
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--export'], capture_output=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=300)
        if result.returncode:
            raise RuntimeError('OSNet 转换失败。新电脑请先安装 requirements-model-build.txt。\n' + result.stderr.decode('utf-8', 'replace')[-1200:])
    download('https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/object_detection_yolox/LICENSE', DEST / 'YOLOX_LICENSE', progress)
    manifest.write_text(json.dumps({'sources': [REID_URL, 'https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_yolox'],
                                   'sha256': {p.name: digest(p) for p in (detector, weights, target)},
                                   'model': 'OSNet x0.5, MSMT17 person ReID; appearance embedding, not clothing semantic labels'}, indent=2), encoding='utf-8')
    progress('人物检测与 OSNet 服装/外观识别模型已安装。')

def export():
    sys.path.insert(0, str(ROOT / '.deps'))
    sys.path.insert(0, str(ROOT / '.build_deps'))
    import torch
    from vendor.osnet.osnet import osnet_x0_5
    torch.set_num_threads(2)
    state = torch.load(DEST / WEIGHT, map_location='cpu', weights_only=True)
    state = state.get('state_dict', state)
    state = {k.removeprefix('module.'): v for k, v in state.items()}
    classes = state['classifier.weight'].shape[0]
    model = osnet_x0_5(num_classes=classes, pretrained=False)
    model.load_state_dict(state, strict=True)
    model.eval()
    target = DEST / 'osnet_x0_5.onnx'
    temp = target.with_suffix('.part.onnx')
    try:
        torch.onnx.export(model, torch.zeros(1, 3, 256, 128), str(temp), input_names=['images'], output_names=['features'],
                          opset_version=13, do_constant_folding=True, dynamo=False)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)

if __name__ == '__main__':
    export() if '--export' in sys.argv else install()
