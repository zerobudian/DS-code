# -*- coding: utf-8 -*-
"""请求速率限制器 (P2-004 / M-010 修复)

令牌桶算法实现, 防止 API 配额被耗尽和服务被 DDoS。
同时限制每秒最多 N 次、每分钟最多 M 次请求。
"""

import time
import threading
from typing import Dict, Optional


# 默认限制：每秒 10 次、每分钟 200 次（可自行在配置中覆盖）
DEFAULT_PER_SECOND = 10.0
DEFAULT_PER_MINUTE = 200.0
DEFAULT_CAPACITY = 20


class RateLimiter:
    """双桶令牌桶速率限制器。

    每个用户/会话独立维护一个令牌桶:
      - 令牌以固定速率 (rate) 补充
      - 桶有最大容量 (capacity), 防止突发流量
      - 每次请求消耗 1 个令牌

    同时增加每分钟维度的全局/单 key 限制，防止短窗口内累积配额滥用 (M-010)。

    用法:
        limiter = RateLimiter(rate=10, capacity=20, per_minute=200)
        if limiter.is_allowed("user_123"):
            # 处理请求
        else:
            # 返回 429 或提示等待
    """

    def __init__(
        self,
        rate: float = DEFAULT_PER_SECOND,
        capacity: int = DEFAULT_CAPACITY,
        per_minute: float = DEFAULT_PER_MINUTE,
    ):
        """
        Args:
            rate: 每秒补充的令牌数 (默认 10 req/s)
            capacity: 桶最大容量 (默认 20, 允许短时突发)
            per_minute: 每分钟允许的最大请求数 (默认 200 req/min)
        """
        if rate <= 0:
            raise ValueError("rate 必须 > 0")
        if capacity <= 0:
            raise ValueError("capacity 必须 > 0")
        if per_minute <= 0:
            raise ValueError("per_minute 必须 > 0")

        self._per_second_rate = float(rate)
        self._per_second_capacity = int(capacity)

        # 每分钟桶：以每分钟 rate 换算成每秒补充速率，容量即每分钟上限
        self._per_minute_rate = float(per_minute) / 60.0
        self._per_minute_capacity = int(per_minute)

        self._lock = threading.RLock()
        self._buckets: Dict[str, Dict[str, float]] = {}
        self._max_buckets = 10000  # 单个限制器最多保留的 key 数量

        # 全局令牌桶: 防止攻击者通过切换 key 绕过 per-key 限制
        self._global_second_bucket = {
            "tokens": float(rate * 5),
            "last_update": time.monotonic(),
        }
        self._global_second_capacity = int(rate * 5)
        self._global_second_rate = float(rate * 2)

        self._global_minute_bucket = {
            "tokens_minute": float(per_minute * 5),
            "last_update_minute": time.monotonic(),
        }
        self._global_minute_capacity = int(per_minute * 5)
        self._global_minute_rate = float(per_minute * 2) / 60.0

    def _wait_for_token(self, bucket: Dict[str, float], capacity: float, rate: float, token_key: str = "tokens") -> float:
        """返回再获得 1 个令牌需要等待的秒数。"""
        if bucket[token_key] >= 1.0:
            return 0.0
        return (1.0 - bucket[token_key]) / float(rate)

    def _add_tokens(
        self,
        bucket: Dict[str, float],
        capacity: float,
        rate: float,
        now: float,
        token_key: str = "tokens",
        last_update_key: str = "last_update",
    ) -> None:
        """按时间补充令牌到指定键，使用独立的最后更新时间戳，避免多维度互相干扰。"""
        last = bucket.get(last_update_key, now)
        elapsed = now - last
        bucket[token_key] = min(
            float(capacity),
            bucket.get(token_key, float(capacity)) + elapsed * float(rate),
        )
        bucket[last_update_key] = now

    def is_allowed(self, key: str) -> bool:
        """检查指定 key 是否可以发送请求。

        Args:
            key: 用户/会话标识符

        Returns:
            True 如果允许, False 如果被限流
        """
        now = time.monotonic()
        with self._lock:
            # ---- Per-key 令牌桶检查 ----
            bucket = self._buckets.get(key)
            per_key_ok = False
            is_new_key = False
            if bucket is None:
                is_new_key = True
                per_key_ok = True
            else:
                self._add_tokens(bucket, self._per_second_capacity, self._per_second_rate, now)
                self._add_tokens(
                    bucket,
                    self._per_minute_capacity,
                    self._per_minute_rate,
                    now,
                    token_key="tokens_minute",
                    last_update_key="last_update_minute",
                )
                per_key_ok = (
                    bucket.get("tokens", 0.0) >= 1.0
                    and bucket.get("tokens_minute", 0.0) >= 1.0
                )

            # ---- 全局令牌桶检查 (防止切换 key 绕过 per-key 限制) ----
            self._add_tokens(
                self._global_second_bucket,
                self._global_second_capacity,
                self._global_second_rate,
                now,
            )
            self._add_tokens(
                self._global_minute_bucket,
                self._global_minute_capacity,
                self._global_minute_rate,
                now,
                token_key="tokens_minute",
                last_update_key="last_update_minute",
            )

            global_ok = (
                self._global_second_bucket["tokens"] >= 1.0
                and self._global_minute_bucket.get("tokens_minute", 0.0) >= 1.0
            )

            if not per_key_ok or not global_ok:
                return False

            # 两边都通过才扣减，避免 per-key 限流时浪费全局令牌
            self._global_second_bucket["tokens"] -= 1.0
            self._global_minute_bucket["tokens_minute"] -= 1.0

            if is_new_key:
                self._buckets[key] = {
                    "tokens": float(self._per_second_capacity - 1),
                    "tokens_minute": float(self._per_minute_capacity - 1),
                    "last_update": now,
                    "last_update_minute": now,
                }
                self._maybe_cleanup(now)
            else:
                bucket["tokens"] -= 1.0
                bucket["tokens_minute"] -= 1.0

            return True

    def wait_time(self, key: str) -> float:
        """返回指定 key 需要等待的秒数才能再次请求。

        Returns:
            等待秒数, 0 表示现在就可以请求
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            waits = []
            if bucket is not None:
                self._add_tokens(bucket, self._per_second_capacity, self._per_second_rate, now)
                self._add_tokens(
                    bucket,
                    self._per_minute_capacity,
                    self._per_minute_rate,
                    now,
                    token_key="tokens_minute",
                    last_update_key="last_update_minute",
                )
                waits.append(
                    self._wait_for_token(bucket, self._per_second_capacity, self._per_second_rate),
                )
                waits.append(
                    self._wait_for_token(bucket, self._per_minute_capacity, self._per_minute_rate, token_key="tokens_minute"),
                )
            else:
                # 新 key：per-key 桶是满的，无需等待 per-key
                waits.append(0.0)
                waits.append(0.0)

            # 即使是新 key，也要检查全局桶（全局可能已耗尽）
            self._add_tokens(
                self._global_second_bucket,
                self._global_second_capacity,
                self._global_second_rate,
                now,
            )
            self._add_tokens(
                self._global_minute_bucket,
                self._global_minute_capacity,
                self._global_minute_rate,
                now,
                token_key="tokens_minute",
                last_update_key="last_update_minute",
            )
            waits.append(
                self._wait_for_token(
                    self._global_second_bucket,
                    self._global_second_capacity,
                    self._global_second_rate,
                )
            )
            waits.append(
                self._wait_for_token(
                    self._global_minute_bucket,
                    self._global_minute_capacity,
                    self._global_minute_rate,
                    token_key="tokens_minute",
                )
            )
            return max(waits)

    def reset(self, key: Optional[str] = None) -> None:
        """重置指定 key 或所有 key 的令牌桶。

        Args:
            key: 要重置的 key, None 表示重置所有
        """
        with self._lock:
            if key is None:
                self._buckets.clear()
            elif key in self._buckets:
                del self._buckets[key]

    def _maybe_cleanup(self, now: float) -> None:
        """清理长时间未活跃的 bucket，并对 key 总数做 LRU 硬上限。"""
        stale_threshold = now - 3600  # 1 小时未活跃视为 stale
        stale_keys = [
            k for k, v in self._buckets.items()
            if v["last_update"] < stale_threshold
        ]
        for k in stale_keys:
            del self._buckets[k]
        # 硬上限：按 last_update 保留最近活跃的 max_buckets 个
        if len(self._buckets) > self._max_buckets:
            sorted_buckets = sorted(
                self._buckets.items(),
                key=lambda x: x[1]["last_update"],
                reverse=True,
            )
            self._buckets = dict(sorted_buckets[: self._max_buckets])


# 全局默认限制器: 每秒最多 10 个请求, 每分钟最多 200 个请求
_global_rate_limiter = RateLimiter(
    rate=DEFAULT_PER_SECOND, capacity=DEFAULT_CAPACITY, per_minute=DEFAULT_PER_MINUTE
)


def get_global_rate_limiter() -> RateLimiter:
    """获取全局速率限制器实例。"""
    return _global_rate_limiter
