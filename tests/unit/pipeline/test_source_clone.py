# -*- coding: utf-8 -*-
"""可信父源码克隆的路径、哈希和压缩包安全测试。"""

from __future__ import annotations

import hashlib
import json
import stat
import uuid
import zipfile
from pathlib import Path

import pytest

from pipeline.source_clone import SourceCloneError, clone_latex_source
from pipeline.generation_service import _package_latex_source
from pipeline.workspace import GENERATION_MARKER

CANDIDATE_ID = str(uuid.uuid4())


def _archive(path: Path, entries: dict[str, bytes]) -> tuple[int, str]:
    with zipfile.ZipFile(path, "w") as output:
        for name, content in entries.items():
            output.writestr(name, content)
    return path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()


def _staging(path: Path) -> Path:
    path.mkdir()
    (path / GENERATION_MARKER).write_text(
        json.dumps({"status": "staging", "generation_id": CANDIDATE_ID}), encoding="utf-8",
    )
    return path


def _clone(tmp_path: Path, entries: dict[str, bytes]):
    archive = tmp_path / "source.zip"
    size, digest = _archive(archive, entries)
    staging = _staging(tmp_path / "staging")
    manifest = clone_latex_source(
        archive, staging, expected_size=size, expected_sha256=digest,
        report_path=tmp_path / "reports" / "clone.json",
        parent_version_id=str(uuid.uuid4()),
        candidate_version_id=CANDIDATE_ID,
    )
    return staging, manifest


def test_clone_preserves_only_verified_source_files(tmp_path: Path) -> None:
    staging, manifest = _clone(tmp_path, {
        "main.tex": b"main", "contents/section_01.tex": b"chapter",
        "img/figure.png": b"png", "includes/section_01.tex": b"section",
        "template.sty": b"style", "img/logo.eps": b"eps",
    })
    assert (staging / "main.tex").read_bytes() == b"main"
    assert (staging / "template.sty").read_bytes() == b"style"
    assert (staging / "img" / "logo.eps").read_bytes() == b"eps"
    assert manifest["file_count"] == 6
    assert all(set(item) == {"relative_path", "size"} for item in manifest["files"])


@pytest.mark.parametrize(
    "dangerous", ["../escape.tex", "C:/escape.tex", "other/a.tex", "main.pdf"],
)
def test_clone_rejects_dangerous_or_unknown_paths(
    tmp_path: Path, dangerous: str,
) -> None:
    with pytest.raises(SourceCloneError):
        _clone(tmp_path, {dangerous: b"bad"})


def test_clone_rejects_tampered_archive_hash(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    size, _ = _archive(archive, {"main.tex": b"main"})
    staging = _staging(tmp_path / "staging")
    with pytest.raises(SourceCloneError, match="哈希"):
        clone_latex_source(
            archive, staging, expected_size=size, expected_sha256="0" * 64,
            report_path=tmp_path / "report.json",
            parent_version_id=str(uuid.uuid4()),
            candidate_version_id=CANDIDATE_ID,
        )


def test_clone_rejects_symbolic_link_entry(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    info = zipfile.ZipInfo("main.tex")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(info, "target.tex")
    staging = _staging(tmp_path / "staging")
    with pytest.raises(SourceCloneError, match="符号链接"):
        clone_latex_source(
            archive, staging, expected_size=archive.stat().st_size,
            expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            report_path=tmp_path / "report.json",
            parent_version_id=str(uuid.uuid4()),
            candidate_version_id=CANDIDATE_ID,
        )


def test_clone_keeps_word_assets_for_feedback_compile(tmp_path: Path) -> None:
    staging, manifest = _clone(tmp_path, {
        "main.tex": rb"\includegraphics{assets/word/figure.png}",
        "assets/word/figure.png": b"synthetic-image",
    })
    assert (staging / "assets" / "word" / "figure.png").read_bytes() == b"synthetic-image"
    assert manifest["file_count"] == 2


def test_source_package_contains_extracted_word_assets(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(
        r"\includegraphics{assets/word/figure.png}", encoding="utf-8",
    )
    asset = tmp_path / "assets" / "word" / "figure.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"synthetic-image")
    archive = _package_latex_source(tmp_path)
    with zipfile.ZipFile(archive) as package:
        assert "assets/word/figure.png" in package.namelist()


def test_source_package_contains_bachelor_template_dependencies(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(
        r"\input{includes/section_01}", encoding="utf-8",
    )
    (tmp_path / "oucart.sty").write_text("% style", encoding="utf-8")
    section = tmp_path / "includes" / "section_01.tex"
    section.parent.mkdir()
    section.write_text("content", encoding="utf-8")
    logo = tmp_path / "img" / "logo.eps"
    logo.parent.mkdir()
    logo.write_bytes(b"eps")
    archive = _package_latex_source(tmp_path)
    with zipfile.ZipFile(archive) as package:
        assert {"oucart.sty", "includes/section_01.tex", "img/logo.eps"} <= set(
            package.namelist()
        )


##### 克隆中断板块 #####


@pytest.mark.parametrize('field,value', [('status', 'published'), ('generation_id', 'other')])
def test_clone_requires_matching_staging_marker(tmp_path, field, value):
    archive = tmp_path / 'source.zip'
    size, digest = _archive(archive, {'main.tex': b'main'})
    staging = _staging(tmp_path / 'staging')
    marker = {'status': 'staging', 'generation_id': CANDIDATE_ID, field: value}
    (staging / GENERATION_MARKER).write_text(json.dumps(marker))
    with pytest.raises(SourceCloneError, match='标记'):
        clone_latex_source(archive, staging, expected_size=size, expected_sha256=digest,
            report_path=tmp_path / 'clone.json', parent_version_id='parent', candidate_version_id=CANDIDATE_ID)
    assert not (staging / 'main.tex').exists()


def test_clone_cleans_current_partial_file_and_directories(tmp_path, monkeypatch):
    from pipeline import source_clone
    def interrupted(reader, writer, **kwargs):
        writer.write(b'partial')
        raise OSError('copy interrupted')
    monkeypatch.setattr(source_clone.shutil, 'copyfileobj', interrupted)
    with pytest.raises(OSError, match='copy interrupted'):
        _clone(tmp_path, {'contents/chapter.tex': b'body'})
    assert [p.name for p in (tmp_path / 'staging').iterdir()] == [GENERATION_MARKER]


def test_clone_report_failure_cleans_all_copied_files(tmp_path, monkeypatch):
    replace = Path.replace
    def interrupted(path, target):
        if Path(target).name == 'clone.json':
            raise OSError('report interrupted')
        return replace(path, target)
    monkeypatch.setattr(Path, 'replace', interrupted)
    with pytest.raises(OSError, match='report interrupted'):
        _clone(tmp_path, {'contents/chapter.tex': b'body'})
    assert [p.name for p in (tmp_path / 'staging').iterdir()] == [GENERATION_MARKER]
    assert not (tmp_path / 'reports' / 'clone.json').exists()


def test_cleanup_enumeration_failure_preserves_copy_error(tmp_path, monkeypatch):
    """目录遍历本身失败也只能附加诊断，不能遮蔽原始复制异常。"""
    from pipeline import source_clone
    def interrupted(reader, writer, **kwargs):
        writer.write(b'partial')
        raise ValueError('original copy failure')
    monkeypatch.setattr(source_clone.shutil, 'copyfileobj', interrupted)
    monkeypatch.setattr(Path, 'rglob', lambda *args: (_ for _ in ()).throw(OSError('cleanup scan failure')))
    with pytest.raises(ValueError, match='original copy failure') as failure:
        _clone(tmp_path, {'contents/chapter.tex': b'body'})
    assert any('cleanup scan failure' in note for note in failure.value.__notes__)
