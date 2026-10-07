from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def is_valid_timezone(value: str) -> bool:
    """Whether `value` is a real IANA zone name ZoneInfo can load."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True
