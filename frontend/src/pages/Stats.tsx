import { useEffect, useState } from "react";
import { get } from "../lib/api";
import { useStore } from "../lib/store";
import { PageTitle } from "../components/Shell";

type Stats = {
  year: number; timezone: string; started: number; finished: number;
  totals: { read_seconds: number; listened_seconds: number; pages: number };
  streak: { current: number; longest: number; minimum_seconds: number };
  days: { day: string; read_seconds: number; listened_seconds: number; pages: number }[];
  longest_session_seconds: number;
  top_authors: { author: string; seconds: number }[]; estimate: string;
};

const duration = (seconds: number) => `${Math.floor(seconds / 3600)}h ${Math.round(seconds % 3600 / 60)}m`;

function Heatmap({ year, days }: Pick<Stats, "year" | "days">) {
  const totals = new Map(days.map((day) => [day.day, day.read_seconds + day.listened_seconds]));
  const start = new Date(Date.UTC(year, 0, 1));
  const end = new Date(Date.UTC(year + 1, 0, 1));
  const cells = [];
  for (const day = new Date(start); day < end; day.setUTCDate(day.getUTCDate() + 1)) {
    const key = day.toISOString().slice(0, 10);
    const seconds = totals.get(key) ?? 0;
    const level = seconds ? Math.min(4, Math.ceil(seconds / 900)) : 0;
    cells.push(<span key={key} className={`stats-day level-${level}`} title={`${key}: ${duration(seconds)}`} />);
  }
  return <div className="stats-heatmap" aria-label={`Calendar heatmap for ${year}`}>{cells}</div>;
}

export function StatsPage() {
  const { me, setPreference, toast } = useStore();
  const [year, setYear] = useState(new Date().getFullYear());
  const [stats, setStats] = useState<Stats | null>(null);
  useEffect(() => { get<Stats>(`/api/stats?year=${year}`).then(setStats).catch((e) => toast(e.message, "error")); }, [year, toast]);
  if (!stats) return <><PageTitle>Reading stats</PageTitle><div className="hint">Loading…</div></>;
  return <>
    <PageTitle>Reading stats</PageTitle>
    <div className="stack">
      <section className="card"><div className="row" style={{ justifyContent: "space-between" }}>
        <div><h2>{year} in review</h2><p className="sub">Private to you. {stats.estimate}</p></div>
        <input aria-label="Review year" type="number" min="1900" max="2999" value={year} onChange={(e) => setYear(Number(e.target.value))} />
      </div>
      <div className="row" style={{ gap: 24, flexWrap: "wrap" }}>
        <strong>{duration(stats.totals.read_seconds)} reading</strong><strong>{duration(stats.totals.listened_seconds)} listening</strong><strong>{Math.round(stats.totals.pages)} estimated pages</strong>
      </div></section>
      <section className="card"><h2>Streak</h2><p className="sub">{stats.streak.current} current · {stats.streak.longest} longest. A day needs at least {stats.streak.minimum_seconds} seconds of counted activity.</p>
        <div className="hint">{stats.days.length} active days · {stats.started} started · {stats.finished} finished · {duration(stats.longest_session_seconds)} longest session</div></section>
      <section className="card"><h2>Calendar</h2><p className="sub">Each square is a day; darker means more counted reading or listening.</p><Heatmap year={year} days={stats.days} /></section>
      <section className="card"><h2>Calendar timezone</h2><p className="sub">Changing it rebuckets retained activity and handles daylight-saving transitions.</p>
        <input aria-label="IANA timezone" value={me?.preferences.reading_timezone ?? stats.timezone} onChange={(e) => setPreference("reading_timezone", e.target.value)} />
      </section>
      <section className="card"><h2>Top authors</h2>{stats.top_authors.length ? <ul>{stats.top_authors.map((a) => <li key={a.author}>{a.author} · {duration(a.seconds)}</li>)}</ul> : <p className="hint">No counted activity this year yet.</p>}</section>
    </div>
  </>;
}
