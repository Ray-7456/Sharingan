"""从源图生成 Sharingan 的全套应用图标。

背景说明：源图是 JPG，四周的"透明格"是画上去的棋盘格像素（JPG 不支持透明），
而且图标外面还有一圈很宽的发光晕——光晕与棋盘格混叠，导致任何单一判据（只看
颜色、只看纹理）都会误判。本工具的做法是：

1. **径向测量几何**——图标本体是红色调，从中心沿八个方向往外找红色的最远边界，
   得到方形半边长与对角长度，再解出圆角半径（圆角矩形满足
   ``对角 = √2·半边 − R·(√2−1)``）；
2. **自校验裁切**——沿边缘往里缩，直到边缘一圈的"亮度纹理"降到阈值以下（棋盘格
   是高频交替纹理，图标本体是平滑的），从而干净地切掉光晕混合带；
3. **预乘 alpha 缩放**——避免缩放时把背景颜色混进半透明边缘。

用法：

    python tools/make_icons.py --source assets/icon-source.jpg --out sharingan/assets/icons

产出：

- ``sharingan-1024.png`` 主图（RGBA，圆角外透明）
- ``sharingan-{512,256,128,64,48,32,16}.png``
- ``sharingan.ico``（Windows，多尺寸）
- ``sharingan.icns``（macOS）或 ``iconset/`` 目录（用 macOS 自带 iconutil 转换）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

MASTER_SIZE = 1024
# 默认入库的档位：界面与常规打包够用；512/1024 与 .icns 体积大，改为 --full 按需生成
PNG_SIZES = (16, 32, 48, 64, 128, 256)
FULL_PNG_SIZES = (512, 1024)
ICO_SIZES = ((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256))
ICNS_SIZES = ((16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024))
ICONSET_NAMES = {
    16: "icon_16x16.png",
    32: "icon_16x32.png",
    64: "icon_32x32.png",
    128: "icon_128x128.png",
    256: "icon_256x256.png",
    512: "icon_512x512.png",
    1024: "icon_512x512@2x.png",
}

REDNESS_THRESHOLD = 15.0
CLEAN_TEXTURE = 8.0  # 边缘亮度纹理低于此值，认为已切进图标本体
TRIM_CANDIDATES = (0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.12, 0.14)
RADIUS_RATIO_RANGE = (0.18, 0.35)


def _box_mean(values: np.ndarray, window: int) -> np.ndarray:
    """滑动窗口均值（积分图实现，O(HW)，不依赖 scipy）。"""
    height, width = values.shape
    integral = np.pad(np.cumsum(np.cumsum(values, axis=0), axis=1), ((1, 0), (1, 0)))
    rows = np.arange(height)
    cols = np.arange(width)
    top = np.clip(rows - window // 2, 0, height)
    bottom = np.clip(rows + window // 2 + 1, 0, height)
    left = np.clip(cols - window // 2, 0, width)
    right = np.clip(cols + window // 2 + 1, 0, width)
    total = (
        integral[np.ix_(bottom, right)]
        - integral[np.ix_(top, right)]
        - integral[np.ix_(bottom, left)]
        + integral[np.ix_(top, left)]
    )
    counts = (bottom - top)[:, None] * (right - left)[None, :]
    return total / counts


def chroma(rgb: np.ndarray) -> np.ndarray:
    """红减绿：图标为红色调，中性灰背景接近 0。"""
    return rgb[..., 0].astype(np.float64) - rgb[..., 1].astype(np.float64)


def _radial_extent(
    chroma_map: np.ndarray, center: tuple[float, float], angle_deg: float, *, run: int = 4
) -> float | None:
    """沿某方向找红色调的最远边界（要求连续 run 个采样点均为红，抗噪）。"""
    height, width = chroma_map.shape
    center_y, center_x = center
    angle = np.deg2rad(angle_deg)
    step_x, step_y = np.cos(angle), -np.sin(angle)
    streak = 0
    furthest: float | None = None
    for distance in range(1, int(max(height, width))):
        y = int(round(center_y + step_y * distance))
        x = int(round(center_x + step_x * distance))
        if not (0 <= y < height and 0 <= x < width):
            break
        if chroma_map[y, x] > REDNESS_THRESHOLD:
            streak += 1
            if streak >= run:
                furthest = distance
        else:
            streak = 0
    return furthest


def measure_geometry(
    rgb: np.ndarray, *, debug: bool = False
) -> tuple[tuple[float, float], float, float]:
    """量出图标中心、半边长与圆角半径（源图像素）。"""
    chroma_map = chroma(rgb)
    strong = chroma_map > 30
    if strong.sum() < 1000:
        raise ValueError("源图中没有找到明显的红色调图标，请检查 --source")
    rows, cols = np.where(strong)
    centroid = (float(rows.mean()), float(cols.mean()))

    # 从加权质心出发测四个轴向的边界，再由左右/上下边界反推真正的几何中心
    # （质心会被更亮的一侧光晕带偏，直接用它会裁偏心）
    extents: dict[int, float] = {}
    for angle in (0, 90, 180, 270, 45, 135, 225, 315):
        value = _radial_extent(chroma_map, centroid, angle)
        if value is not None:
            extents[angle] = value
    missing = [a for a in (0, 90, 180, 270) if a not in extents]
    if missing:
        raise ValueError(f"无法测得图标轮廓（缺方向 {missing}），请检查源图背景是否为中性色")

    left = centroid[1] - extents[180]
    right = centroid[1] + extents[0]
    top = centroid[0] - extents[90]
    bottom = centroid[0] + extents[270]
    center = ((top + bottom) / 2, (left + right) / 2)
    half_width = (right - left) / 2
    half_height = (bottom - top) / 2
    half_side = (half_width + half_height) / 2

    # 用修正后的中心重测对角边界，再解圆角半径
    diagonal = [
        value
        for angle in (45, 135, 225, 315)
        if (value := _radial_extent(chroma_map, center, angle)) is not None
    ]
    if not diagonal:
        raise ValueError("无法测得对角轮廓，请检查源图背景是否为中性色")

    diagonal_radius = float(np.median(diagonal))
    radius = (np.sqrt(2) * half_side - diagonal_radius) / (np.sqrt(2) - 1)
    side = 2 * half_side
    low, high = (ratio * side for ratio in RADIUS_RATIO_RANGE)
    if not low <= radius <= high:
        clamped = min(max(radius, low), high)
        if debug:
            print(f"  圆角半径 {radius:.0f}px 超出合理区间，钳制为 {clamped:.0f}px")
        radius = clamped
    if debug:
        print(f"  加权质心=({centroid[1]:.0f}, {centroid[0]:.0f}) 轴向边界={extents}")
        print(f"  几何中心=({center[1]:.0f}, {center[0]:.0f})")
        print(
            f"  半边长={half_side:.0f}（边长 {side:.0f}）"
            f"圆角={radius:.0f}（{radius / side:.1%}）"
        )
    return center, half_side, radius


def _box_from_center(
    center: tuple[float, float], half_side: float, shape: tuple[int, int]
) -> tuple[int, int, int, int]:
    height, width = shape
    center_y, center_x = center
    return (
        max(int(round(center_x - half_side)), 0),
        max(int(round(center_y - half_side)), 0),
        min(int(round(center_x + half_side)), width - 1),
        min(int(round(center_y + half_side)), height - 1),
    )


def border_texture(master: Image.Image, *, band_ratio: float = 0.03) -> float:
    """边缘一圈的亮度纹理强度：残留棋盘格很高，纯图标本体很低。"""
    array = np.asarray(master)
    alpha = array[..., 3].astype(int)
    gray = array[..., :3].astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    mean = _box_mean(gray, 9)
    std = np.sqrt(np.clip(_box_mean(gray * gray, 9) - mean * mean, 0, None))

    size = alpha.shape[0]
    band = max(2, int(size * band_ratio))
    ring = np.zeros_like(alpha, dtype=bool)
    ring[:band, :] = ring[-band:, :] = ring[:, :band] = ring[:, -band:] = True
    opaque = ring & (alpha > 200)
    if opaque.sum() == 0:
        return 255.0
    return float(np.median(std[opaque]))


def choose_cut(
    rgb: np.ndarray,
    center: tuple[float, float],
    half_side: float,
    radius: float,
    *,
    debug: bool = False,
) -> tuple[tuple[int, int, int, int], float, float]:
    """向内收缩直到边缘不再是棋盘格纹理；返回 (裁切框, 圆角比例, 边缘纹理)。"""
    side = 2 * half_side
    best: tuple[tuple[int, int, int, int], float, float] | None = None
    if debug:
        print(f"  裁切搜索（边缘纹理越低越干净，阈值 {CLEAN_TEXTURE:.0f}）：")
    for trim in TRIM_CANDIDATES:
        trimmed_half = half_side - trim * side
        if trimmed_half <= 8:
            break
        box = _box_from_center(center, trimmed_half, rgb.shape[:2])
        radius_ratio = min(radius / (2 * trimmed_half), RADIUS_RATIO_RANGE[1])
        score = border_texture(build_master(rgb, box, radius_ratio, size=256))
        if debug:
            print(f"    裁掉 {trim:4.1%} → 圆角 {radius_ratio:.0%} 边缘纹理 {score:5.1f}")
        if best is None or score < best[2]:
            best = (box, radius_ratio, score)
        if score <= CLEAN_TEXTURE:
            return box, radius_ratio, score
    assert best is not None
    print(
        f"  警告：收缩到 {TRIM_CANDIDATES[-1]:.0%} 边缘纹理仍有 {best[2]:.1f}"
        "（源图光晕过宽），已取最干净的一组；建议换一张背景更干净的源图"
    )
    return best


def rounded_mask(size: int, radius: float, supersample: int = 4) -> Image.Image:
    """抗锯齿的圆角矩形遮罩（L 模式）。

    缩放会带来轻微振铃（角上残留 1-3 级 alpha），这里把极低值清零，
    保证每一档尺寸的圆角外都是真正的全透明。
    """
    big = size * supersample
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=radius * supersample, fill=255
    )
    resized = mask.resize((size, size), Image.LANCZOS)
    values = np.asarray(resized).astype(np.int16)
    values[values < 8] = 0
    return Image.fromarray(values.astype(np.uint8), "L")


def build_master(
    rgb: np.ndarray,
    box: tuple[int, int, int, int],
    radius_ratio: float,
    *,
    size: int = MASTER_SIZE,
) -> Image.Image:
    """裁切 → 补成正方形 → 圆角遮罩抠透明 → 预乘 alpha 缩放。"""
    x0, y0, x1, y1 = box
    crop = rgb[y0 : y1 + 1, x0 : x1 + 1].astype(np.float64)
    height, width = crop.shape[:2]
    side = max(height, width)
    square = np.zeros((side, side, 3), dtype=np.float64)
    offset_y, offset_x = (side - height) // 2, (side - width) // 2
    square[offset_y : offset_y + height, offset_x : offset_x + width] = crop

    alpha = np.asarray(rounded_mask(side, radius_ratio * side)).astype(np.float64)
    weight = alpha[..., None] / 255.0
    premultiplied = np.clip(square * weight, 0, 255).astype(np.uint8)

    resized_rgb = np.asarray(
        Image.fromarray(premultiplied, "RGB").resize((size, size), Image.LANCZOS)
    ).astype(np.float64)
    resized_alpha = np.asarray(
        Image.fromarray(alpha.astype(np.uint8), "L").resize((size, size), Image.LANCZOS)
    ).astype(np.float64)

    weights = resized_alpha[..., None] / 255.0
    with np.errstate(invalid="ignore", divide="ignore"):
        straight = np.where(weights > 0, resized_rgb / np.maximum(weights, 1e-6), 0.0)
    master = Image.fromarray(np.clip(straight, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    master.putalpha(Image.fromarray(resized_alpha.astype(np.uint8), "L"))
    return master


def render_size(master: Image.Image, size: int, radius_ratio: float) -> Image.Image:
    """缩放到指定尺寸后**重新套一次圆角遮罩**。

    直接从大图缩放会产生 alpha 溢色（小尺寸的角上会留下 1-2 级不透明），
    逐尺寸重新打遮罩可以保证每一档的角都是干净透明的。
    """
    resized = master.resize((size, size), Image.LANCZOS).convert("RGBA")
    resized.putalpha(rounded_mask(size, radius_ratio * size))
    return resized


def export(
    master: Image.Image,
    out_dir: Path,
    radius_ratio: float,
    *,
    full: bool = False,
    debug: bool = False,
) -> list[Path]:
    """导出图标资源。

    ``full=False``（默认）只写 16–256 六档 PNG 与 Windows ``.ico``——界面和常规
    打包够用，仓库也轻；``full=True`` 额外写 512/1024 与 macOS ``.icns``（macOS
    打包、商店素材等场景按需生成，不入库）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    sizes = list(PNG_SIZES) + (list(FULL_PNG_SIZES) if full else [])
    for size in sizes:
        path = out_dir / f"sharingan-{size}.png"
        render_size(master, size, radius_ratio).save(path, "PNG", optimize=True)
        written.append(path)

    ico_images = [render_size(master, size, radius_ratio) for size, _ in ICO_SIZES]
    ico_path = out_dir / "sharingan.ico"
    try:
        ico_images[-1].save(
            ico_path,
            format="ICO",
            append_images=ico_images[:-1],
            sizes=list(ICO_SIZES),
        )
    except (TypeError, ValueError):  # 老版本 Pillow 不支持 append_images
        master.save(ico_path, format="ICO", sizes=list(ICO_SIZES))
    written.append(ico_path)

    if not full:
        return written

    icns_path = out_dir / "sharingan.icns"
    try:
        master.save(icns_path, format="ICNS", sizes=list(ICNS_SIZES))
        written.append(icns_path)
    except Exception as exc:  # Pillow 不支持写 ICNS 时退化为 iconset 目录
        if debug:
            print(f"  提示：Pillow 无法直接写 ICNS（{exc}），改为输出 iconset/ 目录")
        iconset = out_dir / "iconset"
        iconset.mkdir(exist_ok=True)
        for size, name in ICONSET_NAMES.items():
            render_size(master, size, radius_ratio).save(iconset / name, "PNG", optimize=True)
        written.append(iconset)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Sharingan 应用图标")
    parser.add_argument("--source", required=True, help="源图（JPG/PNG）")
    parser.add_argument(
        "--out",
        default="sharingan/assets/icons",
        help="输出目录（默认 sharingan/assets/icons）",
    )
    parser.add_argument("--debug", action="store_true", help="打印测量过程")
    parser.add_argument(
        "--full",
        action="store_true",
        help="额外生成 512/1024 PNG 与 macOS .icns（体积较大，默认不生成）",
    )
    args = parser.parse_args(argv)

    source = Image.open(args.source).convert("RGB")
    rgb = np.asarray(source).astype(np.float64)
    if args.debug:
        print(f"源图：{args.source} 尺寸={source.size}")

    center, half_side, radius = measure_geometry(rgb, debug=args.debug)
    box, radius_ratio, score = choose_cut(rgb, center, half_side, radius, debug=args.debug)
    if args.debug:
        print(f"  选定裁切框={box} 圆角={radius_ratio:.0%} 边缘纹理={score:.1f}")

    master = build_master(rgb, box, radius_ratio)
    written = export(master, Path(args.out), radius_ratio, full=args.full, debug=args.debug)

    print(f"已生成 {len(written)} 项：")
    for path in written:
        if path.is_dir():
            print(f"  {path}/（{len(list(path.iterdir()))} 个文件）")
        else:
            print(f"  {path}  {path.stat().st_size / 1024:.1f} KB")
    if not args.full:
        print("提示：如需 512/1024 PNG 与 macOS .icns（打包 App 用），加 --full 重新生成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
