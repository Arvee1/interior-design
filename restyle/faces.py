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



def _paste(src: np.ndarray, face: np.ndarray, out: np.ndarray, target: np.ndarray) -> np.ndarray | None:
    """Blend src's face onto out's target face; None if it can't be done."""
    h, w = out.shape[:2]
    # Line the original face up with the new one using the eye, nose and mouth points.
    m, _ = cv2.estimateAffinePartial2D(face[4:14].reshape(5, 2), target[4:14].reshape(5, 2))
    if m is None:
        return None
    warped = cv2.warpAffine(src, m, (w, h), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT)
    # Inner face only (brows to chin), so new hats, hair and collars around it are kept.
    tx, ty, tw, th = (int(v) for v in target[:4])
    mask = np.zeros((h, w), np.uint8)
    cv2.ellipse(mask, (tx + tw // 2, ty + int(th * 0.56)), (int(tw * 0.40), int(th * 0.43)), 0, 0, 360, 255, -1)
    mask[[0, -1], :] = mask[:, [0, -1]] = 0  # seamlessClone needs a margin at the photo's edge
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    centre = ((xs.min() + xs.max()) // 2, (ys.min() + ys.max()) // 2)
    try:
        return cv2.seamlessClone(warped, out, mask, centre, cv2.NORMAL_CLONE)
    except cv2.error:  # face too close to the edge of the photo to blend
        return None


def _bgr(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def _main_face(img: np.ndarray):
    """The biggest face in a photo of one person, or None."""
    found = _detect(img)
    return max(found, key=lambda f: f[2] * f[3]) if found else None


def _frontal(face) -> bool:
    """Roughly facing the camera: the nose sits near the middle of the eyes."""
    (rx, _), (lx, _), (nx, _) = face[4:6], face[6:8], face[8:10]
    eyes = abs(lx - rx)
    return eyes > 0 and abs(nx - (rx + lx) / 2) / eyes < 0.2


def has_face(img: Image.Image) -> bool:
    """Whether a photo has a face we can find (for checking reference photos on upload)."""
    return _main_face(_bgr(img)) is not None


def keep_faces(original: Image.Image, edited: bytes, extras: list[Image.Image] | None = None,
               references: list[Image.Image] | None = None) -> tuple[bytes, int]:
    """Return (JPEG bytes of the edited photo with original faces restored, number of faces restored).

    `extras` are photos of people who were added into the picture (one person each); their faces are
    put back too. Faces are matched by who they are, falling back to position when no one was added.
    `references` are close-ups of people already in the photo. They're used for anyone whose face in
    the original couldn't be used (side-on, hidden, too small), but only where the new picture shows
    that person roughly facing the camera: a front-on face pasted onto a side view would look wrong."""
    orig = _bgr(original)
    h, w = orig.shape[:2]
    out = cv2.imdecode(np.frombuffer(edited, np.uint8), cv2.IMREAD_COLOR)
    if out is None:
        return edited, 0
    out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LANCZOS4)  # same frame as the original
    targets = _detect(out)
    sources = [(orig, f) for f in _detect(orig)]
    for extra in extras or []:
        img = _bgr(extra)
        face = _main_face(img)
        if face is not None:
            sources.append((img, face))
    refs = []
    for ref in references or []:
        img = _bgr(ref)
        face = _main_face(img)
        if face is not None:
            refs.append((img, face))
    if not (sources or refs) or not targets:
        return edited, 0

    try:
        pairs = _identity_pairs(sources, out, targets) if sources else {}
    except cv2.error:
        pairs = {}
    if not extras:  # no one added, so anyone not recognised is still where they were
        used = set(pairs.values())
        for i, (_, face) in enumerate(sources):
            if i not in pairs:
                t = _match(face, [c for j, c in enumerate(targets) if j not in used])
                if t is not None:
                    j = next(j for j, c in enumerate(targets) if c is t)
                    pairs[i] = j
                    used.add(j)

    # Reference close-ups fill in for people still without a face, where they now face the camera.
    free = [j for j in range(len(targets)) if j not in pairs.values() and _frontal(targets[j])]
    if refs and free:
        try:
            ref_pairs = _identity_pairs(refs, out, [targets[j] for j in free])
        except cv2.error:
            ref_pairs = {}
        for i, k in ref_pairs.items():
            pairs[len(sources) + i] = free[k]
    sources = sources + refs

    restored = 0
    for i, j in pairs.items():
        src, face = sources[i]
        blended = _paste(src, face, out, targets[j])
        if blended is not None:
            out, restored = blended, restored + 1
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return (buf.tobytes() if ok else edited), restored
