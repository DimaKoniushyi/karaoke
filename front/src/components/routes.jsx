import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Box } from "../theme/ui";

const pages = {
  "/": lazy(() => import("../pages/Library")),
  "/karaoke": lazy(() => import("../pages/Karaoke")),
  "/editor/:songId": lazy(() => import("../pages/MelodyEditor"))
};

export default function AppRoutes({ onOpenAppSettings }) {
  return (
    <Suspense
      fallback={
        <Box aria-hidden="true" sx={{ position: "fixed", inset: 0, zIndex: "var(--z-overlay)" }} />
      }
    >
      <Routes>
        {Object.entries(pages).map(([path, Page]) => (
          <Route key={path} path={path} element={<Page onOpenAppSettings={onOpenAppSettings} />} />
        ))}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
