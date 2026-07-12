---
name: review
description: 代码审查 - 审查当前更改或指定文件的代码质量
command: /review
when: 用户要求审查代码、检查bug、分析代码质量时
arguments:
  - name: target
    description: 审查目标（文件路径或"changes"审查未提交更改）
    required: false
    default: changes
fork_context: false
tools_allowed:
  - git_status
  - git_diff
  - read_file
  - list_files
  - search_code
  - analyze_file
  - find_imports
  - show_dependencies
  - code_metrics
---

# 代码审查流程

执行以下步骤进行代码审查：

1. **确定审查范围**：
   - 如果target为"changes"或未指定，使用 `git_diff` 查看未提交的更改
   - 如果指定了文件路径，使用 `read_file` 读取该文件

2. **审查维度**：
   - 代码正确性：是否有明显的bug、逻辑错误、边界条件未处理
   - 安全性：是否有注入风险、路径穿越、敏感信息泄露
   - 性能：是否有明显的性能问题（如O(n^2)循环、不必要的IO）
   - 可读性：命名是否清晰、注释是否充分
   - 最佳实践：是否符合语言惯用法

3. **输出格式**：
   - 按严重程度分类：Critical（必须修复）、Warning（建议修复）、Suggestion（可选优化）
   - 每个问题给出：位置、问题描述、修复建议
