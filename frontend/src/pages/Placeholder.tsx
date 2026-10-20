import { Link } from "react-router-dom";
import { EmptyState } from "../components/EmptyState";
import { Headphones, Highlighter } from "../components/Icons";
import { PageTitle } from "../components/Shell";

export function ListeningPage() {
  return (
    <>
      <PageTitle sub="Audiobooks and read-along">Listening</PageTitle>
      <EmptyState icon={<Headphones />} title="Listening arrives in a later milestone" action={<Link className="btn" to="/app/library">Browse the library</Link>}>
        Audiobooks will sit next to their EPUBs on the same work, with positions that follow you between reading and listening.
      </EmptyState>
    </>
  );
}

export function HighlightsPage() {
  return (
    <>
      <PageTitle sub="Notes and highlights from KOReader">Highlights</PageTitle>
      <EmptyState icon={<Highlighter />} title="Highlights are on the way" action={<Link className="btn" to="/app">Back to reading</Link>}>
        Passages you mark in KOReader will collect here, grouped by book and searchable from the palette.
      </EmptyState>
    </>
  );
}
