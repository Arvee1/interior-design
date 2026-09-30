"""Put the original faces back onto an AI-edited photo, so people still look like themselves.

Image-editing models redraw faces even when told not to. After the edit we find each face in the
original photo, find the same person's face in the edited photo, line the original up with it using
the eye, nose and mouth points, and blend it in with Poisson (seamless) cloning so skin tone and
lighting match the new picture.
"""

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

try:  # silence OpenCV's backend notices in the server log
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except AttributeError:
    pass

# YuNet face detector (MIT licence, from opencv_zoo). Finds faces plus eye, nose and mouth points.
_MODEL = str(Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx")


def _detect(bgr: np.ndarray) -> list[np.ndarray]:
    """Faces as rows of [x, y, w, h, 5 landmark (x, y) pairs, score]."""
    h, w = bgr.shape[:2]
    detector = cv2.FaceDetectorYN.create(_MODEL, "", (w, h), score_threshold=0.75)
    _, faces = detector.detect(bgr)
    min_side = min(h, w) * 0.03
    return [f for f in (faces if faces is not None else []) if f[2] >= min_side]


def _match(face, candidates):
    """The edited face nearest to where this person's face was, if it's close and a similar size."""
    x, y, w, h = face[:4]
    cx, cy = x + w / 2, y + h / 2
    best, best_d = None, 0.6 * w
    for c in candidates:
        d = np.hypot(c[0] + c[2] / 2 - cx, c[1] + c[3] / 2 - cy)
        if d < best_d and 0.7 < c[2] / w < 1.45:
            best, best_d = c, d
    return best


def keep_faces(original: Image.Image, edited: bytes) -> tuple[bytes, int]:
    """Return (JPEG bytes of the edited photo with original faces restored, number of faces restored)."""
    orig = cv2.cvtColor(np.array(original.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = orig.shape[:2]
    out = cv2.imdecode(np.frombuffer(edited, np.uint8), cv2.IMREAD_COLOR)
    if out is None:
        return edited, 0
    out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LANCZOS4)  # same frame as the original
    targets = _detect(out)
    restored = 0
    for face in _detect(orig):
        target = _match(face, targets)
        if target is None:  # the person moved or the face wasn't found; leave the model's version
            continue
        # Line the original face up with the new one using the eye, nose and mouth points.
        m, _ = cv2.estimateAffinePartial2D(face[4:14].reshape(5, 2), target[4:14].reshape(5, 2))
        if m is None:
            continue
        warped = cv2.warpAffine(orig, m, (w, h), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT)
        # Inner face only (brows to chin), so new hats, hair and collars around it are kept.
        tx, ty, tw, th = (int(v) for v in target[:4])
        mask = np.zeros((h, w), np.uint8)
        cv2.ellipse(mask, (tx + tw // 2, ty + int(th * 0.56)), (int(tw * 0.40), int(th * 0.43)), 0, 0, 360, 255, -1)
        mask[[0, -1], :] = mask[:, [0, -1]] = 0  # seamlessClone needs a margin at the photo's edge
        ys, xs = np.nonzero(mask)
        if not len(xs):
            continue
        centre = ((xs.min() + xs.max()) // 2, (ys.min() + ys.max()) // 2)
        try:
            out = cv2.seamlessClone(warped, out, mask, centre, cv2.NORMAL_CLONE)
            restored += 1
        except cv2.error:  # face too close to the edge of the photo to blend
            continue
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return (buf.tobytes() if ok else edited), restored
