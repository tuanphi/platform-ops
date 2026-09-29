"""Kiểm thử app/services/yaml_service.py - sửa đúng 1 field `version` trong file
values-production.yaml, phải giữ nguyên toàn bộ format/field khác."""

import pytest
from ruamel.yaml import YAML

from app.services.yaml_service import (
    YamlStructureError,
    list_all_project_applications,
    load_replicas_map,
    update_image_version,
    update_replicas,
    update_replicas_bulk,
)


def _read_raw(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_update_image_version_happy_path_preserves_other_fields(tmp_path):
    yaml_path = tmp_path / "values-production.yaml"
    yaml_path.write_text(
        "core:\n"
        "  api:\n"
        '    version: "v1.0.0"\n'
        "    replicas: 3\n"
        "  worker:\n"
        '    version: "v1.0.0"\n'
        "other-group:\n"
        '  other-app:\n'
        '    version: "v9.9.9"\n'
    )

    update_image_version(str(yaml_path), "core", "api", "v2.0.0")

    yaml = YAML()
    data = yaml.load(yaml_path.read_text())
    assert data["core"]["api"]["version"] == "v2.0.0"
    assert data["core"]["api"]["replicas"] == 3  # field khác không bị đụng
    assert data["core"]["worker"]["version"] == "v1.0.0"  # app khác cùng group không đổi
    assert data["other-group"]["other-app"]["version"] == "v9.9.9"  # group khác không đổi


def test_update_image_version_reformats_sequences_to_configured_indent_style(tmp_path):
    """update_image_version (luong deploy) CO CHU DICH GIU NGUYEN cau hinh
    .indent(mapping=2, sequence=4, offset=2) da dung tu truoc gio - KHONG duoc dong vao,
    du no ep dinh dang lai cac list trong file (khac han update_replicas ben duoi, dung
    instance rieng khong ep dinh dang) - xac nhan hanh vi nay khong bi thay doi ngoai y
    muon boi cac fix cho tinh nang Bat/Tat staging/Production."""
    yaml_path = tmp_path / "values-production.yaml"
    yaml_path.write_text(
        "core:\n"
        "  api:\n"
        '    version: "v1.0.0"\n'
        "    applicationConfig:\n"
        "    - .env\n"
    )

    update_image_version(str(yaml_path), "core", "api", "v2.0.0")

    raw = yaml_path.read_text()
    assert '"v2.0.0"' in raw
    assert "      - .env" in raw  # bi ep them thut le - dung y (khong doi hanh vi cu)


def test_update_replicas_preserves_original_sequence_indent_style(tmp_path):
    """Regression: file values-sandbox/values-production (Bat/Tat staging + Production
    replicas) thuong dung kieu "- item" NGANG HANG voi key cha (khong thut le them, pho
    bien trong file values Helm chart thuc te) - update_replicas PHAI giu dung nguyen
    kieu nay (dung instance _yaml_replicas rieng, khong ep .indent() nhu update_image_version
    ben deploy), tranh 1 lan sua chi field `replicas` lam bien dang toan bo list khong
    lien quan trong file, gay diff git kho review."""
    yaml_path = tmp_path / "values-sandbox.yaml"
    original = (
        "standard:\n"
        "  capabilities:\n"
        "    drop:\n"
        "    - ALL\n"
        "core:\n"
        "  api:\n"
        '    replicas: "0"\n'
        "    applicationConfig:\n"
        "    - .env\n"
        "    servicePort:\n"
        "    - name: http\n"
        "      port: 80\n"
    )
    yaml_path.write_text(original)

    update_replicas(str(yaml_path), "core", "api", 3)

    raw = yaml_path.read_text()
    assert raw == original.replace('"0"', '"3"')


def test_update_replicas_bulk_only_changes_target_lines_in_mixed_style_file(tmp_path):
    """Tai hien dung tinh huong nguoi dung bao cao: 1 file that co NHIEU kieu thut le
    sequence LAN LON o cac vi tri khac nhau (mot so cho "- item" ngang hang key cha, mot
    so cho thut le them), cong voi 1 gia tri chuoi dai (JAVA_OPTS, de bi ruamel wrap dong
    neu dung width mac dinh) va 1 dong co trailing whitespace (de bi ruamel strip mat) -
    sua replicas cua nhieu app CHI duoc doi DUNG cac dong replicas tuong ung, moi thu
    khac (bao gom cac kieu thut le khong dong nhat, do dai dong, trailing whitespace)
    phai giu y nguyen tung ky tu."""
    yaml_path = tmp_path / "values-sandbox.yaml"
    original = (
        "core:\n"
        "  bankgateway:\n"
        "    replicas: \"1\"\n"
        "    authorization:\n"
        "      acls:\n"
        "        - resource:\n"
        "            type: topic\n"
        "          operation: All\n"
        "    env:\n"
        "    - name: \"JAVA_OPTS\"\n"
        "      value: \"-Dspring.config.location=classpath:/application.yml,file:/mnt/secrets/application.yml -XX:MaxRAMPercentage=75\"\n"
        "  \n"
        "  confirmation-api:\n"
        "    replicas: \"2\"\n"
        "    applicationConfig:\n"
        "    - \"private1.pem\" \n"
        "    - \"public.pem\"\n"
    )
    yaml_path.write_text(original)

    updated = update_replicas_bulk(
        str(yaml_path), [("core", "bankgateway", 3), ("core", "confirmation-api", 5)]
    )

    assert set(updated) == {("core", "bankgateway"), ("core", "confirmation-api")}
    raw = yaml_path.read_text()
    expected = original.replace('replicas: "1"', 'replicas: "3"').replace('replicas: "2"', 'replicas: "5"')
    assert raw == expected  # tuyet doi khong dong nao khac bi doi, ke ca cac kieu thut le lan lon


def test_update_image_version_keeps_double_quote_style(tmp_path):
    yaml_path = tmp_path / "values-production.yaml"
    yaml_path.write_text('core:\n  api:\n    version: "v1.0.0"\n')

    update_image_version(str(yaml_path), "core", "api", "v2.0.0")

    raw = _read_raw(yaml_path)
    assert 'version: "v2.0.0"' in raw


def test_update_image_version_should_fail_when_project_key_missing(tmp_path):
    """FIXED: khi field project/application KHÔNG tồn tại sẵn trong YAML, phải raise
    YamlStructureError rõ ràng thay vì âm thầm setdefault() tạo mới nhánh đó - tránh ghi
    nhầm cấu trúc vào file values-production.yaml khi project/application đặt sai tên."""
    yaml_path = tmp_path / "values-production.yaml"
    yaml_path.write_text("other-group:\n  other-app:\n    version: \"v1.0.0\"\n")

    with pytest.raises(YamlStructureError):
        update_image_version(str(yaml_path), "core", "api", "v2.0.0")

    # File không bị ghi đè/thay đổi khi lỗi xảy ra.
    raw = yaml_path.read_text()
    assert "core" not in raw


def test_update_image_version_should_fail_when_application_key_missing(tmp_path):
    """FIXED: project tồn tại nhưng application con chưa có -> raise YamlStructureError
    thay vì tạo mới nhánh application."""
    yaml_path = tmp_path / "values-production.yaml"
    yaml_path.write_text('core:\n  api:\n    version: "v1.0.0"\n')

    with pytest.raises(YamlStructureError):
        update_image_version(str(yaml_path), "core", "worker", "v2.0.0")

    # File không bị ghi đè/thay đổi khi lỗi xảy ra.
    raw = yaml_path.read_text()
    assert "worker" not in raw


def test_update_image_version_raises_on_empty_file(tmp_path):
    """File rỗng -> _yaml.load trả về None -> .setdefault trên None phải lỗi rõ ràng
    (AttributeError) thay vì âm thầm ghi đè - xác nhận hành vi lỗi hiện tại."""
    yaml_path = tmp_path / "values-production.yaml"
    yaml_path.write_text("")

    with pytest.raises(AttributeError):
        update_image_version(str(yaml_path), "core", "api", "v2.0.0")


# ---------------------------------------------------------------------------
# update_replicas / update_replicas_bulk / load_replicas_map / list_all_project_applications
# (tinh nang Bat/Tat staging + thay doi replicas Production)
# ---------------------------------------------------------------------------


def test_update_replicas_happy_path_preserves_other_fields(tmp_path):
    yaml_path = tmp_path / "values-sandbox.yaml"
    yaml_path.write_text('core:\n  api:\n    replicas: "0"\n  worker:\n    replicas: "0"\n')

    update_replicas(str(yaml_path), "core", "api", 3)

    yaml = YAML()
    data = yaml.load(yaml_path.read_text())
    assert data["core"]["api"]["replicas"] == "3"
    assert data["core"]["worker"]["replicas"] == "0"  # app khac khong bi dung


def test_update_replicas_raises_when_project_missing(tmp_path):
    yaml_path = tmp_path / "values-sandbox.yaml"
    yaml_path.write_text('other-group:\n  other-app:\n    replicas: "0"\n')

    with pytest.raises(YamlStructureError):
        update_replicas(str(yaml_path), "core", "api", 1)


def test_load_replicas_map_reads_all_entries(tmp_path):
    yaml_path = tmp_path / "values-sandbox.yaml"
    yaml_path.write_text('core:\n  api:\n    replicas: "1"\n  worker:\n    replicas: "0"\n')

    result = load_replicas_map(str(yaml_path))

    assert result == {("core", "api"): "1", ("core", "worker"): "0"}


def test_load_replicas_map_empty_file_returns_empty_dict(tmp_path):
    yaml_path = tmp_path / "values-sandbox.yaml"
    yaml_path.write_text("")

    assert load_replicas_map(str(yaml_path)) == {}


def test_update_replicas_bulk_updates_matching_and_skips_missing(tmp_path):
    yaml_path = tmp_path / "values-sandbox.yaml"
    yaml_path.write_text('core:\n  api:\n    replicas: "0"\n  worker:\n    replicas: "0"\n')

    updated = update_replicas_bulk(
        str(yaml_path),
        [("core", "api", 1), ("core", "worker", 1), ("no-such-group", "app", 1)],
    )

    assert set(updated) == {("core", "api"), ("core", "worker")}
    result = load_replicas_map(str(yaml_path))
    assert result == {("core", "api"): "1", ("core", "worker"): "1"}


def test_update_replicas_bulk_does_not_write_file_when_nothing_matches(tmp_path):
    yaml_path = tmp_path / "values-sandbox.yaml"
    original = 'core:\n  api:\n    replicas: "0"\n'
    yaml_path.write_text(original)

    updated = update_replicas_bulk(str(yaml_path), [("no-such-group", "app", 1)])

    assert updated == []
    assert yaml_path.read_text() == original  # khong ghi lai file khi khong co gi doi


def test_list_all_project_applications_returns_all_pairs(tmp_path):
    yaml_path = tmp_path / "values-sandbox.yaml"
    yaml_path.write_text('core:\n  api:\n    replicas: "1"\n  worker:\n    replicas: "0"\nother:\n  app:\n    replicas: "0"\n')

    pairs = list_all_project_applications(str(yaml_path))

    assert set(pairs) == {("core", "api"), ("core", "worker"), ("other", "app")}
