#!/usr/bin/env node
/* ============================================================
 * Markdown → 单文件 HTML 转换器（零仓库依赖，marked 走 npx）
 *
 * 用法：
 *   node scripts/md2html.mjs <输入.md> [更多.md ...]
 *   node scripts/md2html.mjs --all        # 转换 README / INTERVIEW / 架构图说明
 *
 * 产出：与源文件同目录、同名的 .html（自包含，无外链 CSS/JS）
 *
 * 设计取舍：
 *   - 不引入 markdown 库到 requirements/package.json：这是文档工具，
 *     不该让部署镜像为它多装依赖，所以 marked 走 npx 临时拉取。
 *   - mermaid 代码块改写成 <div class="mermaid">，由 CDN 的 mermaid 渲染，
 *     INTERVIEW.md 里的 4 张图在 HTML 里直接就是画好的。
 *   - 样式内联：仓库里没有静态资源托管，外链 CSS 会让 HTML 一旦离开仓库就废掉。
 * ============================================================ */

import { readFileSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { marked } from "marked";
import { gfmHeadingId } from "marked-gfm-heading-id";

const TARGETS = {
  "--all": ["README.md", "INTERVIEW.md", "SPEAK.md", "docs/diagrams/architecture.md"],
};

// ── 样式 ────────────────────────────────────────────────────
const CSS = `
:root {
  --bg:#f6f7f9; --card:#fff; --ink:#1f2328; --muted:#656d76;
  --line:#d8dee4; --accent:#0969da; --code-bg:#f0f2f5;
  --tbl-head:#f6f8fa; --quote:#d0d7de;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0d1117; --card:#161b22; --ink:#e6edf3; --muted:#8b949e;
    --line:#30363d; --accent:#58a6ff; --code-bg:#1c2128;
    --tbl-head:#1c2128; --quote:#3d444d;
  }
}
* { box-sizing: border-box; }
body {
  margin:0; padding:0;
  background:var(--bg); color:var(--ink);
  font:16px/1.75 -apple-system, BlinkMacSystemFont, "PingFang SC",
       "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap { max-width:960px; margin:0 auto; padding:48px 24px 120px; }

/* ── 标题 ── */
h1,h2,h3,h4 { line-height:1.35; font-weight:650; margin:2em 0 .7em; }
h1 { font-size:1.9em; margin-top:0; padding-bottom:.4em; border-bottom:1px solid var(--line); }
h2 { font-size:1.45em; padding-bottom:.35em; border-bottom:1px solid var(--line); }
h3 { font-size:1.18em; }
h4 { font-size:1.02em; color:var(--muted); }
h1+p,h2+p,h3+p { margin-top:.4em; }

/* ── 正文 ── */
p { margin:.85em 0; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
strong { font-weight:650; }
hr { border:0; border-top:1px solid var(--line); margin:2.5em 0; }
ul,ol { padding-left:1.6em; margin:.8em 0; }
li { margin:.3em 0; }
li > p { margin:.3em 0; }
img { max-width:100%; height:auto; border-radius:6px; }

/* ── 引用块：★ 提示都写在这里，要显眼但别刺眼 ── */
blockquote {
  margin:1.2em 0; padding:.6em 1.1em;
  border-left:4px solid var(--accent);
  background:var(--code-bg); border-radius:0 6px 6px 0;
  color:var(--ink);
}
blockquote > p:first-child { margin-top:.2em; }
blockquote > p:last-child { margin-bottom:.2em; }

/* ── 行内代码：中文文档里默认样式对比度太弱，单独加重 ── */
code {
  font-family:ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size:.875em; padding:.18em .42em;
  background:var(--code-bg); border:1px solid var(--line);
  border-radius:5px; white-space:nowrap;
}
/* 代码块里的 code 不再叠加边框/底色 */
pre {
  margin:1.2em 0; padding:16px 18px; overflow-x:auto;
  background:var(--code-bg); border:1px solid var(--line);
  border-radius:8px; line-height:1.55;
}
pre code {
  padding:0; background:none; border:0; white-space:pre;
  font-size:.86em; line-height:1.6;
}

/* ── 表格：Markdown 表格默认挤在一起，这里给足留白 ── */
table {
  border-collapse:collapse; width:100%; margin:1.4em 0;
  font-size:.94em; display:block; overflow-x:auto;
}
th,td {
  border:1px solid var(--line); padding:9px 13px;
  text-align:left; vertical-align:top;
}
th { background:var(--tbl-head); font-weight:650; white-space:nowrap; }
tr:nth-child(even) td { background:color-mix(in srgb, var(--code-bg) 45%, transparent); }

/* ── mermaid 图 ── */
.mermaid {
  margin:1.6em 0; padding:18px; text-align:center;
  /* ★ 固定白底：图内配色锁死浅色方案，背景就必须跟着白，
     否则深色模式下浅色节点会浮在深色画布上、边缘发灰 */
  background:#fff; border:1px solid var(--line); border-radius:10px;
  overflow-x:auto;
}
.mermaid svg { max-width:100%; height:auto; }

/* ── 返回顶部 ── */
#top {
  position:fixed; right:22px; bottom:22px;
  width:42px; height:42px; border-radius:50%;
  display:none; align-items:center; justify-content:center;
  background:var(--card); border:1px solid var(--line); color:var(--ink);
  cursor:pointer; font-size:17px; box-shadow:0 2px 10px rgba(0,0,0,.12);
}
#top.show { display:flex; }
#top:hover { border-color:var(--accent); color:var(--accent); }

@media print {
  body { background:#fff; }
  .wrap { max-width:none; padding:0; }
  #top { display:none !important; }
  pre,table,.mermaid { break-inside:avoid; }
}
`;

// ── 转换 ────────────────────────────────────────────────────
function slug(s) {
  return s.toLowerCase().replace(/[^\w一-龥]+/g, "-").replace(/^-|-$/g, "");
}

function build(src, outPath) {
  const raw = readFileSync(src, "utf8");

  // 先抽出所有 mermaid 块（fetch 前替换，避免 marked 把内容当代码转义）
  const mermaidBlocks = [];
  const withPlaceholders = raw.replace(
    /```mermaid\n([\s\S]*?)```/g,
    (_, code) => {
      const i = mermaidBlocks.push(code.trim()) - 1;
      return `\n\n<div class="mermaid">MMD${i}ENDMMD</div>\n\n`;
    },
  );

  marked.use({ gfm: true, breaks: false });
  marked.use(gfmHeadingId());

  let body = marked.parse(withPlaceholders);

  // 还原 mermaid（此时已过 marked，内容保持原样）
  mermaidBlocks.forEach((code, i) => {
    body = body.replace(
      new RegExp(`MMD${i}ENDMMD`, "g"),
      // mermaid 需要真实的换行，且 > / & 不能被转义
      `\n${code.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")}\n`,
    );
  });

  // 标题锚点：marked-gfm-heading-id 已给 id，这里补可点链接
  const title = (raw.match(/^#\s+(.+)$/m)?.[1] || basename(src)).trim();

  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${CSS}</style>
</head>
<body>
<main class="wrap">
${body}
</main>
<button id="top" title="返回顶部">↑</button>
<script type="module">
const hasMermaid = document.querySelector(".mermaid");
if (hasMermaid) {
  try {
    const mermaid = (await import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs")).default;
    // 不用 mermaid 内置的 default/dark 主题：default 在浅色底上是一整套
    // 低对比的浅紫填充，节点文字和底色几乎糊在一起（实测截图确认）。
    // 这里给两套显式配色，保证图表在两种模式下都可读。
    const light = {
      darkMode: false, background: "#ffffff", fontSize: "15px",
      primaryColor: "#dbeafe", primaryTextColor: "#0b2545", primaryBorderColor: "#2563eb",
      secondaryColor: "#f1f5f9", secondaryTextColor: "#0b2545", secondaryBorderColor: "#64748b",
      tertiaryColor: "#f8fafc", tertiaryTextColor: "#0b2545", tertiaryBorderColor: "#94a3b8",
      lineColor: "#334155", textColor: "#0b2545",
      noteBkgColor: "#fef3c7", noteTextColor: "#78350f", noteBorderColor: "#d97706",
      actorBkg: "#dbeafe", actorTextColor: "#0b2545", actorBorder: "#2563eb",
      signalColor: "#334155", signalTextColor: "#0b2545",
      labelBoxBkgColor: "#fef3c7", labelBoxBorderColor: "#d97706", labelTextColor: "#78350f",
      sequenceNumberColor: "#ffffff",
      clusterBkg: "#f8fafc", clusterBorder: "#94a3b8",
    };
    const dark = {
      darkMode: true, background: "#161b22", fontSize: "15px",
      primaryColor: "#1f3a5f", primaryTextColor: "#e6edf3", primaryBorderColor: "#58a6ff",
      secondaryColor: "#21262d", secondaryTextColor: "#e6edf3", secondaryBorderColor: "#8b949e",
      tertiaryColor: "#1c2128", tertiaryTextColor: "#e6edf3", tertiaryBorderColor: "#6e7681",
      lineColor: "#8b949e", textColor: "#e6edf3",
      noteBkgColor: "#4d3800", noteTextColor: "#fde68a", noteBorderColor: "#d97706",
      actorBkg: "#1f3a5f", actorTextColor: "#e6edf3", actorBorder: "#58a6ff",
      signalColor: "#8b949e", signalTextColor: "#e6edf3",
      labelBoxBkgColor: "#4d3800", labelBoxBorderColor: "#d97706", labelTextColor: "#fde68a",
      sequenceNumberColor: "#0d1117",
      clusterBkg: "#1c2128", clusterBorder: "#6e7681",
    };
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      // ★ 图表配色固定用浅色方案：讲稿里的图要能导出贴 PPT，
      //   而 PPT 白底是绝对多数；跟随系统会让同一张图在两种模式下颜色不同。
      //   （页面正文仍然跟随系统深浅，只有画布固定白底。）
      theme: "base",
      themeVariables: light,
      // subGraphTitleMargin 必须有：默认子图标题贴着边框，进入子图的箭头
      // 会直接压在标题文字上（实测："9 阶段流水线"/"存储分工" 被箭头划掉）
      flowchart: {
        htmlLabels: true, useMaxWidth: true,
        padding: 14, nodeSpacing: 45, rankSpacing: 50,
        subGraphTitleMargin: { top: 10, bottom: 16 },
      },
      sequence: { useMaxWidth: true, mirrorActors: false, actorMargin: 60 },
    });
    await mermaid.run({ querySelector: ".mermaid" });
  } catch (e) {
    // 离线时保留源码，至少能看到内容而不是空白
    document.querySelectorAll(".mermaid").forEach(el => {
      el.innerHTML = '<pre style="text-align:left;margin:0"><code>'
        + el.textContent.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))
        + '</code></pre>';
    });
    console.warn("mermaid 加载失败，已降级为源码显示：", e);
  }
}
const top = document.getElementById("top");
addEventListener("scroll", () => top.classList.toggle("show", scrollY > 400));
top.onclick = () => scrollTo({ top: 0, behavior: "smooth" });
</script>
</body>
</html>
`;
  writeFileSync(outPath, html, "utf8");
  return { title, mermaid: mermaidBlocks.length, bytes: Buffer.byteLength(html) };
}

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ── 入口 ────────────────────────────────────────────────────
const argv = process.argv.slice(2);
const files = argv.includes("--all")
  ? TARGETS["--all"]
  : argv.filter((a) => a.endsWith(".md"));

if (!files.length) {
  console.error("用法: node scripts/md2html.mjs <文件.md> [...] | --all");
  process.exit(2);
}

let failed = 0;
for (const f of files) {
  const src = resolve(f);
  // ★ 命名约定：<名字>.md → <名字>.html；但 docs/diagrams/architecture.md
  //   的同名 HTML 是**手工维护的查看器**（带缩放/导出按钮），生成物必须
  //   让开，否则会被静默覆盖。加 _print 后缀区分「可打印版」。
  const stem = basename(src).replace(/\.md$/, "");
  const out = join(
    dirname(src),
    stem === "architecture" ? "architecture_print.html" : `${stem}.html`,
  );
  try {
    const r = build(src, out);
    console.log(`  ✓ ${basename(out)}  ${(r.bytes / 1024).toFixed(0)} KB，mermaid ${r.mermaid} 张`);
  } catch (e) {
    failed++;
    console.error(`  ✗ ${f}: ${e.message}`);
  }
}
process.exit(failed ? 1 : 0);
