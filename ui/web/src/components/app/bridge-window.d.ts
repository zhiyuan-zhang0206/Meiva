// meiva:note
// The native host shell (`ui/app` on macOS and Android) publishes `window.__MEIVA_APP__`
// before the console boots and refreshes it on page load / update events. The web
// console reads only this typed bridge contract — never raw Tauri internals.
// meiva:link ui/app/docs/app.md
import type { HostShellPayload } from "./bridge";

declare global {
  interface Window {
    readonly __MEIVA_APP__?: HostShellPayload;
  }
}

export {};
