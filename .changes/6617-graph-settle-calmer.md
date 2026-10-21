type: Changed

- The Fleet graphs settle in a quarter of the time they did: the shared force
  default `alphaDecay` moves from d3's 0.0228 to 0.08, so the 40-node agent
  graph's entrance runs 82 ticks (about 1.4 s at 60 fps) instead of 295 (4.9 s),
  with average node travel down from 108 px to 79 px and no overlapping nodes at
  rest. The landing the user likes stays; the amount it moves does not (user
  2026-10-21). Only the default moves, so a browser that already stored
  `display.*_force_params` keeps its own value and the slider range stays
  0.001-0.2
  ([fleet view](ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md)).
