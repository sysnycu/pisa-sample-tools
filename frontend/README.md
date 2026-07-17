# PISA Research Console

React/TypeScript source for the unified local PISA web interface.

```bash
npm install
npm run dev      # proxies /api to http://127.0.0.1:8000
npm test
npm run build    # writes wheel-ready assets to src/pisa_sample_tools/webapp/static
```

The runtime API contract is rooted at `/api/v1`. Empty datasets and unavailable optional capabilities are represented explicitly; the interface does not fabricate research results.
