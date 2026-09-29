"""Đọc/sửa file values-production.yaml (luồng deploy, field `version`) và file
values-staging/values-production dùng cho Bật/Tắt staging + replicas Production (field
`replicas`), giữ nguyên toàn bộ format/thứ tự key còn lại (round-trip qua ruamel.yaml)."""

import re

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

# Instance rieng cho luong deploy (update_image_version) - GIU NGUYEN cau hinh cu, khong
# dong vao de khong anh huong hanh vi da chay tren file that cua luong nay tu truoc gio.
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)

# Instance RIENG cho Bat/Tat staging + replicas Production (update_replicas/
# update_replicas_bulk/load_replicas_map/list_all_project_applications) - CO Y KHONG goi
# .indent(...) nhu instance tren, de ruamel tu giu dung nguyen kieu thut le sequence that
# su cua file goc (vd "- item" ngang hang voi key cha, kieu pho bien trong file values
# Helm chart cua tinh nang nay) thay vi ep ve 1 kieu co dinh - neu dung chung instance da
# cau hinh .indent() o tren, MOI list trong TOAN BO file se bi dinh dang lai moi lan chi
# sua 1 field `replicas`, du gia tri cac key khac khong doi, gay diff git khong lien quan
# rat lon, kho review.
_yaml_replicas = YAML()
_yaml_replicas.preserve_quotes = True


class YamlStructureError(KeyError):
    """File YAML không có sẵn key project/application mong đợi - KHÔNG được phép tự tạo
    nhánh mới (trước đây dùng setdefault() nên âm thầm tạo cấu trúc sai rồi push nhầm lên
    production), phải dừng lại và báo lỗi rõ ràng để git_service không commit gì cả."""


def update_image_version(path: str, project: str, application: str, version: str) -> None:
    with open(path, encoding="utf-8") as f:
        data = _yaml.load(f)

    # data.get(...) (khong phai `in`) de giu nguyen hanh vi AttributeError khi file rong
    # (_yaml.load tra ve None) - loi ro rang thay vi setdefault() am tham tao moi.
    project_node = data.get(project)
    if project_node is None or application not in project_node:
        raise YamlStructureError(
            f"Key '{project}.{application}' không tồn tại sẵn trong {path}"
            " - từ chối tự tạo mới cấu trúc YAML, kiểm tra lại tên project/application"
        )

    application_node = project_node[application]
    if "version" not in application_node:
        raise YamlStructureError(
            f"Key '{project}.{application}.version' không tồn tại sẵn trong {path}"
        )

    application_node["version"] = DoubleQuotedScalarString(version)

    with open(path, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)


def load_replicas_map(path: str) -> dict[tuple[str, str], str]:
    """Doc file values-{env}.yaml (cau truc {project}: {application}: replicas: "N"),
    tra ve dict {(project, application): replicas} DE HIEN THI - khong sua file. File
    rong/khong ton tai cau truc mong doi -> tra ve dict rong (khac update_replicas, o day
    KHONG raise vi day chi la doc de hien thi, khong phai thao tac ghi)."""
    with open(path, encoding="utf-8") as f:
        data = _yaml_replicas.load(f)
    result: dict[tuple[str, str], str] = {}
    if not data:
        return result
    for project, project_node in data.items():
        if not isinstance(project_node, dict):
            continue
        for application, application_node in project_node.items():
            if isinstance(application_node, dict) and "replicas" in application_node:
                result[(project, application)] = str(application_node["replicas"])
    return result


_SCALAR_FIELD_LINE_RE_TEMPLATE = r'^(\s*{field}\s*:\s*)(.*?)(\s*(?:#.*)?)$'


def _replace_scalar_field_line(lines: list[str], line_no: int, field: str, new_value: str) -> None:
    """Thay THE gia tri cua 1 field scalar don gian (dang "field: value", nam GON tren 1
    dong) - chi doi phan gia tri, giu nguyen y nguyen phan con lai cua dong (thut le, ten
    key, comment cuoi dong neu co, va dung line-ending goc). Sua truc tiep vao list
    `lines` (khong dong/mo lai file), KHONG dung ruamel dump lai ca document - day chinh
    la diem mau chot: dump lai ca file se lam ruamel chuan hoa dinh dang CUA MOI DONG
    KHAC trong file ve 1 kieu thut le/do rong dong duy nhat, ke ca khi file goc von da
    dung LAN LON nhieu kieu khac nhau o cac vi tri khac nhau (thuc te thuong gap trong
    file Helm values lon, duoc nhieu nguoi/nhieu doi sua qua thoi gian) - gay diff git
    khong lien quan rat lon. Sua truc tiep 1 dong bang text thi KHONG the sai dinh dang
    cac dong khac, vi khong dong nao khac bi dung vao."""
    line = lines[line_no]
    # Tach rieng line-ending TRUOC khi regex, roi gan lai NGUYEN VEN sau - tranh regex
    # phai tu phan dinh \s* (khop ca \n) voi \r?\n?$ o cuoi pattern, de gay ambiguous
    # (vd \s* "nuot" luon ca \n khien nhom cuoi + \n them vao sau bi trung lap thanh 2
    # dong trong).
    if line.endswith("\r\n"):
        content, ending = line[:-2], "\r\n"
    elif line.endswith("\n"):
        content, ending = line[:-1], "\n"
    else:
        content, ending = line, ""

    pattern = _SCALAR_FIELD_LINE_RE_TEMPLATE.format(field=re.escape(field))
    match = re.match(pattern, content)
    if match is None:
        raise YamlStructureError(f"Không parse được dòng chứa field '{field}': {line!r}")
    prefix, _old_value, suffix = match.groups()
    lines[line_no] = f'{prefix}"{new_value}"{suffix}{ending}'


def update_replicas(path: str, project: str, application: str, replicas: int) -> None:
    """Sua field `replicas` (luu dang chuoi, vd "0"/"1"/"3") cho 1 project/application co
    san trong file - cung triet ly voi update_image_version: TU CHOI tu tao key moi neu
    project/application chua ton tai san (YamlStructureError). Dung ruamel CHI DE XAC
    DINH dung dong can sua (qua .lc.value - line/column metadata cua round-trip loader),
    roi sua truc tiep DUNG 1 dong do bang text (xem _replace_scalar_field_line) - KHONG
    dump lai ca document, dam bao khong dong nao khac trong file bi dung vao."""
    with open(path, encoding="utf-8") as f:
        data = _yaml_replicas.load(f)

    project_node = data.get(project) if data else None
    if project_node is None or application not in project_node:
        raise YamlStructureError(
            f"Key '{project}.{application}' không tồn tại sẵn trong {path}"
            " - từ chối tự tạo mới cấu trúc YAML, kiểm tra lại tên project/application"
        )

    application_node = project_node[application]
    if "replicas" not in application_node:
        raise YamlStructureError(f"Key '{project}.{application}.replicas' không tồn tại sẵn trong {path}")

    line_no, _col = application_node.lc.value("replicas")

    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    _replace_scalar_field_line(lines, line_no, "replicas", str(replicas))
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def update_replicas_bulk(path: str, entries: list[tuple[str, str, int]]) -> list[tuple[str, str]]:
    """Sua replicas cho NHIEU project/application trong 1 lan doc/ghi file duy nhat (dung
    cho bulk-toggle nhieu app chon cung luc, hoac "Tat tat ca") - tranh mo/ghi file lien
    tuc tung app 1. Tra ve danh sach (project, application) THUC SU duoc sua (bo qua,
    KHONG raise, cho cap nao khong ton tai san trong file). Giong update_replicas: chi
    dung ruamel de xac dinh dung dong, sua truc tiep tung dong do bang text (KHONG dump
    lai ca document) - thay doi NOI DUNG 1 dong khong lam lech so dong cua ca file nen
    cac vi tri (line_no) xac dinh tu ban goc van dung cho toan bo cac entry con lai."""
    with open(path, encoding="utf-8") as f:
        data = _yaml_replicas.load(f)

    edits: list[tuple[int, str, str]] = []  # (line_no, project, application) da xac nhan hop le
    updated: list[tuple[str, str]] = []
    for project, application, replicas in entries:
        project_node = data.get(project) if data else None
        if project_node is None or application not in project_node:
            continue
        application_node = project_node[application]
        if not isinstance(application_node, dict) or "replicas" not in application_node:
            continue
        line_no, _col = application_node.lc.value("replicas")
        edits.append((line_no, str(replicas)))
        updated.append((project, application))

    if edits:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        for line_no, replicas_value in edits:
            _replace_scalar_field_line(lines, line_no, "replicas", replicas_value)
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)

    return updated


def list_all_project_applications(path: str) -> list[tuple[str, str]]:
    """Liet ke TOAN BO cap (project, application) dang co san trong file - dung cho nut
    "Tat tat ca" (set replicas=0 cho moi cap nay, KHONG can biet truoc danh sach tu
    catalog)."""
    with open(path, encoding="utf-8") as f:
        data = _yaml_replicas.load(f)
    pairs: list[tuple[str, str]] = []
    if not data:
        return pairs
    for project, project_node in data.items():
        if not isinstance(project_node, dict):
            continue
        for application, application_node in project_node.items():
            if isinstance(application_node, dict) and "replicas" in application_node:
                pairs.append((project, application))
    return pairs
