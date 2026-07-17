import '@mantine/core/styles.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App } from './App';
import { theme } from './theme';
import './styles.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 300_000, gcTime: 600_000, refetchOnWindowFocus: false, refetchOnMount: false },
    mutations: { retry: false },
  },
});

// The bundled server exposes the canonical /ui/ mount, while also serving the
// same shell at / for a convenient local entry point.
const routerBase = window.location.pathname === '/ui' || window.location.pathname.startsWith('/ui/')
  ? '/ui'
  : undefined;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="light">
      <QueryClientProvider client={queryClient}>
        <BrowserRouter basename={routerBase}>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
);
