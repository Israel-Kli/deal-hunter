from deal_hunter.dates import earliest_yyyy_mm_dd, parse_dd_mm_yyyy, parse_yyyy_mm_dd


def test_parse_yyyy_mm_dd():
    assert parse_yyyy_mm_dd("2024-07-02").isoformat() == "2024-07-02"
    assert parse_yyyy_mm_dd("2024-07-02T12:00:00Z").isoformat() == "2024-07-02"
    assert parse_yyyy_mm_dd("") is None
    assert parse_yyyy_mm_dd("nope") is None


def test_parse_dd_mm_yyyy():
    assert parse_dd_mm_yyyy("02/07/2024").isoformat() == "2024-07-02"
    assert parse_dd_mm_yyyy("2/7/2024").isoformat() == "2024-07-02"
    assert parse_dd_mm_yyyy("") is None


def test_earliest_yyyy_mm_dd():
    assert earliest_yyyy_mm_dd("2024-08-01", "2024-07-02", "2025-01-01") == "2024-07-02"
    assert earliest_yyyy_mm_dd("", "2024-01-01") == "2024-01-01"
    assert earliest_yyyy_mm_dd("", "") == ""
