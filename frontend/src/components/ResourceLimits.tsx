import { useEffect, useState } from "react";
import { get } from "../lib/api";
import { limitCaps, limitState, type ResourceLimits } from "../lib/limits";

/** Admin → Resource limits: the caps in force (set in .env) and what runs or waits under them. */
export function ResourceLimitsCard() {
  const [s, setS] = useState<ResourceLimits | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => get<ResourceLimits>("/api/admin/resource-limits").then((d) => live && setS(d)).catch(() => {});
    load();
    const t = window.setInterval(load, 15000);
    return () => {
      live = false;
      window.clearInterval(t);
    };
  }, []);
  if (!s) return null;
  return (
    <section className="card" id="resource-limits" aria-labelledby="rl-h" style={{ marginTop: 18 }}>
      <h2 id="rl-h">Resource limits</h2>
      <p className="sub">
        Heavy jobs this server runs itself: {s.heavy_jobs.active} of {s.heavy_jobs.limit} running ({s.heavy_jobs.env}). Jobs a worker runs do not count.
        Change them in <code>.env</code> (docs/resource-limits.md) and restart Lectern.
      </p>
      <table className="list">
        <thead>
          <tr>
            <th scope="col">Feature</th>
            <th scope="col">Limits</th>
            <th scope="col">Now</th>
          </tr>
        </thead>
        <tbody>
          {s.features.map((f) => (
            <tr key={f.feature}>
              <td>{f.label}</td>
              <td className="hint">{limitCaps(f)}</td>
              <td>
                <span className="status">
                  {f.waiting_for_limit && <span className="status-dot waiting" aria-hidden="true" />}
                  {limitState(f)}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
