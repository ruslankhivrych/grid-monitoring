import math
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from PIL import Image
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from charts import chart_series, render_chart


def test_chart_lines_break_for_missing_samples_and_long_gaps():
    rows = [(1000, 100, 500, 1), (1300, None, None, 1), (5000, 70, 800, 0)]
    dates, values = chart_series(rows, 1, 900)
    assert len(dates) == 4
    assert values[0] == 100
    assert math.isnan(values[1])
    assert math.isnan(values[2])
    assert values[3] == 70


@pytest.mark.parametrize('period,days', [('day', 1), ('week', 7), ('month', 30)])
def test_chart_is_valid_png_with_single_point_and_missing_power(period, days):
    end = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    rows = [(int(end.timestamp()) - 300, 100, None, 1)]
    image = render_chart(rows, end - timedelta(days=days), end, ZoneInfo('Europe/Kyiv'), period)
    with image:
        assert image.read(8) == b'\x89PNG\r\n\x1a\n'
        image.seek(0)
        with Image.open(image) as png:
            assert png.size == (1540, 980)
            png.verify()
