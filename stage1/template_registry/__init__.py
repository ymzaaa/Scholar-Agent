"""双 OUC 当前模板的轻量注册与物化入口。"""

from .models import CompileRecipe, TemplateManifest, TemplateManifestError
from .registry import (
    ResolvedTemplate,
    TemplateRegistry,
    list_builtin_templates,
    load_builtin_template_registry,
    resolve_builtin_template,
    resolve_template,
)

__all__ = [
    "CompileRecipe", "ResolvedTemplate", "TemplateManifest",
    "TemplateManifestError", "TemplateRegistry", "list_builtin_templates",
    "load_builtin_template_registry", "resolve_builtin_template", "resolve_template",
]
