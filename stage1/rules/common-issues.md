# Word→LaTeX 迁移与模板格式常见错误

> 本文是历史问题与修复经验记录，不是运行时规则事实来源，其中的 OUC 和当前论文示例不得进入通用核心的固定判断。

本文档汇总 OUC 论文 LaTeX 模板使用及 Word→LaTeX 迁移过程中高频出现的格式错误，每条包含**错误现象**、**根因**、**检测方法**和**修复方案**。

---

## 1. 英文题目

### 1.1 虚词首字母大写

**现象**：封面英文题目中 "And""For""In""On""At""Of""To""A""An""The" 等词首字母大写。

**根因**：Word 标题常使用"每个单词首字母大写"(Title Case)自动格式，但 OUC 标准要求虚词(≤5字母)全小写。

**检测**：逐词检查 `cover.tex` 中 `\textbf{...}` 内的英文标题。

**修复**：
```latex
% 错误
Cell Spatial-AttMIL: Cell Microenvironment Fusion And Spatial Graph
Attention For Gastrointestinal Cancer MSI Prediction

% 正确
Cell Spatial-AttMIL: Cell Microenvironment Fusion and Spatial Graph
Attention for Gastrointestinal Cancer MSI Prediction
```

### 1.2 字号非标准

**现象**：英文题目字号 20pt，不符合二号(22pt)或小二号(18pt)。

**根因**：模板默认值或手动调整偏离标准。

**检测**：检查 `\fontsize{##pt}{##pt}` 第一个参数。

**修复**：正常长度用 `\fontsize{22pt}`，较长用 `\fontsize{18pt}`（小二号）。

### 1.3 未指定 Times New Roman

**现象**：英文题目显示为默认衬线字体而非 Times New Roman。

**根因**：未使用 `\timesfont` 命令。

**修复**：在 `\textbf` 前添加 `\timesfont`：
```latex
\centering{\fontsize{18pt}{80pt}\selectfont\timesfont\textbf{...}}
```

---

## 2. 中英文摘要

### 2.1 中文关键词多空4个半角不准确

**现象**：标题间距偏大或偏小。

**根因**：`\zhspace` 参数不对。`\zhspace[2]`=2 个汉字宽度=4 个半角空格。

**检测**：检查 `\ouc@chapter` 第二个参数中的 `\zhspace` 值。

### 2.2 英文关键词冒号用全角

**现象**：`Key Words：xxx`（全角冒号 U+FF1A）。

**根因**：中文输入法残留。

**检测**：检查 `abstract_en.tex` 中 "Key Words" 后的字符。

**修复**：
```latex
% 错误
\noindent\textbf{Key Words：}gastrointestinal cancer; ...

% 正确
\noindent\textbf{Key Words: }gastrointestinal cancer; ...
```
注意：(1) 冒号改为英文半角 `:` (2) 冒号后加一个半角空格。

### 2.3 英文关键词每个单词首字母大写

**现象**：关键词 `Gastrointestinal Cancer; Multi-instance Learning; ...`

**根因**：Word 自动大写习惯。OUC 标准：仅专有名词首字母大写，其余小写。

**修复**：
```
gastrointestinal cancer; multi-instance learning; microsatellite
instability prediction; cellular microenvironment; spatial graph attention
```

### 2.4 关键词行有悬挂缩进

**现象**：关键词第二行及后续行向右缩进，与第一行不对齐。

**根因**：摘要环境末尾的 `\hangindent=4\ccwd` 施加于关键词段落。

**检测**：搜索 `.cls` 文件中 `\end{abstract}` 和 `\end{enabstract}` 代码块。

**修复**：将 `\noindent\hangindent=4\ccwd\relax` 改为 `\noindent\relax`。

---

## 3. 目录

### 3.1 下级标题缩进不足

**现象**：节目录缩进仅 1 个汉字宽度，子节目录缩进仅 2 个汉字宽度。

**根因**：`.cls` 中 `\titlecontents` 的缩进参数偏小。

**检测**：检查 `\titlecontents{section}` 和 `\titlecontents{subsection}` 的第一个参数。

**修复**：
```latex
% 错误
\titlecontents{section}[\ccwd]{...}      % 1汉字
\titlecontents{subsection}[2\ccwd]{...}  % 2汉字

% 正确：下级比上级多2汉字宽度
\titlecontents{section}[2\ccwd]{...}     % 比章(0)多2汉字
\titlecontents{subsection}[4\ccwd]{...}  % 比节(2ccwd)多2汉字
```

### 3.2 目录条目段前间距缺失

**现象**：目录条目之间过于紧凑。

**根因**：`\titlecontents` 未设置段前间距。

**修复**：在 `\titlecontents` 的第二个参数中添加 `\addvspace{6bp}`。

### 3.3 TOC 中"摘要""致谢"空格过大

**现象**：目录中"摘  要""致  谢"中间有大片空白。

**根因**：`\chapter` 的 TOC 可选参数使用了 `\hspace{46pt}` 等大间距。

**检测**：搜索 `.cls` 中 `\ouc@chapter[摘` 和 `\ouc@chapter[致`。

**修复**：
```latex
% 错误（TOC 参数）
\ouc@chapter[摘\hspace{46pt}要]{摘\zhspace[2]要}

% 正确（TOC 参数不含多余空格）
\ouc@chapter[摘要]{摘\zhspace[2]要}
```
页面标题（第二个参数）保持 `\zhspace[2]` 即可。

---

## 4. 页边距/页眉/页码

### 4.1 页边距偏大

**现象**：上下边距 30mm，左右 27.2mm，不符合全校 25mm 标准。

**根因**：模板 `.cls` 的 geometry 设置沿用旧值。

**检测**：检查 `\geometry{vmargin=..., hmargin=...}`。

**修复**：
```latex
\geometry{
  paper      = a4paper,
  vmargin    = {25mm, 25mm},
  hmargin    = 25mm,
  ...
}
```

### 4.2 页脚边距不足

**现象**：页码距页底太近(11mm)或太远。

**根因**：`footskip` 设置不当。标准 15mm。

**修复**：`footskip=15mm`。

### 4.3 页眉距页顶偏大

**现象**：页眉文字位置偏下，超出 15mm 标准。

**根因**：`vmargin` + `headheight` + `headsep` 组合不当。

**修复**：配合 25mm 上边距，`headheight=15mm` + `headsep≈0.5cm` 可使页眉距顶约 15mm。

---

## 5. 章/节/子节标题

### 5.1 题序与标题间距错误

**现象**：标题中"第一章"与标题文字之间间距过大或过小。

**根因**：`aftername` 值不正确。标准为 1 个半角空格 = 0.5ccwd。

**检测**：检查 `\ctexset{chapter/aftername=...}`。

**修复**：
```latex
aftername = \zhspace[0.5]   % 0.5汉字宽 = 1半角空格
```
前置部分 chapter 也需统一（当前默认 `\hspace{\ccwd}`=2半角，仅影响有编号的章节，无编号时无影响）。

### 5.2 节/子节标题未用黑体

**现象**：标题显示为宋体而非黑体。

**根因**：`format` 中缺少 `\sffamily`（ctexbook 中 `\sffamily`→黑体）。

**检测**：检查 `\ctexset{section/format=..., subsection/format=...}`。

**修复**：确保 `format` 以 `\sffamily` 开头。

---

## 6. 图

### 6.1 图标题段前/段后间距不对

**现象**：图与标题间距 5pt，标题后无间距。

**根因**：`skip=5pt` 且未设 `\belowcaptionskip`。

**修复**：
```latex
\captionsetup[figure]{
    skip=6pt,                      % 段前6磅
    ...
}
\setlength{\belowcaptionskip}{6pt} % 段后6磅
```

### 6.2 图序与图题间距过大

**现象**：`labelsep=space` + caption 文本中的 `\quad` 产生双重间距。

**根因**：label 自动分隔 + 手动 `\quad` 叠加。

**修复**：设 `labelsep=none`，仅靠 caption 文本中的 `\quad`(≈1em≈2半角空格) 提供间距：
```latex
\captionsetup[figure]{labelsep=none, ...}
```

---

## 7. 表

### 7.1 英文表序用 "Table." 而非 "Tab."

**现象**：表英文标题显示 "Table. 3-1 ..." 而非 "Tab. 3-1 ..."

**根因**：OUC 标准明确要求英文表序用 **Tab.** 缩写。

**修复**：
```latex
% 错误
\centering \textnormal{Table.}~\thetable\quad #2

% 正确
\centering \textnormal{Tab.}~\thetable\quad #2
```

### 7.2 表标题置于表下方

**现象**：表标题出现在表格下面而非上面。

**根因**：`\captionsetup[table]{position=below}`，OUC 标准要求"表序和表题应置于表上方"。

**修复**：
```latex
\captionsetup[table]{position=above, ...}
\setlength{\abovecaptionskip}{6pt}  % 标题段前6磅
```

### 7.3 表标题段前/段后间距

**注意**：当 `position=above` 时，`skip` 是标题与表格之间的间距（段后），`\abovecaptionskip` 是标题上方的间距（段前）。与 `position=below` 的含义不同。

```latex
\captionsetup[table]{
    position=above,
    skip=6pt,                       % 标题与表格间(段后)
}
\setlength{\abovecaptionskip}{6pt}  % 标题上方(段前)
```

### 7.4 三线表线条粗细错误

**现象**：顶线/底线与栏目线粗细一样，或颠倒。

**根因**：`\toprule`/`\midrule`/`\bottomrule` 参数不对。

**修复**：
```latex
\toprule[1.5pt]     % 顶线粗1.5磅
\midrule[0.5pt]     % 栏目线细0.5磅
\bottomrule[1.5pt]  % 底线粗1.5磅
```

### 7.5 表格末页双底线

**现象**：longtable 最后一页出现两条底线。

**根因**：同时使用了 `\endfoot` 和表格尾部的 `\bottomrule`，末页两者叠加。

**修复**：添加 `\endlastfoot`：
```latex
\bottomrule[1.5pt]
\endfoot           % 非末页表底
\bottomrule[1.5pt]
\endlastfoot       % 末页表底（仅最后一页出现）
```

---

## 8. 公式

### 8.1 行内公式过长溢出

**现象**：长行内公式（$...$）超出页面右边界，log 中出现 `Overfull \hbox`。

**根因**：LaTeX 行内数学模式不自动换行。

**检测**：`grep 'Overfull.*section' main.log`

**修复**：
- 若公式紧跟段落末尾的"："，可考虑改为行间公式 `\begin{equation}`
- 大长行内公式可拆分为多个短公式，用文字衔接
- 在合适位置手动添加 `\allowbreak`

### 8.2 行间公式段前空白过大

**现象**：`\begin{equation}` 前方有大片空白。

**根因**：`\abovedisplayskip` 默认 12bp，叠加段落空行。

**修复**：
```latex
\setlength{\abovedisplayskip}{0pt}
\setlength{\abovedisplayshortskip}{0pt}
```
同时在生成 `.tex` 时去掉 `\begin{equation}` 前的空行。

### 8.3 公式编号括号为半角

**现象**：公式编号显示为 (3-1) 而非（3-1）。

**根因**：未启用全角括号 tag form。

**修复**：
```latex
\newtagform{test}[]{（}{）}
\usetagform{test}
```

### 8.4 公式中花括号丢失

**现象**：正文中出现 `Too many }'s` 错误，或花括号不显示。

**根因**：正文中 `{` `}` 被 LaTeX 当作分组命令。

**修复**：在正文（非数学模式）中，`{` → `\{`，`}` → `\}`。

---

## 9. 参考文献

### 9.1 BibTeX 键名含空格

**现象**：BibTeX 报 `I was expecting ',' or '}'` 错误。

**根因**：`.bib` 文件中引用键名包含空格（如 `1+Author Name`）。

**修复**：键名中空格替换为连字符：`1+Author-Name`。使用 `fix_citations.py` 批量修复。

### 9.2 DOI 下划线触发数学模式

**现象**：`Missing $ inserted` 错误出现在 `\doi{10.1007/978-3-030-40245-7_10}`。

**根因**：DOI 中的 `_` 在 LaTeX 文本模式中需转义。

**修复**：
```latex
\providecommand{\doi}[1]{\href{https://doi.org/#1}{\detokenize{#1}}}
```
`\detokenize` 将 `_` 转为安全字面字符。

### 9.3 参考文献标签格式不对

**现象**：参考文献列表使用 `1.` 而非 `[1]` 标签。

**根因**：`\@biblabel` 定义不符合国标。

**修复**：
```latex
\renewcommand\@biblabel[1]{[#1]\hfill}
```

---

## 10. 特殊字符转义

以下字符在 LaTeX 正文中必须转义：

| 字符 | LaTeX 命令 | 典型场景 | 未转义后果 |
|------|-----------|----------|-----------|
| `%` | `\%` | 百分号 70% | 后续文本被注释 |
| `&` | `\&` | 缩写 H&E | `Misplaced alignment tab` |
| `_` | `\_` | 文件名 file_name | `Missing $ inserted` |
| `#` | `\#` | 编号 #1 | `Illegal parameter number` |
| `$` | `\$` | 金额 $100 | 意外进入数学模式 |
| `~` | `\textasciitilde{}` | 范围 10%~20% | 不可断行空格 |
| `{` `}` | `\{` `\}` | 集合 K∈{2,3,5} | `Too many }'s` |

**检测**：使用 `scan_special_chars.py` 或 `check_tex_files.py` 扫描未转义字符。

---

## 11. Python re.sub 转义字符陷阱

**现象**：`\figurecaption` → `igurecaption`（反斜杠丢失），图表索引全部消失。

**根因**：Python `re.sub` 将替换字符串中 `\f` 解释为换页符(0x0C)，`\t` 解释为制表符(0x09)。

**检测**：检查 `.tex` 文件中 `igurecaption`/`ablecaption`（或直接用 hex dump 查看 `0c` 字节）。

**修复**：替换字符串中双写反斜杠：
```python
# 错误
re.sub(..., r'\figurecaption{...}{...}{English}')

# 正确
re.sub(..., r'\\figurecaption{...}{...}{English}')
```

---

## 12. 浮动体排版

### 12.1 表格独占整页

**现象**：表格单独占据一页，上下有大片空白。

**根因**：`[!htbp]` 中的 `!` 强制覆盖 LaTeX 浮动约束，可能适得其反。

**修复**：改用 `[htbp]`（去掉 `!`）。

### 12.2 行间公式后段落首行缩进

**现象**：`\end{equation}` 下一段"其中...""这里..."等续文产生首行缩进。

**根因**：公式与续文之间的空行使 LaTeX 将其视为新段落。

**修复**：去掉 `\end{equation}` 与"其中/这里/式中/此时/该/此"等连接词之间的空行。

---

## 13. 目录/索引相关

### 13.1 公式编号使用 `$$` 导致无法编号

**现象**：行间公式无编号。

**根因**：使用 `$$...$$` 而非 `\begin{equation}...\end{equation}`。

**修复**：全部行间公式改用 `equation` 环境。

### 13.2 图表索引条目丢失

**现象**：插图清单/表格清单为空。

**根因**：(1) 使用了不含 `\caption` 的图片/表格 (2) `\caption` 前有空行。

**修复**：确保所有图表使用 `\caption`（或封装的 `\figurecaption`/`\tablecaption`）。

---

## 14. URL 与长字符串

### 14.1 URL 超出页面

**现象**：长网址在 PDF 中越过右边界。

**修复**：
```latex
\url{https://www.cbioportal.org}
```
`\url{}` 可自动换行。不要裸露 URL。

### 14.2 长英文单词不换行

**现象**：长英文专业术语或序列号超出右边界。

**修复**：使用 `\seqsplit{}`（需 `\usepackage{seqsplit}`）。
