import { useEffect, useState } from "react";
import { get } from "../lib/api";
import { asOfText, batteryLow, batteryText, freeSpaceText, versionText } from "../lib/dashboard";
import { absoluteTime, percent, relativeTime } from "../lib/format";
import { useStore } from "../lib/store";
import type { DashboardDevice, DeviceDetail, Work } from "../lib/types";
import { Cover } from "./Cover";

/** Battery, free space, and versions from the KOReader plugin's latest report, as a definition list. */
export function DeviceReportMeta({ d, now, latest }: { d: DashboardDevice; now: number; latest?: string | null }) {
  const r = d.report;
  if (!r) return null;
  const battery = batteryText(r);
  const free = freeSpaceText(r);
  const versions = versionText(r);
  return (
    <dl className="device-meta device-report" title={absoluteTime(r.as_of)}>
      <div>
        <dt>Battery</dt>
        <dd data-low={batteryLow(r) || undefined}>{battery ?? "—"}</dd>
      </div>
      <div>
        <dt>Storage</dt>
        <dd>{free ?? "—"}</dd>
      </div>
      <div>
        <dt>Books on it</dt>
        <dd>{r.books}</dd>
      </div>
      <div className="device-report-foot">
        <dt className="sr-only">Versions</dt>
        <dd className="hint">
          {versions ?? "Versions unknown"}
          {r.update_available && latest ? <> · <strong>update available ({latest})</strong></> : r.update_available ? <> · <strong>update available</strong></> : null}
          {" · "}
          {asOfText(r, now)}
        </dd>
      </div>
    </dl>
  );
}

/** One device's dashboard: the Lectern books on it with their position, and books queued but not downloaded yet. */
export function DeviceDashboardView({ deviceId, latest }: { deviceId: number; latest?: string | null }) {
  const { now } = useStore();
  const [data, setData] = useState<DeviceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    get<DeviceDetail>(`/api/devices/dashboard/${deviceId}`).then(setData, (e) => setError((e as Error).message));
  }, [deviceId]);

  if (error) return <div className="alert error" role="alert">{error}</div>;
  if (!data) return <div className="stack" style={{ gap: 10 }} aria-busy="true" aria-label="Loading device">{Array.from({ length: 3 }, (_, i) => <div key={i} className="skeleton-line" />)}</div>;

  const d = data.device;
  const hidden = data.books.filter((b) => !b.work).length;
  const shown = data.books.filter((b) => b.work);
  return (
    <div className="stack">
      <p className="hint" style={{ margin: 0 }}>
        {d.own ? "Your" : `${d.user.display_name || d.user.username}’s`} <strong>{d.label}</strong> · last sync {relativeTime(d.last_sync_at, now)} · catalogue {relativeTime(d.catalogue_at, now)}
      </p>
      {d.report ? (
        <DeviceReportMeta d={d} now={now} latest={latest} />
      ) : (
        <p className="hint" style={{ margin: 0 }}>
          {d.device_id.startsWith("k") ? "The KOReader plugin has not reported this device’s battery, storage, or books yet. It does so when it syncs." : "A catalogue-only device: Lectern knows only when it last used the catalogue."}
        </p>
      )}
      <section aria-labelledby="dash-on-h">
        <h3 className="detail-h" id="dash-on-h">On the device · {data.books.length}</h3>
        {data.books.length === 0 ? (
          <p className="hint" style={{ margin: 0 }}>{d.report ? "No Lectern books reported on this device." : "Nothing reported yet."}</p>
        ) : (
          <ul className="queue-list">
            {shown.map((b) => (
              <li key={b.work!.id} className="queue-item">
                <Cover work={b.work as Work} className="queue-cover" />
                <span className="queue-text">
                  <strong>{b.work!.title}</strong>
                  <span className="hint">{b.work!.author}</span>
                </span>
                <span className="queue-actions hint">{b.percentage != null ? percent(b.percentage) : "not opened"}</span>
              </li>
            ))}
            {hidden > 0 && (
              <li className="queue-item" data-status="removed">
                <span className="queue-text" style={{ gridColumn: "1 / -1" }}>
                  <strong>{hidden === 1 ? "1 hidden book" : `${hidden} hidden books`}</strong>
                  <span className="hint">No longer shared with {d.own ? "you" : d.user.display_name || d.user.username}, so not shown by title.</span>
                </span>
              </li>
            )}
          </ul>
        )}
        {data.not_in_library > 0 && <p className="hint">{data.not_in_library === 1 ? "1 other book" : `${data.not_in_library} other books`} on the device {data.not_in_library === 1 ? "is" : "are"} no longer in the library.</p>}
      </section>
      <section aria-labelledby="dash-queued-h">
        <h3 className="detail-h" id="dash-queued-h">Queued, not downloaded yet · {data.queued.length}</h3>
        {data.queued.length === 0 ? (
          <p className="hint" style={{ margin: 0 }}>Nothing waiting.</p>
        ) : (
          <ul className="queue-list">
            {data.queued.map((q) => (
              <li key={q.item_id} className="queue-item">
                <Cover work={q.work as Work} className="queue-cover" />
                <span className="queue-text">
                  <strong>{q.work.title}</strong>
                  <span className="hint" title={absoluteTime(q.queued_at)}>Queued {relativeTime(q.queued_at, now)}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
