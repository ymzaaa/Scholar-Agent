# -*- coding: utf-8 -*-
"""确定性 LaTeX 编译执行器与真实成功门禁。"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import time
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable

from pipeline.results import CompileResult, ProcessResult
from template_registry.models import CompileRecipe


##### 编译策略板块 #####


STALE_SUFFIXES = {
    ".aux",
    ".bbl",
    ".bcf",
    ".blg",
    ".fdb_latexmk",
    ".fls",
    ".lof",
    ".log",
    ".lot",
    ".out",
    ".pdf",
    ".run.xml",
    ".synctex.gz",
    ".toc",
    ".toe",
}

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
PdfParser = Callable[[Path], tuple[bool, str]]


##### 通用门禁板块 #####


def _gate(passed: bool, detail: str, *, blocking: bool = True) -> dict[str, Any]:
    return {"passed": passed, "blocking": blocking, "detail": detail}


def _is_fresh(path: Path, started_at_ns: int) -> bool:
    return path.exists() and path.stat().st_mtime_ns >= started_at_ns


def _tail(text: str | bytes | None, limit: int = 4000) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[-limit:]


def _safe_auxiliary_directories(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = []
    for value in values:
        relative = PurePosixPath(str(value).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
            raise ValueError(f"辅助文件目录必须是安全的根级相对目录：{value}")
        normalized.append(relative.as_posix())
    return tuple(dict.fromkeys(normalized))


def remove_stale_main_artifacts(
    output_dir: Path,
    stem: str = "main",
    auxiliary_directories: tuple[str, ...] = ("contents", "data"),
) -> list[str]:
    """只删除输出目录中已知的同名编译产物，不递归、不拼接任意通配目标。"""
    removed = []
    for suffix in STALE_SUFFIXES:
        candidate = output_dir / f"{stem}{suffix}"
        if candidate.is_file():
            candidate.unlink()
            removed.append(candidate.name)
    # LaTeX \include 会在子目录生成 aux；只清理两个约定产物目录中的 aux 文件。
    for directory_name in _safe_auxiliary_directories(auxiliary_directories):
        child_root = output_dir / directory_name
        if not child_root.is_dir():
            continue
        for candidate in child_root.rglob("*.aux"):
            resolved = candidate.resolve()
            if output_dir != resolved and output_dir not in resolved.parents:
                raise ValueError(f"拒绝清理输出目录之外的辅助文件：{resolved}")
            if resolved.is_file():
                resolved.unlink()
                removed.append(str(resolved.relative_to(output_dir)))
    return sorted(removed)


##### 子进程执行板块 #####


def run_compile_process(
    label: str,
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
    process_runner: ProcessRunner = subprocess.run,
) -> ProcessResult:
    started = time.monotonic()
    try:
        completed = process_runner(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        return ProcessResult(
            label=label,
            command=command,
            return_code=completed.returncode,
            duration_seconds=round(time.monotonic() - started, 6),
            stdout_tail=_tail(completed.stdout),
            stderr_tail=_tail(completed.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        return ProcessResult(
            label=label,
            command=command,
            return_code=None,
            duration_seconds=round(time.monotonic() - started, 6),
            timed_out=True,
            output_complete=False,
            stdout_tail=_tail(exc.stdout),
            stderr_tail=_tail(exc.stderr),
        )
    except FileNotFoundError as exc:
        return ProcessResult(
            label=label,
            command=command,
            return_code=None,
            duration_seconds=round(time.monotonic() - started, 6),
            missing_tool=True,
            output_complete=False,
            stderr_tail=str(exc),
        )


def _process_passed(result: ProcessResult) -> bool:
    return (
        result.return_code == 0
        and not result.timed_out
        and not result.missing_tool
        and result.output_complete
    )


##### PDF 门禁板块 #####


def _parse_pdf_with_available_tool(pdf_path: Path) -> tuple[bool, str]:
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        completed = subprocess.run(
            [pdfinfo, str(pdf_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return False, _tail(completed.stderr or completed.stdout, 1000)
        pages = re.search(r"^Pages:\s*(\d+)", completed.stdout, flags=re.MULTILINE)
        if not pages or int(pages.group(1)) < 1:
            return False, "pdfinfo 未返回有效页数。"
        return True, f"pdfinfo 可解析，页数={pages.group(1)}。"

    if importlib.util.find_spec("pypdf"):
        from pypdf import PdfReader

        try:
            pages = len(PdfReader(str(pdf_path)).pages)
        except Exception as exc:  # pragma: no cover - 依赖存在时才进入
            return False, f"pypdf 解析失败：{exc}"
        return (pages > 0, f"pypdf 可解析，页数={pages}。")
    return False, "缺少 pdfinfo 或 pypdf，无法完成 PDF 可解析性门禁。"


def validate_pdf_artifact(
    pdf_path: Path,
    started_at_ns: int,
    *,
    pdf_parser: PdfParser = _parse_pdf_with_available_tool,
) -> dict[str, dict[str, Any]]:
    exists = pdf_path.is_file()
    size = pdf_path.stat().st_size if exists else 0
    fresh = _is_fresh(pdf_path, started_at_ns) if exists else False
    signature_ok = False
    eof_ok = False
    if exists and size > 0:
        with pdf_path.open("rb") as stream:
            signature_ok = stream.read(5) == b"%PDF-"
            stream.seek(max(0, size - 2048))
            eof_ok = b"%%EOF" in stream.read()
    parse_ok, parse_detail = (False, "PDF 不存在。")
    if exists and size > 0 and signature_ok and eof_ok:
        parse_ok, parse_detail = pdf_parser(pdf_path)
    return {
        "pdf_exists": _gate(exists, f"path={pdf_path}"),
        "pdf_nonempty": _gate(size > 0, f"size={size}"),
        "pdf_fresh": _gate(fresh, f"mtime_ns={pdf_path.stat().st_mtime_ns if exists else 0}"),
        "pdf_signature": _gate(signature_ok and eof_ok, "PDF header/trailer 检查。"),
        "pdf_parseable": _gate(parse_ok, parse_detail),
    }


##### 日志与引用门禁板块 #####


def validate_log_artifact(log_path: Path, started_at_ns: int) -> dict[str, dict[str, Any]]:
    exists = log_path.is_file()
    content = log_path.read_text(encoding="utf-8", errors="replace") if exists else ""
    fresh = _is_fresh(log_path, started_at_ns) if exists else False
    fatal_patterns = (
        r"(?m)^! ",
        r"Emergency stop",
        r"Fatal error occurred",
        r"No pages of output",
    )
    reference_patterns = (
        r"Citation [`'].+?[`'] .*undefined",
        r"Reference [`'].+?[`'] .*undefined",
        r"There were undefined references",
        r"There were undefined citations",
        r"Label\(s\) may have changed",
        r"Rerun to get cross-references right",
    )
    fatal = [pattern for pattern in fatal_patterns if re.search(pattern, content)]
    unresolved = [pattern for pattern in reference_patterns if re.search(pattern, content)]
    summary_marker = (
        "PDF statistics:" in content
        or "Here is how much of TeX's memory you used:" in content
    )
    complete = "Output written on" in content and summary_marker
    return {
        "log_exists": _gate(exists, f"path={log_path}"),
        "log_nonempty": _gate(bool(content.strip()), f"chars={len(content)}"),
        "log_fresh": _gate(fresh, f"mtime_ns={log_path.stat().st_mtime_ns if exists else 0}"),
        "log_complete": _gate(complete, "要求包含输出标记和 TeX 运行摘要。"),
        "log_no_fatal": _gate(not fatal, f"fatal_patterns={fatal}"),
        "references_resolved": _gate(not unresolved, f"unresolved_patterns={unresolved}"),
    }


def _aux_citation_keys(aux_content: str) -> set[str]:
    keys = set()
    for group in re.findall(r"\\citation\{([^}]*)\}", aux_content):
        keys.update(key.strip() for key in group.split(",") if key.strip() and key.strip() != "*")
    return keys


def _biber_citation_keys(bcf_content: str) -> set[str]:
    """提取 Biber 控制文件中的引用键，不依赖固定 XML 命名空间前缀。"""
    return {
        key.strip()
        for key in re.findall(
            r"<(?:[\w.-]+:)?citekey\b[^>]*>(.*?)</(?:[\w.-]+:)?citekey>",
            bcf_content,
            flags=re.DOTALL,
        )
        if key.strip() and key.strip() != "*"
    }


def _combined_aux_content(
    output_dir: Path,
    stem: str,
    auxiliary_directories: tuple[str, ...],
) -> str:
    """合并主 AUX 与受控子目录 AUX；章节引用通常只写入子 AUX。"""
    paths = [output_dir / f"{stem}.aux"]
    for directory_name in _safe_auxiliary_directories(auxiliary_directories):
        directory = output_dir / directory_name
        if directory.is_dir():
            paths.extend(sorted(directory.rglob("*.aux")))
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in paths if path.is_file()
    )


def validate_auxiliary_artifacts(
    output_dir: Path,
    started_at_ns: int,
    stem: str = "main",
    auxiliary_directories: tuple[str, ...] = ("contents", "data"),
    bibliography_backend: str = "bibtex",
    bibliography_policy: str = "auto_if_cited",
) -> dict[str, dict[str, Any]]:
    aux_path = output_dir / f"{stem}.aux"
    aux_exists = aux_path.is_file()
    aux_content = aux_path.read_text(encoding="utf-8", errors="replace") if aux_exists else ""
    combined_aux = _combined_aux_content(
        output_dir, stem, auxiliary_directories
    )
    bcf_path = output_dir / f"{stem}.bcf"
    bcf_exists = bcf_path.is_file()
    bcf_content = bcf_path.read_text(encoding="utf-8", errors="replace") if bcf_exists else ""
    if bibliography_backend == "bibtex":
        declared = bool(re.search(r"\\bibdata\{|\\bibstyle\{", combined_aux))
        cited = _aux_citation_keys(combined_aux)
    elif bibliography_backend == "biber":
        declared = bcf_exists
        cited = _biber_citation_keys(bcf_content)
    else:
        declared = False
        cited = set()
    gates = {
        "aux_exists": _gate(aux_exists, f"path={aux_path}"),
        "aux_nonempty": _gate(bool(aux_content.strip()), f"chars={len(aux_content)}"),
        "aux_fresh": _gate(_is_fresh(aux_path, started_at_ns), "本轮生成的 aux。"),
    }
    nested_aux = [
        path
        for directory_name in _safe_auxiliary_directories(auxiliary_directories)
        for path in (output_dir / directory_name).rglob("*.aux")
        if (output_dir / directory_name).is_dir()
    ]
    stale_nested = [
        str(path.relative_to(output_dir))
        for path in nested_aux
        if not _is_fresh(path, started_at_ns)
    ]
    gates["nested_aux_fresh"] = _gate(
        not stale_nested, f"stale_nested_aux={stale_nested[:20]}"
    )
    if bibliography_backend == "biber" and bcf_exists:
        gates["bcf_nonempty"] = _gate(bool(bcf_content.strip()), f"chars={len(bcf_content)}")
        gates["bcf_fresh"] = _gate(
            _is_fresh(bcf_path, started_at_ns), "本轮生成的 bcf。"
        )
    bibliography_required = declared and (
        bibliography_policy == "always_if_declared"
        or (bibliography_policy == "auto_if_cited" and bool(cited))
    )
    if not bibliography_required:
        detail = (
            "本轮参考文献后端已禁用或项目未声明对应数据。"
            if not declared else "模板声明了参考文献后端，但本轮没有实际引用。"
        )
        gates["bibliography_complete"] = _gate(True, detail)
        return gates

    bbl_path = output_dir / f"{stem}.bbl"
    bbl_exists = bbl_path.is_file()
    bbl_content = bbl_path.read_text(encoding="utf-8", errors="replace") if bbl_exists else ""
    if bibliography_backend == "bibtex":
        resolved = set(re.findall(r"\\bibcite\{([^}]+)\}", combined_aux))
    else:
        resolved = set(re.findall(r"\\entry\{([^}]+)\}", bbl_content))
    missing = sorted(cited - resolved)
    gates.update(
        {
            "bbl_exists": _gate(bbl_exists, f"path={bbl_path}"),
            "bbl_nonempty": _gate(bool(bbl_content.strip()), f"chars={len(bbl_content)}"),
            "bbl_fresh": _gate(_is_fresh(bbl_path, started_at_ns), "本轮生成的 bbl。"),
            "bibliography_complete": _gate(not missing, f"missing_keys={missing[:20]}"),
        }
    )
    return gates


##### 结果汇总板块 #####


def inspect_compile_artifacts(
    output_dir: Path,
    started_at_ns: int,
    *,
    stem: str = "main",
    auxiliary_directories: tuple[str, ...] = ("contents", "data"),
    bibliography_backend: str = "bibtex",
    bibliography_policy: str = "auto_if_cited",
    pdf_parser: PdfParser = _parse_pdf_with_available_tool,
) -> dict[str, dict[str, Any]]:
    gates = {}
    gates.update(validate_pdf_artifact(output_dir / f"{stem}.pdf", started_at_ns, pdf_parser=pdf_parser))
    gates.update(validate_log_artifact(output_dir / f"{stem}.log", started_at_ns))
    gates.update(validate_auxiliary_artifacts(
        output_dir, started_at_ns, stem, auxiliary_directories,
        bibliography_backend, bibliography_policy,
    ))
    return gates


def _build_result(
    output_dir: Path,
    started_at_ns: int,
    process_results: list[ProcessResult],
    gates: dict[str, dict[str, Any]],
    *,
    stem: str = "main",
) -> CompileResult:
    process_ok = all(_process_passed(item) for item in process_results)
    gates["processes_passed"] = _gate(process_ok, "所有已执行子进程返回码为 0 且输出完整。")
    success = bool(process_results) and all(
        gate["passed"] for gate in gates.values() if gate.get("blocking", True)
    )
    errors = [f"{name}: {gate['detail']}" for name, gate in gates.items() if not gate["passed"]]
    for process in process_results:
        if not _process_passed(process):
            errors.append(
                f"{process.label}: return_code={process.return_code}, "
                f"timeout={process.timed_out}, missing_tool={process.missing_tool}"
            )
    pdf_path = output_dir / f"{stem}.pdf"
    return CompileResult(
        success=success,
        pdf_path=str(pdf_path) if success else "",
        raw_error="\n".join(errors),
        errs=0 if success else -1,
        started_at_ns=started_at_ns,
        finished_at_ns=time.time_ns(),
        process_results=process_results,
        gates=gates,
    )


def compile_project(
    output_dir: str | Path,
    recipe: CompileRecipe,
    *,
    timeout_seconds: int = 120,
    process_runner: ProcessRunner = subprocess.run,
    pdf_parser: PdfParser = _parse_pdf_with_available_tool,
) -> CompileResult:
    """按 Manifest 已验证的配方运行 LaTeX/BibTeX，并统一判断成功。"""
    directory = Path(output_dir).resolve()
    relative_entry = PurePosixPath(recipe.entrypoint)
    auxiliary_directories = recipe.auxiliary_directories
    tex_path = directory.joinpath(*relative_entry.parts).resolve()
    if directory != tex_path and directory not in tex_path.parents:
        raise ValueError("编译入口逃逸模板目录。")
    if not tex_path.is_file():
        return CompileResult(
            success=False, pdf_path="",
            raw_error=f"{relative_entry.as_posix()} not found", errs=-1,
        )
    stem = recipe.artifact_stem

    remove_stale_main_artifacts(directory, stem, auxiliary_directories)
    started_at_ns = time.time_ns()
    processes = []
    command = [
        recipe.engine, "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
        relative_entry.as_posix(),
    ]
    first = run_compile_process(
        f"{recipe.engine}#1", command, directory, timeout_seconds, process_runner
    )
    processes.append(first)
    if _process_passed(first):
        if recipe.bibliography_backend == "biber":
            control_content = (
                (directory / f"{stem}.bcf").read_text(encoding="utf-8", errors="replace")
                if (directory / f"{stem}.bcf").is_file() else ""
            )
            bibliography_declared = bool(control_content)
            citations_present = bool(_biber_citation_keys(control_content))
        else:
            control_content = _combined_aux_content(directory, stem, auxiliary_directories)
            bibliography_declared = bool(
                re.search(r"\\bibdata\{|\\bibstyle\{", control_content)
            )
            citations_present = bool(_aux_citation_keys(control_content))
        should_run_bibliography = (
            recipe.bibliography_policy == "always_if_declared" and bibliography_declared
        ) or (
            recipe.bibliography_policy == "auto_if_cited"
            and bibliography_declared and citations_present
        )
        if should_run_bibliography:
            bibliography = run_compile_process(
                recipe.bibliography_backend, [recipe.bibliography_backend, stem],
                directory, timeout_seconds, process_runner,
            )
            processes.append(bibliography)
        if all(_process_passed(item) for item in processes):
            for run_index in range(2, recipe.engine_runs + 1):
                result = run_compile_process(
                    f"{recipe.engine}#{run_index}", command,
                    directory, timeout_seconds, process_runner,
                )
                processes.append(result)
                if not _process_passed(result):
                    break

    gates = inspect_compile_artifacts(
        directory, started_at_ns, stem=stem,
        auxiliary_directories=auxiliary_directories,
        bibliography_backend=recipe.bibliography_backend,
        bibliography_policy=recipe.bibliography_policy,
        pdf_parser=pdf_parser,
    )
    return _build_result(directory, started_at_ns, processes, gates, stem=stem)
