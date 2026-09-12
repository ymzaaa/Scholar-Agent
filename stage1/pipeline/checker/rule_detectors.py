# -*- coding: utf-8 -*-
"""G5 确定性格式检测器；不负责选择模板或决定发布。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pipeline.checker.rule_models import RuleDefinition


##### 检测上下文板块 #####


@dataclass
class DetectorOutcome:
    status: str
    detail: str
    evidence: list[dict]
    occurrence_count: int | None = None

    def __post_init__(self):
        if self.occurrence_count is None:
            self.occurrence_count = sum(item.get('origin') != 'template_baseline' for item in self.evidence)
        self.evidence = self.evidence[:20]


class CheckContext:
    def __init__(self, tex_dir: str | Path) -> None:
        self.tex_dir = Path(tex_dir).resolve()
        self._text_cache: dict[Path, str] = {}

    def paths(self, patterns: list[str] | None = None) -> list[Path]:
        patterns = patterns or ["**/*.tex"]
        found = {
            path.resolve()
            for pattern in patterns
            for path in self.tex_dir.glob(pattern)
            if path.is_file()
        }
        return sorted(found)

    def text(self, path: Path) -> str:
        if path not in self._text_cache:
            self._text_cache[path] = path.read_text(encoding="utf-8", errors="replace")
        return self._text_cache[path]

    def relative(self, path: Path) -> str:
        return path.relative_to(self.tex_dir).as_posix()


##### 文本扫描板块 #####


def _strip_comments(text: str) -> str:
    return re.sub(r"(?m)(?<!\\)%.*$", "", text)


def _matches(context: CheckContext, rule: RuleDefinition) -> list[dict]:
    config = rule.config
    pattern = re.compile(config["pattern"], re.MULTILINE | re.DOTALL)
    evidence = []
    for path in context.paths(config.get("paths")):
        text = context.text(path)
        if config.get("strip_comments", True):
            text = _strip_comments(text)
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            evidence.append(
                {
                    "file": context.relative(path),
                    "line": line,
                    "excerpt": match.group(0)[:160],
                }
            )
    return evidence


def detect_forbid_regex(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome:
    evidence = _matches(context, rule)
    return DetectorOutcome(
        "fail" if evidence else "pass",
        f"发现 {len(evidence)} 处禁止模式。" if evidence else "未发现禁止模式。",
        evidence,
    )


def detect_cite_in_math(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome:
    """按成对数学定界符检查引用，避免从结束 `$` 跨段误配。"""
    evidence = []
    environments = ("equation", "align", "align*", "gather", "multline")
    environment_pattern = re.compile(
        rf"\\begin\{{({'|'.join(map(re.escape, environments))})\}}"
        rf"(.*?)\\end\{{\1\}}",
        re.DOTALL,
    )
    delimiter_pattern = re.compile(r"(?<!\\)(\$\$|\$)")
    cite_pattern = re.compile(r"\\cite\{[^}]+\}")
    for path in context.paths(rule.config.get("paths")):
        text = _strip_comments(context.text(path))
        for match in environment_pattern.finditer(text):
            cite = cite_pattern.search(match.group(2))
            if cite:
                evidence.append(
                    {
                        "file": context.relative(path),
                        "line": text.count("\n", 0, match.start()) + 1,
                        "excerpt": match.group(0)[:200],
                    }
                )
        open_delimiter = None
        content_start = None
        for match in delimiter_pattern.finditer(text):
            delimiter = match.group(1)
            if open_delimiter is None:
                open_delimiter = delimiter
                content_start = match.end()
                continue
            if delimiter != open_delimiter:
                continue
            content = text[content_start:match.start()]
            if cite_pattern.search(content):
                evidence.append(
                    {
                        "file": context.relative(path),
                        "line": text.count("\n", 0, content_start) + 1,
                        "excerpt": content[:200],
                    }
                )
            open_delimiter = None
            content_start = None
    return DetectorOutcome(
        "fail" if evidence else "pass",
        f"发现 {len(evidence)} 处数学模式引用。" if evidence else "数学模式内未发现引用命令。",
        evidence,
    )


def detect_require_regex(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome:
    paths = context.paths(rule.config.get("paths"))
    if not paths:
        return DetectorOutcome("fail", "要求检查的文件不存在。", [])
    combined = "\n".join(_strip_comments(context.text(path)) for path in paths)
    applicability_pattern = rule.config.get("applicability_pattern")
    if applicability_pattern and not re.search(
        applicability_pattern, combined, re.MULTILINE | re.DOTALL
    ):
        return DetectorOutcome("not_applicable", "本次产物不包含该规则适用对象。", [])
    missing = [
        pattern
        for pattern in rule.config.get("patterns", [])
        if not re.search(pattern, combined, re.MULTILINE | re.DOTALL)
    ]
    missing_literals = [
        value
        for value in rule.config.get("literal_patterns", [])
        if value not in combined
    ]
    evidence = [
        *({"missing_pattern": pattern} for pattern in missing),
        *({"missing_literal": value} for value in missing_literals),
    ]
    return DetectorOutcome(
        "fail" if evidence else "pass",
        f"缺少 {len(evidence)} 个必要模式。" if evidence else "必要模板声明完整。",
        evidence,
    )


##### 引用与参考文献板块 #####


def detect_bib_institution(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome:
    bib_path = context.tex_dir / "cite.bib"
    word_references = context.tex_dir / "data" / "references.tex"
    if not bib_path.is_file() and word_references.is_file():
        return DetectorOutcome("not_applicable", "本次使用 Word 原始参考文献列表。", [])
    if not bib_path.is_file():
        citation_pattern = re.compile(
            r"\\(?:cite|parencite|textcite)\*?(?:\[[^]]*\])?\{[^{}]+\}"
            r"|\\bibliography\{[^{}]+\}|\\addbibresource\{[^{}]+\}"
            r"|\\printbibliography\b|\\input\{[^{}]*references[^{}]*\}",
            re.IGNORECASE,
        )
        evidence = []
        for path in context.paths(["**/*.tex"]):
            text = _strip_comments(context.text(path))
            match = citation_pattern.search(text)
            if match:
                evidence.append({
                    "file": context.relative(path),
                    "line": text.count("\n", 0, match.start()) + 1,
                    "excerpt": match.group(0)[:160],
                })
        if not evidence:
            return DetectorOutcome(
                "not_applicable", "正文没有引用或参考文献后端，本规则不适用。", [],
            )
        return DetectorOutcome(
            "fail", "检测到引用或参考文献命令，但不存在可用文献后端。", evidence,
        )
    bib = context.text(bib_path)
    violations = []
    for match in re.finditer(r"author\s*=\s*\{(.+?)\}\s*,", bib, re.DOTALL | re.I):
        author = match.group(1)
        if re.search(r"University|Department|Institute|College|School|Hospital", author, re.I):
            if "," in author and not re.fullmatch(r"\{.*\}", author.strip(), re.DOTALL):
                violations.append({"file": "cite.bib", "excerpt": author[:160]})
    return DetectorOutcome(
        "fail" if violations else "pass",
        "机构作者需使用额外花括号保护。" if violations else "BibTeX 机构作者格式可接受。",
        violations,
    )


##### 编译日志板块 #####


def _classify_known_baseline_evidence(
    evidence: list[dict], baseline_patterns: list[dict],
) -> tuple[list[dict], list[dict]]:
    """按模板画像中的冻结签名区分模板固有警告和本次新增警告。"""
    compiled = []
    for index, item in enumerate(baseline_patterns):
        pattern = item.get("pattern")
        maximum = item.get("max_count", 1)
        if not isinstance(pattern, str) or not isinstance(maximum, int) or maximum < 1:
            raise ValueError(f"known_baseline_patterns[{index}] 配置无效。")
        compiled.append({
            "pattern": re.compile(pattern, re.IGNORECASE),
            "max_count": maximum,
            "used": 0,
            "baseline_id": item.get("baseline_id", f"baseline-{index + 1}"),
        })

    known: list[dict] = []
    current: list[dict] = []
    for raw in evidence:
        classified = dict(raw)
        for signature in compiled:
            if (
                signature["used"] < signature["max_count"]
                and signature["pattern"].search(raw.get("excerpt", ""))
            ):
                signature["used"] += 1
                classified.update({
                    "origin": "template_baseline",
                    "baseline_id": signature["baseline_id"],
                })
                known.append(classified)
                break
        else:
            classified["origin"] = "current_run"
            current.append(classified)
    return known, current


def detect_log_pattern(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome:
    log_path = context.tex_dir / rule.config.get("log_file", "main.log")
    if not log_path.is_file():
        return DetectorOutcome("fail", f"编译日志不存在：{log_path.name}", [])
    pattern = re.compile(rule.config["pattern"], re.MULTILINE | re.IGNORECASE)
    evidence = []
    for match in pattern.finditer(context.text(log_path)):
        evidence.append(
            {
                "file": log_path.name,
                "line": context.text(log_path).count("\n", 0, match.start()) + 1,
                "excerpt": match.group(0)[:200],
            }
        )

    baseline_patterns = rule.config.get("known_baseline_patterns", [])
    if evidence and baseline_patterns:
        known, current = _classify_known_baseline_evidence(
            evidence, baseline_patterns,
        )
        evidence = [*known, *current]
        if not current:
            return DetectorOutcome(
                "pass",
                f"发现 {len(known)} 项，均与当前冻结模板的注册基线一致。",
                evidence,
            )
        return DetectorOutcome(
            "fail",
            f"发现 {len(current)} 项本次新增警告；另有 {len(known)} 项模板基线警告。",
            evidence,
        )
    return DetectorOutcome(
        "fail" if evidence else "pass",
        f"编译日志发现 {len(evidence)} 项。" if evidence else "编译日志未发现目标警告。",
        evidence,
    )


##### PDF检查板块 #####


def detect_pdf_page_size(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome:
    pdf_path = context.tex_dir / rule.config.get("pdf_file", "main.pdf")
    if not pdf_path.is_file():
        return DetectorOutcome("fail", "PDF 不存在，无法检查页面尺寸。", [])
    try:
        from pypdf import PdfReader
    except ImportError:
        return DetectorOutcome("not_implemented", "缺少 pypdf，无法读取 PDF 页面尺寸。", [])
    try:
        reader = PdfReader(str(pdf_path))
        expected = rule.config["size_points"]
        tolerance = float(rule.config.get("tolerance_points", 2.0))
        wrong = []
        for index, page in enumerate(reader.pages, start=1):
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            portrait = sorted((width, height))
            target = sorted((float(expected[0]), float(expected[1])))
            if any(abs(actual - wanted) > tolerance for actual, wanted in zip(portrait, target)):
                wrong.append({"page": index, "width": width, "height": height})
        return DetectorOutcome(
            "fail" if wrong else "pass",
            f"{len(wrong)} 页尺寸不符合模板画像。" if wrong else f"{len(reader.pages)} 页尺寸符合模板画像。",
            wrong,
        )
    except Exception as exc:
        return DetectorOutcome("internal_error", f"PDF 页面尺寸读取失败：{type(exc).__name__}", [])


##### 表格检查板块 #####


def detect_table_alignment(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome:
    evidence = []
    column_pattern = re.compile(r"\\begin\{(?:tabular|longtable)\}\{([^}]*(?:\{[^}]*\}[^}]*)*)\}")
    for path in context.paths(rule.config.get("paths", ["contents/*.tex", "includes/*.tex"])):
        for match in column_pattern.finditer(_strip_comments(context.text(path))):
            columns = match.group(1)
            unsafe = re.search(r"(?<![A-Za-z])(?:l|r)(?![A-Za-z])|(?<!centering)p\{", columns)
            if unsafe:
                evidence.append({"file": context.relative(path), "columns": columns[:160]})
    return DetectorOutcome(
        "fail" if evidence else "pass",
        f"发现 {len(evidence)} 个非居中列定义。" if evidence else "表格列定义未发现非居中对齐。",
        evidence,
    )


def detect_bilingual_caption(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome:
    """模板要求双语时，拒绝空英文、中文复制和明显仍为中文的英文栏。"""
    kind = rule.config.get("kind")
    if kind == "figure":
        pattern = re.compile(
            r"\\figurecaption\{([^}]+)\}\{([^}]+)\}\{([^}]*)\}"
        )
        indexes = (2, 3)
    elif kind == "table":
        pattern = re.compile(r"\\tablecaption\{([^}]+)\}\{([^}]*)\}")
        indexes = (1, 2)
    else:
        return DetectorOutcome("internal_error", "双语题注检测器缺少有效 kind。", [])
    found = violations = 0
    evidence = []
    for path in context.paths(rule.config.get("paths", ["contents/*.tex"])):
        text = _strip_comments(context.text(path))
        for match in pattern.finditer(text):
            found += 1
            chinese, english = match.group(indexes[0]).strip(), match.group(indexes[1]).strip()
            visible = re.sub(r"\s+", "", english)
            cjk = len(re.findall(r"[\u3400-\u9fff]", visible))
            valid = (
                bool(visible) and english != chinese
                and bool(re.search(r"[A-Za-z]", visible))
                and cjk <= max(1, len(visible) // 5)
            )
            if not valid:
                violations += 1
                evidence.append({
                    "file": context.relative(path),
                    "line": text.count("\n", 0, match.start()) + 1,
                    "reason": "missing_or_non_english_caption",
                })
    if not found:
        return DetectorOutcome("not_applicable", "本次产物不含对应题注。", [])
    return DetectorOutcome(
        "fail" if violations else "pass",
        f"发现 {violations} 个无效英文题注。" if violations else f"{found} 个英文题注有效。",
        evidence,
    )


def detect_english_headings(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome:
    """按模板配置的英文目录命令检查缺失、中文复制和明显非英文结果。"""
    commands = rule.config.get("commands", [])
    if not commands:
        return DetectorOutcome("internal_error", "英文目录检测器缺少命令配置。", [])
    pattern = re.compile(
        r"\\(?:" + "|".join(re.escape(item) for item in commands) + r")\{([^}]*)\}"
    )
    found = violations = 0
    evidence = []
    for path in context.paths(rule.config.get("paths", ["contents/*.tex"])):
        text = _strip_comments(context.text(path))
        for match in pattern.finditer(text):
            found += 1
            english = match.group(1).strip()
            visible = re.sub(r"\s+", "", english)
            cjk = len(re.findall(r"[\u3400-\u9fff]", visible))
            valid = (
                bool(re.search(r"[A-Za-z]", visible))
                and cjk <= max(1, len(visible) // 5)
            )
            if not valid:
                violations += 1
                evidence.append({
                    "file": context.relative(path),
                    "line": text.count("\n", 0, match.start()) + 1,
                    "reason": "missing_or_non_english_heading",
                })
    if not found:
        return DetectorOutcome("not_applicable", "本次产物不含英文目录标题。", [])
    return DetectorOutcome(
        "fail" if violations else "pass",
        f"发现 {violations} 个无效英文目录标题。"
        if violations else f"{found} 个英文目录标题有效。",
        evidence,
    )


##### 调度板块 #####


DETECTORS: dict[str, Callable[[CheckContext, RuleDefinition], DetectorOutcome]] = {
    "forbid_regex": detect_forbid_regex,
    "cite_in_math": detect_cite_in_math,
    "require_regex": detect_require_regex,
    "bib_institution": detect_bib_institution,
    "log_pattern": detect_log_pattern,
    "pdf_page_size": detect_pdf_page_size,
    "table_alignment": detect_table_alignment,
    "bilingual_caption": detect_bilingual_caption,
    "english_headings": detect_english_headings,
}


def _applicability(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome | None:
    """先判断本次产物是否包含规则对象，避免将缺少对象误报为格式失败。"""
    environment = rule.config.get("applicability_nonempty_environment")
    if not environment:
        return None
    pattern = re.compile(
        rf"\\begin\{{{re.escape(environment)}\}}(.*?)"
        rf"\\end\{{{re.escape(environment)}\}}",
        re.DOTALL,
    )
    bodies = []
    for path in context.paths(rule.config.get("paths")):
        bodies.extend(pattern.findall(_strip_comments(context.text(path))))
    if not bodies or not any(body.strip() for body in bodies):
        return DetectorOutcome("not_applicable", f"本次不含非空 {environment} 内容。", [])
    return None


def execute_detector(context: CheckContext, rule: RuleDefinition) -> DetectorOutcome:
    applicability = _applicability(context, rule)
    if applicability is not None:
        return applicability
    if not rule.detector:
        return DetectorOutcome("not_implemented", "当前没有可靠的自动检测器。", [])
    detector = DETECTORS.get(rule.detector)
    if detector is None:
        return DetectorOutcome("not_implemented", f"检测器未注册：{rule.detector}", [])
    try:
        return detector(context, rule)
    except Exception as exc:
        # 检测器自身异常不能伪装成不适用或通过。
        return DetectorOutcome("internal_error", f"检测器执行异常：{type(exc).__name__}: {exc}", [])
