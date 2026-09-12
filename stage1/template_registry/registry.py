# -*- coding: utf-8 -*-
"""只加载 OUC 研究生和本科当前模板的小型注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapters.ouc import OUCTemplateAdapter
from adapters.ouc_bachelor import OUCBachelorTemplateAdapter

from .materialize import materialize_template
from .models import TemplateManifest, TemplateManifestError


##### 固定注册板块 #####


MANIFEST_PATHS = (
    "stage1/template/OUC研究生/TEMPLATE-MANIFEST.json",
    "stage1/template/OUC本科生/TEMPLATE-MANIFEST.json",
)
ADAPTER_FACTORIES = {
    "ouc-graduate": OUCTemplateAdapter,
    "ouc-bachelor": OUCBachelorTemplateAdapter,
}


class TemplateRegistry:
    def __init__(self, repository_root: str | Path) -> None:
        self.repository_root = Path(repository_root).resolve()
        manifests = [
            TemplateManifest.load(self.repository_root / path, self.repository_root)
            for path in MANIFEST_PATHS
        ]
        self._templates = {item.template_id: item for item in manifests}
        if set(self._templates) != set(ADAPTER_FACTORIES):
            raise TemplateManifestError("双 OUC Manifest 与适配器登记不一致。")

    def get(self, template_id: str) -> TemplateManifest:
        try:
            return self._templates[template_id]
        except KeyError as exc:
            raise TemplateManifestError(f"未注册模板：{template_id}") from exc

    def templates(self) -> list[TemplateManifest]:
        return [self._templates[key] for key in sorted(self._templates)]

    def source_root(self, manifest: TemplateManifest) -> Path:
        return self.repository_root.joinpath(*manifest.source_path.split("/")).resolve()


##### 运行上下文板块 #####


@dataclass(frozen=True, slots=True)
class ResolvedTemplate:
    manifest: TemplateManifest
    adapter: Any

    def materialize(self, repository_root: str | Path, destination: str | Path) -> Path:
        return materialize_template(self.manifest, repository_root, destination)


def resolve_template(registry: TemplateRegistry, template_id: str) -> ResolvedTemplate:
    manifest = registry.get(template_id)
    adapter = ADAPTER_FACTORIES[manifest.adapter_name]()
    adapter.bind_manifest(manifest)
    return ResolvedTemplate(manifest, adapter)


def load_builtin_template_registry(
    repository_root: str | Path | None = None,
) -> TemplateRegistry:
    root = Path(repository_root).resolve() if repository_root else Path(__file__).resolve().parents[2]
    return TemplateRegistry(root)


def resolve_builtin_template(
    template_id: str, *, repository_root: str | Path | None = None,
) -> ResolvedTemplate:
    registry = load_builtin_template_registry(repository_root)
    return resolve_template(registry, template_id)


def list_builtin_templates(
    *, repository_root: str | Path | None = None,
) -> list[ResolvedTemplate]:
    registry = load_builtin_template_registry(repository_root)
    return [resolve_template(registry, item.template_id) for item in registry.templates()]
