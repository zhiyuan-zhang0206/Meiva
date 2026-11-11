//! What the notification bridge asks the native side to do with one notification.
//!
//! The server names each on-screen notification by a stable `tag` (`unread:<agent>`,
//! `question:<id>`). Android keys a shown notification by an `i32`, so the tag is folded to one
//! deterministically: the same tag always replaces or retracts the same notification, across
//! webview reloads and app restarts. macOS keys by a string request identifier instead, derived
//! from the same tag.

use serde::Deserialize;

/// The three things a client does with a delivery.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum NotifyMode {
    /// Raise it with the platform's normal sound and banner.
    Alert,
    /// Swap the content of the notification already shown, without sound or banner.
    Replace,
    /// Take the notification away.
    Retract,
}

/// A stable, non-negative `i32` for a tag (FNV-1a, 32 bit). Android is the one platform that
/// keys notifications by id.
#[cfg(any(target_os = "android", test))]
pub fn notification_id(tag: &str) -> i32 {
    let mut hash: u32 = 0x811c_9dc5;
    for byte in tag.bytes() {
        hash ^= u32::from(byte);
        hash = hash.wrapping_mul(0x0100_0193);
    }
    (hash & 0x7fff_ffff) as i32
}

/// The request identifier macOS keys a notification by. Adding a request under an identifier
/// that is already delivered replaces that notification, so the identifier must be a pure
/// function of the tag.
#[cfg(any(target_os = "macos", test))]
pub fn mac_identifier(tag: &str) -> String {
    format!("meiva.notify.{tag}")
}

/// What the macOS layer does for one `app_notify` call.
#[cfg(any(target_os = "macos", test))]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MacOp {
    /// Post under `identifier`. `loud` raises it with sound and banner; otherwise it lands quietly
    /// in the notification centre (passive interruption level, no sound).
    Post { identifier: String, loud: bool },
    /// Remove the delivered notification under `identifier`.
    Remove { identifier: String },
    /// Do nothing.
    Skip,
}

/// Decide the macOS operation for a delivery.
///
/// `delivered` is the identifiers the notification centre still shows; only a replace reads it
/// (see [`mac_needs_delivered`]). A replace that finds its notification gone means the user
/// dismissed it, and a silent update must not pull it back, so it is skipped. An alert always
/// posts: it is the deliberate "tell the user again", and it overwrites a shown notification of
/// the same tag.
#[cfg(any(target_os = "macos", test))]
pub fn decide_mac(mode: NotifyMode, tag: &str, delivered: &[String]) -> MacOp {
    let identifier = mac_identifier(tag);
    match mode {
        NotifyMode::Alert => MacOp::Post {
            identifier,
            loud: true,
        },
        NotifyMode::Retract => MacOp::Remove { identifier },
        NotifyMode::Replace if delivered.contains(&identifier) => MacOp::Post {
            identifier,
            loud: false,
        },
        NotifyMode::Replace => MacOp::Skip,
    }
}

/// Whether [`decide_mac`] needs the delivered list for this mode.
#[cfg(any(target_os = "macos", test))]
pub fn mac_needs_delivered(mode: NotifyMode) -> bool {
    mode == NotifyMode::Replace
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_tag_always_maps_to_the_same_non_negative_id() {
        assert_eq!(notification_id("unread:7"), notification_id("unread:7"));
        assert!(notification_id("unread:7") >= 0);
        assert!(notification_id("question:123456789") >= 0);
    }

    #[test]
    fn different_tags_get_different_ids() {
        assert_ne!(notification_id("unread:7"), notification_id("unread:8"));
        assert_ne!(notification_id("unread:7"), notification_id("question:7"));
    }

    #[test]
    fn the_mode_is_read_from_its_lowercase_name() {
        let mode: NotifyMode = serde_json::from_str("\"retract\"").unwrap();
        assert_eq!(mode, NotifyMode::Retract);
        assert!(serde_json::from_str::<NotifyMode>("\"Alert!\"").is_err());
    }

    fn shown(tags: &[&str]) -> Vec<String> {
        tags.iter().map(|tag| mac_identifier(tag)).collect()
    }

    #[test]
    fn the_mac_identifier_follows_the_tag() {
        assert_eq!(mac_identifier("unread:7"), mac_identifier("unread:7"));
        assert_ne!(mac_identifier("unread:7"), mac_identifier("unread:8"));
        assert_ne!(mac_identifier("unread:7"), mac_identifier("question:7"));
    }

    #[test]
    fn an_alert_always_posts_loudly() {
        let op = decide_mac(NotifyMode::Alert, "unread:7", &[]);
        assert_eq!(
            op,
            MacOp::Post {
                identifier: mac_identifier("unread:7"),
                loud: true
            }
        );
        // Even over a shown one: this is the "tell the user again" case.
        assert_eq!(
            op,
            decide_mac(NotifyMode::Alert, "unread:7", &shown(&["unread:7"]))
        );
    }

    #[test]
    fn a_replace_updates_a_shown_notification_quietly() {
        assert_eq!(
            decide_mac(
                NotifyMode::Replace,
                "unread:7",
                &shown(&["unread:7", "unread:8"])
            ),
            MacOp::Post {
                identifier: mac_identifier("unread:7"),
                loud: false
            }
        );
    }

    #[test]
    fn a_replace_does_not_bring_back_a_dismissed_notification() {
        assert_eq!(
            decide_mac(NotifyMode::Replace, "unread:7", &[]),
            MacOp::Skip
        );
        assert_eq!(
            decide_mac(NotifyMode::Replace, "unread:7", &shown(&["unread:8"])),
            MacOp::Skip
        );
    }

    #[test]
    fn a_retract_removes_by_identifier_whatever_is_shown() {
        assert_eq!(
            decide_mac(NotifyMode::Retract, "question:5", &[]),
            MacOp::Remove {
                identifier: mac_identifier("question:5")
            }
        );
    }

    #[test]
    fn only_a_replace_needs_the_delivered_list() {
        assert!(mac_needs_delivered(NotifyMode::Replace));
        assert!(!mac_needs_delivered(NotifyMode::Alert));
        assert!(!mac_needs_delivered(NotifyMode::Retract));
    }
}
