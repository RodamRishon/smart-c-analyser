#!/usr/bin/env python3
"""
Convenience entry point.

Usage:
    python3 analyse.py tests/sample_valid.c
    python3 analyse.py tests/sample_errors.c

(Equivalent to `python3 -m smartc.cli <file>` run from this directory.)
"""

import sys
from smartc.cli import main

if __name__ == "__main__":
    sys.exit(main())
