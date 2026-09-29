# Prompt: Yêu cầu xây dựng app "form-deploy" bằng Python

Xây dựng một web app nội bộ đóng vai trò **cổng xin duyệt & thực thi deploy production**. App không tự deploy lên Kubernetes — nó chỉ sửa version image trong file YAML của repo Helm charts rồi push lên git, để ArgoCD tự sync xuống cluster. App cần tích hợp Telegram bot + email để thông báo real-time theo từng bước trong quy trình.

## 1. Đăng nhập & phân quyền

- Đăng nhập qua Google OAuth. User chưa tồn tại thì tự tạo mới với quyền thấp nhất.
- 3 role cố định:
  - **Super Admin**: toàn quyền — Confirm/Reject/Run mọi request, và là role duy nhất được vào trang quản lý Group/Project (`/catalog`) + Sync danh mục + quản lý role/quyền của user khác.
  - **Admin**: được Confirm/Reject **toàn bộ** request (không giới hạn theo project) và xem toàn bộ danh sách/dashboard, nhưng **không được Run** (trừ khi được gán full quyền riêng — xem dưới) và **không vào được** trang quản lý Group/Project.
  - **User** (mặc định): chỉ tạo request; chỉ xem được request/danh sách/dashboard của chính mình.
- Ngoài 3 role trên, Super Admin có thể:
  - Đổi role của bất kỳ user nào giữa User/Admin/Super Admin (tại `/catalog`).
  - Gán thêm **"full quyền"** (Confirm/Reject + Run) cho **một user cụ thể**, ở 1 trong 2 mức độ, độc lập với role hiện tại của user đó:
    - Theo **Group (project)**: áp dụng cho Group đó và **mọi** Application con bên trong.
    - Theo **từng Application (project con) lẻ**: chỉ áp dụng cho đúng 1 Application cụ thể trong 1 Group, không ảnh hưởng các Application khác cùng Group.
  - Ví dụ: một User (role thấp nhất) được gán full quyền trên Group X sẽ Confirm/Reject/Run được mọi request thuộc Group X (và chỉ Group X); nếu chỉ gán riêng cho Application `app1` trong Group X thì chỉ thao tác được request của `app1`, không phải các application khác trong X. Một Admin được gán full quyền trên Group Y thì Run được request thuộc Group Y dù Admin mặc định không Run được.
  - User có ít nhất 1 quyền này (dù role là User) thì được xem toàn bộ danh sách/dashboard, không chỉ của mình.

## 2. Tạo request deploy

- Form nhập: chọn **Group** (dự án cha) → **Application** (con) → nhập **CommitID**.
- Trước khi tạo request, phải xác minh commit đó tồn tại thật trên nhánh staging của repo Helm charts và đúng project/application đã chọn (đối chiếu qua lịch sử git, không qua API), tránh tạo request với commit sai/giả.
- Tạo request thành công → lưu trạng thái ban đầu, gửi thông báo (Telegram + email) bất đồng bộ, không chặn response.

## 3. Xem danh sách / chi tiết / dashboard

- Danh sách có filter theo người tạo và theo trạng thái, có phân trang.
- User thường chỉ thấy request của chính mình; Admin/Super Admin thấy toàn bộ.
- Có dashboard đếm số lượng request theo từng trạng thái, và trang xem chi tiết từng request.

## 4. Vòng đời trạng thái & hành động

```
Task ──(Confirm)──> Confirmed ──(Run)──> Running ──(giám sát tự động)──> Done
  │                     │
  └──(Reject)──> Rejected <──(Reject)──┘
```

- Không được Reject hoặc Run một task đã Done.
- **Confirm/Reject**: Admin, Super Admin, hoặc user được gán full quyền trên đúng project (Group) của task đó.
- **Run**: chỉ Super Admin, hoặc user (bất kỳ role) được gán full quyền trên đúng project (Group) của task đó — kiểm tra tại thời điểm thao tác.
- **Confirm**: chọn thời điểm dự kiến chạy (mặc định ngay bây giờ nếu bỏ trống), gửi thông báo.
- **Run**: xác minh lại commit/version tại đúng thời điểm chạy (không tin dữ liệu cũ đã lưu), sửa đúng version image trong file cấu hình chart tương ứng của project/application đó, kiểm tra chắc chắn có thay đổi thật rồi mới commit & push lên git bằng danh tính hệ thống; chỉ khi push thành công mới chuyển trạng thái sang Running.

## 5. Giám sát tự động (chạy nền)

- Định kỳ nhắc nhở khi đến gần giờ chạy đã Confirm.
- Định kỳ đối chiếu trạng thái thực tế trên ArgoCD với version mong muốn của các task đang Running; khớp thì chuyển sang Done và thông báo.
- Cảnh báo nếu một task Running quá lâu (ví dụ 15 phút) mà vẫn chưa khớp version.

## 6. Telegram Bot hai chiều

- Ngoài gửi thông báo, bot phải nhận lệnh qua webhook và trả lời ngay trong Telegram (không cần mở web): liệt kê danh sách task theo từng trạng thái (tất cả/done/confirm/reject/waiting/running).

## 7. Quản lý danh mục Group/Project + phân quyền (chỉ Super Admin)

- CRUD Group và Application con thủ công, có cờ ẩn/hiện khỏi dropdown mà không cần xoá.
- Có chức năng đồng bộ tự động danh mục Group/Application từ cấu trúc thật của repo Helm charts, để không phải nhập tay khi cấu trúc chart thay đổi. **Sync là ghi đè toàn bộ**: Group/Application nào không còn tồn tại trong repo (kể cả loại đã tạo tay) sẽ bị **xoá thẳng** khỏi danh mục, không chỉ ẩn — kèm số liệu tạo mới/xoá sau mỗi lần sync.
- Quản lý role: đổi role bất kỳ user nào giữa User/Admin/Super Admin.
- Quản lý full quyền: gán/gỡ "full quyền" (Confirm/Reject + Run) cho user cụ thể — theo từng Group, hoặc theo từng Application lẻ bên trong Group (user đó phải đã từng đăng nhập ít nhất 1 lần).

## 8. Tích hợp bên ngoài cần hỗ trợ

- Git (thao tác trên repo Helm charts: đọc lịch sử commit, sửa file, commit, push).
- ArgoCD API (chỉ đọc trạng thái, không trigger sync/deploy).
- Telegram Bot API (gửi chủ động + nhận webhook).
- Email/SMTP (thông báo tổng hợp).
- Google OAuth (đăng nhập).

## Ràng buộc chung

- Mọi thông báo (Telegram/email) phải chạy nền, không làm chậm response của các thao tác tạo/duyệt/chạy task.
- Ghi log đầy đủ ai làm gì lúc nào cho các hành động thay đổi trạng thái quan trọng.
- Hành động Run không thể tự động hoàn tác (đã push git thật) nên phải xác minh kỹ trước khi thực hiện, tránh push nhầm hoặc push khi không có gì thay đổi.
