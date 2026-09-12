# -*- coding: utf-8 -*-
"""发布文件的中断、原子替换和自有目录清理。"""

import json
import zipfile
from pathlib import Path

import pytest

from pipeline import workspace
from pipeline.source_package import package_latex_source


##### 工作区故障板块 #####


def test_project_creation_failure_removes_only_new_directory(tmp_path, monkeypatch):
    manager = workspace.WorkspaceManager(tmp_path / 'workspace')
    saved = manager.create_project()
    monkeypatch.setattr(workspace, '_write_json', lambda *args: (_ for _ in ()).throw(OSError('marker write')))
    with pytest.raises(OSError, match='marker write'):
        manager.create_project()
    assert list(manager.projects_dir.iterdir()) == [saved.project_dir]


def test_staging_creation_failure_removes_partial_directory(tmp_path, monkeypatch):
    manager = workspace.WorkspaceManager(tmp_path / 'workspace')
    project = manager.create_project()
    monkeypatch.setattr(workspace, '_write_json', lambda *args: (_ for _ in ()).throw(OSError('marker write')))
    with pytest.raises(OSError, match='marker write'):
        manager.begin_generation(project.project_id)
    assert list(project.generations_dir.iterdir()) == []


def test_publish_move_failure_restores_marker_and_can_abort(tmp_path, monkeypatch):
    manager = workspace.WorkspaceManager(tmp_path / 'workspace')
    project = manager.create_project()
    generation = manager.begin_generation(project.project_id)
    replace = Path.replace
    def interrupted(path, target):
        if path == generation.staging_dir:
            raise OSError('directory busy')
        return replace(path, target)
    monkeypatch.setattr(Path, 'replace', interrupted)
    with pytest.raises(OSError, match='directory busy'):
        generation.publish()
    marker = json.loads((generation.staging_dir / workspace.GENERATION_MARKER).read_text())
    assert marker['status'] == 'staging'
    generation.abort()
    assert not generation.staging_dir.exists()
    assert not generation.final_dir.exists()


def test_cleanup_failure_does_not_replace_business_error(tmp_path, monkeypatch):
    manager = workspace.WorkspaceManager(tmp_path / 'workspace')
    generation = manager.begin_generation(manager.create_project().project_id)
    monkeypatch.setattr(workspace.GenerationWorkspace, 'abort', lambda self: (_ for _ in ()).throw(OSError('cleanup')))
    with pytest.raises(ValueError, match='business') as failure:
        with generation:
            raise ValueError('business')
    assert any('cleanup' in note for note in failure.value.__notes__)


##### 原子文件板块 #####


def test_json_replace_failure_preserves_previous_file(tmp_path, monkeypatch):
    target = tmp_path / 'state.json'
    target.write_text('{"old": true}')
    monkeypatch.setattr(Path, 'replace', lambda *args: (_ for _ in ()).throw(OSError('replace')))
    with pytest.raises(OSError):
        workspace._write_json(target, {'new': True})
    assert json.loads(target.read_text()) == {'old': True}
    assert list(tmp_path.iterdir()) == [target]


def test_empty_source_package_is_rejected(tmp_path):
    (tmp_path / 'main.pdf').write_bytes(b'%PDF')
    with pytest.raises(ValueError, match='空'):
        package_latex_source(tmp_path)
    assert not (tmp_path / 'latex-source.zip').exists()


def test_zip_interrupted_write_preserves_previous_archive(tmp_path, monkeypatch):
    (tmp_path / 'main.tex').write_text('source')
    archive, _ = package_latex_source(tmp_path)
    before = archive.read_bytes()
    monkeypatch.setattr(zipfile.ZipFile, 'write', lambda *args: (_ for _ in ()).throw(OSError('zip write')))
    with pytest.raises(OSError, match='zip write'):
        package_latex_source(tmp_path)
    assert archive.read_bytes() == before
    assert not list(tmp_path.glob('.tmp-*'))


def test_package_excludes_generated_files_and_reports(tmp_path):
    for name in ('main.tex', 'main.pdf', 'other.pdf', 'main.log', 'main.aux', 'reports/debug.tex', 'assets/plot.png'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'content')
    archive, members = package_latex_source(tmp_path)
    assert members == ['assets/plot.png', 'main.tex']
    with zipfile.ZipFile(archive) as source:
        assert source.namelist() == members
