#!/usr/bin/env python
"""Run Django administrative tasks."""

import os
import sys


def main():
    """Run the command-line utility."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django could not be imported. Install the project requirements."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
