"""Put the original faces back onto an AI-edited photo, so people's faces are the real ones.

Image-editing models redraw faces even when told not to. After the edit we find each face in the
original photo, find the same person's face in the edited photo, line the original up with it using
the eye, nose and mouth points, and lay the original face pixels over it with softly faded edges.
Faces are only put back where the head is still in the same pose, so features always line up.
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


# SFace face recogniser (Apache 2.0, from opencv_zoo): tells whose face is whose, so faces are put back
# on the right person even when people move or new people are added. 0.363 is its "same person" cut-off.
_RECOGNISER = str(Path(__file__).resolve().parent / "models" / "face_recognition_sface_2021dec.onnx")
_SAME_PERSON = 0.30  # a little looser than 0.363: costumes, hats and new lighting lower the score


def _identity_pairs(sources: list[tuple[np.ndarray, np.ndarray]], out: np.ndarray, targets: list[np.ndarray]):
    """Pair each (image, face) source with the edited face of the same person, most similar first.
    Returns {source index: target index}."""
    rec = cv2.FaceRecognizerSF.create(_RECOGNISER, "")
    embed = lambda img, f: rec.feature(rec.alignCrop(img, f))  # noqa: E731
    src = [embed(img, f) for img, f in sources]
    dst = [embed(out, t) for t in targets]
    scores = sorted(((rec.match(a, b, cv2.FaceRecognizerSF_FR_COSINE), i, j)
                     for i, a in enumerate(src) for j, b in enumerate(dst)), reverse=True)
    pairs, used = {}, set()
    for score, i, j in scores:
        if score < _SAME_PERSON:
            break
        if i not in pairs and j not in used:
            pairs[i] = j
            used.add(j)
    return pairs



_MAX_MISALIGN = 0.08  # worst landmark error as a share of face width; above this the head has turned
_MAX_TURN = 0.12  # change in how far the nose sits off-centre between the eyes (head turned left/right)


def _turn(face) -> float:
    """How far the head is turned: the nose's offset from between the eyes, as a share of eye distance."""
    (rx, _), (lx, _), (nx, _) = face[4:6], face[6:8], face[8:10]
    return (nx - (rx + lx) / 2) / max(abs(lx - rx), 1.0)


def _paste(src: np.ndarray, face: np.ndarray, out: np.ndarray, target: np.ndarray) -> np.ndarray | None:
    """Lay src's real face over out's target face; None if the poses don't line up."""
    h, w = out.shape[:2]
    # Line the original face up with the new one using the eye, nose and mouth points
    # (move, scale and rotate only, so the face itself is never stretched).
    pts, dst = face[4:14].reshape(5, 2), target[4:14].reshape(5, 2)
    m, _ = cv2.estimateAffinePartial2D(pts, dst, method=cv2.LMEDS)
    if m is None:
        return None
    moved = pts @ m[:, :2].T + m[:, 2]
    tx, ty, tw, th = target[:4]
    if (np.linalg.norm(moved - dst, axis=1).max() > _MAX_MISALIGN * tw
            or abs(_turn(face) - _turn(target)) > _MAX_TURN):
        return None  # the head is at a different angle now: pasting would look wrong
    warped = cv2.warpAffine(src, m, (w, h), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT)
    # Brows to chin, with a soft fade at the edge so new hats, hair and collars around it are kept.
    mask = np.zeros((h, w), np.float32)
    cv2.ellipse(mask, (int(tx + tw / 2), int(ty + th * 0.55)), (int(tw * 0.40), int(th * 0.44)),
                0, 0, 360, 1.0, -1)
    if not mask.any():
        return None
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(1.0, tw * 0.06))
    # Only a gentle brightness/colour match, in case the new scene is lit differently; features are untouched.
    core = mask > 0.9
    if core.any():
        gain = out[core].mean(axis=0) / np.maximum(warped[core].mean(axis=0), 1)
        warped = np.clip(warped * np.clip(gain, 0.92, 1.08), 0, 255)
    a = mask[..., None]
    return (warped * a + out * (1 - a)).astype(np.uint8)


def _bgr(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def _main_face(img: np.ndarray):
    """The biggest face in a photo of one person, or None."""
    found = _detect(img)
    return max(found, key=lambda f: f[2] * f[3]) if found else None


def has_face(img: Image.Image) -> bool:
    """Whether a photo has a face we can find (for checking reference photos on upload)."""
    return _main_face(_bgr(img)) is not None


def keep_faces(original: Image.Image, edited: bytes, people_added: bool = False) -> tuple[bytes, int]:
    """Return (JPEG bytes of the edited photo with original faces put back, number of faces put back).

    Only faces from the original photo are used. Faces are matched by who they are, and also by
    position unless `people_added` (then everyone may have shuffled along to make room)."""
    orig = _bgr(original)
    h, w = orig.shape[:2]
    out = cv2.imdecode(np.frombuffer(edited, np.uint8), cv2.IMREAD_COLOR)
    if out is None:
        return edited, 0
    out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LANCZOS4)  # same frame as the original
    targets = _detect(out)
    sources = [(orig, f) for f in _detect(orig)]
    if not sources or not targets:
        return edited, 0

    try:
        pairs = _identity_pairs(sources, out, targets)
    except cv2.error:
        pairs = {}
    if not people_added:  # anyone not recognised is still where they were
        used = set(pairs.values())
        for i, (_, face) in enumerate(sources):
            if i not in pairs:
                t = _match(face, [c for j, c in enumerate(targets) if j not in used])
                if t is not None:
                    j = next(j for j, c in enumerate(targets) if c is t)
                    pairs[i] = j
                    used.add(j)

    restored = 0
    for i, j in pairs.items():
        src, face = sources[i]
        blended = _paste(src, face, out, targets[j])
        if blended is not None:
            out, restored = blended, restored + 1
    if not restored:
        return edited, 0  # nothing changed: keep the model's picture as it came
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return (buf.tobytes() if ok else edited), restored
