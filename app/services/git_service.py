"""Thao tác trực tiếp trên repo Helm charts qua git CLI (subprocess).

Hai working directory tách biệt: staging (chỉ để verify commit) và master
(để sửa YAML + push khi Run) - giống kiến trúc bản gốc.
"""

import logging
import re
import subprocess
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Timeout (giay) khi xin file lock cho cac thao tac dong bo TRUC TIEP tren duong di cua
# 1 request nguoi dung (Run/Rollback push len master, verify commit tren staging) - CO
# CHU DICH RAT NGAN (fail-fast) thay vi block lau tren chinh thread dang xu ly request
# (app chay 1 process uvicorn, KHONG --workers, moi route sync `def` deu chia chung 1
# threadpool cua Starlette). Ban vá truoc dung 1 timeout duy nhat = 90s cho MOI loai lock,
# nghia la khi tranh chap, request bi giu tren thread request toi 90s - tu no lai tao ra 1
# lo hong MEDIUM moi (1 user spam nhieu Run/Rollback dong thoi la du chiem het threadpool
# dung chung, treo ca cac trang/action khac cua toan bo app). Nay doi sang: cho ngan (vai
# giay), khong lay duoc thi tra loi NGAY cho nguoi dung biet he thong dang ban va tu thu
# lai, thay vi giu thread cho hang chuc giay. Tinh dung dan cua serialize (2 Run khong the
# xen ke nhau) khong doi - chi doi hanh vi KHI TRANH CHAP tu "cho lau" sang "fail nhanh".
_GIT_LOCK_TIMEOUT_SECONDS = 2

# Timeout (giay) rieng, DAI HON, CHI danh cho cong viec chay NEN/it khi thuc su tranh chap
# truc tiep voi 1 request nguoi dung dang cho phan hoi ngay (catalog auto-sync qua
# APScheduler job_auto_sync_catalog, hoac nut "Sync catalog" thu cong - ca 2 deu dung
# staging_repo_lock(background=True), xem catalog_service.sync_catalog_from_chart_repo).
# Thao tac nay tu no co the mat vai giay (fetch + reset + clean + quet toan bo cay thu
# muc charts/) nen khong nen ap dung timeout fail-fast 2s nhu tren (se fail vo co ngay ca
# khi khong co ai dang cho no ca) - nhung van gioi han o muc vua phai (khong dung lai
# _GIT_LOCK_TIMEOUT_SECONDS goc = 90s) de khong vo tinh giu lock staging qua lau, lam
# check_commit_on_staging (Create/Run cua nguoi dung, cung dung chung lock staging) phai
# cho/fail-fast lien tuc trong luc sync dang chay.
_GIT_LOCK_TIMEOUT_BACKGROUND_SECONDS = 30

# Timeout (giay) rieng cho worker nen xu ly hang doi Run/Rollback (app/scheduler.py::
# job_process_task_queue, goi task_service.process_task_queue/run_queued_task) - CUNG
# GIA TRI voi _GIT_LOCK_TIMEOUT_BACKGROUND_SECONDS nhung tach hang so rieng (thay vi dung
# chung) vi 2 y nghia khac nhau: cai tren danh cho catalog auto-sync (thao tac doc, khong
# tranh chap voi Run/Rollback), con cai nay danh cho CHINH duong Run/Rollback (trước day
# dung _GIT_LOCK_TIMEOUT_SECONDS fail-fast vi chay tren thread request nguoi dung - nay
# chay tren worker nen KHONG con giu thread request nao nen doi sang cho lau hon thay vi
# fail-fast). Tach rieng de sau nay neu can chinh 1 trong 2 gia tri doc lap (vd catalog sync
# can nhanh hon/cham hon worker) khong anh huong lan nhau.
_GIT_LOCK_TIMEOUT_TASK_WORKER_SECONDS = 30


def _lock_file_path(directory: str) -> Path:
    """Dat file lock CANH thu muc checkout (vd data/repo-master.lock), KHONG dat BEN
    TRONG thu muc checkout - vi moi lan _ensure_repo() chay `git clean -fd` se xoa mat
    bat ky file nao khong duoc git track nam trong working tree, khien file lock (dang
    duoc 1 tien trinh khac giu) bi xoa giua chung va mat tac dung khoa."""
    path = Path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.parent / f"{path.name}.lock"


@contextmanager
def _repo_lock(directory: str, label: str, timeout: float | None = None) -> Iterator[None]:
    """Critical section bao ve toan bo chuoi thao tac git tren 1 checkout dung chung
    (fetch/reset/clean -> sua file -> add/commit/push) bang 1 file lock (thu vien
    `filelock`, hoat dong ca giua nhieu worker process/container khac nhau, khong chi
    trong 1 process Python) - dam bao 2 lan Run/Rollback dong thoi KHONG the xen ke nhau
    tren cung 1 thu muc checkout (vd 1 tien trinh dang o giua buoc sua YAML/commit thi
    tien trinh kia chay `reset --hard`/`clean -fd` xoa mat thay doi chua commit, hoac 2
    commit push de len nhau).

    `timeout` mac dinh None -> doc _GIT_LOCK_TIMEOUT_SECONDS (module-level, doc TAI THOI
    DIEM GOI - KHONG bind san lam default argument) de code goi test/monkeypatch hang so
    nay van co tac dung dung nhu truoc. Truyen `timeout` tuong minh (vd
    _GIT_LOCK_TIMEOUT_BACKGROUND_SECONDS) cho cac caller chap nhan cho lau hon (xem
    staging_repo_lock(background=True))."""
    effective_timeout = _GIT_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    lock_path = _lock_file_path(directory)
    lock = FileLock(str(lock_path), timeout=effective_timeout)
    try:
        with lock:
            logger.debug("Da lay duoc git lock '%s' (%s)", lock_path, label)
            yield
    except Timeout as exc:
        logger.error(
            "Khong lay duoc git lock '%s' (%s) sau %ss - co thao tac Run/Rollback/Sync khac dang giu lock",
            lock_path,
            label,
            effective_timeout,
        )
        raise GitError(
            f"Hệ thống đang bận xử lý một thao tác Git khác trên '{label}' "
            "(Run/Rollback/Sync catalog), vui lòng thử lại sau ít giây"
        ) from exc


COMMIT_MESSAGE_RE = re.compile(r"(.*)/(.*)/(.*): Update image version to (.*)")

# Che "user:token@" trong URL truoc khi dua vao bat ky exception/log message nao - GIT_REMOTE_CREDENTIALS
# duoc chen thang vao URL luc goi git CLI (xem _build_authenticated_url), neu lenh that bai thi
# stderr/args co the chua nguyen van URL kem credential.
_CREDENTIAL_IN_URL_RE = re.compile(r"://[^/@\s]+@")

# Whitelist ky tu an toan cho project/application truoc khi dua vao
# chart_values_file_template.format(...) roi ghep thanh duong dan file that su tren dia -
# chi cho chu/so/-/_/. , chan het "/" va ".." de khong the path-traversal ra ngoai master_dir.
_SAFE_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# Whitelist ky tu an toan cho ten nhanh git (git_branch_staging/git_branch_master, nhap qua
# UI Settings > General va luu trong AppSetting) truoc khi dua vao bat ky lenh git CLI nao
# (ls-remote/clone/fetch/reset) - chan CWE-88 (git argument/option injection): 1 "ten nhanh"
# bat dau bang "-" (vd "--upload-pack=/bin/touch /tmp/pwned") se bi git parse nham thanh
# option thay vi 1 refname, co the dan toi RCE (tuong tu CVE-2017-1000117). Ky tu dau BAT
# BUOC la chu/so (khong duoc la "-" hay "."), cac ky tu sau cho phep them "." "_" "/" "-" de
# hop le voi ten nhanh dang "release/staging"/"feature/x".
# Neo cuoi dung "\Z" (khong phai "$") - trong Python, "$" (khi KHONG bat co re.MULTILINE)
# van khop truoc 1 ky tu "\n" o cuoi chuoi, khien vd "master\n" lot qua duoc regex nay (chi
# bi chan boi cac dieu kien rieng ben duoi validate_safe_branch_name, vd ".." - nhung "\n" tu
# no khong nam trong bat ky dieu kien chan nao khac). "\Z" chi khop DUNG tai vi tri cuoi cung
# that su cua chuoi, khong co ngoai le voi "\n" cuoi.
_SAFE_GIT_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

# Whitelist scheme an toan cho git_remote_repo_url (sua duoc qua UI Settings > General) -
# dung dinh dang duy nhat he thong ho tro thuc te: URL "https://host/path.git" (khong kem
# credential - xem .env.example va _url_has_credentials), boi _build_authenticated_url luon
# gia dinh URL co "://" de chen "user:token@" ngay sau scheme. Khong whitelist "http://"
# (khong dung trong .env.example/thuc te, giu chat de khong mo rong be mat tan cong), khong
# whitelist dang scp "git@host:path" (khong co "://" nen se lam _build_authenticated_url
# chen credential sai vi tri, khong thuc su duoc he thong ho tro).
_SAFE_REPO_URL_SCHEMES = ("https://", "ssh://")


def redact_credentials(text: str) -> str:
    """Public alias - dung o cac noi khac (vd catalog_router khi hien loi ra UI) lam
    lop phong thu thu 2, phong truong hop 1 exception khong phai GitError cung chua
    URL kem credential."""
    return _CREDENTIAL_IN_URL_RE.sub("://***@", text)


def _redact_credentials(text: str) -> str:
    return redact_credentials(text)


class GitError(RuntimeError):
    pass


def validate_safe_path_segment(value: str, field_name: str) -> None:
    """Chan ky tu nguy hiem (`/`, `..`, khoang trang, ky tu dac biet) trong project/application
    truoc khi dua vao chart_values_file_template.format(...) - khong lam vay, 1 gia tri nhu
    `../../etc/passwd` hoac chua `/` co the ghi de file ngoai y muon tren dia khi ghep path."""
    if not value or not _SAFE_PATH_SEGMENT_RE.match(value) or ".." in value:
        raise GitError(f"Giá trị '{field_name}' chứa ký tự không hợp lệ: '{value}'")


def validate_safe_branch_name(value: str, field_name: str) -> None:
    """Chan ky tu nguy hiem trong ten nhanh git (git_branch_staging/git_branch_master, sua
    duoc qua UI Settings > General) truoc khi dua vao subprocess git ls-remote/clone/fetch/
    reset - khong lam vay, 1 "ten nhanh" bat dau bang "-" co the bi git hieu nham thanh 1
    option CLI (vd "--upload-pack=...") dan toi RCE. Khong log gia tri `value` (loi raise ra
    day CHUA nguyen van ten nhanh do nguoi dung nhap, khong chua credential nen an toan de
    hien thi cho Super Admin, nhung KHONG duoc ghi vao log o cac diem goi ham nay)."""
    if (
        not value
        or not _SAFE_GIT_BRANCH_RE.match(value)
        or ".." in value
        or "//" in value
        or value.endswith("/")
        or value.endswith(".lock")
    ):
        raise GitError(f"Tên nhánh '{field_name}' không hợp lệ: '{value}'")


def validate_safe_repo_url(value: str, field_name: str) -> None:
    """Chan CWE-88 (git argument/option injection) va scheme nguy hiem trong
    git_remote_repo_url (sua duoc qua UI Settings > General, luu trong AppSetting) truoc khi
    dua vao bat ky lenh git CLI nao (clone/remote set-url/ls-remote) - _url_has_credentials
    (settings_router) chi chan URL chua credential dang "user:token@host", KHONG chan duoc 1
    URL bat dau bang "-" (bi git parse nham thanh option, vd "--upload-pack=/bin/sh ...", RCE
    tuong tu CVE-2017-1000117) hay scheme cuc bo/nguy hiem (vd "file:///etc/passwd",
    "ext::sh -c ...") co the doc file/thuc thi lenh tuy y tren server ngay ca khi khong co
    credential nao trong URL. Khong log gia tri `value` (URL o day KHONG con credential do da
    qua _url_has_credentials/duoc doc tu AppSetting, nhung van redact cho nhat quan voi cac
    GitError khac trong module)."""
    if not value or value[0] == "-" or "\n" in value or "\r" in value:
        raise GitError(f"Repo URL ({field_name}) không hợp lệ: '{redact_credentials(value)}'")
    if not value.startswith(_SAFE_REPO_URL_SCHEMES):
        raise GitError(
            f"Repo URL ({field_name}) phải bắt đầu bằng https:// hoặc ssh:// - nhận được: "
            f"'{redact_credentials(value)}'"
        )


def _run(args: list[str], cwd: str) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitError(_redact_credentials(f"git {' '.join(args)} failed: {result.stderr.strip()}"))
    return result.stdout


def _build_authenticated_url(repo_url: str, credentials: str) -> str:
    """Chen 'user:token@' ngay sau scheme cua repo_url (sua duoc qua UI) - credential
    rieng nam trong GIT_REMOTE_CREDENTIALS (.env), khong bao gio luu chung voi URL."""
    scheme, _, rest = repo_url.partition("://")
    return f"{scheme}://{credentials}@{rest}"


def _ensure_repo(directory: str, branch: str, repo_url: str) -> None:
    """Dam bao working directory luon khop CHINH XAC voi remote truoc moi lan dung.

    Dung fetch + reset --hard (khong phai git pull/merge) vi remote co the bi
    force-push/reset (vd revert lai 1 commit) - pull kieu merge se khong dong bo
    dung theo trang thai moi nhat that su cua remote, con lai local checkout
    "stale" khien buoc so sanh git status sau nay bi sai (tuong file da doi
    trong khi thuc ra remote da bi reset ve truoc do)."""
    # Phong thu chieu sau (lop 2, ngoai buoc validate o settings_router.update_general khi
    # luu branch): ensure_staging_repo/ensure_master_repo doc branch TRUC TIEP tu AppSetting
    # trong DB moi lan goi, khong di qua lai router - neu 1 gia tri khong hop le lot qua duoc
    # buoc luu (vd du lieu cu truoc ban va, hoac sua thang trong DB), van phai chan o day
    # truoc khi dua vao clone/fetch/reset (xem validate_safe_branch_name).
    validate_safe_branch_name(branch, "branch")

    if not repo_url:
        raise GitError("Repo URL (Setting > General) chưa được cấu hình")
    # Phong thu chieu sau (lop 2, ngoai buoc validate o settings_router.update_general khi
    # luu git_remote_repo_url) - xem docstring validate_safe_repo_url.
    validate_safe_repo_url(repo_url, "repo_url")
    if not settings.git_remote_credentials:
        raise GitError("GIT_REMOTE_CREDENTIALS chưa được cấu hình")
    authenticated_url = _build_authenticated_url(repo_url, settings.git_remote_credentials)

    path = Path(directory)
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            # "--" danh dau ket thuc option, dam bao authenticated_url/str(path) luon duoc
            # git hieu la tham so vi tri (URL/duong dan), khong bao gio bi parse nham thanh
            # option CLI du gia tri co bat dau bang "-" hay khong (defense-in-depth, dung
            # cung voi validate_safe_repo_url o tren).
            ["git", "clone", "-b", branch, "--", authenticated_url, str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitError(_redact_credentials(f"git clone failed: {result.stderr.strip()}"))
    else:
        # Cap nhat lai remote moi lan dung, phong truong hop repo_url/credentials vua
        # doi qua UI/secret - tranh dung URL cu con cache trong .git/config.
        _run(["git", "remote", "set-url", "--", "origin", authenticated_url], cwd=str(path))
    _run(["git", "fetch", "--", "origin", branch], cwd=str(path))
    _run(["git", "reset", "--hard", f"origin/{branch}"], cwd=str(path))
    _run(["git", "clean", "-fd"], cwd=str(path))


def remote_branch_exists(repo_url: str, branch: str) -> bool:
    """Kiểm tra 1 nhánh có tồn tại trên remote hay không, dùng `git ls-remote --heads`
    (không cần clone toàn bộ repo) - gọi khi Super Admin sửa git_branch_staging/master
    qua UI Settings > General (xem settings_router.update_general), tránh lưu nhầm 1 tên
    nhánh không tồn tại khiến Run/Rollback sau này mới phát hiện lỗi (ensure_staging_repo/
    ensure_master_repo lúc đó mới clone/fetch và fail).

    Raise GitError nếu repo_url rỗng, credential chưa cấu hình, mạng lỗi/timeout, hoặc
    lệnh git ls-remote thất bại (KHÔNG được nuốt lỗi rồi trả về True/False mơ hồ - caller
    (router) cần phân biệt rõ "nhánh không tồn tại" (trả False) với "không kiểm tra được"
    (raise, giữ message KHÔNG chứa credential nhờ _redact_credentials)."""
    if not repo_url:
        raise GitError("Repo URL chưa được cấu hình")
    if not branch:
        raise GitError("Tên nhánh không được để trống")
    # Chan CWE-88 (git argument/option injection) NGAY TU DAU - day la ham dau tien nhan
    # branch/repo_url tu input HTTP cua Super Admin (settings_router.update_general goi ham
    # nay de xac nhan nhanh ton tai truoc khi luu), truoc khi ghep vao lenh `git ls-remote`.
    validate_safe_branch_name(branch, "branch")
    validate_safe_repo_url(repo_url, "repo_url")
    if not settings.git_remote_credentials:
        raise GitError("GIT_REMOTE_CREDENTIALS chưa được cấu hình")
    authenticated_url = _build_authenticated_url(repo_url, settings.git_remote_credentials)

    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "--", authenticated_url, branch],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"Kết nối tới remote '{redact_credentials(repo_url)}' quá thời gian chờ (timeout)") from exc

    if result.returncode != 0:
        raise GitError(_redact_credentials(f"git ls-remote failed: {result.stderr.strip()}"))

    expected_ref = f"refs/heads/{branch}"
    for line in result.stdout.splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 2 and parts[1] == expected_ref:
            return True
    return False


def ensure_staging_repo(repo_url: str) -> str:
    # Ten nhanh doc tu AppSetting (sua duoc qua UI /settings/general) thay vi global env
    # Settings.git_branch_staging - get_app_setting() tu mo/dong session rieng vi ham nay
    # duoc goi trong context da giu file lock (staging_repo_lock), khong co san session
    # SQLAlchemy truyen vao.
    from app.services.app_setting_service import get_app_setting

    branch = get_app_setting().git_branch_staging
    _ensure_repo(settings.git_directory_applications_staging, branch, repo_url)
    return settings.git_directory_applications_staging


def ensure_master_repo(repo_url: str) -> str:
    from app.services.app_setting_service import get_app_setting

    branch = get_app_setting().git_branch_master
    _ensure_repo(settings.git_directory_applications_master, branch, repo_url)
    return settings.git_directory_applications_master


def staging_repo_lock(background: bool = False) -> AbstractContextManager[None]:
    """Context manager cong khai bao ve checkout staging dung chung - dung cho bat ky
    noi nao doc/ghi thu muc nay (vd check_commit_on_staging, catalog_service.sync).
    Tach rieng lock voi master (staging_repo_lock/master_repo_lock la 2 file lock khac
    nhau) - Run goi tuan tu check_commit_on_staging() (lay+nha lock staging) roi moi toi
    _apply_version_and_push() (lay+nha lock master), khong bao gio giu ca 2 lock cung
    luc nen khong co rui ro deadlock giua 2 lock nay.

    `background=False` (mac dinh): dung cho cac caller nam TRUC TIEP tren duong di cua 1
    request nguoi dung dang cho phan hoi ngay (check_commit_on_staging, goi tu
    task_service.create_task va git_service.perform_run) - fail-fast (xem
    _GIT_LOCK_TIMEOUT_SECONDS).
    `background=True`: danh cho cong viec chay nen/it khi nguoi dung dang cho ngay ket qua
    (catalog_service.sync_catalog_from_chart_repo, chay ca tu APScheduler job dinh ky lan
    tu nut "Sync catalog" thu cong) - cho lau hon (xem _GIT_LOCK_TIMEOUT_BACKGROUND_SECONDS)
    de khong fail vo co giua chung 1 lan fetch+reset+clean+quet cay thu muc binh thuong."""
    timeout = _GIT_LOCK_TIMEOUT_BACKGROUND_SECONDS if background else None
    return _repo_lock(settings.git_directory_applications_staging, "staging", timeout=timeout)


def master_repo_lock(background: bool = False) -> AbstractContextManager[None]:
    """Context manager cong khai bao ve checkout master dung chung (xem staging_repo_lock).

    `background=False` (mac dinh): fail-fast (_GIT_LOCK_TIMEOUT_SECONDS) - danh cho cac
    caller CON LAI tren duong di truc tiep cua 1 request nguoi dung dang cho phan hoi ngay
    (hien tai: toggle_production_app/bulk_toggle_production_apps qua env_toggle_service -
    tinh nang thay doi replicas Production van dong bo, KHONG di qua hang doi Run/Rollback).
    `background=True`: cho lau hon (_GIT_LOCK_TIMEOUT_TASK_WORKER_SECONDS) - CHI danh cho
    worker xu ly hang doi (task_service.run_queued_task, chay tu APScheduler
    job_process_task_queue) vi luc nay KHONG con giu thread request nao ca (xem
    TaskStatus.QUEUED trong models.py)."""
    timeout = _GIT_LOCK_TIMEOUT_TASK_WORKER_SECONDS if background else None
    return _repo_lock(settings.git_directory_applications_master, "master", timeout=timeout)


@dataclass
class CommitCheckResult:
    status: str  # "ok" | "project_mismatch" | "not_found"
    message: str
    image_version: str | None = None


# Git SHA that su chi gom hex - chan commitid chua ky tu regex dac biet (vd ".*", "|") duoc
# dua thang vao `git log --grep=...` (grep coi la pattern, khong phai chuoi literal) danh lua
# khop bua vao 1 commit message khac thay vi dung yeu cau "biet dung SHA that".
COMMITID_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def check_commit_on_staging(
    project: str, application: str, commitid: str, git_remote_repo_url: str, background: bool = False
) -> CommitCheckResult:
    """Xác minh commit tồn tại trên nhánh staging và đúng project/application đã chọn.

    `background=False` (mac dinh): dung cho duong request nguoi dung (Tao request qua
    task_service.create_task) - fail-fast. `background=True`: dung khi ham nay duoc goi LAI
    (re-verify) tu worker xu ly hang doi (task_service.run_queued_task -> git_service.
    perform_run, chay tu job_process_task_queue) - luc do KHONG con giu thread request nao,
    nen doi sang cho lau hon thay vi fail-fast (xem staging_repo_lock)."""
    if len(commitid) < 7:
        return CommitCheckResult(status="not_found", message="CommitID sai định dạng (cần >= 7 ký tự)")
    if not COMMITID_RE.match(commitid[:7]):
        return CommitCheckResult(status="not_found", message="CommitID sai định dạng (chỉ được chứa ký tự hex)")
    commitid = commitid[:7]

    # Khoa checkout staging dung chung trong luc reset/fetch + doc log - tranh 1 tien
    # trinh khac (vd job sync catalog, hoac 1 lan verify commit khac dang chay song
    # song) reset/clean thu muc nay giua chung khien ket qua doc bi sai lech.
    with staging_repo_lock(background=background):
        staging_dir = ensure_staging_repo(git_remote_repo_url)
        output = _run(["git", "log", f"--grep={commitid}"], cwd=staging_dir)

    if project not in output:
        return CommitCheckResult(status="not_found", message=f"Không tìm thấy commit '{commitid}' cho project '{project}'")

    match = COMMIT_MESSAGE_RE.search(output)
    if not match:
        return CommitCheckResult(status="not_found", message="Không parse được message commit theo format chuẩn")

    matched_project, matched_application, matched_version = match.group(2), match.group(3), match.group(4)

    # So khop CHINH XAC (khong phai substring `in`) - "core" khong duoc phep khop nham
    # voi "core-payments", tranh bypass catalog bang cach chon 1 project/application co
    # ten la substring cua project/application that su duoc phep.
    if project == matched_project and application == matched_application and commitid in matched_version:
        return CommitCheckResult(status="ok", message="ok", image_version=matched_version)

    return CommitCheckResult(
        status="project_mismatch",
        message=(
            f"Tìm thấy project '{project}' nhưng application/commit không khớp "
            f"(commit message: {matched_project}/{matched_application}: ...{matched_version})"
        ),
    )


@dataclass
class RunResult:
    ok: bool
    message: str
    image_version: str | None = None


@dataclass
class ChartPushConfig:
    """Gia tri lay tu AppSetting (sua duoc qua /settings/general), luon di cung nhau
    moi lan verify/sua YAML/commit/push nen gop thanh 1 struct thay vi truyen roi rac."""

    chart_values_file_template: str
    git_commit_author_email: str
    git_commit_author_name: str
    git_remote_repo_url: str


def _apply_version_and_push(
    project: str,
    application: str,
    image_version: str,
    commit_message: str,
    config: ChartPushConfig,
    background: bool = False,
) -> RunResult:
    """Sua dung 1 field version trong file chart cua project/application, commit & push
    len master neu file thuc su doi. Dung chung cho ca Run va Rollback.

    `background=True`: goi tu worker xu ly hang doi (khong con giu thread request nao) -
    dung timeout lock DAI hon (xem master_repo_lock)."""
    from app.services.yaml_service import YamlStructureError, update_image_version

    # Whitelist ky tu an toan truoc khi dua project/application vao .format() de ghep path -
    # chan path traversal (vd application="../../etc/cron.d/x") tu goc, khong phu thuoc vao
    # buoc kiem tra sau (resolve()/is_relative_to) co bi bo sot hay khong.
    validate_safe_path_segment(project, "project")
    validate_safe_path_segment(application, "application")

    # Khoa checkout master dung chung cho toan bo chuoi thao tac ben duoi (reset/fetch
    # -> sua YAML -> git add/commit/push) trong 1 critical section duy nhat - day chinh
    # la phan vi pha bao mat HIGH: neu khong khoa, 2 lan Run/Rollback dong thoi co the
    # giam chan nhau tren cung 1 thu muc (1 tien trinh dang sua YAML/commit thi tien
    # trinh kia chay reset --hard/clean -fd xoa mat thay doi, hoac 2 commit push de len
    # nhau gay push sai/mat version).
    with master_repo_lock(background=background):
        master_dir = ensure_master_repo(config.git_remote_repo_url)
        master_root = Path(master_dir).resolve()

        rel_path = config.chart_values_file_template.format(project=project, application=application)
        yaml_path = (Path(master_dir) / rel_path).resolve()
        # Lop phong thu thu 2: du input da qua whitelist, van xac nhan path cuoi cung thuc su
        # nam trong master_dir - phong truong hop chart_values_file_template (cau hinh boi
        # Super Admin qua UI) tu no chua ".." hoac duong dan tuyet doi.
        if not yaml_path.is_relative_to(master_root):
            logger.error("Path traversal detected: %s không nằm trong %s", yaml_path, master_root)
            raise GitError("Đường dẫn file chart không hợp lệ (ngoài phạm vi repo)")
        if not yaml_path.exists():
            return RunResult(ok=False, message=f"File {rel_path} không tồn tại")

        try:
            update_image_version(str(yaml_path), project, application, image_version)
        except YamlStructureError as exc:
            logger.error("Cấu trúc YAML %s không khớp project/application mong đợi: %s", rel_path, exc)
            return RunResult(
                ok=False,
                message=f"File {rel_path} sai cấu trúc (thiếu key project/application/version) - DevOps team kiểm tra thủ công.",
            )

        status_output = _run(["git", "status"], cwd=master_dir)

        if rel_path not in status_output:
            logger.info("File %s không đổi sau khi set version, dừng lại không push", rel_path)
            return RunResult(
                ok=False,
                message=f"Config file {rel_path} unchanged!",
            )

        _run(["git", "config", "user.email", config.git_commit_author_email], cwd=master_dir)
        _run(["git", "config", "user.name", config.git_commit_author_name], cwd=master_dir)
        _run(["git", "add", rel_path], cwd=master_dir)
        _run(["git", "commit", "-m", commit_message], cwd=master_dir)
        _run(["git", "push"], cwd=master_dir)

    return RunResult(ok=True, message="Task is running!", image_version=image_version)


def perform_run(
    project: str,
    application: str,
    commitid: str,
    run_by_email: str,
    config: ChartPushConfig,
    background: bool = False,
) -> RunResult:
    """Toàn bộ luồng Run: re-verify commit -> sửa YAML -> commit & push lên master.

    `background=True`: goi tu worker xu ly hang doi (task_service.run_queued_task, chay tu
    APScheduler job_process_task_queue) - KHONG con giu thread request nguoi dung nao, nen
    dung timeout lock DAI hon cho ca 2 buoc (re-verify staging + push master) thay vi
    fail-fast (xem check_commit_on_staging/master_repo_lock)."""
    check = check_commit_on_staging(project, application, commitid, config.git_remote_repo_url, background=background)
    if check.status != "ok":
        return RunResult(ok=False, message=f"Re-verify commit thất bại: {check.message}")

    image_version = check.image_version
    if "staging" in image_version:
        image_version = image_version.replace("staging", "production")

    commit_message = f"{run_by_email}: {project}/{application}: Update image version to {image_version}"
    return _apply_version_and_push(project, application, image_version, commit_message, config, background=background)


def perform_rollback(
    project: str,
    application: str,
    target_version: str,
    run_by_email: str,
    config: ChartPushConfig,
    background: bool = False,
) -> RunResult:
    """Sua YAML ve dung 1 image_version cu da tung deploy thanh cong truoc do (khong verify
    lai commit tren staging, vi ban than target_version da la 1 gia tri da qua Run/Done).

    `background=True`: xem docstring perform_run."""
    commit_message = f"{run_by_email}: {project}/{application}: Rollback image version to {target_version}"
    return _apply_version_and_push(project, application, target_version, commit_message, config, background=background)


# ---------------------------------------------------------------------------
# Bat/Tat staging + Production replicas - sua field `replicas` trong 1 file DUY NHAT
# chua nhieu app (khac ChartPushConfig/_apply_version_and_push - moi app 1 file rieng).
# Staging sua tren checkout/nhanh staging (ensure_staging_repo/staging_repo_lock);
# Production sua tren checkout/nhanh master (ensure_master_repo/master_repo_lock, DUNG
# CHUNG voi Run/Rollback - cung 1 lock nen khong bao gio xen ke voi push image version).
# 2 tinh nang giong het nhau ve co che, chi khac file dich + checkout/nhanh + (production
# khong co "Tat tat ca") nen dung chung 1 bo ham generic ben duoi, tham so hoa qua
# `ensure_repo`/`repo_lock`.
# ---------------------------------------------------------------------------


@dataclass
class EnvReplicasPushConfig:
    values_file_path: str
    catalog_charts_dir: str
    catalog_excluded_dirs: str
    git_commit_author_email: str
    git_commit_author_name: str
    git_remote_repo_url: str
    # Chi Production dung (mac dinh False/"" cho staging): khi True, thay vi push thang
    # len nhanh hien tai, tao 1 nhanh tam + Merge Request tro ve target_branch (xem
    # _create_merge_request_push). target_branch bat buoc khac rong khi use_merge_request=True.
    use_merge_request: bool = False
    target_branch: str = ""


@dataclass
class ReplicasToggleResult:
    ok: bool
    message: str
    changed: tuple[tuple[str, str], ...] = ()
    # True khi thay doi CHUA thuc su ap dung len nhanh chinh (chi tao Merge Request dang
    # cho duyet, xem _create_merge_request_push) - env_toggle_service.toggle_app/bulk_toggle
    # dung co nay de KHONG ghi de EnvAppCache.replicas (write_through_cache), tranh UI hien
    # sai gia tri nhu da ap dung xong trong khi MR chua duoc merge.
    pending_merge_request: bool = False


def _is_chart_application_dir(path: Path) -> bool:
    """Trung lap co y voi catalog_service._is_application_dir (khong import cheo de
    tranh circular import: catalog_service da import tu git_service) - nhan dien thu muc
    chart application that (co Chart.yaml hoac templates/)."""
    return (path / "Chart.yaml").exists() or (path / "templates").is_dir()


def _list_env_apps(config: EnvReplicasPushConfig, ensure_repo, repo_lock) -> list[tuple[str, str, str]]:
    """Liet ke TOAN BO (project, application, replicas) - danh sach app QUYET DINH boi
    cau truc thu muc charts/{group}/{application}/ tren checkout nay (GIONG HET
    catalog_service.sync_catalog_from_chart_repo - staging doc tren nhanh staging,
    production doc tren nhanh master), KHONG phai key co san trong file values. replicas
    lay tu file values_file_path tren CUNG checkout nay, mac dinh "0" neu app chua co
    entry (chua tung duoc bat/tat qua tinh nang nay). Dung repo_lock() vi checkout dung
    CHUNG voi cac thao tac khac (check_commit_on_staging/catalog sync cho staging;
    Run/Rollback cho master)."""
    from app.services.yaml_service import load_replicas_map

    with repo_lock():
        repo_dir = ensure_repo(config.git_remote_repo_url)
        repo_root = Path(repo_dir).resolve()

        charts_dir = (Path(repo_dir) / config.catalog_charts_dir).resolve()
        pairs: list[tuple[str, str]] = []
        if charts_dir.is_relative_to(repo_root) and charts_dir.is_dir():
            excluded = {name.strip() for name in config.catalog_excluded_dirs.split(",") if name.strip()}
            for group_path in sorted(p for p in charts_dir.iterdir() if p.is_dir()):
                for app_path in sorted(p for p in group_path.iterdir() if p.is_dir()):
                    if app_path.name in excluded or not _is_chart_application_dir(app_path):
                        continue
                    pairs.append((group_path.name, app_path.name))

        yaml_path = (Path(repo_dir) / config.values_file_path).resolve()
        replicas_map: dict[tuple[str, str], str] = {}
        if yaml_path.is_relative_to(repo_root) and yaml_path.exists():
            replicas_map = load_replicas_map(str(yaml_path))

        return [(project, application, replicas_map.get((project, application), "0")) for project, application in pairs]


def _apply_env_replicas_and_push(
    entries: list[tuple[str, str, int]], commit_message: str, config: EnvReplicasPushConfig, ensure_repo, repo_lock
) -> ReplicasToggleResult:
    """Sua replicas cho 1 hoac nhieu app trong CUNG 1 file, commit + push MOT LAN DUY NHAT
    - dung chung cho toggle 1 app, bulk-toggle nhieu app chon, va "Tat tat ca" (staging
    only; entries luc do = toan bo cap trong file)."""
    from app.services.yaml_service import update_replicas_bulk

    for project, application, _ in entries:
        # Whitelist ky tu an toan cho project/application - phong thu tu goc du gia tri
        # nay chi dung de so khop key trong YAML (khong ghep path nhu ChartPushConfig),
        # giu nhat quan voi _apply_version_and_push va chan du lieu la (vd chua "\n") lot
        # vao commit message/log.
        validate_safe_path_segment(project, "project")
        validate_safe_path_segment(application, "application")

    with repo_lock():
        repo_dir = ensure_repo(config.git_remote_repo_url)
        repo_root = Path(repo_dir).resolve()

        rel_path = config.values_file_path
        yaml_path = (Path(repo_dir) / rel_path).resolve()
        if not yaml_path.is_relative_to(repo_root):
            logger.error("Path traversal detected: %s không nằm trong %s", yaml_path, repo_root)
            raise GitError("Đường dẫn file values không hợp lệ (ngoài phạm vi repo)")
        if not yaml_path.exists():
            return ReplicasToggleResult(ok=False, message=f"File {rel_path} không tồn tại")

        updated = update_replicas_bulk(str(yaml_path), entries)
        if not updated:
            return ReplicasToggleResult(
                ok=False,
                message=f"Không tìm thấy ứng dụng nào khớp trong {rel_path} - kiểm tra lại cấu hình catalog/file",
            )

        status_output = _run(["git", "status"], cwd=repo_dir)
        if rel_path not in status_output:
            return ReplicasToggleResult(
                ok=False, message=f"File {rel_path} không thay đổi (giá trị hiện tại đã đúng)", changed=tuple(updated)
            )

        _run(["git", "config", "user.email", config.git_commit_author_email], cwd=repo_dir)
        _run(["git", "config", "user.name", config.git_commit_author_name], cwd=repo_dir)
        _run(["git", "add", rel_path], cwd=repo_dir)
        _run(["git", "commit", "-m", commit_message], cwd=repo_dir)
        _run(["git", "push"], cwd=repo_dir)

    return ReplicasToggleResult(ok=True, message="Đã cập nhật", changed=tuple(updated))


def _create_merge_request_push(
    entries: list[tuple[str, str, int]], commit_message: str, config: EnvReplicasPushConfig, ensure_repo, repo_lock
) -> ReplicasToggleResult:
    """Bien the cua _apply_env_replicas_and_push khi config.use_merge_request=True (hien
    tai chi Production dung, xem docstring EnvReplicasPushConfig.use_merge_request): thay
    vi `git push` thang len nhanh hien tai, commit len 1 nhanh TAM (dat ten theo timestamp,
    KHONG checkout local - chi push qua refspec HEAD:refs/heads/<temp_branch>) roi dung
    GitLab push options (-o merge_request.create) de GitLab tu dong tao Merge Request tro
    ve config.target_branch, cho nguoi duyet thu cong truoc khi ap dung that su. Khong can
    token API GitLab rieng - dung chung credential git push da co san
    (GIT_REMOTE_CREDENTIALS). Nhanh tam khong bao gio duoc don lai o day (khong checkout/
    xoa) - vo hai vi lan ke tiep ensure_repo() se fetch + reset --hard ve
    origin/<branch chinh>, tu dong bo working directory local, khong lien quan gi den
    nhanh tam da push len remote."""
    from app.services.yaml_service import update_replicas_bulk

    validate_safe_branch_name(config.target_branch, "target_branch")

    for project, application, _ in entries:
        validate_safe_path_segment(project, "project")
        validate_safe_path_segment(application, "application")

    with repo_lock():
        repo_dir = ensure_repo(config.git_remote_repo_url)
        repo_root = Path(repo_dir).resolve()

        rel_path = config.values_file_path
        yaml_path = (Path(repo_dir) / rel_path).resolve()
        if not yaml_path.is_relative_to(repo_root):
            logger.error("Path traversal detected: %s không nằm trong %s", yaml_path, repo_root)
            raise GitError("Đường dẫn file values không hợp lệ (ngoài phạm vi repo)")
        if not yaml_path.exists():
            return ReplicasToggleResult(ok=False, message=f"File {rel_path} không tồn tại")

        updated = update_replicas_bulk(str(yaml_path), entries)
        if not updated:
            return ReplicasToggleResult(
                ok=False,
                message=f"Không tìm thấy ứng dụng nào khớp trong {rel_path} - kiểm tra lại cấu hình catalog/file",
            )

        status_output = _run(["git", "status"], cwd=repo_dir)
        if rel_path not in status_output:
            return ReplicasToggleResult(
                ok=False, message=f"File {rel_path} không thay đổi (giá trị hiện tại đã đúng)", changed=tuple(updated)
            )

        temp_branch = f"production-replicas-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"

        _run(["git", "config", "user.email", config.git_commit_author_email], cwd=repo_dir)
        _run(["git", "config", "user.name", config.git_commit_author_name], cwd=repo_dir)
        _run(["git", "add", rel_path], cwd=repo_dir)
        _run(["git", "commit", "-m", commit_message], cwd=repo_dir)
        _run(
            [
                "git",
                "push",
                "-o",
                "merge_request.create",
                "-o",
                f"merge_request.target={config.target_branch}",
                "-o",
                f"merge_request.title={commit_message}",
                "--",
                "origin",
                f"HEAD:refs/heads/{temp_branch}",
            ],
            cwd=repo_dir,
        )

    return ReplicasToggleResult(
        ok=True,
        message=f"Đã tạo Merge Request vào nhánh {config.target_branch}",
        changed=tuple(updated),
        pending_merge_request=True,
    )


def _push_env_replicas(
    entries: list[tuple[str, str, int]], commit_message: str, config: EnvReplicasPushConfig, ensure_repo, repo_lock
) -> ReplicasToggleResult:
    """Diem re duy nhat giua push thang (mac dinh, staging luon di duong nay) va tao
    Merge Request (config.use_merge_request=True, chi Production) - xem
    EnvReplicasPushConfig.use_merge_request."""
    if config.use_merge_request:
        return _create_merge_request_push(entries, commit_message, config, ensure_repo, repo_lock)
    return _apply_env_replicas_and_push(entries, commit_message, config, ensure_repo, repo_lock)


def _toggle_env_app(
    env_label: str, project: str, application: str, replicas: int, actor_email: str, config, ensure_repo, repo_lock
) -> ReplicasToggleResult:
    commit_message = f"{actor_email}: {project}/{application}: Set replicas={replicas}"
    return _push_env_replicas(
        [(project, application, replicas)], commit_message, config, ensure_repo, repo_lock
    )


def _bulk_toggle_env_apps(
    env_label: str, apps: list[tuple[str, str]], enabled: bool, actor_email: str, config, ensure_repo, repo_lock
) -> ReplicasToggleResult:
    replicas = 1 if enabled else 0
    entries = [(project, application, replicas) for project, application in apps]
    commit_message = f"{actor_email}: Set replicas={replicas} for {len(apps)} apps ({env_label})"
    return _push_env_replicas(entries, commit_message, config, ensure_repo, repo_lock)


# --- Staging (nhanh staging) ---


def list_staging_apps(config: EnvReplicasPushConfig) -> list[tuple[str, str, str]]:
    return _list_env_apps(config, ensure_staging_repo, staging_repo_lock)


def toggle_staging_app(
    project: str, application: str, replicas: int, actor_email: str, config: EnvReplicasPushConfig
) -> ReplicasToggleResult:
    return _toggle_env_app(
        "staging", project, application, replicas, actor_email, config, ensure_staging_repo, staging_repo_lock
    )


def bulk_toggle_staging_apps(
    apps: list[tuple[str, str]], enabled: bool, actor_email: str, config: EnvReplicasPushConfig
) -> ReplicasToggleResult:
    return _bulk_toggle_env_apps(
        "staging", apps, enabled, actor_email, config, ensure_staging_repo, staging_repo_lock
    )


def turn_off_all_staging(actor_email: str, config: EnvReplicasPushConfig) -> ReplicasToggleResult:
    """Set replicas=0 cho TOAN BO cap (project, application) dang co san trong file values-
    sandbox (khong can biet truoc danh sach tu catalog - lay truc tiep tu noi dung file).
    Doc danh sach + sua + commit + push trong CUNG 1 lan giu staging_repo_lock() (khong
    goi lai _apply_env_replicas_and_push - ham do tu no lai ensure_staging_repo/fetch 1
    lan nua duoi 1 lock rieng, gay fetch mang 2 lan khong can thiet). Chi danh cho staging -
    production KHONG co nut "Tat tat ca" (theo yeu cau, rui ro cao hon)."""
    from app.services.yaml_service import list_all_project_applications, update_replicas_bulk

    with staging_repo_lock():
        staging_dir = ensure_staging_repo(config.git_remote_repo_url)
        staging_root = Path(staging_dir).resolve()
        rel_path = config.values_file_path
        yaml_path = (Path(staging_dir) / rel_path).resolve()
        if not yaml_path.is_relative_to(staging_root):
            logger.error("Path traversal detected: %s không nằm trong %s", yaml_path, staging_root)
            raise GitError("Đường dẫn file values-staging không hợp lệ (ngoài phạm vi repo)")
        if not yaml_path.exists():
            return ReplicasToggleResult(ok=False, message=f"File {rel_path} không tồn tại")

        all_pairs = list_all_project_applications(str(yaml_path))
        if not all_pairs:
            return ReplicasToggleResult(ok=False, message="File values-staging không có ứng dụng nào")

        entries = [(project, application, 0) for project, application in all_pairs]
        updated = update_replicas_bulk(str(yaml_path), entries)
        if not updated:
            return ReplicasToggleResult(ok=False, message="Không có ứng dụng nào cần cập nhật")

        status_output = _run(["git", "status"], cwd=staging_dir)
        if rel_path not in status_output:
            return ReplicasToggleResult(
                ok=False, message=f"File {rel_path} không thay đổi (tất cả đã ở replicas=0)", changed=tuple(updated)
            )

        _run(["git", "config", "user.email", config.git_commit_author_email], cwd=staging_dir)
        _run(["git", "config", "user.name", config.git_commit_author_name], cwd=staging_dir)
        _run(["git", "add", rel_path], cwd=staging_dir)
        _run(["git", "commit", "-m", f"{actor_email}: Disable all staging ({len(updated)} apps)"], cwd=staging_dir)
        _run(["git", "push"], cwd=staging_dir)

    return ReplicasToggleResult(ok=True, message="Đã tắt tất cả staging", changed=tuple(updated))


# --- Production (nhanh master, dung CHUNG checkout/lock voi Run/Rollback) ---


def list_production_apps(config: EnvReplicasPushConfig) -> list[tuple[str, str, str]]:
    return _list_env_apps(config, ensure_master_repo, master_repo_lock)


def toggle_production_app(
    project: str, application: str, replicas: int, actor_email: str, config: EnvReplicasPushConfig
) -> ReplicasToggleResult:
    return _toggle_env_app(
        "production", project, application, replicas, actor_email, config, ensure_master_repo, master_repo_lock
    )


def bulk_toggle_production_apps(
    apps: list[tuple[str, str]], enabled: bool, actor_email: str, config: EnvReplicasPushConfig
) -> ReplicasToggleResult:
    return _bulk_toggle_env_apps(
        "production", apps, enabled, actor_email, config, ensure_master_repo, master_repo_lock
    )
