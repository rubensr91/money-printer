"""
Face Tracker — detects faces in a video segment and produces a smooth
x-center trajectory for dynamic cropping in panoramic format.

v2: Uses OpenCV DNN CUDA backend (10-20x faster than Haar Cascade).
    Falls back to Haar Cascade if CUDA/DNN unavailable.
    Model: opencv_face_detector_uint8.pb (TensorFlow, ~2.7MB)
"""

import os
import shutil
import tempfile
import cv2
import numpy as np

_FACE_DETECTOR = None  # ("dnn", net) | ("haar", cascade) | None
_MODEL_DIR = None


def _model_paths():
    """Candidate locations for the DNN model and haar cascade."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    mp = os.path.join(root, ".mp")
    return {
        "dnn_proto": os.path.join(mp, "opencv_face_detector.pbtxt"),
        "dnn_model": os.path.join(mp, "opencv_face_detector_uint8.pb"),
        "haar_candidates": [
            os.path.join(mp, "haarcascade_frontalface_default.xml"),
            os.path.join(root, "assets", "haarcascade_frontalface_default.xml"),
            os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"),
        ],
    }


def _load_haar_ascii(src):
    """OpenCV cannot read Unicode paths on Windows; copy XML to an ASCII temp path."""
    try:
        tmp_dir = tempfile.gettempdir()
        dst = os.path.join(tmp_dir, "haarcascade_frontalface_default.xml")
        shutil.copyfile(src, dst)
        c = cv2.CascadeClassifier(dst)
        return c if not c.empty() else None
    except Exception:
        return None


def _init_detector():
    """Initialize the face detector. Prefers DNN+CUDA, falls back to Haar Cascade."""
    global _FACE_DETECTOR

    paths = _model_paths()

    # Try OpenCV DNN face detector (TF model, CUDA backend if available)
    if os.path.exists(paths["dnn_proto"]) and os.path.exists(paths["dnn_model"]):
        try:
            net = cv2.dnn.readNetFromTensorflow(paths["dnn_model"], paths["dnn_proto"])
            # Prefer CUDA backend
            if cv2.cuda.getCudaEnabledDeviceCount() > 0:
                net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
                net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
            else:
                net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            _FACE_DETECTOR = ("dnn", net)
            return True
        except Exception:
            pass  # Fall through to Haar Cascade

    # Fallback: Haar Cascade
    for p in paths["haar_candidates"]:
        if os.path.exists(p):
            try:
                c = _load_haar_ascii(p)
                if c is not None:
                    _FACE_DETECTOR = ("haar", c)
                    return True
            except Exception:
                continue

    return False


def track_faces(video_path, clip_start, clip_end, sample_every=0.5):
    """Sample frames and return list of (t, x_center_normalized).
    x_center_normalized in [0,1] relative to frame width.
    Returns [] if no faces detected."""
    if _FACE_DETECTOR is None:
        if not _init_detector():
            return []

    det_type, detector = _FACE_DETECTOR

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920
    height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080

    start_frame = int(clip_start * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    positions = []
    t = clip_start
    frame_idx = start_frame

    while t < clip_end:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
        ok, frame = cap.read()
        if not ok:
            break

        faces = _detect_faces(frame, width, height, det_type, detector)
        if faces:
            # Use largest face
            largest = max(faces, key=lambda f: f[2] * f[3])
            x, y, w, h = largest
            center = (x + w / 2.0) / width
            positions.append((t, center))

        t += sample_every
        frame_idx += sample_every * fps

    cap.release()
    return positions


def _detect_faces(frame, width, height, det_type, detector):
    """Detect faces in a frame. Returns list of (x, y, w, h)."""
    if det_type == "dnn":
        # DNN expects 300x300 input
        blob = cv2.dnn.blobFromImage(frame, 1.0, (300, 300),
                                      [104, 117, 123], False, False)
        detector.setInput(blob)
        detections = detector.forward()
        faces = []
        for i in range(detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            if confidence > 0.5:
                x1 = int(detections[0, 0, i, 3] * width)
                y1 = int(detections[0, 0, i, 4] * height)
                x2 = int(detections[0, 0, i, 5] * width)
                y2 = int(detections[0, 0, i, 6] * height)
                faces.append((x1, y1, x2 - x1, y2 - y1))
        return faces
    else:
        # Haar Cascade
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector.detectMultiScale(gray, scaleFactor=1.1,
                                           minNeighbors=5, minSize=(60, 60))
        return [(f[0], f[1], f[2], f[3]) for f in faces]


def smooth_trajectory(positions, clip_duration, steps=100):
    """Interpolate sparse (t, x) samples into a smooth dense trajectory.
    Returns list of (t, x) of length `steps`. Handles empty input -> centered."""
    if not positions:
        return [(t / steps * clip_duration, 0.5) for t in range(steps + 1)]

    times = np.array([p[0] for p in positions])
    xs = np.array([p[1] for p in positions])

    t_grid = np.linspace(0, clip_duration, steps + 1)
    x_smooth = np.interp(t_grid, times, xs)
    kernel = np.ones(5) / 5
    x_smooth = np.convolve(x_smooth, kernel, mode="same")
    x_smooth = np.clip(x_smooth, 0.05, 0.95)
    return [(float(t_grid[i]), float(x_smooth[i])) for i in range(len(t_grid))]
