import { Skeleton } from "antd";
import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { useProject } from "./ProjectContext";

const OverviewPage = lazy(() => import("./pages/OverviewPage").then((module) => ({ default: module.OverviewPage })));
const ChecksPage = lazy(() => import("./pages/ChecksPage").then((module) => ({ default: module.ChecksPage })));
const MonitoringPage = lazy(() => import("./pages/MonitoringPage").then((module) => ({ default: module.MonitoringPage })));
const RunsPage = lazy(() => import("./pages/RunsPage").then((module) => ({ default: module.RunsPage })));
const RunDetailPage = lazy(() => import("./pages/RunDetailPage").then((module) => ({ default: module.RunDetailPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((module) => ({ default: module.SettingsPage })));
const MembersPage = lazy(() => import("./pages/MembersPage").then((module) => ({ default: module.MembersPage })));
const ProjectsPage = lazy(() => import("./pages/ProjectsPage").then((module) => ({ default: module.ProjectsPage })));
const DebugPage = lazy(() => import("./pages/DebugPage").then((module) => ({ default: module.DebugPage })));
const OperationsPage = lazy(() => import("./pages/OperationsPage").then((module) => ({ default: module.OperationsPage })));

function page(element: ReactNode) {
  return <Suspense fallback={<RouteLoading />}>{element}</Suspense>;
}

function RouteLoading() {
  return (
    <div className="page-content">
      <Skeleton active paragraph={{ rows: 8 }} />
    </div>
  );
}

export default function App() {
  const { currentProject } = useProject();
  return (
    <Routes key={currentProject.id}>
      <Route element={<Layout />}>
        <Route path="/" element={page(<OverviewPage />)} />
        <Route path="/ui-checks" element={page(<ChecksPage type="ui" />)} />
        <Route path="/api-checks" element={page(<ChecksPage type="api" />)} />
        <Route path="/monitoring" element={page(<MonitoringPage />)} />
        <Route path="/runs" element={page(<RunsPage />)} />
        <Route path="/runs/:runId" element={page(<RunDetailPage />)} />
        <Route path="/operations" element={page(<OperationsPage />)} />
        <Route path="/settings" element={<Navigate to="/settings/execution" replace />} />
        <Route path="/settings/alerts" element={<Navigate to="/projects/alerts" replace />} />
        <Route path="/settings/:tab" element={page(<SettingsPage />)} />
        <Route path="/members" element={page(<MembersPage />)} />
        <Route path="/projects" element={page(<ProjectsPage />)} />
        <Route path="/projects/alerts" element={page(<SettingsPage tabOverride="alerts" />)} />
        <Route path="/debug/:type/:checkId" element={page(<DebugPage />)} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
