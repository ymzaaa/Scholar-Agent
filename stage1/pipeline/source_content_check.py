# -*- coding: utf-8 -*-
"""直接从持久化 token 核对实际文字和对象，不以渲染账本哈希代替内容证明。"""

import re

from pipeline.text_processing import escape_text, hyperlink_argument


##### 注释与普通文字板块 #####


def strip_latex_comments(text: str) -> str:
    """去掉未转义百分号后的注释，保留网址参数中的百分号。"""
    return re.sub(r'\\url\{[^}]*\}|\\.|%[^\n]*',
                  lambda match: '' if match.group().startswith('%') else match.group(), text)


def _literal(text: str) -> str:
    return re.escape(text)


##### 有序语义核对板块 #####


def ordered_token_ids(unit, by_id, kind):
    """沿根单元的有序 token 递归遍历嵌套对象，保留出现次数。"""
    payload = unit.get('payload', {})
    sequences = [payload.get('inline_tokens', [])]
    sequences.extend(block.get('tokens', []) for block in payload.get('blocks', []))
    sequences.extend(cell.get('tokens', []) for row in payload.get('physical_rows', []) for cell in row.get('cells', []))
    result = []
    for tokens in sequences:
        for token in tokens:
            child = by_id.get(token.get('unit_id'))
            if not child:
                continue
            if child.get('unit_type') == kind:
                result.append(child['unit_id'])
            result.extend(ordered_token_ids(child, by_id, kind))
    return result


def token_pattern(tokens, by_id, *, mapping, decisions, assets, labels, context='body', bound_ids=()):
    """只允许各持久化 token 的确定性输出，不识别新的公式、引用或标题。"""
    parts = []
    for token in tokens:
        kind = token.get('kind')
        unit_id = token.get('unit_id')
        unit = by_id.get(unit_id, {})
        payload = unit.get('payload', {})
        if kind == 'text':
            parts.append(_literal(escape_text(token.get('text', ''))))
        elif kind == 'line_break':
            parts.append(_literal(r' \\ '))
        elif kind == 'formula':
            if unit.get('status') != 'extracted':
                raise ValueError(f'公式未成功抽取：{unit_id}')
            if context == 'body' and unit.get('properties', {}).get('display'):
                parts.append(_literal('\n' + r'\begin{equation}' + '\n' + unit['text'] + '\n' + r'\end{equation}' + '\n'))
            else:
                parts.append(_literal('$' + unit['text'] + '$'))
        elif kind == 'citation':
            numbers = payload.get('numbers', [])
            keys = [mapping[number] for number in numbers if number in mapping]
            if decisions.get(unit_id, {}).get('decision') == 'body_citation' and numbers and len(keys) == len(numbers):
                value = r'\cite{' + ','.join(keys) + '}'
            else:
                value = escape_text(unit.get('text', ''))
                if payload.get('citation_syntax') == 'numeric_superscript':
                    value = r'\textsuperscript{' + value + '}'
            parts.append(_literal(value))
        elif kind == 'image':
            if unit_id in bound_ids:
                continue
            asset = assets.get(unit_id, {})
            if asset.get('status') != 'verified':
                raise ValueError(f'图片未验证：{unit_id}')
            parts.append(_literal(r'\includegraphics[width=0.9\linewidth]{' + asset['relative_path'] + '}'))
        elif kind in {'bookmark_start', 'bookmark_end'}:
            name = unit.get('properties', {}).get('name')
            if kind == 'bookmark_start' and name in labels:
                parts.append(_literal(r'\label{' + labels[name] + '}'))
        elif kind in {'footnote', 'text_box'}:
            children = [token_pattern(block.get('tokens', []), by_id, mapping=mapping,
                        decisions=decisions, assets=assets, labels=labels, context=kind)
                        for block in payload.get('blocks', [])]
            nested = _literal(r'\par ').join(children)
            if kind == 'footnote':
                parts.append(_literal(r'\footnote{') + nested + _literal('}'))
            else:
                parts.append(_literal('\n' + r'\begin{quote}\noindent \fbox{\parbox{0.92\linewidth}{')
                             + nested + _literal(r'}}\end{quote}' + '\n'))
        elif kind in {'field', 'hyperlink'}:
            target = payload.get('target_bookmark')
            field_type = unit.get('properties', {}).get('field_type')
            display = escape_text(unit.get('text', ''))
            if unit.get('status') != 'extracted':
                raise ValueError(f'语义对象未成功抽取：{unit_id}')
            if target in labels:
                value = (r'\pageref{' + labels[target] + '}' if field_type == 'PAGEREF'
                         else r'\hyperref[' + labels[target] + ']{' + display + '}')
            elif payload.get('url'):
                url = hyperlink_argument(payload['url'])
                value = r'\href{' + url + '}{' + display + '}'
            else:
                raise ValueError(f'语义对象没有有效目标：{unit_id}')
            parts.append(_literal(value))
        else:
            raise ValueError(f'不能证明的语义 token：{kind}')
    return ''.join(parts)


##### 正文独立验证板块 #####


def check_body_text(expected, by_id, segments, review, trace):
    """逐段完整匹配普通文字与原有语义对象，插入、删除、重复均不可放行。"""
    decisions = {item['unit_id']: item for item in review.get('citation_review', {}).get('decisions', [])}
    bindings = review.get('object_bindings', [])
    bound_ids = {unit_id for binding in bindings for unit_id in binding.get('object_unit_ids', [])}
    mapping = trace.get('bibliography', {}).get('render_number_to_key', {})
    errors = []
    checked = 0
    for paragraph in expected:
        unit_id = paragraph['unit_id']
        segment = segments.get(unit_id, {})
        if segment.get('role') != 'body_paragraph':
            continue
        checked += 1
        unit = by_id[unit_id]
        tokens = unit.get('payload', {}).get('inline_tokens') or [{'kind': 'text', 'text': unit.get('text', '')}]
        try:
            pattern = token_pattern(tokens, by_id, mapping=mapping, decisions=decisions,
                                    assets=trace.get('asset_manifest', {}),
                                    labels=trace.get('bookmark_index', {}).get('labels', {}), bound_ids=bound_ids)
            if unit.get('properties', {}).get('semantic_role') == 'equation_explanation':
                pattern = _literal(r'\noindent ') + pattern
            if not re.fullmatch(pattern, strip_latex_comments(segment['fragment'])):
                errors.append(unit_id)
        except ValueError as exc:
            errors.append(str(exc))
    return checked, errors


##### 物理单元格核对板块 #####


def check_table_text(tables, by_id, segments, review, trace):
    """从物理网格核对每个单元格的文字、语义对象及合并跨度。"""
    decisions = {item['unit_id']: item for item in review.get('citation_review', {}).get('decisions', [])}
    mapping = trace.get('bibliography', {}).get('render_number_to_key', {})
    source_cells = [cell for table in tables for row in table['payload']['physical_rows'] for cell in row['cells']]
    actual = []
    for segment in segments.values():
        actual.extend(re.findall(r'% SCHOLAR_CELL_BEGIN (\S+)\n(.*?)\n% SCHOLAR_CELL_END \1', segment['fragment'], re.S))
    actual_by_id = dict(actual)
    errors = []
    if len(actual) != len(actual_by_id) or [item[0] for item in actual] != [cell['cell_id'] for cell in source_cells]:
        errors.append('物理单元格来源、数量或唯一性不一致')
    for table in tables:
        rows = table['payload']['physical_rows']
        for index, row in enumerate(rows):
            for cell in row['cells']:
                try:
                    pattern = token_pattern(cell.get('tokens', []), by_id, mapping=mapping,
                        decisions=decisions, assets=trace.get('asset_manifest', {}),
                        labels=trace.get('bookmark_index', {}).get('labels', {}), context='table')
                    if cell['vertical_merge'] == 'continue':
                        if cell.get('text', '').strip() or any(token['kind'] not in {'text', 'line_break'} for token in cell.get('tokens', [])):
                            raise ValueError('纵向续接单元格含真实内容')
                        pattern = ''
                    else:
                        if cell.get('diagonal_border') != 'none':
                            labels = [escape_text(item.strip()) for item in cell.get('text', '').splitlines() if item.strip()]
                            if len(labels) != 2 or any(token['kind'] not in {'text', 'line_break'} for token in cell.get('tokens', [])):
                                raise ValueError('复杂斜线表头')
                            pattern = _literal(r'\diagbox{' + labels[1] + '}{' + labels[0] + '}')
                        if _literal(r'\\') in pattern:
                            pattern = _literal(r'\makecell[c]{') + pattern + _literal('}')
                        span = int(cell.get('grid_span') or 1)
                        if span > 1:
                            pattern = _literal(r'\multicolumn{' + str(span) + '}{c}{') + pattern + _literal('}')
                        if cell['vertical_merge'] == 'restart':
                            row_span = 1
                            for following in rows[index + 1:]:
                                candidate = next((item for item in following['cells'] if item['grid_column'] == cell['grid_column']), {})
                                if candidate.get('vertical_merge') != 'continue':
                                    break
                                row_span += 1
                            pattern = _literal(r'\multirow{' + str(row_span) + '}{*}{') + pattern + _literal('}')
                    if not re.fullmatch(pattern, strip_latex_comments(actual_by_id.get(cell['cell_id'], ''))):
                        errors.append(cell['cell_id'])
                except ValueError as exc:
                    errors.append(f"{cell['cell_id']}: {exc}")
    return errors
