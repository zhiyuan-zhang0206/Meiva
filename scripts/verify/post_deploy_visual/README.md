# Real-browser structural probes

`layout_assertions.py` is the only module here: document overflow, viewport
containment, center-point occlusion, nonempty blocks, settle-before-capture and
the bounded wait for asynchronously mounted panels. The layout suites in
`tests/e2e/visual/` consume it so their definitions cannot drift.

The pixel gate that gave this directory its name (the deployment matrix, its 44
goldens, the refresh tool and the CI minting workflow) was removed on 2026-10-20;
nothing here renders or compares pixels.
