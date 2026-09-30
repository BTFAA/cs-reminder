#!/usr/bin/env python3
"""CS2 赛事提醒入口。用法：python run.py [--dry-run|--test|--check|--force]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from csreminder.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
