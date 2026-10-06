"""Local YOLOX person crops + OSNet x0.5 MSMT17 appearance embeddings."""
import sys
from pathlib import Path
import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.deps'))
import onnxruntime as ort

class AppearanceBackend:
    def __init__(self, progress=lambda _:None, force_cpu=False, profile=False):
        self.progress, self.profile = progress, profile
        self.directory = ROOT / 'models' / 'appearance'
        provider = 'DmlExecutionProvider' if not force_cpu and 'DmlExecutionProvider' in ort.get_available_providers() else 'CPUExecutionProvider'
        grids, strides = [], []
        for stride in (8, 16, 32):
            yy, xx = np.mgrid[:640 // stride, :640 // stride]
            grids.append(np.stack((xx, yy), axis=-1).reshape(-1, 2))
            strides.append(np.full(((640 // stride) ** 2, 1), stride))
        self.grid, self.strides = np.concatenate(grids), np.concatenate(strides)
        try:
            self.initialize(provider)
        except Exception as exc:
            if provider == 'CPUExecutionProvider':
                raise
            progress('外观模型 GPU 初始化失败，改用 CPU：' + str(exc)[:160])
            self.initialize('CPUExecutionProvider')
        progress(self.status)

    def initialize(self, provider):
        sessions = []
        for filename in ('yolox.onnx', 'osnet_x0_5.onnx'):
            options = ort.SessionOptions()
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.intra_op_num_threads = 4
            options.enable_profiling = self.profile
            if self.profile:
                options.profile_file_prefix = str(ROOT / 'test-artifacts' / ('appearance_' + filename))
            sessions.append(ort.InferenceSession(str(self.directory / filename), sess_options=options,
                            providers=[provider, 'CPUExecutionProvider'] if provider != 'CPUExecutionProvider' else [provider]))
        self.detector, self.reid = sessions
        self.detector_input = self.detector.get_inputs()[0].name
        self.reid_input = self.reid.get_inputs()[0].name
        self.detector.run(None, {self.detector_input: np.zeros((1, 3, 640, 640), np.float32)})
        self.reid.run(None, {self.reid_input: np.zeros((1, 3, 256, 128), np.float32)})
        self.provider = 'DirectML GPU' if all('DmlExecutionProvider' in s.get_providers() for s in sessions) else 'CPU'
        self.status = f'YOLOX + OSNet x0.5 · {self.provider}'

    def people(self, image):
        try:
            return self._people(image)
        except ort.capi.onnxruntime_pybind11_state.Fail:
            if self.provider == 'CPU':
                raise
            self.progress('外观模型 GPU 推理失败，切换 CPU 并重试。')
            self.initialize('CPUExecutionProvider')
            return self._people(image)

    def _people(self, image):
        height, width = image.shape[:2]
        scale = min(640 / height, 640 / width)
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        canvas = np.full((640, 640, 3), 114, dtype=np.float32)
        resized = cv2.resize(rgb, (max(1, int(width * scale)), max(1, int(height * scale))))
        canvas[:resized.shape[0], :resized.shape[1]] = resized
        blob = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None])
        output = self.detector.run(None, {self.detector_input: blob})[0][0].copy()
        # OpenCV Zoo YOLOX uses raw RGB 0..255 and undecoded grid outputs.
        centers = (output[:, :2] + self.grid) * self.strides
        sizes = np.exp(output[:, 2:4]) * self.strides
        scores = output[:, 4] * output[:, 5]  # COCO class 0 = person
        boxes = np.concatenate((centers - sizes / 2, sizes), axis=1) / scale
        keep = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), .45, .5)
        result = []
        for index in np.asarray(keep).reshape(-1):
            x, y, w, h = boxes[index]
            x1, y1, x2, y2 = max(0, int(x)), max(0, int(y)), min(width, int(x + w)), min(height, int(y + h))
            if x2 - x1 < 24 or y2 - y1 < 64:
                continue
            crop = Image.fromarray(rgb[y1:y2, x1:x2]).resize((128, 256), Image.Resampling.BILINEAR)
            data = np.asarray(crop, dtype=np.float32) / 255
            data = (data - np.array([.485, .456, .406], dtype=np.float32)) / np.array([.229, .224, .225], dtype=np.float32)
            blob = np.ascontiguousarray(data.transpose(2, 0, 1)[None])
            feature = self.reid.run(None, {self.reid_input: blob})[0].reshape(-1)
            norm = np.linalg.norm(feature)
            if len(feature) == 512 and np.isfinite(feature).all() and norm > 1e-8:
                result.append({'bbox': [x1, y1, x2, y2], 'appearance': (feature / norm).tolist(), 'confidence': float(scores[index])})
        return result

    def end_profiles(self):
        return [s.end_profiling() for s in (self.detector, self.reid)]
