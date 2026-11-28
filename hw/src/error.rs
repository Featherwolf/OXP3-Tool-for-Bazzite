// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! The Hw1 errors (schemas/hw1.xml) and their D-Bus names.

use zbus::message::{Header, Message};
use zbus::names::ErrorName;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HwError {
    /// org.freedesktop.DBus.Error.InvalidArgs: out of range, unknown key, knob or mode; nothing written.
    InvalidArgs(String),
    /// org.freedesktop.DBus.Error.AccessDenied: the seat0 check failed.
    AccessDenied(String),
    /// org.freedesktop.DBus.Error.LimitsExceeded: more than 20 calls per second from one sender.
    LimitsExceeded(String),
    /// …Error.Busy: a co-writer, "lease:" + unique name, "governor", "battery" or "cooldown:" + s.
    Busy(String),
    /// …Error.NotApplied: read-back mismatch, "KNOB=OBSERVED".
    NotApplied(String),
    /// …Error.Locked: a RAPL constraint refused the write with EACCES (skipped for this boot).
    Locked(String),
    /// …Error.NotSupported: the knob's Describe reason.
    NotSupported(String),
    /// …Error.Disabled: the kill switch exists.
    Disabled(String),
}

impl HwError {
    pub fn name_str(&self) -> &'static str {
        match self {
            HwError::InvalidArgs(_) => "org.freedesktop.DBus.Error.InvalidArgs",
            HwError::AccessDenied(_) => "org.freedesktop.DBus.Error.AccessDenied",
            HwError::LimitsExceeded(_) => "org.freedesktop.DBus.Error.LimitsExceeded",
            HwError::Busy(_) => "io.github.featherwolf.Bathysphere.Hw1.Error.Busy",
            HwError::NotApplied(_) => "io.github.featherwolf.Bathysphere.Hw1.Error.NotApplied",
            HwError::Locked(_) => "io.github.featherwolf.Bathysphere.Hw1.Error.Locked",
            HwError::NotSupported(_) => "io.github.featherwolf.Bathysphere.Hw1.Error.NotSupported",
            HwError::Disabled(_) => "io.github.featherwolf.Bathysphere.Hw1.Error.Disabled",
        }
    }

    pub fn message(&self) -> &str {
        match self {
            HwError::InvalidArgs(m)
            | HwError::AccessDenied(m)
            | HwError::LimitsExceeded(m)
            | HwError::Busy(m)
            | HwError::NotApplied(m)
            | HwError::Locked(m)
            | HwError::NotSupported(m)
            | HwError::Disabled(m) => m,
        }
    }

    /// Property sets go through zbus's own org.freedesktop.DBus.Properties, which can only reply
    /// with org.freedesktop.DBus.Error names: the Hw1-specific ones become Failed with the Hw1 name
    /// in front of the message.
    pub fn to_fdo(&self) -> zbus::fdo::Error {
        match self {
            HwError::InvalidArgs(m) => zbus::fdo::Error::InvalidArgs(m.clone()),
            HwError::AccessDenied(m) => zbus::fdo::Error::AccessDenied(m.clone()),
            HwError::LimitsExceeded(m) => zbus::fdo::Error::LimitsExceeded(m.clone()),
            HwError::NotSupported(m) => zbus::fdo::Error::NotSupported(m.clone()),
            other => zbus::fdo::Error::Failed(format!("{}: {}", other.name_str(), other.message())),
        }
    }
}

impl std::fmt::Display for HwError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.name_str(), self.message())
    }
}

impl std::error::Error for HwError {}

impl zbus::DBusError for HwError {
    fn create_reply(&self, call: &Header<'_>) -> zbus::Result<Message> {
        Message::error(call, self.name())?.build(&(self.message(),))
    }

    fn name(&self) -> ErrorName<'_> {
        ErrorName::from_static_str_unchecked(self.name_str())
    }

    fn description(&self) -> Option<&str> {
        Some(self.message())
    }
}

impl From<zbus::Error> for HwError {
    fn from(e: zbus::Error) -> Self {
        // A failure of the bus itself while serving a call (never a hardware state): the caller
        // retries; nothing was written.
        HwError::Busy(format!("bus: {e}"))
    }
}
