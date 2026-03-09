"""Top-level script entry point — used by PyInstaller and direct execution."""

from huesync.app import HueSyncApp

if __name__ == "__main__":
    HueSyncApp().run()
