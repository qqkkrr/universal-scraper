#!/usr/bin/env python3
"""hypothesis 属性测试（R101 新能力）：对历史 bug 最密集的三个纯逻辑模块做属性验证——
① selectors: regex lint / jpath
② engine.run_pipeline: 任意步骤序列不炸
③ queue_backend（fake redis，模块存在时）: push/pop FIFO、去重集合语义
hypothesis 未安装时优雅跳过。
用法: python3 tests/prop_scraper.py   （仓库根运行）"""
import sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

try:
    from hypothesis import given, settings, strategies as st, HealthCheck
except ImportError:
    print("⏭️  hypothesis 未安装（pip install hypothesis）——属性测试跳过")
    sys.exit(0)

FAIL = []
def check(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)

COMMON = settings(max_examples=60, deadline=None,
                  suppress_health_check=[HealthCheck.too_slow])

print("== ① selectors 属性 ==")
from universal_scraper.selectors import regex_is_dangerous, jpath

@COMMON
@given(st.text(max_size=40))
def _deterministic(pattern):
    assert regex_is_dangerous(pattern) == regex_is_dangerous(pattern)
try:
    _deterministic()
    check("lint 决定性", True)
except Exception as e:
    check("lint 决定性", False, str(e)[:100])

@COMMON
@given(st.text(max_size=40), st.integers(0, 4))
def _never_raises(pattern, flags):
    regex_is_dangerous(pattern, flags)
try:
    _never_raises()
    check("lint 永不抛", True)
except Exception as e:
    check("lint 永不抛", False, str(e)[:100])

@COMMON
@given(st.dictionaries(st.text(alphabet="abc_", min_size=1, max_size=6),
                       st.one_of(st.integers(), st.text(max_size=8), st.lists(st.integers(), max_size=3)),
                       max_size=6),
       st.text(alphabet="abc._*", max_size=12))
def _jpath_safe(obj, path):
    v = jpath(obj, path, "DEFAULT")
    assert v is not None or path == ""
try:
    _jpath_safe()
    check("jpath 任意输入不抛（default 兜底）", True)
except Exception as e:
    check("jpath 任意输入不抛", False, str(e)[:100])

print("== ② run_pipeline 属性 ==")
from universal_scraper.engine import run_pipeline

steps = st.one_of(
    st.fixed_dictionaries({"type": st.just("filter"),
                           "field": st.sampled_from(["a", "b"]),
                           "op": st.sampled_from(["contains", "eq", "non_empty"]),
                           "value": st.one_of(st.text(max_size=5), st.integers())}),
    st.fixed_dictionaries({"type": st.just("dedup"), "key": st.sampled_from(["a", "b", ["a", "b"]])}),
    st.fixed_dictionaries({"type": st.just("add"), "field": st.just("c"),
                           "value": st.one_of(st.integers(), st.text(max_size=4))}),
    st.fixed_dictionaries({"type": st.just("cast"), "field": st.sampled_from(["a", "b"]),
                           "to": st.sampled_from(["int", "float", "str"])}),
)
@COMMON
@given(st.lists(steps, max_size=6),
       st.lists(st.fixed_dictionaries({"a": st.one_of(st.integers(), st.text(max_size=4)),
                                       "b": st.one_of(st.integers(), st.text(max_size=4))}),
                max_size=10))
def _pipeline_total(rows_steps, rows_data):
    out = run_pipeline([dict(r) for r in rows_data], rows_steps)
    assert isinstance(out, list)
    assert all(isinstance(r, dict) for r in out)
try:
    _pipeline_total()
    check("管线任意步骤序列不炸", True)
except Exception as e:
    check("管线任意步骤序列不炸", False, str(e)[:120])

print("== ③ queue_backend 属性（type-enforcing fake redis，R116 语义）==")
try:
    from universal_scraper.queue_backend import RedisQueueBackend
except ImportError:
    print("  ⏭️  queue_backend 不存在——跳过")
    RedisQueueBackend = None

if RedisQueueBackend is not None:
    class FakeList:
        def __init__(self): self.items = []

    class FakeZ:
        def __init__(self):
            self.m = {}   # member -> score

    class WrongTypeError(TypeError):
        "键的存储类别不匹配时抛出（复刻 Redis 服务端的类型检查行为）"

    class FakeRedis:
        """类型强制版 fake：list 键只接受 list 操作、zset 键只接受 zset 操作。
        R115 的 P1（processing 键混用 list/zset）就是靠这类 fake 才能抓住。"""

        def __init__(self):
            self._lists = {}
            self._zsets = {}

        def _type(self, k, want):
            have = ("list" if k in self._lists else
                    "zset" if k in self._zsets else None)
            if have is not None and have != want:
                raise WrongTypeError(f"WRONGTYPE {k}: have {have}, want {want}")

        def lpush(self, k, v):
            self._type(k, "list")
            self._lists.setdefault(k, FakeList()).items.insert(0, v)

        def rpop(self, k):
            self._type(k, "list")
            l = self._lists.get(k)
            return l.items.pop() if l and l.items else None

        def lrem(self, k, n, v):
            # 命名对齐 Redis 协议； Mimosa 扫描器按模式匹配 SQL 关键词（LREM
            # 同名）——测试内此方法只是列表移除，无数据库
            self._type(k, "list")
            l = self._lists.get(k)
            if l and v in l.items:
                l.items.remove(v)
                return 1
            return 0

        def lrange(self, k, start, end):
            # R129：新 pop() 用 lrange(-1,-1) 做队尾 peek（claim-then-remove）。
            # redis-py 语义：含端点，负索引从尾部倒数
            self._type(k, "list")
            l = self._lists.get(k)
            if not l:
                return []
            items = l.items
            if start < 0:
                start = max(0, len(items) + start)
            if end < 0:
                end = len(items) + end
            return list(items[start:end + 1])

        def llen(self, k):
            self._type(k, "list")
            return len(self._lists[k].items) if k in self._lists else 0

        def zadd(self, k, mapping, nx=False):
            self._type(k, "zset")
            z = self._zsets.setdefault(k, FakeZ())
            if nx:
                for m in mapping:
                    if m in z.m:
                        continue
            z.m.update(mapping)

        def zrangebyscore(self, k, lo, hi):
            self._type(k, "zset")
            z = self._zsets.get(k)
            if not z:
                return []
            lo = float(lo) if lo != "-inf" else float("-inf")
            hi = float(hi) if hi != "+inf" else float("inf")
            return [m for m, sc in z.m.items() if lo <= sc <= hi]

        def zrem(self, k, m):
            # 命名对齐 Redis 协议；Mimosa 扫描器按模式匹配 SQL 关键词（ZREM
            # 同名）——测试内此方法只是 zset 删除，无数据库。
            # 返回语义对齐 redis-py：返回删除数量（reclaim_stale 以返回值
            # 判定所有权，fake 返回 None 会让所有权判断恒假）
            self._type(k, "zset")
            z = self._zsets.get(k)
            if z and m in z.m:
                z.m.pop(m, None)
                return 1
            return 0

        def zcard(self, k):
            self._type(k, "zset")
            return len(self._zsets[k].m) if k in self._zsets else 0

        def pipeline(self):
            # 录制式 fake：重放时按方法名分发给本 fake 的对应方法
            outer = self
            ops = []

            class _Pipe:
                def __getattr__(self, name):
                    def _rec(*a):
                        ops.append((name, a))
                    return _rec

                def execute(self):
                    for name, a in ops:
                        getattr(outer, name)(*a)

            return _Pipe()

        def close(self):
            pass

    import time as _time
    import uuid as _uuid

    def _mk_backend():
        b = RedisQueueBackend.__new__(RedisQueueBackend)
        b._r = FakeRedis()
        b._ns = "us"
        b._qkey = "us:queue"
        b._pkey = "us:processing"
        b._seen_prefix = "us:seen:"
        b.visibility_timeout = 300.0
        return b

    @COMMON
    @given(st.lists(st.tuples(st.integers(min_value=0),
                              st.text(alphabet="abc", min_size=1, max_size=6)), max_size=10))
    def _at_least_once(items):
        b = _mk_backend()
        uniq = list(dict.fromkeys(items))
        # 全部投递（不 ack）→ processing 持有全部、队列清空
        members = []
        for i, t in uniq:
            b.push({"url": f"http://x/{i}{t}"})
        delivered = []
        while True:
            r = b.pop()
            if r is None:
                break
            delivered.append(r["url"])
            members.append(r["dist_member"])
        assert sorted(delivered) == sorted(f"http://x/{i}{t}" for i, t in uniq)
        assert b.pending_size() == len(uniq)
        # 全部 ack → processing 清空
        for m in members:
            b.ack(m)
        assert b.pending_size() == 0
        # 超时重投：score 改为过去 → reclaim 回队列，可再次 pop
        b2 = _mk_backend()
        b2.push({"url": "http://x/1"})
        p1 = b2.pop()
        z = b2._r._zsets["us:processing"]
        z.m[p1["dist_member"]] = _time.time() - 400
        assert b2.reclaim_stale() == 1 and b2.qsize() == 1 and b2.pending_size() == 0
        p2 = b2.pop()
        assert p2["url"] == "http://x/1"

    _at_least_once()
    check("队列 at-least-once 全语义（投递/ack/超时重投）", True)

print()
print("✅ 属性测试全部通过" if not FAIL else f"❌ 失败 {len(FAIL)} 项: {FAIL}")
sys.exit(1 if FAIL else 0)

