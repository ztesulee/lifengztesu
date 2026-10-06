"""InsightFace buffalo_l inference with ONNX Runtime DirectML / CPU.

SCRFD is the upstream MIT implementation; ArcFace alignment uses a least-squares
similarity transform to the published five-point 112x112 template.
"""
import sys
from pathlib import Path
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.deps'))
import onnxruntime as ort
from vendor.insightface.scrfd import SCRFD
from model_options import OPTIONS, PACKS

MODEL_ID = 'insightface-buffalo_l-scrfd10g-arcface-r50-v1'
DIMENSION = 512
TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)

def align_face(image, landmarks):
    source = np.asarray(landmarks, dtype=np.float64)
    if source.shape != (5, 2) or not np.isfinite(source).all():
        raise ValueError('无效的人脸关键点')
    # x'=a*x-b*y+tx, y'=b*x+a*y+ty. Same non-reflective similarity family as ArcFace.
    matrix = np.zeros((10, 4), dtype=np.float64)
    matrix[0::2, 0] = source[:, 0]
    matrix[0::2, 1] = -source[:, 1]
    matrix[0::2, 2] = 1
    matrix[1::2, 0] = source[:, 1]
    matrix[1::2, 1] = source[:, 0]
    matrix[1::2, 3] = 1
    a, b, tx, ty = np.linalg.lstsq(matrix, TEMPLATE.reshape(-1), rcond=None)[0]
    affine = np.array([[a, -b, tx], [b, a, ty]], dtype=np.float32)
    return cv2.warpAffine(image, affine, (112, 112), borderValue=0)

class InsightBackend:
    def __init__(self, progress=lambda _:None, force_cpu=False, profile=False, option='balanced'):
        self.progress = progress
        self.profile = profile
        self.option = OPTIONS[option]
        self.model_id = self.option['id']
        self.directory = ROOT / 'models' / self.option['pack']
        self.files = PACKS[self.option['pack']]
        for name in self.files:
            if not (self.directory / name).is_file():
                raise RuntimeError('InsightFace 模型未安装。请点击“安装/检查识别模型”，或运行 python setup_models.py。')
        wanted = 'DmlExecutionProvider' if not force_cpu and 'DmlExecutionProvider' in ort.get_available_providers() else 'CPUExecutionProvider'
        try:
            self.initialize(wanted)
        except Exception as exc:
            if wanted == 'CPUExecutionProvider':
                raise
            progress('显卡初始化失败，切换 CPU：' + str(exc)[:180])
            self.initialize('CPUExecutionProvider')
        progress(self.status)

    def initialize(self, provider):
        sessions = []
        for name in self.files:
            options = ort.SessionOptions()
            options.intra_op_num_threads = 4
            options.inter_op_num_threads = 1
            options.enable_mem_pattern = False  # Required by DirectML.
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.enable_profiling = self.profile
            if self.profile:
                folder = ROOT / 'test-artifacts'
                folder.mkdir(exist_ok=True)
                options.profile_file_prefix = str(folder / ('ort_' + name))
            model_source = str(self.directory / name)
            if name == self.files[0] and self.option['size'] != 640:
                import onnx
                model = onnx.load(model_source)
                size = self.option['size']
                dimensions = model.graph.input[0].type.tensor_type.shape.dim
                dimensions[2].dim_value = size
                dimensions[3].dim_value = size
                # Official SCRFD graph has stale 640px output metadata; DML requires exact shapes.
                for index, output in enumerate(model.graph.output):
                    dims = output.type.tensor_type.shape.dim
                    stride = (8, 16, 32)[index % 3]
                    dims[-2].dim_value = (size // stride) ** 2 * 2
                    dims[-1].dim_value = (1, 4, 10)[index // 3]
                del model.graph.value_info[:]
                model_source = model.SerializeToString()
            sessions.append(ort.InferenceSession(model_source, sess_options=options,
                            providers=[provider, 'CPUExecutionProvider'] if provider != 'CPUExecutionProvider' else [provider]))
        self.detection_session, self.recognition_session = sessions
        self.detector = SCRFD(str(self.directory / self.files[0]), session=self.detection_session)
        size = self.option['size']
        self.detector.prepare(ctx_id=0, input_size=(size, size), det_thresh=.6)
        self.input_name = self.recognition_session.get_inputs()[0].name
        # Exercise both networks before reporting readiness, including GPU graph compilation.
        self.detector.detect(np.zeros((size, size, 3), dtype=np.uint8))
        self.recognition_session.run(None, {self.input_name: np.zeros((1, 3, 112, 112), dtype=np.float32)})
        accelerated = all('DmlExecutionProvider' in s.get_providers() for s in sessions)
        self.provider = 'DirectML GPU' if accelerated else 'CPU'
        self.status = f'{self.option["pack"]} · {self.provider} · 检测 {size}px'

    def faces(self, image):
        try:
            return self._faces(image)
        except ort.capi.onnxruntime_pybind11_state.Fail:
            if self.provider == 'CPU':
                raise
            self.progress('显卡推理失败，重新初始化 CPU 后重试当前画面。')
            self.initialize('CPUExecutionProvider')
            self.progress(self.status)
            return self._faces(image)

    def _faces(self, image):
        boxes, landmarks = self.detector.detect(image)
        results = []
        for box, points in zip(boxes, landmarks if landmarks is not None else []):
            x1, y1, x2, y2, confidence = box
            if min(x2 - x1, y2 - y1) < 24:
                continue
            crop = align_face(image, points)
            blob = cv2.dnn.blobFromImage(crop, 1 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
            feature = self.recognition_session.run(None, {self.input_name: blob})[0].reshape(-1)
            norm = np.linalg.norm(feature)
            if feature.size != DIMENSION or not np.isfinite(feature).all() or norm < 1e-8:
                continue
            results.append((box, feature / norm))
        return results

    def end_profiles(self):
        return [s.end_profiling() for s in (self.detection_session, self.recognition_session)]
