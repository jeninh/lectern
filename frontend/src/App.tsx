import { lazy, Suspense } from "react";
import { Navigate, Route, Routes, useSearchParams } from "react-router-dom";
import { Shell } from "./components/Shell";
import { StoreProvider, useStore } from "./lib/store";
import { ActivityPage } from "./pages/Activity";
import { DevicesPage } from "./pages/Devices";
import { HomePage } from "./pages/Home";
import { LibraryPage } from "./pages/Library";
import { ReadAlongPage } from "./pages/ReadAlong";
import { SecurityPage } from "./pages/Security";
import { AdminPage } from "./pages/Admin";
import { HighlightsPage } from "./pages/Highlights";
import { ListeningPage } from "./pages/Listening";
import { TrashPage } from "./pages/Trash";
import { DownloadsPage } from "./pages/Downloads";
import { ReaderPage } from "./pages/Reader";
import { PlayerProvider } from "./lib/player";
import { sharedUrl } from "./lib/ingest";

// The docs and their Markdown parser load on first visit (they are precached for offline use).
const DocsPage = lazy(() => import("./pages/Docs"));
const docs = <Suspense fallback={<div className="hint">Loading…</div>}><DocsPage /></Suspense>;

function OwnerOnly({ children }: { children: JSX.Element }) {
  const { me } = useStore();
  if (!me) return null;
  return me.user.role === "owner" ? children : <Navigate to="/app" replace />;
}

function EditorsOnly({ children }: { children: JSX.Element }) {
  const { me, canEdit } = useStore();
  if (!me) return null;
  return canEdit ? children : <Navigate to="/app" replace />;
}

/** The installed app's share target (manifest.webmanifest): a shared link opens the
 * "Save a web article" dialog in the Library with it filled in. */
function ShareTarget() {
  const [params] = useSearchParams();
  const url = sharedUrl(params);
  return <Navigate to={`/app/library?article=${encodeURIComponent(url ?? "1")}`} replace />;
}

export default function App() {
  return (
    <StoreProvider>
      <PlayerProvider>
      <Shell>
        <Routes>
          <Route path="/app" element={<HomePage />} />
          <Route path="/app/library" element={<LibraryPage />} />
          <Route path="/app/library/:id" element={<LibraryPage />} />
          <Route path="/app/read/:id" element={<ReadAlongPage />} />
          <Route path="/app/devices" element={<DevicesPage />} />
          <Route path="/app/devices/new" element={<DevicesPage />} />
          <Route path="/app/listening" element={<ListeningPage />} />
          <Route path="/app/downloads" element={<DownloadsPage />} />
          <Route path="/app/reader/:id" element={<ReaderPage />} />
          <Route path="/app/highlights" element={<HighlightsPage />} />
          <Route path="/app/security" element={<SecurityPage />} />
          <Route path="/app/activity" element={<ActivityPage />} />
          <Route path="/app/trash" element={<EditorsOnly><TrashPage /></EditorsOnly>} />
          <Route path="/app/save-article" element={<EditorsOnly><ShareTarget /></EditorsOnly>} />
          <Route path="/app/docs" element={docs} />
          <Route path="/app/docs/:slug" element={docs} />
          <Route path="/app/admin" element={<OwnerOnly><AdminPage /></OwnerOnly>} />
          <Route path="/app/users" element={<Navigate to="/app/admin" replace />} />
          <Route path="*" element={<Navigate to="/app" replace />} />
        </Routes>
      </Shell>
      </PlayerProvider>
    </StoreProvider>
  );
}
