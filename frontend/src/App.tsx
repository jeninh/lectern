import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { StoreProvider, useStore } from "./lib/store";
import { ActivityPage } from "./pages/Activity";
import { AddDevicePage } from "./pages/AddDevice";
import { DevicesPage } from "./pages/Devices";
import { HomePage } from "./pages/Home";
import { LibraryPage } from "./pages/Library";
import { SecurityPage } from "./pages/Security";
import { UsersPage } from "./pages/Users";

function OwnerOnly({ children }: { children: JSX.Element }) {
  const { me } = useStore();
  if (!me) return null;
  return me.user.role === "owner" ? children : <Navigate to="/app" replace />;
}

export default function App() {
  return (
    <StoreProvider>
      <Shell>
        <Routes>
          <Route path="/app" element={<HomePage />} />
          <Route path="/app/library" element={<LibraryPage />} />
          <Route path="/app/library/:id" element={<LibraryPage />} />
          <Route path="/app/devices" element={<DevicesPage />} />
          <Route path="/app/devices/new" element={<AddDevicePage />} />
          <Route path="/app/security" element={<SecurityPage />} />
          <Route path="/app/activity" element={<ActivityPage />} />
          <Route path="/app/users" element={<OwnerOnly><UsersPage /></OwnerOnly>} />
          <Route path="*" element={<Navigate to="/app" replace />} />
        </Routes>
      </Shell>
    </StoreProvider>
  );
}
