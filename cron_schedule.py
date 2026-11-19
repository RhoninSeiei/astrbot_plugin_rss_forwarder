"""Numeric five-field cron with conjunctive day and weekday matching."""
import calendar
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


@lru_cache(maxsize=256)
def parse_cron(value: str) -> tuple[tuple[int, ...], ...]:
    fields = value.split()
    if len(fields) != 5:
        raise ValueError("cron must contain five valid numeric fields")
    parsed = []
    for field, (minimum, maximum) in zip(fields, _CRON_RANGES):
        values = set()
        for part in field.split(","):
            match = re.fullmatch(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?", part)
            if match is None:
                raise ValueError("invalid cron field")
            base, step_text = match.groups()
            step = int(step_text) if step_text is not None else 1
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                start, end = map(int, base.split("-"))
            else:
                start = end = int(base)
            if not minimum <= start <= end <= maximum or step <= 0:
                raise ValueError("cron field is out of range")
            values.update(range(start, end + 1, step))
        if maximum == 7:
            values = {0 if item == 7 else item for item in values}
        parsed.append(tuple(sorted(values)))
    if not any(day <= calendar.monthrange(2000, month)[1] for month in parsed[3] for day in parsed[2]):
        raise ValueError("cron has no valid calendar date")
    return tuple(parsed)


def next_fire(cron: str, base: datetime) -> datetime:
    """Skip nonexistent local times; fire only once per repeated wall-clock minute."""
    if base.tzinfo is None:
        raise ValueError("next_fire requires a timezone-aware datetime")
    minutes, hours, days, months, weekdays = parse_cron(cron)
    date = base.date()
    while True:
        if date.month in months and date.day in days and (date.weekday() + 1) % 7 in weekdays:
            for hour in hours:
                if date == base.date() and hour < base.hour:
                    continue
                for minute in minutes:
                    if date == base.date() and (hour, minute) <= (base.hour, base.minute):
                        continue
                    result = datetime(date.year, date.month, date.day, hour, minute, tzinfo=base.tzinfo)
                    utc = result.astimezone(timezone.utc)
                    if utc.timestamp() > base.timestamp() and utc.astimezone(base.tzinfo) == result:
                        return result
        date += timedelta(days=1)
