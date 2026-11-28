// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! Caller authorization (Bathysphere SPEC 11.7), on every setter, Release, TakeControl, remote
//! property set and Sample. Nothing is cached.
//!
//! The caller's uid comes from the bus daemon (GetConnectionCredentials, i.e. SO_PEERCRED). It
//! must not be 0 (no legitimate caller is root: steamos-manager's user daemon relays as the
//! session user) and must equal the User of logind's seat0 ActiveSession, which must be local
//! (Remote false), of class "user" and in state "active". PIDs, executable paths and cgroups are
//! never used to grant anything; the only cgroup use is an advisory refusal of Steam's game scopes.

use std::collections::HashMap;

use zbus::zvariant::{OwnedObjectPath, OwnedValue};
use zbus::Connection;

use crate::error::HwError;

const LOGIN1: &str = "org.freedesktop.login1";
const SEAT0: &str = "/org/freedesktop/login1/seat/seat0";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Session {
    pub user: u32,
    pub remote: bool,
    pub class: String,
    pub state: String,
}

/// The pure part of the check.
pub fn check(uid: u32, s: Option<&Session>) -> Result<(), HwError> {
    if uid == 0 {
        return Err(HwError::AccessDenied("uid 0 is never a caller".into()));
    }
    let Some(s) = s else {
        return Err(HwError::AccessDenied("no active session on seat0".into()));
    };
    if s.user != uid {
        return Err(HwError::AccessDenied(
            "caller is not the active seat0 session's user".into(),
        ));
    }
    if s.remote {
        return Err(HwError::AccessDenied("the active session is remote".into()));
    }
    if s.class != "user" {
        return Err(HwError::AccessDenied(format!(
            "the active session's class is {:?}",
            s.class
        )));
    }
    if s.state != "active" {
        return Err(HwError::AccessDenied(format!(
            "the active session's state is {:?}",
            s.state
        )));
    }
    Ok(())
}

async fn get_prop(conn: &Connection, path: &str, iface: &str, name: &str) -> zbus::Result<OwnedValue> {
    let reply = conn
        .call_method(
            Some(LOGIN1),
            path,
            Some("org.freedesktop.DBus.Properties"),
            "Get",
            &(iface, name),
        )
        .await?;
    let v: OwnedValue = reply.body().deserialize()?;
    Ok(v)
}

/// logind's seat0 ActiveSession, read now.
pub async fn active_session(conn: &Connection) -> Result<Option<Session>, HwError> {
    let unavailable = |e: zbus::Error| HwError::AccessDenied(format!("logind: {e}"));
    let v = get_prop(conn, SEAT0, "org.freedesktop.login1.Seat", "ActiveSession")
        .await
        .map_err(unavailable)?;
    let (_id, path): (String, OwnedObjectPath) = v
        .try_into()
        .map_err(|_| HwError::AccessDenied("logind: bad ActiveSession".into()))?;
    if path.as_str() == "/" {
        return Ok(None);
    }
    let reply = conn
        .call_method(
            Some(LOGIN1),
            path.as_str(),
            Some("org.freedesktop.DBus.Properties"),
            "GetAll",
            &("org.freedesktop.login1.Session",),
        )
        .await
        .map_err(unavailable)?;
    let all: HashMap<String, OwnedValue> = reply
        .body()
        .deserialize()
        .map_err(|_| HwError::AccessDenied("logind: bad session".into()))?;
    let bad = || HwError::AccessDenied("logind: incomplete session".into());
    let user: (u32, OwnedObjectPath) = all
        .get("User")
        .ok_or_else(bad)?
        .try_clone()
        .map_err(|_| bad())?
        .try_into()
        .map_err(|_| bad())?;
    let remote: bool = all
        .get("Remote")
        .ok_or_else(bad)?
        .try_clone()
        .map_err(|_| bad())?
        .try_into()
        .map_err(|_| bad())?;
    let class: String = all
        .get("Class")
        .ok_or_else(bad)?
        .try_clone()
        .map_err(|_| bad())?
        .try_into()
        .map_err(|_| bad())?;
    let state: String = all
        .get("State")
        .ok_or_else(bad)?
        .try_clone()
        .map_err(|_| bad())?
        .try_into()
        .map_err(|_| bad())?;
    Ok(Some(Session {
        user: user.0,
        remote,
        class,
        state,
    }))
}

/// The caller's uid and pid from the bus daemon.
pub async fn credentials(dbus: &zbus::fdo::DBusProxy<'_>, sender: &str) -> Result<(u32, Option<u32>), HwError> {
    let name = zbus::names::BusName::try_from(sender).map_err(|_| HwError::AccessDenied("no sender".into()))?;
    let c = dbus
        .get_connection_credentials(name)
        .await
        .map_err(|e| HwError::AccessDenied(format!("credentials: {e}")))?;
    let uid = c.unix_user_id().ok_or_else(|| HwError::AccessDenied("no uid".into()))?;
    Ok((uid, c.process_id()))
}

/// Steam starts games in app-steam-app*.scope units; such a caller is refused (advisory only:
/// a same-uid process can move cgroups, so this never grants anything).
pub fn in_game_scope(pid: Option<u32>) -> bool {
    let Some(pid) = pid else { return false };
    std::fs::read_to_string(format!("/proc/{pid}/cgroup"))
        .map(|t| t.contains("/app-steam-app"))
        .unwrap_or(false)
}

pub async fn authorize(conn: &Connection, dbus: &zbus::fdo::DBusProxy<'_>, sender: &str) -> Result<u32, HwError> {
    let (uid, pid) = credentials(dbus, sender).await?;
    if uid == 0 {
        return Err(HwError::AccessDenied("uid 0 is never a caller".into()));
    }
    let s = active_session(conn).await?;
    check(uid, s.as_ref())?;
    if in_game_scope(pid) {
        return Err(HwError::AccessDenied(
            "callers in a Steam game scope are refused".into(),
        ));
    }
    Ok(uid)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(user: u32, remote: bool, class: &str, state: &str) -> Session {
        Session {
            user,
            remote,
            class: class.into(),
            state: state.into(),
        }
    }

    #[test]
    fn rules() {
        assert!(check(1000, Some(&s(1000, false, "user", "active"))).is_ok());
        for (uid, sess) in [
            (0, Some(s(0, false, "user", "active"))),
            (0, Some(s(1000, false, "user", "active"))),
            (1001, Some(s(1000, false, "user", "active"))),
            (1000, Some(s(1000, true, "user", "active"))),
            (1000, Some(s(1000, false, "greeter", "active"))),
            (1000, Some(s(1000, false, "user", "online"))),
            (1000, Some(s(1000, false, "user", "closing"))),
            (1000, None),
        ] {
            let e = check(uid, sess.as_ref()).unwrap_err();
            assert_eq!(e.name_str(), "org.freedesktop.DBus.Error.AccessDenied");
        }
    }

    #[test]
    fn game_scope_is_advisory_and_reads_proc() {
        assert!(!in_game_scope(None));
        assert!(!in_game_scope(Some(std::process::id())));
        assert!(!in_game_scope(Some(u32::MAX)));
    }
}
