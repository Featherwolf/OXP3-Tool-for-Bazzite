// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! sd_notify(3) without libsystemd: one datagram to $NOTIFY_SOCKET (a path, or "@name" for an
//! abstract socket). Only the safety task sends WATCHDOG=1, so a hung provider is killed by the
//! service manager and ExecStopPost restores the safe state.

use std::os::unix::net::UnixDatagram;

pub fn notify(state: &str) {
    let Some(addr) = std::env::var_os("NOTIFY_SOCKET") else {
        return;
    };
    let addr = addr.to_string_lossy().into_owned();
    let Ok(sock) = UnixDatagram::unbound() else { return };
    let r = if let Some(name) = addr.strip_prefix('@') {
        use std::os::linux::net::SocketAddrExt;
        match std::os::unix::net::SocketAddr::from_abstract_name(name.as_bytes()) {
            Ok(a) => sock.send_to_addr(state.as_bytes(), &a),
            Err(e) => Err(e),
        }
    } else {
        sock.send_to(state.as_bytes(), &addr)
    };
    if let Err(e) = r {
        eprintln!("oxp3-hwd: sd_notify {state}: {e}");
    }
}
