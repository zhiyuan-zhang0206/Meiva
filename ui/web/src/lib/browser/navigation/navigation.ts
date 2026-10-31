// meiva:note
// Where the app is, and the two moves its surfaces make. A component reads this value instead of
// importing the router, so a redirect or a jump is a call on a value a test hands in — `push` adds
// a history entry, `replace` does not, and `pathname()` answers the address the current render was
// decided on.
//
// A destination is `next`'s `Route`, the type the router's own `push` accepts (typed routes are on),
// so a caller writes the path it means and the checker holds it to a route the app has.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md
import type { Route } from "next";

export interface AppNavigation {
  pathname(): string;
  push(href: Route): void;
  replace(href: Route): void;
}
