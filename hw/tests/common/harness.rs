// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! The private-bus harness: a dbus-daemon with the system bus's default policy plus the package's
//! real policy file, a fake logind, the daemon under test and callers under chosen uids.
//!
//! Nothing here needs root. Run as root (CI containers), every party gets its own uid through
//! setpriv: the provider runs as an existing system account standing in for oxp3-hw (dbus-daemon
//! refuses uids without a passwd entry), the session user and a second user are two others, and
//! uid 0 is the test itself. Run as a normal user, the provider and the callers share the test's
//! uid, and a wrong uid is the fake logind reporting another user as the active one.

use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use futures_util::StreamExt;
use serde_json::{json, Value as J};
use zbus::object_server::SignalEmitter;
use zbus::zvariant::{OwnedObjectPath, OwnedValue};

use super::fake::{root_test, Owner, Tree};

pub const DEST: &str = "io.github.featherwolf.Bathysphere.Hw1";
pub const PATH: &str = "/io/github/featherwolf/Bathysphere/Hw1";
pub const SOM: &str = "/io/github/featherwolf/Bathysphere/Hw1/SteamOSManager";
pub const IF: &str = "io.github.featherwolf.Bathysphere.Hw1";
pub const TDP_NAME: &str = "io.github.featherwolf.Bathysphere.Hw1.TdpLimit1";
pub const GPU_NAME: &str = "io.github.featherwolf.Bathysphere.Hw1.GpuPerformanceLevel1";
pub const PROFILE_NAME: &str = "io.github.featherwolf.Bathysphere.Hw1.GpuPowerProfile1";
pub const CHARGE_NAME: &str = "io.github.featherwolf.Bathysphere.Hw1.BatteryChargeLimit1";

static SEQ: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug)]
pub struct Account {
    pub uid: u32,
    pub gid: u32,
}

#[derive(Clone, Debug)]
pub struct Users {
    pub multi: bool,
    pub provider_name: String,
    pub provider: Account,
    pub session: Account,
    pub other: Option<Account>,
}

fn lookup(name: &str) -> Option<Account> {
    let c = std::ffi::CString::new(name).ok()?;
    // SAFETY: getpwnam returns a pointer into static storage or null; read it at once.
    unsafe {
        let p = libc::getpwnam(c.as_ptr());
        if p.is_null() {
            return None;
        }
        Some(Account {
            uid: (*p).pw_uid,
            gid: (*p).pw_gid,
        })
    }
}

impl Users {
    /// As root: provider "daemon", session user "bin", other user "sys" (override with
    /// OXP3_HW_TEST_USERS=provider,session,other).
    pub fn detect() -> Users {
        if root_test() {
            assert!(
                Path::new("/usr/bin/setpriv").exists(),
                "a root test run needs setpriv to drop to other uids"
            );
            let names = std::env::var("OXP3_HW_TEST_USERS").unwrap_or_else(|_| "daemon,bin,sys".into());
            let n: Vec<&str> = names.split(',').collect();
            let get = |s: &str| lookup(s).unwrap_or_else(|| panic!("no passwd entry for {s}"));
            return Users {
                multi: true,
                provider_name: n[0].into(),
                provider: get(n[0]),
                session: get(n[1]),
                other: Some(get(n[2])),
            };
        }
        // SAFETY: plain getters
        let (uid, gid) = unsafe { (libc::getuid(), libc::getgid()) };
        // SAFETY: getpwuid returns static storage or null
        let name = unsafe {
            let p = libc::getpwuid(uid);
            assert!(!p.is_null(), "the test user needs a passwd entry");
            std::ffi::CStr::from_ptr((*p).pw_name).to_string_lossy().into_owned()
        };
        let me = Account { uid, gid };
        Users {
            multi: false,
            provider_name: name,
            provider: me,
            session: me,
            other: None,
        }
    }

    pub fn owner(&self) -> Owner {
        if self.multi {
            Owner::Multi { gid: self.provider.gid }
        } else {
            Owner::Single
        }
    }
}

fn as_user(cmd: &str, acct: Option<Account>) -> Command {
    match acct {
        Some(a) => {
            let mut c = Command::new("/usr/bin/setpriv");
            c.arg(format!("--reuid={}", a.uid))
                .arg(format!("--regid={}", a.gid))
                .arg("--clear-groups")
                .arg("--")
                .arg(cmd);
            c
        }
        None => Command::new(cmd),
    }
}

// ---- the bus ----------------------------------------------------------------------------------

pub struct Bus {
    child: Child,
    pub address: String,
    pub dir: PathBuf,
}

impl Drop for Bus {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

pub fn policy_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("packaging/io.github.featherwolf.Bathysphere.Hw1.conf")
}

impl Bus {
    pub fn start(users: &Users) -> Bus {
        let n = SEQ.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("oxp3-hw-bus-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // the package's policy file, with only the user name replaced by the stand-in account
        let real = std::fs::read_to_string(policy_path()).unwrap();
        assert_eq!(real.matches("user=\"oxp3-hw\"").count(), 1);
        let policy = real.replace("user=\"oxp3-hw\"", &format!("user=\"{}\"", users.provider_name));
        std::fs::write(dir.join("policy.conf"), policy).unwrap();
        let abstract_name = format!("oxp3-hw-test-{}-{n}", std::process::id());
        let conf = format!(
            r#"<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>system</type>
  <auth>EXTERNAL</auth>
  <listen>unix:abstract={abstract_name}</listen>
  <!-- the default policy of dbus's system.conf -->
  <policy context="default">
    <allow user="*"/>
    <deny own="*"/>
    <deny send_type="method_call"/>
    <allow send_type="signal"/>
    <allow send_requested_reply="true" send_type="method_return"/>
    <allow send_requested_reply="true" send_type="error"/>
    <allow receive_type="method_call"/>
    <allow receive_type="method_return"/>
    <allow receive_type="error"/>
    <allow receive_type="signal"/>
    <allow send_destination="org.freedesktop.DBus" send_interface="org.freedesktop.DBus"/>
    <allow send_destination="org.freedesktop.DBus" send_interface="org.freedesktop.DBus.Introspectable"/>
    <allow send_destination="org.freedesktop.DBus" send_interface="org.freedesktop.DBus.Properties"/>
  </policy>
  <!-- the test's fakes: logind and co-writers -->
  <policy context="default">
    <allow own="org.freedesktop.login1"/>
    <allow send_destination="org.freedesktop.login1"/>
    <allow own="org.freedesktop.UPower.PowerProfiles"/>
    <allow own="org.shadowblip.PowerStation"/>
    <allow own="org.freedesktop.thermald"/>
  </policy>
  <include>{}</include>
</busconfig>
"#,
            dir.join("policy.conf").display()
        );
        std::fs::write(dir.join("bus.conf"), conf).unwrap();
        let mut child = Command::new("dbus-daemon")
            .arg(format!("--config-file={}", dir.join("bus.conf").display()))
            .arg("--nofork")
            .arg("--print-address=1")
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("dbus-daemon");
        let mut line = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut line)
            .unwrap();
        let address = line.trim().to_string();
        assert!(address.starts_with("unix:abstract="), "dbus-daemon: {address:?}");
        Bus { child, address, dir }
    }

    pub async fn conn(&self) -> zbus::Connection {
        zbus::connection::Builder::address(self.address.as_str())
            .unwrap()
            .build()
            .await
            .unwrap()
    }
}

// ---- fake logind ------------------------------------------------------------------------------

#[derive(Debug)]
pub struct LoginState {
    /// The active seat0 session's user, or None for no active session.
    pub user: Option<u32>,
    pub remote: bool,
    pub class: String,
    pub state: String,
    /// Inhibit() calls: (what, who, why, mode).
    pub inhibits: Vec<(String, String, String, String)>,
    /// Read ends of the inhibitor pipes handed out (EOF once the provider closes its end).
    pub inhibit_reads: Vec<std::fs::File>,
}

#[derive(Clone)]
pub struct Logind {
    pub st: Arc<Mutex<LoginState>>,
    pub conn: zbus::Connection,
}

struct Seat(Arc<Mutex<LoginState>>);
struct Session(Arc<Mutex<LoginState>>);
struct Manager(Arc<Mutex<LoginState>>);

#[zbus::interface(name = "org.freedesktop.login1.Seat")]
impl Seat {
    #[zbus(property)]
    fn active_session(&self) -> (String, OwnedObjectPath) {
        match self.0.lock().unwrap().user {
            Some(_) => (
                "1".into(),
                OwnedObjectPath::try_from("/org/freedesktop/login1/session/_31").unwrap(),
            ),
            None => (String::new(), OwnedObjectPath::try_from("/").unwrap()),
        }
    }
}

#[zbus::interface(name = "org.freedesktop.login1.Session")]
impl Session {
    #[zbus(property)]
    fn user(&self) -> (u32, OwnedObjectPath) {
        let u = self.0.lock().unwrap().user.unwrap_or(65534);
        (
            u,
            OwnedObjectPath::try_from(format!("/org/freedesktop/login1/user/_{u}")).unwrap(),
        )
    }
    #[zbus(property)]
    fn remote(&self) -> bool {
        self.0.lock().unwrap().remote
    }
    #[zbus(property)]
    fn class(&self) -> String {
        self.0.lock().unwrap().class.clone()
    }
    #[zbus(property)]
    fn state(&self) -> String {
        self.0.lock().unwrap().state.clone()
    }
    #[zbus(property, name = "Type")]
    fn kind(&self) -> String {
        "wayland".into()
    }
}

#[zbus::interface(name = "org.freedesktop.login1.Manager")]
impl Manager {
    fn inhibit(
        &self,
        what: String,
        who: String,
        why: String,
        mode: String,
    ) -> zbus::fdo::Result<zbus::zvariant::OwnedFd> {
        let mut fds = [0i32; 2];
        // SAFETY: pipe2 fills two descriptors
        let r = unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC | libc::O_NONBLOCK) };
        assert_eq!(r, 0);
        // SAFETY: both descriptors were just created and are owned here
        let (rd, wr) = unsafe {
            use std::os::fd::FromRawFd;
            (
                std::fs::File::from_raw_fd(fds[0]),
                std::os::fd::OwnedFd::from_raw_fd(fds[1]),
            )
        };
        let mut st = self.0.lock().unwrap();
        st.inhibits.push((what, who, why, mode));
        st.inhibit_reads.push(rd);
        Ok(zbus::zvariant::OwnedFd::from(wr))
    }

    #[zbus(signal)]
    async fn prepare_for_sleep(emitter: &SignalEmitter<'_>, start: bool) -> zbus::Result<()>;
}

impl Logind {
    pub async fn start(bus: &Bus, user: u32) -> Logind {
        let st = Arc::new(Mutex::new(LoginState {
            user: Some(user),
            remote: false,
            class: "user".into(),
            state: "active".into(),
            inhibits: vec![],
            inhibit_reads: vec![],
        }));
        let conn = zbus::connection::Builder::address(bus.address.as_str())
            .unwrap()
            .serve_at("/org/freedesktop/login1/seat/seat0", Seat(st.clone()))
            .unwrap()
            .serve_at("/org/freedesktop/login1/session/_31", Session(st.clone()))
            .unwrap()
            .serve_at("/org/freedesktop/login1", Manager(st.clone()))
            .unwrap()
            .name("org.freedesktop.login1")
            .unwrap()
            .build()
            .await
            .unwrap();
        Logind { st, conn }
    }

    pub async fn sleep(&self, start: bool) {
        let em = SignalEmitter::new(&self.conn, "/org/freedesktop/login1").unwrap();
        Manager::prepare_for_sleep(&em, start).await.unwrap();
    }

    /// Whether every inhibitor handed out has been closed by the provider.
    pub fn inhibitors_closed(&self) -> bool {
        let mut st = self.st.lock().unwrap();
        st.inhibit_reads.iter_mut().all(|f| {
            let mut b = [0u8; 1];
            matches!(f.read(&mut b), Ok(0))
        })
    }
}

// ---- the daemon -------------------------------------------------------------------------------

pub struct Daemon {
    pub child: Child,
    pub log: PathBuf,
}

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub const SCALE: &str = "0.1";

impl Daemon {
    pub fn command(tree: &Tree, bus: &Bus, users: &Users, scale: &str) -> Command {
        let mut c = as_user(env!("CARGO_BIN_EXE_oxp3-hwd"), users.multi.then_some(users.provider));
        c.arg("--sysfs-root")
            .arg(&tree.root)
            .arg("--bus")
            .arg(&bus.address)
            .arg("--time-scale")
            .arg(scale);
        c.env_remove("NOTIFY_SOCKET");
        c
    }

    pub fn spawn(mut cmd: Command, bus: &Bus) -> Daemon {
        let n = SEQ.fetch_add(1, Ordering::SeqCst);
        let log = bus.dir.join(format!("daemon-{n}.log"));
        let f = std::fs::File::create(&log).unwrap();
        let child = cmd
            .stdout(Stdio::null())
            .stderr(Stdio::from(f))
            .spawn()
            .expect("oxp3-hwd");
        Daemon { child, log }
    }

    pub fn log(&self) -> String {
        std::fs::read_to_string(&self.log).unwrap_or_default()
    }

    /// Wait until the daemon owns the main name and has claimed its remote names (it logs
    /// "serving" after both).
    pub async fn ready(&mut self, conn: &zbus::Connection) {
        let dbus = zbus::fdo::DBusProxy::new(conn).await.unwrap();
        let deadline = Instant::now() + Duration::from_secs(15);
        loop {
            if dbus.name_has_owner(DEST.try_into().unwrap()).await.unwrap_or(false)
                && self.log().contains("oxp3-hwd: serving ")
            {
                return;
            }
            if let Ok(Some(st)) = self.child.try_wait() {
                panic!("oxp3-hwd exited with {st}:\n{}", self.log());
            }
            assert!(Instant::now() < deadline, "oxp3-hwd did not start:\n{}", self.log());
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    pub fn signal(&self, sig: i32) {
        // SAFETY: the pid is our child's
        unsafe { libc::kill(self.child.id() as i32, sig) };
    }

    pub fn wait_exit(&mut self, within: Duration) -> Option<std::process::ExitStatus> {
        let deadline = Instant::now() + within;
        loop {
            if let Ok(Some(st)) = self.child.try_wait() {
                return Some(st);
            }
            if Instant::now() > deadline {
                return None;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

// ---- callers ----------------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Who {
    /// The active session's user.
    Session,
    /// Another (non-root) user: only in a root test run.
    Other,
    /// uid 0: only in a root test run.
    Root,
}

pub struct Env {
    pub users: Users,
    pub tree: Tree,
    pub bus: Bus,
    pub logind: Logind,
    pub conn: zbus::Connection,
    pub daemon: Option<Daemon>,
    signals: Arc<Mutex<Vec<(String, String)>>>,
}

impl Env {
    /// A bus, a fake logind with the session user active, and a tree laid out by `build`.
    pub async fn new(build: impl FnOnce(&Tree)) -> Env {
        let users = Users::detect();
        let tree = Tree::new(users.owner());
        build(&tree);
        let bus = Bus::start(&users);
        let logind = Logind::start(&bus, users.session.uid).await;
        let conn = bus.conn().await;
        let signals = Arc::new(Mutex::new(Vec::new()));
        {
            let rule = zbus::MatchRule::builder()
                .msg_type(zbus::message::Type::Signal)
                .interface("io.github.featherwolf.Bathysphere.Hw1.Provider1")
                .unwrap()
                .member("Changed")
                .unwrap()
                .build();
            let mut stream = zbus::MessageStream::for_match_rule(rule, &conn, None).await.unwrap();
            let sink = signals.clone();
            tokio::spawn(async move {
                while let Some(Ok(m)) = stream.next().await {
                    if let Ok((k, r)) = m.body().deserialize::<(String, String)>() {
                        sink.lock().unwrap().push((k, r));
                    }
                }
            });
        }
        Env {
            users,
            tree,
            bus,
            logind,
            conn,
            daemon: None,
            signals,
        }
    }

    pub async fn oxp3() -> Env {
        let mut e = Env::new(|t| t.oxp3(true)).await;
        e.start().await;
        e
    }

    pub fn command(&self) -> Command {
        Daemon::command(&self.tree, &self.bus, &self.users, SCALE)
    }

    pub async fn start_with(&mut self, cmd: Command) {
        let mut d = Daemon::spawn(cmd, &self.bus);
        d.ready(&self.conn).await;
        self.daemon = Some(d);
    }

    pub async fn start(&mut self) {
        let cmd = self.command();
        self.start_with(cmd).await;
    }

    pub fn log(&self) -> String {
        self.daemon.as_ref().map(|d| d.log()).unwrap_or_default()
    }

    fn account(&self, who: Who) -> Option<Option<Account>> {
        match (self.users.multi, who) {
            (true, Who::Session) => Some(Some(self.users.session)),
            (true, Who::Other) => Some(self.users.other),
            (true, Who::Root) => Some(None),
            (false, Who::Session) => Some(None),
            (false, _) => None,
        }
    }

    /// Whether this run can act as `who` (Other and Root need a root test run).
    pub fn can(&self, who: Who) -> bool {
        self.account(who).is_some()
    }

    fn caller(&self, who: Who, script: &J) -> Command {
        let acct = self.account(who).expect("this caller needs a root test run");
        let mut c = as_user(env!("CARGO_BIN_EXE_oxp3-hw-testcall"), acct);
        c.arg(&self.bus.address).arg(script.to_string());
        c
    }

    /// Run a script of calls on one connection as `who`; one JSON result per step.
    pub fn run(&self, who: Who, script: J) -> Vec<J> {
        let out = self
            .caller(who, &script)
            .stderr(Stdio::inherit())
            .output()
            .expect("testcall");
        let text = String::from_utf8_lossy(&out.stdout);
        let v: Vec<J> = text.lines().map(|l| serde_json::from_str(l).unwrap()).collect();
        assert_eq!(v.len(), script.as_array().unwrap().len(), "testcall output: {text}");
        v
    }

    pub fn one(&self, who: Who, step: J) -> J {
        self.run(who, json!([step])).remove(0)
    }

    /// A caller that keeps its connection (and so its leases) until dropped.
    pub fn hold(&self, who: Who, steps: J) -> Held {
        let mut script = steps.as_array().unwrap().clone();
        script.push(json!({"op": "name"}));
        script.push(json!({"op": "wait_stdin"}));
        let n = script.len() - 1;
        let mut child = self
            .caller(who, &J::Array(script))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let stdin = child.stdin.take();
        let mut rd = BufReader::new(child.stdout.take().unwrap());
        let mut results = vec![];
        for _ in 0..n {
            let mut line = String::new();
            rd.read_line(&mut line).unwrap();
            results.push(serde_json::from_str::<J>(&line).unwrap_or(J::Null));
        }
        let name = results
            .last()
            .and_then(|r| r["value"].as_str())
            .unwrap_or_default()
            .to_string();
        Held {
            child,
            stdin,
            results,
            name,
        }
    }

    pub fn signals(&self) -> Vec<(String, String)> {
        self.signals.lock().unwrap().clone()
    }

    pub fn clear_signals(&self) {
        self.signals.lock().unwrap().clear();
    }

    /// Wait for Changed(knob, reason).
    pub async fn changed(&self, knob: &str, reason: &str, within: Duration) -> bool {
        let deadline = Instant::now() + within;
        loop {
            if self.signals().iter().any(|(k, r)| k == knob && r == reason) {
                return true;
            }
            if Instant::now() > deadline {
                return false;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    pub async fn has_owner(&self, name: &str) -> bool {
        let dbus = zbus::fdo::DBusProxy::new(&self.conn).await.unwrap();
        dbus.name_has_owner(name.try_into().unwrap()).await.unwrap_or(false)
    }

    /// Wait for `cond` on the tree.
    pub async fn until(&self, within: Duration, mut cond: impl FnMut(&Tree) -> bool) -> bool {
        let deadline = Instant::now() + within;
        loop {
            if cond(&self.tree) {
                return true;
            }
            if Instant::now() > deadline {
                return false;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    pub async fn get(&self, path: &str, iface: &str, prop: &str) -> OwnedValue {
        let p = zbus::Proxy::new(&self.conn, DEST, path, iface).await.unwrap();
        p.get_property::<OwnedValue>(prop).await.unwrap()
    }

    /// A fresh (uncached) property read.
    pub async fn prop(&self, path: &str, iface: &str, prop: &str) -> OwnedValue {
        let r = self
            .conn
            .call_method(
                Some(DEST),
                path,
                Some("org.freedesktop.DBus.Properties"),
                "Get",
                &(iface, prop),
            )
            .await
            .unwrap();
        r.body().deserialize::<OwnedValue>().unwrap()
    }
}

pub struct Held {
    child: Child,
    stdin: Option<ChildStdin>,
    pub results: Vec<J>,
    pub name: String,
}

impl Held {
    /// Close the connection (its unique name vanishes).
    pub fn drop_connection(mut self) {
        drop(self.stdin.take());
        let _ = self.child.wait();
    }
}

impl Drop for Held {
    fn drop(&mut self) {
        if let Some(mut s) = self.stdin.take() {
            let _ = s.write_all(b"");
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

// ---- call builders ----------------------------------------------------------------------------

pub fn call(iface: &str, method: &str, sig: &str, args: J) -> J {
    json!({"op": "call", "dest": DEST, "path": PATH, "iface": format!("{IF}.{iface}"), "method": method, "sig": sig, "args": args})
}

pub fn som_set(dest: &str, iface: &str, prop: &str, sig: &str, value: J) -> J {
    json!({"op": "set", "dest": dest, "path": SOM, "iface": format!("com.steampowered.SteamOSManager1.{iface}"), "prop": prop, "sig": sig, "value": value})
}

pub fn som_get(dest: &str, iface: &str, prop: &str) -> J {
    json!({"op": "get", "dest": dest, "path": SOM, "iface": format!("com.steampowered.SteamOSManager1.{iface}"), "prop": prop})
}

pub fn set_tdp(w: u32) -> J {
    som_set(TDP_NAME, "TdpLimit1", "TdpLimit", "v", json!({"t": "u", "v": w}))
}

pub fn err(r: &J) -> String {
    r["error"].as_str().unwrap_or("").to_string()
}

pub fn ok(r: &J) -> bool {
    r["ok"].as_bool().unwrap_or(false)
}
