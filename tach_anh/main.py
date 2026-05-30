from __future__ import annotations

from pathlib import Path
from typing import Iterable, Tuple

import cv2
import numpy as np


SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


# ============================================================
# FILE IO
# ============================================================

def imread_unicode(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {path}")
    return img


def imwrite_unicode(path: Path, img: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError(f"Cannot write image: {path}")
    encoded.tofile(str(path))


# ============================================================
# IMAGE NORMALIZATION
# ============================================================

def normalize_bgra(img: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    if img.ndim == 2:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        alpha = np.full(img.shape, 255, dtype=np.uint8)
        return bgr, alpha

    if img.shape[2] == 3:
        bgr = img
        alpha = np.full(img.shape[:2], 255, dtype=np.uint8)
        return bgr, alpha

    if img.shape[2] == 4:
        bgr = img[:, :, :3]
        alpha = img[:, :, 3]
        return bgr, alpha

    raise ValueError("Unsupported image channel format.")


# ============================================================
# GREEN DETECTION - #00FF00 FAMILY
# ============================================================

def build_green_family_masks(
    bgr: np.ndarray,
    alpha: np.ndarray,
    aggressive: bool = True,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Bắt họ màu xanh liên quan gần #00FF00.

    Trả về:
    - core_green:
        Mask chính, dùng để xóa thẳng alpha = 0.
        Bắt nền xanh kiểu Grok hay tạo ra: #09F705, #0AF706, #1EF112...

    - spill_green:
        Mask mềm hơn, bắt viền xanh / xanh bị anti-alias / xanh bị nén.
        Mask này chỉ được lan từ core_green, giúp không ăn nhầm màu khác.

    - green_score:
        Điểm giống xanh #00FF00 từ 0..1, dùng để giảm ám xanh viền.
    """
    b, g, r = cv2.split(bgr)

    b_i = b.astype(np.int16)
    g_i = g.astype(np.int16)
    r_i = r.astype(np.int16)

    b_f = b.astype(np.float32)
    g_f = g.astype(np.float32)
    r_f = r.astype(np.float32)

    visible = alpha > 0

    # Khoảng cách RGB tới BGR(0,255,0), tức HEX #00FF00.
    distance_to_00ff00 = np.sqrt(
        (b_f - 0.0) ** 2 +
        (g_f - 255.0) ** 2 +
        (r_f - 0.0) ** 2
    )

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)

    # OpenCV HSV:
    # #00FF00 có H xấp xỉ 60.
    # Vùng 34..96 bắt xanh lá, xanh neon, xanh bị lệch do AI/nén ảnh.
    green_hue_core = (h >= 34) & (h <= 96)
    green_hue_spill = (h >= 32) & (h <= 100)

    # G phải trội hơn R/B để tránh ăn nhầm vàng, trắng, xám, da, cam.
    green_dominance = g_i - np.maximum(r_i, b_i)

    if aggressive:
        core_distance = 145
        spill_distance = 185
        min_core_sat = 32
        min_core_value = 50
        min_core_green = 65
        min_core_dom = 12
        max_core_rb = 180

        min_spill_sat = 10
        min_spill_value = 35
        min_spill_green = 45
        min_spill_dom = 5
        max_spill_rb = 220
    else:
        core_distance = 115
        spill_distance = 155
        min_core_sat = 45
        min_core_value = 70
        min_core_green = 90
        min_core_dom = 22
        max_core_rb = 155

        min_spill_sat = 18
        min_spill_value = 45
        min_spill_green = 60
        min_spill_dom = 8
        max_spill_rb = 190

    near_target_core = (
        (distance_to_00ff00 <= core_distance) &
        (g >= min_core_green) &
        (green_dominance >= min_core_dom)
    )

    hsv_core = (
        green_hue_core &
        (s >= min_core_sat) &
        (v >= min_core_value) &
        (g >= min_core_green) &
        (green_dominance >= min_core_dom) &
        (r <= max_core_rb) &
        (b <= max_core_rb)
    )

    core_green = (near_target_core | hsv_core) & visible

    near_target_spill = (
        (distance_to_00ff00 <= spill_distance) &
        (g >= min_spill_green) &
        (green_dominance >= min_spill_dom)
    )

    hsv_spill = (
        green_hue_spill &
        (s >= min_spill_sat) &
        (v >= min_spill_value) &
        (g >= min_spill_green) &
        (green_dominance >= min_spill_dom) &
        (r <= max_spill_rb) &
        (b <= max_spill_rb)
    )

    spill_green = (near_target_spill | hsv_spill) & visible

    # Score giống xanh #00FF00.
    distance_score = np.clip(1.0 - distance_to_00ff00 / 210.0, 0.0, 1.0)
    hue_score = np.clip(1.0 - np.abs(h.astype(np.float32) - 60.0) / 40.0, 0.0, 1.0)
    sat_score = np.clip((s.astype(np.float32) - 8.0) / 130.0, 0.0, 1.0)
    dom_score = np.clip((green_dominance.astype(np.float32) - 3.0) / 90.0, 0.0, 1.0)
    value_score = np.clip((v.astype(np.float32) - 30.0) / 180.0, 0.0, 1.0)

    green_score = np.maximum(
        distance_score,
        hue_score * sat_score * dom_score * value_score,
    )

    green_score[~visible] = 0.0

    return (
        core_green.astype(np.uint8) * 255,
        spill_green.astype(np.uint8) * 255,
        green_score.astype(np.float32),
    )


# ============================================================
# MASK UTILS
# ============================================================

def clean_mask(mask: np.ndarray, close_iter: int = 1) -> np.ndarray:
    kernel = np.ones((3, 3), dtype=np.uint8)
    out = mask.copy()

    if close_iter > 0:
        out = cv2.morphologyEx(out, cv2.MORPH_CLOSE, kernel, iterations=close_iter)

    return out


def remove_tiny_components(mask: np.ndarray, min_area: int = 2) -> np.ndarray:
    if min_area <= 1:
        return mask

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8),
        connectivity=8,
    )

    out = np.zeros_like(mask)

    for label_id in range(1, num_labels):
        area = int(stats[label_id, cv2.CC_STAT_AREA])
        if area >= min_area:
            out[labels == label_id] = 255

    return out


def flood_from_edges(mask: np.ndarray) -> np.ndarray:
    """
    Chỉ lấy vùng xanh nối với mép ảnh.
    Dùng cho mode='background' nếu bạn không muốn xóa chi tiết xanh nằm trong artwork.
    """
    h, w = mask.shape

    padded = cv2.copyMakeBorder(
        mask,
        1,
        1,
        1,
        1,
        borderType=cv2.BORDER_CONSTANT,
        value=255,
    )

    flood = padded.copy()
    ff_mask = np.zeros((h + 4, w + 4), dtype=np.uint8)

    cv2.floodFill(
        flood,
        ff_mask,
        seedPoint=(0, 0),
        newVal=128,
    )

    return (flood[1:-1, 1:-1] == 128).astype(np.uint8) * 255


def grow_mask_into_allowed(
    seed_mask: np.ndarray,
    allowed_mask: np.ndarray,
    steps: int = 12,
) -> np.ndarray:
    """
    Lan vùng xóa từ core_green sang spill_green.
    Cách này xóa được viền xanh, xanh bị anti-alias,
    nhưng không tự tiện ăn sang vùng không liên quan.
    """
    kernel = np.ones((3, 3), dtype=np.uint8)
    grown = ((seed_mask > 0) & (allowed_mask > 0)).astype(np.uint8) * 255
    allowed = (allowed_mask > 0).astype(np.uint8) * 255

    for _ in range(steps):
        expanded = cv2.dilate(grown, kernel, iterations=1)
        expanded = cv2.bitwise_and(expanded, allowed)

        if np.array_equal(expanded, grown):
            break

        grown = expanded

    return grown


# ============================================================
# DESPILL / PREVIEW
# ============================================================

def despill_green_edges(
    bgr: np.ndarray,
    alpha: np.ndarray,
    green_score: np.ndarray,
    strength: float = 0.9,
    edge_iterations: int = 2,
) -> np.ndarray:
    """
    Giảm ám xanh còn dính ở rìa vật thể.
    Không đổi alpha, chỉ giảm kênh G ở vùng sát nền trong suốt.
    """
    if strength <= 0:
        return bgr

    out = bgr.copy()

    kernel = np.ones((3, 3), dtype=np.uint8)

    transparent = (alpha == 0).astype(np.uint8) * 255
    near_edge = cv2.dilate(transparent, kernel, iterations=edge_iterations)
    near_edge = (near_edge > 0) & (alpha > 0) & (green_score > 0.02)

    if not np.any(near_edge):
        return out

    b, g, r = cv2.split(out)

    b_f = b.astype(np.float32)
    g_f = g.astype(np.float32)
    r_f = r.astype(np.float32)

    target_g = np.maximum(r_f, b_f)
    factor = np.clip(green_score * strength, 0.0, 1.0)

    g_new = g_f * (1.0 - factor) + target_g * factor
    g_f[near_edge] = g_new[near_edge]

    out[:, :, 1] = np.clip(g_f, 0, 255).astype(np.uint8)
    return out


def save_preview_on_checker(output_path: Path, result_bgra: np.ndarray, cell: int = 24) -> Path:
    bgr = result_bgra[:, :, :3]
    alpha = result_bgra[:, :, 3].astype(np.float32) / 255.0

    h, w = alpha.shape
    yy, xx = np.indices((h, w))
    checker = ((xx // cell + yy // cell) % 2).astype(np.uint8)

    bg = np.zeros_like(bgr)
    bg[checker == 0] = (205, 205, 205)
    bg[checker == 1] = (245, 245, 245)

    comp = (
        bgr.astype(np.float32) * alpha[:, :, None]
        + bg.astype(np.float32) * (1.0 - alpha[:, :, None])
    )
    comp = np.clip(comp, 0, 255).astype(np.uint8)

    preview_path = output_path.with_name(f"{output_path.stem}_preview_checker.png")
    imwrite_unicode(preview_path, comp)
    return preview_path


# ============================================================
# MAIN REMOVE FUNCTION
# ============================================================

def remove_green_00ff00_smart(
    input_path: Path,
    output_path: Path,
    mode: str = "all",
    aggressive: bool = True,
    save_preview: bool = True,
    debug: bool = False,
    despill: bool = True,
) -> Path:
    """
    mode='all':
        Xóa tất cả pixel xanh liên quan gần #00FF00 ở mọi nơi.
        Phù hợp nhất với ảnh AI dùng nền xanh để tách nền.

    mode='background':
        Chỉ xóa vùng xanh nối với mép ảnh.
        Dùng khi artwork có chi tiết xanh muốn giữ lại ở bên trong.
    """
    if mode not in {"all", "background"}:
        raise ValueError("mode must be 'all' or 'background'.")

    img = imread_unicode(input_path)
    bgr, original_alpha = normalize_bgra(img)

    core_green, spill_green, green_score = build_green_family_masks(
        bgr=bgr,
        alpha=original_alpha,
        aggressive=aggressive,
    )

    core_green = clean_mask(core_green, close_iter=1)
    core_green = remove_tiny_components(core_green, min_area=2)

    if mode == "background":
        seed_mask = flood_from_edges(core_green)
    else:
        seed_mask = core_green

    # Lan từ core green sang xanh mềm / xanh viền / xanh bị anti-alias.
    grow_steps = 16 if aggressive else 9
    remove_mask = grow_mask_into_allowed(
        seed_mask=seed_mask,
        allowed_mask=spill_green,
        steps=grow_steps,
    )

    # Với mode='all', cộng lại core để không bỏ sót các đốm xanh rời.
    if mode == "all":
        remove_mask = cv2.bitwise_or(remove_mask, core_green)

    remove_mask = clean_mask(remove_mask, close_iter=1)
    remove_mask = remove_tiny_components(remove_mask, min_area=2)

    final_alpha = original_alpha.copy()
    final_alpha[remove_mask > 0] = 0

    final_bgr = bgr
    if despill:
        final_bgr = despill_green_edges(
            bgr=bgr,
            alpha=final_alpha,
            green_score=green_score,
            strength=0.95 if aggressive else 0.75,
            edge_iterations=2,
        )

    result = cv2.cvtColor(final_bgr, cv2.COLOR_BGR2BGRA)
    result[:, :, 3] = final_alpha

    imwrite_unicode(output_path, result)

    if save_preview:
        save_preview_on_checker(output_path, result)

    if debug:
        debug_dir = output_path.parent / f"{output_path.stem}_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)

        imwrite_unicode(debug_dir / "01_core_green.png", core_green)
        imwrite_unicode(debug_dir / "02_spill_green.png", spill_green)
        imwrite_unicode(debug_dir / "03_seed_mask.png", seed_mask)
        imwrite_unicode(debug_dir / "04_remove_mask.png", remove_mask)
        imwrite_unicode(debug_dir / "05_alpha.png", final_alpha)
        imwrite_unicode(debug_dir / "06_result.png", result)

    return output_path


# ============================================================
# BATCH INPUT / OUTPUT
# ============================================================

def iter_images(input_path: Path) -> Iterable[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() in SUPPORTED_EXTS:
            yield input_path
        return

    if input_path.is_dir():
        for path in sorted(input_path.rglob("*")):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
                yield path
        return

    raise FileNotFoundError(f"Input path not found: {input_path}")


def make_output_path(image_path: Path, input_root: Path, output_dir: Path) -> Path:
    if input_root.is_dir():
        relative = image_path.relative_to(input_root)
        return output_dir / relative.with_name(f"{relative.stem}_transparent.png")

    return output_dir / f"{image_path.stem}_transparent.png"


# ============================================================
# AUTO RUN CONFIG - KHÔNG CẦN TRUYỀN THAM SỐ
# ============================================================

INPUT_PATH = Path("input")
OUTPUT_DIR = Path("output")

# 'all' = xóa tất cả pixel xanh liên quan gần #00FF00.
# 'background' = chỉ xóa xanh nối với mép ảnh.
MODE = "all"

# True = xóa mạnh hơn cho ảnh AI/Grok vì màu xanh hay lệch khỏi #00FF00.
AGGRESSIVE = True

SAVE_PREVIEW = True
DEBUG = False
DESPILL = True


# ============================================================
# MAIN AUTO RUN
# ============================================================

def main() -> None:
    input_path = INPUT_PATH
    output_dir = OUTPUT_DIR

    if not input_path.exists():
        input_path.mkdir(parents=True, exist_ok=True)
        print(f"Created input folder: {input_path.resolve()}")
        print("Put your image files into this folder, then run the script again.")
        return

    images = list(iter_images(input_path))

    if not images:
        print(f"No supported images found in: {input_path.resolve()}")
        print(f"Supported formats: {', '.join(sorted(SUPPORTED_EXTS))}")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Input : {input_path.resolve()}")
    print(f"Output: {output_dir.resolve()}")
    print(f"Found {len(images)} image(s).")
    print(f"Mode  : {MODE}")
    print("Target: #00FF00 and nearby related green shades")

    for i, image_path in enumerate(images, start=1):
        output_path = make_output_path(
            image_path=image_path,
            input_root=input_path,
            output_dir=output_dir,
        )

        try:
            saved = remove_green_00ff00_smart(
                input_path=image_path,
                output_path=output_path,
                mode=MODE,
                aggressive=AGGRESSIVE,
                save_preview=SAVE_PREVIEW,
                debug=DEBUG,
                despill=DESPILL,
            )

            print(f"[{i}/{len(images)}] Done: {saved}")

            if SAVE_PREVIEW:
                preview_path = saved.with_name(f"{saved.stem}_preview_checker.png")
                print(f"           Preview: {preview_path}")

        except Exception as e:
            print(f"[{i}/{len(images)}] Failed: {image_path}")
            print(f"Reason: {e}")

    print("All done.")


if __name__ == "__main__":
    main()
