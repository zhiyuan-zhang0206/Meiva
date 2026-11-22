# Meiva Windows Session 1 UIA Desktop Helper

Standalone loopback-only Streamable HTTP MCP helper for an external Windows desktop machine
(zero Meiva cluster runtime dependencies; Meiva cluster nodes remain macOS/Linux only per the
2026-10-11 ADR).

## Human Interaction & Safety Contract

- **Sunset Orange Dashed Border (`#ED8755`)**: While a desktop lease/session is active, a topmost,
  click-through, non-activating dashed orange border frames the primary display. It deliberately
  avoids `WDA_EXCLUDEFROMCAPTURE` so humans watching via physical monitor, RustDesk, or Sunshine
  always see the active indicator.
- **Uncontaminated Agent Screenshots**: `screenshot()` briefly hides the overlay, flushes two DWM
  compositor frames (`DwmFlush() x 2`), grabs pixels via `mss`, and restores the border immediately
  so Agent screenshots contain zero orange border pixels.
- **Concurrent Human + Agent Collaboration**: Non-corner human mouse and keyboard activity never
  pauses or blocks Agent calls.
- **PyAutoGUI-Style Top-Left Corner Failsafe (`x <= 4, y <= 4`)**: Moving the mouse (physically or
  via RustDesk/Sunshine; helper's own `0x4D454956`-tagged `SendInput` events are ignored) into the
  primary display top-left corner immediately aborts and releases the active session/lease, hides
  the orange border, and returns `SESSION_ABORTED_BY_CORNER_FAILSAFE` until the pointer leaves the
  corner and a new session is acquired.

## Manual One-Time Installation (No Auto-Deployment)

Meiva never pushes code over SSH automatically. Install once on the Windows host:

1. Copy `services/desktop/win_helper/` to `D:\meiva-win-bridge\win_helper\` on the Windows machine.
2. Create an isolated Python 3.12 virtualenv and install `requirements.txt`:
   ```powershell
   python -m venv D:\meiva-win-bridge\.venv
   D:\meiva-win-bridge\.venv\Scripts\pip.exe install -r D:\meiva-win-bridge\win_helper\requirements.txt
   ```
3. Generate a 256-bit Bearer token in `D:\meiva-win-bridge\secret.token` and restrict NTFS ACLs
   (`icacls /inheritance:r`) to `SYSTEM`, `Administrators`, and the interactive Windows user.
4. Register the interactive Session 1 Scheduled Task (runs under `pythonw.exe` with `RunLevel Highest`
   and binds strictly to `127.0.0.1:18792`):
   ```powershell
   powershell -ExecutionPolicy Bypass -File D:\meiva-win-bridge\win_helper\install_task.ps1
   ```
5. Register the host on your Meiva cluster machine in `$MEIVA_HOME/remote_hosts.toml`:
   ```toml
   [hosts.zzyasus]
   os = "windows"
   ssh_host = "zzyasus"
   helper_port = 18792
   # Optional: omit local_port to let computer-mcp manage an on-demand `ssh -L` tunnel,
   # or set local_port = 18792 to reuse an existing LaunchAgent/systemd `ssh -L` forward.
   local_port = 18792
   token_file = "~/.meiva/secrets/zzyasus.token"
   notes = "Win11 · RTX 2060 · CC4 / Headshot"
   ```
6. Verify discovery from any Meiva cluster node:
   ```bash
   meiva computer list
   ```
