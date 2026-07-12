// DScode V2 — 工具函数

/** 安全的 HTML 转义 */
function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/\n/g, '<br>');
}

/** 简单的 Markdown 渲染（代码块 + 行内代码 + 粗体） */
function renderMarkdown(text) {
  if (!text) return '';
  let result = escapeHtml(text);

  // 代码块 ```...```
  result = result.replace(/```(\w*)\n([\s\S]*?)```/g, (match, lang, code) => {
    return `<pre class="code-block"><code>${escapeHtml(code.trim())}</code></pre>`;
  });

  // 行内代码 `...`
  result = result.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');

  // 粗体 **...**
  result = result.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  return result;
}

/** 截断字符串 */
function truncate(str, maxLen = 200) {
  if (!str) return '';
  return str.length > maxLen ? str.slice(0, maxLen) + '...' : str;
}

/** 格式化工具名（双语） */
function formatToolName(name) {
  var map = {
    'read_file': '读取文件|Read File',
    'list_files': '列出文件|List Files',
    'create_file': '创建文件|Create File',
    'write_file': '写入文件|Write File',
    'modify_file': '修改文件|Modify File',
    'edit_file': '编辑文件|Edit File',
    'mark_delete': '标记删除|Mark Delete',
    'delete_file': '删除文件|Delete File',
    'run_command': '执行命令|Run Command',
    'web_search': '网页搜索|Web Search',
    'web_fetch': '抓取网页|Web Fetch',
    'git_status': 'Git 状态|Git Status',
    'git_diff': 'Git 差异|Git Diff',
    'git_commit': 'Git 提交|Git Commit',
  };
  var entry = map[name] || name;
  var parts = entry.split('|');
  return (I18N.current === 'en') ? (parts[1] || name) : (parts[0] || name);
}
