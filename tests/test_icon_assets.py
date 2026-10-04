"""图标资源测试：文件齐全、尺寸正确、圆角透明、边缘无棋盘格残留。

需要 Pillow（仅测试用）；未安装时整个测试类跳过，核心测试保持零依赖。
"""

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # QIcon 需要 QGuiApplication

try:
    import numpy as np
    from PIL import Image

    HAVE_PILLOW = True
except ImportError:  # pragma: no cover - 取决于运行环境
    HAVE_PILLOW = False

ICON_DIR = Path(__file__).resolve().parent.parent / "sharingan" / "assets" / "icons"
# 入库的必需档位（大档位 512/1024 与 .icns 按需生成，见 README 图标规范）
PNG_SIZES = (16, 32, 48, 64, 128, 256)
OPTIONAL_LARGE = ("sharingan-512.png", "sharingan-1024.png", "sharingan.icns")


@unittest.skipUnless(HAVE_PILLOW, "未安装 Pillow")
class IconAssetTest(unittest.TestCase):
    def test_required_files_exist(self):
        for size in PNG_SIZES:
            self.assertTrue((ICON_DIR / f"sharingan-{size}.png").exists(), f"{size}px")
        ico = ICON_DIR / "sharingan.ico"
        self.assertTrue(ico.exists(), "缺少 Windows .ico")
        self.assertGreater(ico.stat().st_size, 1024)

    def test_optional_large_assets_are_valid_if_present(self):
        """大档位不入库，但本地生成过就要保证是合格文件。"""
        for name in OPTIONAL_LARGE:
            path = ICON_DIR / name
            if not path.exists():
                continue
            if name.endswith(".icns"):
                self.assertGreater(path.stat().st_size, 1024, name)
                continue
            with Image.open(path) as image:
                size = int(name.split("-")[1].split(".")[0])
                self.assertEqual(image.size, (size, size), name)
                self.assertEqual(image.mode, "RGBA", name)

    def test_pngs_square_rgba_with_transparent_corners(self):
        sizes = list(PNG_SIZES) + [
            int(name.split("-")[1].split(".")[0])
            for name in OPTIONAL_LARGE
            if name.endswith(".png") and (ICON_DIR / name).exists()
        ]
        for size in sizes:
            with Image.open(ICON_DIR / f"sharingan-{size}.png") as image:
                self.assertEqual(image.size, (size, size))
                self.assertEqual(image.mode, "RGBA")
                alpha = image.getchannel("A")
                self.assertEqual(alpha.getpixel((0, 0)), 0, f"{size}px 左上角应透明")
                self.assertEqual(
                    alpha.getpixel((size - 1, size - 1)), 0, f"{size}px 右下角应透明"
                )
                self.assertEqual(
                    alpha.getpixel((size // 2, 0)), 255, f"{size}px 上边中点应不透明"
                )

    def test_border_has_no_checkerboard_leftover(self):
        """边缘一圈的相邻像素跳变必须很小：残留棋盘格会让它飙升到 20 以上。"""
        with Image.open(ICON_DIR / "sharingan-256.png") as image:
            array = np.asarray(image)
        alpha = array[..., 3].astype(int)
        gray = array[..., :3].astype(float) @ np.array([0.299, 0.587, 0.114])

        size = alpha.shape[0]
        band = max(2, size // 32)
        ring = np.zeros_like(alpha, dtype=bool)
        ring[:band, :] = ring[-band:, :] = ring[:, :band] = ring[:, -band:] = True
        opaque = ring & (alpha > 200)
        self.assertGreater(int(opaque.sum()), 100, "边缘不透明像素太少，图标可能被裁太小")

        horizontal = np.abs(np.diff(gray, axis=1))
        vertical = np.abs(np.diff(gray, axis=0))
        mask_h = opaque[:, :-1] & opaque[:, 1:]
        mask_v = opaque[:-1, :] & opaque[1:, :]
        edge_diff = np.concatenate([horizontal[mask_h], vertical[mask_v]])
        self.assertGreater(edge_diff.size, 50)
        self.assertLess(
            float(np.median(edge_diff)),
            12.0,
            "图标边缘出现明显的高频纹理，可能残留了源图的棋盘格背景",
        )

    def test_ico_contains_multiple_sizes(self):
        with Image.open(ICON_DIR / "sharingan.ico") as image:
            sizes = image.info.get("sizes") or {image.size}
        self.assertIn((256, 256), set(sizes), f"ICO 应包含 256×256，实际 {sizes}")
        self.assertGreaterEqual(len(set(sizes)), 4, f"ICO 尺寸档位太少：{sizes}")

    def test_ui_module_exposes_multi_size_icon(self):
        from sharingan.ui import UI_AVAILABLE

        if not UI_AVAILABLE:
            self.skipTest("未安装 PySide6")
        from PySide6.QtWidgets import QApplication

        from sharingan.ui import app as ui_app

        self.application = QApplication.instance() or QApplication([])
        icon = ui_app.app_icon()
        self.assertIsNotNone(icon, "未能装配应用图标")
        self.assertFalse(icon.isNull())
        available = {size.width() for size in icon.availableSizes()}
        self.assertTrue({16, 256} <= available, f"图标缺少小尺寸或大尺寸：{available}")


if __name__ == "__main__":
    unittest.main()
