//! What the notification bridge asks the native side to do with one notification.
//!
//! The server names each on-screen notification by a stable `tag` (`unread:<agent>`,
//! `question:<id>`). Android keys a shown notification by an `i32`, so the tag is folded to one
//! deterministically: the same tag always replaces or retracts the same notification, across
//! webview reloads and app restarts.

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
}
