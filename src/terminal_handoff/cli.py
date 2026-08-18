"""Command-line entry point for Terminal Handoff.

Subcommands: statusline, evaluate, launch, build-command, install, uninstall,
coverage, status, reset-circuit, version.
"""

import sys

from terminal_handoff.core import main

__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
