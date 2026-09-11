# 参与开发 / Contributing

感谢你愿意改进这个项目。以下是最小必要约定。

## 环境

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
~~~

## 提交前必须通过

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
ruff check qslcard tests scripts app_main.py
ruff format --check qslcard tests scripts app_main.py
~~~

## 约定

* **不改动别人的仓库**：本项目的工作区里可能并存其他检出，提交前请用 git status 确认没有误加入无关目录。
* **凭证永不落盘明文**：新增数据源时必须走 qslcard/privacy.py 的凭证库与域名白名单，
  Secret 只能通过环境变量传给子进程，不能进入命令行、日志或导出物。
* **测试要能无显示环境运行**：涉及 Tk 的测试请用 pytest.importorskip 并在无显示时跳过，
  对话框一律在测试里替换掉，避免挂起。
* **性能取向**：核心保持只依赖标准库 + fpdf2；新增重依赖需要有明确理由。
* 提交信息建议使用英文祈使句，一行说清做了什么。

## 数据源接口

新增一个数据源只需在 qslcard/sources/ 下实现 fetch / lookup / test / is_configured
并在 qslcard/sources/__init__.py 的 SOURCES 中登记，核心不需要改动。

---

English: see the sections above; keep credentials local, keep tests headless-safe,
and run ruff plus pytest before opening a pull request.
