// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! oxp3-hw: the ONEXPLAYER 3 hardware helper's library (the daemon is src/main.rs).

pub mod auth;
pub mod dbus;
pub mod device;
pub mod discover;
#[cfg(feature = "test-sysfs")]
pub mod emu;
pub mod error;
pub mod hw;
pub mod limits;
pub mod notify;
pub mod rate;
pub mod sysfs;
