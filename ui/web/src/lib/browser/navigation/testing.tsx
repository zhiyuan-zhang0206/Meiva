// meiva:note
// The navigation value a test hands in: the moves a component made, in order, each labelled with
// the call it stands for — so `replace` and `push` are told apart by the record, not by which spy
// the test happened to wire. It lives beside the port because a component test drives it directly
// and lib/ may not import src/test-support/.
import type { ReactNode } from "react";

import type { Route } from "next";

import type { AppNavigation } from "./navigation";
import { NavigationProvider } from "./navigation-provider";

// meiva:note
// One move the component asked for: `kind` is the call (`push` adds history, `replace` does not)
// and `href` what it asked for.
export interface NavigationMove {
  readonly kind: "push" | "replace";
  readonly href: Route;
}

export interface FakeNavigation extends AppNavigation {
  // meiva:note
  // Move the page the component renders on, the way a route change would.
  moveTo(pathname: string): void;
  readonly moves: NavigationMove[];
}

export function fakeNavigation(initialPathname = "/"): FakeNavigation {
  let pathname = initialPathname;
  const moves: NavigationMove[] = [];
  return {
    pathname: () => pathname,
    push: (href) => void moves.push({ kind: "push", href }),
    replace: (href) => void moves.push({ kind: "replace", href }),
    moveTo(next) {
      pathname = next;
    },
    moves,
  };
}

export function navigationWrapper(navigation: AppNavigation) {
  return function Page({ children }: { children: ReactNode }) {
    return <NavigationProvider navigation={navigation}>{children}</NavigationProvider>;
  };
}
