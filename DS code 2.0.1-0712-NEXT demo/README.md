# DS Code v2.0.1-0712-NEXT

> AI 编程助手 · Electron + Python Sidecar · DeepSeek V4 API

## 快速启动 / Quick Start

```bash
cd "DS code 2.0.1-0712-NEXT demo"
./start.sh
```

首次启动会自动下载 Electron，之后秒开。

## 目录结构 / File Structure

```
├── start.sh              # 一键启动脚本
├── stop.sh               # 停止脚本
├── sidecar/              # Python Sidecar (纯 stdlib HTTP/SSE)
│   ├── sidecar.py        # 主程序 (端口 8765)
│   ├── sidecar_engine.py # 引擎封装 (备用)
│   └── sidecar_api.py    # FastAPI 版本 (备用)
├── renderer/             # 前端界面
│   ├── index.html        # 主页面
│   ├── js/
│   │   ├── app.js        # 主逻辑 (向导/聊天/设置)
│   │   ├── i18n.js       # 中英文国际化
│   │   └── utils.js      # Markdown 渲染 / 工具函数
│   └── css/
│       ├── theme.css     # 设计令牌 + 深色模式
│       └── app.css       # 布局 + 组件样式
├── engine/src/           # AI 引擎
│   ├── core/             # ChatEngine / DeepSeek API / Config / Hooks
│   ├── tools/            # 91 内置工具
│   ├── sandbox/          # 项目隔离 / 历史记录
│   ├── prompts/          # 系统提示词
│   └── agents/           # 子代理
├── electron/             # Electron 窗口
├── data/                 # 配置和数据
├── assets/               # 图标/图片
└── node_modules/         # 依赖 (npm install 后)
```

## 功能 / Features

### 对话 / Chat
- DeepSeek V4 Pro / Flash 流式对话
- Thinking 模式：实时推理链输出
- 对话历史自动保存与恢复

### 引导页 / Onboarding Wizard
- 选择服务提供商：DeepSeek 官方 / 自定义供应商
- 自定义供应商支持：Base URL + 模型配置 (最多 2 个)
- 1M Context 开关 per model

### 工具调用 / Tool Calls
- 91 个内置工具 (文件操作 / 命令执行 / Git / 搜索 / MCP / …)
- 原生 DeepSeek V4 function calling (`tools` 参数)
- 文本提取 fallback 兼容旧格式

### UI
- `/` 斜杠命令菜单
- 中英文双语 (实时切换)
- 深色/浅色主题
- 项目侧边栏 (创建/切换/删除)
- 待办事项、文件管理面板

### API 端点 / API Endpoints
| 端点 | 说明 |
|---|---|
| `GET /api/health` | 健康检查 |
| `GET /api/config` | 读取配置 |
| `PUT /api/config` | 更新配置 |
| `GET /api/projects` | 列出项目 |
| `POST /api/projects` | 创建项目 |
| `DELETE /api/projects/{name}` | 删除项目 |
| `GET /api/chat/stream` | SSE 流式对话 |
| `POST /api/chat/stop` | 停止对话 |
| `GET /api/balance` | 查询余额 (http.client) |
| `GET /api/models` | 列出模型 |

## 配置 / Configuration

首次启动会弹出引导页。配置加密存储在 `~/dscode_data/config.enc` (Fernet)。

## 技术栈 / Tech Stack

| 层 | 技术 |
|---|---|
| 前端 | HTML/CSS/JS (vanilla, zero framework) |
| 后端 | Python 3.9+ asyncio stdlib (zero pip install) |
| 引擎 | OpenAI SDK + http.client |
| 流式 | Server-Sent Events (SSE) |
| 配置 | Fernet 加密存储 |

## License

Apache-2.0
