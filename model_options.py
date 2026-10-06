from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKS = {
    'buffalo_l': ('det_10g.onnx', 'w600k_r50.onnx'),
    'buffalo_sc': ('det_500m.onnx', 'w600k_mbf.onnx'),
    'appearance': ('yolox.onnx', 'osnet_x0_5.onnx'),
    'hybrid': (),
}
OPTIONS = {
    'balanced': {'label': '均衡 · 中近景人像', 'pack': 'buffalo_l', 'size': 640,
                 'id': 'insightface-buffalo_l-scrfd10g-arcface-r50-v1',
                 'help': '使用当前 buffalo_l。适合一般中近景、人脸较清晰的照片。'},
    'small_faces': {'label': '细致 · 远景 / 多人小脸', 'pack': 'buffalo_l', 'size': 960,
                    'id': 'insightface-buffalo_l-scrfd10g-arcface-r50-960-v1',
                    'help': '仍使用 buffalo_l，将人脸检测输入从 640 提升到 960。尝试减少小脸漏检，计算较慢；不能识别完全看不清的脸，也不是场景识别。'},
    'fast': {'label': '快速 · 清晰正脸 / 大批量', 'pack': 'buffalo_sc', 'size': 640,
             'id': 'insightface-buffalo_sc-scrfd500m-mbf-640-v1',
             'help': '使用较小的 buffalo_sc。适合先快速粗分清晰人像；困难人脸识别可能弱于均衡模式，实际速度需按素材测量。'},
    'outfit': {'label': '服装 · 背影 / 遮脸 / 全身', 'pack': 'appearance', 'size': 640,
               'id': 'appearance-yolox-osnet05-msmt17-v1', 'appearance': True, 'face_option': None,
               'help': 'YOLOX 找出人物，OSNet 比较服装与整体外观。不使用人脸，相同衣服可能混淆；需要检查候选分组。'},
    'hybrid': {'label': '综合 · 人脸 + 服装外观', 'pack': 'hybrid', 'size': 640,
               'id': 'hybrid-buffalo_l-yolox-osnet05-msmt17-v1', 'appearance': True, 'face_option': 'balanced',
               'help': '清晰人脸优先，外观辅助背影与遮脸，适合模特不换装的混合素材。计算量更大；不含场景识别。'},
}

def installed(pack):
    if pack == 'hybrid':
        return installed('buffalo_l') and installed('appearance')
    return all((ROOT / 'models' / pack / name).is_file() for name in PACKS[pack])
