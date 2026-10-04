"""图形界面层（可选依赖 PySide6，见 docs/design.md §7）。"""

from .app import UI_AVAILABLE, UI_IMPORT_ERROR, main

__all__ = ["UI_AVAILABLE", "UI_IMPORT_ERROR", "main"]
