"use client";

// meiva:note
// The app's navigation value, carried to every component that navigates. There is no fallback to
// the real router: a surface rendered outside <NavigationProvider> is a composition error, the same
// rule <BrowserProvider> states, so a test cannot read a page the app would not have.
import { createContext, useContext, type ReactNode } from "react";

import type { AppNavigation } from "./navigation";

const NavigationContext = createContext<AppNavigation | null>(null);

export function NavigationProvider({
  navigation,
  children,
}: {
  navigation: AppNavigation;
  children: ReactNode;
}) {
  return <NavigationContext value={navigation}>{children}</NavigationContext>;
}

export function useNavigation(): AppNavigation {
  const navigation = useContext(NavigationContext);
  if (navigation === null) {
    throw new Error("useNavigation must be used inside <NavigationProvider>");
  }
  return navigation;
}
