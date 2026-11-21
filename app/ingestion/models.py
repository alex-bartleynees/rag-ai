from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Chunk:
    text: str
    source: Path
    position: int
    heading: str | None = field(default=None, kw_only=True)


@dataclass
class EmbeddedChunk(Chunk):
    embedding: list[float]
