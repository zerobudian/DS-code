# -*- coding: utf-8 -*-
"""全局配置管理"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import io
import json
import os
import platform
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from src.core.crypto import encrypt_value, decrypt_value


# M-008: JSON 反序列化大小上限 50 MB
MAX_JSON_SIZE = 50 * 1024 * 1024


def _get_user_data_dir() -> Path:
    # 1) 环境变量显式指定（最高优先级）
    env_dir = os.environ.get("DSCODE_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    # 2) 便携模式：从 config.py 所在目录往上找 dscode_data/
    #    不要依赖 sys.argv[0]（在 module import 时可能指向 -c / pytest 等）
    try:
        _this_file = Path(__file__).resolve()
        # 从 src/core/config.py 往上走两层到项目根
        _project_root = _this_file.parent.parent.parent
        _portable = _project_root / "dscode_data"
        if _portable.exists() and _portable.is_dir():
            return _portable
    except Exception:
        pass

    # 3) 系统默认路径
    try:
        import platformdirs
        return Path(platformdirs.user_data_dir("dscode", "dscode"))
    except ImportError:
        pass

    system = sys.platform
    if system == "linux":
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        return Path(base) / "dscode"
    elif system == "darwin":
        return Path.home() / "Library" / "Application Support" / "dscode"
    elif system == "win32":
        return Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "dscode"
    else:
        return Path.home() / ".dscode"


def _atomic_write_json(path: Path, data: Any, ensure_utf8: bool = True) -> bool:
    """原子化写入 JSON：先写临时文件，成功后 os.replace 替换目标文件。

    防止进程在写入中途被 kill 导致目标文件残留部分内容（无法解析的 JSON）。
    写入前限制 umask 并使用 O_EXCL | 0o600 创建临时文件，避免权限泄露；
    若目标文件是符号链接则拒绝写入（H-004）。
    目录/权限类 OSError 会向上抛出，供调用方判断是否为可恢复降级。
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 目录不存在或不可写：需要调用方感知以做优雅降级
        raise
    except Exception as _exc:
        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    # H-004: 拒绝写入符号链接目标，防止配置被重定向到外部路径
    try:
        if target.is_symlink() or os.path.islink(str(target)):
            raise OSError(f"目标文件是符号链接，拒绝写入: {target}")
    except OSError:
        raise
    except Exception as _exc:
        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    # H-004: 写入前收紧 umask，确保临时文件仅所有者可读写
    old_umask = os.umask(0o077)
    try:
        parent = target.parent
        tmp_name = None
        tmp_path = None
        fd = None
        # 使用 O_EXCL 创建唯一临时文件，避免复用已被替换/符号链接的文件
        for _ in range(10):
            candidate = f".dscode_{os.getpid()}_{os.urandom(8).hex()}.tmp"
            tmp_path = parent / candidate
            try:
                fd = os.open(
                    str(tmp_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                tmp_name = str(tmp_path)
                break
            except FileExistsError:
                continue
        if fd is None:
            raise OSError("无法创建唯一临时文件")

        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as f:
                json.dump(data, f, ensure_ascii=ensure_utf8, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            # 原子替换
            try:
                os.replace(tmp_name, str(target))
            except OSError:
                # Fallback：Windows 或某些环境下 replace 可能受限
                shutil.move(tmp_name, str(target))
            # M-003: 限制配置文件权限为 0o600
            try:
                os.chmod(str(target), 0o600, follow_symlinks=False)
            except OSError as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return True
        except Exception:
            try:
                if tmp_name:
                    os.unlink(tmp_name)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            raise
    except OSError as e:
        print(f"[Config] 原子写入失败 ({target}): {e}")
        raise
    except Exception as e:
        print(f"[Config] 原子写入失败 ({target}): {e}")
        return False
    finally:
        os.umask(old_umask)


class Config:
    """全局配置单例（线程安全）。

    线程安全要点：
    - _instance 的创建受 _instance_lock（RLock）保护，避免竞态创建两个实例。
    - 对 data 的读写通过 _instance_lock 保护（多读、多写都要持锁）。
    - get/set 对外返回值的**副本**（对可变对象），避免调用方误改内部状态。
    - 保存/加载操作持锁执行，避免多线程同时落盘造成残缺 JSON。
    """

    _instance = None
    _instance_lock = threading.RLock()  # 保护单例创建 & 内部状态读写
    # 用户数据目录（延迟计算——类属性在 import 时算，import 时 CWD 可能未就绪）
    _BASE_DIR = None
    _PROJECTS_DIR = None
    _CONFIG_FILE = None

    @classmethod
    def _get_base_dir(cls) -> Path:
        if cls._BASE_DIR is None:
            cls._BASE_DIR = _get_user_data_dir()
        return cls._BASE_DIR

    @classmethod
    def _get_config_file(cls) -> Path:
        if cls._CONFIG_FILE is None:
            cls._CONFIG_FILE = cls._get_base_dir() / "config.json"
        return cls._CONFIG_FILE

    @classmethod
    def _get_projects_dir(cls) -> Path:
        if cls._PROJECTS_DIR is None:
            cls._PROJECTS_DIR = cls._get_base_dir() / "projects"
        return cls._PROJECTS_DIR

    # 向后兼容属性——调用上面的 lazy getter
    @property
    def BASE_DIR(self):
        return self._get_base_dir()

    @property
    def PROJECTS_DIR(self):
        return self._get_projects_dir()

    @property
    def CONFIG_FILE(self):
        return self._get_config_file()

    # P1-1: 批量保存防抖机制
    _save_dirty: bool = False
    _save_timer: Optional[threading.Timer] = None
    _save_debounce_interval: float = 1.0  # 防抖间隔（秒）

    # 旧版源码目录路径（用于向后兼容迁移）
    _LEGACY_BASE_DIR = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    _LEGACY_CONFIG_FILE = _LEGACY_BASE_DIR / "config.json"

    DEFAULT_CONFIG = {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro",
        "available_models": [
            {
                "name": "deepseek-v4-pro",
                "label": "DeepSeek-V4-Pro",
                "description": "更强推理，适合复杂任务和计划决策（主模型）",
                "primary": True,
            },
            {
                "name": "deepseek-v4-flash",
                "label": "DeepSeek-V4-Flash",
                "description": "快速响应，高性价比，适合子代理执行和简单任务",
                "primary": False,
            },
        ],
        "subagent": {
            "max_parallel": 5,           # 最多并行子代理数
            "default_model": "deepseek-v4-flash",  # 子代理默认模型
            "allow_model_switch": True,  # 允许子代理选择不同模型
            "max_tokens_per_subagent": 12000,
        },
        "username": "User",
        "language": "zh",           # zh / en / wenyan
        "theme": "light",              # light / dark
        "theme_mode": "light",         # light / dark / system
        "reasoning_effort": "high",
        "chat_mode": "normal",
        "balance": 0.0,
        "nickname": "",
        "onboarding_completed": False,
        "last_project": None,
        "permission_mode": "full",
        "dark_mode": False,
        "auto_build_context": True,
        "balance_cny": 0.0,
        "cache_hit_rate": 0.0,
        "total_tokens_used": 0,
    }

    # 合法的权限模式
    VALID_PERMISSION_MODES = ("reviewer", "coder", "full")

    # 已知模型白名单（M-005）：配置加载后据此校验 available_models，防止动态列表被污染
    KNOWN_MODELS = {
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-chat",
        "deepseek-reasoner",
        "deepseek-v3",
        "deepseek-flash",
    }

    # 需要加密存储的 key 列表
    _ENCRYPTED_KEYS = {"api_key"}

    @classmethod
    def _coerce_to_default_type(cls, default_val: Any, loaded_val: Any) -> Any:
        """将 loaded_val 温和转换为 default_val 的类型；失败回退默认值。

        统一的类型转换逻辑，供 _deep_merge_defaults 复用。
        - 类型一致 → 直接返回 loaded_val
        - default 为 None → 信任 loaded_val
        - bool: str→bool("1"/"true"/"yes"/"on"/"y"/"t"), int/float→bool
        - int/float: str→int/float
        - str: 非 dict/list → str
        - 其他 → 回退默认值
        """
        # 类型一致直接返回
        if type(loaded_val) is type(default_val):
            return loaded_val
        # default 为 None → 信任 loaded 值
        if default_val is None:
            return loaded_val
        try:
            if isinstance(default_val, bool):
                if isinstance(loaded_val, str):
                    low = loaded_val.strip().lower()
                    return low in ("1", "true", "yes", "on", "y", "t")
                elif isinstance(loaded_val, (int, float)):
                    return bool(loaded_val)
                else:
                    return default_val
            elif isinstance(default_val, (int, float)) and isinstance(loaded_val, str):
                return type(default_val)(loaded_val)
            elif isinstance(default_val, (int, float)) and isinstance(loaded_val, (int, float)):
                return type(default_val)(loaded_val)
            elif isinstance(default_val, str) and not isinstance(loaded_val, (dict, list)):
                return str(loaded_val)
            else:
                return default_val
        except (ValueError, TypeError) as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return default_val

    @classmethod
    def _deep_merge_defaults(cls, loaded: dict) -> dict:
        """将加载到的配置与 DEFAULT_CONFIG 深度合并，补齐缺失字段。

        - 顶层缺失 key → 从默认值填充
        - subagent 等嵌套 dict → 递归填充
        - 类型与默认值不一致 → 委托 _coerce_to_default_type 做类型安全转换
        """
        if not isinstance(loaded, dict):
            return dict(cls.DEFAULT_CONFIG)
        result = dict(cls.DEFAULT_CONFIG)  # 顶层浅拷贝（值为不可变或 list/dict 将在下面处理）
        for key, default_val in cls.DEFAULT_CONFIG.items():
            loaded_val = loaded.get(key, None)
            # key 不存在 → 直接用默认值（上面已经赋值过）
            if key not in loaded:
                continue
            # 嵌套 dict → 递归合并
            if isinstance(default_val, dict) and isinstance(loaded_val, dict):
                merged_sub = dict(default_val)
                for sk, sv in default_val.items():
                    if sk in loaded_val:
                        merged_sub[sk] = cls._coerce_to_default_type(sv, loaded_val[sk])
                result[key] = merged_sub
                continue
            # list 类型：如果磁盘也是 list 则信任；否则用默认
            if isinstance(default_val, list) and isinstance(loaded_val, list):
                result[key] = loaded_val
                continue
            # 基本类型：委托统一类型转换
            result[key] = cls._coerce_to_default_type(default_val, loaded_val)
        # 把 loaded 中不在 DEFAULT_CONFIG 的额外键也原样保留
        # （否则新增配置字段如 onboarding_completed 会被静默丢弃）
        for key, val in loaded.items():
            if key not in result and val is not None:
                result[key] = val
        return result

    @classmethod
    def _validate_available_models(cls, data: dict) -> None:
        """校验 available_models 中的模型名是否在白名单中（M-005）。

        未知模型仅记录 warning，不阻止启动，避免供应商更新模型名后应用无法使用。
        """
        if not isinstance(data, dict):
            return
        models = data.get("available_models")
        if not isinstance(models, list):
            return
        for item in models:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name not in cls.KNOWN_MODELS:
                logger.warning(
                    "available_models 包含未知模型 '%s'，可能被污染或拼写错误", name
                )

    @classmethod
    def _validate_config_types(cls, data: dict) -> None:
        """对关键配置字段做类型检查，类型错误时 raise ValueError。"""
        if not isinstance(data, dict):
            raise ValueError("配置根节点必须是 dict")

        expected: Dict[str, Union[type, Tuple[type, ...]]] = {
            "api_key": str,
            "base_url": str,
            "model": str,
            "username": str,
            "language": str,
            "theme": str,
            "theme_mode": str,
            "reasoning_effort": str,
            "nickname": str,
            "custom_rules": str,
            "provider_type": str,
            "chat_mode": str,
            "permission": str,
            "permission_mode": str,
            "dark_mode": bool,
            "auto_build_context": bool,
            "balance": (int, float),
            "balance_cny": (int, float),
            "cache_hit_rate": (int, float),
            "total_tokens_used": int,
        }
        for key, expected_type in expected.items():
            if key in data and data[key] is not None:
                if not isinstance(data[key], expected_type):
                    raise ValueError(
                        f"配置字段 '{key}' 类型错误: 期望 {expected_type}, 得到 {type(data[key])}"
                    )

        # permission_mode 值域校验
        if "permission_mode" in data and data["permission_mode"] is not None:
            if data["permission_mode"] not in cls.VALID_PERMISSION_MODES:
                raise ValueError(
                    f"配置字段 'permission_mode' 值非法: {data['permission_mode']!r}, "
                    f"必须是 {cls.VALID_PERMISSION_MODES} 之一"
                )

        subagent = data.get("subagent")
        if isinstance(subagent, dict):
            sub_expected: Dict[str, type] = {
                "max_parallel": int,
                "default_model": str,
                "allow_model_switch": bool,
                "max_tokens_per_subagent": int,
            }
            for key, expected_type in sub_expected.items():
                if key in subagent and subagent[key] is not None:
                    if not isinstance(subagent[key], expected_type):
                        raise ValueError(
                            f"配置字段 'subagent.{key}' 类型错误: 期望 {expected_type}, 得到 {type(subagent[key])}"
                        )

        providers = data.get("providers")
        if providers is not None and not isinstance(providers, list):
            raise ValueError("配置字段 'providers' 类型错误: 期望 list")
        active_idx = data.get("active_provider_index")
        if active_idx is not None and not isinstance(active_idx, int):
            raise ValueError("配置字段 'active_provider_index' 类型错误: 期望 int")

    _EXPECTED_TYPES: Dict[str, Union[type, Tuple[type, ...]]] = {
        "api_key": str,
        "base_url": str,
        "model": str,
        "username": str,
        "language": str,
        "theme": str,
        "theme_mode": str,
        "reasoning_effort": str,
        "nickname": str,
        "custom_rules": str,
        "provider_type": str,
        "chat_mode": str,
        "permission": str,
        "permission_mode": str,
        "dark_mode": bool,
        "auto_build_context": bool,
        "balance": (int, float),
        "balance_cny": (int, float),
        "cache_hit_rate": (int, float),
        "total_tokens_used": int,
        "active_provider_index": int,
    }

    def _coerce_value(self, key: str, value: Any) -> Any:
        """将写入值温和地转换为目标字段的期望类型；无法转换时回退到默认值。"""
        expected = self._EXPECTED_TYPES.get(key)
        if expected is None:
            return value
        if value is None:
            return self.DEFAULT_CONFIG.get(key)
        if isinstance(value, expected):
            return value
        try:
            if expected is bool:
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "yes", "on", "y", "t")
                return bool(value)
            if expected is int:
                # 显式排除 bool，因为 isinstance(True, int) 为 True
                if isinstance(value, bool):
                    return self.DEFAULT_CONFIG.get(key)
                return int(value)
            if expected is float:
                return float(value)
            if expected is str:
                return str(value)
            if expected == (int, float):
                return float(value)
        except (ValueError, TypeError) as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return self.DEFAULT_CONFIG.get(key)

    @classmethod
    def _decrypt_loaded(cls, data: Any) -> Any:
        """递归解密加载数据中的敏感字段（支持 providers 等嵌套结构）。"""
        if isinstance(data, dict):
            result = {}
            for k, v in data.items():
                if k in cls._ENCRYPTED_KEYS and isinstance(v, str) and v:
                    decrypted = decrypt_value(v)
                    if not decrypted and v:
                        # 解密失败时不阻断启动，清空该字段并提示用户重新输入
                        logger.warning(
                            "无法解密敏感字段 '%s'，已清空该字段，请重新输入", k
                        )
                        result[k] = ""
                    else:
                        result[k] = decrypted
                else:
                    result[k] = cls._decrypt_loaded(v)
            return result
        if isinstance(data, list):
            return [cls._decrypt_loaded(item) for item in data]
        return data

    @classmethod
    def _encrypt_save_data(cls, data: Any) -> Any:
        """递归加密待保存数据中的敏感字段（支持 providers 等嵌套结构）。"""
        if isinstance(data, dict):
            result = {}
            for k, v in data.items():
                if k in cls._ENCRYPTED_KEYS and isinstance(v, str) and v:
                    encrypted = encrypt_value(v)
                    if not encrypted:
                        logger.error("加密敏感字段 '%s' 失败，中止保存", k)
                        raise RuntimeError(
                            f"配置保存失败：敏感字段 '{k}' 加密失败，未写入磁盘。"
                        )
                    result[k] = encrypted
                else:
                    result[k] = cls._encrypt_save_data(v)
            return result
        if isinstance(data, list):
            return [cls._encrypt_save_data(item) for item in data]
        return data

    def __new__(cls):
        # 双检锁：减少多数情况下的锁开销
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._load()  # _instance_lock 为 RLock，允许同一线程重入，安全
                    cls._instance = instance
        return cls._instance

    def _migrate_legacy_config(self):
        """如果源码目录下有旧的 config.json，迁移到新位置。"""
        if self._LEGACY_CONFIG_FILE.exists() and not self.CONFIG_FILE.exists():
            try:
                import shutil
                import stat
                # 安全校验：源文件必须由当前用户拥有且 group/other 不可写
                try:
                    st = self._LEGACY_CONFIG_FILE.stat()
                    if hasattr(os, "getuid") and st.st_uid != os.getuid():
                        print("[Config] 拒绝迁移：旧配置文件所有者不是当前用户")
                        return
                    mode = stat.S_IMODE(st.st_mode)
                    if mode & stat.S_IWGRP or mode & stat.S_IWOTH:
                        print("[Config] 拒绝迁移：旧配置文件对 group/other 可写")
                        return
                except Exception as e:
                    print(f"[Config] 校验旧配置文件权限失败: {e}")
                    return
                self.BASE_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self._LEGACY_CONFIG_FILE, self.CONFIG_FILE)
                # 限制目标文件权限：仅所有者可读写
                try:
                    self.CONFIG_FILE.chmod(0o600)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                print(f"[Config] 已从 {self._LEGACY_CONFIG_FILE} 迁移配置到 {self.CONFIG_FILE}")
                # 将旧文件重命名为备份
                bak_path = self._LEGACY_CONFIG_FILE.with_suffix(".json.bak")
                try:
                    self._LEGACY_CONFIG_FILE.rename(bak_path)
                    print(f"[Config] 旧配置文件已备份为 {bak_path}")
                except Exception as e:
                    print(f"[WARN] 无法重命名旧配置文件: {e}")
            except Exception as e:
                print(f"[Config] 迁移旧配置失败: {e}")

    def _load(self):
        """从文件加载配置。

        - 存在旧版 ~/.dscode/config.json 或源码目录 config.json → 自动迁移
        - 文件存在但损坏 → 回退到 DEFAULT_CONFIG（不覆盖损坏文件）
        - 敏感字段（api_key）解密失败 → 清空该字段并记录警告，不阻断启动
        - 关键字段类型错误 → raise ValueError
        """
        try:
            self._migrate_legacy_config()
        except Exception as e:
            print(f"[Config] 旧配置迁移出错（忽略，继续使用默认路径）: {e}")
        try:
            self.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        if self.CONFIG_FILE.exists():
            try:
                # 先以原子方式读取：一次性读入内存，再 json.loads
                raw_bytes = None
                try:
                    with open(self.CONFIG_FILE, "rb") as f:
                        raw_bytes = f.read()
                except OSError as e:
                    raise RuntimeError(f"无法读取配置文件: {e}") from e
                # M-008: JSON 反序列化前检查大小，防止超大配置文件导致 OOM
                if len(raw_bytes) > MAX_JSON_SIZE:
                    print(f"[Config] 配置文件超过 {MAX_JSON_SIZE} bytes 大小限制，使用默认配置")
                    self.data = dict(self.DEFAULT_CONFIG)
                    return
                try:
                    loaded = json.loads(raw_bytes.decode("utf-8"))
                except UnicodeDecodeError:
                    loaded = json.loads(raw_bytes.decode("utf-8", errors="replace"))
                if not isinstance(loaded, dict):
                    raise ValueError("配置根节点不是 JSON object")

                # 递归解密敏感字段（支持 providers 等嵌套结构）；解密失败必须 raise RuntimeError，禁止回退明文
                decrypted = self._decrypt_loaded(loaded)
                if not isinstance(decrypted, dict):
                    raise ValueError("解密后配置根节点不是 JSON object")

                # 对解密后的加载值做关键字段类型检查（在合并默认值前拦截恶意/损坏类型）
                self._validate_config_types(decrypted)
                # 深度合并默认值（向后兼容：新字段/类型偏差时保持安全）
                merged = self._deep_merge_defaults(decrypted)
                self._validate_config_types(merged)
                # M-005: 校验可用模型列表，防止动态模型列表被污染
                self._validate_available_models(merged)
                self.data = merged
            except json.JSONDecodeError as e:
                # 配置文件损坏：回退默认，不覆盖损坏文件
                print(f"[Config] 配置文件损坏，使用默认配置: {e}")
                self.data = dict(self.DEFAULT_CONFIG)
            except (RuntimeError, ValueError):
                # 安全/校验类异常必须向上抛出，禁止静默回退到默认配置
                raise
            except Exception as e:
                print(f"[Config] 加载失败，使用默认配置: {e}")
                self.data = dict(self.DEFAULT_CONFIG)
        else:
            self.data = dict(self.DEFAULT_CONFIG)
            # 初始化：原子写一次（失败不影响后续写入能力）
            try:
                self._save()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _get_lock_file(self) -> Path:
        """获取配置文件的锁文件路径。"""
        return self.CONFIG_FILE.parent / f".{self.CONFIG_FILE.name}.lock"

    def _acquire_lock(
        self, lock_path: Path, timeout: int = 5
    ) -> Optional[io.TextIOWrapper]:
        """获取跨进程文件锁，返回锁文件对象；失败返回 None。

        Unix 使用 fcntl.flock，Windows 使用 msvcrt.locking。
        支持超时，超时后放弃并返回 None。
        返回文件对象而非 fd，避免 fd 在函数返回后被 Python 文件对象析构关闭。
        """
        try:
            lock_file = open(str(lock_path), "w")
        except (IOError, OSError):
            return None

        fd = lock_file.fileno()
        deadline = time.monotonic() + timeout
        is_windows = platform.system() == "Windows"
        while True:
            try:
                if is_windows:
                    import msvcrt
                    # LK_NBLCK 为非阻塞；若失败会抛出 OSError
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return lock_file
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return lock_file
            except (IOError, OSError):
                if time.monotonic() >= deadline:
                    try:
                        lock_file.close()
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                    return None
                time.sleep(0.05)

    def _save_safe(self):
        """P1-1: 线程安全的防抖保存方法，供属性 setter 使用。

        不再每次 setter 都立即写盘，而是标记 dirty 并启动 1 秒延迟定时器。
        1 秒内的多次 setter 调用会合并为一次磁盘写入。
        紧急场景可调用 save() 立即同步写入。
        当 _save_debounce_interval <= 0 时，退化为同步保存（供测试使用）。
        """
        if self._save_debounce_interval <= 0:
            # 同步保存模式（测试环境）
            with self._instance_lock:
                self._save_dirty = False
            self._save()
            return
        with self._instance_lock:
            self._save_dirty = True
            # 如果已有定时器在等待，不重复创建
            if self._save_timer is not None:
                return
        # 启动防抖定时器（在锁外创建，避免锁内 I/O）
        self._save_timer = threading.Timer(
            self._save_debounce_interval,
            self._flush_debounced_save,
        )
        self._save_timer.daemon = True
        self._save_timer.name = "ConfigDebouncedSave"
        self._save_timer.start()

    def _flush_debounced_save(self):
        """P1-1: 防抖定时器回调，执行实际的磁盘写入。"""
        with self._instance_lock:
            self._save_timer = None
            if not self._save_dirty:
                return
            self._save_dirty = False
        # 在锁外执行实际保存（_save 内部会重新获取锁）
        self._save()

    def _flush_pending_save(self):
        """P1-1: 立即执行所有待保存的变更（如果有）。

        供应用退出、设置对话框关闭等场景调用，确保不丢失数据。
        """
        timer_to_cancel = None
        with self._instance_lock:
            if self._save_timer is not None:
                timer_to_cancel = self._save_timer
                self._save_timer = None
            dirty = self._save_dirty
            self._save_dirty = False
        if timer_to_cancel is not None:
            try:
                timer_to_cancel.cancel()
            except Exception:
                pass
        if dirty:
            self._save()

    def _save(self):
        """保存配置到文件（敏感字段自动加密 + 原子写入 + 跨进程文件锁）。

        持锁执行，避免多线程/多进程并发写入中间态 JSON。
        """
        lock_path = self._get_lock_file()
        lock_file: Optional[io.TextIOWrapper] = None
        try:
            lock_file = self._acquire_lock(lock_path, timeout=5)
            if lock_file is None:
                logger.warning("无法获取配置写锁，配置保存被跳过")
                return
            with self._instance_lock:
                self._save_locked()
        finally:
            if lock_file is not None:
                try:
                    if platform.system() == "Windows":
                        import msvcrt
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    lock_file.close()
                    try:
                        os.unlink(str(lock_path))
                    except OSError:
                        pass
                except (OSError, Exception) as _exc:
                    logger.debug("释放配置锁失败: %s", _exc, exc_info=True)

    def _save_locked(self):
        """在锁内完成持久化（调用方必须已持有 _instance_lock）。

        与 _save() 的区别：不额外获取锁，避免 set()/update() 两次持锁之间的竞态窗口。
        注意：此方法不获取跨进程文件锁，仅用于已确认单进程场景。
        属性 setter 应调用 _save_safe() 以获取完整的跨进程锁保护。

        安全行为（MEDIUM-003）：
        - 敏感字段加密失败时记录日志（不泄露原值）并中止保存，抛出 RuntimeError。
        - 写入前创建 .bak 备份；写入失败时尝试恢复备份。
        - 写入后读取验证，确保加密字段非空。
        """
        if not isinstance(self.data, dict):
            raise RuntimeError("[Config] self.data 不是 dict，拒绝写入。")

        # 递归加密敏感字段（构造副本，不影响 self.data；支持 providers 嵌套）
        save_data = self._encrypt_save_data(self.data)
        if not isinstance(save_data, dict):
            raise RuntimeError("[Config] 加密后配置根节点不是 dict，拒绝写入。")

        # 确保目录存在
        try:
            self.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        # 写入前备份：保留上一个可用版本，以便失败时恢复
        backup_path = self.CONFIG_FILE.with_suffix(self.CONFIG_FILE.suffix + ".bak")
        backup_created = False
        if self.CONFIG_FILE.exists():
            try:
                shutil.copy2(str(self.CONFIG_FILE), str(backup_path))
                backup_created = True
            except Exception as _exc:
                logger.warning("创建配置备份失败: %s", _exc, exc_info=True)

        def _restore_backup():
            """尝试用备份恢复原始配置。"""
            if not backup_created or not backup_path.exists():
                return
            try:
                shutil.copy2(str(backup_path), str(self.CONFIG_FILE))
                logger.warning("配置保存失败，已从 .bak 恢复备份")
            except Exception as _exc:
                logger.error("恢复配置备份失败: %s", _exc, exc_info=True)

        write_ok = False
        try:
            write_ok = _atomic_write_json(self.CONFIG_FILE, save_data)
            if not write_ok:
                # 原子写入失败时不使用普通写入 fallback，避免不完整/损坏配置落盘
                raise RuntimeError(
                    f"配置原子写入失败: {self.CONFIG_FILE}"
                )

            # 写入后读取验证：确保加密字段非空
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    written = json.load(f)
            except Exception as e:
                _restore_backup()
                raise RuntimeError(
                    f"配置保存后读取验证失败: {e}"
                ) from e

            for k in self._ENCRYPTED_KEYS:
                expected = self.data.get(k)
                if isinstance(expected, str) and expected:
                    actual = written.get(k)
                    if not isinstance(actual, str) or not actual:
                        _restore_backup()
                        raise RuntimeError(
                            f"配置保存验证失败：敏感字段 '{k}' 为空或丢失"
                        )
        except Exception:
            if not write_ok:
                _restore_backup()
            raise

    # 对外暴露的 save（小写）作为 _save 的别名，便于外部模块直接调用
    def save(self):
        """save() 是 _save() 的公共别名。"""
        self._save()

    def get(self, key, default=None):
        """通用 get —— 线程安全：在锁内读取并返回；对 dict/list 类型返回浅副本。"""
        try:
            with self._instance_lock:
                if isinstance(self.data, dict) and key in self.data:
                    value = self.data[key]
                    if value is None:
                        return default
                    # 返回不可变类型原样；对 dict/list 返回浅拷贝，避免外部修改污染内部状态
                    if isinstance(value, dict):
                        return dict(value)
                    if isinstance(value, list):
                        return list(value)
                    return value
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return default

    def set(self, key, value):
        """通用 set —— 线程安全+进程安全：持 instance_lock 更新 data，再通过 _save() 获取跨进程文件锁写入。"""
        try:
            with self._instance_lock:
                if not isinstance(self.data, dict):
                    self.data = dict(self.DEFAULT_CONFIG)
                self.data[key] = self._coerce_value(key, value)
            # _save() 内部获取跨进程文件锁后重新获取 instance_lock（RLock 可重入）
            self._save()
        except (RuntimeError, ValueError):
            # 安全/校验类异常必须向上抛出，禁止静默吞掉
            raise
        except OSError as e:
            # 目录不可写等 IO 问题：保留内存配置并记录日志，不阻断程序运行
            print(f"[Config] set({key!r}) 写入失败（将仅保留在内存）: {e}")
        except Exception as e:
            print(f"[Config] set({key!r}) 失败: {e}")

    def update(self, **kwargs):
        """线程安全+进程安全的批量更新：批量应用 kwargs 后通过 _save() 原子保存。"""
        try:
            with self._instance_lock:
                if not isinstance(self.data, dict):
                    self.data = dict(self.DEFAULT_CONFIG)
                coerced = {k: self._coerce_value(k, v) for k, v in kwargs.items()}
                self.data.update(coerced)
            # _save() 内部获取跨进程文件锁后重新获取 instance_lock（RLock 可重入）
            self._save()
        except (RuntimeError, ValueError):
            # 安全/校验类异常必须向上抛出，禁止静默吞掉
            raise
        except OSError as e:
            # 目录不可写等 IO 问题：保留内存配置并记录日志，不阻断程序运行
            print(f"[Config] update 写入失败（将仅保留在内存）: {e}")
        except Exception as e:
            print(f"[Config] update 失败: {e}")

    def get_api_key(self) -> str:
        """获取 API Key（内部解密）。线程安全。"""
        try:
            with self._instance_lock:
                return self.data.get("api_key", "")
        except Exception:
            return ""

    @property
    def active_provider(self) -> dict:
        """返回当前激活的供应商配置副本；无有效供应商时返回空 dict。"""
        with self._instance_lock:
            providers = self.data.get("providers")
            if not isinstance(providers, list) or not providers:
                return {}
            idx = self.data.get("active_provider_index", 0)
            if not isinstance(idx, int) or idx < 0 or idx >= len(providers):
                idx = 0
            p = providers[idx]
            if not isinstance(p, dict):
                return {}
            return dict(p)

    @property
    def active_provider_index(self) -> int:
        with self._instance_lock:
            idx = self.data.get("active_provider_index", 0)
            if not isinstance(idx, int):
                idx = 0
            providers = self.data.get("providers", [])
            if isinstance(providers, list) and idx >= len(providers):
                idx = max(0, len(providers) - 1)
            return idx

    @active_provider_index.setter
    def active_provider_index(self, value):
        value = self._coerce_value("active_provider_index", value)
        with self._instance_lock:
            self.data["active_provider_index"] = value
        self._save_safe()

    # 快捷属性：全部线程安全，读/写持锁
    @property
    def api_key(self):
        """获取 API Key：优先级 环境变量 DSCODE_API_KEY > 激活供应商 > 配置文件 > 空字符串。"""
        with self._instance_lock:
            env_key = os.environ.get("DSCODE_API_KEY", "")
            if env_key:
                return env_key
            active = self.active_provider
            if active.get("api_key"):
                return active["api_key"]
            return self.data.get("api_key", "")

    @api_key.setter
    def api_key(self, value):
        with self._instance_lock:
            self.data["api_key"] = value
        self._save_safe()

    @property
    def model(self):
        with self._instance_lock:
            active = self.active_provider
            if active.get("model"):
                return active["model"]
            return self.data.get("model", "deepseek-v4-pro")

    @model.setter
    def model(self, value):
        with self._instance_lock:
            self.data["model"] = str(value or "deepseek-v4-pro")
        self._save_safe()

    @property
    def base_url(self) -> str:
        with self._instance_lock:
            active = self.active_provider
            if active.get("base_url"):
                return active["base_url"]
            return self.data.get("base_url", "https://api.deepseek.com")

    @base_url.setter
    def base_url(self, value):
        with self._instance_lock:
            self.data["base_url"] = str(value or "https://api.deepseek.com")
        self._save_safe()

    @property
    def language(self):
        with self._instance_lock:
            return self.data.get("language", "zh")

    @language.setter
    def language(self, value):
        with self._instance_lock:
            self.data["language"] = str(value or "zh").lower()
        self._save_safe()

    @property
    def permission_mode(self):
        """获取当前权限模式。优先级：环境变量 DSCODE_PERMISSION_MODE > 配置文件。

        返回值恒为 'reviewer' / 'coder' / 'full' 之一；非法值回退到 'coder'。
        """
        with self._instance_lock:
            env_mode = os.environ.get("DSCODE_PERMISSION_MODE", "").strip().lower()
            if env_mode in self.VALID_PERMISSION_MODES:
                return env_mode
            val = self.data.get("permission_mode", "coder")
            if val in self.VALID_PERMISSION_MODES:
                return val
            return "coder"

    @permission_mode.setter
    def permission_mode(self, value):
        value = str(value or "coder").strip().lower()
        if value not in self.VALID_PERMISSION_MODES:
            value = "coder"
        with self._instance_lock:
            self.data["permission_mode"] = value
        # 权限模式是安全敏感配置：必须立即同步写盘，避免防抖延迟导致进程
        # 重启或单例重建后回退到默认值（coder），从而静默放大权限。
        self._save_safe()
        self._flush_pending_save()

    @property
    def theme(self):
        with self._instance_lock:
            return self.data.get("theme", "light")

    @theme.setter
    def theme(self, value):
        with self._instance_lock:
            self.data["theme"] = str(value or "light").lower()
        self._save_safe()

    @property
    def dark_mode(self):
        with self._instance_lock:
            return bool(self.data.get("dark_mode", False)) or (self.data.get("theme") == "dark")

    @dark_mode.setter
    def dark_mode(self, value):
        with self._instance_lock:
            self.data["dark_mode"] = bool(value)
            if bool(value):
                self.data["theme"] = "dark"
            else:
                self.data["theme"] = "light"
        self._save_safe()

    @property
    def reasoning_effort(self):
        """思考深度: 'high' / 'max' (DeepSeek V4 API 仅支持这两个值)"""
        with self._instance_lock:
            val = self.data.get("reasoning_effort", "high")
            # 兼容旧配置中的 low/medium，自动修正为 high
            if val not in ("high", "max"):
                val = "high"
            return val

    @reasoning_effort.setter
    def reasoning_effort(self, value):
        with self._instance_lock:
            self.data["reasoning_effort"] = str(value)
        self._save_safe()

    @property
    def nickname(self):
        """用户昵称，欢迎页/对话中个性化显示"""
        with self._instance_lock:
            return self.data.get("nickname", "") or self.data.get("username", "User")

    @nickname.setter
    def nickname(self, value):
        with self._instance_lock:
            self.data["nickname"] = str(value or "用户").strip()
            self.data["username"] = self.data["nickname"]
        self._save_safe()

    # ---------- 可用模型 & 子代理配置 便捷方法 ----------

    def get_available_models(self) -> list:
        """获取所有可用模型列表（线程安全：读取时持锁）。"""
        with self._instance_lock:
            models = self.data.get("available_models", [])
            # 返回浅副本，防止外部修改影响内部状态
            if isinstance(models, list):
                return list(models)
            return []

    def get_primary_model(self) -> str:
        """获取主模型名（primary=True 的那一个）"""
        for m in self.get_available_models():
            if isinstance(m, dict) and m.get("primary"):
                return str(m["name"])
        with self._instance_lock:
            return str(self.data.get("model", "deepseek-v4-pro"))

    def get_subagent_config(self) -> dict:
        """获取子代理配置（线程安全：持锁读取 + 类型回退）。"""
        defaults = {
            "max_parallel": 5,
            "default_model": "deepseek-v4-flash",
            "allow_model_switch": True,
            "max_tokens_per_subagent": 12000,
        }
        with self._instance_lock:
            if not isinstance(self.data, dict):
                return dict(defaults)
            raw = self.data.get("subagent")
        if not isinstance(raw, dict):
            return dict(defaults)
        result = dict(defaults)
        for key, default_val in defaults.items():
            if key in raw:
                val = raw[key]
                if type(val) is type(default_val):
                    result[key] = val
                elif isinstance(default_val, bool) and isinstance(val, (int, float)):
                    result[key] = bool(val)
                elif isinstance(default_val, int) and isinstance(val, str):
                    try:
                        result[key] = int(val)
                    except (ValueError, TypeError) as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                elif isinstance(default_val, str) and not isinstance(val, (dict, list)):
                    try:
                        result[key] = str(val)
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return result

    def get_model_info(self, model_name: str) -> dict:
        """根据模型名获取完整信息（线程安全：基于列表副本工作）"""
        for m in self.get_available_models():
            if isinstance(m, dict) and m.get("name") == model_name:
                return dict(m)
        return {"name": model_name, "label": model_name, "description": "", "primary": False}