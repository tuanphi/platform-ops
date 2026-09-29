"""Nguoi dung nhap/xem gio theo GMT+7 (VN), nhung DB luu UTC thong nhat voi cac truong
datetime khac trong app (created_at, done_at, ...) de scheduler so sanh dung voi
datetime.utcnow()."""
from datetime import datetime, timedelta

GMT7_OFFSET = timedelta(hours=7)


def gmt7_to_utc(local_dt: datetime) -> datetime:
    return local_dt - GMT7_OFFSET


def utc_to_gmt7(utc_dt: datetime) -> datetime:
    return utc_dt + GMT7_OFFSET


def format_dmy(dt: datetime | None) -> str | None:
    """Format datetime thanh dd/mm/yyyy HH:MM de hien thi thong nhat tren webUI. CHI doi
    cach hien thi (khong doi timezone) - dung sau (hoac khong dung sau) filter `gmt7` tuy
    tung truong, xem dang ky trong tasks_router/actions_router. Tra ve None (khong phai
    '-') khi dt la None/falsy, de template tu quyet dinh fallback bang `or '-'` giong cach
    filter `gmt7` da lam san."""
    return dt.strftime("%d/%m/%Y %H:%M") if dt else None
