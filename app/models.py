import enum
from datetime import datetime, timedelta

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Role(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


# Ngoai role co dinh, Super Admin co the gan them "full quyen" (Approve/Reject + Run)
# cho 1 user cu the tren 1 Group (project) + toan bo Project (application) con ben trong,
# doc lap voi role hien tai cua user do.
group_admin_access = Table(
    "group_admin_access",
    Base.metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("group_id", ForeignKey("groups.id"), primary_key=True),
)

# Gan chi tiet hon: full quyen chi tren 1 Project (application) le, khong anh huong
# cac application khac cung Group.
project_admin_access = Table(
    "project_admin_access",
    Base.metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("project_id", ForeignKey("projects.id"), primary_key=True),
)


class TaskStatus(str, enum.Enum):
    TASK = "Task"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    CANCELLED = "Cancelled"
    QUEUED = "Queued"
    """Task da bam Run/Rollback nhung CHUA thuc su chay git - dang cho trong hang doi nen
    (xem app/scheduler.py::job_process_task_queue va app/services/task_service.py::
    process_task_queue). Them trang thai nay de tach roi 2 buoc "nguoi dung xin Run" (tra
    loi ngay, khong giu git lock/thread request) va "worker nen thuc su chay git" (giu git
    lock TIMEOUT DAI hon vi khong con ai dang cho tren request) - tranh loi that trong
    production: bam Run nhieu task gan nhau -> cac task sau bi GitError do khong xin duoc
    git lock trong _GIT_LOCK_TIMEOUT_SECONDS (2s, van GIU NGUYEN cho duong request nguoi
    dung nhu check_commit_on_staging luc Tao request)."""
    RUNNING = "Running"
    DONE = "Done"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.USER)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    can_toggle_staging: Mapped[bool] = mapped_column(Boolean, default=False)
    """Cong tac TONG (khong theo tung project/group) do Super Admin cap qua Settings >
    Users - cho phep Admin/User thuong SUA replicas (Bat/Tat, doi so pod, Sync, Tat tat
    ca) o menu Staging. Hien tren UI la cot "Staging Replicas". KHONG con bao gom Restart
    tu ban vá tach quyen 2026-08-18 - xem can_toggle_restart_staging rieng. Super Admin
    luon co quyen bat ke gia tri nay (xem can_write_staging)."""
    can_toggle_production: Mapped[bool] = mapped_column(Boolean, default=False)
    """Cong tac TONG rieng cho quyen SUA replicas o menu Production - DOC LAP voi
    can_toggle_staging (2 quyen tach biet). Hien tren UI la cot "Production Replicas".
    Super Admin luon co quyen (xem can_write_production)."""
    can_view_staging: Mapped[bool] = mapped_column(Boolean, default=False)
    """Cong tac TONG cho quyen CHI XEM (read-only) menu Staging - duoc vao xem trang
    nhung KHONG duoc sua bat ky gi. Hien tren UI la cot "Staging Read". Doc lap voi
    can_toggle_staging/can_toggle_restart_staging - 1 user co the duoc cap bat ky to hop
    nao trong 3 cot Staging (Replicas/Restart/Read), hoac khong cot nao (khong vao duoc
    trang)."""
    can_view_production: Mapped[bool] = mapped_column(Boolean, default=False)
    """Tuong tu can_view_staging, rieng cho Production. Hien tren UI la cot
    "Production Read"."""
    can_toggle_restart_staging: Mapped[bool] = mapped_column(Boolean, default=False)
    """Cong tac TONG rieng cho quyen bam nut Restart o menu Staging - TACH BIET hoan toan
    voi can_toggle_staging (quyen sua replicas) tu 2026-08-18, vi Restart chi khoi dong
    lai pod qua ArgoCD (khong doi replicas/file values.yaml) nen la 1 loai thao tac khac
    ban chat, co the can cap rieng (vd 1 user chi duoc restart de xu ly su co, khong duoc
    sua so luong pod). Hien tren UI la cot "Staging Restart". Xem can_restart_staging."""
    can_toggle_restart_production: Mapped[bool] = mapped_column(Boolean, default=False)
    """Tuong tu can_toggle_restart_staging, rieng cho Production - DOC LAP hoan toan.
    Hien tren UI la cot "Production Restart". Xem can_restart_production."""
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    granted_groups: Mapped[list["Group"]] = relationship(secondary=group_admin_access, back_populates="granted_users")
    granted_projects: Mapped[list["Project"]] = relationship(
        secondary=project_admin_access, back_populates="granted_users"
    )

    @property
    def can_manage_settings(self) -> bool:
        """Vao /catalog: quan ly Group/Project + Sync danh muc + gan role/quyen. Chi Super Admin."""
        return self.role == Role.SUPER_ADMIN

    @property
    def can_view_all_tasks(self) -> bool:
        """LUU Y: True chi co nghia la user co THAM GIA vao view-scope nao do (Admin/Super
        Admin HOAC duoc grant it nhat 1 Group/Project) - KHONG dong nghia voi "xem duoc MOI
        task". Property nay dung de dieu khien UI (vd hien o tim kiem theo email trong
        task_list.html) va cac test hien co. De loc DUNG pham vi task duoc xem, dung
        `is_full_access_role` (Admin/Super Admin xem tat ca) ket hop `can_view_task()`
        (grant Group/Project chi xem dung pham vi duoc cap) tai tang router/query -
        xem app/routers/tasks_router.py::list_tasks/task_detail."""
        return (
            self.role in (Role.ADMIN, Role.SUPER_ADMIN)
            or bool(self.granted_groups)
            or bool(self.granted_projects)
        )

    @property
    def is_full_access_role(self) -> bool:
        """Chi Admin/Super Admin duoc xem TOAN BO task cua moi project/group. User chi co
        grant Group/Project rieng le (du can_view_all_tasks tra True) KHONG duoc coi la
        full access - pham vi xem cua ho phai gioi han dung phan duoc cap (xem
        can_view_task)."""
        return self.role in (Role.ADMIN, Role.SUPER_ADMIN)

    def _has_project_grant(self, project_name: str, application_name: str) -> bool:
        if any(g.group_name == project_name for g in self.granted_groups):
            return True
        return any(
            p.application_name == application_name and p.group.group_name == project_name
            for p in self.granted_projects
        )

    def can_view_task(self, project_name: str, application_name: str) -> bool:
        """View-scope: user duoc grant Group hoac Project khop dung project_name/
        application_name nay thi duoc xem task tuong ung (DeployTask.project luu
        group.group_name, DeployTask.application luu project.application_name - xem
        task_service.create_task). Tach rieng khoi can_approve/can_run de neu sau nay
        logic action-scope va view-scope tach nhau thi khong anh huong lan nhau; hien tai
        dung chung dieu kien voi _has_project_grant vi cung 1 nguon grant."""
        return self._has_project_grant(project_name, application_name)

    def can_approve(self, project_name: str, application_name: str) -> bool:
        if self.role in (Role.ADMIN, Role.SUPER_ADMIN):
            return True
        return self._has_project_grant(project_name, application_name)

    def can_run(self, project_name: str, application_name: str) -> bool:
        if self.role in (Role.ADMIN, Role.SUPER_ADMIN):
            return True
        return self._has_project_grant(project_name, application_name)

    @property
    def can_delete_task(self) -> bool:
        """Xoa vinh vien task (khac Cancel - chi doi status, van con lai trong DB): gioi
        han CHI Super Admin, khong mo rong theo grant Group/Project nhu can_approve/can_run
        vi thao tac khong the hoan tac."""
        return self.role == Role.SUPER_ADMIN

    @property
    def can_write_staging(self) -> bool:
        """SUA REPLICAS duoc o menu Staging (Bat/Tat, doi so pod, Sync, Tat tat ca) -
        KHONG bao gom Restart (xem can_restart_staging rieng tu 2026-08-18): Super Admin
        luon duoc (full quyen mac dinh); Admin/User thuong CHI duoc khi da duoc Super
        Admin bat cong tac can_toggle_staging qua Settings > Users (cong tac TONG, khong
        theo tung project/group)."""
        return self.role == Role.SUPER_ADMIN or self.can_toggle_staging

    @property
    def can_restart_staging(self) -> bool:
        """Bam duoc nut Restart o menu Staging - quyen RIENG, DOC LAP hoan toan voi
        can_write_staging (SUA replicas). Super Admin luon duoc; Admin/User thuong CHI
        duoc khi da duoc Super Admin bat cong tac can_toggle_restart_staging qua
        Settings > Users."""
        return self.role == Role.SUPER_ADMIN or self.can_toggle_restart_staging

    @property
    def can_access_staging(self) -> bool:
        """Vao XEM duoc menu Staging (GET /staging) - True neu co IT NHAT 1 trong 3 quyen
        Staging (can_write_staging: sua replicas, can_restart_staging: Restart,
        can_view_staging: chi xem). KHONG dung property nay de gac hanh dong sua/mutate -
        moi hanh dong PHAI dung dung property rieng cua no (can_write_staging cho Bat/Tat/
        Sync/Tat tat ca/bulk-toggle, can_restart_staging cho Restart), neu khong user chi
        duoc cap 1 trong 3 se dung duoc CA 3 loai hanh dong du bi tuong la gioi han."""
        return self.can_write_staging or self.can_restart_staging or self.can_view_staging

    @property
    def can_turn_off_all_staging(self) -> bool:
        """Nut "Tat tat ca" trong menu Staging: chi Admin/Super Admin, KHONG mo rong cho
        User thuong du da duoc cap can_toggle_staging - thao tac anh huong toan bo app
        nen sieet quyen hon thao tac tung app le. LUU Y: day CHI la dieu kien theo ROLE -
        caller (router/template) PHAI kem THEM can_write_staging (property nay khong tu
        bao gom quyen ghi, vd Admin CHI duoc cap can_view_staging/can_restart_staging -
        khong duoc cap can_write_staging - van thoa dieu kien role o day nhung KHONG duoc
        phep bam Tat tat ca)."""
        return self.role in (Role.ADMIN, Role.SUPER_ADMIN)

    @property
    def can_write_production(self) -> bool:
        """SUA REPLICAS duoc o menu Production - tuong tu can_write_staging, DOC LAP hoan
        toan (2 quyen tach biet)."""
        return self.role == Role.SUPER_ADMIN or self.can_toggle_production

    @property
    def can_restart_production(self) -> bool:
        """Bam duoc nut Restart o menu Production - tuong tu can_restart_staging, DOC LAP
        hoan toan."""
        return self.role == Role.SUPER_ADMIN or self.can_toggle_restart_production

    @property
    def can_access_production(self) -> bool:
        """Vao XEM duoc menu Production (GET /production) - tuong tu can_access_staging,
        True neu co it nhat 1 trong 3 quyen Production (can_write_production/
        can_restart_production/can_view_production). KHONG dung de gac hanh dong sua/mutate
        (xem docstring can_access_staging)."""
        return self.can_write_production or self.can_restart_production or self.can_view_production


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_name: Mapped[str] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    projects: Mapped[list["Project"]] = relationship(back_populates="group", cascade="all, delete-orphan")
    granted_users: Mapped[list["User"]] = relationship(secondary=group_admin_access, back_populates="granted_groups")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    application_name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    group: Mapped[Group] = relationship(back_populates="projects")
    granted_users: Mapped[list["User"]] = relationship(secondary=project_admin_access, back_populates="granted_projects")


class CatalogSetting(Base):
    """Bang singleton (chi 1 dong, id co dinh) luu cau hinh runtime cho catalog - hien tai
    chi co auto_sync_enabled, doc lap voi Settings tu .env vi can bat/tat duoc ngay tu UI
    ma khong phai restart app."""

    __tablename__ = "catalog_setting"

    SINGLETON_ID = 1

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    auto_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class EnvAppCache(Base):
    """Cache danh sach app + replicas hien tai cho tinh nang Bat/Tat staging + thay doi
    replicas Production - dong bo tu file YAML tren git (staging_service.sync_apps/
    production_service.sync_apps), GIONG HET co che catalog Group/Project
    (catalog_service.sync_catalog_from_chart_repo): dong bo dinh ky (scheduler) + nut
    "Lam moi" thu cong, cac trang chi doc tu bang nay (nhanh, khong goi git luc load
    trang) thay vi fetch git moi lan xem trang (cham, tung la thiet ke ban dau).

    `env` phan biet "staging"/"production" (dung 1 bang chung, khac catalog Group/Project
    vi 2 moi truong co the co danh sach app khac nhau tren 2 file/nhanh khac nhau)."""

    __tablename__ = "env_app_cache"
    __table_args__ = (UniqueConstraint("env", "project", "application", name="uq_env_app_cache_env_project_app"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    env: Mapped[str] = mapped_column(String(20))
    project: Mapped[str] = mapped_column(String(255))
    application: Mapped[str] = mapped_column(String(255))
    replicas: Mapped[str] = mapped_column(String(20), default="0")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppSetting(Base):
    """Bang singleton (chi 1 dong, id co dinh) luu cac gia tri cau hinh ma Super Admin
    can sua truc tiep tu UI (/settings/general) thay vi phai sua .env + restart app.
    Dong 1 duoc seed tu Settings (.env) khi tao lan dau; sau do DB la nguon that su,
    .env chi con la gia tri mac dinh luc seed va cho cac field KHONG nam trong bang nay:
    - Token/credential nhay cam (ARGOCD_TOKEN, TELEGRAM_BOT_TOKEN, SMTP_PASSWORD, ...).
    - AUTH_DEV_MODE/AUTH_ENABLE_PASSWORD: cong tac anh huong truc tiep co che xac thuc,
      khong phai config van hanh thong thuong.
    - ARGOCD_URL_API: neu sua duoc qua UI trong khi ARGOCD_TOKEN van la secret, day se
      thanh kenh tu dong ro ri Bearer token toi bat ky URL nao duoc dien vao.

    Rieng git_remote_repo_url la ngoai le duoc chap nhan rui ro tuong tu (URL sua duoc
    qua UI trong khi GIT_REMOTE_CREDENTIALS van la secret) - da trao doi va xac nhan
    voi nguoi yeu cau tinh nang."""

    __tablename__ = "app_setting"

    SINGLETON_ID = 1

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    catalog_charts_dir: Mapped[str] = mapped_column(String(255), default="charts")
    catalog_excluded_dirs: Mapped[str] = mapped_column(Text, default="")
    chart_values_file_template: Mapped[str] = mapped_column(
        String(255), default="charts/{project}/{application}/values-production.yaml"
    )
    rollback_allowed_seconds: Mapped[int] = mapped_column(Integer, default=259200)

    max_concurrent_running_tasks: Mapped[int] = mapped_column(Integer, default=0)
    """So task duoc phep o trang thai Running CUNG LUC tren toan he thong. THAY DOI Y NGHIA
    kem theo tinh nang hang doi (xem TaskStatus.QUEUED): Run/bulk Run/Rollback/scheduler
    auto-deploy giờ CHỈ đưa task vào QUEUED ngay lập tức (không kiểm tra giới hạn này nữa,
    xem task_service.run_task/rollback_task) - gioi han duoc worker nen (app/scheduler.py::
    job_process_task_queue, goi task_service.process_task_queue) AP DUNG khi quyet dinh moi
    lan lay toi da bao nhieu task QUEUED de CAS sang RUNNING (FIFO theo id). Mac dinh 0 =
    KHONG gioi han. Validate server-side o settings_router.update_general (KHONG chi tin
    min cua input HTML): phai la so nguyen >= 0. UI (settings_general.html) hien thi field
    nay trong card "Task" (section="rollback" - giu nguyen key gui len server, chi doi nhan
    hien thi, xem settings_router.SECTION_LABELS)."""

    session_remember_hours: Mapped[int] = mapped_column(Integer, default=168)
    """So gio song cua cookie session khi user tick 'Ghi nho dang nhap' luc login,
    gioi han doc tu .env qua Settings.session_remember_min_hours/max_hours (mac dinh
    24..168 = 1 tuan) - validate server-side o settings_router.update_general (KHONG
    chi tin min/max cua input HTML). Khac voi cac field int khac trong bang nay,
    gia tri nay duoc doc lai tai thoi diem set cookie trong app.auth.RememberMeMiddleware
    (goi get_app_setting() moi request) thay vi Settings.session_remember_hours co dinh
    luc app khoi dong - vi vay Super Admin doi qua UI co hieu luc ngay cho session moi,
    khong can restart app (xem them ghi chu trong RememberMeMiddleware)."""

    argocd_url_application: Mapped[str] = mapped_column(String(255), default="")
    argocd_application_postfix: Mapped[str] = mapped_column(String(255), default="")
    """Dung rieng cho tinh nang Running/DeployTask (scheduler.py) - CHI theo doi 1 moi
    truong duy nhat, KHONG lien quan nut Restart (xem 2 cot ben duoi)."""
    argocd_application_postfix_staging: Mapped[str] = mapped_column(String(255), default="")
    argocd_application_postfix_production: Mapped[str] = mapped_column(String(255), default="")
    """Hau to ten Application ArgoCD rieng cho nut Restart o /staging va /production
    (restart_service.py/argocd_service.py::restart_workload) - tach biet voi
    argocd_application_postfix o tren vi Staging/Production co the tro toi 2 moi truong
    ArgoCD khac nhau (vd non-prod ArgoCD anh xa Staging -> app hau to "-dev", Production
    -> app hau to "-sandbox") - dung 1 hau to chung se lam ca 2 nut Restart cung tac dong
    vao 1 app (bug thuc te gap phai, xem incident 2026-08-18)."""
    restart_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60)
    """Cooldown (giay) giua 2 lan Restart CUNG 1 app (khoa theo env/project/application) -
    xem app/services/restart_service.py::_check_restart_rate_limit. Sua duoc qua UI
    Settings > General > ArgoCD - mac dinh 60 (tang tu 30 ban dau theo yeu cau thuc te,
    xem incident 2026-08-18)."""

    telegram_chat_id_system: Mapped[str] = mapped_column(String(255), default="")

    enable_mail: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_scheduler: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_30min_reminder: Mapped[bool] = mapped_column(Boolean, default=False)
    running_alert_after_minutes: Mapped[int] = mapped_column(Integer, default=15)
    running_alert_repeat_minutes: Mapped[int] = mapped_column(Integer, default=30)
    """Khoang cach toi thieu (phut) giua 2 lan canh bao lap lai CHO CUNG 1 task dang
    RUNNING qua lau (xem scheduler.py::job_check_running_too_long), dua tren
    DeployTask.last_alert_at. Gia tri 0 = CHI canh bao MOT LAN duy nhat, khong lap lai
    (khong spam kenh Telegram cho toi khi co nguoi xu ly). Mac dinh 30 (khac voi 5 phut
    chu ky quet cua job) de tranh spam nhung van nhac lai deu dan. Validate server-side o
    settings_router.update_general: phai la so nguyen >= 0. UI (settings_general.html)
    hien thi field nay trong card "Task" cung rollback_allowed_seconds/
    max_concurrent_running_tasks."""

    git_commit_author_email: Mapped[str] = mapped_column(String(255), default="form-deploy@g-pay.vn")
    git_commit_author_name: Mapped[str] = mapped_column(String(255), default="form-deploy@g-pay.vn")
    git_remote_repo_url: Mapped[str] = mapped_column(String(255), default="")
    git_branch_staging: Mapped[str] = mapped_column(String(255), default="staging")
    """Nhanh git dung cho repo staging (chi de verify commit) - doc boi
    git_service.ensure_staging_repo() qua get_app_setting(), thay cho global env
    Settings.git_branch_staging cu."""
    git_branch_master: Mapped[str] = mapped_column(String(255), default="master")
    """Nhanh git dung cho repo master (sua YAML + push luc Run/Rollback) - doc boi
    git_service.ensure_master_repo() qua get_app_setting(), thay cho global env
    Settings.git_branch_master cu."""
    """URL repo Helm charts KHONG chua credential (vd https://gitlab.example.com/devops/helm/applications.git)
    - credential rieng nam trong GIT_REMOTE_CREDENTIALS (.env), duoc ghep vao luc dung
    (xem git_service._build_authenticated_url)."""

    mail_from: Mapped[str] = mapped_column(String(255), default="form-deploy@g-pay.vn")
    mail_to: Mapped[str] = mapped_column(Text, default="")
    mail_subject: Mapped[str] = mapped_column(String(255), default="[form-deploy] Deploy notification")

    staging_values_file_path: Mapped[str] = mapped_column(String(255), default="envValues/values-staging.yaml")
    """Duong dan (tuong doi trong repo helm charts) toi file YAML dung chung nhieu app,
    cau truc {project}: {application}: replicas: "N" - sua boi tinh nang Bat/Tat staging,
    KHAC voi chart_values_file_template (template rieng tung app cho luong deploy)."""
    production_values_file_path: Mapped[str] = mapped_column(String(255), default="envValues/values-production.yaml")
    """Tuong tu staging_values_file_path nhung cho tinh nang thay doi replicas Production
    (sua tren nhanh master, cung cau truc {project}: {application}: replicas: "N")."""
    telegram_chat_id_staging: Mapped[str] = mapped_column(String(255), default="")
    """Chat id Telegram CHI dung cho thong bao Bat/Tat staging - doc lap voi
    telegram_chat_id_system (luong deploy) VA telegram_chat_id_production (kenh rieng cho
    Production). Dung CHUNG bot token voi luong deploy (settings.telegram_bot_token,
    khong con bot rieng)."""
    telegram_chat_id_production: Mapped[str] = mapped_column(String(255), default="")
    """Chat id Telegram RIENG cho thong bao thay doi replicas Production - dung chung bot
    token voi staging/deploy (settings.telegram_bot_token) nhung gui toi 1 group/channel khac."""
    enable_staging_notify: Mapped[bool] = mapped_column(Boolean, default=True)
    """Cong tac bat/tat gui thong bao Telegram cho kenh staging - doc lap voi
    enable_production_notify."""
    enable_production_notify: Mapped[bool] = mapped_column(Boolean, default=True)
    """Cong tac bat/tat gui thong bao Telegram cho kenh production - doc lap voi
    enable_staging_notify."""
    production_use_merge_request: Mapped[bool] = mapped_column(Boolean, default=False)
    """Neu BAT: thay doi replicas Production KHONG push thang len git_branch_master, ma
    push len 1 nhanh tam + tu dong tao Merge Request (GitLab push options,
    -o merge_request.create) tro ve git_branch_master, cho nguoi duyet thu cong truoc khi
    ap dung that su. Neu TAT (mac dinh): push thang nhu cu. CHI anh huong Production -
    Staging luon push thang, khong co tuy chon nay."""


class DeployTask(Base):
    __tablename__ = "deploy_task"
    __table_args__ = (
        # (status, id): worker hang doi (task_service._promote_queue_candidate_ids) filter
        # status='Queued' ORDER BY id ASC MOI 5 GIAY (job_process_task_queue), cong them
        # cac job scheduler khac filter theo status (Running/Approved/khong phai terminal)
        # va dashboard /home dem so luong theo tung status - bang nay tich luy lich su nen
        # phan lon row se o status cuoi (Done/Rejected/Cancelled), index rat chon loc cho
        # cac status "dang hoat dong" (Queued/Running/Approved) chi chiem so nho.
        Index("ix_deploy_task_status_id", "status", "id"),
        # (project, application, status): ho tro check trung commit luc tao task
        # (task_service.create_task) va list_rollback_candidates (project+application+
        # status=Done, order by id desc limit 5).
        Index("ix_deploy_task_project_application_status", "project", "application", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project: Mapped[str] = mapped_column(String(255))
    application: Mapped[str] = mapped_column(String(255))
    commitid: Mapped[str] = mapped_column(String(7))
    config_env: Mapped[str | None] = mapped_column(Text, nullable=True)
    updatefor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.TASK)

    email: Mapped[str] = mapped_column(String(255))
    maintainer_confirmed: Mapped[str | None] = mapped_column(String(255), nullable=True)
    maintainer_run: Mapped[str | None] = mapped_column(String(255), nullable=True)

    confirmed_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    """Thoi diem gui canh bao "Running qua lau" gan nhat cho task nay (xem
    scheduler.py::job_check_running_too_long, dung chung cho ca 2 loai canh bao "not
    match version" va "chua Healthy/Synced") - dung de giai cach lan canh bao lap lai
    tiep theo it nhat AppSetting.running_alert_repeat_minutes phut, tranh spam Telegram
    moi 5 phut/lan cho 1 task treo lau. PHAI reset ve None moi khi task ROI khoi RUNNING
    (Done/Rejected/Cancelled/tra ve trang thai truoc do) de lan RUNNING sau (deploy/
    rollback moi) khong bi anh huong boi lich su canh bao cu - xem cac diem CAS status
    khoi RUNNING trong task_service.py va job_check_running_done o tren."""

    auto_deploy: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    notify_status: Mapped[int] = mapped_column(Integer, default=0)

    queued_from_status: Mapped[TaskStatus | None] = mapped_column(Enum(TaskStatus), nullable=True)
    """CHI khac None trong luc task dang o QUEUED, danh cho task Run binh thuong (KHONG
    phai Rollback - xem rollback_target_task_id): luu lai status TRUOC do (Task hoac
    Approved, xem task_service.RUNNABLE_STATUSES) tai thoi diem CAS sang QUEUED, de worker
    (task_service.run_queued_task) co the TRA VE dung trang thai nay neu git that bai luc
    toi luot chay (giu dung hanh vi "khong doi trang thai khi Run loi" nhu truoc khi co
    hang doi, dù buoc CAS->QUEUED va buoc thuc su chay git nay gio cach nhau ve thoi gian/
    request). Duoc xoa lai (set None) ngay khi worker chay THANH CONG."""

    rollback_target_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """CHI khac None neu dong nay la 1 task MOI duoc tao boi task_service.rollback_task
    (status ban dau QUEUED) - luu id cua task DA DUOC CHON de rollback ve (task nguon xem
    trong updatefor). Worker (task_service.run_queued_task) dung cot nay de PHAN BIET: khac
    None -> goi git_service.perform_rollback (dung luon self.image_version da duoc copy tu
    target luc INSERT, KHONG re-verify commit tren staging); None -> goi
    git_service.perform_run (task Run/bulk-Run/auto-deploy binh thuong). CO CHU DICH KHONG
    khai bao ForeignKey("deploy_task.id") o day: task DICH (vd task Done qua cu) co the bi
    Super Admin xoa vinh vien (delete_task, cho phep xoa task Done) trong luc dong rollback
    nay van dang QUEUED cho toi luot - 1 FK constraint that su se chan thao tac xoa do hoac
    doi hoi ON DELETE phuc tap khong can thiet, trong khi cot nay chi mang tinh tham khao/
    audit (gia tri can thiet de push - project/application/image_version - da duoc COPY
    SAN vao chinh dong nay tu luc tao, KHONG can query lai target lien ket qua cot nay)."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def is_rollback_window_open(self, rollback_allowed_seconds: int) -> bool:
        """Nut Rollback chi con hien trong vong N giay ke tu luc Done (cau hinh qua
        AppSetting.rollback_allowed_seconds, sua duoc tu UI /settings/general) - deploy
        qua cu se an han nut di, tranh rollback nham ve 1 version qua xa, khong con phu
        hop nua."""
        if self.done_at is None:
            return False
        return datetime.utcnow() - self.done_at <= timedelta(seconds=rollback_allowed_seconds)


class RestartLog(Base):
    """Lich su bam nut "Restart" (khoi dong lai workload Deployment/StatefulSet qua ArgoCD
    API, xem app/services/argocd_service.py::restart_workload) tren /staging va
    /production - GHI 1 dong MOI LAN bam, du thanh cong hay that bai (khac EnvAppCache chi
    luu trang thai HIEN TAI, bang nay la audit log tang dan, khong ghi de/xoa).

    KHONG anh huong cot `replicas`/badge "Trang thai" cua EnvAppCache - Restart la hanh
    dong RIENG BIET (khoi dong lai pod voi cung so replicas dang co, khong doi so luong),
    xem docstring restart_workload() cho co che that (goi API "Resource Actions" co san
    cua ArgoCD, action "restart", KHAC voi PatchResource ban dau da bi 403 tren
    production do khong khop RBAC - xem incident 2026-08-18).

    `user_id` de nullable (khong bat buoc FK) vi day la bang audit-only: neu sau nay co
    tinh nang xoa User that su, log restart cu (bang chung ai da bam) khong nen bi mat/
    chan xoa theo - `user_email` moi la nguon doc chinh khi hien thi lich su, luon co san
    (copy truc tiep tu user.email luc ghi, KHONG doc qua relationship)."""

    __tablename__ = "restart_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    env: Mapped[str] = mapped_column(String(20))
    """"staging" hoac "production" - dung chung 1 bang cho ca 2 menu, giong quy uoc
    EnvAppCache.env."""
    project: Mapped[str] = mapped_column(String(255))
    application: Mapped[str] = mapped_column(String(255))
    workload_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    """"Deployment" hoac "StatefulSet" - None neu that bai TRUOC khi xac dinh duoc loai
    workload (vd khong tim thay resource nao tren ArgoCD resource-tree, xem
    restart_service.py)."""
    workload_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    user_email: Mapped[str] = mapped_column(String(255))

    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
