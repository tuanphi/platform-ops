"""Test riêng cho bản vá race condition khi seed dòng singleton `app_setting`
(app/services/app_setting_service.py::get_app_setting) - 2 request/thread cùng phát hiện
dòng singleton chưa tồn tại và cùng INSERT gần như đồng thời -> chỉ 1 cái thắng (UNIQUE
constraint trên id), cái thua phải đọc lại dòng đã tạo thay vì để IntegrityError lan
thẳng ra ngoài thành lỗi 500.

Mô phỏng bằng monkeypatch db.get()/db.commit() để tái hiện đúng chuỗi sự kiện mã nguồn
mô tả (db.get() lần 1 -> None, seed + add, commit() raise IntegrityError, rollback, db.get()
lần 2 -> đã có dòng do "thread khác" tạo), thay vì threading thật (không đáng tin cậy với
SQLite in-memory dùng chung 1 connection qua StaticPool)."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import AppSetting
from app.services.app_setting_service import get_app_setting


def test_get_app_setting_recovers_from_integrity_error_on_concurrent_seed(db_session, monkeypatch):
    winner_row = AppSetting(id=AppSetting.SINGLETON_ID, catalog_charts_dir="charts", chart_values_file_template="tpl")

    real_get = db_session.get
    get_calls = {"n": 0}

    def fake_get(model, pk):
        get_calls["n"] += 1
        if get_calls["n"] == 1:
            return None  # lan dau: chua co dong singleton nao (goc du lieu that su rong)
        return winner_row  # lan 2 (sau rollback): da co dong do "thread khac" tao xong

    def fake_commit():
        raise IntegrityError("insert", {}, Exception("UNIQUE constraint failed: app_setting.id"))

    monkeypatch.setattr(db_session, "get", fake_get)
    monkeypatch.setattr(db_session, "commit", fake_commit)
    rollback_called = {"v": False}
    real_rollback = db_session.rollback
    monkeypatch.setattr(db_session, "rollback", lambda: (rollback_called.__setitem__("v", True), real_rollback())[1])

    result = get_app_setting(db_session)

    assert rollback_called["v"] is True, "Phai rollback() session sau khi commit() bi IntegrityError"
    assert result is winner_row, "Phai tra ve dong DA duoc tao boi 'thread thang' thay vi raise loi ra ngoai"
    assert get_calls["n"] == 2


def test_get_app_setting_reraises_when_integrity_error_but_row_still_missing(db_session, monkeypatch):
    """Neu commit() raise IntegrityError nhung doc lai (lan 2) van KHONG thay dong singleton
    dau (tinh huong khong nen xay ra trong thuc te, nhung phai an toan) - phai re-raise
    IntegrityError goc thay vi nuot loi va tra ve None/silently sai."""

    def fake_get(model, pk):
        return None  # ca 2 lan deu None - tinh huong bat thuong

    def fake_commit():
        raise IntegrityError("insert", {}, Exception("UNIQUE constraint failed: app_setting.id"))

    monkeypatch.setattr(db_session, "get", fake_get)
    monkeypatch.setattr(db_session, "commit", fake_commit)
    monkeypatch.setattr(db_session, "rollback", lambda: None)

    with pytest.raises(IntegrityError):
        get_app_setting(db_session)
