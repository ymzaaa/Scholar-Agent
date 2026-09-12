# -*- coding: utf-8 -*-
"""generation 交付登记与下载共用的最小产物契约。"""

import re
from pathlib import Path, PurePosixPath


##### 路径与清单板块 #####


def artifact_path(item, *, output_dir, report_dir, check_file=True):
    """只解析登记的两类根与安全相对路径；下载不重新计算哈希。"""
    if not isinstance(item, dict) or not re.fullmatch(r'[0-9a-fA-F]{64}', str(item.get('sha256', ''))):
        raise ValueError('产物登记缺少有效 SHA-256。')
    root_name = item.get('root')
    if root_name not in {'output', 'report'}:
        raise ValueError('产物根无效。')
    root_value = output_dir if root_name == 'output' else report_dir
    name = item.get('relative_path')
    if not root_value or not isinstance(name, str) or not name:
        raise ValueError('产物路径缺失。')
    relative = PurePosixPath(name.replace('\\', '/'))
    if relative.is_absolute() or '..' in relative.parts or ':' in name or not relative.parts:
        raise ValueError('产物路径无效。')
    root = Path(root_value).resolve()
    path = (root / relative.as_posix()).resolve()
    if root not in path.parents:
        raise ValueError('产物路径越界。')
    if check_file and (not path.is_file() or type(item.get('size')) is not int
                       or path.stat().st_size != item['size']):
        raise ValueError('产物缺失或大小不一致。')
    return path


def validate_artifact_roots(*, workspace_dir, generation_id, output_dir, report_dir):
    """报告也只能来自当前项目和任务，不能因失败状态放宽目录所有权。"""
    project = Path(workspace_dir).resolve()
    for value, category in ((output_dir, 'generations'), (report_dir, 'reports')):
        if value:
            expected = project / category / generation_id
            if Path(value).resolve() != expected or project not in expected.resolve().parents:
                raise ValueError('产物目录不属于当前项目和 generation。')


def validate_delivery_manifest(*, workspace_dir, generation_id, output_dir,
                               report_dir, manifest, check_files=True):
    """可信登记要求正式目录和唯一 PDF、源码包、流水线报告，哈希由生产者计算。"""
    project = Path(workspace_dir).resolve()
    expected = project / 'generations' / generation_id
    if not output_dir or Path(output_dir).resolve() != expected or project not in expected.resolve().parents:
        raise ValueError('正式输出目录不属于当前项目和 generation。')
    if report_dir and Path(report_dir).resolve() != project / 'reports' / generation_id:
        raise ValueError('报告目录不属于当前 generation。')
    items = manifest.get('artifacts') if isinstance(manifest, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError('可信产物清单为空。')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get('kind') not in {'pdf', 'latex_source', 'report'}:
            raise ValueError('产物条目类型无效。')
        if type(item.get('size')) is not int or item['size'] <= 0 or not re.fullmatch(r'[0-9a-fA-F]{64}', str(item.get('sha256', ''))):
            raise ValueError('产物大小或 SHA-256 清单不完整。')
        path = artifact_path(item, output_dir=output_dir, report_dir=report_dir, check_file=check_files)
        key = (item['kind'], item.get('name'))
        if key in seen or item.get('name') != path.name:
            raise ValueError('产物名称重复或与相对路径不一致。')
        seen.add(key)
    for kind, name in (('pdf', None), ('latex_source', 'latex-source.zip')):
        matches = [item for item in items if item['kind'] == kind]
        if (len(matches) != 1 or matches[0]['root'] != 'output'
            or (name is not None and matches[0]['name'] != name)):
            raise ValueError('可信版本缺少唯一 PDF 或源码包。')
    # 首次生成和反馈修订有各自现存的正式审计报告；不强迫反馈复制旧流水线报告。
    if not any(item['kind'] == 'report' and item['name'] in {'pipeline_log.json', 'agent_feedback.json'}
               for item in items):
        raise ValueError('可信版本缺少正式审计报告。')
