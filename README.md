# 业余无线电 QSO 记录聚合与 QSL 卡片打印系统

> **QSL Card Manager** — a local-first desktop application that aggregates amateur
> radio QSO records (ADIF, QRZ.com, LoTW, eQSL, Club Log, HamQTH), keeps a
> de-duplicated logbook in SQLite, records QSL card dispatches and returns, and
> renders print-ready QSL card sheets: 90 x 140 mm with 3 mm bleed, imposition,
> crop marks, 3 mm calibration guides, optional device CMYK and PDF/X style page
> boxes.
>
> GUI-first (Tkinter, no browser runtime), core is the standard library plus
> fpdf2, and every credential stays in a local encrypted vault (DPAPI on Windows,
> ChaCha20 + HMAC elsewhere). No telemetry, no cloud, no developer server.
>
> Built from a formal requirements specification:
> [docs/QSL卡片打印系统-需求规格说明书.md](docs/QSL卡片打印系统-需求规格说明书.md)

按 [需求规格说明书](docs/QSL卡片打印系统-需求规格说明书.md) 实现的可运行程序：从 ADIF 文件、
QRZ.com、ARRL LoTW、eQSL.cc、Club Log、HamQTH 汇集 QSO 记录，去重入库，按规则筛选，
再用模板渲染成 **90 x 140 mm、四边 3 mm 出血** 的可直接打印 PDF（含拼版、裁切线、3 mm 校准线）。

设计目标是 **低硬件开销**：核心只用标准库，PDF 引擎按需加载，重活走流式处理与批量事务。

---

## 快速开始

~~~powershell
# 1. 准备环境（唯一第三方依赖是 fpdf2）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 生成配置并填写台站信息
.\.venv\Scripts\python.exe -m qslcard config init --path demo-config.json

# 3. 导入 ADIF（文件或整个目录，可重复执行不会产生重复记录）
.\.venv\Scripts\python.exe -m qslcard -c demo-config.json import samples

# 4. 查看台账
.\.venv\Scripts\python.exe -m qslcard -c demo-config.json db --info

# 5. 生成打印文件
.\.venv\Scripts\python.exe -m qslcard -c demo-config.json cards --out out/cards.pdf

# 6. 图形界面
.\.venv\Scripts\python.exe -m qslcard -c demo-config.json gui
~~~

安装为命令后也可以直接使用子命令：

~~~powershell
.\.venv\Scripts\python.exe -m pip install -e .
qslcard cards --template minimal --paper A4 --out out/cards.pdf
~~~

---

## 打包为 Windows 可执行文件（GUI）

程序以图形界面为主要使用方式，双击 exe 即可，不需要命令行。

~~~powershell
.\build_exe.ps1          # 生成图标 -> 跑测试 -> 打包 -> 自动自检
~~~

产物：

| 文件 | 说明 |
| --- | --- |
| dist\QSL卡片打印系统.exe | 图形界面的单文件程序，双击运行，无控制台窗口 |
| dist\qslcard-cli.exe | 命令行版本（同一核心），便于脚本化与排障 |

首次运行会在用户主目录下的 .qslcard 文件夹中自动创建数据目录、数据库、本机凭证库，
并把内置模板复制成可编辑的 JSON；随后弹出设置向导要求填写台站呼号。

GUI 提供：菜单栏（文件 / 操作 / 设置）、导入 ADIF 文件或整个目录、按呼号前缀/波段/模式筛选、
模板与纸张选择、双面与校准线开关、一键生成并打开 PDF、导出 CSV、隐私与凭证查看。

### GUI 中的三个易用性修正

| 位置 | 说明 |
| --- | --- |
| 设置 → **数据源与凭证…**（工具栏也有「数据源凭证」按钮） | 分页填写 QRZ.com（用户名/密码/Logbook API Key）、HamQTH、LoTW（TQSL 路径 + 证书口令）、Club Log（邮箱/应用密码/API Key）以及 eQSL/LoTW 报告路径。密钥写入本机凭证库（Windows DPAPI 加密），路径写入配置文件；默认掩码显示，可勾选「显示明文」，可单项清除或清空全部。拉取日志失败时也会提示到这里来填写。 |
| 设置 → 纸张 | 每个纸张选项后面都标注了毫米尺寸，例如 A4 (210 x 297 mm)、LETTER (215.9 x 279.4 mm)；主工具栏的纸张下拉与商业印刷面板同样如此。 |
| 设置 → QSL 经手 | 改为**多选**：卡片局 (Bureau)、直接邮寄 (Direct)、OQRS、LoTW、eQSL 可同时勾选。存储为规范化的逗号分隔值（如 BUREAU,DIRECT），写入 ADIF 的 QSL_VIA 后仍可被日志软件读取；卡片上会照原样印出该字符串。 |

证书口令等敏感值只经环境变量传给 TQSL 子进程，不进入命令行、日志或导出物。

无界面自检（用于验证打包结果，返回码 0 表示成功）：

~~~powershell
dist\QSL卡片打印系统.exe --selftest .\dist\selftest
~~~

自检会跑通「写入样例 ADIF -> 导入去重 -> 规则筛选 -> A4 双面拼版出片」，
并把过程写入 selftest-report.json（含导入条数、命中数、拼版描述、PDF 页数与字节数、警告列表）。

窗口化 exe 没有控制台，因此程序启动时若发现标准输出为空，会把输出重定向到
.qslcard\qslcard.log，崩溃信息也会写入该日志并在界面上提示。

---

## 命令行一览

| 命令 | 作用 |
| --- | --- |
| import PATH... | 导入 ADIF/ADX 文件或目录，策略可选 skip / merge / overwrite |
| sync [--source NAME] | 从在线数据源同步（qrz、hamqth、lotw、clublog、eqsl） |
| callbook --provider qrz,hamqth | 批量补全对方姓名、QTH、地址、网格 |
| cards --rule R --template T | 按规则筛选并生成拼版 PDF，可 --single-page、--mark-sent |
| export adif 或 csv --out F | 导出 ADIF（回合日志软件）或 CSV（邮件合并） |
| track --batch N --status sent 或 received | 登记发卡与回卡 |
| db --info 或 --optimize 或 --vacuum | 台账统计与数据库维护 |
| privacy [PATH...] | 隐私自检：扫描配置、备份、日志中是否出现凭证明文 |
| vault set 或 list 或 delete 或 clear 或 export 或 import | 本机凭证库管理（默认掩码显示） |
| templates / sources / config | 列出模板、数据源状态、查看配置 |

常用参数：--paper A4|LETTER|A3、--bleed-mm、--card-width-mm / --card-height-mm、
--flip x|y（双面翻转方向）、--no-crop-marks、--no-calibration、--core-font（不嵌入字体，便于检索文本）。

---

## 商业印刷参数与卡片设计器

### 商业印刷参数面板（设置 → 商业印刷参数…）

- 预设按钮：**家用打印（推荐）** 与 **商业印刷**，切换时用通俗文字列出差异，例如"颜色将转换为 CMYK，家用打印机上可能显得偏暗"。
- 图形化选项：文件类型（通用 PDF / PDF/X 风格）、色彩模式（RGB / CMYK）、分辨率（300/600/1200 DPI）、纸张、出血、双面。
- 标记开关：裁切线、套准标记、色彩条、3 mm 校准线。
- 高级参数默认折叠：卡片间距、纸张边距、标记长度、图片最低分辨率。
- 保存后写入配置文件，下次生成卡片立即生效。

两种预设的实际输出差异：

| 项目 | 家用打印 | 商业印刷 |
| --- | --- | --- |
| 文件类型 | 通用 PDF | PDF/X 风格（写入 TrimBox / BleedBox / ArtBox） |
| 色彩 | RGB（rg / RG 操作符） | 设备 CMYK（k / K 操作符） |
| 分辨率 | 300 DPI | 600 DPI |
| 裁切线 / 套准标记 / 色彩条 | 仅裁切线 | 全部开启 |
| 校准线 | 开 | 关（交付印刷用的成品文件） |

**诚实说明**：PDF/X-1a 认证还要求 ICC 输出意图，本程序尚未实现。导出的是"PDF/X 风格"文件：
PDF 1.3、字体全部嵌入、无透明、含成品框/出血框、CMYK 色彩。若印刷厂要求正式认证，
请在预检工具中补入该厂的 ICC 配置文件。

### 卡片设计器（设置 → 卡片设计器…）

- 画布按毫米缩放，显示出血框、成品线（虚线）与成品线内侧 3 mm 校准线（点线）。
- 元素类型：文字、图片、色块、线条、通联表格。
- 鼠标拖动移动，拖动 8 个控制点缩放；方向键微调 0.5 mm（按住 Shift 为 5 mm）；Delete 删除；双击文字直接编辑。
- 属性面板：X/Y/宽/高（mm）、字号、对齐、颜色选择器、加粗，以及占位符快捷插入按钮。
- 图片素材：浏览选择、画布内预览、一键"铺满整张卡（含出血）"；面板实时显示像素尺寸与**有效 DPI**，低于阈值时红色告警。
- 整面背景图：正面与背面各自可设，支持 cover / contain / stretch 三种适配方式。
- 模板保存为 JSON（可纳入版本控制），也可一键设为当前模板。
- 封面图片按目标比例只裁剪一次并在本次出片中复用，避免上千张卡片反复解码同一张大图。

---

## 硬件开销是怎么控制的

| 措施 | 说明 |
| --- | --- |
| 只依赖标准库 + fpdf2 | 无 GUI 框架、无科学计算栈；fpdf2 仅在真要出 PDF 时才导入，CLI/GUI 启动很快 |
| Tkinter 而非浏览器内核 | 图形界面内存占用几 MB 级，启动毫秒级，不额外安装运行时 |
| ADIF 流式解析 | 1 MiB 分块读取，内存与日志大小几乎无关（SR-ADIF-004） |
| SQLite 调优 | WAL + synchronous=NORMAL（每事务一次 fsync）、32 MB 页缓存、内存临时表、memory-mapped I/O |
| 批量事务与 executemany | 每 5000 条一个事务，10 万条日志只需几十次提交而不是 10 万次 |
| 批量去重预取 | 用临时表 JOIN 一次取回整批已存在记录，避免逐条 SELECT |
| WITHOUT ROWID 主键 | 业务键即主键，去重索引与主键合一，省一套索引与一次查找 |
| 行物化走位置构造 | 直接 QSO(*row) 而不是 45 次 setattr |
| 渲染复用 | 一个文档、每张纸一页；字号自适应做了记忆化，同款台站信息不重复测量 |
| 拼版求解走细粒度阶梯 | 用 0.5 mm 阶梯搜索边距与间距，找出每张纸最多卡片数，并提示被压缩的参数 |
| 对象轻量化 | 热路径数据类使用 __slots__ |

---

## 隐私与凭证（对应规格书第 18 章）

* 凭证只存本机：Windows 走 DPAPI（绑定当前用户），其他平台用 **纯标准库 ChaCha20 + HMAC-SHA256**
  保险库，由主口令解锁（PBKDF2 派生）。不存明文、不落日志、不进备份与导出。
* 只用本地调用：请求前统一经过域名白名单（见 qslcard/privacy.py 的 DEFAULT_ALLOWED_HOSTS），
  非白名单域名在打开套接字之前就被拒绝；配置项 offline 设为 true 可一键断网。
* LoTW 证书口令通过子进程环境变量传递，绝不进入命令行参数（SR-LOTW-010）。
* privacy 子命令会扫描配置、备份与日志，确认没有凭证明文；vault list 只显示掩码。

---

## 数据源现状

| 数据源 | 实现程度 | 说明 |
| --- | --- | --- |
| ADIF / ADX 文件 | 完整 | 流式解析，未知字段原样保留在 extra 中，可无损往返 |
| QRZ.com | 完整 | XML 订阅查询（会话 Key + 磁盘缓存）与 Logbook API（FETCH） |
| HamQTH | 完整 | 免费兜底 Callbook |
| LoTW | 按决策 D-01 实现 | 调用本地 TQSL CLI 上传/下载；也支持导入官网导出的 ADIF 报告 |
| Club Log | 完整（限实时接口） | realtime.php 上传，403 立即停止并提示更换凭证；支持 OQRS CSV 导入 |
| eQSL.cc | 导入路径 | 官方无通用 API（OQ-02 未决），支持导入 InBox 导出的 ADIF |

TQSL 的具体开关随版本变化，因此上传与下载命令模板放在配置项 lotw.upload_command 与
lotw.download_command 中，默认值为 tqsl -u -a {adi} 与 tqsl -d -a -o {adi}。

---

## 目录结构

~~~
qslcard/            程序包
  model.py          QSO 规范模型与去重键
  adif.py           ADIF/ADX 流式读写
  store.py          SQLite 台账（WAL、批量事务、去重）
  rules.py          制卡规则（SQL 快路径 + 派生条件）
  geometry.py       拼版几何：90x140 + 3 mm 出血、裁切线、校准线、双面镜像
  templates.py      模板模型、占位符引擎、3 套内置模板
  cards.py          QSO 分组为卡片
  pdfout.py         fpdf2 渲染与出片
  privacy.py        凭证本地化、白名单、ChaCha20、隐私自检
  net.py            带限流重试的传输与响应缓存
  sources/          六个连接器
  cli.py            命令行接口
  gui.py            Tkinter 图形界面（主要使用方式）
  desktop.py        桌面入口：无控制台安全处理、首次运行初始化、--selftest
app_main.py         打包入口（PyInstaller 使用）
qslcard.spec        PyInstaller 配置（GUI 与 CLI 两个单文件产物）
build_exe.ps1       一键构建脚本
scripts/make_icon.py 生成应用图标 assets/qslcard.ico
templates/          导出的模板 JSON（可自行编辑）
samples/            演示用 ADIF 数据
tests/              pytest 测试
docs/               需求规格说明书
~~~

---

## 测试与质量

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q      # 单元 + 端到端测试
ruff check qslcard tests                     # 静态检查
ruff format --check qslcard tests
mypy qslcard                                 # 可选
~~~

覆盖点包括：ChaCha20 对 RFC 8439 测试向量、ADIF 往返、去重与状态保留、
拼版张数（A4 4-up、Letter 出血后仅 2-up、A3 旋转后 8-up）、校准线坐标、
双面镜像、凭证库加解密与错误口令、白名单拒绝、TQSL 口令不外泄、
Club Log 403 停止，以及 **ADIF 落盘到可打印 PDF** 的完整链路。

---

## 日志拉取、QSL 回写与发卡记录表

### 从 QRZ Logbook 与 LoTW 拉取日志

- QRZ Logbook：走 Logbook API 的 FETCH，支持 MODSINCE 增量拉取（只取某日之后变更的记录），
  确认状态与 QSO 一起合并入库。
- LoTW：官方没有通用 API，因此通过本地 TQSL 命令行下载确认数据到临时 ADIF，再解析合并，
  等价于手工在 LoTW 网站导出一次。
- 图形界面：「拉取日志」按钮一次拉取两个来源；命令行：qslcard sync --source qrz --since 2026-09-01。

### 回写 QRZ Logbook 的 QSL Card Sent 日期

发卡登记时可勾选「同时回写 QRZ Logbook 的 QSL Card Sent 日期」：

- 使用 Logbook API 的 ACTION=INSERT，只发送呼号 / 日期 / 波段 / 模式与 QSL 状态字段
  （QSL_SENT、QSLSDATE、QSL_SENT_VIA；回卡时为 QSL_RCVD、QSLRDATE、QSL_RCVD_VIA）。
- 每条记录都按 QSO 主键匹配 QRZ 中已有的联络并更新其 QSL 状态，不会新建重复记录。
- 批量按 40 条一组发送；一旦返回 FAIL 立即停止并报错，因为 QRZ 会封锁反复提交失败请求的账号。
- 不发送地址、邮箱等个人信息；API Key 只保存在本机凭证库。

### 发卡记录表（回卡追踪）

新增 card_log 表记录每一次发卡与回卡，界面在「操作 → 发卡记录表…」，命令行在 qslcard cardlog：

| 能力 | 说明 |
| --- | --- |
| 快速登记发卡 | 生成卡片后立即弹窗询问，填日期与寄送方式即可；也可对当前筛选批量登记 |
| 回卡登记 | 双击一行即登记回卡，或选行后点「登记回卡」 |
| 退件 | 一键把选中行标记为退件，便于后续换地址重寄 |
| 统计 | 已发出 / 已回卡 / 待回卡 / 退件 / 回卡率 |
| 导出 | CSV（UTF-8 with BOM，Excel 直接打开） |
| 回写 QRZ | 对选中的行单独回写 QSL 状态 |

记录发卡时同时更新 QSO 的 ADIF 字段（QSL_SENT、QSLSDATE、QSL_SENT_VIA），
回卡时更新（QSL_RCVD、QSLRDATE、QSL_RCVD_VIA），因此导出的 ADIF 与卡片台账始终一致。
同一呼号重复发卡会保留历史行，不会被覆盖。

命令行示例：

~~~powershell
qslcard track --batch 2 --status sent --date 2026-09-15 --via BURO --push-qrz
qslcard track --call JA1AA --status received --date 2026-10-05 --via BURO
qslcard cardlog list --status sent --limit 50
qslcard cardlog stats
qslcard cardlog export --out qsl-card-log.csv
~~~

### 数据库自动升级

新增 QSLSDATE / QSLRDATE 与 card_log 表后，旧数据库在打开时会自动补齐缺失列并重建索引
（schema 版本升到 2），已有记录不会丢失；自检脚本会在一个旧版结构的数据库上验证这一点。

---

## 开发与构建

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt

.\.venv\Scripts\python.exe -m pytest -q                       # 测试
ruff check qslcard tests scripts app_main.py                    # 静态检查
ruff format --check qslcard tests scripts app_main.py

.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean qslcard.spec   # 打包 exe
~~~

或直接用一键脚本 .\build_exe.ps1（生成图标 -> 跑测试 -> 打包 -> 自检）。

持续集成见 .github/workflows/ci.yml：在 Ubuntu 与 Windows 上跑 ruff 与 pytest，
并在 Windows 上构建 exe 后执行无界面自检。

## 贡献与安全

* 参与开发请阅读 CONTRIBUTING.md。
* 安全问题请按 SECURITY.md 私下报告，不要开公开 issue。

## 许可证

本项目以 **MIT 许可证**发布，详见 LICENSE 文件。

Copyright (c) 2026 qslcard contributors

---

## 已知限制

* eQSL 自动下载未实现（官方无通用 API，见待确认问题 OQ-02）。
* 商业印刷的 PDF/X-1a 与 CMYK 在规格书中列为可选增强，当前默认输出通用 RGB PDF；
  GUI 已提供纸张与校准线的图形化入口，完整的商业印刷参数面板仍待补。
* 未内置 DXCC 实体表：可用 QSO 的 country 字段，或后续接入 cty.dat 做离线派生。
* 图片元素已支持渲染，但模板编辑器尚未提供图形化的图片与元素拖拽。
