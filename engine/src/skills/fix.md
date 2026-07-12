---
name: fix
description: 自动修复错误：运行测试/检查类型，发现错误自动修复，循环直到通过或达到最大次数
arguments:
  - name: max_attempts
    required: false
    default: 5
    description: 最大修复尝试次数
  - name: command
    required: false
    default: ""
    description: 指定运行的命令（默认自动检测pytest/npm test等）
  - name: target
    required: false
    default: ""
    description: 只修复指定文件或测试
---

# /fix — 自动修复循环

你现在进入**自动修复模式**。请严格按以下步骤执行，直到问题解决或达到最大尝试次数（{{max_attempts}}次）。

## 执行流程

**Step 1: 检测并运行测试/检查命令**
{% if command %}
运行用户指定命令: `{{command}}`
{% else %}
按优先级自动检测并运行：
1. 如果有 pytest 配置（pytest.ini/pyproject.toml 中 [tool.pytest]），运行 `pytest -x --tb=short {{target}}`
2. 如果是 Python 项目，运行 `python -m pytest -x --tb=short {{target}}` 或 `python -m py_compile <主要py文件>`
3. 如果有 package.json 且包含 test 脚本，运行 `npm test`
4. 尝试 `cargo check`（Rust）或 `go build ./...`（Go）
5. 如果都没有，运行 `get_diagnostics` 获取LSP诊断
{% endif %}

**Step 2: 分析结果**
- 如果命令退出码为 0（成功）：输出 "✓ 所有检查通过！" 并结束
- 如果失败：仔细阅读错误信息，找到根本原因

**Step 3: 修复代码**
- 精确定位错误位置（使用 goto_definition / find_references / document_symbols）
- 一次只修复一个问题（最小改动）
- 修改后记录修改了什么

**Step 4: 重新运行命令并判断**
- 回到 Step 1 重新运行
- 如果同样的错误连续出现 2 次，换一种修复思路
- 达到 {{max_attempts}} 次仍失败：停止并报告剩余问题

## 安全约束
- 只修复测试/类型/语法错误，不做重构
- 不要删除现有功能
- 每次修复后必须重新运行验证
- 如果发现错误超出代码范围（如依赖缺失、环境问题），告知用户而非无限尝试

开始执行自动修复。
