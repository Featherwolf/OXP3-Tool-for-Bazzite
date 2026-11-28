// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! oxp3-hwd: the ONEXPLAYER 3 hardware helper.
//!
//! A Bathysphere Hw1 provider (schemas/hw1.xml) and a steamos-manager remote for TdpLimit1,
//! GpuPerformanceLevel1, GpuPowerProfile1 and BatteryChargeLimit1. It runs as the dedicated
//! system user oxp3-hw, never as root, and writes only the sysfs attributes its package grants to
//! that group (packaging/udev, packaging/grants.conf). No network, no exec, no home access.
//!
//! Usage:
//!   oxp3-hwd                 serve on the system bus (the systemd unit's ExecStart)
//!   oxp3-hwd --restore-safe  fan and charge behaviour back to auto, then exit (ExecStopPost)
//!   oxp3-hwd --check         print what would be available and why, write nothing
//!   oxp3-hwd --dmi-match     exit 0 on an exact ONEXPLAYER 3 DMI match, else 1 (the unit's
//!                            ExecCondition, which gates the privileged grants step)
//!   oxp3-hwd --version
//! Test builds only (feature test-sysfs): --sysfs-root DIR, --bus ADDRESS, --time-scale F,
//! --no-emulation (the fake root without src/emu.rs, for a suite that models the kernel itself).
//! A test build with --sysfs-root also starts as uid 0 inside an unprivileged user namespace
//! (where uid 0 is nobody outside), so a conformance suite can check the uid-0 caller rule.

use std::process::ExitCode;
use std::time::Instant;

use oxp3_hw::hw::{self, Hw, Paths, Timing};
use oxp3_hw::sysfs::Sys;
use oxp3_hw::{dbus, device, discover, limits, notify};

const EX_USAGE: u8 = 2;
const EX_CONFIG: u8 = 78;

#[derive(Default)]
struct Args {
    restore_safe: bool,
    check: bool,
    dmi_match: bool,
    version: bool,
    #[cfg_attr(not(feature = "test-sysfs"), allow(dead_code))]
    no_emulation: bool,
    #[cfg_attr(not(feature = "test-sysfs"), allow(dead_code))]
    sysfs_root: Option<std::path::PathBuf>,
    #[cfg_attr(not(feature = "test-sysfs"), allow(dead_code))]
    bus: Option<String>,
    time_scale: f64,
}

// the test-only options take their value from the iterator, so it is not a plain for loop
#[allow(clippy::while_let_on_iterator)]
fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        time_scale: 1.0,
        ..Default::default()
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--restore-safe" => a.restore_safe = true,
            "--check" => a.check = true,
            "--dmi-match" => a.dmi_match = true,
            "--version" => a.version = true,
            "--help" | "-h" => return Err(String::new()),
            #[cfg(feature = "test-sysfs")]
            "--sysfs-root" => a.sysfs_root = Some(it.next().ok_or("--sysfs-root needs a directory")?.into()),
            #[cfg(feature = "test-sysfs")]
            "--bus" => a.bus = Some(it.next().ok_or("--bus needs an address")?),
            #[cfg(feature = "test-sysfs")]
            "--no-emulation" => a.no_emulation = true,
            #[cfg(feature = "test-sysfs")]
            "--time-scale" => {
                let v: f64 = it
                    .next()
                    .ok_or("--time-scale needs a number")?
                    .parse()
                    .map_err(|_| "bad --time-scale")?;
                if !(0.001..=1.0).contains(&v) {
                    return Err("--time-scale must be in 0.001..1".into());
                }
                a.time_scale = v;
            }
            other => return Err(format!("unknown option {other:?}")),
        }
    }
    Ok(a)
}

/// Test builds only: uid 0 inside a user namespace whose uid 0 is not the host's root, with a
/// fake root. A release build never starts as uid 0.
#[cfg(feature = "test-sysfs")]
fn namespaced_test_root(args: &Args) -> bool {
    let Some(_) = &args.sysfs_root else { return false };
    let map = std::fs::read_to_string("/proc/self/uid_map").unwrap_or_default();
    let initial = map.split_whitespace().collect::<Vec<_>>() == ["0", "0", "4294967295"];
    if !initial && !map.trim().is_empty() {
        eprintln!(
            "oxp3-hwd: test build: uid 0 of a user namespace ({}), not the host's root",
            map.trim()
        );
        return true;
    }
    false
}

#[cfg(not(feature = "test-sysfs"))]
fn namespaced_test_root(_args: &Args) -> bool {
    false
}

fn usage() {
    eprintln!("usage: oxp3-hwd [--restore-safe | --check | --dmi-match | --version]");
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            if !e.is_empty() {
                eprintln!("oxp3-hwd: {e}");
            }
            usage();
            return ExitCode::from(EX_USAGE);
        }
    };
    if args.version {
        println!("oxp3-hwd {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }
    // SAFETY: plain libc getters without arguments.
    let (uid, euid) = unsafe { (libc::getuid(), libc::geteuid()) };
    if (uid == 0 || euid == 0) && !namespaced_test_root(&args) {
        eprintln!("oxp3-hwd: refusing to run as root; the unit runs it as the oxp3-hw system user");
        return ExitCode::FAILURE;
    }

    #[cfg(feature = "test-sysfs")]
    let sys = match &args.sysfs_root {
        Some(r) if args.no_emulation => Sys::rooted(r.clone()),
        Some(r) => Sys::emulated(r.clone()),
        None => Sys::real(),
    };
    #[cfg(not(feature = "test-sysfs"))]
    let sys = Sys::real();

    let Some(dev) = device::match_dmi(|f| sys.read(&sys.p(&format!("{}/{f}", discover::DMI)))) else {
        eprintln!("oxp3-hwd: this is not an ONEXPLAYER 3 (DMI): exporting nothing");
        return if args.dmi_match {
            ExitCode::FAILURE
        } else {
            ExitCode::SUCCESS
        };
    };
    if args.dmi_match {
        return ExitCode::SUCCESS;
    }
    let paths = Paths {
        etc: sys.p("/etc/oxp3-hw"),
        state: sys.p("/var/lib/oxp3-hw"),
        run: sys.p("/run/oxp3-hw"),
    };

    if args.restore_safe {
        return restore_safe(&sys);
    }
    if paths.etc.join("disabled").exists() {
        eprintln!(
            "oxp3-hwd: {} exists: not starting",
            paths.etc.join("disabled").display()
        );
        return ExitCode::SUCCESS;
    }
    let limits = match std::fs::read_to_string(paths.etc.join("limits.toml")) {
        Ok(text) => match limits::parse(&text) {
            Ok(l) => l,
            Err(e) => {
                eprintln!("oxp3-hwd: /etc/oxp3-hw/limits.toml: {e}");
                return ExitCode::from(EX_CONFIG);
            }
        },
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => limits::Limits::default(),
        Err(e) => {
            eprintln!("oxp3-hwd: /etc/oxp3-hw/limits.toml: {e}");
            return ExitCode::from(EX_CONFIG);
        }
    };
    let env = match limits::envelope(dev, &limits) {
        Ok(e) => e,
        Err(e) => {
            eprintln!("oxp3-hwd: /etc/oxp3-hw/limits.toml: {e}");
            return ExitCode::from(EX_CONFIG);
        }
    };
    let d = discover::discover(&sys);
    eprintln!(
        "oxp3-hwd: found {} RAPL package zone(s), GPU {}, {} CPU policies, SoC slider {}, oxp_ec fan {}, battery {}",
        d.zones.len(),
        d.gpu
            .as_ref()
            .map(|g| format!("{} {}..{} MHz", g.card, g.rpn, g.rp0))
            .unwrap_or_else(|| "none".into()),
        d.cpu.as_ref().map(|c| c.policies.len()).unwrap_or(0),
        if d.soc.is_some() { "yes" } else { "no" },
        if d.fan.is_some() { "yes" } else { "no" },
        d.battery.as_ref().map(|b| b.name.as_str()).unwrap_or("none"),
    );
    let timing = Timing { scale: args.time_scale };
    let hw = Hw::new(sys, dev, env, d, timing, paths);

    if args.check {
        let r: serde_json::Map<String, serde_json::Value> = hw
            .reasons()
            .into_iter()
            .map(|(k, r)| {
                (
                    k.to_string(),
                    serde_json::Value::String(if r == hw::Reason::Ok { "ok".into() } else { r.word() }),
                )
            })
            .collect();
        println!("{}", serde_json::Value::Object(r));
        return ExitCode::SUCCESS;
    }

    let rt = match tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("oxp3-hwd: {e}");
            return ExitCode::FAILURE;
        }
    };
    rt.block_on(serve(hw, dev.id, args.bus, timing))
}

async fn serve(hw: Hw, device_id: &'static str, bus: Option<String>, timing: Timing) -> ExitCode {
    let conn = match bus {
        Some(addr) => match zbus::connection::Builder::address(addr.as_str()) {
            Ok(b) => b.build().await,
            Err(e) => Err(e),
        },
        None => zbus::Connection::system().await,
    };
    let conn = match conn {
        Ok(c) => c,
        Err(e) => {
            eprintln!("oxp3-hwd: system bus: {e}");
            return ExitCode::FAILURE;
        }
    };
    let shared = dbus::Shared::new(hw);
    let _ = shared.conn.set(conn.clone());
    if let Err(e) = dbus::serve(&conn, &shared, device_id).await {
        eprintln!("oxp3-hwd: cannot export the Hw1 objects: {e}");
        return ExitCode::FAILURE;
    }
    // watch disconnects and sleep before any caller can reach the provider, so no lease holder's
    // disconnect is missed
    let names = tokio::spawn(dbus::watch_names(shared.clone()));
    let sleep = tokio::spawn(dbus::watch_sleep(shared.clone()));
    // the main name first: nothing is written and no remote name is claimed by a second provider
    let taken = |why: String| {
        eprintln!(
            "oxp3-hwd: {} is taken ({why}): one provider per machine",
            dbus::BUS_NAME
        );
        ExitCode::FAILURE
    };
    match conn
        .request_name_with_flags(dbus::BUS_NAME, zbus::fdo::RequestNameFlags::DoNotQueue.into())
        .await
    {
        Ok(zbus::fdo::RequestNameReply::PrimaryOwner) | Ok(zbus::fdo::RequestNameReply::AlreadyOwner) => {}
        Ok(other) => return taken(format!("{other:?}")),
        Err(zbus::Error::NameTaken) => return taken("NameTaken".into()),
        Err(e) => {
            eprintln!("oxp3-hwd: cannot own {}: {e}", dbus::BUS_NAME);
            return ExitCode::FAILURE;
        }
    }
    // only the one provider that owns the name writes: first a fan or charging pause nobody
    // holds goes back to auto, then the persisted charge threshold is applied
    shared.run(|hw| hw.reset_unheld()).await;
    shared.run(|hw| hw.apply_persisted()).await;
    // co-writers before any remote name, so none is claimed for a knob another daemon owns
    dbus::update_cowriters(&shared).await;
    shared.serving.store(true, std::sync::atomic::Ordering::SeqCst);
    shared.sync_names().await;
    {
        let knobs = shared.hw.lock().await.knobs();
        eprintln!(
            "oxp3-hwd: serving {} on device {device_id}; knobs: {}",
            dbus::BUS_NAME,
            knobs.join(", ")
        );
    }
    notify::notify("READY=1");

    // hhd has no bus name, only a unit: its state is polled apart from the safety task, so a slow
    // reply from the service manager never delays the watchdog
    let cow = {
        let s = shared.clone();
        tokio::spawn(async move {
            let mut iv = tokio::time::interval(timing.rediscover());
            iv.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                iv.tick().await;
                dbus::update_cowriters(&s).await;
            }
        })
    };
    let safety = {
        let s = shared.clone();
        async move {
            let mut iv = tokio::time::interval(timing.safety_tick());
            iv.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                iv.tick().await;
                if !s.run(|hw| hw.tick(Instant::now())).await {
                    return;
                }
                // only this task feeds the watchdog: a hang anywhere under the lock stops it
                notify::notify("WATCHDOG=1");
            }
        }
    };
    let mut term = match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("oxp3-hwd: {e}");
            return ExitCode::FAILURE;
        }
    };
    let mut int = match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("oxp3-hwd: {e}");
            return ExitCode::FAILURE;
        }
    };
    let why = tokio::select! {
        _ = term.recv() => "SIGTERM",
        _ = int.recv() => "SIGINT",
        _ = safety => "the kill switch /etc/oxp3-hw/disabled",
    };
    eprintln!("oxp3-hwd: stopping ({why}): restoring the safe state");
    notify::notify("STOPPING=1");
    shared.run(|hw| hw.stop()).await;
    shared.release_all_names().await;
    names.abort();
    sleep.abort();
    cow.abort();
    ExitCode::SUCCESS
}

/// ExecStopPost: the fan to auto and charge behaviour to auto, with the group grants only.
fn restore_safe(sys: &Sys) -> ExitCode {
    let d = discover::discover(sys);
    let mut ok = true;
    if let Some(f) = &d.fan {
        match sys.write(&f.enable, "2") {
            Ok(()) if sys.read_u64(&f.enable) == Some(2) => eprintln!("oxp3-hwd: fan back to auto"),
            Ok(()) => {
                eprintln!("oxp3-hwd: fan: pwm1_enable does not read 2");
                ok = false;
            }
            Err(e) => {
                eprintln!("oxp3-hwd: fan: {e}");
                ok = false;
            }
        }
    }
    if let Some(p) = d.battery.as_ref().and_then(|b| b.behaviour.as_ref()) {
        let cur = sys.read(p).and_then(|t| discover::bracketed(&t));
        if cur.as_deref() != Some("auto") {
            match sys.write(p, "auto") {
                Ok(()) if sys.read(p).and_then(|t| discover::bracketed(&t)).as_deref() == Some("auto") => {
                    eprintln!("oxp3-hwd: charge behaviour back to auto")
                }
                Ok(()) => {
                    eprintln!("oxp3-hwd: charge behaviour does not read auto");
                    ok = false;
                }
                Err(e) => {
                    eprintln!("oxp3-hwd: charge behaviour: {e}");
                    ok = false;
                }
            }
        }
    }
    if ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
