"""Flash message dung chung qua session (pop-once): thay the cach cu nhet thang ?ok=..&msg=..
vao query string cua URL redirect (lo thong tin/URL xau, F5 khong mat vi nam san tren URL).
set_flash ghi vao request.session, pop_flash doc 1 lan roi xoa - F5 sau do se khong hien lai.
Toan bo 7 router da chuyen sang set_flash/session (khong con noi nao sinh ?ok=..&msg=.. tren
URL nua), nen pop_flash CHI doc tu session, khong doc query_params - tranh client tu chen
flash_ok/flash_msg gia qua URL (fake toast / social engineering)."""

from fastapi import Request


def set_flash(request: Request, ok: bool, message: str) -> None:
    request.session["flash_ok"] = "1" if ok else "0"
    request.session["flash_msg"] = message


def is_ajax(request: Request) -> bool:
    """Form co class="ajax-form" (base.html) submit bang fetch() voi header Accept:
    application/json de hien thong bao ngay tai cho, khong load lai trang (tranh sinh
    query string ?ok=..&msg=.. tren URL); form submit thuong (khong JS) van di duong
    redirect nhu cu. Gom lai 1 noi duy nhat, truoc day bi copy-paste rieng trong tung
    router (users/catalog/settings/staging/production/actions)."""
    return "application/json" in request.headers.get("accept", "")


def pop_flash(request: Request) -> tuple[str | None, str | None]:
    flash_ok = request.session.pop("flash_ok", None)
    flash_msg = request.session.pop("flash_msg", None)
    return flash_ok, flash_msg
