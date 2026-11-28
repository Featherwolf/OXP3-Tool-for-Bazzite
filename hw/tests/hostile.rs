// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! A hostile local user and a hardware-safety review, run against oxp3-hwd on the private bus:
//! forged logind signals, a fan request racing sleep, kernel writes that do not take, register
//! values outside the envelope, state another tool left behind, and a flood of open reads that
//! must never starve the safety task (and so the watchdog).

mod common;

use std::time::{Duration, Instant};

use common::fake::MW;
use common::harness::*;
use serde_json::json;

const S: Duration = Duration::from_secs(1);
const BUSY: &str = "io.github.featherwolf.Bathysphere.Hw1.Error.Busy";
const NOT_APPLIED: &str = "io.github.featherwolf.Bathysphere.Hw1.Error.NotApplied";

fn fan(t: &common::fake::Tree) -> String {
    t.get("sys/class/hwmon/hwmon5/pwm1_enable")
}

fn set_fan(m: &str) -> serde_json::Value {
    call("FanMode1", "SetFanMode", "s", json!([m]))
}

fn sleep_signal(dest: Option<&str>, start: bool) -> serde_json::Value {
    let mut s = json!({"op": "signal", "path": "/org/freedesktop/login1",
        "iface": "org.freedesktop.login1.Manager", "member": "PrepareForSleep", "sig": "b", "args": [start]});
    if let Some(d) = dest {
        s["dest"] = json!(d);
    }
    s
}

async fn log_has(env: &Env, text: &str, within: Duration) -> bool {
    let deadline = Instant::now() + within;
    loop {
        if env.log().contains(text) {
            return true;
        }
        if Instant::now() > deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

/// Any local process may send a signal, and a unicast one reaches the provider whatever the
/// match rules say: only logind's own connection may put the fan back to auto for sleep or
/// start the resume re-assertion.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn forged_sleep_signals_from_another_process_are_ignored() {
    let env = Env::oxp3().await;
    let a = env.hold(Who::Session, json!([set_fan("quiet")]));
    assert!(ok(&a.results[0]), "{:?}\n{}", a.results, env.log());
    assert_eq!(fan(&env.tree), "1");
    // the forger: another user where the run can switch uids, else the session user's own
    // second connection (neither is logind)
    let forger = if env.can(Who::Other) { Who::Other } else { Who::Session };
    let own = env.one(forger, json!({"op": "name"}));
    let unique = own["value"].as_str().unwrap().to_string();
    assert!(!unique.is_empty());
    // and the bus's own NameOwnerChanged, claiming the lease holder disconnected
    let gone = json!({"op": "signal", "dest": DEST, "path": "/org/freedesktop/DBus",
        "iface": "org.freedesktop.DBus", "member": "NameOwnerChanged", "sig": "sss",
        "args": [a.name, a.name, ""]});
    assert!(!a.name.is_empty());
    let r = env.run(
        forger,
        json!([
            sleep_signal(Some(DEST), true),
            sleep_signal(Some(TDP_NAME), true),
            sleep_signal(None, true),
            sleep_signal(Some(DEST), false),
            gone,
        ]),
    );
    assert!(r.iter().all(ok), "the bus delivers signals from anyone: {r:?}");
    tokio::time::sleep(S).await;
    assert_eq!(
        fan(&env.tree),
        "1",
        "a forged PrepareForSleep moved the fan:\n{}",
        env.log()
    );
    assert!(
        !env.signals().iter().any(|(k, r)| k == "fan" && r == "reverted-sleep"),
        "{:?}",
        env.signals()
    );
    assert!(!env.log().contains("preparing for sleep"), "{}", env.log());
    assert!(
        !env.log().contains(" left: "),
        "a forged NameOwnerChanged ended a lease:\n{}",
        env.log()
    );
    assert!(!env.log().contains("oxp3-hwd: resumed"), "{}", env.log());
    // logind's own signal still works
    env.logind.sleep(true).await;
    assert!(env.until(2 * S, |t| fan(t) == "2").await, "{}", env.log());
    assert!(env.changed("fan", "reverted-sleep", S).await);
    env.logind.sleep(false).await;
    drop(a);
}

/// A fan request that is still on its way (inhibitor, write slot) when logind announces sleep
/// must not leave the fan non-auto through the sleep: between PrepareForSleep(true) and (false)
/// only auto is accepted.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn no_fan_mode_but_auto_while_the_system_prepares_to_sleep() {
    let env = Env::oxp3().await;
    env.logind.sleep(true).await;
    assert!(log_has(&env, "preparing for sleep", 2 * S).await, "{}", env.log());
    for m in ["quiet", "max"] {
        let r = env.one(Who::Session, set_fan(m));
        assert_eq!(err(&r), BUSY, "{m}: {r}\n{}", env.log());
        assert_eq!(r["message"], "sleep", "{r}");
        assert_eq!(fan(&env.tree), "2");
    }
    assert!(ok(&env.one(Who::Session, set_fan("auto"))));
    assert!(env.logind.inhibitors_closed(), "no inhibitor is left behind");
    env.logind.sleep(false).await;
    assert!(log_has(&env, "oxp3-hwd: resumed", 2 * S).await);
    let a = env.hold(Who::Session, json!([set_fan("max")]));
    assert!(ok(&a.results[0]), "{:?}", a.results);
    assert_eq!(fan(&env.tree), "0");
    drop(a);
}

/// The firmware may accept a PL2 write without taking it (a locked MMIO register): PL1 must then
/// not be raised above the PL2 the zone really has.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn pl1_is_never_raised_above_a_pl2_that_did_not_take() {
    let env = Env::oxp3().await;
    assert!(ok(&env.one(Who::Session, set_tdp(10))), "{}", env.log());
    let b = env.hold(
        Who::Session,
        json!([call("PowerLimits1", "SetBoost", "suu", json!(["follow", 0, 2]))]),
    );
    assert!(ok(&b.results[0]), "{:?}\n{}", b.results, env.log());
    for z in ["intel-rapl-mmio:0", "intel-rapl:0"] {
        assert_eq!(env.tree.watts(z, "short_term"), 12.0, "{z}");
    }
    env.tree
        .rules(json!({"ignore": ["intel-rapl-mmio:0/constraint_1_power_limit_uw"]}));
    let r = env.one(Who::Session, set_tdp(30));
    assert!(!ok(&r), "PL2 did not take on MMIO: {r}");
    for z in ["intel-rapl-mmio:0", "intel-rapl:0"] {
        let (pl1, pl2) = (env.tree.watts(z, "long_term"), env.tree.watts(z, "short_term"));
        assert!(pl2 >= pl1, "{z}: PL1 {pl1} W above PL2 {pl2} W\n{}", env.log());
    }
    env.tree.rules(json!({}));
    drop(b);
}

/// Bounds never come from register values: with PL1 left above the envelope by another tool and
/// Steam's slider untouched, a boost request must not write PL2 above the device's 52 W.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_register_pl1_above_the_envelope_never_raises_pl2_above_it() {
    let mut env = Env::new(|t| {
        t.oxp3(true);
        t.set_watts("intel-rapl:0", "long_term", 56);
        t.set_watts("intel-rapl-mmio:0", "long_term", 56);
    })
    .await;
    env.start().await;
    for (mode, limit, offset) in [("manual", 40, 0), ("follow", 0, 10), ("firmware", 0, 0)] {
        let r = env.one(
            Who::Session,
            call("PowerLimits1", "SetBoost", "suu", json!([mode, limit, offset])),
        );
        assert_eq!(err(&r), NOT_APPLIED, "{mode}: {r}");
        assert!(r["message"].as_str().unwrap().starts_with("tdp="), "{r}");
    }
    let over: Vec<_> = env
        .tree
        .writes()
        .into_iter()
        .filter(|(p, v, _)| p.ends_with("constraint_1_power_limit_uw") && v.parse::<u64>().unwrap_or(0) > 52 * MW)
        .collect();
    assert!(over.is_empty(), "PL2 above the envelope: {over:?}");
    for z in ["intel-rapl-mmio:0", "intel-rapl:0"] {
        assert_eq!(env.tree.watts(z, "short_term"), 52.0, "{z}");
    }
    // Steam's slider brings PL1 back into the envelope; boost works again
    assert!(ok(&env.one(Who::Session, set_tdp(20))), "{}", env.log());
    let b = env.hold(
        Who::Session,
        json!([call("PowerLimits1", "SetBoost", "suu", json!(["manual", 40, 0]))]),
    );
    assert!(ok(&b.results[0]), "{:?}", b.results);
    drop(b);
}

/// A manual fan or a charging pause that nobody holds (a provider killed before ExecStopPost ran,
/// another tool) is never left unguarded: the provider returns both to auto when it starts.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_manual_fan_and_a_charging_pause_nobody_holds_are_reset_at_start() {
    let mut env = Env::new(|t| {
        t.oxp3(true);
        t.poke("sys/class/hwmon/hwmon5/pwm1_enable", 1);
        t.poke("sys/class/hwmon/hwmon5/pwm1", 0);
        t.poke(
            "sys/class/power_supply/BAT0/charge_behaviour",
            "auto inhibit-charge [inhibit-charge-awake]",
        );
    })
    .await;
    env.start().await;
    assert_eq!(fan(&env.tree), "2", "{}", env.log());
    assert_eq!(
        env.tree.get("sys/class/power_supply/BAT0/charge_behaviour"),
        "[auto] inhibit-charge inhibit-charge-awake"
    );
}

/// Reads are open to every local user. A flood of them, each as slow as an embedded-controller
/// transaction, must never hold the safety task (the fan guard and the only WATCHDOG=1 sender)
/// behind a queue of readers.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_flood_of_open_reads_never_starves_the_safety_task() {
    let mut env = Env::new(|t| t.oxp3(true)).await;
    let sock_path = env.bus.dir.join("notify");
    let sock = std::os::unix::net::UnixDatagram::bind(&sock_path).unwrap();
    std::fs::set_permissions(&sock_path, std::os::unix::fs::PermissionsExt::from_mode(0o777)).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(50))).unwrap();
    let mut cmd = env.command();
    cmd.env("NOTIFY_SOCKET", &sock_path);
    env.start_with(cmd).await;
    // every EC read takes 20 ms
    std::fs::write(
        env.tree.path(".kernel/slow.json"),
        json!({"hwmon5/pwm1_enable": 20, "BAT0/charge_behaviour": 20, "BAT0/charge_control_end_threshold": 20})
            .to_string(),
    )
    .unwrap();
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let mut tasks = vec![];
    for i in 0..48 {
        let c = env.bus.conn().await;
        let stop = stop.clone();
        tasks.push(tokio::spawn(async move {
            let (iface, prop) = match i % 3 {
                0 => ("FanMode1", "FanMode"),
                1 => ("BatteryChargeBehaviour1", "ChargeBehaviour"),
                _ => ("Provider1", "Knobs"),
            };
            let iface = format!("{IF}.{iface}");
            while !stop.load(std::sync::atomic::Ordering::Relaxed) {
                let _ = c
                    .call_method(
                        Some(DEST),
                        PATH,
                        Some("org.freedesktop.DBus.Properties"),
                        "Get",
                        &(iface.as_str(), prop),
                    )
                    .await;
            }
        }));
    }
    // the safety tick is 100 ms at the test's time scale: measure the gaps between pings
    let start = Instant::now();
    let mut last = Instant::now();
    let mut worst = Duration::ZERO;
    let mut buf = [0u8; 256];
    let mut n = 0;
    while start.elapsed() < Duration::from_secs(4) {
        if let Ok(k) = sock.recv(&mut buf) {
            if &buf[..k] == b"WATCHDOG=1" {
                let now = Instant::now();
                if n > 0 {
                    worst = worst.max(now - last);
                }
                last = now;
                n += 1;
            }
        }
    }
    stop.store(true, std::sync::atomic::Ordering::Relaxed);
    for t in tasks {
        let _ = t.await;
    }
    let _ = std::fs::remove_file(env.tree.path(".kernel/slow.json"));
    assert!(n >= 10, "only {n} watchdog pings in 4 s\n{}", env.log());
    // one reader in front of the tick (20 ms) plus the tick's own work; a queue of 48 readers
    // would be about a second
    assert!(
        worst < Duration::from_millis(450),
        "the safety task waited {worst:?} behind readers ({n} pings)"
    );
}

/// A paired or plugged peripheral brings its own power_supply (scope Device). It is never taken
/// for the handheld's battery (whose charge controls are the granted ones), and a controller
/// charging over USB never makes the provider believe the handheld is on AC (where the AC-only
/// CPU floor and an admin AC sustained limit apply).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_peripheral_battery_is_neither_the_battery_nor_ac() {
    let mut env = Env::new(|t| {
        t.oxp3(true);
        // no Mains supply: the AC state then comes from the batteries' status
        std::fs::remove_dir_all(t.path("sys/class/power_supply/ADP1")).unwrap();
        let p = "sys/class/power_supply/0000:0005:054C:0CE6.0001-battery";
        t.put(&format!("{p}/type"), "Battery", common::fake::Grant::No);
        t.put(&format!("{p}/scope"), "Device", common::fake::Grant::No);
        t.put(&format!("{p}/status"), "Charging", common::fake::Grant::No);
        t.put(&format!("{p}/capacity"), 40, common::fake::Grant::No);
        t.put("sys/class/power_supply/BAT0/scope", "System", common::fake::Grant::No);
    })
    .await;
    env.start().await;
    let knobs: Vec<String> = env
        .prop(PATH, &format!("{IF}.Provider1"), "Knobs")
        .await
        .try_into()
        .unwrap();
    assert!(knobs.contains(&"chargeLimit".to_string()), "{knobs:?}\n{}", env.log());
    assert!(env.log().contains("battery BAT0"), "{}", env.log());
    let r = env.one(
        Who::Session,
        som_set(
            CHARGE_NAME,
            "BatteryChargeLimit1",
            "MaxChargeLevel",
            "v",
            json!({"t": "i", "v": 80}),
        ),
    );
    assert!(ok(&r), "{r}");
    assert_eq!(
        env.tree.get("sys/class/power_supply/BAT0/charge_control_end_threshold"),
        "80"
    );
    // on battery: a CPU floor above the boot value is refused
    let r = env.one(
        Who::Session,
        call(
            "CpuLimits1",
            "SetCpuLimits",
            "a{sv}",
            json!([{"min_perf_pct": {"t": "u", "v": 30}}]),
        ),
    );
    assert_eq!(err(&r), BUSY, "{r}");
    assert_eq!(r["message"], "battery", "{r}");
}

fn vm(pid: u32, key: &str) -> u64 {
    std::fs::read_to_string(format!("/proc/{pid}/status"))
        .unwrap_or_default()
        .lines()
        .find_map(|l| {
            l.strip_prefix(key)
                .map(|v| v.trim().trim_end_matches(" kB").trim().parse().unwrap_or(0))
        })
        .unwrap_or(0)
}

/// Arguments as large as the system bus lets through (32 MiB messages): a huge string, a huge
/// dictionary, deep variants. Each is refused, the provider stays up and answers, writes
/// nothing, and its memory stays within the unit's MemoryMax.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn huge_arguments_are_refused_and_bounded() {
    let env = Env::oxp3().await;
    let pid = env.daemon.as_ref().unwrap().child.id();
    let base = vm(pid, "VmRSS:");
    env.tree.clear_writes();
    let c = env.bus.conn().await;
    let p = |iface: &str| format!("{IF}.{iface}");
    let big = "q".repeat(24 << 20);
    let r = c
        .call_method(
            Some(DEST),
            PATH,
            Some(p("FanMode1").as_str()),
            "SetFanMode",
            &(big.as_str(),),
        )
        .await;
    assert!(r.is_err(), "a 24 MiB fan mode was accepted");
    drop(big);
    let mut dict = std::collections::HashMap::new();
    for i in 0..20_000 {
        dict.insert(format!("{i:0>1000}"), "performance".to_string());
    }
    let r = c
        .call_method(
            Some(DEST),
            PATH,
            Some(p("CpuEnergy1").as_str()),
            "SetEnergyPreference",
            &(dict,),
        )
        .await;
    assert!(r.is_err(), "a 20 MB preference map was accepted");
    // a variant nested as deep as the format allows, as a remote property value
    let mut v = zbus::zvariant::Value::from(15u32);
    for _ in 0..60 {
        v = zbus::zvariant::Value::Value(Box::new(v));
    }
    let r = c
        .call_method(
            Some(TDP_NAME),
            SOM,
            Some("org.freedesktop.DBus.Properties"),
            "Set",
            &("com.steampowered.SteamOSManager1.TdpLimit1", "TdpLimit", v),
        )
        .await;
    assert!(r.is_err(), "a nested variant was taken as a TdpLimit");
    // still up, still answering, nothing written
    let d = env.one(Who::Session, call("Provider1", "Describe", "s", json!(["fan"])));
    assert!(ok(&d), "{d}\n{}", env.log());
    assert!(env.tree.writes().is_empty(), "{:?}", env.tree.writes());
    let peak = vm(pid, "VmHWM:");
    eprintln!("oxp3-hwd memory: {base} kB at start, peak {peak} kB");
    assert!(
        peak < 128 * 1024,
        "peak RSS {peak} kB is above the unit's MemoryMax=128M"
    );
}

/// The unit's ExecCondition: the exact four-field DMI match, before the privileged grants step
/// (ConditionFirmware only checks the board vendor, which other ONE-NETBOOK models share).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn dmi_match_gates_the_unit_exactly() {
    let oxp3 = Env::new(|t| t.oxp3(true)).await;
    let st = Daemon::command(&oxp3.tree, &oxp3.bus, &oxp3.users, SCALE)
        .arg("--dmi-match")
        .status()
        .unwrap();
    assert_eq!(st.code(), Some(0), "the ONEXPLAYER 3 matches");
    for (vendor, product) in [
        ("ONE-NETBOOK", "ONEXPLAYER F1"),
        ("ONE-NETBOOK", "ONEXPLAYER 3 Pro"),
        ("Valve", "Jupiter"),
    ] {
        let other = Env::new(|t| {
            t.oxp3(true);
            t.dmi(vendor, product);
        })
        .await;
        let st = Daemon::command(&other.tree, &other.bus, &other.users, SCALE)
            .arg("--dmi-match")
            .status()
            .unwrap();
        assert_eq!(st.code(), Some(1), "{vendor} {product} must not match");
        assert!(other.tree.writes().is_empty());
    }
}

/// The test build's uid-0 allowance is for a user namespace only: as the host's real root it
/// still refuses to start.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_test_build_still_refuses_the_real_root() {
    if !common::fake::root_test() {
        return;
    }
    let env = Env::new(|t| t.oxp3(true)).await;
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_oxp3-hwd"))
        .arg("--sysfs-root")
        .arg(&env.tree.root)
        .arg("--bus")
        .arg(&env.bus.address)
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).contains("refusing to run as root"));
    assert!(env.tree.writes().is_empty());
}
