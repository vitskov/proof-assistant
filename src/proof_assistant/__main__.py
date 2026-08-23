"""Module entry point used by detached verification workers."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
