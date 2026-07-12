---
name: explain
description: 代码解释 - 解释指定代码或函数的工作原理
command: /explain
when: 用户不理解某段代码、需要解释实现原理时
arguments:
  - name: target
    description: 文件路径或符号名
    required: true
fork_context: false
tools_allowed:
  - read_file
  - find_symbol
  - analyze_file
  - find_imports
  - show_call_chain
  - code_metrics
  - search_code
---

# 代码解释流程

执行以下步骤解释代码：

1. **定位目标**：
   - 如果target是文件路径，使用 `read_file` 读取文件
   - 如果target是符号名（函数/类名），使用 `find_symbol` 查找定义位置
   - 如果找不到，使用 `search_code` 全文搜索

2. **分析代码结构**：
   - 整体功能：这段代码做什么
   - 输入输出：接受什么参数，返回什么
   - 关键算法：核心逻辑是什么
   - 依赖关系：调用了哪些函数，被谁调用（使用 `show_call_chain`）

3. **解释层次**：
   - 先用一句话概括
   - 然后分步骤解释关键逻辑
   - 标注需要注意的边界条件或陷阱
   - 如果有复杂算法，用简单语言解释原理

4. **输出格式**：
   - 使用清晰的分段和小标题
   - 关键代码片段用反引号标注
   - 避免过于专业的术语，必要时解释术语
