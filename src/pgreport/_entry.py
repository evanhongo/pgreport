"""Thin entry point that catches KeyboardInterrupt during import."""

import sys


def main():
    try:
        from .pgreport_cli import main as _main

        _main()
    except KeyboardInterrupt:
        sys.exit(130)
