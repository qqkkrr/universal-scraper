#!/usr/bin/env python3
"""分布式队列/去重后端（R101 新能力，对标 Scrapy-Redis）：
- RedisQueueBackend：redis lib 懒加载；list 做 FIFO 队列、zset 做投递暂存
  （R116 起为纯 zset，at-least-once）、set 做去重
- 不可用（redis 未装/连不上）时 get_backend 返回 None，调用方回落本地队列
  （OCR R131（M）：docstring 曾描述已废弃的 QueueBackend 协议字段名）

设计口径：Redis 侧只做"跨进程共享的 URL 待抓 + 已见"两件事，
引擎本地的统计/限速/代理仍在单进程内（多机共享Redis即得分布式去重）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class RedisQueueBackend:
    """基于 Redis 的分布式队列 + 去重集合。

    - push: LPUSH 待抓请求（JSON 串）
    - pop: RPOP（FIFO）
    - is_seen/mark: SADD/SISMEMBER（键 = inc_key 域 + 记录键）
    """

    def __init__(self, redis_url: str = "redis://127.0.0.1:6379/0",
                 namespace: str = "us", socket_timeout: float = 5.0,
                 visibility_timeout: float = 300.0):
        try:
            import redis  # 懒加载：未装 redis 库时给出清晰 ImportError
        except ImportError as e:
            raise ImportError("需要 redis 库：pip install redis") from e
        self._r = redis.Redis.from_url(redis_url, socket_timeout=socket_timeout,
                                       decode_responses=True)
        self._r.ping()  # 连不上立刻暴露，由调用方回落本地队列
        self._ns = namespace.strip(":") or "us"
        self._qkey = f"{self._ns}:queue"
        self._pkey = f"{self._ns}:processing"   # R116：at-least-once 投递暂存区（zset）
        self._seen_prefix = f"{self._ns}:seen:"
        self.visibility_timeout = float(visibility_timeout)

    # ---- 队列 ----
    def push(self, payload: Dict[str, Any]) -> None:
        import json
        self._r.lpush(self._qkey, json.dumps(payload, ensure_ascii=False))

    def pop(self, timeout: float = 0.0) -> Optional[Dict[str, Any]]:
        """弹出队首 URL 并登记进 processing 暂存区（R116 at-least-once）：
        worker 崩溃后 URL 不丢——reclaim_stale 在 visibility_timeout 后重投。
        返回的 payload 带 dist_member（含唯一投递 token），ack 按它精确移除。

        R116 修复（P1）：processing 必须是纯 ZSET（member=原文|token，score=投递
        时间）——首版曾对同一键混用 RPOPLPUSH(list) 与 ZADD(zset)，真 Redis 必抛
        WRONGTYPE 且条目永久卡死。type-enforcing FakeRedis 单测已覆盖。
        阻塞等待（timeout≥1）不再用 brpoplpush（需要 list 目的地）——统一
        非阻塞 rpop，等待节奏由调用方的轮询循环控制。

        R129 修复（P1）：先 claim（zadd processing）再从队列移除——旧序
        rpop→zadd 两步非原子，进程在两步之间崩溃 = URL 既不在队列也不在
        processing，reclaim_stale 永远看不到它（违反 at-least-once）。新序下
        任何崩溃点条目要么仍在队列、要么必在 processing（超时必重投），
        最坏情况是重复投递（at-least-once 允许），绝不丢失。"""
        import json, time, uuid
        # OCR R131（M）：reclaim 曾每次 pop 都全量扫 processing——高频 pop 下
        # zrangebyscore 是稳定开销。限频 5s 一次（崩溃恢复粒度足够）
        _now = time.time()
        if _now - getattr(self, "_last_reclaim_at", 0.0) >= 5.0:
            self._last_reclaim_at = _now
            self.reclaim_stale()
        # 并发争抢同一队尾时短暂自旋：抢不到（lrem 返回 0）就撤销自己的
        # claim 换下一条；claim 期间崩溃也无害——该条仍在队列里
        for _ in range(16):
            items = self._r.lrange(self._qkey, -1, -1)
            if not items:
                return None
            item = items[0]
            try:
                payload = json.loads(item)
            except Exception:
                # 无法解析的垃圾条目：直接消费掉不入 processing（外层 seen 去重
                # 防重复来源），消费失败也只重试有界次数
                self._r.lrem(self._qkey, 1, item)
                continue
            # OCR R131（H）：8 hex=32bit 生日碰撞 ~93K 在途即 1%——两投递同 member
            # 时 ack 会互删对方拷贝。扩到 16 hex=64bit（~19Q 槽位，万级并发无忧）
            member = f"{item}|{uuid.uuid4().hex[:16]}"  # 唯一投递 token：同一 URL 的
            # 多次投递互不干扰（ack 只移除自己的拷贝，不误伤重投副本）
            self._r.zadd(self._pkey, {member: time.time()})
            if self._r.lrem(self._qkey, 1, item):
                payload["dist_member"] = member
                return payload
            self._r.zrem(self._pkey, member)  # 没抢到：撤销本次 claim，换下一条
        if timeout and timeout > 0:
            # OCR R131：timeout 形参曾被无视——调用方传 0.2s 期待节流等待，
            # 实际立即返回 None 退化成忙轮询。队列为空时按 timeout 节流
            import time as _t
            _t.sleep(min(float(timeout), 1.0))
        return None

    def ack(self, member: str) -> None:
        """worker 成功处理后确认：从 processing 移除（URL 完成交付，不重投）；
        若 reclaim 已把该条重投回队列且尚未被领取，连带移除那份排队拷贝。"""
        if not member:
            return
        self._r.zrem(self._pkey, member)
        self._r.lrem(self._qkey, 1, member.rsplit("|", 1)[0])

    def reclaim_stale(self) -> int:
        """把 processing 中超过 visibility_timeout 的条目重投回队列
        （worker 崩溃/断网的兜底）。返回重投条数。

        R116 修复：member = 原文 JSON + "|" + uuid token——重投必须剥掉 token
        还原纯 JSON（否则下次 pop json.loads 失败返回 None，URL 永远取不回）。"""
        import time
        cutoff = time.time() - self.visibility_timeout
        stale = self._r.zrangebyscore(self._pkey, "-inf", cutoff)
        n = 0
        for member in stale or []:
            raw = member.rsplit("|", 1)[0]  # uuid hex 不含 "|"，末段必为 token
            # R128 修复（OCR CRITICAL）：zrem 返回 1 = 本 worker 赢得所有权，
            # 才允许重投（zrangebyscore 非锁定读，多 worker 并发 reclaim 曾
            # 对同一 stale 条目双重 lpush → 队列重复条目）
            if self._r.zrem(self._pkey, member):
                try:
                    self._r.lpush(self._qkey, raw)
                except Exception as e:
                    # 审查八轮（MEDIUM，at-least-once 保底）：zrem 已成功而 lpush
                    # 抛错（连接抖动/Redis 重启）时，该 URL 会既不在队列也不在
                    # 处理集合——**永久丢失**。回滚：把条目按原 member 放回处理
                    # 集合，等下一轮 reclaim 再重投。
                    # 残余窗口：两条命令之间进程被杀（需 EVAL/MULTI 才能原子化，
                    # 本机无 Redis 环境无法验证，故不引入未验证的原子化改造）。
                    try:
                        self._r.zadd(self._pkey, {member: time.time()})
                    except Exception:
                        pass
                    import sys as _sys
                    print(f"⚠️ reclaim 重投失败已回滚（{type(e).__name__}: {e}）", file=_sys.stderr)
                    continue
                n += 1
        return n

    def pending_size(self) -> int:
        return int(self._r.zcard(self._pkey) or 0)

    def qsize(self) -> int:
        return int(self._r.llen(self._qkey) or 0)

    # ---- 去重集合 ----
    def _skey(self, domain_key: str) -> str:
        return f"{self._seen_prefix}{domain_key or 'default'}"

    def is_seen(self, domain_key: str, key: str) -> bool:
        if not key:
            return False
        return bool(self._r.sismember(self._skey(domain_key), key))

    def mark(self, domain_key: str, key: str) -> None:
        if key:
            self._r.sadd(self._skey(domain_key), key)

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:
            pass


def get_backend(queue_cfg: Dict[str, Any], default_ns: str = "us") -> Optional[RedisQueueBackend]:
    """按 queue 配置返回分布式后端。
    返回 None = 未启用 redis（正常分支，调用方回落本地队列）；
    抛异常 = 用户明确配了 redis 但连不上（显式失败优于静默回落——静默回落曾
    造成"分布式变单机"假象，多机重复抓同一批 URL）。
    配置: {"backend": "redis", "redis_url": "...", "namespace": "同任务多机共用时显式指定",
           "visibility_timeout": 300}。
    namespace 缺省由调用方按任务名派生（R105 修复：曾恒为 "us"，跨任务互偷 URL）。
    R116：投递语义 at-least-once——pop 后 URL 进 processing 暂存，ack 或超时重投。"""
    if not isinstance(queue_cfg, dict) or queue_cfg.get("backend") != "redis":
        return None
    url = queue_cfg.get("redis_url") or "redis://127.0.0.1:6379/0"
    ns = queue_cfg.get("namespace") or default_ns
    vt = float(queue_cfg.get("visibility_timeout", 300) or 300)
    try:
        return RedisQueueBackend(redis_url=url, namespace=str(ns),
                                 visibility_timeout=vt)
    except ImportError as e:
        raise ImportError(f"queue.backend=redis 需要 redis 库：{e}") from e
    except Exception as e:
        # 连不上：显式失败优于静默回落（静默回落曾造成"分布式变单机"假象，
        # 多机重复抓同一批 URL）
        raise RuntimeError(f"Redis 队列连接失败（{url}）: {e}") from e
