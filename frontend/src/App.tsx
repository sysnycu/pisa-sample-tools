import { lazy, Suspense } from 'react';
import { Button, Card } from '@mantine/core';
import { IconCompass } from '@tabler/icons-react';
import { Link, Navigate, Route, Routes } from 'react-router-dom';
import { AppLayout } from './components/AppLayout';
import { EmptyState, PageLoading } from './components/Feedback';

const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })));
const ExperimentsPage = lazy(() => import('./pages/ExperimentsPage').then((module) => ({ default: module.ExperimentsPage })));
const SamplesPage = lazy(() => import('./pages/SamplesPage').then((module) => ({ default: module.SamplesPage })));
const ReportWorkspacePage = lazy(() => import('./pages/ReportWorkspacePage').then((module) => ({ default: module.ReportWorkspacePage })));
const AdvancedToolsPage = lazy(() => import('./pages/AdvancedToolsPage').then((module) => ({ default: module.AdvancedToolsPage })));

export function App() {
  return (
    <Suspense fallback={<PageLoading label="Opening workspace…" />}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<DashboardPage />} />
          <Route path="experiments" element={<ExperimentsPage />} />
          <Route path="samples" element={<SamplesPage />} />
          <Route path="reports" element={<Navigate to="/#reports" replace />} />
          <Route path="reports/:datasetId/:section?/:runId?" element={<ReportWorkspacePage />} />
          <Route path="advanced" element={<AdvancedToolsPage />} />
          <Route path="*" element={<Card><EmptyState title="Page not found" description="This workspace route does not exist." icon={<IconCompass size={23} />} action={<Button component={Link} to="/">Return to Dashboard</Button>} /></Card>} />
        </Route>
      </Routes>
    </Suspense>
  );
}
