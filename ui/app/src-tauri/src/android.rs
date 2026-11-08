//! Android specifics: native plugins and notification permission.
//!
//! Notifications on Android are raised only when an actual agent notification
//! event arrives (or is caught up on resume/reconnect), never as a permanent
//! foreground-service status bar item.

use tauri::plugin::{Builder as PluginBuilder, PluginHandle, TauriPlugin};
use tauri::{AppHandle, Manager, Runtime};

/// Java package the overlay installs the plugin class into. Must match
/// `identifier` in `tauri.conf.json`, which is what the Android project's
/// package name is derived from.
pub(crate) const PLUGIN_PACKAGE: &str = "com.meiva.app";
const SECRET_PLUGIN_CLASS: &str = "MeivaSecretPlugin";
const SECRET_PLUGIN_NAME: &str = "meivasecret";
const COOKIE_PLUGIN_CLASS: &str = "MeivaCookiePlugin";
const COOKIE_PLUGIN_NAME: &str = "meivacookie";
const CLICK_PLUGIN_CLASS: &str = "MeivaClickPlugin";
const CLICK_PLUGIN_NAME: &str = "meivaclick";

/// `run_mobile_plugin` must never be called on the Android main thread: the
/// JNI round-trip is serviced by the main looper, so a main-thread call
/// deadlocks waiting for the response it prevents. All command wrappers below
/// use the async API, and notification's synchronous public API runs on the
/// blocking executor for the same reason.

/// Managed handle to the Keystore plugin. A missing handle is never treated as
/// an empty secret when saving: storing a credential must fail closed.
#[derive(Clone)]
struct SecretPlugin<R: Runtime>(PluginHandle<R>);

/// Managed handle to the Android WebView cookie-store bridge. A missing handle
/// must fail native login rather than silently losing its session cookie.
#[derive(Clone)]
struct CookiePlugin<R: Runtime>(PluginHandle<R>);

/// Managed handle to the notification-click mailbox. A missing overlay means
/// the app continues without notification deep-link capture.
#[derive(Clone)]
struct ClickPlugin<R: Runtime>(PluginHandle<R>);

#[derive(serde::Deserialize)]
struct StoredSecret {
    secret: Option<String>,
}

/// Register the Kotlin Android-Keystore plugin.
pub fn secret_plugin<R: Runtime>() -> TauriPlugin<R> {
    PluginBuilder::new(SECRET_PLUGIN_NAME)
        .setup(|app, api| {
            match api.register_android_plugin(PLUGIN_PACKAGE, SECRET_PLUGIN_CLASS) {
                Ok(handle) => {
                    app.manage(SecretPlugin(handle));
                }
                Err(err) => log::error!("Keystore secret plugin unavailable: {err}"),
            }
            Ok(())
        })
        .build()
}

/// Register the Kotlin Android-WebView cookie plugin.
pub fn cookie_plugin<R: Runtime>() -> TauriPlugin<R> {
    PluginBuilder::new(COOKIE_PLUGIN_NAME)
        .setup(|app, api| {
            match api.register_android_plugin(PLUGIN_PACKAGE, COOKIE_PLUGIN_CLASS) {
                Ok(handle) => {
                    app.manage(CookiePlugin(handle));
                }
                Err(err) => log::error!("Android cookie plugin unavailable: {err}"),
            }
            Ok(())
        })
        .build()
}

/// Register the Kotlin notification-click plugin.
pub fn click_plugin<R: Runtime>() -> TauriPlugin<R> {
    PluginBuilder::new(CLICK_PLUGIN_NAME)
        .setup(|app, api| {
            match api.register_android_plugin(PLUGIN_PACKAGE, CLICK_PLUGIN_CLASS) {
                Ok(handle) => {
                    app.manage(ClickPlugin(handle));
                }
                // A missing class means the Android overlay was not applied.
                // The app still starts; it simply cannot capture click taps.
                Err(err) => log::error!("notification-click plugin unavailable: {err}"),
            }
            Ok(())
        })
        .build()
}

/// Consume the pending notification click, if the Android overlay is present.
pub async fn take_pending_click<R: Runtime>(app: &AppHandle<R>) -> Option<bool> {
    let Some(plugin) = app
        .try_state::<ClickPlugin<R>>()
        .map(|plugin| plugin.0.clone())
    else {
        return None;
    };
    // See the Android main-thread rule above: never use run_mobile_plugin here.
    match plugin
        .run_mobile_plugin_async::<serde_json::Value>("takePendingClick", serde_json::json!({}))
        .await
    {
        Ok(result) => result.get("pending").and_then(serde_json::Value::as_bool),
        Err(err) => {
            log::error!("could not consume notification click: {err}");
            None
        }
    }
}

/// Store the cluster secret after a successful native login only.
pub async fn save_secret_async<R: Runtime>(app: &AppHandle<R>, secret: &str) -> Result<(), String> {
    let plugin = app
        .try_state::<SecretPlugin<R>>()
        .map(|plugin| plugin.0.clone())
        .ok_or_else(|| "secure cluster-secret storage is unavailable".to_string())?;
    // See the Android main-thread rule above: never use run_mobile_plugin here.
    plugin
        .run_mobile_plugin_async::<serde_json::Value>(
            "save",
            serde_json::json!({ "secret": secret }),
        )
        .await
        .map(|_| ())
        .map_err(|_| "could not save the cluster secret securely".to_string())
}

/// Put a session cookie into Android WebView and wait until the cookie manager
/// accepts it. Unlike Wry's Android `set_cookie`, the native cookie manager
/// actually writes to the WebView cookie store.
pub async fn install_session_cookie_async<R: Runtime>(
    app: &AppHandle<R>,
    url: &str,
    cookie: String,
) -> Result<(), String> {
    let plugin = app
        .try_state::<CookiePlugin<R>>()
        .map(|plugin| plugin.0.clone())
        .ok_or_else(|| "Android session-cookie storage is unavailable".to_string())?;
    // See the Android main-thread rule above: never use run_mobile_plugin here.
    plugin
        .run_mobile_plugin_async::<serde_json::Value>(
            "set",
            serde_json::json!({ "url": url, "cookie": cookie }),
        )
        .await
        .map(|_| ())
        .map_err(|_| "could not install the session cookie".to_string())
}

/// Load and cache the Keystore secret without blocking the Android main looper.
///
/// The first caller may observe no cached value while this request is in
/// flight; a read failure is equivalent to no startup credential.
pub async fn load_stored_secret<R: Runtime>(app: &AppHandle<R>) -> Option<String> {
    let state = app.state::<crate::state::AppState>();
    if state.android_secret_loaded() {
        return state.android_secret();
    }

    let Some(plugin) = app
        .try_state::<SecretPlugin<R>>()
        .map(|plugin| plugin.0.clone())
    else {
        return state.cache_android_secret_if_unloaded(None);
    };
    // The Android main-thread rule above requires this async JNI path.
    let secret = plugin
        .run_mobile_plugin_async::<StoredSecret>("get", serde_json::json!({}))
        .await
        .ok()
        .and_then(|stored| stored.secret);
    state.cache_android_secret_if_unloaded(secret)
}

/// Clear an explicitly removed Android secret without affecting settings.json.
pub async fn clear_secret_async<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let plugin = app
        .try_state::<SecretPlugin<R>>()
        .map(|plugin| plugin.0.clone())
        .ok_or_else(|| "secure cluster-secret storage is unavailable".to_string())?;
    // See the Android main-thread rule above: never use run_mobile_plugin here.
    plugin
        .run_mobile_plugin_async::<serde_json::Value>("clear", serde_json::json!({}))
        .await
        .map(|_| ())
        .map_err(|_| "could not clear the cluster secret".to_string())
}

/// Ask for Android notification permission without blocking the main looper.
pub async fn request_notification_permission_async<R: Runtime>(app: AppHandle<R>) {
    let result = tauri::async_runtime::spawn_blocking(move || {
        use tauri_plugin_notification::NotificationExt;

        let notifier = app.notification();
        match notifier.permission_state() {
            Ok(tauri_plugin_notification::PermissionState::Granted) => Ok(()),
            // Notification's public API is synchronous and calls
            // run_mobile_plugin internally, so this must stay off the main thread.
            _ => notifier
                .request_permission()
                .map(|_| ())
                .map_err(|err| err.to_string()),
        }
    })
    .await;
    match result {
        Ok(Err(err)) => log::warn!("notification permission request failed: {err}"),
        Err(err) => log::error!("notification permission task failed: {err}"),
        Ok(Ok(())) => {}
    }
}

/// Show an Android notification without synchronously invoking its plugin.
pub async fn show_notification<R: Runtime>(app: AppHandle<R>, title: String, body: String) {
    let result = tauri::async_runtime::spawn_blocking(move || {
        use tauri_plugin_notification::NotificationExt;

        // NotificationBuilder::show also uses run_mobile_plugin internally.
        app.notification()
            .builder()
            .title(&title)
            .body(&body)
            .show()
            .map_err(|err| err.to_string())
    })
    .await;
    match result {
        Ok(Err(err)) => log::error!("could not show a notification: {err}"),
        Err(err) => log::error!("notification task failed: {err}"),
        Ok(Ok(())) => {}
    }
}

/// Startup wiring: restore the persisted notification permission choice. Fresh
/// installs default it off, so onboarding happens first.
pub fn setup<R: Runtime>(app: &AppHandle<R>) {
    let settings = app.state::<crate::state::AppState>().settings();
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        // Prime the cache asynchronously. Window startup uses the same loader
        // before it attempts auto-login, so no main-thread JNI call is needed.
        let _ = load_stored_secret(&handle).await;
        if settings.notifications {
            request_notification_permission_async(handle.clone()).await;
        }
    });
}
