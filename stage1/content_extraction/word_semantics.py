# -*- coding: utf-8 -*-
"""Word脚注部件、域指令、书签和链接的确定性语义辅助。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from docx.oxml.ns import qn
from lxml import etree

from content_extraction.content_units import ExtractionIssue


##### 语义上下文板块 #####


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@dataclass
class SemanticContext:
    """跨 document.xml 和 footnotes.xml 的共享语义上下文。"""

    footnotes: dict[str, dict[str, Any]] = field(default_factory=dict)
    referenced_footnotes: set[str] = field(default_factory=set)
    bookmark_names: dict[str, list[str]] = field(default_factory=dict)


def _related_part(document: Any, suffix: str) -> Any | None:
    for relation in document.part.rels.values():
        if str(getattr(relation, "reltype", "")).endswith(suffix):
            return getattr(relation, "target_part", None)
    return None


def build_semantic_context(document: Any) -> SemanticContext:
    context = SemanticContext()
    footnotes_part = _related_part(document, "/footnotes")
    if footnotes_part is not None:
        root = etree.fromstring(footnotes_part.blob)
        for note in root.findall("./" + qn("w:footnote")):
            note_id = note.get(qn("w:id"))
            if note_id is not None and int(note_id) > 0:
                context.footnotes[note_id] = {
                    "element": note,
                    "document": SimpleNamespace(part=footnotes_part),
                }
    return context


##### 域指令板块 #####


SUPPORTED_FIELD_TYPES = {"REF", "PAGEREF", "HYPERLINK"}


def parse_field_instruction(instruction: str) -> dict[str, Any]:
    normalized = " ".join(instruction.split())
    type_match = re.match(r"^([A-Za-z]+)", normalized)
    field_type = type_match.group(1).upper() if type_match else "UNKNOWN"
    quoted = re.findall(r'"([^"]*)"', normalized)
    tokens = re.findall(r'"[^"]*"|\S+', normalized)
    target = None
    url = None
    if field_type in {"REF", "PAGEREF"} and len(tokens) > 1:
        target = tokens[1].strip('"')
    elif field_type == "HYPERLINK":
        local_match = re.search(r"\\l\s+(?:\"([^\"]+)\"|(\S+))", normalized)
        if local_match:
            target = next(value for value in local_match.groups() if value)
        elif quoted:
            url = quoted[0]
        elif len(tokens) > 1 and not tokens[1].startswith("\\"):
            url = tokens[1]
    return {
        "field_type": field_type,
        "instruction": instruction,
        "normalized_instruction": normalized,
        "target_bookmark": target,
        "url": url,
        "switches": re.findall(r"\\[A-Za-z@#*]+", normalized),
        "supported": field_type in SUPPORTED_FIELD_TYPES,
    }


def create_field_unit(
    *,
    factory: Any,
    parent: Any,
    source: dict[str, Any],
    instruction: str,
    result_text: str,
    locked: bool,
    dirty: bool,
    malformed: bool,
    nested: bool,
    issues: list[ExtractionIssue],
) -> Any:
    parsed = parse_field_instruction(instruction)
    supported = parsed["supported"] and not malformed and not nested
    if parsed["field_type"] == "HYPERLINK" and not parsed["target_bookmark"]:
        supported = supported and safe_external_url(parsed["url"])
    status = "extracted" if supported else "degraded"
    unit = factory.create(
        "field",
        text=result_text,
        source=source,
        properties={
            "field_type": parsed["field_type"],
            "locked": locked,
            "dirty": dirty,
            "malformed": malformed,
            "nested": nested,
        },
        relations={"parent_unit_id": parent.unit_id},
        payload=parsed,
        status=status,
    )
    if status != "extracted":
        code = "FIELD_STRUCTURE_MALFORMED" if malformed else "FIELD_SEMANTIC_UNSUPPORTED"
        issues.append(
            ExtractionIssue(
                code=code,
                message=(
                    f"Word域无法安全转换：type={parsed['field_type']}, "
                    f"nested={nested}"
                ),
                severity="error",
                unit_id=unit.unit_id,
                source=source,
            )
        )
    return unit


##### 链接与书签板块 #####


def safe_external_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme.lower() in {"http", "https", "mailto"}


def bookmark_label(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"word-bm-{digest}"


def resolve_hyperlink(node: Any, document: Any) -> dict[str, Any]:
    relationship_id = node.get(qn("r:id"))
    anchor = node.get(qn("w:anchor"))
    url = None
    if relationship_id and relationship_id in document.part.rels:
        url = document.part.rels[relationship_id].target_ref
    return {
        "relationship_id": relationship_id,
        "target_bookmark": anchor,
        "url": url,
        "safe_external": safe_external_url(url),
    }
