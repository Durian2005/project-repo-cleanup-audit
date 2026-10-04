---
name: project-repo-cleanup-audit
description: >
  清理「项目 / 代码仓库」内部的无用文件：只读扫描 → 四档分类 → 逐档确认 → 清单驱动送回收站 → 复核。
  与 windows-cleanup-audit 的分工：那个面向**系统**（已装软件重复项、系统垃圾、回收站体积）；
  这个面向**仓库内部**（构建产物、未入库的证据材料、陈旧产物、模板残留、临时脚本）。
  触发词：清理项目、清理仓库、项目里没用的文件、清理一下项目、仓库瘦身、删掉没用的文件、
  检查一下项目、clean up the project、项目太大。
agent_created: true
---

# 项目仓库残留清理

用户说"检查一下项目 / 清理一下没用的文件"时走这个流程。

## 铁律

1. **第一轮只读。** 只出报告 + `AskUserQuestion` 逐档确认，一个字节都不删。
2. **回收站，不是删除。** 项目里的文件往往**未入库**（被 `.gitignore` 挡着）⇒ 删了就真没了。
   一律 `SHFileOperationW` 送回收站，用户清空回收站前可还原。
   ⚠️ **但"送进回收站"必须实测核验，不能假定生效。** 批量删之前先建一个自建测试文件
   试删一次，确认它真的出现在 `$Recycle.Bin` 里 —— 本案实测：同一环境下同类调用，
   有批次的删除没有留下任何回收站条目（事后无法找回，`$I`/`$R` 都查不到）。
   省掉这一步，"可还原"就是空头承诺。
3. **已入库的文件是另一条路**：走 `git rm` + 编译验证 + 单独 commit（历史里有，可恢复），
   **不要**送回收站 —— 否则工作区改动和"删掉了"混在一起，看不出发生了什么。
4. **凭据类文件单独拎出来**：任何 `*pepper*` / `*.pem` / `*.key` / 授权码配置文件，
   先移出待清目录、单独报告，**不参与批量回收**。
5. **库、日志、备份目录不在动刀范围**：数据库数据目录、`tools/backup/`（回滚用）、
   `tools/evidence/`（取证材料）默认保留，只在报告里列为"已确认不动"。

## 一、只读扫描（照抄这个顺序）

```bash
git status --porcelain            # 先确认工作区干净（不干净就别清，避免混淆）
cat .gitignore                    # 忽略规则决定"删了能不能找回"
ls -la                            # 顶层
for d in *; do echo "$(du -sm "$d"|cut -f1) MB  $d"; done   # 各顶层体积，找体积大头
du -sm <大目录>/* | sort -rn | head -12                     # 再往里钻一层
```

必查的几类：

| 查什么 | 命令/判据 |
|---|---|
| 构建产物 | `bin/ obj/ publish/ target/ dist/ node_modules/` —— 记体积，**别急着删**（重建成本差异大） |
| 未入库的垃圾 | `git status --porcelain --ignored=matching \| grep '^!!'` |
| 临时目录 | `.workbuddy/tmp/`、项目内 `tmp/` —— 一次性脚本与日志 |
| 嵌套/重复输出 | `ls */publish/publish`、`bin/**/publish`（`dotnet publish` 默认输出常与 `-o` 目录重复） |
| 陈旧产物 | 比"源产物"旧的同名目录，如后端托管的 `wwwroot/` 落后于 `dist/` |
| 模板残留 | 脚手架文件（`WeatherForecast.cs`、`*.http`、示例 Controller）—— **grep 全项目确认零引用** |
| 一次性截图/日志 | 逐个 basename 去 md/py/mjs/json 里 grep，**零引用**才算候选 |

> 判"零引用"的脚本口径：遍历全项目文本文件（跳过 `node_modules/.git/target/bin/obj/dist`），
> 对每个候选取 basename 求 `basename in text`，**排除它自己所在的文件**。

## 二、四档分类（这是报告的主干）

| 档 | 内容 | 默认建议 |
|---|---|---|
| A | 纯本地产物/残留（临时脚本、旧安装包、重复 publish、IDE 状态 `.vs/`） | 清（零风险，自动重建） |
| B | 构建缓存（`target/`、`bin/obj`、`node_modules`） | **看磁盘余量**：空间富裕就留（重建代价高） |
| C | 证据/备份（含真实数据的截图、库快照） | 只清**零引用**的；被文档点名的全留 |
| D | 已入库的残留（模板文件等） | 走 `git rm` + 编译 + commit |

**先算磁盘余量再给建议**：`df -h`。若余量充足（如 >100 GB），把 B 档标成"保留（推荐）"，
因为删构建缓存换来的是十几分钟到几十分钟的重新编译，收益只有几 GB。

## 三、确认

用 `AskUserQuestion`，**一档一题**，最多四题。题干里写清：体积、为什么可以删、删了要付什么代价。
选项给"推荐"项并置于首位。别把四档合成一个 yes/no。

## 四、执行：清单驱动（关键设计）

**不要临时拼路径列表去删。** 分两步：

**第 1 步** 生成清单 `cleanup-manifest-<日期>.txt`，逐项写 `FILE/DIR + 字节数 + 相对路径`，
同时把凭据类文件先搬走。清单既是给用户看的凭据，也是第 2 步的输入。

**第 2 步** 回收脚本**解析清单**取路径 ⇒ 保证"看到什么就删什么"，不会漏也不会多。

```python
import ctypes, os, re
from ctypes import wintypes

FO_DELETE = 3
FLAGS = 0x0004 | 0x0010 | 0x0040 | 0x0400 | 0x0200   # SILENT|NOCONFIRM|ALLOWUNDO|NOERRORUI|NOCONFIRMMKDIR

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

def recycle(path):
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = path + "\0\0"        # 必须双 NUL 结尾
    op.fFlags = FLAGS
    ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return not os.path.exists(path)  # 判据只能用 exists，函数返回值/异常都不可靠
```

- **一次一个路径**。多路径一次传入会中途失败，还返回 2 却已经删掉几个。
- 成败判据**只认** `os.path.exists` ⇒ 删成功也可能返回非零码。
- 被进程占用的项跳过即可，别让一项拖垮整批。
- ⚠️ **解析清单必须用正则，不能按空格切分**。清单行形如 `DIR  10705 B  <相对路径>  (5 个文件)`，
  按空格切会把 `10705 B` 一并当成路径 ⇒ 白名单校验必然失败。用
  `^\s*DIR\s+\d+\s+B\s+(\S+)\s+\(\d+` 取路径。（本案首轮就因此触发中止 —— 见下条。）
- **白名单 + 整批中止**是这套设计的关键：相对路径必须与白名单**逐字符相等**（不做通配、不做推断），
  再叠一条"必须在 ROOT 前缀之下"与禁区名单（`.git` / `memory` / `evidence` / `backup`），
  任一不满足就 `sys.exit(1)` 且**一个都不删**。解析 bug 正是被这条拦住的。

## 五、复核（缺一不可）

1. 逐项目标 `test -e` 必须全部为"否"；顺带报"回收站 **n/n 项**、**x MB**，失败 0"。
2. 回收站里要真有。**结论性核验只走"按路径解析"**：
   - ⚠️ **别拿"按名搜"当结论**：读 `X:\$Recycle.Bin\<SID>\$I*` 原始字节搜 UTF-16LE 的 basename，
     只在 basename **唯一且够长**时才有意义。遇到 `publish` / `bin` / `tmp` / `dist` 这类常见名会
     命中几十条噪音（实测按 `publish` 搜出 **32 项**），既不能证明删到了，也不能证明没删到。
     它只适合当"探针文件"这类唯一名字的快查。
   - **按路径解析（唯一可靠）**：⚠️ **不要假设路径偏移**。实测 `offset 24` 解出来是单字符垃圾
     （`16:20` 那 4 字节是 FILETIME 低位，不是路径长度）。稳妥做法：整个 `$I` 按 UTF-16LE
     在 **0 / 1 两种对齐**下各解一遍，用 `[A-Za-z]:\\[^\x00-\x1f<>|"*?]{0,500}` 正则抓最长的那个；
     再把解出的原始路径与**目标绝对路径逐字符比对**（去尾 `\`、大小写不敏感）才算通过。
   - `$I` 与 `$R` **后缀相同**，可据此把索引与数据体配成对。
3. `git status` 应仍与清理前一致（除了你刻意 `git rm` 的）。
4. 改动过源码就重编一次（`dotnet build -c Release` 之类），确认为 0 错误 0 警告。
5. 报体积前后对比，并**明说"进回收站 ≠ 空间已释放"**。

## 六、清空回收站（仅在用户明确要求时）

清空**不可逆**。动手前必须：① 把回收站现有内容逐条导出留档（原始路径 + 删除时间 + 是否有数据体）；
② 确认里面**没有**活跃项目文件；③ 用户已明确说"可以清了"。

```python
import ctypes
# 所有卷、当前用户；flags = 无确认框|无进度条|无声
rc = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)  # 0 == S_OK
```

**清空后通常还剩两类残留，`SHEmptyRecycleBinW` 都不管**，需再手动清一遍：

| 残留 | 判据（对 `$I`/`$R` 取后缀集合） | 处理 |
|---|---|---|
| 孤儿索引（有 `$I` 无 `$R`，不占空间） | `suf($I) - suf($R)` | `os.remove` 这些 `$I` |
| 孤儿数据（有 `$R` 无 `$I`，可能是 0 字节目录） | `suf($R) - suf($I)` | 文件 `os.remove` / 目录 `os.rmdir`（只删空目录） |

- ⚠️ 别碰 `$Recycle.Bin` 里的 **`desktop.ini`**（目录系统文件）。清完剩它一个才是正常态。
- ⚠️ 删 `$I`/`$R` 是在写系统回收站目录 ⇒ 可能触发**沙箱提权**（会出现 sandbox bypass 提示）。
- 清空前留档写成 `<workspace>/.workbuddy/recycle-final-<日期>.txt`，别只在终端里滚过去。

## 七、坑

| 坑 | 真相 |
|---|---|
| 直接删构建缓存 | `tauri/target` 常占项目 80%+（本案 1846 MB / 2121 MB）。先看磁盘余量，别为了数字好看把增量编译毁掉 |
| `dotnet publish` 默认输出 | 会在 `bin/<cfg>/<tfm>/publish/` 再留一份，与 `-o` 目标重复。两边都算"产物"，别误判成两套构建 |
| `-o` 也挡不住嵌套 publish | 即便显式 `dotnet publish -o out`，**仍会在 `out/` 内再建 `out/publish/`**（只含 `staticwebassets.endpoints.json` + appsettings + `web.config`）。加上 `bin/<cfg>/<tfm>/publish/` 与 `bin/<cfg>/<tfm>/<rid>/publish/`，同一份工程能留 **4 处**冗余 |
| RID 构建中间产物 | `bin/<cfg>/<tfm>/<rid>/` 可占 20 MB+，且**无任何脚本引用**（`-o` 才是真发布目标）。但它位于 `bin/` 内 ⇒ 归"构建缓存"档，别当成零风险残留 |
| 运行时日志目录会被重建 | `tools/buildlogs/` 这类目录看着像残留，但验证/构建脚本会 `mkdir -p` 重建 —— 删了下次跑就回来。要么当"活目录"不动，要么在报告里明说它会再生，别声称"已清掉" |
| 凭据副本随构建产物扩散 | `appsettings.Local.json` / `.env` 这类本地覆盖配置会被复制进每个 `bin`/`publish` 目录（本案 **9 份、md5 全同、每份都含真实授权码**）。删目录会连带删副本，但**送回收站 ≠ 擦除** —— 单独报给用户，不要混在"已清理"里一句带过 |
| 目录名与文件名撞车 | 路径写成 `D:\x.pepper` 而 `D:\x.pepper` 是个**空目录** ⇒ `UnauthorizedAccessException`，看着像权限问题实为路径类型不对 |
| 删除源文件后忘记编译 | 模板残留往往被 `using` 或 DI 隐式引用；**先 grep 再删，删完必编译** |
| 陈旧产物不是垃圾 | 诸如后端托管的 `wwwroot/` 落后于 `dist/`，删了会让"只跑后端"直接 404。它是**待修的不一致**，报告出来让用户选刷新还是移除 |
| 只清仓库不管别处 | 探针可能在 `%TEMP%` 留过含密钥的目录；扫一遍 `*pepper*` / `*token*`，别让副本留在外面 |
| "回收站里没东西" ≠ 东西还在 | 资源管理器只显示**有 `$R` 数据体**的项；孤儿 `$I` 既不显示也不占空间。所以"回收站体积小"既不能证明"没删过"，也不能证明"还能还原"——要还原就按 §五 实测 |
| 清空回收站后还有残留 | 见 §六：孤儿 `$I` 索引、孤儿 `$R` 数据体都不受 API 管 |

## 八、收尾

- 保留清单文件当记录：`<workspace>/.workbuddy/cleanup-manifest-<日期>.txt`。
  若之后又清了回收站，再加一份 `<workspace>/.workbuddy/recycle-final-<日期>.txt`（清空前的全量留档）。
- 写工作区日志 `<workspace>/.workbuddy/memory/YYYY-MM-DD.md`：清了什么、多少 MB、
  回避了什么、待用户决的问题（如陈旧产物）。
- 已入库的删除单独 commit 并说明理由；**未被要求就 don't push**，明确告知本地领先远端几个提交。
