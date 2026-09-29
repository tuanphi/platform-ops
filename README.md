# form-deploy (Python)

Cổng xin duyệt & thực thi deploy production, viết lại bằng Python (FastAPI) theo
requirement tại [requirement-python.md](requirement-python.md) — bản gốc PHP/Laravel
tham khảo tại `../form-deploy-old`.

App không tự deploy lên Kubernetes — nó chỉ sửa version image trong file YAML của
repo Helm charts rồi `git push`, để ArgoCD tự sync xuống cluster.

## Stack

FastAPI, SQLAlchemy + Alembic (SQLite mặc định, đổi `DATABASE_URL` để dùng MySQL),
APScheduler (cron nội bộ), Jinja2 (server-rendered UI), Authlib (Google OAuth), git CLI
qua `subprocess`, `ruamel.yaml` (sửa YAML giữ nguyên format), Telegram Bot API, SMTP.

## Cài đặt & chạy local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# sửa .env theo môi trường của bạn (DATABASE_URL, ...)

alembic upgrade head   # tao schema DB - bat buoc chay truoc lan dau, va sau moi lan pull code co migration moi

uvicorn app.main:app --reload --port 8000
```

App **không** tự tạo/sửa bảng khi khởi động — schema DB hoàn toàn do Alembic quản lý qua
lệnh riêng, tách biệt với việc chạy app:

```bash
alembic upgrade head                              # ap dung migration moi nhat
alembic revision --autogenerate -m "them cot X"    # sau khi sua models.py, sinh migration moi
alembic downgrade -1                               # roll back 1 buoc neu can
```

Mở http://localhost:8000 — mặc định `AUTH_DEV_MODE=true` nên có thể đăng nhập
bằng cách nhập email bất kỳ (không cần cấu hình Google OAuth để chạy thử). User
mới tạo sẽ có role `user`; muốn test quyền Admin/Super Admin thì sửa trực tiếp
cột `role` trong bảng `users` (giá trị: `user`, `admin`, `super_admin`).

## Các tính năng đã cài (bám theo requirement-python.md)

- Đăng nhập Google OAuth (`AUTH_DEV_MODE=false` để bắt buộc dùng Google thật) + 3 cấp quyền.
- Tạo request deploy có xác minh commit thật trên nhánh `staging` của repo Helm charts qua git CLI.
- Danh sách/dashboard/chi tiết request, giới hạn theo quyền.
- Vòng đời Task → Confirmed → Running → Done / Rejected, với luồng Run re-verify
  commit, sửa đúng 1 field YAML, kiểm tra thay đổi thật trước khi commit/push.
- Scheduler nền: nhắc giờ chạy, đối chiếu ArgoCD để tự chuyển Done, cảnh báo Running quá lâu.
- Telegram bot 2 chiều: webhook `/webhook/sendtelegram` nhận `/list`, `/listdone`,
  `/listconfirm`, `/listreject`, `/listwaiting`, `/listrunning`.
- Quản lý danh mục Group/Project + đồng bộ tự động từ `envValues/values-staging.yaml`.

## Cấu hình cần thiết để dùng thật (không chỉ demo)

- `GIT_REMOTE_APPLICATIONS_URL` + credential git (SSH key/token) có quyền push, mount
  vào máy/container chạy app.
- `ARGOCD_URL_API` + `ARGOCD_TOKEN` của 1 local account read-only trên ArgoCD.
- `TELEGRAM_BOT_TOKEN` + set webhook Telegram trỏ về `{APP_URL}/webhook/sendtelegram`.
- `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` và tắt `AUTH_DEV_MODE`.
- SMTP thật cho `SMTP_HOST`/`MAIL_TO`.
