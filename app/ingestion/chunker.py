import logging
import os
import re
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path

from bs4 import BeautifulSoup
from yaml import YAMLError, safe_load

from app.config import settings
from app.ingestion.models import Chunk

logger = logging.getLogger(__name__)


def is_ignored(path: Path, docs_dir: Path) -> bool:
    """Match names at any depth, or slash-containing globs relative to the root."""
    try:
        relative = path.absolute().relative_to(docs_dir.absolute())
    except ValueError:
        return True
    candidates = [relative, *relative.parents]
    for pattern in settings.docs_ignore_patterns:
        pattern = pattern.rstrip("/")
        if not pattern:
            continue
        for candidate in candidates:
            value = candidate.as_posix() if "/" in pattern else candidate.name
            if fnmatchcase(value, pattern):
                return True
    return False


def iter_files(docs_dir: Path) -> Iterator[Path]:
    """Recursively iterate through all files in the given directory."""
    def raise_walk_error(error):
        raise error

    if not docs_dir.is_dir():
        raise FileNotFoundError(f"Document directory does not exist: {docs_dir}")
    for directory, dirs, files in os.walk(docs_dir, onerror=raise_walk_error):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if not is_ignored(parent / name, docs_dir)]
        for name in files:
            path = parent / name
            if not is_ignored(path, docs_dir) and path.suffix[1:] in settings.supported_file_types:
                yield path


def extract_text_from_file(file_path: Path) -> str | None:
    """Extract text content from a file based on its type."""
    logger.debug("Extracting text from file: %s", file_path)
    if file_path.suffix[1:] in settings.plain_text_formats:
        return read_file(file_path)
    elif file_path.suffix[1:] in settings.markup_formats:
        html_content = read_file(file_path)
        if html_content is not None:
            soup = BeautifulSoup(html_content, "html.parser")
            return soup.get_text()

    return None


def read_file(file_path: Path) -> str | None:
    """Read a file and extract its text content."""
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Error reading file %s", file_path)
        return None


_ATX_HEADING = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*)|$)")
_SETEXT_HEADING = re.compile(r" {0,3}(=+|-+)[ \t]*$")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


def split_frontmatter(text: str) -> tuple[str, str]:
    """Separate a top-of-file YAML block from the Markdown body."""
    lines = text.removeprefix("\ufeff").splitlines(keepends=True)
    if lines and lines[0].rstrip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.rstrip() in {"---", "..."}:
                return "".join(lines[1:index]), "".join(lines[index + 1:])
    return "", text.removeprefix("\ufeff")


def markdown_tags(frontmatter: str) -> list[str]:
    try:
        properties = safe_load(frontmatter)
    except YAMLError:
        logger.warning("Ignoring malformed Markdown frontmatter tags")
        return []
    tags = properties.get("tags") if isinstance(properties, dict) else None
    if isinstance(tags, str):
        tags = [tags]
    return [tag for tag in tags if isinstance(tag, str) and tag.strip()] if isinstance(tags, list) else []


def markdown_sections(text: str) -> Iterator[tuple[str | None, str]]:
    """Yield Markdown section bodies with their enclosing heading paths."""
    _, body_text = split_frontmatter(text)
    lines = body_text.splitlines()

    headings: list[tuple[int, str]] = []
    body: list[str] = []
    fence: str | None = None
    for line in lines:
        marker = _FENCE.fullmatch(line)
        if fence is not None:
            body.append(line)
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = None
            continue
        if marker and (marker[1][0] == "~" or "`" not in marker[2]):
            fence = marker[1]
            body.append(line)
            continue

        atx = _ATX_HEADING.fullmatch(line)
        setext = _SETEXT_HEADING.fullmatch(line)
        if atx:
            level = len(atx[1])
            title = re.sub(r"(?:^|[ \t]+)#+[ \t]*$", "", atx[2] or "").strip()
        elif setext and body and _is_setext_title(body[-1]):
            level = 1 if setext[1][0] == "=" else 2
            title = body.pop().strip()
        else:
            body.append(line)
            continue

        yield " > ".join(title for _, title in headings) or None, "\n".join(body)
        while headings and headings[-1][0] >= level:
            headings.pop()
        if title:
            headings.append((level, title))
        body.clear()

    yield " > ".join(title for _, title in headings) or None, "\n".join(body)


def _is_setext_title(line: str) -> bool:
    return bool(line.strip()) and not re.match(
        r"(?: {4}|\t| {0,3}(?:>|[-+*][ \t]|\d+[.)][ \t]|(?:`{3,}|~{3,})))", line
    )


def chunk_text(source: Path, text: str) -> Iterator[Chunk]:
    """Chunk each section independently and number chunks across the file."""
    size = settings.chunk_size * 4
    overlap = settings.chunk_overlap * 4
    if not 0 <= overlap < size:
        raise ValueError("Chunk overlap must be non-negative and smaller than chunk size")

    sections = markdown_sections(text) if source.suffix == ".md" else [(None, text)]
    position = 0
    for heading, body in sections:
        remaining = body.strip()
        while remaining:
            end = min(size, len(remaining))
            next_start = end - overlap
            if end < len(remaining):
                boundary = remaining.rfind("\n\n", overlap + 1, end + 1)
                if boundary != -1:
                    end = boundary
                    paragraph_start = boundary + 2
                    paragraph_end = remaining.find("\n\n", paragraph_start, paragraph_start + size + 2)
                    if paragraph_end == -1:
                        paragraph_end = min(len(remaining), paragraph_start + size)
                    # Keep the next paragraph whole when it fits without full overlap.
                    carry = min(overlap, max(0, size - (paragraph_end - paragraph_start) - 2))
                    next_start = boundary - carry if carry else boundary + 2
            yield Chunk(remaining[:end].strip(), source, position, heading=heading)
            position += 1
            if end == len(remaining):
                break
            remaining = remaining[next_start:].lstrip()
