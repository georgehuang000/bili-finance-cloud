"""Report date checks make no database or model requests."""
import unittest
from datetime import datetime, timezone
from report_schedule import report_date


class ReportDateTests(unittest.TestCase):
    def test_utc_evening_is_next_beijing_day(self):
        self.assertEqual(report_date(datetime(2026, 10, 13, 16, 1, tzinfo=timezone.utc)), "2026-10-14")

    def test_both_scheduled_slots_share_beijing_calendar_day(self):
        for hour in (0, 10):
            self.assertEqual(report_date(datetime(2026, 10, 14, hour, tzinfo=timezone.utc)), "2026-10-14")
