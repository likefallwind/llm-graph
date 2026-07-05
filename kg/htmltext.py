"""HTML -> 纯文本提取（stdlib，无第三方依赖）。

给 docs 通道抓教材页面用：跳过导航/脚本/代码块，优先取 <main>/<article> 区域，
输出压缩过空行的正文文本。
"""
from html.parser import HTMLParser

# 整棵子树丢弃的标签：页面骨架 + 代码块（代码对概念提取无用，且是 evidence 噪声）
_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "pre", "code",
              "svg", "button", "form", "noscript"}
# 正文主区域标签：页面若有，只保留其中内容
_MAIN_TAGS = {"main", "article"}
# 块级标签：进出时补换行，保持段落结构
_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "div", "section",
               "blockquote", "tr", "table", "ul", "ol", "br"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces = []       # (是否在 main/article 内, 文本片段)
        self.skip_depth = 0
        self.main_depth = 0
        self.saw_main = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
        if tag in _MAIN_TAGS:
            self.main_depth += 1
            self.saw_main = True
        if tag in _BLOCK_TAGS:
            self.pieces.append((self.main_depth > 0, "\n"))

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        if tag in _MAIN_TAGS and self.main_depth:
            self.main_depth -= 1
        if tag in _BLOCK_TAGS:
            self.pieces.append((self.main_depth > 0, "\n"))

    def handle_data(self, data):
        if self.skip_depth == 0 and data:
            self.pieces.append((self.main_depth > 0, data))


def extract(html: str) -> str:
    """提取正文文本：有 <main>/<article> 则只取其中，否则取全文；压缩连续空行。"""
    parser = _TextExtractor()
    parser.feed(html)
    if parser.saw_main:
        raw = "".join(t for in_main, t in parser.pieces if in_main)
    else:
        raw = "".join(t for _, t in parser.pieces)
    raw = raw.replace("¶", "")  # sphinx 标题锚点符号
    lines, blank = [], False
    for line in raw.splitlines():
        line = line.strip()
        if line:
            lines.append(line)
            blank = False
        elif not blank and lines:
            lines.append("")
            blank = True
    return "\n".join(lines).strip()
