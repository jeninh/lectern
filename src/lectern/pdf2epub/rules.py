"""The conversion rules a person reviews before converting (issue #63, the analyse → review
rules → convert flow): how page numbers, chapters, verse, footnotes, figures, tables, maths,
page images, and front or back matter are treated. Detected defaults come from the analysis
(:func:`defaults`); the confirmed rules are saved with the edition
(``derived/pdf2epub-rules.json``) so a re-run uses them.

Running headers, columns, and paragraph joins are decided by the text extractor
(:mod:`lectern.pdftext.layout`) for every reader of the PDF's text; the review sheet shows
what it found but they are not rules of this conversion.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_PAGES = 10_000
XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
Page = Annotated[int, Field(ge=1, le=MAX_PAGES)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageNumbers(_Strict):
    keep: bool = Field(True, description="Keep the PDF's page numbers as reference pages.")
    offset: int = Field(0, ge=-10_000, le=10_000, description="Added to numeric page labels.")


class ChapterRule(_Strict):
    index: int = Field(ge=0, le=MAX_PAGES, description="The detected chapter's index.")
    title: str | None = Field(None, max_length=120, description="A new title.")
    merge: bool = Field(False, description="Join this chapter onto the one before it.")

    @field_validator("title")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = " ".join(XML_BAD.sub("", v).split())
        return v or None


class Rules(_Strict):
    version: Literal[1] = 1
    page_numbers: PageNumbers = PageNumbers()
    chapters: list[ChapterRule] = Field(default_factory=list, max_length=2000)
    verse: Literal["auto", "off"] = Field("auto", description="Detect verse (line breaks kept).")
    verse_pages: list[Page] = Field(default_factory=list, max_length=MAX_PAGES)
    footnotes: Literal["end", "popup", "inline"] = Field(
        "end", description="Notes at the chapter's end, after their paragraph, or left as text."
    )
    figures: Literal["keep", "drop"] = "keep"
    tables: Literal["auto", "image", "text"] = Field(
        "auto", description="auto: simple grids as tables, complex ones as images."
    )
    maths: Literal["image", "text"] = "image"
    styles: bool = Field(True, description="Keep italic, bold, small caps, super/subscripts.")
    page_images: list[Page] = Field(default_factory=list, max_length=MAX_PAGES)
    drop_pages: list[Page] = Field(default_factory=list, max_length=MAX_PAGES)


def defaults(structure: dict) -> Rules:
    """The detected rules: low-confidence pages with content are kept as images."""
    return Rules(page_images=[p["page"] for p in structure.get("pages") or [] if p.get("flagged")])
