"""Kiểm thử app/timeutil.py::format_dmy (filter Jinja `dmy` mới, đổi hiển thị datetime
sang dd/mm/yyyy HH:MM trên webUI). Đây là hàm PURE format - KHÔNG được đổi giá trị/
timezone, chỉ đổi cách trình bày chuỗi. Test riêng biệt với test convert GMT+7<->UTC đã
có sẵn (gmt7_to_utc/utc_to_gmt7 test nằm rải rác ở test_create_task_flow.py /
test_router_integration.py)."""

from datetime import datetime

import pytest

from app.timeutil import format_dmy


# ---------------------------------------------------------------------------
# None/falsy input -> None (KHÔNG phải '-', template tự quyết fallback bằng `or '-'`)
# ---------------------------------------------------------------------------


def test_format_dmy_none_returns_none():
    assert format_dmy(None) is None


# ---------------------------------------------------------------------------
# Zero-padding cho ngày/tháng/giờ/phút 1 chữ số
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dt,expected",
    [
        (datetime(2026, 1, 1, 0, 0), "01/01/2026 00:00"),
        (datetime(2026, 8, 1, 3, 0), "01/08/2026 03:00"),
        (datetime(2026, 12, 31, 23, 59), "31/12/2026 23:59"),
        (datetime(2028, 2, 29, 9, 5), "29/02/2028 09:05"),  # nam nhuan
        (datetime(1, 1, 1, 0, 0), "01/01/0001 00:00"),  # datetime.min - edge nam nho
        (datetime(9999, 12, 31, 23, 59), "31/12/9999 23:59"),  # datetime.max - edge nam lon
    ],
)
def test_format_dmy_pads_and_orders_dd_mm_yyyy(dt, expected):
    assert format_dmy(dt) == expected


def test_format_dmy_ignores_seconds_microseconds():
    """Field hien thi chi can phut, giay/microgiay (neu co) phai bi bo qua, khong
    lam sai dinh dang hoac lam tron sai."""
    dt = datetime(2026, 7, 29, 14, 30, 59, 999999)
    assert format_dmy(dt) == "29/07/2026 14:30"


def test_format_dmy_does_not_shift_value_pure_formatting_only():
    """format_dmy KHONG duoc tu doi timezone - neu can GMT+7 phai qua filter `gmt7`
    truoc (dang ky rieng trong tasks_router/actions_router), format_dmy chi format chuoi
    tren gia tri dau vao y nguyen."""
    dt = datetime(2026, 8, 1, 3, 0, 0)
    assert format_dmy(dt) == "01/08/2026 03:00"
    # Doi chieu: khong bang gio GMT+7 tuong ung (03:00 UTC -> 10:00 GMT+7) neu khong
    # qua filter gmt7 truoc.
    assert format_dmy(dt) != "01/08/2026 10:00"
