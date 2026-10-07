# -*- coding: utf-8 -*-
"""recycle_bin.py 的端到端自测。

从零构造真实文件，走完整流程：送回收站 -> 核验 -> 反例验证。
反例很重要：**从不报警的检查器等于没测过**。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recycle_bin import (  # noqa: E402
    find_orphans,
    is_locked,
    list_recycle_bin,
    recycle,
    verify_recycled,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("[%s] %s %s" % ("OK  " if cond else "FAIL", name, detail))


# ---------- 准备：造两个真实目标 ----------
base = tempfile.mkdtemp(prefix="rb_selftest_")
f1 = os.path.join(base, "probe_file.txt")
with open(f1, "w", encoding="utf-8") as fh:
    fh.write("recycle_bin selftest payload 中文内容\n" * 20)
d1 = os.path.join(base, "probe_dir")
os.makedirs(os.path.join(d1, "sub"))
with open(os.path.join(d1, "sub", "inner.bin"), "wb") as fh:
    fh.write(b"\x00\x01\x02" * 500)

size1 = os.path.getsize(f1)
print("测试目标：")
print("  文件 %s (%d B)" % (f1, size1))
print("  目录 %s" % d1)
print()

# ---------- 1. 送回收站 ----------
res = recycle([f1, d1])
for path, ok, note in res:
    check("送回收站: %s" % os.path.basename(path), ok, note)

check("文件已从原位置消失", not os.path.exists(f1))
check("目录已从原位置消失", not os.path.exists(d1))

# ---------- 2. 核验（正向） ----------
ok_n, failed, details = verify_recycled([f1, d1])
check("核验: 两项都确认在回收站", ok_n == 2, "ok=%d failed=%s" % (ok_n, failed))
for t, hit in details:
    check("  命中记录可读: %s" % os.path.basename(t),
          hit is not None and hit["size"] is not None,
          "size=%s" % (hit["size"] if hit else None))

# 大小应一致（目录的 size 语义不同，只查文件）
f1_hit = next((h for t, h in details if t == f1), None)
if f1_hit:
    check("文件原大小记录一致", f1_hit["size"] == size1,
          "rec=%s real=%s" % (f1_hit["size"], size1))

# ---------- 3. 反例验证（关键：证明检查器真的会报警） ----------
ghost = os.path.join(base, "never_existed_%d.txt" % os.getpid())
ok_n2, failed2, _ = verify_recycled([ghost])
check("反例: 不存在的路径必须报 FAIL", ok_n2 == 0 and len(failed2) == 1,
      "ok=%d failed=%d" % (ok_n2, len(failed2)))

# 大小写不敏感应仍然命中
ok_n3, failed3, _ = verify_recycled([f1.upper()])
check("反例对照: 大小写不同仍应命中（大小写不敏感）", ok_n3 == 1, "ok=%d" % ok_n3)

# 末尾多个反斜杠应仍命中
ok_n4, _, _ = verify_recycled([f1 + os.sep])
check("反例对照: 末尾多一个分隔符仍应命中", ok_n4 == 1, "ok=%d" % ok_n4)

# ---------- 4. 解析遍历一致性 ----------
entries = list_recycle_bin()
check("list_recycle_bin 能解析出大量条目", len(entries) > 100, "共 %d 条" % len(entries))
no_path = [e for e in entries if not e.get("orphan") and not e.get("path")]
check("解析出的条目都带合法路径", len(no_path) == 0, "异常 %d 条" % len(no_path))

# 抽样确认路径形态正确（第 2 个字符是冒号）
bad_shape = [e for e in entries[:500]
             if not e.get("orphan") and e.get("path")
             and not (len(e["path"]) > 1 and e["path"][1] == ":")]
check("抽样 500 条路径形态正确（盘符）", len(bad_shape) == 0, "异常 %d 条" % len(bad_shape))

# ---------- 5. 孤儿统计不报错 ----------
orph = find_orphans()
check("find_orphans 可正常执行",
      isinstance(orph, dict) and "index" in orph and "data" in orph,
      "孤儿索引 %d / 孤儿数据 %d" % (len(orph["index"]), len(orph["data"])))

# ---------- 6. is_locked ----------
tiny = os.path.join(base, "lockprobe.txt")
with open(tiny, "w") as fh:
    fh.write("x")
check("is_locked: 未被占用的文件返回 False", is_locked(tiny) is False)

# ---------- 汇总 ----------
print()
print("=" * 56)
print("通过 %d 项 / 失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败清单：")
    for n in FAIL:
        print("  -", n)
print("=" * 56)
sys.exit(1 if FAIL else 0)
