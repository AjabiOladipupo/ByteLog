"""PyInstaller entry point for ByteLog.exe (the dashboard)."""

from bytelog.ui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
