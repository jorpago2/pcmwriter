from __future__ import annotations

import sys
from pathlib import Path

from pumpauto.ui import PumpAutoUI, launch


if __name__ == "__main__":
    config = Path(sys.executable).with_name("config.json") if getattr(sys, "frozen", False) else Path("config.json")
    if "--smoke-test" in sys.argv:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        app = PumpAutoUI(root, config)
        root.update_idletasks()
        assert app.app_icon.width() == 512
        root.destroy()
    else:
        launch(config)
