//! Desktop residency: the tray, the close-hides-to-tray rule, and updates.
//!
//! The app is meant to sit in the tray all day. Closing the window hides it;
//! the only real exit is the tray's Quit (or Cmd+Q), keeping the console one
//! click away.

use tauri::menu::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem};
use tauri::tray::{TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, WebviewWindow};

use crate::notify::NotifyMode;
use crate::window::{self, MAIN_WINDOW};

const MENU_OPEN: &str = "open";
const MENU_AUTOSTART: &str = "autostart";
const MENU_UPDATE: &str = "update";
const MENU_QUIT: &str = "quit";

/// Bring the window back from the tray.
pub fn show_main_window(app: &AppHandle) {
    match app.get_webview_window(MAIN_WINDOW) {
        Some(window) => {
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
        }
        // The window is only ever absent if a rebuild failed; recreate it
        // rather than leaving the tray icon controlling nothing.
        None => window::open_entry(app),
    }
}

/// Close hides instead of quitting — the app stays resident — and the native
/// webview background tracks OS light/dark theme switches.
pub fn attach_window_behavior(window: &WebviewWindow) {
    let handle = window.clone();
    window.on_window_event(move |event| match event {
        tauri::WindowEvent::CloseRequested { api, .. } => {
            api.prevent_close();
            let _ = handle.hide();
        }
        tauri::WindowEvent::ThemeChanged(theme) => {
            let _ = handle.set_background_color(Some(window::theme_background_color(*theme)));
        }
        _ => {}
    });
}

/// Build the tray icon and its menu.
pub fn setup_tray(app: &AppHandle) -> tauri::Result<()> {
    let autostart_on = autostart_enabled(app);
    let open = MenuItem::with_id(app, MENU_OPEN, "Open Meiva", true, None::<&str>)?;
    let autostart = CheckMenuItem::with_id(
        app,
        MENU_AUTOSTART,
        "Launch at login",
        true,
        autostart_on,
        None::<&str>,
    )?;
    let update = MenuItem::with_id(app, MENU_UPDATE, "Check for updates…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, MENU_QUIT, "Quit Meiva", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &open,
            &PredefinedMenuItem::separator(app)?,
            &autostart,
            &update,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;

    let mut tray = TrayIconBuilder::with_id("meiva")
        .tooltip("Meiva")
        .menu(&menu)
        // A left click opens the window; the menu is the right-click surface.
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button, .. } = event {
                if button == tauri::tray::MouseButton::Left {
                    show_main_window(tray.app_handle());
                }
            }
        })
        .on_menu_event(on_menu_event);
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

fn on_menu_event(app: &AppHandle, event: MenuEvent) {
    match event.id().as_ref() {
        MENU_OPEN => show_main_window(app),
        MENU_AUTOSTART => toggle_autostart(app),
        MENU_UPDATE => check_for_updates(app.clone(), true),
        MENU_QUIT => app.exit(0),
        other => log::warn!("unknown tray menu item '{other}'"),
    }
}

fn autostart_enabled(app: &AppHandle) -> bool {
    use tauri_plugin_autostart::ManagerExt;
    app.autolaunch().is_enabled().unwrap_or(false)
}

fn toggle_autostart(app: &AppHandle) {
    use tauri_plugin_autostart::ManagerExt;
    let manager = app.autolaunch();
    let result = if autostart_enabled(app) {
        manager.disable()
    } else {
        manager.enable()
    };
    if let Err(err) = result {
        log::error!("could not change the launch-at-login setting: {err}");
    }
}

/// Ask the updater for a newer release and install it when one exists.
///
/// `interactive` distinguishes the tray's "Check for updates…" from the silent
/// startup check: only the former is worth surfacing when nothing is found.
pub fn check_for_updates(app: AppHandle, interactive: bool) {
    tauri::async_runtime::spawn(async move {
        use tauri_plugin_updater::UpdaterExt;

        let updater = match app.updater() {
            Ok(updater) => updater,
            Err(err) => {
                log::error!("updater unavailable: {err}");
                return;
            }
        };
        match updater.check().await {
            Ok(Some(update)) => {
                let version = update.version.clone();
                log::info!("installing update {version}");
                match update.download_and_install(|_, _| {}, || {}).await {
                    // The new binary only runs after a restart; the user is
                    // mid-session, so ask rather than yanking the window away.
                    Ok(()) => log::info!("update {version} staged — restart Meiva to run it"),
                    Err(err) => log::error!("could not install update {version}: {err}"),
                }
            }
            Ok(None) => {
                if interactive {
                    log::info!("no update available");
                }
            }
            Err(err) => log::error!("update check failed: {err}"),
        }
    });
}

/// Wire the native notification centre at startup (macOS only; a no-op elsewhere).
///
/// The delegate must exist before the app finishes launching so a tap that launched it is
/// delivered. Authorization is requested here when the switch is already on, so an install that
/// turned notifications on before this build gets its one-time system prompt.
pub fn setup_notifications(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    if crate::macos_notify::is_available() {
        crate::macos_notify::install(app);
        if app
            .state::<crate::state::AppState>()
            .settings()
            .notifications
        {
            crate::macos_notify::request_authorization();
        }
    }
    #[cfg(not(target_os = "macos"))]
    let _ = app;
}

/// Ask the operating system for permission to post notifications.
///
/// macOS answers with its own one-time prompt and remembers the choice, so this is safe to call
/// whenever the user turns the switch on; a later denial is left alone rather than re-prompted.
/// The other desktop platforms have no permission prompt for the plugin to trigger.
pub fn request_notification_permission(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    if crate::macos_notify::is_available() {
        crate::macos_notify::request_authorization();
        return;
    }
    let _ = app;
}

/// Render one delivered notification on the desktop.
///
/// The console window stays the full view; this renders the same delivered notification event
/// Android renders, so both platforms agree on what is worth interrupting for.
///
/// On macOS from an app bundle all three modes go through the native notification centre
/// (`macos_notify`). Everywhere else the notification plugin can only post: it has no id, no
/// silent update and no way to withdraw one that is shown, so a replace and a retract do nothing
/// and the one alert per unread round stays until the user clears it. A macOS binary that is not
/// in a bundle (`tauri dev`) takes that same path.
pub fn show_notification(
    app: &AppHandle,
    mode: NotifyMode,
    tag: &str,
    title: String,
    body: String,
) {
    use tauri_plugin_notification::NotificationExt;

    #[cfg(target_os = "macos")]
    if crate::macos_notify::is_available() {
        crate::macos_notify::apply(mode, tag, title, body);
        return;
    }
    let _ = tag;

    if mode != NotifyMode::Alert {
        log::debug!("desktop notifications cannot {mode:?} a shown notification");
        return;
    }

    if let Err(err) = app
        .notification()
        .builder()
        .title(&title)
        .body(&body)
        .show()
    {
        log::error!("could not show a notification: {err}");
    }
}
