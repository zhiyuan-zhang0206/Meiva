import * as React from "react"

import { cn } from "@/lib/format/utils"
import { FLEX } from "@/lib/layout/layout";

interface TextareaProps extends React.ComponentProps<"textarea"> {
  /** Show the focus ring/border highlight. The composer's message input
   *  turns it off (user ruling 2026-08-06): a blinking caret is the only
   *  focus affordance there — no bold highlighted border. */
  focusVisible?: boolean;
}

// meiva:note
// Explicit credential tokens opt in to autofill; workspace textareas suppress Password / Card /
// Address autofill popups (`CriOS` WKWebView triggers them on bare textareas) and keep >= 16px
// on coarse pointers across phone and tablet widths so WebKit never auto-zooms on focus.
const CREDENTIAL_AUTOCOMPLETE = new Set([
  "username",
  "current-password",
  "new-password",
  "one-time-code",
]);

function Textarea({
  className,
  focusVisible = true,
  autoComplete,
  ...props
}: TextareaProps) {
  const isCredential = autoComplete !== undefined && CREDENTIAL_AUTOCOMPLETE.has(autoComplete);
  return (
    <textarea
      data-slot="textarea"
      autoComplete={autoComplete ?? "off"}
      {...(!isCredential && {
        "data-form-type": "other",
        "data-1p-ignore": "",
        "data-lpignore": "true",
      })}
      className={cn(
        "field-sizing-content min-h-16 w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-base transition-colors outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:pointer-fine:text-sm dark:bg-input/30 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40",
        focusVisible &&
          "focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
        className,
        "pointer-coarse:text-base md:pointer-coarse:text-base",
        FLEX
      )}
      {...props}
    />
  )
}

export { Textarea }
