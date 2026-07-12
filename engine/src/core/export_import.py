# -*- coding: utf-8 -*-
"""项目 ZIP 导出 / 导入。

将某个项目根目录（ProjectSandbox.root）下的全部文件打包为 zip，
或解压 zip 到一个新的项目目录，并把它登记到 ProjectManager。

设计目标:
  1. 仅依赖标准库 (zipfile / shutil / pathlib / json)。
  2. 导出时写入 manifest.json（项目名 / 导出版本号 / 时间戳）。
  3. 导入时按"新名字 + 当前项目根"生成新目录，防止覆盖冲突。
  4. 防止 zip slip: 拒绝任何跳出目标目录的绝对路径或 `..`。
  5. 防御符号链接 TOCTOU、ZIP 炸弹与 manifest 注入。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

import errno
import json
import os
import re
import shutil
import stat
import time
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

# 允许调用方不传 ProjectManager，此时我们自己 lazy 构造一个
try:
    from src.sandbox.project_manager import ProjectManager  # noqa: F401
except Exception:  # pragma: no cover - 允许在没有 GUI 时直接运行
    ProjectManager = None  # type: ignore

MANIFEST_NAME = ".dscode_manifest.json"
EXPORT_VERSION = 1


def _sanitize_audit_text(text: object) -> str:
    """清理审计日志中的控制字符，防止用户可控数据被当作格式化字符串或转义序列。"""
    if text is None:
        return ""
    s = str(text)
    return "".join(ch if not (0 <= ord(ch) <= 31) else "?" for ch in s)

# ---------------------------------------------------------------------------
# 安全限制（M-010 ZIP 炸弹检测）
# ---------------------------------------------------------------------------
MAX_ZIP_SIZE = 500 * 1024 * 1024          # 单个 zip 文件 500 MB
MAX_ZIP_ENTRIES = 10000                    # 文件数量上限
MAX_TOTAL_UNCOMPRESSED = 2 * 1024 * 1024 * 1024  # 解压后总大小 2 GB
MAX_COMPRESSION_RATIO = 100                # 压缩比上限 100:1
MAX_JSON_SIZE = 50 * 1024 * 1024           # JSON 反序列化大小上限 50 MB (M-008)
MAX_PER_FILE_UNCOMPRESSED = 256 * 1024 * 1024  # 单个文件解压上限 256 MB

# 全局实际解压字节数计数器（防止 file_size 元数据伪造的 zip 炸弹）
_actual_uncompressed_total = 0
_uncompressed_lock = threading.Lock()
_import_mutex = threading.Lock()  # 防止并发导入的全局互斥锁


class _LimitedCopyError(Exception):
    """解压字节数超出限制时抛出。"""
    pass


def _limited_copyfileobj(src, dst, max_bytes: int):
    """类似 shutil.copyfileobj，但限制写入字节数，防止 zip 炸弹。"""
    global _actual_uncompressed_total
    written = 0
    while True:
        buf = src.read(64 * 1024)
        if not buf:
            break
        written += len(buf)
        with _uncompressed_lock:
            _actual_uncompressed_total += len(buf)
            total = _actual_uncompressed_total
        if written > max_bytes or total > MAX_TOTAL_UNCOMPRESSED:
            raise _LimitedCopyError(f"解压大小超出限制 (file={written}, total={total})")
        dst.write(buf)

# ---------------------------------------------------------------------------
# manifest schema（H-002）
# ---------------------------------------------------------------------------
_ALLOWED_MANIFEST_KEYS = {"version", "project_name", "exported_at"}


def _validate_manifest(manifest: object) -> bool:
    """校验 manifest 结构，防止字段注入与版本漂移（H-002）。"""
    if not isinstance(manifest, dict):
        return False
    if set(manifest.keys()) - _ALLOWED_MANIFEST_KEYS:
        return False

    version = manifest.get("version")
    if not isinstance(version, int) or version != EXPORT_VERSION:
        return False

    project_name = manifest.get("project_name")
    if not isinstance(project_name, str):
        return False
    # 禁止路径穿越与控制字符注入
    if ".." in project_name or "/" in project_name or "\\" in project_name:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in project_name):
        return False

    exported_at = manifest.get("exported_at")
    if not isinstance(exported_at, (str, int, float)):
        return False
    if isinstance(exported_at, str) and any(ord(c) < 32 for c in exported_at):
        return False

    return True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _safe_relative(rel: str, target_root: Path) -> Optional[Path]:
    """拒绝 zip slip: 任何绝对路径或 `..` 跳出 target_root 的条目都返回 None"""
    if not rel:
        return None
    try:
        candidate = (target_root / rel).resolve()
        target_root_resolved = target_root.resolve()
    except Exception:
        return None
    try:
        candidate.relative_to(target_root_resolved)
    except ValueError:
        return None
    return candidate


def _safe_extract_path(rel: str, target_root: Path) -> Optional[Path]:
    """安全解压路径验证: 在 _safe_relative 基础上增加符号链接攻击防护。

    在 POSIX 上使用 O_DIRECTORY | O_NOFOLLOW 逐层打开父目录 fd，
    验证路径链上的每个组件都不是符号链接，防止 TOCTOU 竞态。
    非 POSIX 系统回退到 is_symlink() 路径检查。

    返回: 安全的 Path 对象, 或 None (不安全)
    """
    if not rel:
        return None
    try:
        target_root_resolved = target_root.resolve()
        rel_parts = (target_root / rel).relative_to(target_root_resolved).parts
    except Exception:
        return None

    for part in rel_parts:
        if not part or part == "." or part.startswith(".."):
            return None

    _O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
    _O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

    # POSIX 强化路径：通过 dir_fd 验证父目录链，避免 symlink TOCTOU
    if _O_DIRECTORY and _O_NOFOLLOW:
        try:
            fd = os.open(
                str(target_root_resolved),
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
            )
        except OSError:
            return None
        current = target_root_resolved
        try:
            for idx, part in enumerate(rel_parts):
                if idx == len(rel_parts) - 1:
                    # 最终组件：可能尚未创建，仅检查它不是已存在的符号链接
                    try:
                        if os.path.islink(part, dir_fd=fd):
                            return None
                    except FileNotFoundError:
                        pass
                    except OSError:
                        return None
                    return current / part
                child_fd = _open_dir_nofollow(fd, part)
                if child_fd is None:
                    return None
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = child_fd
                current = current / part
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    # 非 POSIX 回退路径
    current = target_root_resolved
    for part in rel_parts:
        current = current / part
        try:
            if current.is_symlink():
                return None
        except OSError:
            return None
    return current


def _default_zip_name(project_name: str) -> str:
    """形如 `myproj_20260615_103015.zip`"""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (project_name or "project"))
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{safe}_{ts}.zip"


def _projects_root_from_manager(projects_root: Optional[os.PathLike]) -> Path:
    """解析目标项目根目录。若未传入，则从 Config.PROJECTS_DIR 推导"""
    if projects_root is not None:
        return Path(projects_root)
    try:
        from src.core.config import Config  # type: ignore
        cfg = Config()
        return Path(cfg.PROJECTS_DIR)
    except Exception:
        # 退到项目仓库中的默认 projects 目录
        return Path(__file__).resolve().parents[2] / "projects"


def _safe_mkdir_parents(path: Path, target_root: Path) -> Optional[Path]:
    """安全创建 path 在 target_root 下的所有父目录。

    在 POSIX 上使用 O_DIRECTORY | O_NOFOLLOW 逐层打开父目录 fd，
    在已验证的目录 fd 内创建子目录，避免 symlink 竞态。
    非 POSIX 系统回退到 is_symlink() + mkdir 路径检查。
    创建的目录权限固定为 0o700（M-008）。
    """
    try:
        target_root_resolved = target_root.resolve()
        path_resolved = path.resolve()
        rel_parts = path_resolved.relative_to(target_root_resolved).parts
    except Exception:
        return None

    for part in rel_parts:
        if not part or part == "." or part.startswith(".."):
            return None

    _O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
    _O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

    # POSIX 强化路径：使用 dir_fd 创建目录，避免 symlink TOCTOU
    if _O_DIRECTORY and _O_NOFOLLOW:
        try:
            fd = os.open(
                str(target_root_resolved),
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
            )
        except OSError:
            return None
        current = target_root_resolved
        try:
            for part in rel_parts:
                child_fd = _open_dir_nofollow(fd, part)
                if child_fd is None:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        child_fd = _open_dir_nofollow(fd, part)
                        if child_fd is None:
                            return None
                    except OSError:
                        return None
                    else:
                        child_fd = _open_dir_nofollow(fd, part)
                        if child_fd is None:
                            return None
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = child_fd
                current = current / part
            return current
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    # 非 POSIX 回退路径
    current = target_root_resolved
    for part in rel_parts:
        current = current / part
        try:
            if current.is_symlink():
                return None
            try:
                current.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
            if current.is_symlink():
                return None
            resolved = current.resolve()
            resolved.relative_to(target_root_resolved)
            st = current.lstat()
            if not stat.S_ISDIR(st.st_mode):
                return None
        except (OSError, ValueError):
            return None
    return current


def _check_zip_bomb(zip_path: Path) -> Tuple[bool, str]:
    """检查 ZIP 炸弹：大小、条目数、解压后总大小与压缩比。"""
    try:
        zip_size = zip_path.stat().st_size
    except OSError:
        return False, "无法读取 zip 文件信息"
    if zip_size > MAX_ZIP_SIZE:
        return False, "zip 文件超过大小限制"

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                return False, "zip 文件条目数量超过限制"

            total_uncompressed = sum(info.file_size for info in infos)
            total_compressed = sum(info.compress_size for info in infos)
    except Exception:
        return False, "无法解析 zip 文件结构"

    if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
        return False, "zip 解压后总大小超过限制"

    if total_compressed <= 0:
        if total_uncompressed > 0:
            return False, "zip 压缩比异常"
    elif total_uncompressed / total_compressed > MAX_COMPRESSION_RATIO:
        return False, "zip 压缩比超过限制"

    return True, ""


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    """检测 ZIP 条目是否为符号链接（external_attr 高 4 位或 is_symlink）。"""
    if getattr(info, "is_symlink", lambda: False)():
        return True
    if not hasattr(info, "external_attr"):
        return False
    # Unix file type 位于 external_attr 的高 4 位；0xA 表示符号链接
    return ((info.external_attr >> 28) & 0xF) == 0xA


def _open_dir_nofollow(dir_fd: int, name: str) -> Optional[int]:
    """在 dir_fd 目录下打开名为 name 的子目录，要求必须是目录且不是符号链接。"""
    _O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
    _O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
    if not _O_DIRECTORY or not _O_NOFOLLOW:
        return None
    try:
        return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return None


def _atomic_extract(zf: zipfile.ZipFile, info: zipfile.ZipInfo, target_root: Path) -> bool:
    """原子化单文件安全解压（强化版）。

    防御要点：
      1. 拒绝 ZIP 内的符号链接条目（_is_zip_symlink）。
      2. 在 POSIX 上通过 O_DIRECTORY | O_NOFOLLOW 逐层打开父目录，
         在打开瞬间检测并阻止符号链接替换（TOCTOU 防护）。
      3. 最终文件使用 O_NOFOLLOW | O_CREAT 在已验证的目录 fd 内创建；
         写入前通过 fstat 检查 st_nlink，拒绝硬链接覆盖外部文件。
      4. 非 POSIX / dir_fd 不可用时回退到路径检查 + O_NOFOLLOW。
    """
    rel = info.filename
    if not rel or rel == MANIFEST_NAME or rel.endswith("/"):
        return False
    if _is_zip_symlink(info):
        return False

    # 拒绝 Windows 风格绝对路径与路径分隔符，防止跨平台 zip slip
    if ":" in rel or "\\" in rel:
        return False
    rel_parts = rel.split("/")
    for part in rel_parts:
        if not part or part == "." or part == ".." or part.startswith(".."):
            return False

    try:
        target_root_resolved = target_root.resolve()
    except Exception:
        return False

    _O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

    # ------------------------------------------------------------------
    # POSIX 强化路径：使用 dir_fd 避免路径链上的 TOCTOU
    # ------------------------------------------------------------------
    if getattr(os, "O_DIRECTORY", 0) and _O_NOFOLLOW:
        try:
            dir_fd = os.open(
                str(target_root_resolved),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError:
            return False
        try:
            for part in rel_parts[:-1]:
                child_fd = _open_dir_nofollow(dir_fd, part)
                if child_fd is None:
                    # 目录不存在则安全创建，然后再打开验证
                    try:
                        os.mkdir(part, 0o700, dir_fd=dir_fd)
                    except FileExistsError:
                        child_fd = _open_dir_nofollow(dir_fd, part)
                        if child_fd is None:
                            return False
                    except OSError:
                        return False
                    else:
                        child_fd = _open_dir_nofollow(dir_fd, part)
                        if child_fd is None:
                            return False
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
                dir_fd = child_fd

            file_part = rel_parts[-1]
            try:
                # 先不带 O_TRUNC 打开，写入前检测硬链接（O_TRUNC 会提前破坏外部文件）
                fd = os.open(
                    file_part,
                    os.O_WRONLY | os.O_CREAT | _O_NOFOLLOW,
                    0o600,
                    dir_fd=dir_fd,
                )
            except OSError:
                return False

            # 硬链接检测：若目标路径是硬链接到沙箱外，st_nlink > 1
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                    os.close(fd)
                    return False
                # 安全清空后再写入
                os.ftruncate(fd, 0)
            except OSError:
                os.close(fd)
                return False

            fd_owned = False
            try:
                # os.fdopen 接管 fd 所有权；with 块退出时自动关闭 fd
                with os.fdopen(fd, "wb") as dst_f:
                    fd_owned = True
                    with zf.open(info) as src_f:
                        _limited_copyfileobj(src_f, dst_f, MAX_PER_FILE_UNCOMPRESSED)
                    try:
                        os.fchmod(fd, 0o600)
                    except OSError as e:
                        logger.warning("[_atomic_extract] 设置解压文件权限 0o600 失败: %s", e)
            except _LimitedCopyError:
                logger.warning("[_atomic_extract] 文件解压超出大小限制: %s", info.filename)
                if not fd_owned:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                return False
            except Exception:
                # 如果 fdopen 成功，with 块已关闭 fd；如果 fdopen 本身失败，fd 仍需手动关闭
                if not fd_owned:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                return False
            return True
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 回退路径（Windows 等不支持 dir_fd 的场景）
    # ------------------------------------------------------------------
    dest = _safe_extract_path(rel, target_root)
    if dest is None:
        return False

    # 使用 _safe_mkdir_parents 在已验证的目录 fd 内创建父目录
    if _safe_mkdir_parents(dest.parent, target_root_resolved) is None:
        return False

    try:
        if dest.is_symlink():
            return False
    except OSError:
        return False

    try:
        # 先不带 O_TRUNC 打开，避免提前破坏可能的硬链接目标
        fd = os.open(
            dest,
            os.O_WRONLY | os.O_CREAT | _O_NOFOLLOW,
            0o600,
        )
    except OSError:
        return False

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
    except OSError:
        os.close(fd)
        return False

    fd_owned = False
    try:
        with os.fdopen(fd, "wb") as dst_f:
            fd_owned = True
            with zf.open(info) as src_f:
                _limited_copyfileobj(src_f, dst_f, MAX_PER_FILE_UNCOMPRESSED)
    except _LimitedCopyError:
        logger.warning("[_atomic_extract] 文件解压超出大小限制(回退路径): %s", info.filename)
        if not fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        return False
    except Exception:
        if not fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        return False

    try:
        os.chmod(str(dest), 0o600, follow_symlinks=False)
    except OSError as e:
        logger.warning("[_atomic_extract] 设置解压文件权限 0o600 失败: %s (%s)", dest, e)
    return True


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def export_project(
    project_name: str,
    output_path: Optional[os.PathLike] = None,
    projects_root: Optional[os.PathLike] = None,
) -> Tuple[bool, str, List[str]]:
    """把项目 project_name 的整个目录打包为 zip。

    参数:
        project_name: 项目名 (在 projects_root 下的子目录)
        output_path:  zip 输出路径；为 None 时在项目根同目录生成默认文件名
        projects_root: 可选；默认由 Config.PROJECTS_DIR 推导

    返回: (success: bool, zip_path_or_error: str, failed_files: list[str])
    """
    if not project_name or not isinstance(project_name, str):
        return False, "项目名称无效", []
    # 安全校验：禁止路径分隔符与 .. 穿越
    if not re.match(r"^[A-Za-z0-9_\-]{1,128}$", project_name):
        return False, "项目名称包含非法字符", []

    root = _projects_root_from_manager(projects_root)
    # 安全：拒绝 projects_root 本身是符号链接
    try:
        if root.is_symlink():
            return False, "项目根目录包含符号链接，导出被拒绝", []
    except OSError:
        return False, "无法解析项目根目录", []
    # 安全：拒绝项目目录本身是符号链接（防止导出符号链接指向的外部文件）
    project_path = root / project_name
    try:
        if project_path.is_symlink():
            return False, "项目目录是符号链接，导出被拒绝", []
    except OSError:
        return False, "无法解析项目目录", []
    src_dir = project_path.resolve()
    root_resolved = root.resolve()
    # 确保解析后的项目目录仍在 root 下（使用 relative_to 避免前缀匹配误判）
    try:
        src_dir.relative_to(root_resolved)
    except ValueError:
        return False, "项目路径非法", []
    if not src_dir.exists() or not src_dir.is_dir():
        return False, "项目目录不存在或无法访问", []

    if output_path is None:
        output_path = root / _default_zip_name(project_name)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # M-018: 审计日志（导出项目）
    logger.info(
        "[AUDIT] export project: %s -> %s",
        project_name,
        _sanitize_audit_text(output_path),
    )

    # 生成 manifest
    manifest = {
        "version": EXPORT_VERSION,
        "project_name": project_name,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    failed_files: List[str] = []

    try:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # 先写 manifest
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            # 手动遍历，不跟随符号链接（兼容 Python 3.10，Path.rglob 的
            # follow_symlinks 参数在 3.12 才加入）。
            for dirpath, dirnames, filenames in os.walk(str(src_dir), followlinks=False):
                # 就地修改 dirnames 防止 os.walk 进入符号链接目录
                dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
                for fname in filenames:
                    p = Path(dirpath) / fname
                    # MEDIUM-005: 打包时跳过任何符号链接，不跟随也不解引用，
                    # 避免 ZIP 中出现符号链接属性或 zip slip 风险。
                    try:
                        if p.is_symlink():
                            rel_link = str(p.relative_to(src_dir))
                            logger.warning("导出跳过符号链接: %s", rel_link)
                            continue
                    except Exception:
                        # 无法解析路径，保守跳过
                        failed_files.append(str(p.relative_to(src_dir)))
                        continue
                    if not p.is_file():
                        continue
                    # 跳过运行时产生的临时锁文件 / 正在写入的 zip
                    try:
                        if p.resolve() == output_path.resolve():
                            continue
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                    rel = str(p.relative_to(src_dir))
                    # 不要把 manifest 压两次（如果项目里恰好有同名文件，保留用户的）
                    if rel == MANIFEST_NAME:
                        rel = MANIFEST_NAME + ".user"
                    try:
                        zf.write(p, arcname=rel)
                    except Exception:
                        # 记录失败的文件，不中断整体导出
                        failed_files.append(rel)
    except Exception as e:
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return False, f"打包失败: {e}", failed_files

    # M-003: 导出的 zip 文件同样限制为 0o600
    try:
        os.chmod(str(output_path), 0o600, follow_symlinks=False)
    except OSError as e:
        logger.warning("[export_project] 设置导出 zip 权限 0o600 失败: %s (%s)", output_path, e)

    zip_path = str(output_path)
    if failed_files:
        return False, f"部分文件导出失败 ({len(failed_files)} 个)", failed_files

    return True, zip_path, []


def _import_has_required_files(target: Path) -> bool:
    """校验导入后的目录是否包含关键文件（M-016）：DS.md 或 work/ 目录至少存在一个。"""
    try:
        return (target / "DS.md").exists() or (target / "work").is_dir()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------
def import_project(
    zip_path: os.PathLike,
    target_project_name: Optional[str] = None,
    projects_root: Optional[os.PathLike] = None,
) -> Tuple[bool, str]:
    """把 zip 解压为新的项目目录。

    参数:
        zip_path: 源 zip 文件路径
        target_project_name: 目标项目名；为 None 时按 manifest.project_name 推导
        projects_root: 可选；默认由 Config.PROJECTS_DIR 推导

    返回: (success, new_project_name_or_error)
    """
    zip_path = Path(zip_path)
    if not zip_path.exists() or not zip_path.is_file():
        return False, "zip 文件不存在或无法访问"
    try:
        if not zipfile.is_zipfile(zip_path):
            return False, "文件不是有效的 zip 归档"
    except Exception:
        return False, "无法读取 zip 文件"

    # M-010: ZIP 炸弹检测
    ok, msg = _check_zip_bomb(zip_path)
    if not ok:
        return False, msg

    root = _projects_root_from_manager(projects_root)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except Exception:
        return False, "创建项目根目录失败"

    # H-002: 读取并严格校验 manifest
    manifest: dict = {}
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if MANIFEST_NAME not in zf.namelist():
                return False, "manifest 文件缺失或格式错误"
            with zf.open(MANIFEST_NAME) as mf:
                raw = mf.read()
                # M-008: JSON 反序列化前检查大小
                if len(raw) > MAX_JSON_SIZE:
                    return False, "manifest 超过大小限制"
                manifest = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return False, "manifest 解析失败"
    except Exception:
        return False, "读取 manifest 失败"

    if not _validate_manifest(manifest):
        return False, "manifest 格式或版本无效"

    base = target_project_name or manifest.get("project_name") or zip_path.stem or "imported"
    base = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(base)).strip("_-") or "imported"

    # Bug 4修复: 先获取互斥锁，再进行冲突避免与目录创建。若两个线程
    # 计算出相同的目标目录，未获锁的线程不再做任何清理（直接返回 False），
    # 避免误删已获锁线程正在使用的工作目录。
    # 安全：root 符号链接校验也必须在 mutex 内执行，否则存在 TOCTOU 窗口
    # （检查通过后、获取 mutex 前 root 可能被替换为符号链接）
    global _actual_uncompressed_total
    if not _import_mutex.acquire(timeout=0):
        return False, "另一个导入操作正在进行中，请稍后重试"
    try:
        # 安全：拒绝项目根目录本身是符号链接的情况，防止解压到外部路径
        try:
            if root.is_symlink() or root.resolve() != Path(os.path.realpath(root)):
                return False, "项目根目录包含符号链接，导入被拒绝"
        except OSError:
            return False, "无法解析项目根目录"

        # H-002: 检测 projects_root 下预先放置的符号链接
        try:
            for entry in root.iterdir():
                if entry.is_symlink():
                    return False, "项目根目录下存在符号链接，导入被拒绝"
        except OSError:
            return False, "无法枚举项目根目录"

        with _uncompressed_lock:
            _actual_uncompressed_total = 0

        # 保证不与现有项目冲突；H-002: 目标目录若是符号链接则拒绝覆盖
        candidate = base
        idx = 1
        target = root / candidate
        while target.exists() or target.is_symlink():
            if target.is_symlink():
                return False, "目标目录存在且为符号链接，导入被拒绝"
            candidate = f"{base}_imp{idx}"
            idx += 1
            target = root / candidate

        try:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        except Exception:
            return False, "创建目标目录失败"

        # M-018: 审计日志（导入项目）
        logger.info(
            "[AUDIT] import project: %s -> %s",
            _sanitize_audit_text(zip_path),
            candidate,
        )

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                failed_any = False
                for info in zf.infolist():
                    # manifest 与目录条目无需解压
                    if info.filename == MANIFEST_NAME or info.filename.endswith("/"):
                        continue
                    # H-001: 使用原子化安全解压；任意条目失败即认为导入不安全
                    if not _atomic_extract(zf, info, target):
                        failed_any = True
                if failed_any:
                    try:
                        shutil.rmtree(target, ignore_errors=True)
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                    return False, "解压失败（ZIP 包含不安全路径或符号链接）"
        except Exception:
            # 失败回滚
            try:
                shutil.rmtree(target, ignore_errors=True)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return False, "解压失败"

        # M-016: 导入完整性验证；缺少关键文件则回滚删除目标目录
        if not _import_has_required_files(target):
            try:
                shutil.rmtree(target, ignore_errors=True)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return False, "导入项目不完整：缺少 DS.md 或 work/ 目录"

        # 写入新的 manifest；仅保留 _ALLOWED_MANIFEST_KEYS 中的字段，导入元数据不再写入 manifest
        new_manifest = {
            "version": EXPORT_VERSION,
            "project_name": candidate,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        manifest_path = target / MANIFEST_NAME
        try:
            manifest_path.write_text(
                json.dumps(new_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # M-003: 写入的 manifest 文件同样限制为 0o600
            try:
                os.chmod(str(manifest_path), 0o600, follow_symlinks=False)
            except OSError as e:
                logger.warning("[import_project] 设置 manifest 权限 0o600 失败: %s (%s)", manifest_path, e)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        return True, candidate
    finally:
        _import_mutex.release()


# ---------------------------------------------------------------------------
# CLI (便于直接 `python -m src.core.export_import ...`)
# ---------------------------------------------------------------------------
def _cli(argv: list) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="dscode project zip export/import")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="导出项目为 zip")
    p_exp.add_argument("project")
    p_exp.add_argument("--output", default=None, help="输出 zip 路径")
    p_exp.add_argument("--projects-root", default=None)

    p_imp = sub.add_parser("import", help="从 zip 导入项目")
    p_imp.add_argument("zip_path")
    p_imp.add_argument("--name", default=None, help="目标项目名 (可选)")
    p_imp.add_argument("--projects-root", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "export":
        ok, info, failed = export_project(args.project, args.output, args.projects_root)
        print(("OK: " if ok else "ERR: ") + info)
        if failed:
            print("失败文件:")
            for f in failed:
                print(f"  - {f}")
        return 0 if ok else 1
    if args.cmd == "import":
        ok, info = import_project(args.zip_path, args.name, args.projects_root)
        print(("OK: " if ok else "ERR: ") + info)
        return 0 if ok else 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(_cli(sys.argv[1:]))
