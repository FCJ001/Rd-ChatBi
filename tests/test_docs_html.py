# ============================================================
# 文档 HTML 一致性测试
#
# docs/*.md 会转换成同名 .html 供浏览器阅读。风险是「改了 md 忘了重新
# 生成」—— HTML 悄悄过期，读的人拿到旧内容却不自知。
# 这里锁住两件事：
#   1. 每个 md 都有对应 html，且 html 比 md 新（没被落下）
#   2. html 里确实渲染出了预期的结构（不是空壳）
#
# 生成命令：npm run docs
#
# ★ 覆盖边界（别高估这组测试）：
#   - 本地有效：改了 md 忘了重新生成 → 时间戳断言会红
#   - **CI 无效**：fresh checkout 把所有文件 mtime 设成同一时刻，
#     `html >= md` 恒真。CI 只验证结构完整性（文件在、图在、没退化成空壳）。
#     过期问题靠本地跑测试 + code review 兜。
# ============================================================

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# md → html（architecture 的 HTML 是手工查看器，生成物带 _print 后缀）
DOCS = [
    (REPO / "README.md", REPO / "README.html"),
    (REPO / "INTERVIEW.md", REPO / "INTERVIEW.html"),
    (REPO / "SPEAK.md", REPO / "SPEAK.html"),
    (REPO / "docs/diagrams/architecture.md", REPO / "docs/diagrams/architecture_print.html"),
]


def test_doc_pairs_exist():
    for md, html in DOCS:
        assert md.exists(), f"源文件缺失: {md}"
        assert html.exists(), f"HTML 未生成（跑 npm run docs）: {html}"


@pytest.mark.parametrize("md,html", DOCS, ids=[m.name for m, _ in DOCS])
def test_html_not_stale(md: Path, html: Path):
    """★ HTML 不能比 md 旧：改了 md 没重新生成是最容易犯的错"""
    assert html.stat().st_mtime >= md.stat().st_mtime, (
        f"{html.name} 比 {md.name} 旧，HTML 已过期 —— 跑 npm run docs"
    )


def test_interview_html_has_diagrams():
    """INTERVIEW 的 4 张图必须真的进了 HTML（且以 mermaid 容器形式）"""
    t = (REPO / "INTERVIEW.html").read_text(encoding="utf-8")
    blocks = re.findall(r'<div class="mermaid">(.*?)</div>', t, re.S)
    assert len(blocks) == 5, f"期望 5 张 mermaid 图，实际 {len(blocks)}"
    # ★ 图 0 必须是横向总览：第一张要是 TB 的细节图，读者一上来就淹在细节里
    assert "flowchart LR" in blocks[0], "图 0 总览应为横向（LR）"
    assert "自然语言提问" in blocks[0], "图 0 内容不像总览"
    assert "sequenceDiagram" in blocks[4], "最后一张应为 SSE 时序图"


def test_interview_html_structure():
    """结构完整性：标题锚点、表格、代码块都在（防生成器退化成只输出正文）"""
    t = (REPO / "INTERVIEW.html").read_text(encoding="utf-8")
    assert len(re.findall(r"<h[1-4] id=", t)) > 30, "标题锚点太少"
    assert len(re.findall(r"<table>", t)) >= 5, "表格丢了"
    assert len(re.findall(r"<pre>", t)) >= 20, "代码块丢了"
    # 目录里的 <a name> 锚点必须保留（浏览器能解码 fragment 后匹配，已实测）
    assert len(re.findall(r'<a name="', t)) >= 5, "目录锚点丢了"


def test_mermaid_source_roundtrips_to_original():
    """★ mermaid 源码必须与 md 原文逐字一致（转义后再反转义要能还原）。

    比"检查有没有 &gt;"准确：mermaid 的箭头 `-->` 本来就**必须**转义成
    `--&gt;`，否则会被浏览器当标签解析、图直接渲染失败。所以这里验证的是
    往返一致性，而不是某个字符有没有出现。"""
    import html

    md = (REPO / "INTERVIEW.md").read_text(encoding="utf-8")
    src_blocks = [b.strip() for b in re.findall(r"```mermaid\n(.*?)```", md, re.S)]

    t = (REPO / "INTERVIEW.html").read_text(encoding="utf-8")
    html_blocks = [html.unescape(b.strip()) for b in
                   re.findall(r'<div class="mermaid">(.*?)</div>', t, re.S)]

    assert len(src_blocks) == len(html_blocks) == 5
    for i, (a, b) in enumerate(zip(src_blocks, html_blocks), 1):
        assert a == b, f"图 {i} 与 md 原文不一致（转义破坏了内容）"


def test_readme_html_has_no_raw_markdown():
    """粗转义检查：表格分隔行 |---|---| 不该原样出现在 HTML 正文里"""
    t = (REPO / "README.html").read_text(encoding="utf-8")
    assert "|---|---|" not in t, "Markdown 表格未被解析"
    assert "<table>" in t


# ════════════════════════════════════════════════════════════════
# 前端页面完整性（chatbi.html / review.html）
#
# 两个页面都是单文件、无构建，改了 JS 语法错误只有在浏览器里才会暴露 ——
# 这里做最小静态检查，把「渲染出来是白屏」挡在合并前。
# ════════════════════════════════════════════════════════════════

PAGES = [REPO / "src/static/chatbi.html", REPO / "src/static/review.html"]


@pytest.mark.parametrize("page", PAGES, ids=[p.name for p in PAGES])
def test_page_is_wellformed(page: Path):
    t = page.read_text(encoding="utf-8")
    assert t.lstrip().startswith("<!DOCTYPE html>")
    assert t.rstrip().endswith("</html>")
    # <script> / </script> 必须成对（标签写漏 = 整页变白屏）
    assert t.count("<script") == t.count("</script>")
    # 内联脚本恰好一个（页面自带的那段）；echarts 之类的外部脚本另算
    assert t.count("<script>") == 1, "内联 <script> 应恰好一个"


@pytest.mark.parametrize("page", PAGES, ids=[p.name for p in PAGES])
def test_page_escapes_user_content(page: Path):
    """★ 两个页面都靠转义函数防 XSS（问题/SQL 都是用户可控内容）。

    之前 SQL 段踩过的坑：escHtml 只保护 HTML 结构，保护不了内联事件处理器
    属性里的 JS —— HTML 实体在属性值解析阶段还原成引号，随后就能逃逸
    JS 字符串字面量。所以动态内容一律走 data-* + addEventListener。

    ★ 静态 HTML 里给固定元素挂的事件属性（如 select 的 onchange）不在此列：
      那些属性值里没有任何插值，不构成注入面。这里锁的是「模板串里带事件属性」
      这个真正的危险形态。"""
    t = page.read_text(encoding="utf-8")
    assert re.search(r"(function\s+esc\w*|const\s+esc\w*\s*=)", t), "缺少转义函数"
    assert "addEventListener" in t, "应使用 addEventListener 而非内联事件"

    # JS 模板串（反引号内）里不能出现 on*=" 事件属性 —— 那是插值逃逸的入口
    for tpl in re.findall(r"`[^`]*`", t):
        assert not re.search(r"\son[a-z]+\s*=", tpl), (
            f"{page.name}: 模板串里出现内联事件属性（应改用 data-* + addEventListener）:\n"
            f"{tpl[:200]}"
        )
