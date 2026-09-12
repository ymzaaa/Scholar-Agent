# OUC 硕士学位论文格式规范速查表

> 适用边界：本文档仅是 OUC 模板画像的规范来源，不是 Scholar Agent 的全局格式标准。运行时由 OUC 模板适配器选择；其他模板必须提供自己的规则画像。

本文档提炼 OUC 学位论文格式要求，逐条列出检测项、标准值和对应的 LaTeX 检查位置，用于 Word→LaTeX 迁移后的格式自检。

---

## 1. 英文题目

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 1.1 | 第一个单词 | 首字母大写 | `data/cover.tex` → `\textbf{...}` |
| 1.2 | 实词（名/动/代/形/副） | 首字母大写 | 逐词核对 |
| 1.3 | 虚词（介/冠/连）≤5字母 | 首字母小写（and, for, in, on, at, of, to, a, an, the） | 逐词核对 |
| 1.4 | 虚词 >5字母 | 首字母大写（Between, Without, Through） | 逐词核对 |
| 1.5 | 字体 | Times New Roman | `\timesfont` 或 `\setmainfont` |
| 1.6 | 字号 | 二号(22pt)加粗，太长可用小二号(18pt) | `\fontsize{##pt}{##pt}\selectfont\textbf{...}` |
| 1.7 | 专有缩写 | 保持全大写（MSI, TCGA, DNA 等） | 逐词核对 |

---

## 2. 中文摘要

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 2.1 | 标题字体 | 黑体三号(≈16pt) | `\sffamily\fontsize{16bp}` |
| 2.2 | 标题对齐 | 居中，无缩进 | `\centering` |
| 2.3 | "摘要"字间距 | 4个半角空格 | `\zhspace[2]` 或 `\hspace{4em}` |
| 2.4 | 标题段前 | 24磅 | `beforeskip=24bp` |
| 2.5 | 标题段后 | 18磅 | `afterskip=18bp` |
| 2.6 | 标题行距 | 单倍 | `\fontsize{16bp}{26.67bp}` 或 `\baselineskip` |
| 2.7 | 内容字体 | 宋体小四号(≈12pt) | ctexbook 默认宋体 |
| 2.8 | 内容对齐 | 两端对齐，首行缩进2字符 | `\parindent=2\ccwd` |
| 2.9 | 内容段前/段后 | 0行 | `\parskip=0pt` |
| 2.10 | 内容行距 | 固定值20磅 | `\baselineskip=20pt` |
| 2.11 | 关键词标签 | "关键词："宋体小四加粗，顶格 | `\noindent\textbf{关键词：}` |
| 2.12 | 关键词数量 | 3—5个 | 逐词计数 |
| 2.13 | 关键词分隔 | 分号分开，末尾无标点 | `；` 分隔，最后无标点 |
| 2.14 | 关键词缩进 | 无缩进 | `\noindent`（注意：无 `\hangindent`） |

---

## 3. 英文摘要

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 3.1 | 标题字体 | Times New Roman 三号(≈16pt) 加粗 | `\bfseries\timesfont\fontsize{16bp}` |
| 3.2 | 标题对齐 | 居中，无缩进 | `\centering` |
| 3.3 | 标题段前 | 24磅 | `beforeskip=24bp` |
| 3.4 | 标题段后 | 18磅 | `afterskip=18bp` |
| 3.5 | 标题行距 | 单倍 | 同 chapter 格式 |
| 3.6 | 内容字体 | 小四号(≈12pt) | `\normalsize` |
| 3.7 | 内容对齐 | 两端对齐，首行缩进2字符 | `\parindent=2\ccwd` |
| 3.8 | 内容段前/段后 | 0行 | `\parskip=0pt` |
| 3.9 | 内容行距 | 固定值20磅 | `\baselineskip=20pt` |
| 3.10 | 关键词标签 | "Key Words: " 小四加粗顶格（半角冒号+空格） | `\noindent\textbf{Key Words: }` |
| 3.11 | 关键词大小写 | 专有名词首字母大写，其余均小写 | 逐词核对 |
| 3.12 | 关键词标点 | 英文半角标点，标点后空一个半角空格 | `; ` 分隔 |
| 3.13 | 关键词末尾 | 无标点 | 最后无标点 |

---

## 4. 目录

### 4.1 中文目录

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 4.1.1 | 标题字体 | 黑体三号(≈16pt) | `\sffamily\fontsize{16bp}` |
| 4.1.2 | "目录"字间距 | 4个半角空格 | `\zhspace[2]` |
| 4.1.3 | 标题段前 | 24磅 | `beforeskip=24bp` |
| 4.1.4 | 标题段后 | 18磅 | `afterskip=18bp` |
| 4.1.5 | 章目录字体 | 宋体四号(≈14pt) | `\titlecontents{chapter}` → `\fontsize{14bp}` |
| 4.1.6 | 节目录字体 | 宋体小四号(≈12pt) | `\titlecontents{section}` → `\normalsize` |
| 4.1.7 | 子节目录字体 | 宋体小四号(≈12pt) | `\titlecontents{subsection}` → `\normalsize` |
| 4.1.8 | 对齐 | 两端对齐 | 默认 |
| 4.1.9 | 段前 | 6磅 | `\titlespacing` 或 `\addvspace{6bp}` |
| 4.1.10 | 段后 | 0磅 | 默认 |
| 4.1.11 | 行距 | 单倍 | `\fontsize{##bp}{##bp}` 的第二个参数 |
| 4.1.12 | 缩进 | 下级比上级左边多2汉字宽度 | 节比章+2ccwd，子节比节+2ccwd |
| 4.1.13 | 目录深度 | 列至三级标题 | 确保 subsection 在 TOC 中 |
| 4.1.14 | 特殊处理 | 去掉"摘要""致谢"中多余空格 | `\chapter[摘要]{摘\zhspace[2]要}` → TOC 用 `摘要` |

### 4.2 英文目录

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 4.2.1 | 标题 | 三号加粗，居中 | `\bfseries\timesfont\fontsize{16bp}` |
| 4.2.2 | 章目录字体 | 四号(≈14pt) | 同中文目录 |
| 4.2.3 | 其他字体 | 小四号(≈12pt) | 同中文目录 |
| 4.2.4 | 其他格式 | 同中文目录 | 同中文目录 |

---

## 5. 页边距、页眉、页码

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 5.1 | 上边距 | 25mm | `\geometry{vmargin={25mm,25mm}}` |
| 5.2 | 下边距 | 25mm | 同上 |
| 5.3 | 左边距 | 25mm | `\geometry{hmargin=25mm}` |
| 5.4 | 右边距 | 25mm | 同上 |
| 5.5 | 页眉起始 | 正文开始（封面/声明/答辩委员会无页眉） | `\mainmatter` → `\pagestyle{title}` |
| 5.6 | 奇数页页眉 | "中国海洋大学硕（博）士学位论文" | `\fancyhead[CO]{...}` |
| 5.7 | 偶数页页眉 | 学位论文题目 | `\fancyhead[CE]{\@title}` |
| 5.8 | 页眉字体 | 宋体五号(≈10.5pt) 居中 | `\fontsize{10.5bp}{12bp}` |
| 5.9 | 页眉下划线 | 有（约0.4pt） | `\headrulewidth=0.4pt` |
| 5.10 | 页眉边距 | 15mm | `headheight=15mm` + `headsep` 配合 |
| 5.11 | 页码起始 | 摘要开始编排 | `\pagenumberingnoreset{Roman}` |
| 5.12 | 前置页码 | 大写罗马数字(Ⅰ,Ⅱ,Ⅲ…) | `\pagenumberingnoreset{Roman}` |
| 5.13 | 正文页码 | 阿拉伯数字(1,2,3…) | `\pagenumbering{arabic}` |
| 5.14 | 页码位置 | 页脚居中 | `\fancyfoot[C]{\thepage}` |
| 5.15 | 页码修饰 | 不加"-"等修饰线 | 仅 `\thepage` |
| 5.16 | 页脚边距 | 15mm | `footskip=15mm` |

---

## 6. 章/节/子节标题与正文

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 6.1 | 章标题字体 | 黑体三号(≈16pt) | `\ctexset{chapter/format=\sffamily\fontsize{16bp}}` |
| 6.2 | 章标题对齐 | 居中，无缩进 | `\centering` |
| 6.3 | 章标题段前 | 24磅 | `beforeskip=24pt` |
| 6.4 | 章标题段后 | 18磅 | `afterskip=18pt` |
| 6.5 | 章标题行距 | 单倍 | 字体声明的 baseline |
| 6.6 | 题序与标题间距 | 1个半角空格 | `aftername=\zhspace[0.5]` |
| 6.7 | 每章另起页 | 是 | `\clearpage` |
| 6.8 | 节标题字体 | 黑体四号(≈14pt) | `\ctexset{section/format=\sffamily\fontsize{14bp}}` |
| 6.9 | 节标题对齐 | 两端对齐，无缩进 | 无 `\centering` |
| 6.10 | 节标题段前 | 24磅 | `beforeskip=24pt` |
| 6.11 | 节标题段后 | 6磅 | `afterskip=6pt` |
| 6.12 | 节标题行距 | 单倍 | baseline |
| 6.13 | 节标题题序间距 | 1个半角空格 | `aftername=\zhspace[0.5]` |
| 6.14 | 子节标题字体 | 黑体小四号(≈12pt) | `\ctexset{subsection/format=\sffamily\fontsize{12bp}}` |
| 6.15 | 子节标题对齐 | 两端对齐，无缩进 | `indent=\z@` |
| 6.16 | 子节标题段前 | 12磅 | `beforeskip=12pt` |
| 6.17 | 子节标题段后 | 6磅 | `afterskip=6pt` |
| 6.18 | 正文中文字体 | 宋体小四号(≈12pt) | ctexbook 默认 |
| 6.19 | 正文英文字体 | Times New Roman | `\setmainfont{TeX Gyre Termes}` |
| 6.20 | 正文对齐 | 两端对齐 | 默认 |
| 6.21 | 正文缩进 | 首行缩进2字符 | `\parindent=2\ccwd` |
| 6.22 | 正文段前/段后 | 0行 | `\parskip=0pt` |
| 6.23 | 正文行距 | 固定值20磅 | `\baselineskip=20pt` |
| 6.24 | 引文 | 可用楷体 | `\kaishu` 或 `\textit` |

---

## 7. 公式

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 7.1 | 行间公式 | 另起一行，居中 | `\begin{equation}` |
| 7.2 | 编号 | 分章编号(章-序号) | `\theequation=\arabic{chapter}-\arabic{equation}` |
| 7.3 | 编号括号 | 全角圆括号（） | `\newtagform{test}[]{（}{）}` |
| 7.4 | 编号位置 | 右端对齐，不加虚线 | LaTeX 默认 |
| 7.5 | 续行编号 | 编号在最后一行 | 使用 `split`/`multline` 环境 |
| 7.6 | 长公式回行 | 在=处回行，其次在+/-/×//后 | 手动 `\\` 或 `\allowbreak` |
| 7.7 | 公式内文字号 | 小四号 | 继承 `\normalsize` |
| 7.8 | 公式内中文 | 宋体 | ctexbook 默认 |
| 7.9 | 公式内符号/英文/数字 | Times New Roman | `\setmathfont{TeX Gyre Termes Math}` |
| 7.10 | 段前间距 | 不过大 | `\abovedisplayskip=0pt` |

---

## 8. 图

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 8.1 | 图序编号 | 阿拉伯数字分章，如"图3-2" | `\thefigure=\thechapter-\arabic{figure}` |
| 8.2 | 英文编号 | "Fig. 1-1" 格式 | `Fig.~\thefigure` |
| 8.3 | 图序+图题字体 | 宋体五号(≈10.5pt)，居中无缩进 | `\captionsetup[figure]{font={small}}` |
| 8.4 | 段前 | 6磅 | `skip=6pt` |
| 8.5 | 段后 | 6磅 | `\belowcaptionskip=6pt` |
| 8.6 | 行距 | 单倍 | `font={small}` 的 baseline |
| 8.7 | 图序-图题间距 | 2个半角空格(≈1em) | `labelsep`+间距控制 |
| 8.8 | 图题末尾 | 不加标点 | 无标点 |
| 8.9 | 图注字体 | 宋体五号，两端对齐，首行缩进2字符 | `\small` |
| 8.10 | 图注段前/段后 | 6磅 | 手动设置 |

---

## 9. 表

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 9.1 | 表序编号 | 阿拉伯数字分章，如"表3-1" | `\thetable=\thechapter-\arabic{table}` |
| 9.2 | 英文编号 | **"Tab. 1-1"**（非 Table.） | `Tab.~\thetable` |
| 9.3 | 标题位置 | **表上方** | `position=above` |
| 9.4 | 表序+表题字体 | 宋体五号(≈10.5pt)，居中无缩进 | `font={small}` |
| 9.5 | 段前 | 6磅 | `\abovecaptionskip=6pt` |
| 9.6 | 段后 | 6磅 | `skip=6pt` |
| 9.7 | 行距 | 单倍 | baseline |
| 9.8 | 表序-表题间距 | 2个半角空格(≈1em) | `labelsep`+间距控制 |
| 9.9 | 表题末尾 | 不加标点 | 无标点 |
| 9.10 | 表格宽度 | 建议通栏（与正文版心平齐） | `\textwidth` |
| 9.11 | 表内字体 | 宋体五号居中 | `\zihao{5}\songti` |
| 9.12 | 表内行高 | ≈0.8cm | `\arraystretch{1.6}` |
| 9.13 | 顶线/底线 | 粗线 1.5pt | `\toprule[1.5pt]` / `\bottomrule[1.5pt]` |
| 9.14 | 栏目线 | 细线 0.5pt | `\midrule[0.5pt]` |
| 9.15 | 长表跨页 | 续表标记"续表X-X"，重复表头 | longtable `\endhead`/`\endfirsthead` |
| 9.16 | 表注字体 | 宋体五号，两端对齐，首行缩进2字符 | `\small` |
| 9.17 | 表注段前/段后 | 6磅 | 手动设置 |

---

## 10. 参考文献

| # | 检测项 | 标准值 | LaTeX 检查位置 |
|---|--------|--------|---------------|
| 10.1 | 标题字体 | 黑体三号(≈16pt)，居中 | chapter 格式 |
| 10.2 | 标题段前 | 24磅 | `beforeskip=24pt` |
| 10.3 | 标题段后 | 18磅 | `afterskip=18pt` |
| 10.4 | 文本字体 | 宋体五号(≈10.5pt) | `\bibfont=\fontsize{10.5bp}` |
| 10.5 | 文本对齐 | 两端对齐 | 默认 |
| 10.6 | 悬挂缩进 | 2字符 | `\bibhang=2\ccwd` |
| 10.7 | 段前/段后 | 0行 | `\bibsep=0pt` |
| 10.8 | 行距 | 固定值20磅 | baseline=20bp |
| 10.9 | 著录标准 | GB/T 7714-2015 | `\bibliographystyle` + `.bib` 条目格式 |

---

## 附录：字号对照

| 中文字号 | 磅值(pt) | bp 近似值 | 用途 |
|----------|----------|-----------|------|
| 二号 | 22pt | 22bp | 英文题目（正常长度） |
| 小二号 | 18pt | 18bp | 英文题目（较长时） |
| 三号 | 16pt | 16bp | 章标题、摘要/目录/参考文献标题 |
| 四号 | 14pt | 14bp | 节标题、章目录 |
| 小四号 | 12pt | 12bp | 正文、子节标题、节目录 |
| 五号 | 10.5pt | 10.5bp | 图表标题、表内文字、参考文献文本、页眉 |
