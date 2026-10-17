"""PyInstaller entry point for ByteLogCollector.exe (the headless collector)."""

from bytelog.collector.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
