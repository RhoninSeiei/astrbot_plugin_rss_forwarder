import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from cron_schedule import next_fire, parse_cron


class CronCalendarTests(unittest.TestCase):
    def test_leap_year_and_unsatisfiable_date(self):
        base = datetime(2028, 3, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.assertEqual(next_fire('0 8 29 2 *', base).isoformat(), '2032-02-29T08:00:00+08:00')
        with self.assertRaises(ValueError):
            parse_cron('0 8 31 2 *')

    def test_sunday_alias_and_both_day_fields_required(self):
        base = datetime(2026, 11, 1, tzinfo=ZoneInfo('UTC'))
        self.assertEqual(parse_cron('0 8 * * 7'), parse_cron('0 8 * * 0'))
        self.assertEqual(next_fire('15 8-22/2 15 * 7', base).isoformat(), '2026-11-15T08:15:00+00:00')

    def test_dst_skips_nonexistent_time(self):
        zone = ZoneInfo('America/New_York')
        self.assertEqual(next_fire('30 2 * * *', datetime(2026, 3, 8, tzinfo=zone)).isoformat(), '2026-03-09T02:30:00-04:00')
