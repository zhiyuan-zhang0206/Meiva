# Acquire Home streams after commit

Home's connection hooks must acquire their StreamHub transport inside the
connection effect, after authentication and visibility permit participation.
A transport constructor owns browser listeners and broadcast channels; render
can run on the server or be abandoned before commit, so it cannot own that work.
The hub continues to own transport sharing and disposal across subscriptions.

On GitHub main 7803dcd527, an isolated Next 16.3.8 development preview returned
HTTP 500 for a plain Home document request, before any browser API fixtures.
Render called transportFor, whose constructor read document. React then rebuilt
the page on the client and warned about the next-themes inline script. Moving
acquisition into both connection effects produced HTTP 200 and removed both
errors with the existing ThemeProvider unchanged, with and without browser
fixtures. The pre-hydration theme script remains necessary to avoid theme flash.
Hiding the warning, disabling SSR, or returning dummy browser objects would
leave the lifecycle defect in place.

Evidence is development-mode only; production-mode impact was not measured.
The locked Next guide and current official Server and Client Components guide
both describe prerendering Client Components on first load:
https://nextjs.org/docs/app/getting-started/server-and-client-components.
