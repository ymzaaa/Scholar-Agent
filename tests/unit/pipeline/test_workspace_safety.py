# -*- coding: utf-8 -*-
"""G2-A 项目工作区、generation、staging 和非覆盖渲染测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from tests.support.paths import STAGE1_DIR


##### 测试环境板块 #####


from pipeline.workspace import (
    GENERATION_MARKER,
    PROJECT_MARKER,
    WorkspaceManager,
    WorkspaceSafetyError,
    prepare_render_directory,
)


##### 项目与版本板块 #####


class WorkspaceLifecycleTests(unittest.TestCase):
    def test_generation_marker_records_version_lineage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(Path(temp_dir) / "workspace")
            project = manager.create_project()
            context = {
                "version_id": str(uuid.uuid4()),
                "parent_version_id": str(uuid.uuid4()),
                "version_number": 2,
            }
            generation = manager.begin_generation(
                project.project_id, version_context=context,
            )
            marker = json.loads(
                (generation.staging_dir / GENERATION_MARKER).read_text(encoding="utf-8")
            )
            self.assertEqual(marker["version"], context)
            generation.abort()

    def test_generation_marker_supports_deep_windows_workspace(self):
        with tempfile.TemporaryDirectory(prefix="scholar-workspace-path-") as temp_dir:
            deep_root = Path(temp_dir) / ("nested-" + "x" * 40) / "workspace"
            manager = WorkspaceManager(deep_root)
            project = manager.create_project()
            generation = manager.begin_generation(project.project_id)
            self.assertTrue((generation.staging_dir / GENERATION_MARKER).is_file())
            self.assertEqual(list(generation.staging_dir.glob(".tmp-*")), [])
            generation.abort()

    def test_create_project_records_resolved_readonly_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "paper.docx"
            source.write_bytes(b"input")
            manager = WorkspaceManager(root / "workspace")
            project = manager.create_project(source_paths={"docx": str(source)})
            marker = json.loads(
                (project.project_dir / PROJECT_MARKER).read_text(encoding="utf-8")
            )
        self.assertEqual(marker["source_paths"]["docx"], str(source.resolve()))

    def test_publish_creates_new_generation_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(Path(temp_dir) / "workspace")
            project = manager.create_project()
            generation = manager.begin_generation(project.project_id)
            generation_id = generation.generation_id
            with generation:
                prepare_render_directory(generation.staging_dir)
                (generation.staging_dir / "main.tex").write_text("first", encoding="utf-8")
                final = generation.publish()
            self.assertTrue((final / "main.tex").is_file())
            self.assertFalse(generation.staging_dir.exists())

            with self.assertRaises(WorkspaceSafetyError):
                manager.begin_generation(project.project_id, generation_id)

            second = manager.begin_generation(project.project_id)
            with second:
                (second.staging_dir / "main.tex").write_text("second", encoding="utf-8")
                second_final = second.publish()
            self.assertNotEqual(final, second_final)
            self.assertEqual((final / "main.tex").read_text(encoding="utf-8"), "first")

    def test_failure_removes_only_owned_staging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(Path(temp_dir) / "workspace")
            project = manager.create_project()
            generation = manager.begin_generation(project.project_id)
            with self.assertRaisesRegex(RuntimeError, "injected"):
                with generation:
                    (generation.staging_dir / "partial.tex").write_text("partial", encoding="utf-8")
                    raise RuntimeError("injected")
            self.assertFalse(generation.staging_dir.exists())
            self.assertFalse(generation.final_dir.exists())

    def test_tampered_staging_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(Path(temp_dir) / "workspace")
            project = manager.create_project()
            generation = manager.begin_generation(project.project_id)
            marker_path = generation.staging_dir / GENERATION_MARKER
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["project_id"] = str(uuid.uuid4())
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            with self.assertRaises(WorkspaceSafetyError):
                generation.abort()
            self.assertTrue(generation.staging_dir.exists())


##### 路径边界板块 #####


class WorkspaceBoundaryTests(unittest.TestCase):
    def test_nonempty_unowned_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "not-owned"
            root.mkdir()
            (root / "user-file.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(WorkspaceSafetyError):
                WorkspaceManager(root)
            self.assertTrue((root / "user-file.txt").is_file())

    def test_invalid_ids_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(Path(temp_dir) / "workspace")
            with self.assertRaises(WorkspaceSafetyError):
                manager.get_project("../../stage1")

    def test_render_refuses_existing_content_without_deleting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "existing"
            output.mkdir()
            sentinel = output / "user-result.tex"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaises(WorkspaceSafetyError):
                prepare_render_directory(output)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
