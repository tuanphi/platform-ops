"""Kiểm thử thuật toán JS window.parseDmyDatetime / window.formatDmyDatetime định nghĩa
trong app/templates/base.html (dòng ~295-323), dùng ở task_form.html (confirmed_run_at) và
task_list.html (modal Auto deploy #auto-deploy-time) TRƯỚC khi submit.

Môi trường test không có trình duyệt/engine JS thật (`node` không có sẵn trên máy chạy
test - đã kiểm tra bằng `which node`), nên các hàm dưới đây PORT SANG PYTHON đúng nguyên
văn thuật toán JS (đọc trực tiếp từ base.html, không "sửa cho đẹp"):

  parseDmyDatetime(text):
    1. text phải là string, .trim() trước khi match.
    2. regex bắt buộc: ^(\\d{2})/(\\d{2})/(\\d{4})\\s+(\\d{2}):(\\d{2})$ (ĐÚNG 2 chữ số
       ngày/tháng/giờ/phút, ĐÚNG 4 chữ số năm - không tự thêm số 0, không chấp nhận thiếu).
    3. month phải 1..12, hour 0..23, minute 0..59 (kiểm tra RIÊNG, trước khi biết day có
       hợp lệ hay không).
    4. day phải nằm trong khoảng 1..daysInMonth, với daysInMonth = new Date(year, month,
       0).getDate() - đây là cách JS tính số ngày trong tháng CHÍNH XÁC theo lịch (bao gồm
       năm nhuận đúng chuẩn Gregorian: chia hết 4 trừ chia hết 100 trừ khi chia hết 400) -
       tương đương calendar.monthrange(year, month)[1] của Python cho MỌI năm >= 100 (ứng
       dụng chỉ dùng năm thực tế >= 2020 nên phạm vi này đủ để port an toàn). LƯU Ý: JS
       Date constructor có 1 quirk lịch sử KHÔNG port ở đây - nếu year nằm trong khoảng
       0-99 thì `new Date(year, ...)` tự động hiểu thành 1900+year (theo đặc tả ECMA-262),
       điều này có thể làm daysInMonth lệch so với lịch Gregorian thuần cho các năm 4 chữ số
       dạng "00xx" (vd "0000", "0099"). Không thể verify quirk này bằng cách thực thi thật
       (không có node/browser trong môi trường test) nên KHÔNG đưa vào bộ test tự động, chỉ
       ghi chú ở đây để tránh kết luận sai - trên thực tế input năm 2 chữ số kiểu này không
       phát sinh trong nghiệp vụ deploy (task deploy luôn dùng năm hiện tại/tương lai gần).
    5. return null nếu bất kỳ bước nào sai, ngược lại trả về "yyyy-mm-ddThh:mm" (zero-pad).

  formatDmyDatetime(iso): chiều ngược - match ^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2}),
    trả về "dd/mm/yyyy hh:mm", hoặc '' nếu không match/không phải string.
"""

import calendar
import re

import pytest

_PARSE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})$")
_FORMAT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})")


def parse_dmy_datetime(text):
    """Port trung thuc window.parseDmyDatetime (base.html dong 295-314)."""
    if not isinstance(text, str):
        return None
    m = _PARSE_RE.match(text.strip())
    if not m:
        return None
    day, month, year, hour, minute = (int(g) for g in m.groups())
    if month < 1 or month > 12:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    # new Date(year, month, 0).getDate() - tuong duong monthrange cho nam >= 100.
    days_in_month = calendar.monthrange(year, month)[1]
    if day < 1 or day > days_in_month:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}"


def format_dmy_datetime(iso):
    """Port trung thuc window.formatDmyDatetime (base.html dong 316-323)."""
    if not isinstance(iso, str):
        return ""
    m = _FORMAT_RE.match(iso.strip())
    if not m:
        return ""
    year, month, day, hour, minute = m.groups()
    return f"{day}/{month}/{year} {hour}:{minute}"


# ---------------------------------------------------------------------------
# Bang edge case parseDmyDatetime theo dung yeu cau cua task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Hop le co ban
        ("29/07/2026 14:30", "2026-07-29T14:30"),
        ("01/01/2026 00:00", "2026-01-01T00:00"),
        ("31/12/2026 23:59", "2026-12-31T23:59"),
        # Nam nhuan / khong nhuan
        ("29/02/2028 09:05", "2028-02-29T09:05"),  # 2028 nhuan (chia het 4, khong chia het 100)
        ("31/02/2026 10:00", None),  # thang 2 khong bao gio co 31 ngay
        ("29/02/2027 10:00", None),  # 2027 khong nhuan
        ("29/02/2028 10:00", "2028-02-29T10:00"),  # 2028 nhuan -> hop le
        ("29/02/2000 10:00", "2000-02-29T10:00"),  # chia het 400 -> nhuan
        ("29/02/1900 10:00", None),  # chia het 100, khong chia het 400 -> khong nhuan
        # Gio/phut tran
        ("01/01/2026 24:00", None),  # gio 24 khong hop le (0-23)
        ("01/01/2026 23:60", None),  # phut 60 khong hop le (0-59)
        ("01/01/2026 25:70", None),
        # Thieu so 0 dau (khong duoc JS tu bu)
        ("1/1/2026 9:5", None),
        ("1/1/2026 09:05", None),  # ngay/thang van thieu so 0
        # Thang/ngay = 0 hoac am (khong khop regex \d{2} cho so am, nhung 00 thi khop regex
        # roi moi bi chan boi range check)
        ("00/01/2026 10:00", None),  # ngay = 0
        ("01/00/2026 10:00", None),  # thang = 0
        ("01/13/2026 10:00", None),  # thang = 13
        # Chuoi rong / chi khoang trang
        ("", None),
        ("   ", None),
        # Khoang trang thua o dau/cuoi -> van hop le sau trim(); thua o giua dau va gio
        # -> \s+ van khop
        ("  29/07/2026 14:30  ", "2026-07-29T14:30"),
        ("29/07/2026    14:30", "2026-07-29T14:30"),
        # Ky tu la
        ("29/07/2026 14:3a", None),
        ("ab/cd/efgh ij:kl", None),
        ("29-07-2026 14:30", None),  # sai dau phan cach (phai la /)
        ("29/07/2026T14:30", None),  # thieu khoang trang phan cach
        ("29/07/26 14:30", None),  # nam chi 2 chu so (regex bat buoc 4 chu so)
        (None, None),
        (12345, None),
    ],
)
def test_parse_dmy_datetime_edge_cases(text, expected):
    assert parse_dmy_datetime(text) == expected


# ---------------------------------------------------------------------------
# Round-trip formatDmyDatetime(parseDmyDatetime(x)) == x voi input hop le da chuan hoa
# (2 chu so ngay/thang/gio/phut, 4 chu so nam, khong khoang trang thua)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "29/07/2026 14:30",
        "01/01/2026 00:00",
        "31/12/2026 23:59",
        "29/02/2028 09:05",
        "29/02/2000 10:00",
    ],
)
def test_round_trip_parse_then_format_returns_original(text):
    iso = parse_dmy_datetime(text)
    assert iso is not None
    assert format_dmy_datetime(iso) == text


# ---------------------------------------------------------------------------
# formatDmyDatetime rieng le - it edge case hon vi day la chieu server -> UI, dau vao la
# ISO da duoc backend/DB dam bao dung dinh dang (khong phai input nguoi dung tu do)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("2026-08-01T10:00", "01/08/2026 10:00"),
        ("2026-08-01T10:00:00", "01/08/2026 10:00"),  # co giay van khop (regex khong yeu cau $)
        ("", ""),
        ("not-an-iso-string", ""),
        ("2026-08-01", ""),  # thieu phan gio -> khong khop
        (None, ""),
        (12345, ""),
    ],
)
def test_format_dmy_datetime_edge_cases(iso, expected):
    assert format_dmy_datetime(iso) == expected
