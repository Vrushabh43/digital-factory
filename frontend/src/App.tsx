import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useLive } from "./api/live";
import { Banners } from "./components/Banners";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Header } from "./components/Header";
import { FactoryMap } from "./pages/FactoryMap";
import { History } from "./pages/History";
import { PiecesNow } from "./pages/PiecesNow";

export function Footer() {
  const { plant } = useLive();
  if (!plant) return null;
  const verified = plant.node_map_verification === "verified";
  return (
    <p className="footer-note">
      This dashboard only reads data from the factory; it never controls it. Times are in {plant.timezone} time.
      {!verified &&
        " Note: the list of factory signals the dashboard reads was taken from the PLC program and has not yet been checked on the real factory, so single values may be wrong."}
    </p>
  );
}

export function App() {
  const location = useLocation();
  return (
    <>
      <Header />
      <Banners />
      <ErrorBoundary resetKey={`${location.pathname}${location.search}`}>
        <Routes>
          <Route path="/" element={<FactoryMap />} />
          <Route path="/pieces" element={<PiecesNow />} />
          <Route path="/history" element={<History />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </ErrorBoundary>
      <Footer />
    </>
  );
}
