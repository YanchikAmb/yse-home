# -*- coding: utf-8 -*-
"""Метки времени в логах стенда (по умолчанию Europe/Moscow)."""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo


def log_timestamp(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    tz_name = os.environ.get("STAND_TZ", os.environ.get("TZ", "Europe/Moscow"))
    return datetime.now(ZoneInfo(tz_name)).strftime(fmt)