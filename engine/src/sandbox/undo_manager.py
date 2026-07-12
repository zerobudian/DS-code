# -*- coding: utf-8 -*-
"""快照 / 回滚管理器 (Snapshot & Undo Manager)。

用途: 允许对项目目录内容进行"保存快照 / 还原 / 删除"，用于在
不可预测的工具操作前/后进行兜底。

安全重点:
1. 所有快照目录与操作路径都限制在 `sandbox.work_dir / .dscode / snapshots / <id>` 内；
2. id 字符白名单（字母/数字/下划线/连字符），拒绝路径穿越字符；
3. 还原时先将目标目录清空或移动到 `undo__<id>` 备份目录，避免删除项目外文件；
4. 绝不调用外部 shell 命令。
"""

import logging

logger = logging.getLogger(__name__)

import shutil
import time
import json
import os
import threading
import secrets
from pathlib import Path
from typing import List, Optional


_MAX_NAME_BYTES = 128            # id 最大长度
_SNAPSHOT_ROOT_SUBDIR = ".dscode"
_SNAPSHOT_DIR = "snapshots"
_INDEX_FILE = "_index.json"
_MAX_SNAPSHOTS = 50              # 最大快照数量，超过时自动删除最旧的


def _safe_relative_to(p: Path, base: Path) -> bool:
    """返回 True 当且仅当 p 在 base 之下。"""
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except (ValueError, Exception):
        return False


class UndoManager:
    """快照管理。"""

    def __init__(self, sandbox):
        self._sandbox = sandbox
        self._lock = threading.RLock()  # 保护快照索引 & 目录并发修改
        self.snapshots_dir = self._init_root()

    # ---------- 基础 ----------
    def _init_root(self) -> Optional[Path]:
        try:
            work = getattr(self._sandbox, "work_dir", None) or "."
            root = (Path(work) / _SNAPSHOT_ROOT_SUBDIR / _SNAPSHOT_DIR).resolve()
            root.mkdir(parents=True, exist_ok=True)
            return root
        except Exception:
            return None

    def _index_path(self) -> Optional[Path]:
        if self.snapshots_dir is None:
            return None
        return self.snapshots_dir / _INDEX_FILE

    @staticmethod
    def _clean_id(raw: str) -> str:
        if not isinstance(raw, str):
            return ""
        sid = raw.strip()
        if not sid or len(sid) > _MAX_NAME_BYTES:
            return ""
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if any(ch not in allowed for ch in sid):
            return ""
        return sid

    def _safe_remove_dir(self, path: Path) -> bool:
        try:
            if not path.is_dir():
                return False
            # 先移动到临时路径，再删除，避免中途崩溃
            # 使用线程ID + 随机token防止高并发下毫秒级时间戳碰撞
            ts = int(time.time() * 1000)
            tmp = path.parent / f"__dscode_remove_{ts}_{threading.get_ident():x}_{secrets.token_hex(4)}"
            try:
                shutil.move(str(path), str(tmp))
            except Exception:
                tmp = path
            shutil.rmtree(str(tmp), ignore_errors=True)
            return not Path(tmp).exists()
        except Exception:
            return False

    # ---------- 索引 ----------
    def _load_index(self) -> List[dict]:
        """读取快照索引（线程安全：内部持 RLock）。"""
        with self._lock:
            idx = self._index_path()
            if idx is None or not idx.is_file():
                return []
            try:
                with open(str(idx), "r", encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return [e for e in data if isinstance(e, dict)]
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return []

    def _save_index(self, entries: List[dict]) -> bool:
        """写入快照索引（线程安全：内部持 RLock）。"""
        with self._lock:
            idx = self._index_path()
            if idx is None:
                return False
            try:
                tmp = idx.with_name(idx.name + ".tmp")
                with open(str(tmp), "w", encoding="utf-8") as f:
                    json.dump(entries, f, ensure_ascii=False, indent=2)
                os.replace(str(tmp), str(idx))
                return True
            except Exception:
                return False

    def _add_index_entry(self, snapshot_id: str, title: str) -> None:
        entries = self._load_index()
        # 去重
        entries = [e for e in entries if e.get("id") != snapshot_id]
        entries.append({
            "id": snapshot_id,
            "title": (title or snapshot_id)[:256],
            "ts": int(time.time()),
        })
        # 快照数量上限：超过时删除最旧的快照（按时间戳排序）
        if len(entries) > _MAX_SNAPSHOTS:
            # 按时间戳升序排序，删除最旧的
            entries.sort(key=lambda e: e.get("ts", 0))
            while len(entries) > _MAX_SNAPSHOTS:
                oldest = entries.pop(0)
                old_id = oldest.get("id", "")
                if old_id:
                    old_dir = self.snapshots_dir / old_id
                    try:
                        if _safe_relative_to(old_dir, self.snapshots_dir):
                            self._safe_remove_dir(old_dir)
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        self._save_index(entries)

    # ---------- 公共接口 ----------
    def create_snapshot(self, title: str = "") -> Optional[str]:
        """创建一个新快照（线程安全），返回 snapshot_id。"""
        with self._lock:
            if self.snapshots_dir is None:
                return None
            try:
                work = getattr(self._sandbox, "work_dir", None)
            except Exception:
                work = None
            if not work:
                return None
            try:
                work_path = Path(work).resolve()
            except Exception:
                return None

            sid = f"auto_{int(time.time() * 1000)}_{os.getpid()}"
            sid = self._clean_id(sid)
            if not sid:
                return None

            target = self.snapshots_dir / sid
            # 确保没有同名残留
            if target.exists():
                for i in range(1, 10):
                    cand = self.snapshots_dir / f"{sid}_{i}"
                    if not cand.exists():
                        target = cand
                        sid = cand.name
                        break
                else:
                    return None

            try:
                # symlinks=True 保留符号链接本身（不跟随），防止复制沙箱外部的
                # 敏感文件（如 ~/.ssh/id_rsa）或因跟随大目录（如 /）造成 DoS。
                # ignore_dangling_symlinks=True 跳过已失效的符号链接。
                # 排除快照根目录本身，防止快照递归爆炸。
                shutil.copytree(
                    str(work_path), str(target),
                    symlinks=True, ignore_dangling_symlinks=True,
                    ignore=shutil.ignore_patterns(_SNAPSHOT_ROOT_SUBDIR),
                )
            except Exception:
                self._safe_remove_dir(target)
                return None

            self._add_index_entry(sid, title or sid)
            return sid

    def list_snapshots(self) -> List[dict]:
        """返回快照列表 (id, title, ts)（线程安全）。"""
        with self._lock:
            entries = self._load_index()
        if not entries:
            return []
        return [
            {"id": e.get("id", ""), "title": e.get("title", e.get("id", "")), "ts": e.get("ts", 0)}
            for e in entries
        ]

    def restore_snapshot(self, snapshot_id: str) -> bool:
        """还原一个快照到工作目录（线程安全）。

        整个还原操作在锁内执行，防止并发 create/delete/restore 竞态。
        恢复失败时保留备份目录，避免数据永久丢失。
        """
        with self._lock:
            sid = self._clean_id(snapshot_id or "")
            if not sid or self.snapshots_dir is None:
                return False
            src = self.snapshots_dir / sid
            if not _safe_relative_to(src, self.snapshots_dir) or not src.is_dir():
                return False
            try:
                work = getattr(self._sandbox, "work_dir", None) or "."
                work_path = Path(work).resolve()
            except Exception:
                return False
            if not work_path.is_dir():
                return False

            # 先把当前工作目录拷贝到备份目录
            # 使用线程ID + 随机token防止高并发下毫秒级时间戳碰撞
            ts = int(time.time() * 1000)
            backup = self.snapshots_dir / f"undo_backup_{ts}_{threading.get_ident():x}_{secrets.token_hex(4)}"
            try:
                if backup.exists():
                    shutil.rmtree(str(backup), ignore_errors=True)
                backup.mkdir(parents=True, exist_ok=True)
                _protected = {self.snapshots_dir.resolve(), self.snapshots_dir.parent.resolve()}
                for entry in work_path.iterdir():
                    try:
                        if entry.resolve() in _protected:
                            continue
                        if entry.is_dir():
                            shutil.copytree(str(entry), str(backup / entry.name), symlinks=True)
                        else:
                            shutil.copy2(str(entry), str(backup))
                    except Exception as _exc:
                        logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
            except Exception:
                return False

            # 清空 work_path 下现有内容
            protected_dirs = {self.snapshots_dir.resolve(), self.snapshots_dir.parent.resolve()}
            try:
                for entry in list(work_path.iterdir()):
                    try:
                        entry_resolved = entry.resolve()
                        if entry_resolved in protected_dirs:
                            continue
                        if entry.is_dir():
                            self._safe_remove_dir(entry)
                        else:
                            entry.unlink(missing_ok=True)
                    except Exception as _exc:
                        logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
            except Exception:
                return False

            # 从快照拷贝到 work_path
            try:
                for entry in src.iterdir():
                    dst = work_path / entry.name
                    if entry.is_dir():
                        shutil.copytree(str(entry), str(dst), symlinks=True)
                    else:
                        shutil.copy2(str(entry), str(dst))
                # 还原成功后清理备份目录
                self._safe_remove_dir(backup)
                return True
            except Exception:
                # 还原失败：尝试把 backup 内容恢复回 work 目录
                restore_ok = False
                try:
                    for entry in list(work_path.iterdir()):
                        entry_resolved = entry.resolve()
                        if entry_resolved in protected_dirs:
                            continue
                        if entry.is_dir():
                            self._safe_remove_dir(entry)
                        else:
                            entry.unlink(missing_ok=True)
                    for entry in backup.iterdir():
                        dst = work_path / entry.name
                        if entry.is_dir():
                            shutil.copytree(str(entry), str(dst), symlinks=True)
                        else:
                            shutil.copy2(str(entry), str(dst))
                    restore_ok = True
                except Exception as _restore_exc:
                    logger.error("备份恢复失败，保留备份目录: %s — %s", backup, _restore_exc, exc_info=True)
                # 恢复成功才删除备份；失败则保留备份供人工恢复
                if restore_ok:
                    self._safe_remove_dir(backup)
                else:
                    logger.error("数据恢复失败！备份保留于: %s", backup)
                return False

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """删除某个快照（线程安全），并做路径安全性校验。"""
        with self._lock:
            sid = self._clean_id(snapshot_id or "")
            if not sid or self.snapshots_dir is None:
                return False

            entries = self._load_index()
            new_entries = []
            removed = False
            for e in entries:
                if e.get("id") == sid:
                    sdir = self.snapshots_dir / sid
                    try:
                        if _safe_relative_to(sdir, self.snapshots_dir):
                            self._safe_remove_dir(sdir)
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                    removed = True
                    continue
                new_entries.append(e)
            if removed:
                self._save_index(new_entries)
        return removed
