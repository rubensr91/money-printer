"""
Face Tracker — detects faces in a video segment and produces a smooth
x-center trajectory for dynamic cropping in panoramic format.
Uses OpenCV haar cascade (lightweight, no downloads). Falls back to
center (no movement) when no faces are found.
"""

import os
import shutil
import tempfile
import cv2
import numpy as np

_FACE_CASCADE = None


def _cascade_paths():
    """Candidate locations for the haar cascade XML (source copies)."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    return [
        os.path.join(root, ".mp", "haarcascade_frontalface_default.xml"),
        os.path.join(root, "assets", "haarcascade_frontalface_default.xml"),
        os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"),
    ]


def _load_cascade_ascii(src):
    """OpenCV cannot read Unicode paths on Windows; copy XML to an ASCII temp path."""
    try:
        tmp_dir = tempfile.gettempdir()
        dst = os.path.join(tmp_dir, "haarcascade_frontalface_default.xml")
        shutil.copyfile(src, dst)
        c = cv2.CascadeClassifier(dst)
        return c if not c.empty() else None
    except Exception:
        return None


def _get_cascade():
    global _FACE_CASCADE
    if _FACE_CASCADE is None:
        for p in _cascade_paths():
            if os.path.exists(p):
                try:
                    c = _load_cascade_ascii(p)
                    if c is not None:
                        _FACE_CASCADE = c
                        return _FACE_CASCADE
                except Exception:
                    continue
        _FACE_CASCADE = None
    return _FACE_CASCADE


def track_faces(video_path, clip_start, clip_end, sample_every=0.5):
    """Sample frames of the clip and return list of (t, x_center_normalized).
    x_center_normalized in [0,1] relative to frame width.
    Returns [] if no faces detected anywhere."""
    cascade = _get_cascade()
    if cascade is None:
        return []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920

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
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        if len(faces) > 0:
            # Largest face
            largest = max(faces, key=lambda f: f[2] * f[3])
            x, y, w, h = largest
            center = (x + w / 2.0) / width
            positions.append((t, center))
        t += sample_every
        frame_idx += sample_every * fps

    cap.release()
    return positions


def smooth_trajectory(positions, clip_duration, steps=100):
    """Interpolate sparse (t, x) samples into a smooth dense trajectory.
    Returns list of (t, x) of length `steps`. Handles empty input → centered."""
    if not positions:
        return [(t / steps * clip_duration, 0.5) for t in range(steps + 1)]

    times = np.array([p[0] for p in positions])
    xs = np.array([p[1] for p in positions])

    # Clamp to clip duration
    t_grid = np.linspace(0, clip_duration, steps + 1)
    x_smooth = np.interp(t_grid, times, xs)
    # Light smoothing (moving average)
    kernel = np.ones(5) / 5
    x_smooth = np.convolve(x_smooth, kernel, mode="same")
    x_smooth = np.clip(x_smooth, 0.05, 0.95)  # keep face inside frame
    return [(float(t_grid[i]), float(x_smooth[i])) for i in range(len(t_grid))]
