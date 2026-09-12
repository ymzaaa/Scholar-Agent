# -*- coding: utf-8 -*-
"""通用工具的受限读取、模式权限、原子补丁与精确回滚。"""

import hashlib
import json

import pytest

from agents.quality_repair.models import AgentState
from agents.quality_repair.patching import WriteRegion
from agents.quality_repair.tools import QualityTools
from agents.quality_repair.action_journal import FileActionJournal


##### 私有工作副本板块 #####

@pytest.fixture()
def case(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    (root / ".scholar-generation.json").write_text(json.dumps({"generation_id": "g1", "status": "staging"}))
    source = b"% SCHOLAR_UNIT_BEGIN u-1 body_paragraph\r\nOriginal text.\r\n% SCHOLAR_UNIT_END u-1\r\n"
    target = root / "chapter.tex"
    target.write_bytes(source)
    journal = FileActionJournal(root, tmp_path / "journal", "run-1", generation_id="g1")
    tools = QualityTools(root=root, generation_id="g1", regions=[WriteRegion("chapter.tex", "u-1", "body_paragraph")],
                         evidence=[{"unit_id": "u-1", "text": "Original text.", "role": "body_paragraph"}],
                         template_context={"template_id": "ouc-bachelor"}, journal=journal)
    state = AgentState(run_id="run-1", project_id="p1", mode="user_feedback", template_id="ouc-bachelor",
                       goals=[{"goal_id": "goal-1", "kind": "format", "confirmed": True, "target_unit_ids": ["u-1"]}])
    return tools, state, journal, target, source


def proposal(target, *, action_id="action-1", new=r"\small Original text."):
    return {"tool": "apply_protected_patch", "action_id": action_id, "arguments": {
        "unit_id": "u-1", "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "edits": [{"old_text": "Original text.", "new_text": new}]}}


def test_atomic_write_preserves_original_error_when_cleanup_also_fails(tmp_path, monkeypatch):
    from pathlib import Path
    def fail_replace(*args):
        raise OSError("original write failure")
    def fail_unlink(*args, **kwargs):
        raise OSError("cleanup failure")
    monkeypatch.setattr(Path, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(OSError, match="original write failure") as caught:
        FileActionJournal._atomic(tmp_path / "record.json", b"new data")
    assert any("cleanup failure" in note for note in caught.value.__notes__)
    assert not (tmp_path / "record.json").exists()


def test_full_action_hash_in_deep_workspace_can_be_written_and_rolled_back(case, tmp_path):
    tools, state, _journal, target, original = case
    journal = FileActionJournal(target.parent, tmp_path / ('project-' + 'p' * 32)
        / ('generation-' + 'g' * 32) / 'agent-actions', 'r' * 36, generation_id='g1')
    tools.journal = journal
    action = proposal(target, action_id='a' * 64)
    tools.execute(action, state)
    assert journal.entries()[0]['action_id'] == 'a' * 64
    assert target.read_bytes() != original
    journal.rollback_all()
    assert target.read_bytes() == original


def test_atomic_target_temporary_file_supports_long_workspace_path(tmp_path):
    target = tmp_path / ('source-' + 's' * 90) / ('nested-' + 'n' * 32) / 'chapter.tex'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'original')
    FileActionJournal._atomic(target, b'updated')
    assert target.read_bytes() == b'updated'
    assert list(target.parent.iterdir()) == [target]


##### 权限与证据板块 #####

def test_tool_catalog_has_only_generic_reads_and_guarded_mutations(case):
    tools, state, *_ = case
    reads = {item["name"] for item in tools.definitions("recognition_review")}
    assert reads == {"read_quality_report", "read_compile_diagnostics", "search_source", "read_source",
                     "locate_content_unit", "read_template_context"}
    assert {item["name"] for item in tools.definitions(state.mode)} == reads | {"apply_protected_patch", "replace_confirmed_text"}


def test_read_returns_only_located_fragment_and_current_hash(case):
    tools, state, _, target, _ = case
    result = tools.execute({"tool": "read_source", "arguments": {"unit_id": "u-1"}}, state)
    assert result["data"]["text"] == "Original text.\r\n"
    assert result["data"]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert str(target) not in json.dumps(result)
    unknown = tools.execute({"tool": "read_source", "arguments": {"unit_id": "../../secret"}}, state)
    assert unknown["status"] == "rejected"


@pytest.mark.parametrize("fault", ["readonly", "unconfirmed", "stale", "content"])
def test_protected_patch_rejects_invalid_proposal_without_writing(case, fault):
    tools, state, _, target, original = case
    action = proposal(target)
    if fault == "readonly":
        state.mode = "recognition_review"
    elif fault == "unconfirmed":
        state.goals[0]["confirmed"] = False
    elif fault == "stale":
        action["arguments"]["expected_sha256"] = "0" * 64
    else:
        action["arguments"]["edits"][0]["new_text"] = "Changed scientific content."
    assert tools.execute(action, state)["status"] == "rejected"
    assert target.read_bytes() == original


##### 文件日志与恢复板块 #####

def test_applied_patch_and_exact_byte_rollback(case):
    tools, state, journal, target, original = case
    result = tools.execute(proposal(target), state)
    assert result["status"] == "ok" and result["changed_files"] == ["chapter.tex"]
    assert b"\\small Original text." in target.read_bytes()
    journal.rollback_all()
    assert target.read_bytes() == original


def test_atomic_write_failure_leaves_original_and_recoverable_journal(case, monkeypatch):
    tools, state, journal, target, original = case
    action = proposal(target)
    monkeypatch.setattr(journal, "_replace_target", lambda *args: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(OSError, match="injected"):
        tools.execute(action, state)
    assert target.read_bytes() == original
    journal.rollback_all()
    assert target.read_bytes() == original


def test_rollback_refuses_external_file_change(case):
    tools, state, journal, target, _ = case
    tools.execute(proposal(target), state)
    target.write_bytes(target.read_bytes() + b"unrelated edit")
    with pytest.raises(RuntimeError):
        journal.rollback_all()
    assert target.read_bytes().endswith(b"unrelated edit")


def test_interruption_after_file_commit_recovers_without_reapplying(case, monkeypatch):
    tools, state, journal, target, original = case
    action = proposal(target)
    write = journal._write
    def interrupted(path, payload):
        if payload["status"] == "applied":
            raise KeyboardInterrupt("process interruption")
        write(path, payload)
    monkeypatch.setattr(journal, "_write", interrupted)
    with pytest.raises(KeyboardInterrupt):
        tools.execute(action, state)
    committed_bytes = target.read_bytes()
    assert committed_bytes != original
    monkeypatch.setattr(journal, "_write", write)
    monkeypatch.setattr(journal, "_replace_target", lambda *args: (_ for _ in ()).throw(AssertionError("duplicate write")))
    result = tools.execute(action, state)
    assert result["status"] == "ok" and result["recovered"] is True
    assert target.read_bytes() == committed_bytes
