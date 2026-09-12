# -*- coding: utf-8 -*-
"""模板必需语言插槽的通用数据契约。

核心层只理解插槽语义和完整文件编辑，不理解任一模板的 LaTeX 宏、
目录布局或参数顺序。插槽的发现与更新必须由已注册模板适配器完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


##### 插槽模型板块 #####


@dataclass(frozen=True, slots=True)
class TemplateLanguageSlot:
    """一个由模板适配器定位、允许补充目标语言的既有插槽。"""

    slot_id: str
    category: str
    kind: str
    source_text: str
    current_text: str
    relative_path: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TemplateLanguageEdit:
    """适配器针对一个 TeX 文件生成的完整、可审计文本替换。"""

    relative_path: str
    old_text: str
    new_text: str

