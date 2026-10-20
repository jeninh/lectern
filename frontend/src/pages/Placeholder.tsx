import { Link } from "react-router-dom";
import { EmptyState } from "../components/EmptyState";
import { Highlighter } from "../components/Icons";
import { PageTitle } from "../components/Shell";

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
