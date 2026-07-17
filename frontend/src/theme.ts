import { createTheme } from '@mantine/core';

export const theme = createTheme({
  primaryColor: 'indigo',
  primaryShade: { light: 6, dark: 5 },
  fontFamily: 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  fontFamilyMonospace: '"IBM Plex Mono", "SFMono-Regular", Consolas, monospace',
  headings: {
    fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif',
    fontWeight: '650',
  },
  defaultRadius: 'md',
  colors: {
    research: [
      '#eef3ff', '#dce5ff', '#b8c9ff', '#91a9ff', '#708dff',
      '#5d7cff', '#526ff0', '#425dcf', '#364daa', '#304487',
    ],
  },
  components: {
    Card: { defaultProps: { radius: 'lg', withBorder: true } },
    Button: { defaultProps: { radius: 'md' } },
    TextInput: { defaultProps: { radius: 'md' } },
    Select: { defaultProps: { radius: 'md' } },
  },
});
