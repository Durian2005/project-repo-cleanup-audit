#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回收站操作与核验的公共实现（Windows）。

被两个技能共同引用，是这部分知识的**唯一权威来源**：
  - `project-repo-cleanup-audit`（仓库内部清理）
  - `windows-cleanup-audit`（系统级清理）

改这里的任何一处，两个技能同时受益；不要在各自的 SKILL.md 里再抄一份。

包含三件事：
  1. recycle()            —— 把文件/目录送进回收站（走资源管理器同一条路）
  2. list_recycle_bin()   —— 解析 $I/$R 元数据，列出回收站条目（含原始路径）
  3. verify_recycled()    —— 核验"目标确实进了回收站"，按路径精确比对

用法：
    python recycle_bin.py list                    # 列出回收站全部条目
    python recycle_bin.py verify <绝对路径> [更多路径...]
    python recycle_bin.py delete <绝对路径> [更多路径...]   # 送回收站（会二次确认）

设计约束（都是实测换来的，别改）：
  - **返回码不可信**：FO_DELETE 成功也可能返回 2（ERROR_FILE_NOT_FOUND）。
    成败判据只有 `not os.path.exists(target)` + `fAnyOperationsAborted == False`。
  - **一次只传一个路径**：多路径塞进一次调用，会中途失败却只删掉一部分。
  - **$I 路径从 offset 28 起**（不是 24）；用结构体解析，别手工按冒号切。
"""

import ctypes
import os
import struct
import sys
from ctypes import wintypes

# ---------------------------------------------------------------- 常量

FO_DELETE = 3

# SILENT | NOCONFIRMATION | ALLOWUNDO | NOERRORUI | NOCONFIRMMKDIR
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_NOCONFIRMMKDIR = 0x0200

FLAGS = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR


# ---------------------------------------------------------------- 结构体

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


# ---------------------------------------------------------------- 送回收站

def recycle_one(path):
    """把单个路径送进回收站。

    返回 (ok: bool, note: str)。
    ⚠️ 判据是"目标是否还存在"，**不是** SHFileOperationW 的返回码 —— 见模块 docstring。
    """
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return False, "目标不存在，无需处理"

    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = path + "\0\0"        # 必须双 NUL 结尾
    op.fFlags = FLAGS
    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as exc:                       # noqa: BLE001
        return False, "调用失败: %s" % exc

    aborted = bool(op.fAnyOperationsAborted)
    gone = not os.path.exists(path)
    if gone and not aborted:
        return True, "已回收 (rc=%s，返回值不可信属正常)" % rc
    if gone and aborted:
        return False, "操作被中止，但目标已消失 —— 需人工确认 (rc=%s)" % rc
    return False, "仍在原地 (rc=%s, aborted=%s)" % (rc, aborted)


def recycle(paths, dry_run=False):
    """批量送回收站：**逐个调用**，每项独立成败，一项失败不拖垮整批。

    返回 [(path, ok, note), ...]。
    """
    results = []
    for p in paths:
        p = os.path.abspath(p)
        if dry_run:
            results.append((p, True, "[dry-run] 未执行"))
            continue
        ok, note = recycle_one(p)
        results.append((p, ok, note))
    return results


# ---------------------------------------------------------------- 解析回收站

def _recycle_roots():
    """枚举本机所有卷的 $Recycle.Bin 目录。"""
    roots = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        r = "%s:\\$Recycle.Bin" % letter
        if os.path.isdir(r):
            roots.append(r)
    return roots


def _decode_index(raw):
    """解析单个 $I 文件的元数据。

    布局（v2，Vista 及以后）—— 已逐字段实测核对：
        偏移  长度  含义
        0     8     版本头（当前为 2）
        8     8     原文件字节数
        16    8     删除时间（FILETIME）
        24    4     文件名字符数（**含**结尾 \\0）
        28    变长   文件名的 UTF-16LE 编码   ← 路径从这里开始，不是 24！

    ⚠️ 为什么不能按 24 解析：那 4 字节是长度字段，低字节会被当成路径的
    第一个字符（实测解出 `\\x19`）。肉眼看不出来、`in` 判断也照样通过，
    但做**精确相等**比对时必然失败，会误判成"回收站里没有该文件"。

    返回 (path, size, is_dir) 或 None。is_dir 需从 $R 数据体推断，这里返回 None。
    """
    if len(raw) < 28:
        return None
    try:
        size = struct.unpack("<Q", raw[8:16])[0]
        plen = struct.unpack("<I", raw[24:28])[0]
    except struct.error:
        return None

    if plen <= 0 or plen > 32767:      # 不合理长度，说明解析跑偏
        path = None
    else:
        try:
            path = raw[28:28 + (plen - 1) * 2].decode("utf-16-le")
        except UnicodeDecodeError:
            path = None

    # 兜底：整个 $I 按 UTF-16LE 在 0 / 1 两种对齐下各解一遍，正则抓最长的盘符路径。
    # 只有当结构体解析拿不到合理路径时才用 —— 它比结构体解析弱（可能抓到尾巴），
    # 但在布局与预期不符时是唯一的救命通道。
    if not path or not _looks_like_path(path):
        alt = _salvage_path(raw)
        if alt:
            path = alt
    if not path:
        return None
    return path, size


def _looks_like_path(s):
    if not s or len(s) < 4:
        return False
    return (len(s) > 1 and s[1] == ":") or s.startswith("\\\\")


def _salvage_path(raw):
    """双对齐正则兜底：返回最长的盘符路径，失败返回 None。"""
    import re
    pattern = re.compile(rb"(?:[A-Za-z]:\\|\\\\)[\x20-\x7e\u4e00-\u9fff]{3,500}")
    best = ""
    for shift in (0, 1):
        chunk = raw[shift:]
        # 按 UTF-16LE 解成文本再抓路径更稳（宽字符）
        try:
            text = chunk.decode("utf-16-le", errors="ignore")
        except Exception:                      # noqa: BLE001
            continue
        for m in re.finditer(r"[A-Za-z]:\\[^\x00-\x1f<>|\"*?]{0,500}", text):
            cand = m.group(0).rstrip("\x00").rstrip("\\")
            if len(cand) > len(best):
                best = cand
    return best or None


def list_recycle_bin():
    """列出回收站条目。

    返回 [{'path': 原始绝对路径, 'size': 原字节数, 'suffix': 后缀, 'i_file': $I路径, 'r_file': $R路径或None}, ...]

    ⚠️ **不要用"按名搜"当结论**：读原始字节搜 UTF-16LE basename，遇到
    `publish` / `bin` / `tmp` / `dist` 这类常见名会命中几十条噪音
    （实测按 `publish` 搜出 32 项）。按路径解析才是唯一可靠的口径。
    """
    entries = []
    for root in _recycle_roots():
        try:
            sids = os.listdir(root)
        except OSError:
            continue
        for sid in sids:
            sid_dir = os.path.join(root, sid)
            if not os.path.isdir(sid_dir):
                continue
            try:
                names = os.listdir(sid_dir)
            except OSError:
                continue
            r_suffixes = {n[2:] for n in names if n.startswith("$R")}
            for name in names:
                if not name.startswith("$I"):
                    continue
                i_path = os.path.join(sid_dir, name)
                try:
                    with open(i_path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                parsed = _decode_index(raw)
                if not parsed:
                    continue
                orig, size = parsed
                suffix = name[2:]
                r_path = os.path.join(sid_dir, "$R" + suffix)
                entries.append({
                    "path": orig,
                    "size": size,
                    "suffix": suffix,
                    "i_file": i_path,
                    "r_file": r_path if suffix in r_suffixes else None,
                })
            # 孤儿 $R（有数据体无索引）也记一笔，清空时要用
            i_suffixes = {n[2:] for n in names if n.startswith("$I")}
            for suffix in r_suffixes - i_suffixes:
                entries.append({
                    "path": None,
                    "size": None,
                    "suffix": suffix,
                    "i_file": None,
                    "r_file": os.path.join(sid_dir, "$R" + suffix),
                    "orphan": True,
                })
    return entries


# ---------------------------------------------------------------- 核验

def verify_recycled(targets):
    """核验 targets 里的每个绝对路径是否确实躺在回收站里（**按路径精确比对**）。

    返回 (ok_count, failed:[路径...], details:[(路径, 命中的回收站条目 or None), ...])
    """
    entries = list_recycle_bin()

    def norm(p):
        if p is None:
            return None
        p = os.path.normpath(p)
        if p.endswith("\\") and len(p) > 3:
            p = p[:-1]
        return p.lower()

    index = {}
    for e in entries:
        key = norm(e["path"])
        if key:
            index.setdefault(key, []).append(e)

    failed, details = [], []
    for t in targets:
        key = norm(t)
        hit = index.get(key)
        details.append((t, hit[0] if hit else None))
        if not hit:
            failed.append(t)
    return len(targets) - len(failed), failed, details


# ---------------------------------------------------------------- 清空后的孤儿清理

def find_orphans():
    """找出 SHEmptyRecycleBinW 清不掉的孤儿项。

    返回 {'index': [$I路径...], 'data': [$R路径...]}
      - 孤儿索引：有 $I 无 $R（不占空间）
      - 孤儿数据：有 $R 无 $I（可能是 0 字节目录）
    ⚠️ 别碰 `$Recycle.Bin` 里的 desktop.ini。
    """
    result = {"index": [], "data": []}
    for root in _recycle_roots():
        try:
            sids = os.listdir(root)
        except OSError:
            continue
        for sid in sids:
            sid_dir = os.path.join(root, sid)
            if not os.path.isdir(sid_dir):
                continue
            try:
                names = os.listdir(sid_dir)
            except OSError:
                continue
            i_suf = {n[2:] for n in names if n.startswith("$I")}
            r_suf = {n[2:] for n in names if n.startswith("$R")}
            for s in i_suf - r_suf:
                result["index"].append(os.path.join(sid_dir, "$I" + s))
            for s in r_suf - i_suf:
                result["data"].append(os.path.join(sid_dir, "$R" + s))
    return result


def empty_recycle_bin():
    """清空回收站（所有卷 / 当前用户）。不可逆，调用方负责先留档与确认。

    返回 SHEmptyRecycleBinW 的返回码（0 == S_OK）。**返回码在这里是可信的**。
    """
    # 无确认框 | 无进度条 | 无声
    return ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)


# ---------------------------------------------------------------- 移动/属性辅助

def chmod_for_delete(path):
    """递归清掉只读属性，给删除让路（回收站目录里的文件常带只读）。"""
    for base, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(base, name), 0o666)
            except OSError:
                pass
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass


def is_locked(path):
    """用独占打开判断目标是否被进程占用（比 handle.exe 省事）。

    对目录返回其下第一个被找到的文件的结果；全部成功即视为未被占用。
    """
    targets = []
    if os.path.isfile(path):
        targets = [path]
    else:
        for base, _dirs, files in os.walk(path):
            targets = [os.path.join(base, f) for f in files[:5]]
            if targets:
                break
    if not targets:
        return False

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    INVALID = ctypes.c_void_p(-1).value
    kernel32 = ctypes.windll.kernel32
    for t in targets:
        handle = kernel32.CreateFileW(t, GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
        if handle == INVALID:
            return True
        kernel32.CloseHandle(handle)
    return False


# ---------------------------------------------------------------- CLI

def _main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 0
    cmd = argv[1]

    if cmd == "list":
        entries = list_recycle_bin()
        print("回收站条目：%d" % len(entries))
        for e in entries:
            if e.get("orphan"):
                print("  [孤儿数据] %s" % e["r_file"])
            else:
                size = e["size"] or 0
                print("  %10d B  %s" % (size, e["path"]))
        return 0

    if cmd == "verify":
        targets = [os.path.abspath(p) for p in argv[2:]]
        if not targets:
            print("用法: recycle_bin.py verify <绝对路径> [...]")
            return 2
        ok, failed, details = verify_recycled(targets)
        for t, hit in details:
            if hit:
                print("[OK  ] 已回收（在回收站找到，原大小 %s B）: %s" % (hit["size"], t))
            else:
                print("[FAIL] 回收站里找不到: %s" % t)
        print("\n%d/%d 项已确认进回收站" % (ok, len(targets)))
        return 1 if failed else 0

    if cmd == "delete":
        targets = [os.path.abspath(p) for p in argv[2:]]
        if not targets:
            print("用法: recycle_bin.py delete <绝对路径> [...]")
            return 2
        print("将把以下路径送进回收站（可还原）：")
        for t in targets:
            print("  %s" % t)
        if os.environ.get("RECYCLE_YES") != "1":
            answer = input("确认？输入 yes 继续: ").strip().lower()
            if answer != "yes":
                print("已取消。")
                return 0
        for path, ok, note in recycle(targets):
            print("  [%s] %s  %s" % ("OK  " if ok else "FAIL", path, note))
        return 0

    print("未知命令: %s" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
