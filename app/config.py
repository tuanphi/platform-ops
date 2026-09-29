import logging
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

DEFAULT_SECRET_KEY = "change-me-in-prod"
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_url: str = "http://localhost:8000"
    secret_key: str = DEFAULT_SECRET_KEY

    database_url: str = "sqlite:///./form_deploy.db"

    # Auth
    auth_dev_mode: bool = False  # cho phep dang nhap bang email khi chua co Google OAuth - CHI danh cho dev local
    auth_enable_password: bool = False  # cho phep dang nhap bang email+password (doc lap voi Google/dev mode)
    session_remember_hours: int = 168  # so gio session song khi user tick "Ghi nho dang nhap" (168h = 7 ngay)
    session_remember_min_hours: int = 24  # nguong min cho o "so gio remember" o Settings > General - env-only, khong len UI
    session_remember_max_hours: int = 168  # nguong max (168h = 1 tuan) - dong thoi la tran cung Max-Age cua SessionMiddleware, xem app/main.py
    google_client_id: str = ""
    google_client_secret: str = ""
    google_callback_url: str = "http://localhost:8000/auth/google/callback"

    # Git repo Helm charts
    git_remote_credentials: str = ""  # dang "user:token", chi phan authen - URL repo sua o Setting > General
    git_directory_applications_staging: str = "./data/repo-staging"
    git_directory_applications_master: str = "./data/repo-master"
    git_branch_staging: str = "staging"
    git_branch_master: str = "master"
    git_commit_author_email: str = "form-deploy@g-pay.vn"
    git_commit_author_name: str = "form-deploy@g-pay.vn"
    catalog_charts_dir: str = "charts"
    catalog_excluded_dirs: str = "redis,rabbitmq,kafka,mqtt-proxy,standard,ambassador,temporal"  # phan cach boi dau phay
    chart_values_file_template: str = "charts/{project}/{application}/values-production.yaml"
    catalog_auto_sync_interval_minutes: int = 30  # chu ky chay job auto sync catalog khi da bat qua UI /catalog
    rollback_allowed_seconds: int = 259200  # sau bao nhieu giay ke tu luc Done thi nut Rollback bi an (mac dinh 3 ngay)
    staging_values_file_path: str = "envValues/values-staging.yaml"  # duong dan file YAML dung chung nhieu app cho tinh nang Bat/Tat staging
    production_values_file_path: str = "envValues/values-production.yaml"  # tuong tu, cho tinh nang thay doi replicas Production (nhanh master)
    production_use_merge_request: bool = False  # BAT: thay doi Production tao Merge Request (GitLab push option) thay vi push thang
    git_webhook_secret: str = ""  # xac thuc GitLab push webhook (header X-Gitlab-Token) - trigger sync catalog/staging/production ngay khi co push, khong cho scheduler dinh ky - secret, env-only

    # ArgoCD
    argocd_url_api: str = ""  # vd: https://argocd.example.com/api/v1/applications/
    argocd_url_application: str = ""  # vd: https://argocd.example.com/applications/
    argocd_token: str = ""
    argocd_application_postfix: str = ""  # dung rieng cho tinh nang theo doi Running (DeployTask/scheduler), KHONG dung cho nut Restart
    # Hau to ten Application ArgoCD RIENG cho tung moi truong, dung boi nut "Restart" o
    # /staging va /production (app/services/argocd_service.py::restart_workload) - vd
    # ArgoCD non-prod co the anh xa Staging -> "-dev", Production -> "-sandbox". KHAC voi
    # argocd_application_postfix o tren (dung chung 1 postfix cho tinh nang Running cu, gia
    # dinh chi theo doi 1 moi truong duy nhat).
    argocd_application_postfix_staging: str = ""
    argocd_application_postfix_production: str = ""
    # Cooldown (giay) giua 2 lan Restart CUNG 1 app (env/project/application) - xem
    # app/services/restart_service.py::_check_restart_rate_limit. Sua duoc qua UI
    # Settings > General > ArgoCD, gia tri nay chi la mac dinh luc seed lan dau.
    restart_cooldown_seconds: int = 60

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id_system: str = ""
    telegram_webhook_secret: str = ""
    telegram_chat_id_staging: str = ""
    telegram_chat_id_production: str = ""
    enable_staging_notify: bool = True
    enable_production_notify: bool = True

    # Mail
    enable_mail: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = "form-deploy@g-pay.vn"
    mail_to: str = ""  # phan cach boi dau phay
    mail_subject: str = "[form-deploy] Deploy notification"

    # Scheduler
    enable_scheduler: bool = True
    enable_30min_reminder: bool = False  # tuong duong job bi tat trong ban goc
    running_alert_after_minutes: int = 15

    def is_local_app_url(self) -> bool:
        """Heuristic phat hien moi truong dev local: APP_URL tro ve localhost/127.0.0.1/...
        Dung de quyet dinh co cho phep AUTH_DEV_MODE/SECRET_KEY mac dinh hay khong - KHONG
        phai 100% chinh xac (vd reverse proxy) nhung du an khong co bien ENVIRONMENT rieng
        nen day la tin hieu ro rang nhat dang co san."""
        hostname = urlparse(self.app_url).hostname or ""
        return hostname.lower() in _LOCAL_HOSTNAMES

    def _looks_like_local_env(self) -> bool:
        """2 tin hieu DOC LAP deu phai dung moi coi la local: APP_URL tro localhost VA
        DATABASE_URL la sqlite (production trong du an nay luon dung MySQL - xem docstring
        DATABASE_URL trong .env.example). Chi dua vao is_local_app_url() la single point
        of failure: quen sua APP_URL khi deploy that (van con mysql+pymysql://...) se vo
        hieu ca 2 chot an toan ben duoi cung luc; them dieu kien DATABASE_URL giam rui ro
        nay vi 2 bien phai CUNG bi cau hinh sai thi moi bypass duoc."""
        if not self.is_local_app_url():
            return False
        return self.database_url.strip().lower().startswith("sqlite")

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> "Settings":
        """Fail-fast (thay vi fail-open) khi cau hinh nguy hiem duoc bat trong moi truong
        nghi la production - tranh lap lai loi AUTH_DEV_MODE mac dinh bat + SECRET_KEY
        hardcode ma van chay binh thuong khong canh bao gi."""
        if self._looks_like_local_env():
            return self

        if self.auth_dev_mode:
            logger.error(
                "AUTH_DEV_MODE=true nhung APP_URL='%s' khong phai localhost - nghi la "
                "production, dev-login cho phep dang nhap bang bat ky email nao (mao danh "
                "admin/super_admin co san) la lo hong nghiem trong. Tu choi khoi dong.",
                self.app_url,
            )
            raise ValueError(
                "AUTH_DEV_MODE khong duoc bat khi APP_URL khong phai localhost/127.0.0.1 "
                "(nghi la production) - sua APP_URL hoac tat AUTH_DEV_MODE."
            )

        if self.secret_key == DEFAULT_SECRET_KEY:
            logger.error(
                "SECRET_KEY dang la gia tri mac dinh khong an toan ('%s') trong khi "
                "APP_URL='%s' khong phai localhost. Tu choi khoi dong.",
                DEFAULT_SECRET_KEY,
                self.app_url,
            )
            raise ValueError(
                "SECRET_KEY phai duoc dat gia tri rieng (khong dung default "
                f"'{DEFAULT_SECRET_KEY}') khi APP_URL khong phai localhost/127.0.0.1."
            )

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
