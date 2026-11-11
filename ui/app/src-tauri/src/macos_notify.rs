//! macOS notifications through `UNUserNotificationCenter`.
//!
//! The notification plugin posts through the legacy API and has no identifier, so it can neither
//! replace nor withdraw a notification. This module is the thin native layer that can: a request
//! added under an identifier that is already delivered replaces it, and
//! `removeDeliveredNotifications` withdraws it. What to do is decided by the pure
//! `notify::decide_mac`; this file only talks to the system.
//!
//! `UNUserNotificationCenter` exists only for a process inside a `.app` bundle with a bundle
//! identifier. `tauri dev` runs the bare binary, where asking for the centre raises an
//! Objective-C exception, so [`is_available`] gates every call and the caller falls back to the
//! plugin there.

use std::ptr::NonNull;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;

use block2::RcBlock;
use objc2::rc::Retained;
use objc2::runtime::{NSObject, NSObjectProtocol};
use objc2::{define_class, msg_send, sel, AnyThread};
use objc2_foundation::{NSArray, NSBundle, NSError, NSString};
use objc2_user_notifications::{
    UNAuthorizationOptions, UNMutableNotificationContent, UNNotification,
    UNNotificationInterruptionLevel, UNNotificationPresentationOptions, UNNotificationRequest,
    UNNotificationResponse, UNNotificationSound, UNUserNotificationCenter,
    UNUserNotificationCenterDelegate,
};
use tauri::AppHandle;

use crate::notify::{decide_mac, mac_needs_delivered, MacOp, NotifyMode};

/// Set by a tap on a notification, consumed by `app_take_pending_click`.
static PENDING_CLICK: AtomicBool = AtomicBool::new(false);
static APP: OnceLock<AppHandle> = OnceLock::new();

/// Whether this process can use the notification centre: it is the main executable of a `.app`
/// bundle that has a bundle identifier.
pub fn is_available() -> bool {
    let bundle = NSBundle::mainBundle();
    bundle.bundleIdentifier().is_some() && bundle.bundlePath().to_string().ends_with(".app")
}

/// Consume the pending tap, if any.
pub fn take_pending_click() -> bool {
    PENDING_CLICK.swap(false, Ordering::SeqCst)
}

define_class!(
    /// Shows notifications while the app is in the foreground and turns a tap into "open the
    /// window". The centre keeps only a weak reference to its delegate, so the instance is
    /// leaked on purpose in [`install`].
    #[unsafe(super(NSObject))]
    #[name = "MeivaNotificationDelegate"]
    struct Delegate;

    unsafe impl NSObjectProtocol for Delegate {}

    unsafe impl UNUserNotificationCenterDelegate for Delegate {
        #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
        fn will_present(
            &self,
            _center: &UNUserNotificationCenter,
            notification: &UNNotification,
            handler: &block2::DynBlock<dyn Fn(UNNotificationPresentationOptions)>,
        ) {
            let quiet = notification.request().content().interruptionLevel()
                == UNNotificationInterruptionLevel::Passive;
            handler.call((presentation_options(quiet),));
        }

        #[unsafe(method(userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:))]
        fn did_receive_response(
            &self,
            _center: &UNUserNotificationCenter,
            _response: &UNNotificationResponse,
            handler: &block2::DynBlock<dyn Fn()>,
        ) {
            // The bridge turns the flag into a deep link when the window regains focus.
            PENDING_CLICK.store(true, Ordering::SeqCst);
            if let Some(app) = APP.get() {
                crate::desktop::show_main_window(app);
            }
            handler.call(());
        }
    }
);

/// What the foreground app shows: a quiet update only lands in the notification centre list; an
/// alert also gets its banner and sound.
fn presentation_options(quiet: bool) -> UNNotificationPresentationOptions {
    if quiet {
        UNNotificationPresentationOptions::List
    } else {
        UNNotificationPresentationOptions::List
            | UNNotificationPresentationOptions::Banner
            | UNNotificationPresentationOptions::Sound
    }
}

/// Install the delegate. Call once at startup, before the app finishes launching, so a tap that
/// launched the app is delivered.
pub fn install(app: &AppHandle) {
    if !is_available() {
        log::info!("not running from an app bundle: notifications use the legacy path");
        return;
    }
    let _ = APP.set(app.clone());
    let delegate: Retained<Delegate> = unsafe { msg_send![Delegate::alloc(), init] };
    UNUserNotificationCenter::currentNotificationCenter()
        .setDelegate(Some(objc2::runtime::ProtocolObject::from_ref(&*delegate)));
    std::mem::forget(delegate);
}

/// Ask for permission to alert and play sounds. The system shows its one-time prompt the first
/// time and remembers the answer; later calls are silent.
pub fn request_authorization() {
    let handler = RcBlock::new(|granted: objc2::runtime::Bool, error: *mut NSError| {
        if !error.is_null() {
            log::warn!("notification authorization failed: {:?}", unsafe {
                &*error
            });
        } else if !granted.as_bool() {
            log::info!("notification authorization was not granted");
        }
    });
    UNUserNotificationCenter::currentNotificationCenter()
        .requestAuthorizationWithOptions_completionHandler(
            UNAuthorizationOptions::Alert | UNAuthorizationOptions::Sound,
            &handler,
        );
}

/// Run one delivery against the notification centre.
pub fn apply(mode: NotifyMode, tag: &str, title: String, body: String) {
    if mac_needs_delivered(mode) {
        // Only a replace needs to know what is still shown; the answer arrives asynchronously.
        let tag = tag.to_owned();
        let handler = RcBlock::new(move |shown: NonNull<NSArray<UNNotification>>| {
            let delivered: Vec<String> = unsafe { shown.as_ref() }
                .iter()
                .map(|n| n.request().identifier().to_string())
                .collect();
            run(decide_mac(mode, &tag, &delivered), &title, &body);
        });
        UNUserNotificationCenter::currentNotificationCenter()
            .getDeliveredNotificationsWithCompletionHandler(&handler);
    } else {
        run(decide_mac(mode, tag, &[]), &title, &body);
    }
}

fn run(op: MacOp, title: &str, body: &str) {
    let center = UNUserNotificationCenter::currentNotificationCenter();
    match op {
        MacOp::Skip => log::debug!("replace skipped: the notification is no longer shown"),
        MacOp::Remove { identifier } => {
            let ids = NSArray::from_retained_slice(&[NSString::from_str(&identifier)]);
            center.removeDeliveredNotificationsWithIdentifiers(&ids);
        }
        MacOp::Post { identifier, loud } => {
            let content = UNMutableNotificationContent::new();
            content.setTitle(&NSString::from_str(title));
            content.setBody(&NSString::from_str(body));
            if loud {
                content.setSound(Some(&UNNotificationSound::defaultSound()));
            } else if content.respondsToSelector(sel!(setInterruptionLevel:)) {
                // Passive (macOS 12+) updates the notification centre without banner or sound.
                content.setInterruptionLevel(UNNotificationInterruptionLevel::Passive);
            }
            let request = UNNotificationRequest::requestWithIdentifier_content_trigger(
                &NSString::from_str(&identifier),
                &content,
                None,
            );
            let done = RcBlock::new(|error: *mut NSError| {
                if !error.is_null() {
                    log::error!("could not post a notification: {:?}", unsafe { &*error });
                }
            });
            center.addNotificationRequest_withCompletionHandler(&request, Some(&done));
        }
    }
}
