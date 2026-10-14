"""Structure-aware chunking, deterministic by construction.

Three rules, one per shape of input, and each has to survive the *same*
constraint: the same bytes must always produce the same chunks, because chunk ids
feed the golden-set metrics — a shifty splitter turns every retrieval number into
noise. Markdown splits on headings, Python on definitions via :mod:`ast`, and
everything else on a sliding paragraph window. No model, no tokenizer, no network.
"""

from __future__ import annotations

import ast

from os_know.types import ChunkDraft

#: A chunk bigger than this is split again; smaller than ``min_chars`` it is
#: merged forward, so a one-line heading never becomes its own retrieval unit.
MAX_CHARS = 1600
MIN_CHARS = 80


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p.strip()]


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def chunk_markdown(text: str, *, max_chars: int = MAX_CHARS) -> list[ChunkDraft]:
    """Split on ATX headings, keeping the heading trail as the chunk's title."""
    lines = str(text).splitlines()
    drafts: list[tuple[list[str], int, int, list[str]]] = []  # (heading trail, start, end, body)
    trail: list[str] = []
    start = 1
    body: list[str] = []

    def flush(end_line: int) -> None:
        if body and any(line.strip() for line in body):
            drafts.append((list(trail), start, end_line, list(body)))

    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped.lstrip("#").strip()
            flush(index - 1)
            body = []
            start = index
            trail = trail[: max(level - 1, 0)] + ([heading] if heading else [])
            continue
        body.append(line)
    flush(len(lines))

    out: list[ChunkDraft] = []
    ordinal = 0
    for headings, s_line, e_line, body_lines in drafts:
        heading = " › ".join(h for h in headings if h)
        block = "\n".join(body_lines).strip()
        if not block:
            continue
        for piece in _split_long(block, max_chars):
            out.append(
                ChunkDraft(
                    text=piece,
                    heading=heading,
                    start_line=s_line,
                    end_line=e_line,
                    ordinal=ordinal,
                )
            )
            ordinal += 1
    return out


def chunk_python(text: str, *, max_chars: int = MAX_CHARS) -> list[ChunkDraft]:
    """Split on top-level and nested defs/classes using the AST, not a regex.

    A syntax error is not a failure mode here: unparseable Python is still
    searchable text, so it falls back to the generic splitter and the caller can
    index half-written files without the corpus refusing them.
    """
    source = str(text)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return chunk_text(source)

    out: list[ChunkDraft] = []
    lines = source.splitlines()

    def emit(node: ast.AST, prefix: str) -> None:
        start_line = int(getattr(node, "lineno", 1)) - 1
        end_line = int(getattr(node, "end_lineno", start_line + 1))
        block = "\n".join(lines[start_line:end_line])
        name = getattr(node, "name", "")
        heading = f"{prefix}{name}" if name else prefix
        if block.strip():
            for piece in _split_long(block, max_chars):
                out.append(
                    ChunkDraft(
                        text=piece,
                        heading=heading,
                        start_line=start_line + 1,
                        end_line=end_line,
                        ordinal=len(out),
                    )
                )
        for child in getattr(node, "body", []) or []:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                emit(child, f"{heading}.")

    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            emit(top, "")
        else:
            start_line = int(getattr(top, "lineno", 1)) - 1
            end_line = int(getattr(top, "end_lineno", start_line + 1))
            block = "\n".join(lines[start_line:end_line]).strip()
            if not block:
                continue
            out.append(
                ChunkDraft(
                    text=block,
                    heading="",
                    start_line=start_line + 1,
                    end_line=end_line,
                    ordinal=len(out),
                )
            )
    if not out:  # a file of only imports/comments
        return chunk_text(source)
    return out


def chunk_text(text: str, *, size: int = 1200, overlap: int = 200) -> list[ChunkDraft]:
    """Sliding window on paragraph boundaries, with overlap so a fact spanning a
    boundary is still reachable from either side."""
    source = "\n".join(line.rstrip() for line in str(text).splitlines()).strip()
    if not source:
        return []
    paragraphs = [p.strip() for p in source.split("\n\n") if p.strip()]
    drafts: list[ChunkDraft] = []
    buffer = ""
    line_no = 1
    chunk_start = 1
    for para in paragraphs:
        candidate = f"{buffer}\n\n{para}" if buffer else para
        if len(candidate) > size and buffer:
            drafts.append(_draft(buffer, chunk_start, line_no - 1, len(drafts)))
            tail = buffer[-overlap:] if overlap > 0 else ""
            buffer, chunk_start = tail, max(line_no - 1, 1)
            candidate = f"{buffer}\n\n{para}" if buffer else para
        if len(candidate) > size:
            # A single oversized paragraph: emit it whole rather than lose it.
            if buffer:
                drafts.append(_draft(buffer, chunk_start, line_no - 1, len(drafts)))
                buffer, chunk_start = "", line_no
            drafts.append(_draft(para, line_no, line_no + para.count("\n"), len(drafts)))
            line_no += para.count("\n") + 2
            continue
        buffer, line_no = candidate, line_no + para.count("\n") + 2
    if buffer:
        drafts.append(_draft(buffer, chunk_start, max(line_no - 2, chunk_start), len(drafts)))
    # Merge drafts that came out too short to be worth their own retrieval slot.
    merged: list[ChunkDraft] = []
    for draft in drafts:
        if merged and len(draft.text) < MIN_CHARS:
            previous = merged[-1]
            merged[-1] = previous.model_copy(
                update={
                    "text": f"{previous.text}\n\n{draft.text}",
                    "end_line": draft.end_line,
                }
            )
            continue
        merged.append(draft.model_copy(update={"ordinal": len(merged)}))
    return merged


def _draft(text: str, start_line: int, end_line: int, ordinal: int) -> ChunkDraft:
    return ChunkDraft(
        text=text,
        heading="",
        start_line=max(start_line, 1),
        end_line=max(end_line, start_line),
        ordinal=ordinal,
    )


def chunk_code(text: str, *, language: str = "") -> list[ChunkDraft]:
    """Dispatch for source files; only Python is structure-aware today."""
    if language.lower() in {"python", "py"} or (not language):
        try:
            ast.parse(text)
        except SyntaxError:
            return chunk_text(text)
        return chunk_python(text)
    return chunk_text(text)


_CODE_MIMES = {"text/x-python", "application/x-python", "text/x-c", "text/plain"}


def chunk_for(text: str, *, mime: str = "text/markdown", language: str = "") -> list[ChunkDraft]:
    """The one entry point the store uses."""
    lowered = (mime or "").lower()
    if lowered.startswith("text/markdown") or language.lower() in {"md", "markdown"}:
        return chunk_markdown(text)
    if language.lower() in {"python", "py"} or lowered in _CODE_MIMES:
        return chunk_code(text, language=language)
    return chunk_text(text)


__all__ = [
    "MAX_CHARS",
    "MIN_CHARS",
    "chunk_code",
    "chunk_for",
    "chunk_markdown",
    "chunk_python",
    "chunk_text",
]
