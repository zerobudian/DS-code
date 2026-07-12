---
name: release
description: 准备发布 - 更新版本号、生成changelog、创建发布提交
command: /release
when: 用户准备发布新版本、生成changelog时
arguments:
  - name: version
    description: 新版本号（如1.0.0）
    required: true
  - name: message
    description: 发布说明
    required: false
    default: ""
fork_context: false
tools_allowed:
  - git_status
  - git_log
  - git_diff
  - read_file
  - modify_file
  - edit_file
  - run_command
tools_forbidden:
  - delete_file
  - git_reset
  - git_checkout
---

# 发布准备流程

执行以下步骤准备发布：

1. **检查工作区状态**：
   - 使用 `git_status` 确保没有未提交的更改
   - 如果有未提交更改，提示用户先提交或stash

2. **查看历史提交**：
   - 使用 `git_log` 查看自上次tag以来的提交
   - 分析提交内容，分类为：新功能、bug修复、性能优化、文档更新、其他

3. **更新版本号**：
   - 查找项目中的版本文件（package.json、pyproject.toml、setup.py、__init__.py等）
   - 使用 `edit_file` 更新版本号为指定版本

4. **更新Changelog**：
   - 查找CHANGELOG.md或HISTORY.md
   - 添加新版本条目，包含分类后的提交摘要
   - 如果没有changelog文件，创建CHANGELOG.md

5. **验证**：
   - 如果项目有测试，运行测试确保一切正常
   - 检查版本号是否正确更新

6. **创建发布提交**：
   - 展示所有更改摘要
   - 提示用户确认后执行 `git_add` 和 `git_commit`
   - commit message格式: `chore: release v{version}`

注意：不要自动执行 git push 或 git tag，等待用户确认。
