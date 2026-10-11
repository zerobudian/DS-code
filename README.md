# DS Code v2.0.1-0712-NEXT

AI 编程助手：Electron 界面、Python Sidecar、ChatEngine 与 OpenAI 兼容 API。

## 安装与启动

需要 Node.js 22+、npm、Python 3.10+。从仓库根目录运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm ci
./start.sh
```

macOS/Linux 使用 `./start.sh`；Windows 安装依赖后运行 `npm start`，虚拟环境 Python 位于 `.venv/Scripts/python.exe`。`requirements.txt` 定义依赖范围，`requirements.lock` 保存本次验证的精确版本。

MCP、图片处理与旧 Qt 界面需要可选运行依赖：`.venv/bin/python -m pip install -r requirements-optional.lock`。开发和完整验证使用 `requirements-dev.txt`，其中包含可选依赖、Ruff 和 coverage。Ubuntu 上 Qt 还需要 `libegl1`、`libopengl0` 系统库。

Electron 负责启动一个 Sidecar、检查 HTTP 健康状态、随后加载界面；关闭窗口时只清理自己创建的进程。端口已占用会报告启动失败。`npm run dev` 打开开发工具，不会另起一个 Sidecar。旧 demo 目录中的启动脚本会转发到根目录。

可通过 `DSCODE_PYTHON` 选择 Python、`DSCODE_SIDECAR_PORT` 选择端口、`DSCODE_DATA_DIR` 指定用户数据目录。没有覆盖时使用 platformdirs 的系统用户数据目录。配置写入该目录的 `config.json`，敏感字段使用 Fernet 加密；数据不保存在仓库。

## 结构

```text
 electron/                   桌面窗口、受限 preload、Sidecar 进程管理
 renderer/                   HTML/CSS/JS、聊天客户端、异步确认对话框
 sidecar/http_protocol.py    HTTP 请求边界、JSON 与 SSE 编码
 sidecar/runtime.py          单引擎请求管理、事件隔离、取消与确认
 sidecar/service.py          配置、项目、历史与聊天 API
 sidecar/project_data.py     持久任务、文件预览与附件上传
 sidecar/providers.py        供应商添加、切换、删除与脱敏
 sidecar/sidecar.py          本地服务器入口
 engine/src/                 引擎、工具、沙箱、记忆、旧 Qt 界面
 tests/                      本地回归与模拟供应商集成测试
 docs/DELIVERY.md             源码依据、故障证据、修复及验证范围
```

## API 开发

独立调试可运行 `python3 -m sidecar.sidecar --port 8765`。只监听 `127.0.0.1`。桌面模式自动生成本次进程的认证令牌；独立调试可通过 `DSCODE_SIDECAR_TOKEN` 配置令牌。设置令牌后 JSON API 需要 `Authorization: Bearer <token>`，EventSource 流需要认证头或 `token` 参数。API 不接受任意网站 Origin。

| 端点 | 行为 |
| --- | --- |
| `GET /api/health` | 服务身份与健康状态 |
| `GET/PUT /api/config` | 读取脱敏配置 / 校验后原子保存 |
| `GET/POST /api/projects` | 项目列表 / 创建并返回规范名称 |
| `GET /api/projects/{name}/history` | 最新持久化对话 |
| `PUT /api/projects/{name}` | 保存 `pinned` 或 `display_name` |
| `GET/PUT /api/projects/{name}/tasks` | 读取 / 保存任务；PUT 包含读取时的 `revision`，冲突返回 409 |
| `GET /api/projects/{name}/files` | work 与 preview 文件列表 |
| `GET /api/projects/{name}/file?path=...` | 安全文本或图片预览 |
| `POST /api/projects/{name}/uploads` | `{name,data}`，data 为文件真实内容的 base64 |
| `DELETE /api/projects/{name}` | 删除项目；处理中返回 409 |
| `GET/POST /api/providers` | 脱敏供应商列表 / 添加供应商 |
| `PUT/DELETE /api/providers/{index}` | `{active:true}` 切换 / 删除供应商；处理中返回 409 |
| `GET /api/models` | 自定义模型配置或供应商模型列表 |
| `GET /api/balance` | 供应商余额；区分货币 |
| `POST /api/chat/send` | `{project,text,attachments?:[上传返回的名称]}` 创建请求，返回 `run_id` |
| `GET /api/chat/stream?run_id=...` | 接收该请求的 SSE，不重复启动模型 |
| `POST /api/chat/stop` | `{run_id}` 取消指定请求 |
| `POST /api/chat/confirm` | `{run_id,request_id,kind,value}` 回复命令/编辑/问答确认 |
| `POST /api/chat/plan` | `{project,confirmed}` 接受或拒绝待执行计划 |

保留旧的 `GET /api/chat/stream?project=...&text=...` 兼容入口。一个引擎同时处理一个请求；重复请求返回 409，一个流只允许一个接收端。断线、超时会取消该请求；事件缓冲有上限。终止事件是 `done` 或 `srv_error`。

Todo 和 Pin 持久化到项目数据；Todo 使用 revision 防止旧页面覆盖新任务。文件对话框支持文本和 PNG/JPEG/GIF/WebP 预览，上传每个文件不超过 512 KiB。每次对话最多 8 个附件、合计 2 MiB；UTF-8 文本内容与图片会进入模型请求。PDF、DOCX 和其他二进制附件会明确报错。

设置中的供应商高级选项可以指定 `thinking`、`reasoning_effort`、原生工具、usage 和 vision 能力；通用供应商默认不会收到 DeepSeek 专用参数。`context_tokens` 设置请求预算（1024–1048576），按保守估算删除较早的完整对话组，保留工具调用与结果的配对，不修改落盘历史。它不是供应商实际窗口大小的保证；`context_1m` 仅为模型元数据。自动重试只发生在输出前，已收到部分回复后出错会保留该回复并报告错误。

## 验证

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
PYTHONPATH=engine .venv/bin/python -m unittest discover -s tests -v
npm test
.venv/bin/python -m ruff check engine sidecar tests
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/qt_smoke.py
```

有桌面显示环境时还可运行 `.venv/bin/python tests/electron_smoke.py`；Linux 无显示时使用 `xvfb-run -a .venv/bin/python tests/electron_smoke.py`。它使用本地模拟供应商打开真实 Electron 窗口，验证 IPC、认证、项目创建、Todo/Pin 持久化、原生工具、文件预览、真实附件、上下文设置、流式代码块、刷新后历史与工具卡片恢复，以及退出后的 Sidecar 清理；截图保存在 `test-results/electron.png`。

GitHub Actions 执行完整依赖安装、Python/JavaScript 回归、Qt/Electron 冒烟、Ruff、语法检查及覆盖率采集。集成测试运行真实 Sidecar、ChatEngine 和 OpenAI SDK，并连接本地模拟供应商，不调用收费 API。

模块审计、复现证据和验证限制见 [交付报告](docs/DELIVERY.md)。Qt 界面不是当前 Electron 启动入口。

Apache-2.0，见 LICENSE。
