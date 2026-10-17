# 名单类附件解析（通报 / 公示 / 招标清单 / 处罚名录）

> **什么时候读这章**：任务形态是"一份页面 = 一份名单，名单在附件里（doc/docx/pdf/图片）
> 或直接贴在正文表格里"，且验收口径是"**解析行数 ≈ 正文声明的款数/家数**"。
> 典型：工信部 APP 通报、地方监管公示、上市公司处罚名录、招标中标清单。
>
> 引擎侧入口（Full 版）：`universal_scraper.name_list`（解析语义）、
> `universal_scraper.attachments`（发现与落盘）、`universal_scraper.local_ocr`（本地 OCR）、
> `core.fetch_json` / `core.run_tool`（接口与外部转换器）。

## 一、附件发现：**镜像并集**是硬要求

同一篇通报常跨多个栏目镜像。**只信 canonical 会少内容**：实测某通报正页只挂 3 张名单图
（26 条），镜像页挂 5 张（= 正文声明的 38 条）。

- 抽链规则（`attachments.extract_targets`）：
  ① `viewer.html?file=<真附件>` —— pdfjs 外壳，取 `file=` 参数才是真附件；
  ② `href/src` 直达已知扩展名（**跳过 viewer 外壳本身**）；
  ③ `/attach/`、`/oldfile/` 等路径兜底（含无扩展名的 attach 链）；
  ④ 正文图片（`/picture/...png`）—— 名单期常把表格做成图片。
- 多页取并集：`attachments.mirror_union([正页, 镜像1, ...], base)`，按出现顺序去重。
- 落盘契约（`attachments.download_targets`）：
  - 文件名 = `前缀_序号_真扩展名`；**存在性按 `前缀_序号.*` 判**（落盘前会按内容改名，
    只查 URL 推断名会让改名过的文件每轮"不存在"→ 重下），存在且非空即跳过（幂等）；
  - 扩展名**三级定名**：URL 路径 → `?fileUrl=` 包壳参数 → 默认 `.bin`；
    下载后再按 **magic bytes 校正**（OLE/ZIP 容器按流名子串细分 doc/docx、xls/xlsx；
    ZIP 目录区可能在文件尾部，必须扫全量；OLE 先认 WordDocument 再认 Workbook）；
  - 内容是 HTML/XML 外壳（viewer 页、网关错误页，**含 BOM 与 `<?xml/<!--` 开头**）
    → **不写盘**，记 `failed`；已落盘的假附件在复检时删除重试；
  - `/files/` 等路径兜底会排除 js/css/json 等静态资源（否则下成 `.bin` 垃圾）；
  - 每个 URL 过 `core.assert_public_url`（URL 来自页面，不可信）。

> 坑（真实事故）：白名单漏了图片类型 → 30 个真实附件（8 OLE/12 ZIP/10 PNG）落成 `.bin`；
> 而"清理下错文件"的逻辑每轮又删掉它们 → 每轮重复下载 30 个文件，幂等性被破坏。

## 二、五条解析链与选链顺序

`name_list.aggregate(files, inline_html=...)` 按 **文档优先 → 图片兜底 → 内联殿后** 选链：

| 链 | 前提 | 实现要点 |
|---|---|---|
| docx | python-docx | 整篇表格串接后统一装配（第二张表常无表头） |
| doc | LibreOffice 在（`core.run_tool(["soffice", ...])`） | 转 HTML 取**真表格**；textutil 扁平分格仅兜底 |
| pdf | pdfplumber | **跨页串接**再装配（第 2 页起没有表头行）；无文本层则渲染成图走图片链 |
| 图片 | 云端 VLM（+ 本地 Vision 交叉核验） | 见第五节 |
| 内联 | 调用方传 `inline_html` | 正文 `<table>`，同样整篇串接 |
| xlsx/xls | openpyxl（经 `pdf_table.parse_xlsx`） | 该函数返回的行是"表头→值"字典，按**列名**映射，不喂给按列位的装配 |

`parse_attachment_ex` 逐附件回传 `{rows, chain, error}`；`aggregate_ex` 再把它们汇成
`files` 列表 + `notes`（0 行与失败也进账，`src` 里以 `| 未出结果: …:0(原因)` 结尾）。
**"解析坏了"与"名单本来就是空的"必须可区分**——error 里带异常类型 / 工具退出码 / 缺失依赖。

> 坑（真实事故）：同一篇通报既有 doc 名单又有它的截图版 → 两条链都解析会**重复计数**
> （实测 41 → 55 行）。所以图片只在文档类附件**全部解析不出行**时才启用。
> 下架/回头看类常把名单直接贴正文（无附件）——`aggregate` 需要 `inline_html` 才能兜住。

## 三、行语义六条（每条都对应一个真实缺陷）

1. **表头驱动列位映射**，且**空单元格不能删**——删了整行左移，"企业名称"会被读成"应用名称"。
   表头别名要够全（"应用版本""运营者名称""样品来源""问题项""下架版本"…）；
   判定表头行时**允许一个未知格**（别名滞后一次就漏判）。
2. **rowspan 续行并回**：前几列 `rowspan=N` 时，每个物理行只装一格
   （41 个 APP 被读成 89 行）。判据：非空格全落在"所涉问题"列及其右侧，或整行只有一格。
3. **"一问题一行"合并**：同一 APP 按所涉问题拆成多行（89 行数字序号只到 60）。
   判据：身份字段（名称/企业/版本/来源）全同。
4. **同 APP 多来源/版本合并**：序号与名称相同（1 款 APP 列了 App Store 与安卓两个来源）；
   额外来源存 `alt_sources`，不丢信息。**声明数是按 APP 计的**，不合并就会超差。
5. **"无序号即续行"只在**该段**序号列至少出现 2 个序号值时启用**：Word 自动编号不落文本时
   序号格整列为空，误用该规则会把 41 行并成 1 行（名称粘连成"智慧树ClassIn"）。
   "段"按表头切：`parse_docx`/`parse_pdf`/内联都把整篇表格串接成一份行序列，
   后段表头可能没有序号列——`seq_ok` 必须**逐段重算**，只在首段算一次会让后段整段塌成一条。
6. **换行碎片并回**：单元格折行被切成独立行（"上海邮乐网络"/"技术有限公司"）；
   按列位并回，内容形状（问题词/版本形）可覆盖列位。并回 0 个字段（内容全在未映射列）
   时必须落回兜底装配，不能把行吞掉。
7. **合计/小计/备注行、分节行**：跳过并计入 `stats["dropped"]`——并进上一条会把
   "合计 41款"写成上一条 APP 的"所涉问题"（实测）。

`table_rows(cells_rows, stats={...})` 会回填 `segments / merged_fragments / dropped`
（丢行样本 [(行号, 原因, 前几格)]）：0 行必须可归因，不能只有"空结果"。

合并只在**附件内部**做；跨附件不合并（同一 APP 出现在不同地区名单里是两条独立记录）。

## 四、声明数对账与完备率

- **声明数**（`name_list.stated_count`）是**句子级求和**：多附件期正文给多个数
  （部本级 + 各省管理局分列，如 71+74=145；51+282=333；107 APP+13 SDK=120）；
  **同一句里并列两个口径也要都算**（"尚有71款…，各通信管理局检查发现仍有74款…"）。
  中文数字按 零/〇/两 + 十/百/千/万 正确进位（`一百零六` = 106，不是 6）。
- 必须排除两类句子：
  - 累计口径："对…368款APP提出整改要求"（不是本批名单）；
  - 汇总复述："对上述共计106款APP进行下架"（重复计数；"共计"仅与"上述"同句才排除）。
- 验收口径：**逐篇** `|解析行数 − 声明数| ≤ 5%`。用 `audit_counts_ex(pairs)`：
  它把"没抽出声明数"单列成 `no_stated`——**不要**用 `assert not audit_counts(...)`，
  那种写法在"根本没做对账"时恒过（空列表既是"全达标"也是"没检查"）。
- **完备率按"源表声明的列"计**（`name_list.declared_fields` + `audit_completeness`）：
  下架名单的表头常只有"序号/应用名称/应用开发者/应用版本"——这类空值不是解析缺失，
  拿"五列全要求"去算会把合格数据判成不合格。表头**读不到**时 `declared_fields` 返回
  `None`（≠ 空集合），这些来源按五列从严并计入 `sources_unreadable`；
  一个格都没检查时 `rate` 为 `None`（不是 0% 也不是 100%）。
- **特殊名单**：表头含"复测"的附件是"反复出现同类问题企业"名单（如下架通报的附件7），
  其行打 `list_kind` 后**不计入声明数**，但记录保留。表头读不到时无法判定，会进 `notes`。

## 五、图片名单 OCR

- **切行锚在序号列**：按整宽横线切会把"一格多问题"的内部细线也算进来，切出的带不对齐
  逻辑行（下一行的公司名被并进上一行）。序号列每个逻辑行恰好一格，其上下边界即行界
  （`local_ocr.row_bands(path, x_frac=(0.0, 0.10))`）。
- **逐行 3× 放大**再送 VLM：整图一次性 OCR 实测公司名错字 ~11%（小图每行只有几十像素高）。
- **双引擎交叉核验**（`local_ocr.cross_check_verdict`）：
  - 本地 macOS Vision 是独立第二引擎（离线免费）：`objc.loadBundle` 手动加载
    `/System/Library/Frameworks/Vision.framework`，**recognitionLevel 必须用 0（fast）**
    ——1（accurate）档对中文小字返回乱码；
  - 两读一致 → `agree`；不一致 → `differ`（保留主读，副读存 `ocr_alt` 备查）；
    主读比副读**恰好多一个字**且副读是完整字段值 → `alt_wins`（改信副读）。
    实测：云端把"苏州云网通信息科技"写成"苏州云网通**信**信息科技"（顺句补字偏置）。
  - **本地引擎不可用时写 `ocr_check="unavailable"`**（不是 `differ`）——把"第二引擎没跑"
    标成"两引擎分歧"是对交付数据的虚假陈述；`local_ocr.unavailable_reason()` 给出原因。
- 无框线图（或本地 OCR 不可用）→ 整图 + **数组提示词**兜底；拿"单行"提示词去问整图
  只会返回一行。
- **空结果不写缓存**：一次瞬时失败（限流/空响应）若被缓存，会把"这张图没有名单"
  永久固化，重跑也修不回来；命中空缓存视为未命中并重试。
- 扫描版 PDF：`parse_attachment_ex` 检测无文本层 → 逐页渲染 PNG → 走图片链；
  一页渲染失败保留已渲染的页，并把"第几页 + 异常类型"写进 error。

## 六、最小用法

```python
from pathlib import Path
from universal_scraper import name_list as NL, attachments as AT

# 1) 附件发现（正页 + 全部镜像取并集）
atts, imgs = AT.mirror_union([html_canonical, *mirror_htmls], base="https://site")
r = AT.download_targets(client, atts + imgs, Path("data/files/art_x"), prefix="att",
                        referer="https://site/")
# 2) 解析（文档优先、图片兜底、内联殿后）+ 附件内合并 + 二次上传版本去重
d = NL.aggregate_ex(r["files"], inline_html=html_canonical,
                    cache_dir=Path("out/doc_html"), ocr_cache=Path("out/ocr_cache"))
for f in d["files"]:            # 0 行/失败也要看：error 非空即"解析坏了"而非"名单为空"
    if f["rows"] == 0:
        print("⚠️", f["name"], f["chain"], f["error"])
# 3) 对账（no_stated 必须为空，否则说明正文计数句没识别出来）
stated = NL.stated_count(html_canonical)
main = [x for x in d["rows"] if not x.get("list_kind")]
rep = NL.audit_counts_ex([("art_x", len(main), stated)])
assert not rep["bad"] and not rep["no_stated"], rep
NL.write_jsonl(d["rows"], Path("data/records.jsonl"))
```

## 七、坑速查（现象 → 处置）

| 现象 | 处置 |
|---|---|
| 行数比声明数多一截 | ① 图片与文档都解析了（文档优先）② 同一名单二次上传版本（名称集合重叠 ≥90% 去重） |
| 行数只有声明数的零头 | ① 序号列整列为空时误用"无序号即续行"（须逐段判"≥2 个序号值"）② 跨页续表没串接（丢列映射） |
| 名称被粘连成一串 | rowspan 续行没并回 / 表头列位映射被空单元格位移破坏 |
| 某列整列空 | 源表本来没有该列（按声明列算完备率）；或表头别名缺失（补别名表） |
| `.bin` 附件反复重下 | 存在性要按 `前缀_序号.*` 判（落盘前会按内容改名）；别用"清理下错文件"当补救 |
| 附件"下载成功"却解析 0 行 | 先看 `aggregate_ex["files"][i]["error"]`：HTML 外壳 / 损坏文件 / 缺依赖各不相同；BOM 开头的错误页也算外壳 |
| `.xlsx` 名单解析为空 | xlsx 链复用 `pdf_table.parse_xlsx`（行是"表头→值"字典，按列名映射）；`.xls` 不受支持会显式报错 |
| OCR 公司名错字多 | 换"序号列锚定逐行 + 3× 放大"；开本地 Vision 交叉核验（`ocr_check` 全 `differ` 时先查 `unavailable_reason()`） |
| 重跑结果反而变差/空 | 检查 OCR 缓存：空结果不得写缓存（命中空缓存应视为未命中重试） |
| 声明数比名单少 | 正文是"部本级 + 各省分列"，要句子级求和（含同句多值）；别把累计口径句算进来 |
| 对账"全绿"但心里没底 | 用 `audit_counts_ex`：`no_stated` 非空 = 根本没做对账，不是达标 |
