import * as React from "react"

import { cn } from "@/lib/format/utils"
import { MIN_W_0 } from "@/lib/layout/layout";

// meiva:note
// Explicit credential tokens opt in to browser and password-manager autofill; every other
// internal workspace input suppresses Password / Credit Card / Address autofill popups and
// enforces >= 16px on coarse pointers (including >= 768px tablets) so WebKit never auto-zooms.
const CREDENTIAL_AUTOCOMPLETE = new Set([
  "username",
  "current-password",
  "new-password",
  "one-time-code",
]);

function Input({ className, type, autoComplete, ...props }: React.ComponentProps<"input">) {
  const isCredential = autoComplete !== undefined && CREDENTIAL_AUTOCOMPLETE.has(autoComplete);
  return (
    <input
      type={type}
      data-slot="input"
      autoComplete={autoComplete ?? "off"}
      {...(!isCredential && {
        "data-form-type": "other",
        "data-1p-ignore": "",
        "data-lpignore": "true",
      })}
      className={cn(
        "h-8 w-full rounded-lg border border-input bg-transparent px-2.5 py-1 text-base transition-colors outline-none file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:pointer-fine:text-sm dark:bg-input/30 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40",
        className,
        "pointer-coarse:text-base md:pointer-coarse:text-base",
        MIN_W_0
      )}
      {...props}
    />
  )
}

export { Input }
