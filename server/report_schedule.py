"""Report dates always follow the product's Beijing timezone."""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def report_date(now=None):
    now = now if now is not None else datetime.now(BEIJING)
    return now.astimezone(BEIJING).strftime("%Y-%m-%d")
