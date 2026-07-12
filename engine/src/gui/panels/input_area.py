# -*- coding: utf-8 -*-
"""``InputArea`` 兼容 shim。

历史实现曾位于本文件（1376 行单类）。已按职责拆分为同名的子包
``src.gui.panels.input_area``，分为：

  - ``__init__.py`` — 主 ``InputArea(QFrame)`` 类（组合各 Mixin）
  - ``_attachment_manager.py`` — 文件 / 图片附件
  - ``_model_tag.py`` — 模型 / 权限 / 模式 标签 + 索引状态
  - ``_text_handler.py`` — 文本输入 / 历史 / Slash 弹窗
  - ``_token_stats.py`` — token 统计
  - ``_toolbar.py`` — 工具栏按钮

注意：当同名包目录 ``input_area/`` 存在时，Python 导入系统优先加载
包目录的 ``__init__.py``，本 shim 仅作为旧路径的兜底再导出。
"""
from src.gui.panels.input_area import InputArea

__all__ = ["InputArea"]
