# -*- coding: utf-8 -*-
"""通用补丁只能改已定位区域，保护真实内容、模板和当前输入证据。"""

import hashlib
import json

import pytest

from agents.quality_repair.patching import WriteRegion, prepare_patch, PatchSafetyError


##### 候选区域夹具板块 #####

def fixture(tmp_path, body="正文保持不变。", role="body_paragraph"):
    (tmp_path / ".scholar-generation.json").write_text(json.dumps({
        "status": "staging", "generation_id": "candidate-1",
    }), encoding="utf-8")
    path = tmp_path / "chapter.tex"
    path.write_text(f"% SCHOLAR_UNIT_BEGIN u-1 {role}\n{body}\n% SCHOLAR_UNIT_END u-1\n"
                    f"% SCHOLAR_UNIT_BEGIN u-2 body_paragraph\n{body}\n% SCHOLAR_UNIT_END u-2\n", encoding="utf-8")
    region = WriteRegion("chapter.tex", "u-1", role)
    return path, region


def prepare(tmp_path, path, region, old, new):
    return prepare_patch(tmp_path, generation_id="candidate-1", region=region,
                         expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                         edits=[{"old_text": old, "new_text": new}])


##### 内容与区域保护板块 #####

def test_unique_match_is_scoped_to_confirmed_unit_not_whole_file(tmp_path):
    path, region = fixture(tmp_path)
    original = path.read_bytes()
    change = prepare(tmp_path, path, region, "正文保持不变。", r"{\small 正文保持不变。}")
    assert change["before"] == original
    assert change["after"].decode("utf-8").count(r"\small") == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize("new", [
    "正文已经改变。", "正文保持不变！", "正文保持不变。额外内容", "正文保持不变。正文保持不变。",
    r"\input{secret}正文保持不变。", r"\csname input\endcsname{secret}正文保持不变。",
])
def test_content_changes_and_file_commands_are_rejected(tmp_path, new):
    path, region = fixture(tmp_path)
    with pytest.raises(PatchSafetyError):
        prepare(tmp_path, path, region, "正文保持不变。", new)


def test_math_group_meaning_and_reference_order_are_preserved(tmp_path):
    body = r"$x^{12}$引用\cite{ref1,ref2}。"
    path, region = fixture(tmp_path, body)
    for new in (r"$x^12$引用\cite{ref1,ref2}。", r"$x^{12}$引用\cite{ref2,ref1}。"):
        with pytest.raises(PatchSafetyError):
            prepare(tmp_path, path, region, body, new)


def test_word_boundaries_cannot_be_collapsed(tmp_path):
    path, region = fixture(tmp_path, "an other result")
    with pytest.raises(PatchSafetyError):
        prepare(tmp_path, path, region, "an other result", "another result")


def test_generic_figure_placement_patch_preserves_resource(tmp_path):
    body = r"\begin{figure}\includegraphics{image.png}\caption{示意图}\end{figure}"
    path, region = fixture(tmp_path, body, "figure_caption")
    assert prepare(tmp_path, path, region, r"\begin{figure}", r"\begin{figure}[htbp]")["after"]
    with pytest.raises(PatchSafetyError):
        prepare(tmp_path, path, region, "image.png", "other.png")


def test_plain_paragraph_indentation_is_a_format_only_change(tmp_path):
    path, region = fixture(tmp_path)
    assert prepare(tmp_path, path, region, "正文保持不变。", r"\noindent 正文保持不变。")["after"]


@pytest.mark.parametrize("options", ["trim=10pt 0 0 0,clip", "draft", "page=2", "width=0pt", "height=-1cm"])
def test_image_options_cannot_hide_or_replace_source_content(tmp_path, options):
    body = r"\includegraphics[width=0.9\linewidth]{image.png}"
    path, region = fixture(tmp_path, body)
    with pytest.raises(PatchSafetyError):
        prepare(tmp_path, path, region, body, r"\includegraphics[" + options + r"]{image.png}")


def test_generic_table_patch_can_change_float_to_longtable_without_changing_cells(tmp_path):
    body = r"\begin{table}\caption{数据}\begin{tabular}{cc}A&B\\1&2\\\end{tabular}\end{table}"
    new = r"\begin{longtable}{cc}\caption{数据}\\A&B\\1&2\\\end{longtable}"
    path, region = fixture(tmp_path, body, "table_caption")
    assert prepare(tmp_path, path, region, body, new)["after"]
    with pytest.raises(PatchSafetyError):
        prepare(tmp_path, path, region, body, new.replace("1&2", "1&3"))


def test_longtable_patch_cannot_leak_format_declarations_to_later_units(tmp_path):
    body = r'\begingroup\centering\begin{longtable}{cc}A&B\\1&2\\\end{longtable}\endgroup'
    path, region = fixture(tmp_path, body, 'table_caption')
    before = path.read_bytes()
    with pytest.raises(PatchSafetyError, match='局部'):
        prepare(tmp_path, path, region, body, body.replace(r'\begingroup', '').replace(r'\endgroup', ''))
    assert path.read_bytes() == before


##### 输入与路径边界板块 #####

def test_stale_evidence_and_published_workspace_fail_closed(tmp_path):
    path, region = fixture(tmp_path)
    with pytest.raises(PatchSafetyError):
        prepare_patch(tmp_path, generation_id="candidate-1", region=region,
                      expected_sha256="bad", edits=[{"old_text": "正文", "new_text": "正文"}])
    marker = tmp_path / ".scholar-generation.json"
    marker.write_text(json.dumps({"status": "published", "generation_id": "candidate-1"}), encoding="utf-8")
    with pytest.raises(PatchSafetyError):
        prepare(tmp_path, path, region, "正文", "正文")


@pytest.mark.parametrize("relative", ["../chapter.tex", "D:/outside.tex", "template.cls", "template.bst"])
def test_untrusted_paths_and_template_files_cannot_be_written(tmp_path, relative):
    path, region = fixture(tmp_path)
    with pytest.raises(PatchSafetyError):
        prepare(tmp_path, path, WriteRegion(relative, "u-1", "body_paragraph"), "正文", "正文")
