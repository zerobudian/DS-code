# -*- coding: utf-8 -*-
"""加密 / 解密与密钥管理。

设计要点：
1. 使用 cryptography.Fernet 进行对称加密；
2. 密钥从 `sandbox.work_dir/.dscode/secret.key` 读取，若不存在则创建；
3. 密钥文件权限：仅 owner 读写（0o600）；
4. 降级密钥（derived key）仅在环境变量中启用时使用，用 secrets.token_urlsafe(32)。
"""

import base64
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False
    Fernet = None
    InvalidToken = None


# Fernet key 是 32 字节 urlsafe_b64 编码 = 44 字符
_FERNET_KEY_BYTES = 32
_FERNET_KEY_ENCODED_LEN = 44
_KEY_DIR_NAME = ".dscode"
_KEY_FILE_NAME = "secret.key"
_SECRET_MAX_BYTES = 32  # 降级 key 最大字节数


def _validate_fernet_key(raw: bytes) -> bool:
    """校验 raw 是否是合法的 Fernet 密钥（32 字节 urlsafe_b64 编码）。"""
    if not isinstance(raw, (bytes, bytearray)):
        return False
    if len(raw) != _FERNET_KEY_ENCODED_LEN:
        return False
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception:
        return False
    return len(decoded) == _FERNET_KEY_BYTES


def _is_windows() -> bool:
    return os.name == "nt"


def _chmod_600(path: Path) -> None:
    """设置密钥文件权限为仅 owner 读写。

    Unix: POSIX 0o600 (S_IRUSR | S_IWUSR)
    Windows: 使用 icacls 限制为当前用户完全控制, 并设为隐藏文件
    """
    try:
        if _is_windows():
            # P0-003 修复: Windows 上 POSIX chmod 无效,
            # 改用 icacls 设置 ACL + 隐藏属性
            import subprocess as _sp
            try:
                # 设为隐藏文件
                import ctypes as _ct
                try:
                    _ct.windll.kernel32.SetFileAttributesW(str(path), 2)  # FILE_ATTRIBUTE_HIDDEN
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            try:
                # 继承移除 + 仅当前用户完全控制
                username = os.environ.get("USERNAME", os.environ.get("USER", ""))
                if username:
                    _sp.run(
                        ["icacls", str(path), "/inheritance:r", "/grant:r",
                         f"{username}:F"],
                        capture_output=True, timeout=10, check=False,
                    )
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        else:
            os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR)  # Unix: 0o600
    except Exception:
        # P0-003 修复: 权限设置失败时记录警告, 不应静默忽略
        import logging as _logging
        _logger = _logging.getLogger("dscode.crypto")
        _logger.warning("无法设置密钥文件权限: %s", path)


def _check_key_file_permissions(path: Path) -> bool:
    """验证密钥文件权限和所有权。

    非 Windows 系统上:
      - 其他用户不能有任何访问权限 (st_mode & 0o077 == 0)
      - 文件所有者必须匹配当前进程 uid

    返回: True 表示权限安全, False 表示权限不安全
    """
    if _is_windows():
        return True  # Windows 上使用 icacls 管理, 跳过 POSIX 检查
    try:
        st = path.stat()
    except Exception:
        return False
    # 检查其他用户权限: 不允许任何组/其他人访问
    if st.st_mode & 0o077 != 0:
        return False
    # 检查所有者匹配
    if st.st_uid != os.getuid():
        return False
    return True


def _check_key_dir_safe(path: Path) -> bool:
    """验证密钥目录是否安全（所有者匹配且不是符号链接）。"""
    if _is_windows():
        return True
    try:
        st = os.lstat(path)
    except Exception:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if st.st_uid != os.getuid():
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    return True


class CryptoManager:
    """加解密管理：与 sandbox.work_dir 的密钥一起使用。"""

    def __init__(self, sandbox=None):
        self._sandbox = sandbox
        self._key_lock = threading.Lock()  # 保护 _load_key 中的密钥生成竞态

    # ---------- 内部辅助 ----------
    def _work_dir(self) -> Optional[Path]:
        """获取密钥存储根目录。
        优先使用 sandbox.work_dir；若不可用则回退到用户主目录下的 .dscode。
        """
        try:
            work = getattr(self._sandbox, "work_dir", None)
            if work:
                return Path(work).resolve()
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        # 回退：使用用户主目录下的 .dscode 作为全局密钥存储
        try:
            fallback = Path.home() / _KEY_DIR_NAME
            return fallback.resolve()
        except Exception:
            return None

    def _key_path(self) -> Optional[Path]:
        root = self._work_dir()
        if root is None:
            return None
        return root / _KEY_DIR_NAME / _KEY_FILE_NAME

    # ---------- 主接口 ----------
    def _ensure_dir(self) -> Optional[Path]:
        """确保密钥目录（仅 owner 可见）。"""
        key_path = self._key_path()
        if key_path is None:
            return None
        try:
            key_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        # 尝试限制目录权限（忽略失败）
        try:
            if not _is_windows():
                os.chmod(str(key_path.parent), stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return key_path

    def _load_key(self) -> Optional[bytes]:
        """读取/生成密钥。返回原始 bytes（urlsafe b64）。

        使用原子写入（temp file + os.replace）防止 TOCTOU 竞态 (P2-001)。
        """
        if not _CRYPTO_OK:
            return None
        key_path = self._ensure_dir()
        if key_path is None:
            return None
        try:
            if key_path.is_file():
                # P0-002 修复: 验证密钥文件权限和所有权
                if not _check_key_file_permissions(key_path):
                    return None
                # CWE-377 修复: 使用 O_NOFOLLOW 打开，防止符号链接攻击
                try:
                    fd = os.open(str(key_path), os.O_RDONLY | os.O_NOFOLLOW)
                except OSError:
                    return None
                try:
                    st = os.fstat(fd)
                    # 通过 fstat 避免 TOCTOU: 验证权限和所有权
                    if not _is_windows():
                        if st.st_mode & 0o077 != 0:
                            return None
                        if st.st_uid != os.getuid():
                            return None
                    data = os.read(fd, _FERNET_KEY_ENCODED_LEN + 8).strip()
                    if _validate_fernet_key(data):
                        _chmod_600(key_path)
                        return data
                finally:
                    os.close(fd)
        except Exception:
            return None
        # 生成新密钥 — 加锁防竞态，避免多线程同时生成不同密钥
        with self._key_lock:
            # 二次检查：可能其他线程已经生成并写入
            try:
                if key_path.is_file():
                    # P0-002 修复: 验证密钥文件权限和所有权
                    if not _check_key_file_permissions(key_path):
                        return None
                    # CWE-377 修复: 使用 O_NOFOLLOW 打开，防止符号链接攻击
                    try:
                        fd = os.open(str(key_path), os.O_RDONLY | os.O_NOFOLLOW)
                    except OSError:
                        return None
                    try:
                        st = os.fstat(fd)
                        if not _is_windows():
                            if st.st_mode & 0o077 != 0:
                                return None
                            if st.st_uid != os.getuid():
                                return None
                        data = os.read(fd, _FERNET_KEY_ENCODED_LEN + 8).strip()
                        if _validate_fernet_key(data):
                            _chmod_600(key_path)
                            return data
                    finally:
                        os.close(fd)
            except Exception:
                return None
            try:
                new_key = Fernet.generate_key()
            except Exception:
                return None
            try:
                # P2-001 修复: 使用临时文件 + 原子移动 (os.replace),
                # 防止 TOCTOU 竞态 — 攻击者无法在写入间隙替换密钥文件
                # CWE-377 修复: 校验目录所有者，防止符号链接目录攻击
                if not _check_key_dir_safe(key_path.parent):
                    return None
                import tempfile as _tempfile
                fd, tmp_path = _tempfile.mkstemp(
                    dir=str(key_path.parent), prefix=".tmp_key_",
                )
                try:
                    # 校验临时文件 fd 的所有权
                    if not _is_windows():
                        tmp_st = os.fstat(fd)
                        if tmp_st.st_uid != os.getuid():
                            raise PermissionError("临时密钥文件所有者不是当前用户")
                    with os.fdopen(fd, "wb") as f:
                        f.write(new_key)
                    # 设置权限后再移动
                    _chmod_600(Path(tmp_path))
                    # 原子替换 (Unix rename / Windows MoveFileEx)
                    os.replace(tmp_path, key_path)
                    return new_key
                except Exception:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    try:
                        os.unlink(tmp_path)
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                    raise
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return None

    def encrypt_bytes(self, data: bytes) -> Tuple[bool, bytes]:
        key = self._load_key()
        if key is None or Fernet is None:
            return False, b""
        try:
            f = Fernet(key)
            return True, f.encrypt(data)
        except Exception:
            return False, b""

    def decrypt_bytes(self, data: bytes) -> Tuple[bool, bytes]:
        key = self._load_key()
        if key is None or Fernet is None:
            return False, b""
        try:
            f = Fernet(key)
            return True, f.decrypt(data)
        except Exception:
            return False, b""

    def encrypt_text(self, text: str) -> str:
        # P2-005 修复: 使用 bytearray 替代 bytes,
        # 在 finally 块中显式覆写内存中的敏感数据
        data = bytearray()
        try:
            data = bytearray(text.encode("utf-8"))
        except Exception:
            return ""
        ok = False
        enc = b""
        try:
            ok, enc = self.encrypt_bytes(bytes(data))
        finally:
            # 覆写内存中的明文数据
            for i in range(len(data)):
                data[i] = 0
        if not ok:
            return ""
        try:
            return enc.decode("ascii")
        except Exception:
            return ""

    def decrypt_text(self, encoded: str) -> str:
        if not isinstance(encoded, str) or not encoded.strip():
            return ""
        try:
            data = encoded.encode("ascii")
        except Exception:
            return ""
        ok, dec = self.decrypt_bytes(data)
        if not ok:
            return ""
        try:
            return dec.decode("utf-8", errors="replace")
        except Exception:
            return ""


# -------- 顶层兼容层（供 Config 等外部调用方使用） --------
import threading as _threading
_crypto_lock = _threading.Lock()
_crypto_instance: Optional["CryptoManager"] = None


def _get_crypto() -> CryptoManager:
    """惰性、线程安全地获取全局 CryptoManager。"""
    global _crypto_instance
    if _crypto_instance is None:
        with _crypto_lock:
            if _crypto_instance is None:
                _crypto_instance = CryptoManager()
    return _crypto_instance


def encrypt_value(value: str) -> str:
    """线程安全：加密字符串，失败返回空字符串。"""
    if not isinstance(value, str) or not value:
        return ""
    try:
        return _get_crypto().encrypt_text(value)
    except Exception:
        return ""


def decrypt_value(value: str) -> str:
    """线程安全：解密字符串，失败返回空字符串（不泄漏异常给调用方）。"""
    if not isinstance(value, str) or not value:
        return ""
    try:
        return _get_crypto().decrypt_text(value)
    except Exception:
        return ""


def reset_key() -> bool:
    """删除当前密钥文件，下次加解密时自动生成新密钥。

    用于配置损坏或机器指纹变更导致旧密钥无法解密的场景。
    返回是否成功删除。
    """
    try:
        cm = _get_crypto()
        key_path = cm._key_path()
        if key_path is not None and key_path.is_file():
            try:
                key_path.unlink()
                logger.warning("已重置加密密钥文件: %s", key_path)
                return True
            except Exception as _exc:
                logger.warning("重置加密密钥文件失败: %s", _exc)
                return False
    except Exception as _exc:
        logger.warning("重置加密密钥失败: %s", _exc)
    return False
