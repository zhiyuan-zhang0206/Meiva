"use client";

// meiva:note
// The one file that names next/navigation: it turns the app router's current pathname and its
// push/replace into the navigation value every component reads.
import { usePathname, useRouter } from "next/navigation";
import { useMemo, type ReactNode } from "react";

import type { AppNavigation } from "./navigation";
import { NavigationProvider } from "./navigation-provider";

export function SystemNavigationProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const navigation = useMemo<AppNavigation>(
    () => ({
      pathname: () => pathname,
      push: (href) => router.push(href),
      replace: (href) => router.replace(href),
    }),
    [pathname, router],
  );
  return <NavigationProvider navigation={navigation}>{children}</NavigationProvider>;
}
