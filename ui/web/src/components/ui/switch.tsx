"use client"

import * as SwitchPrimitive from "@radix-ui/react-switch"

import { cn } from "@/lib/format/utils"

// meiva:note
// The default tone follows the theme accent. Dense inventories use a neutral
// track and light thumb so repeated enabled controls do not dominate the list.
function Switch({
  className,
  tone = "default",
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root> & { tone?: "default" | "neutral" }) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      className={cn(
        "peer focus-visible:border-ring focus-visible:ring-ring/50 inline-flex h-[1.15rem] w-8 shrink-0 items-center rounded-full border border-transparent shadow-xs transition-all outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50",
        tone === "neutral"
          ? "data-[state=checked]:bg-zinc-600 dark:data-[state=checked]:bg-zinc-500 data-[state=unchecked]:bg-zinc-300 dark:data-[state=unchecked]:bg-zinc-800"
          : "data-[state=checked]:bg-primary data-[state=unchecked]:bg-input",
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className={cn(
          "pointer-events-none block size-4 rounded-full ring-0 transition-transform data-[state=checked]:translate-x-[calc(100%-2px)] data-[state=unchecked]:translate-x-0",
          tone === "neutral" ? "bg-white" : "bg-background"
        )}
      />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
