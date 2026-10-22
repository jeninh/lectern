// Book formats as the UI names them. EPUB has text (reader, read-along, alignment); PDF and CBZ
// comics have pages (page viewer, page progress that KOReader syncs as page numbers).

import type { BookFile, Work } from "./types";

export const EPUB = "application/epub+zip";
export const PDF = "application/pdf";
export const CBZ = "application/vnd.comicbook+zip";

export type Format = "EPUB" | "PDF" | "CBZ";

const LABEL: Record<string, Format> = { [EPUB]: "EPUB", [PDF]: "PDF", [CBZ]: "CBZ" };

export function formatOf(f: Pick<BookFile, "media_type">): Format | null {
  return LABEL[f.media_type] ?? null;
}

/** A friendlier name where there is room for one ("Comic (CBZ)"). */
export function formatName(f: Pick<BookFile, "media_type">): string {
  const fmt = formatOf(f);
  return fmt === "CBZ" ? "Comic (CBZ)" : fmt ?? "File";
}

export function isPaged(f: Pick<BookFile, "media_type">): boolean {
  return f.media_type === PDF || f.media_type === CBZ;
}

/** The work's book files (EPUB first, then PDF, then comics), the way lists show them. */
export function bookFiles(work: Pick<Work, "files">): BookFile[] {
  const order = (f: BookFile) => [EPUB, PDF, CBZ].indexOf(f.media_type);
  return work.files.filter((f) => f.kind === "ebook" && formatOf(f)).sort((a, b) => order(a) - order(b) || a.id - b.id);
}

export function hasEpub(work: Pick<Work, "files">): boolean {
  return work.files.some((f) => f.media_type === EPUB);
}

/** Where "Read" goes: the EPUB reader, or the page viewer for a work with only a PDF or comic. */
export function readerPath(work: Pick<Work, "id" | "files">, file?: BookFile): string {
  if (file && isPaged(file)) return `/app/reader/${work.id}?file=${file.id}`;
  if (file || hasEpub(work)) return `/app/reader/${work.id}`;
  const paged = bookFiles(work).find(isPaged);
  return paged ? `/app/reader/${work.id}?file=${paged.id}` : `/app/reader/${work.id}`;
}

/** KOReader positions: "/body/DocFragment[12]/…" for EPUBs, a page number for PDFs and comics. */
export function positionLabel(progress: string): string {
  if (/^\d+$/.test(progress)) return `Page ${progress}`;
  const m = /DocFragment\[(\d+)\]/.exec(progress);
  return m ? `Section ${m[1]}` : progress.length > 24 ? `${progress.slice(0, 24)}…` : progress;
}

export const READALONG_NEEDS_EPUB = "Read-along needs an EPUB or a text PDF";
