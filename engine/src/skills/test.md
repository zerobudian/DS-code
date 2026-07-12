---
name: test
description: 运行测试并分析失败原因，自动修复可修复的问题
command: /test
when: 用户要求运行测试、验证代码、修复测试失败时
arguments:
  - name: path
    description: 测试路径（文件或目录）
    required: false
    default: ""
  - name: fix
    description: 是否自动修复失败的测试
    required: false
    default: "true"
fork_context: false
tools_allowed:
  - run_command
  - run_command_stream
  - read_file
  - list_files
  - search_code
  - edit_file
  - quick_syntax_check
  - validate_file
---

# 测试流程

执行以下步骤运行测试并分析结果：

1. **发现测试**：
   - 如果指定了path，在该路径下查找测试文件
   - 如果未指定，先查看项目结构，查找常见的测试目录（tests/、test/等）
   - 识别测试框架（pytest、unittest、jest、go test等）

2. **运行测试**：
   - 使用 `run_command` 执行测试命令
   - 先运行快速语法检查：`quick_syntax_check`
   - 捕获完整输出，区分通过和失败的测试

3. **分析失败**：
   - 对每个失败的测试，阅读相关代码
   - 分析失败原因（断言失败、异常、导入错误等）
   - 定位问题代码位置

4. **修复（如果fix=true）**：
   - 使用 `edit_file` 修复问题
   - 重新运行测试验证修复
   - 最多修复3轮，避免无限循环

5. **输出总结**：
   - 通过/失败数量
   - 修复了哪些问题
   - 仍需人工处理的问题
