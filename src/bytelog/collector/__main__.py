"""``python -m bytelog.collector`` / ``ByteLogCollector.exe``."""

from __future__ import annotations

import argparse
import logging
import sys
import uuid

from bytelog import __version__


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bytelog-collector", description="ByteLog background data usage collector"
    )
    parser.add_argument("--data-dir", help="use this data directory instead of the default")
    parser.add_argument("--once", action="store_true", help="sample once and exit")
    parser.add_argument("--stop", action="store_true", help="ask a running collector to stop")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)

    from bytelog.logging_setup import setup_logging
    from bytelog.paths import set_data_dir_override

    set_data_dir_override(args.data_dir)
    log = setup_logging("collector", logging.DEBUG if args.debug else logging.INFO)

    from bytelog.collector import service
    from bytelog.platform import get_platform
    from bytelog.storage import settings
    from bytelog.storage.db import open_db

    platform = get_platform()

    if args.stop:
        conn = open_db()
        try:
            settings.set_value(conn, "stop_requested", uuid.uuid4().hex)
        finally:
            conn.close()
        platform.signal_stop()
        log.info("stop requested")
        return 0

    lock = platform.acquire_collector_lock()
    if lock is None:
        log.info("another collector is already running; exiting")
        return 0

    service.install_crash_handlers()
    log.info("ByteLog collector %s starting", __version__)
    try:
        conn = open_db()
    except Exception:
        log.exception("could not open the database")
        lock.release()
        return 1
    try:
        return service.run(conn, platform, once=args.once, lock=lock)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
