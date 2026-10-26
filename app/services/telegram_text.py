"""Dich sang tieng Anh cac thong bao ket qua (tieng Viet) dung trong tin Telegram cua
Restart/Replicas staging-production va Schedule. CHI dung cho Telegram - UI (toast/flash)
van giu nguyen tieng Viet tu cac service goc (git_service/argocd_service/restart_service/
schedule_service). Thong bao khong khop mau nao (vd loi git dong) giu nguyen."""

import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # git_service - thay doi replicas
    (re.compile(r"^Đã cập nhật$"), "Updated"),
    (re.compile(r"^Đã tạo Merge Request vào nhánh (.+)$"), "Merge Request created to branch {0}"),
    (re.compile(r"^Đã tắt tất cả staging$"), "All staging apps turned off"),
    (re.compile(r"^File (.+) không tồn tại$"), "File {0} does not exist"),
    (
        re.compile(r"^Không tìm thấy ứng dụng nào khớp trong (.+) - kiểm tra lại cấu hình catalog/file$"),
        "No matching application found in {0} - check the catalog/file configuration",
    ),
    (re.compile(r"^File (.+) không thay đổi \(giá trị hiện tại đã đúng\)$"), "File {0} unchanged (value already up to date)"),
    (re.compile(r"^File (.+) không thay đổi \(tất cả đã ở replicas=0\)$"), "File {0} unchanged (all already at replicas=0)"),
    (re.compile(r"^File values-staging không có ứng dụng nào$"), "values-staging file has no applications"),
    (re.compile(r"^Không có ứng dụng nào cần cập nhật$"), "No applications need updating"),
    (re.compile(r"^(.+) chưa được cấu hình$"), "{0} is not configured"),
    # argocd_service / restart_service - restart
    (re.compile(r"^Đã khởi động lại (.+)$"), "Restarted {0}"),
    (
        re.compile(r"^ArgoCD chưa được cấu hình \(ARGOCD_URL_API rỗng\), không thể restart$"),
        "ArgoCD is not configured (ARGOCD_URL_API is empty), cannot restart",
    ),
    (
        re.compile(r"^Không tìm thấy Deployment/StatefulSet nào cho ứng dụng (.+) trên ArgoCD$"),
        "No Deployment/StatefulSet found for application {0} on ArgoCD",
    ),
    (
        re.compile(r"^Dữ liệu resource-tree của (.+) thiếu name/namespace, không thể restart$"),
        "Resource-tree data of {0} is missing name/namespace, cannot restart",
    ),
    (re.compile(r"^Gọi ArgoCD API để restart thất bại cho (.+)$"), "ArgoCD API restart call failed for {0}"),
    (re.compile(r"^Tên project/application không hợp lệ$"), "Invalid project/application name"),
    (re.compile(r"^Ứng dụng không tồn tại trong danh sách được phép$"), "Application is not in the allowed list"),
    (re.compile(r"^Vui lòng đợi (\d+) giây rồi thử lại$"), "Restart cooldown: please wait {0} seconds and try again"),
    # schedule_service
    (re.compile(r"^Người tạo lịch \((.+)\) không còn quyền thực hiện$"), "Schedule creator ({0}) no longer has permission"),
    (re.compile(r"^Lỗi không xác định: (.*)$"), "Unexpected error: {0}"),
]


def to_english(message: str) -> str:
    for pattern, template in _PATTERNS:
        match = pattern.match(message or "")
        if match:
            return template.format(*match.groups())
    return message
