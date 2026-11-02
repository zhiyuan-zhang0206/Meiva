// meiva:note
// The theme service the composition root hands <Providers>: the provider that wraps the tree and
// the hook that answers the active scheme. next-themes is one implementation of it
// (`system-theme.tsx`, what `app/layout.tsx` passes); a test passes its own instead of replacing
// the module, which is why this exists as a value at all.
import type { ComponentType, ReactNode } from "react";

export type ThemeProviderComponent = ComponentType<{ nonce?: string; children: ReactNode }>;

export interface ThemeRuntime {
  Provider: ThemeProviderComponent;
  useResolvedTheme: () => string | undefined;
}
