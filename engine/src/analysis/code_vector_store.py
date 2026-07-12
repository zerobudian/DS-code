# -*- coding: utf-8 -*-
"""代码向量检索（Codebase Vector Retrieval）。

优先尝试 sentence-transformers + faiss 的语义向量检索；
当相关依赖不可用时，自动降级为内存关键词/倒排索引，保证代码检索不中断。

支持：
  - 增量索引（按文件 mtime 判断是否更新）
  - 按查询返回最相关的代码片段（文件路径 + 行号范围 + 相似度）
  - 自动跳过二进制文件与超过 1MB 的超大文件
  - 持久化索引到 .dscode/code_vector_index.json
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# 可选依赖：优先向量语义检索
try:
    import numpy as np

    HAS_NUMPY = True
except Exception:  # pragma: no cover - 降级环境
    np = None  # type: ignore
    HAS_NUMPY = False

try:
    from sentence_transformers import SentenceTransformer

    HAS_SENTENCE_TRANSFORMERS = True
except Exception:  # pragma: no cover - 降级环境
    HAS_SENTENCE_TRANSFORMERS = False

try:
    import faiss

    HAS_FAISS = True
except Exception:  # pragma: no cover - 降级环境
    HAS_FAISS = False


# 默认索引的文件扩展名
DEFAULT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".kt", ".scala", ".go", ".rs",
    ".c", ".cpp", ".cc", ".h", ".hpp",
    ".rb", ".php", ".swift", ".m", ".mm",
    ".sql", ".sh", ".bash", ".ps1", ".yaml", ".yml", ".json",
    ".md", ".rst", ".txt",
}

# 默认忽略目录
DEFAULT_IGNORE_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", ".pytest_cache",
    "node_modules", "venv", ".venv", "env", ".env",
    "dist", "build", "target", ".dscode", ".idea", ".vscode",
}

_MAX_FILE_BYTES = 1 * 1024 * 1024  # 单文件最大 1MB
_CHUNK_LINES = 60  # 每个代码块最多 60 行
_CHUNK_OVERLAP = 10  # 相邻块重叠行数
_TOP_K_DEFAULT = 8


# 脱敏：在索引前把 api_key/password/secret/token 等敏感赋值的具体值替换掉
# 同时支持裸键或引号包裹的键（如 api_key = "..." 或 {"token": "..."}）
_SENSITIVE_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])([\"']?)(api[_-]?key|password|passwd|secret|token)\1\s*([=:])\s*(\"[^\"]*\"|'[^']*')"
)


# 不应进入向量索引的敏感文件/目录名（类）
_SENSITIVE_FILE_NAMES = frozenset({"secrets.py", ".env"})


def _sanitize_code_for_index(text: str) -> str:
    """把代码中敏感键的字符串值替换为 <REDACTED>，防止写入向量索引。"""
    return _SENSITIVE_ASSIGN_RE.sub(
        lambda m: f'{m.group(1)}{m.group(2)}{m.group(1)} {m.group(3)} "<REDACTED>"', text
    )


def _tokenize(text: str) -> List[str]:
    """简单分词：拆分驼峰/下划线，保留长度 >=2 的单词。"""
    # 把 camelCase / PascalCase / snake_case 拆成单词
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    result = []
    for t in tokens:
        parts = [p for p in t.split("_") if len(p) >= 2]
        if not parts and len(t) >= 2:
            parts = [t]
        result.extend(p.lower() for p in parts)
    return result


def _build_vocab(doc_tokens: List[List[str]], min_df: Optional[int] = None,
                 max_df_ratio: Optional[float] = None) -> Dict[str, int]:
    """构建词表并返回 term -> index。小项目自动降低 min_df 与 max_df。"""
    n = len(doc_tokens)
    if min_df is None:
        # 小项目放宽要求：文件少时每词至少出现 1 次即可
        min_df = 1 if n <= 5 else 2
    if max_df_ratio is None:
        # 小项目不过滤全局词，否则太少的文档会导致所有关键词被当作停用词
        max_df_ratio = 1.0 if n <= 5 else 0.9
    df: Dict[str, int] = {}
    for tokens in doc_tokens:
        seen = set(tokens)
        for t in seen:
            df[t] = df.get(t, 0) + 1
    vocab = {}
    for term, count in df.items():
        if count < min_df:
            continue
        if n > 0 and count / n > max_df_ratio:
            continue
        vocab[term] = len(vocab)
    return vocab


def _compute_tfidf(doc_tokens: List[List[str]], vocab: Dict[str, int]) -> Tuple[np.ndarray, np.ndarray]:
    """计算 TF-IDF 矩阵 [n_docs, vocab_size]，同时返回 idf 数组。"""
    n_docs = len(doc_tokens)
    vocab_size = len(vocab)
    mat = np.zeros((n_docs, vocab_size), dtype=np.float32)
    idf = np.zeros(vocab_size, dtype=np.float32)
    df = np.zeros(vocab_size, dtype=np.int32)
    for tokens in doc_tokens:
        seen = set(tokens)
        for t in seen:
            idx = vocab.get(t)
            if idx is not None:
                df[idx] += 1
    for i in range(vocab_size):
        idf[i] = math.log((n_docs + 1) / (df[i] + 1)) + 1.0

    for i, tokens in enumerate(doc_tokens):
        if not tokens:
            continue
        tf: Dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        for t, c in tf.items():
            idx = vocab.get(t)
            if idx is not None:
                # sublinear tf
                mat[i, idx] = (1 + math.log(c)) * idf[idx]
    # L2 归一化
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms, idf


def _is_binary(path: Path, sample_size: int = 8192) -> bool:
    """基于内容采样判断文件是否为二进制文件。"""
    try:
        with path.open("rb") as f:
            chunk = f.read(sample_size)
    except Exception:
        return True
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    # 非可打印字符比例过高也视为二进制
    non_text = sum(1 for b in chunk if b < 8 or (14 <= b < 32 and b not in (9, 10, 13)))
    return non_text / len(chunk) > 0.30


def _format_result(doc: Dict, score: float) -> Dict:
    """把内部 doc 格式化为统一的检索结果。"""
    return {
        "path": doc.get("path", ""),
        "rel_path": doc.get("rel_path", ""),
        "start_line": doc.get("start_line", 1),
        "end_line": doc.get("end_line", 1),
        "score": score,
        "snippet": doc.get("text", "")[:800],
    }


# ---------------------------------------------------------------------------
# 索引后端抽象
# ---------------------------------------------------------------------------
class _IndexBackend:
    """代码检索后端基类。"""

    name = "base"

    def __init__(self, docs: Optional[List[Dict]] = None):
        self._docs: List[Dict] = list(docs or [])

    def index(self, docs: List[Dict]) -> Dict:
        self._docs = list(docs)
        return self.get_stats()

    def search(self, query: str, top_k: int = _TOP_K_DEFAULT,
               min_score: float = 0.05) -> List[Dict]:
        raise NotImplementedError

    def get_stats(self) -> Dict:
        return {"chunks": len(self._docs), "backend": self.name}


class _KeywordBackend(_IndexBackend):
    """基于 TF-IDF（有 numpy）或内存倒排索引（无 numpy）的关键词后端。"""

    name = "keyword"

    def __init__(self, docs: Optional[List[Dict]] = None):
        super().__init__(docs)
        self._vocab: Dict[str, int] = {}
        self._vectors: Optional[np.ndarray] = None
        self._idf_array: Optional[np.ndarray] = None
        self._inverted: Dict[str, Set[int]] = {}

    def index(self, docs: List[Dict]) -> Dict:
        super().index(docs)
        if not self._docs:
            self._vocab = {}
            self._vectors = None
            self._idf_array = None
            self._inverted = {}
            return self.get_stats()

        # 若文档已丢弃 tokens，则从 text 重新分词
        doc_tokens = [d.get("tokens") or _tokenize(d.get("text", "")) for d in self._docs]

        if HAS_NUMPY and any(doc_tokens):
            self._vocab = _build_vocab(doc_tokens)
            if self._vocab:
                self._vectors, self._idf_array = _compute_tfidf(doc_tokens, self._vocab)
            else:
                self._vectors = None
                self._idf_array = None
            self._inverted = {}
        else:
            # 纯内存倒排索引（无 numpy 时）
            self._vocab = {}
            self._vectors = None
            self._idf_array = None
            self._inverted = {}
            for i, tokens in enumerate(doc_tokens):
                for t in set(tokens):
                    self._inverted.setdefault(t, set()).add(i)
        return self.get_stats()

    def search(self, query: str, top_k: int = _TOP_K_DEFAULT,
               min_score: float = 0.05) -> List[Dict]:
        if not self._docs:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        # numpy TF-IDF 检索
        if HAS_NUMPY and self._vectors is not None and self._vocab:
            q_vec = np.zeros((1, len(self._vocab)), dtype=np.float32)
            tf: Dict[str, int] = {}
            for t in query_tokens:
                tf[t] = tf.get(t, 0) + 1
            for t, c in tf.items():
                idx = self._vocab.get(t)
                if idx is not None:
                    # 查询向量也使用 TF-IDF 加权（与文档向量一致）
                    if self._idf_array is not None:
                        q_vec[0, idx] = (1 + math.log(c)) * self._idf_array[idx]
                    else:
                        q_vec[0, idx] = 1 + math.log(c)
            norm = np.linalg.norm(q_vec)
            if norm == 0:
                return []
            q_vec = q_vec / norm
            scores = (self._vectors @ q_vec.T).flatten()
            top_indices = np.argsort(scores)[::-1][:top_k]
            results = []
            for idx in top_indices:
                score = float(scores[idx])
                if score < min_score:
                    continue
                results.append(_format_result(self._docs[idx], round(score, 4)))
            return results

        # 纯 dict 倒排检索（无 numpy 时）
        n_docs = len(self._docs)
        scores = [0.0] * n_docs
        for t in query_tokens:
            postings = self._inverted.get(t)
            if not postings:
                continue
            idf = math.log((n_docs + 1) / (len(postings) + 1)) + 1.0
            for idx in postings:
                tokens = self._docs[idx].get("tokens") or _tokenize(self._docs[idx].get("text", ""))
                tf = tokens.count(t)
                if tf > 0:
                    scores[idx] += (1 + math.log(tf)) * idf
        max_score = max(scores) if scores else 0.0
        if max_score > 0:
            scores = [s / max_score for s in scores]
        indexed = sorted(enumerate(scores), key=lambda x: -x[1])[:top_k]
        results = []
        for idx, score in indexed:
            if score < min_score:
                continue
            results.append(_format_result(self._docs[idx], round(score, 4)))
        return results

    def get_stats(self) -> Dict:
        stats = super().get_stats()
        stats["vocab_size"] = len(self._vocab)
        stats["has_vectors"] = self._vectors is not None
        return stats


class _VectorBackend(_IndexBackend):
    """基于 sentence-transformers + faiss 的语义向量后端。"""

    name = "vector"

    def __init__(self, docs: Optional[List[Dict]] = None,
                 model_name: str = "all-MiniLM-L6-v2"):
        super().__init__(docs)
        self.model_name = model_name
        self._model: Optional[SentenceTransformer] = None
        self._index: Optional[faiss.IndexFlatIP] = None
        self._dim = 0

    def _load_model(self) -> SentenceTransformer:
        if self._model is None:
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def index(self, docs: List[Dict]) -> Dict:
        if not HAS_NUMPY:
            raise RuntimeError("numpy 是向量后端的必要依赖")
        super().index(docs)
        if not self._docs:
            self._index = None
            self._dim = 0
            return self.get_stats()

        model = self._load_model()
        texts = [d.get("text", "") for d in self._docs]
        embeddings = model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        embeddings = np.asarray(embeddings, dtype=np.float32)
        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)
        self._dim = embeddings.shape[1]
        self._index = faiss.IndexFlatIP(self._dim)
        self._index.add(embeddings)
        return self.get_stats()

    def search(self, query: str, top_k: int = _TOP_K_DEFAULT,
               min_score: float = 0.05) -> List[Dict]:
        if self._index is None or not self._docs or not HAS_NUMPY:
            return []
        model = self._load_model()
        q = model.encode(
            [query],
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        q = np.asarray(q, dtype=np.float32).reshape(1, -1)
        k = min(top_k, len(self._docs))
        scores_arr, indices_arr = self._index.search(q, k)
        results = []
        for score, idx in zip(scores_arr[0], indices_arr[0]):
            if idx < 0:
                continue
            if float(score) < min_score:
                continue
            results.append(_format_result(self._docs[int(idx)], round(float(score), 4)))
        return results

    def get_stats(self) -> Dict:
        stats = super().get_stats()
        stats["has_vectors"] = self._index is not None
        return stats


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
class CodeVectorStore:
    """代码库向量索引。"""

    def __init__(self, work_dir: Path, index_dir: Optional[Path] = None,
                 use_vector: bool = True):
        self.work_dir = Path(work_dir).resolve()
        if index_dir is None:
            index_dir = self.work_dir / ".dscode" / "vector_index"
        self.index_dir = Path(index_dir)
        self.index_file = self.index_dir / "code_index.json"
        self.lock = threading.RLock()

        self._docs: List[Dict] = []  # 每个 chunk 的元信息
        self._indexed_at = 0.0

        self._backend = self._create_backend(use_vector)
        self._ensure_dirs()
        self._load_index()

    # ------------------------------------------------------------------
    # 目录 / IO
    # ------------------------------------------------------------------
    def _ensure_dirs(self) -> None:
        try:
            self.index_dir.mkdir(parents=True, exist_ok=True)
        except OSError as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _create_backend(self, use_vector: bool) -> _IndexBackend:
        if use_vector and HAS_SENTENCE_TRANSFORMERS and HAS_FAISS and HAS_NUMPY:
            try:
                return _VectorBackend()
            except Exception as exc:
                logger.debug("向量后端初始化失败，降级到关键词后端: %s", exc, exc_info=True)
        return _KeywordBackend()

    def _load_index(self) -> None:
        """从磁盘加载索引（若存在）。"""
        if not self.index_file.exists():
            return
        try:
            with self.index_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self._docs = data.get("docs", [])
            self._indexed_at = data.get("indexed_at", 0.0)
            saved_backend = data.get("backend")
            # 若保存时使用向量后端但当前不可用，则降级
            self._backend = self._create_backend(use_vector=(saved_backend == "vector"))
            if self._docs:
                try:
                    self._backend.index(self._docs)
                except Exception as exc:
                    logger.debug("加载索引后重建后端失败，降级: %s", exc, exc_info=True)
                    self._backend = _KeywordBackend()
                    self._backend.index(self._docs)
        except Exception as exc:
            print(f"[CodeVectorStore] 加载索引失败: {exc}")
            self._docs = []
            self._indexed_at = 0.0
            self._backend = self._create_backend(use_vector=False)

    def _save_index(self) -> bool:
        """保存索引到磁盘。"""
        try:
            self._ensure_dirs()
            data = {
                "docs": self._docs,
                "indexed_at": self._indexed_at,
                "backend": self._backend.name,
            }
            tmp = self.index_file.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.index_file)
            return True
        except Exception as exc:
            print(f"[CodeVectorStore] 保存索引失败: {exc}")
            return False

    # ------------------------------------------------------------------
    # 索引
    # ------------------------------------------------------------------
    def _list_source_files(self) -> List[Path]:
        """列出需要索引的源代码文件。"""
        files = []
        try:
            for p in self.work_dir.rglob("*"):
                # 不跟随符号链接，避免把项目外的文件纳入索引
                if p.is_symlink():
                    continue
                if not p.is_file():
                    continue
                if p.suffix.lower() not in DEFAULT_EXTENSIONS:
                    continue
                # 忽略隐藏/黑名单目录
                rel = p.relative_to(self.work_dir)
                parts = set(rel.parts)
                if parts & DEFAULT_IGNORE_DIRS:
                    continue
                # 跳过 .env、secrets.py 等敏感文件
                if p.name in _SENSITIVE_FILE_NAMES or any(part.startswith(".env") for part in rel.parts):
                    continue
                # 跳过二进制与超大文件
                try:
                    if _is_binary(p):
                        continue
                    if p.stat().st_size > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                files.append(p)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return files

    @staticmethod
    def _chunk_file(path: Path) -> List[Dict]:
        """把文件切分为重叠的代码块。"""
        try:
            size = path.stat().st_size
            if size > _MAX_FILE_BYTES:
                return []
            if _is_binary(path):
                return []
            text = path.read_text(encoding="utf-8", errors="ignore")
            # 索引前脱敏：抹掉 api_key/password/secret/token 等敏感赋值
            text = _sanitize_code_for_index(text)
        except Exception:
            return []
        lines = text.splitlines()
        if not lines:
            return []
        chunks = []
        start = 0
        while start < len(lines):
            end = min(start + _CHUNK_LINES, len(lines))
            chunk_text = "\n".join(lines[start:end])
            tokens = _tokenize(chunk_text)
            if tokens:
                chunks.append({
                    "path": str(path.relative_to(path.parent)),
                    "rel_path": str(path.name),
                    "abs_path": str(path.resolve()),
                    "start_line": start + 1,
                    "end_line": end,
                    "text": chunk_text,
                    "tokens": tokens,
                })
            if end >= len(lines):
                break
            start = max(end - _CHUNK_OVERLAP, start + 1)
        return chunks

    def index(self, force: bool = False) -> Dict:
        """重建索引。返回统计信息。"""
        with self.lock:
            files = self._list_source_files()
            # 检查是否有文件变更
            if not force and self._indexed_at and self._docs:
                max_mtime = 0.0
                for f in files:
                    try:
                        mt = f.stat().st_mtime
                        if mt > max_mtime:
                            max_mtime = mt
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                if max_mtime <= self._indexed_at + 1:
                    return {"status": "up_to_date", "files": len(files), "chunks": len(self._docs),
                            "backend": self._backend.name}

            docs: List[Dict] = []
            for f in files:
                try:
                    chunks = self._chunk_file(f)
                    for ch in chunks:
                        ch["path"] = str(f.relative_to(self.work_dir))
                        ch["rel_path"] = str(f.name)
                        ch["abs_path"] = str(f.resolve())
                    docs.extend(chunks)
                except Exception as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True)
                    continue

            if not docs:
                self._docs = []
                self._backend.index([])
                self._indexed_at = time.time()
                self._save_index()
                return {"status": "empty", "files": len(files), "chunks": 0,
                        "backend": self._backend.name}

            try:
                self._backend.index(docs)
            except Exception as exc:
                # 向量后端失败时降级到关键词后端
                if self._backend.name == "vector":
                    print(f"[CodeVectorStore] 向量索引失败，降级关键词索引: {exc}")
                    self._backend = _KeywordBackend()
                    self._backend.index(docs)
                else:
                    raise

            # 丢弃 tokens 节省内存（关键词后端已在 index 中完成计算）
            for d in docs:
                d.pop("tokens", None)
            self._docs = docs
            self._indexed_at = time.time()
            self._save_index()
            stats = self._backend.get_stats()
            return {
                "status": "indexed",
                "files": len(files),
                "chunks": len(docs),
                "backend": self._backend.name,
                **stats,
            }

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = _TOP_K_DEFAULT,
               min_score: float = 0.05) -> List[Dict]:
        """检索与查询最相关的代码块。"""
        with self.lock:
            return self._backend.search(query, top_k=top_k, min_score=min_score)

    def search_similar(self, query: str, top_k: int = 5,
                       min_score: float = 0.05) -> List[Dict]:
        """语义相似检索：优先使用向量，否则自动回退到关键词匹配。"""
        return self.search(query, top_k=top_k, min_score=min_score)

    def get_stats(self) -> Dict:
        with self.lock:
            stats = self._backend.get_stats()
            stats["indexed_at"] = self._indexed_at
            stats["status"] = self.status
            return stats

    @property
    def status(self) -> str:
        """返回索引状态：ready / degraded / unavailable。"""
        if self._backend.name == "vector":
            return "ready"
        if self._docs:
            return "degraded"
        return "unavailable"
