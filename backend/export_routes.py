"""Snapshot-based, local document export with real CSL citation rendering."""
from __future__ import annotations

from datetime import datetime
import html
import json
import os
from pathlib import Path
import re
import warnings
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import core

router = APIRouter(prefix="/api", tags=["exports"])
STYLES = {"apa": "APA 7", "ieee": "IEEE", "gbt": "GB/T 7714—2015（顺序编码）"}
TOKEN = re.compile(r"\[@([A-Za-z0-9_-]+)\]")
GROUP_TOKEN = re.compile(r"\[((?:@[A-Za-z0-9_-]+\s*(?:;\s*)?)+)\]")


class ExportRequest(BaseModel):
    languages: list[Literal["zh", "en"]] = Field(default_factory=lambda: ["zh"], min_length=1, max_length=2)
    formats: list[Literal["md", "docx", "pdf", "tex", "bib"]] = Field(default_factory=lambda: ["md"], min_length=1, max_length=5)
    style: Literal["apa", "ieee", "gbt"] = "apa"


def cite_key(identifier: str) -> str:
    return identifier if identifier.startswith("ref_") else "ref_" + identifier


def authors_for_csl(value) -> list[dict]:
    authors = core.json_load(value, [])
    if isinstance(authors, str):
        authors = [authors]
    converted = []
    for author in authors or []:
        if isinstance(author, dict):
            if author.get("family") or author.get("given"):
                converted.append({key: str(author[key]) for key in ("family", "given") if author.get(key)})
            elif author.get("literal") or author.get("name"):
                converted.append({"literal": str(author.get("literal") or author["name"])})
        elif str(author).strip():
            # Do not invent family names for ambiguous imported strings.
            converted.append({"literal": str(author).strip()})
    return converted


def reference_to_csl(ref: dict) -> dict:
    metadata = core.json_load(ref.get("metadata"), {})
    record = {
        "id": cite_key(ref["id"]),
        "type": {"journal-article": "article-journal", "proceedings-article": "paper-conference", "book-chapter": "chapter", "dissertation": "thesis", "posted-content": "manuscript"}.get(ref.get("type"), ref.get("type") or "article-journal"),
        "title": ref.get("title") or "Untitled",
        "author": authors_for_csl(ref.get("authors")),
    }
    accepted_types = {"article-journal", "article", "book", "chapter", "paper-conference", "report", "thesis", "webpage", "dataset", "manuscript"}
    if record["type"] not in accepted_types:
        record["type"] = "article-journal"
    if ref.get("year"):
        record["issued"] = {"date-parts": [[int(ref["year"])]]}
    for source, target in (("doi", "DOI"), ("url", "URL"), ("publisher", "publisher")):
        if ref.get(source):
            record[target] = str(ref[source])
    for field in ("container-title", "volume", "issue", "page", "edition", "ISBN", "ISSN"):
        value = metadata.get(field) if isinstance(metadata, dict) else None
        if value:
            record[field] = str(value[0] if isinstance(value, list) else value)
    return record


def citation_groups(text: str, references: list[dict]) -> list[list[str]]:
    known = {ref["id"]: cite_key(ref["id"]) for ref in references}
    known.update({cite_key(ref["id"]): cite_key(ref["id"]) for ref in references})
    groups = []
    for match in GROUP_TOKEN.finditer(text):
        keys = re.findall(r"@([A-Za-z0-9_-]+)", match.group(1))
        unknown = [key for key in keys if key not in known]
        if unknown:
            raise HTTPException(422, "稿件含有不属于本项目的引用：" + ", ".join(unknown[:8]))
        groups.append([known[key] for key in keys])
    # Catch malformed reference placeholders rather than silently printing them.
    remainder = GROUP_TOKEN.sub("", text)
    if "[@" in remainder:
        raise HTTPException(422, "存在无法识别的引用标记，请使用 [@ref_文献ID]，多个文献用分号分隔。")
    return groups


def render_citations(text: str, references: list[dict], style: str, language: str) -> tuple[str, list[str]]:
    groups = citation_groups(text, references)
    if not groups:
        return text, []
    from citeproc import Citation, CitationItem, CitationStylesBibliography, CitationStylesStyle
    from citeproc import formatter
    from citeproc.source.json import CiteProcJSON

    csl_path = core.ROOT / "assets" / "csl" / (style + ".csl")
    if not csl_path.is_file():
        raise HTTPException(503, "引用样式文件缺失，请修复本地安装。")
    notices = []
    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            csl = CitationStylesStyle(str(csl_path), locale="zh-CN" if style == "gbt" else "en-US", validate=False)
            bibliography = CitationStylesBibliography(csl, CiteProcJSON([reference_to_csl(ref) for ref in references]), formatter.plain)
            citations = [Citation([CitationItem(key) for key in group]) for group in groups]
            for citation in citations:
                bibliography.register(citation)
            bibliography.sort()
            rendered = [str(bibliography.cite(citation, lambda item: None)) for citation in citations]
            entries = [str(entry) for entry in bibliography.bibliography()]
            if captured:
                notices.append("CSL 引擎报告样式兼容性提示，请检查最终参考文献排版。")
        items = iter(rendered)
        result = GROUP_TOKEN.sub(lambda match: next(items), text)
        if entries:
            title = "参考文献" if language == "zh" else "References"
            result += "\n\n## " + title + "\n\n" + "\n\n".join(entries)
        return result, notices
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "引用渲染失败，请检查作者、年份和文献类型等元数据。") from exc


def plain_inline(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"[图片：\1；\2]", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)", lambda m: m.group(1) or m.group(2), text)
    return re.sub(r"`([^`]+)`", r"\1", text)


def blocks(markdown: str):
    """A conservative block parser: content is retained, never interpreted as code."""
    pending = []
    code = []
    fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            if pending:
                yield ("p", " ".join(pending), 0)
                pending = []
            if fence:
                yield ("code", "\n".join(code), 0)
                code = []
            fence = not fence
            continue
        if fence:
            code.append(line)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        bullet = re.match(r"^\s*([-*+]\s+|\d+[.)]\s+)(.+)$", line)
        if not line.strip() or heading or bullet or line.startswith("> "):
            if pending:
                yield ("p", " ".join(pending), 0)
                pending = []
            if heading:
                yield ("h", heading.group(2), len(heading.group(1)))
            elif bullet:
                yield ("list", bullet.group(2), int(re.match(r"\d+", bullet.group(1)).group()) if bullet.group(1)[0].isdigit() else 0)
            elif line.startswith("> "):
                yield ("quote", line[2:], 0)
        else:
            pending.append(line)
    if pending:
        yield ("p", " ".join(pending), 0)
    if code:
        yield ("code", "\n".join(code), 0)


def write_docx(path: Path, title: str, text: str, language: str):
    from docx import Document
    from docx.shared import Inches, Pt
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Inches(0.85)
    section.left_margin = section.right_margin = Inches(0.9)
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = 1.45
    normal.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "宋体")
    document.core_properties.title = title
    document.core_properties.language = "zh-CN" if language == "zh" else "en-US"
    if not text.lstrip().startswith("# "):
        document.add_heading(title, 0)
    for kind, content, level in blocks(text):
        content = plain_inline(content)
        if kind == "h":
            document.add_heading(content, min(level, 6))
        else:
            style = "List Number" if kind == "list" and level else "List Bullet" if kind == "list" else "Quote" if kind == "quote" else None
            paragraph = document.add_paragraph(content, style=style)
            if kind == "code":
                for run in paragraph.runs:
                    run.font.name = "Consolas"
                    run.font.size = Pt(9)
    footer = section.footer.paragraphs[0]
    footer.alignment = 1
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    document.save(path)


def pdf_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    name = "WorkbenchChinese"
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "simhei.ttf"
    if path.is_file():
        pdfmetrics.registerFont(TTFont(name, str(path)))
        return name
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def write_pdf(path: Path, title: str, text: str, language: str):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    font = pdf_font()
    normal = ParagraphStyle("Body", fontName=font, fontSize=10.5, leading=17,
                            wordWrap="CJK", alignment=TA_LEFT, spaceAfter=9,
                            textColor=colors.HexColor("#26332d"), splitLongWords=True)
    heading = ParagraphStyle("Heading", parent=normal, fontSize=17, leading=24, spaceBefore=16, spaceAfter=10, keepWithNext=True)
    story = []
    if not text.lstrip().startswith("# "):
        story.extend([Paragraph(html.escape(title), heading), Spacer(1, 8)])
    for kind, content, level in blocks(text):
        safe = html.escape(plain_inline(content)).replace("\n", "<br/>")
        if kind == "list":
            safe = (str(level) + ". " if level else '<font name="Helvetica">&#8226;</font> ') + safe
        style = heading if kind == "h" else normal
        story.append(Paragraph(safe or " ", style))
    def page_number(canvas, document):
        canvas.saveState()
        canvas.setFont(font, 9)
        canvas.setFillColor(colors.HexColor("#6b7770"))
        canvas.drawCentredString(A4[0] / 2, 28, str(document.page))
        canvas.restoreState()
    SimpleDocTemplate(str(path), pagesize=A4, rightMargin=55, leftMargin=55,
                      topMargin=50, bottomMargin=48, title=title,
                      author="", pageCompression=1).build(story, onFirstPage=page_number, onLaterPages=page_number)


def tex_escape(text: str) -> str:
    mapping = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(mapping.get(character, character) for character in text)


def write_tex(path: Path, title: str, text: str, language: str):
    # A self-contained XeLaTeX source. We deliberately do not execute arbitrary TeX.
    lines = ["% Compile using XeLaTeX (requires ctex and fonts).",
             r"\documentclass[UTF8,a4paper,11pt]{ctexart}", r"\usepackage[margin=2.5cm]{geometry}",
             r"\usepackage{hyperref}", r"\setlength{\parskip}{0.5em}", r"\begin{document}"]
    if not text.lstrip().startswith("# "):
        lines.extend([r"\title{" + tex_escape(title) + "}", r"\author{}", r"\date{}", r"\maketitle"])
    for kind, content, level in blocks(text):
        safe = tex_escape(plain_inline(content))
        if kind == "h":
            command = ["section", "subsection", "subsubsection", "paragraph"][min(level - 1, 3)]
            lines.append("\\" + command + "*{" + safe + "}")
        elif kind == "list":
            prefix = str(level) + ". " if level else r"\textbullet\ "
            lines.append(r"\noindent " + prefix + safe + r"\par")
        elif kind == "quote":
            lines.extend([r"\begin{quote}", safe, r"\end{quote}"])
        else:
            lines.extend([safe.replace("\n", r"\par "), ""])
    lines.append(r"\end{document}")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_bib(path: Path, references: list[dict]):
    entries = []
    for ref in references:
        csl = reference_to_csl(ref)
        authors = []
        for author in csl["author"]:
            authors.append("{" + author["literal"] + "}" if "literal" in author else (author.get("family", "") + ", " + author.get("given", "")).strip(", "))
        fields = {"title": csl["title"], "author": " and ".join(authors), "year": str(ref.get("year") or ""),
                  "doi": ref.get("doi", ""), "url": ref.get("url", ""),
                  "journal": csl.get("container-title", ""), "publisher": ref.get("publisher", ""),
                  "volume": csl.get("volume", ""), "number": csl.get("issue", ""), "pages": csl.get("page", "")}
        kind = {"book": "book", "chapter": "incollection", "paper-conference": "inproceedings", "thesis": "phdthesis", "report": "techreport", "webpage": "misc", "dataset": "misc"}.get(csl["type"], "article")
        # Escape every imported field; the generated key contains only validated identifiers.
        fields["author"] = " and ".join("{" + tex_escape(a.get("literal", "")) + "}" if "literal" in a else tex_escape((a.get("family", "") + ", " + a.get("given", "")).strip(", ")) for a in csl["author"])
        rendered = []
        for key, value in fields.items():
            if value:
                escaped = value if key == "author" else tex_escape(str(value).replace("\n", " "))
                rendered.append("  " + key + " = {" + escaped + "}")
        entries.append("@" + kind + "{" + cite_key(ref["id"]) + ",\n" + ",\n".join(rendered) + "\n}")
    path.write_text("\n\n".join(entries) + "\n", encoding="utf-8")


def filename_stem(title: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:70] or "paper"
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *["COM" + str(i) for i in range(1, 10)], *["LPT" + str(i) for i in range(1, 10)]}:
        value = "paper_" + value
    return value


def export_directory(db) -> Path:
    row = db.execute("SELECT value FROM settings WHERE key='export_dir'").fetchone()
    configured = core.json_load(row["value"], row["value"]) if row else ""
    path = Path(configured).expanduser() if isinstance(configured, str) and configured.strip() else core.ROOT / "exports"
    if not path.is_absolute():
        raise HTTPException(422, "导出目录必须是本机绝对路径。")
    return path.resolve()


@router.post("/projects/{project_id}/export")
def export_project(project_id: str, request: ExportRequest):
    # Begin one read transaction so draft language variants and references share a snapshot.
    with core.conn() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "项目不存在")
        project = dict(row)
        refs = [dict(row) for row in db.execute('SELECT * FROM "references" WHERE project_id=? ORDER BY rowid', (project_id,)).fetchall()]
        export_root = export_directory(db)
    languages = list(dict.fromkeys(request.languages))
    formats = list(dict.fromkeys(request.formats))
    rendered = {}
    notices = []
    if any(fmt != "bib" for fmt in formats):
        for language in languages:
            draft = project["draft_" + language]
            if not draft.strip():
                raise HTTPException(422, ("中文" if language == "zh" else "英文") + "稿为空，请先编辑或生成稿件。")
            rendered[language], messages = render_citations(draft, refs, request.style, language)
            notices.extend(messages)
            if not citation_groups(draft, refs):
                notices.append(("中文" if language == "zh" else "英文") + "稿未插入结构化引用，未自动添加参考文献。")
    if "bib" in formats and not refs:
        raise HTTPException(422, "项目文献库为空，无法导出 BibTeX。")
    if any(fmt in formats for fmt in ("docx", "pdf", "tex")):
        notices.append("此版本导出正文、标题与列表；公式和复杂表格暂按文本保留，投稿前请核对排版。")
    if any(citation_groups(project["draft_" + language], refs) for language in rendered):
        notices.append("引用由 CSL 引擎生成；当前导出保留文字、顺序与编号，斜体、上标等细节需在投稿前复核。")
    if "tex" in formats:
        notices.append("LaTeX 为使用 ctex 的 XeLaTeX 源文件；已渲染引用文本，不包含可重新排版的 cite 命令。")
    token = core.uid()
    destination = export_root / ("paper-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + token[:8])
    stem = filename_stem(project["title"])
    files = []
    try:
        destination.mkdir(parents=True, exist_ok=False)
        for language in languages:
            for fmt in formats:
                if fmt == "bib":
                    continue
                path = destination / f"{stem}_{language}_{request.style}.{fmt}"
                content = rendered[language]
                if fmt == "md":
                    path.write_text(content, encoding="utf-8")
                elif fmt == "docx":
                    write_docx(path, project["title"], content, language)
                elif fmt == "pdf":
                    write_pdf(path, project["title"], content, language)
                elif fmt == "tex":
                    write_tex(path, project["title"], content, language)
                files.append({"name": path.name, "url": f"/api/exports/{token}/{path.name}", "path": str(path)})
        if "bib" in formats:
            path = destination / (stem + "_references.bib")
            write_bib(path, refs)
            files.append({"name": path.name, "url": f"/api/exports/{token}/{path.name}", "path": str(path)})
        manifest = {"token": token, "project_id": project_id, "snapshot_revision": project["revision"],
                    "created_at": core.now(), "directory": str(destination), "files": files,
                    "style": request.style, "warnings": list(dict.fromkeys(notices))}
        manifests = core.DATA_DIR / "export_manifests"
        manifests.mkdir(parents=True, exist_ok=True)
        (manifests / (token + ".json")).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest
    except HTTPException:
        raise
    except Exception as exc:
        # Do not delete user directories or return a successful partial export.
        raise HTTPException(500, "导出未完成，请检查目录写入权限和稿件格式；已写入的文件可能保留在本次导出目录。") from exc


@router.get("/exports/{token}/{filename}")
def download_export(token: str, filename: str):
    if not re.fullmatch(r"[a-f0-9]{32}", token) or Path(filename).name != filename or any(c in filename for c in "\\/"):
        raise HTTPException(404, "导出文件不存在")
    manifest_path = core.DATA_DIR / "export_manifests" / (token + ".json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        root = Path(manifest["directory"]).resolve()
        file = next(item for item in manifest["files"] if item["name"] == filename)
        path = Path(file["path"])
        resolved = path.resolve()
        if path.is_symlink() or resolved.parent != root or not resolved.is_file():
            raise ValueError("Invalid export path")
    except (OSError, ValueError, KeyError, StopIteration):
        raise HTTPException(404, "导出文件不存在")
    return FileResponse(resolved, filename=filename)
