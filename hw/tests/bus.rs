// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! oxp3-hwd itself on a private dbus-daemon that loads the package's real policy file, against a
//! fake logind and a --sysfs-root tree with kernel semantics. Callers are separate processes
//! (oxp3-hw-testcall), under other uids through setpriv when the test runs as root.

mod common;

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use common::fake::{Grant, MW};
use common::harness::*;
use serde_json::json;

const S: Duration = Duration::from_secs(1);

const ACCESS: &str = "org.freedesktop.DBus.Error.AccessDenied";
const INVALID: &str = "org.freedesktop.DBus.Error.InvalidArgs";
const BUSY: &str = "io.github.featherwolf.Bathysphere.Hw1.Error.Busy";

// ---- introspection ----------------------------------------------------------------------------

type Members = BTreeMap<String, BTreeSet<String>>;

/// interface -> canonical member lines; nested node names under "<nodes>".
fn canon(xml: &str, prefix: &str) -> BTreeMap<String, Members> {
    use quick_xml::events::Event;
    let mut r = quick_xml::Reader::from_str(xml);
    let mut out: BTreeMap<String, Members> = BTreeMap::new();
    let mut depth_node = 0;
    let mut iface: Option<String> = None;
    let mut member: Option<(String, String)> = None; // (kind, name)
    let mut lines: BTreeSet<String> = BTreeSet::new();
    let mut arg_i = 0;
    let attr = |e: &quick_xml::events::BytesStart, k: &str| -> String {
        e.attributes()
            .flatten()
            .find(|a| a.key.as_ref() == k.as_bytes())
            .map(|a| String::from_utf8_lossy(&a.value).into_owned())
            .unwrap_or_default()
    };
    loop {
        let ev = r.read_event().unwrap();
        let (e, empty) = match &ev {
            Event::Start(e) => (Some(e.clone()), false),
            Event::Empty(e) => (Some(e.clone()), true),
            Event::End(e) => {
                match e.name().as_ref() {
                    b"node" => depth_node -= 1,
                    b"interface" => iface = None,
                    b"method" | b"signal" | b"property" => {
                        if let (Some(i), Some((k, n))) = (&iface, member.take()) {
                            out.entry(i.clone())
                                .or_default()
                                .insert(format!("{k} {n}"), std::mem::take(&mut lines));
                        }
                    }
                    _ => {}
                }
                continue;
            }
            Event::Eof => break,
            _ => continue,
        };
        let e = e.unwrap();
        match e.name().as_ref() {
            b"node" => {
                if depth_node == 1 && !attr(&e, "name").is_empty() {
                    out.entry("<nodes>".into())
                        .or_default()
                        .insert(attr(&e, "name"), BTreeSet::new());
                }
                if !empty {
                    depth_node += 1;
                }
            }
            b"interface" => {
                let n = attr(&e, "name");
                if n.starts_with(prefix) || n.starts_with("com.steampowered") {
                    iface = Some(n);
                } else {
                    iface = None;
                }
            }
            b"method" | b"signal" | b"property" if iface.is_some() => {
                let kind = String::from_utf8_lossy(e.name().as_ref()).into_owned();
                let mut name = attr(&e, "name");
                if kind == "property" {
                    name = format!("{name} {} {}", attr(&e, "type"), attr(&e, "access"));
                }
                arg_i = 0;
                if empty {
                    out.entry(iface.clone().unwrap())
                        .or_default()
                        .insert(format!("{kind} {name}"), BTreeSet::new());
                } else {
                    member = Some((kind, name));
                }
            }
            b"arg" if member.is_some() => {
                let dir = attr(&e, "direction");
                let dir = if dir.is_empty() && member.as_ref().unwrap().0 == "method" {
                    "in".to_string()
                } else {
                    dir
                };
                lines.insert(format!("arg{arg_i} {} {} {dir}", attr(&e, "name"), attr(&e, "type")));
                arg_i += 1;
            }
            b"annotation" if member.is_some() => {
                lines.insert(format!("annotation {}={}", attr(&e, "name"), attr(&e, "value")));
            }
            _ => {}
        }
    }
    out
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn introspection_matches_hw1_xml() {
    let env = Env::oxp3().await;
    let schema = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/schemas/hw1.xml")).unwrap();
    if let Ok(p) = std::env::var("BATHYSPHERE_HW1_XML") {
        assert_eq!(
            std::fs::read_to_string(p).unwrap(),
            schema,
            "schemas/hw1.xml differs from Bathysphere's copy"
        );
    }
    let want = canon(&schema, IF);
    let intro = |path: &'static str| {
        let conn = env.conn.clone();
        async move {
            let r = conn
                .call_method(
                    Some(DEST),
                    path,
                    Some("org.freedesktop.DBus.Introspectable"),
                    "Introspect",
                    &(),
                )
                .await
                .unwrap();
            r.body().deserialize::<String>().unwrap()
        }
    };
    let main = intro(PATH).await;
    let child = intro(SOM).await;
    let mut got = canon(&main, IF);
    for (k, v) in canon(&child, IF) {
        if k != "<nodes>" {
            got.insert(k, v);
        }
    }
    assert_eq!(got.keys().collect::<Vec<_>>(), want.keys().collect::<Vec<_>>());
    for (iface, members) in &want {
        assert_eq!(got.get(iface), Some(members), "{iface} differs from schemas/hw1.xml");
    }
    let n: usize = want.iter().filter(|(k, _)| *k != "<nodes>").map(|(_, m)| m.len()).sum();
    assert_eq!(n, 60, "hw1.xml has 60 members");
}

// ---- authorization ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_session_user_sets_tdp_like_steamos_managers_relay() {
    let env = Env::oxp3().await;
    // what steamos-manager's user daemon does: Ping "/" on the remote's bus name, then use the
    // configured object path through that name, as the session user
    let r = env.run(
        Who::Session,
        json!([
            {"op": "ping", "dest": TDP_NAME, "path": "/"},
            som_get(TDP_NAME, "TdpLimit1", "TdpLimitMin"),
            som_get(TDP_NAME, "TdpLimit1", "TdpLimitMax"),
            set_tdp(20),
            som_get(TDP_NAME, "TdpLimit1", "TdpLimit"),
        ]),
    );
    assert!(r.iter().all(ok), "{r:?}\n{}", env.log());
    assert_eq!(
        (r[1]["value"].as_u64(), r[2]["value"].as_u64(), r[4]["value"].as_u64()),
        (Some(5), Some(35), Some(20))
    );
    assert_eq!(env.tree.watts("intel-rapl:0", "long_term"), 20.0);
    assert_eq!(env.tree.watts("intel-rapl-mmio:0", "long_term"), 20.0);
    assert!(env.changed("tdp", "set", S).await);
    let uid = env.users.session.uid;
    assert!(
        env.log().contains(&format!("audit uid={uid} knob=tdp")),
        "{}",
        env.log()
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn uid_0_is_refused() {
    let env = Env::oxp3().await;
    if !env.can(Who::Root) {
        eprintln!("skipped: needs a root test run (the bus reports the real uid); auth::check covers uid 0");
        return;
    }
    // even when logind says root's session is the active one
    env.logind.st.lock().unwrap().user = Some(0);
    for step in [
        set_tdp(20),
        call("PowerLimits1", "SetBoost", "suu", json!(["manual", 40, 0])),
        call("PackagePower1", "Sample", "", json!([])),
    ] {
        let r = env.one(Who::Root, step);
        assert_eq!(err(&r), ACCESS, "{r}");
    }
    assert!(env.tree.writes().is_empty());
    // reads stay open
    let r = env.one(Who::Root, call("Provider1", "Describe", "s", json!(["tdp"])));
    assert!(ok(&r), "{r}");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_wrong_uid_is_refused() {
    let env = Env::oxp3().await;
    if env.can(Who::Other) {
        let r = env.one(Who::Other, set_tdp(20));
        assert_eq!(err(&r), ACCESS);
    } else {
        // single-uid run: the caller's uid is right, logind's active user is someone else
        env.logind.st.lock().unwrap().user = Some(env.users.session.uid + 1);
        let r = env.one(Who::Session, set_tdp(20));
        assert_eq!(err(&r), ACCESS);
    }
    assert!(env.tree.writes().is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn remote_greeter_inactive_or_no_session_is_refused() {
    let env = Env::oxp3().await;
    type Change = fn(&mut common::harness::LoginState);
    let cases: [(&str, Change); 5] = [
        ("remote", |s| s.remote = true),
        ("greeter", |s| s.class = "greeter".into()),
        ("online", |s| s.state = "online".into()),
        ("closing", |s| s.state = "closing".into()),
        ("none", |s| s.user = None),
    ];
    for (what, change) in cases {
        let saved = {
            let mut st = env.logind.st.lock().unwrap();
            let saved = (st.user, st.remote, st.class.clone(), st.state.clone());
            change(&mut st);
            saved
        };
        let r = env.one(Who::Session, call("FanMode1", "SetFanMode", "s", json!(["auto"])));
        assert_eq!(err(&r), ACCESS, "{what}: {r}");
        let mut st = env.logind.st.lock().unwrap();
        (st.user, st.remote, st.class, st.state) = saved;
    }
    let r = env.one(Who::Session, call("FanMode1", "SetFanMode", "s", json!(["auto"])));
    assert!(ok(&r), "{r}");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn only_the_provider_user_may_own_the_names_and_one_provider_runs() {
    let mut env = Env::oxp3().await;
    if env.can(Who::Other) {
        for name in [
            DEST,
            TDP_NAME,
            GPU_NAME,
            PROFILE_NAME,
            CHARGE_NAME,
            "io.github.featherwolf.Bathysphere.Hw1.Other",
        ] {
            for who in [Who::Session, Who::Other] {
                let r = env.one(who, json!({"op": "own", "name": name}));
                assert_eq!(err(&r), ACCESS, "{who:?} owning {name}: {r}");
            }
        }
    } else {
        eprintln!("partly skipped: name ownership by another uid needs a root test run");
    }
    // a second provider exits: one provider per machine
    let mut second = Daemon::spawn(env.command(), &env.bus);
    let st = second
        .wait_exit(Duration::from_secs(10))
        .expect("the second provider exits");
    assert!(!st.success());
    assert!(second.log().contains("one provider per machine"), "{}", second.log());
    assert!(env.has_owner(DEST).await);
    env.daemon.take();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn package_power_needs_authorization_and_reads_are_open() {
    let env = Env::oxp3().await;
    let r = env.one(Who::Session, call("PackagePower1", "Sample", "", json!([])));
    assert!(ok(&r), "{r}");
    let v = r["value"].as_array().unwrap();
    assert!(
        v[1].as_u64().unwrap() >= 1000 && v[0].as_u64().unwrap() % 100 == 0,
        "{r}"
    );
    if env.can(Who::Other) {
        assert_eq!(
            err(&env.one(Who::Other, call("PackagePower1", "Sample", "", json!([])))),
            ACCESS
        );
        for step in [
            call("Provider1", "Describe", "s", json!(["fan"])),
            json!({"op": "get", "dest": DEST, "path": PATH, "iface": format!("{IF}.Provider1"), "prop": "Knobs"}),
            som_get(TDP_NAME, "TdpLimit1", "TdpLimit"),
        ] {
            assert!(ok(&env.one(Who::Other, step.clone())), "{step}");
        }
    }
}

// ---- names, bounds, errors ------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn remote_names_are_claimed_only_while_their_knob_works() {
    // Linux 7.2: no oxpec, so no BatteryChargeLimit1 (steamos-manager would show a dead slider)
    let mut env = Env::new(|t| t.oxp3(false)).await;
    env.start().await;
    assert!(env.has_owner(TDP_NAME).await && env.has_owner(GPU_NAME).await && env.has_owner(PROFILE_NAME).await);
    assert!(!env.has_owner(CHARGE_NAME).await);
    let knobs = env.prop(PATH, &format!("{IF}.Provider1"), "Knobs").await;
    let knobs: Vec<String> = knobs.try_into().unwrap();
    assert!(!knobs.contains(&"chargeLimit".to_string()) && !knobs.contains(&"fan".to_string()));
    let r = env.one(Who::Session, call("Provider1", "Describe", "s", json!(["fan"])));
    assert_eq!(r["value"]["reason"], "kernel");
    drop(env);

    // a missing grant fails closed: the knob is unavailable and its name is not claimed
    let mut env = Env::new(|t| {
        t.oxp3(true);
        t.set_grant(&t.path(&t.limit("intel-rapl:0", "long_term")), Grant::No);
        t.set_grant(
            &t.path("sys/class/power_supply/BAT0/charge_control_end_threshold"),
            Grant::No,
        );
    })
    .await;
    env.start().await;
    assert!(!env.has_owner(TDP_NAME).await && !env.has_owner(CHARGE_NAME).await);
    assert!(env.has_owner(GPU_NAME).await);
    let r = env.one(Who::Session, call("Provider1", "Describe", "s", json!(["tdp"])));
    assert_eq!(
        (r["value"]["available"].as_bool(), r["value"]["reason"].as_str()),
        (Some(false), Some("grants")),
        "{r}"
    );
    let r = env.one(
        Who::Session,
        call("PowerLimits1", "SetBoost", "suu", json!(["manual", 40, 0])),
    );
    assert_eq!(
        (err(&r).as_str(), r["message"].as_str()),
        (
            "io.github.featherwolf.Bathysphere.Hw1.Error.NotSupported",
            Some("grants")
        )
    );
    drop(env);

    // a constraint that refuses writes with EACCES locks the knob for this boot: the name goes
    let env = Env::oxp3().await;
    env.tree
        .rules(json!({"fail": {env.tree.limit("intel-rapl:0", "long_term"): "EACCES"}}));
    let r = env.one(Who::Session, set_tdp(20));
    assert_eq!(err(&r), "org.freedesktop.DBus.Error.Failed");
    assert!(
        r["message"]
            .as_str()
            .unwrap()
            .contains("Error.Locked: intel-rapl:0/pl1"),
        "{r}"
    );
    assert!(env.changed("tdp", "unavailable", S).await);
    assert!(!env.has_owner(TDP_NAME).await);
    let r = env.one(
        Who::Session,
        call("PowerLimits1", "SetBoost", "suu", json!(["manual", 40, 0])),
    );
    assert_eq!(r["message"], "locked");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn out_of_range_is_invalid_and_writes_nothing() {
    let env = Env::oxp3().await;
    let steps = vec![
        set_tdp(4),
        set_tdp(36),
        set_tdp(5000),
        set_tdp(u32::MAX),
        som_set(TDP_NAME, "TdpLimit1", "TdpLimit", "v", json!({"t": "s", "v": "20"})),
        som_set(
            GPU_NAME,
            "GpuPerformanceLevel1",
            "GpuPerformanceLevel",
            "v",
            json!({"t": "s", "v": "low"}),
        ),
        som_set(
            GPU_NAME,
            "GpuPerformanceLevel1",
            "ManualGpuClock",
            "v",
            json!({"t": "u", "v": 99}),
        ),
        som_set(
            GPU_NAME,
            "GpuPerformanceLevel1",
            "ManualGpuClock",
            "v",
            json!({"t": "u", "v": 2301}),
        ),
        som_set(
            PROFILE_NAME,
            "GpuPowerProfile1",
            "GpuPowerProfile",
            "v",
            json!({"t": "s", "v": "performance"}),
        ),
        som_set(
            CHARGE_NAME,
            "BatteryChargeLimit1",
            "MaxChargeLevel",
            "v",
            json!({"t": "i", "v": 0}),
        ),
        som_set(
            CHARGE_NAME,
            "BatteryChargeLimit1",
            "MaxChargeLevel",
            "v",
            json!({"t": "i", "v": 49}),
        ),
        som_set(
            CHARGE_NAME,
            "BatteryChargeLimit1",
            "MaxChargeLevel",
            "v",
            json!({"t": "i", "v": 101}),
        ),
        som_set(
            CHARGE_NAME,
            "BatteryChargeLimit1",
            "MaxChargeLevel",
            "v",
            json!({"t": "i", "v": -2}),
        ),
        call("PowerLimits1", "SetBoost", "suu", json!(["manual", 53, 0])),
        call("PowerLimits1", "SetBoost", "suu", json!(["manual", 4294967295u32, 0])),
        call("PowerLimits1", "SetBoost", "suu", json!(["follow", 0, 48])),
        call("PowerLimits1", "SetBoost", "suu", json!(["firmware", 52, 0])),
        call("PowerLimits1", "SetBoost", "suu", json!(["pl4", 0, 0])),
        call("CpuEnergy1", "SetEnergyPreference", "a{ss}", json!([{"all": "turbo"}])),
        call(
            "CpuEnergy1",
            "SetEnergyPreference",
            "a{ss}",
            json!([{"all": "power", "p": "power"}]),
        ),
        call(
            "CpuLimits1",
            "SetCpuLimits",
            "a{sv}",
            json!([{"max_perf_pct": {"t": "u", "v": 29}}]),
        ),
        call(
            "CpuLimits1",
            "SetCpuLimits",
            "a{sv}",
            json!([{"max_perf_pct": {"t": "s", "v": "50"}}]),
        ),
        call(
            "CpuLimits1",
            "SetCpuLimits",
            "a{sv}",
            json!([{"no_turbo": {"t": "u", "v": 1}}]),
        ),
        call(
            "CpuLimits1",
            "SetCpuLimits",
            "a{sv}",
            json!([{"core_type_max_freq": {"t": "a{su}", "v": {"p": 9000}}}]),
        ),
        call("CpuLimits1", "SetCpuLimits", "a{sv}", json!([{}])),
        call("SocBalance1", "SetSocBalance", "uu", json!([0, 0])),
        call("SocBalance1", "SetSocBalance", "uu", json!([6, 0])),
        call("SocBalance1", "SetSocBalance", "uu", json!([3, 4])),
        call("GpuRange1", "SetAutoRange", "uu", json!([99, 2300])),
        call("GpuRange1", "SetAutoRange", "uu", json!([100, 2301])),
        call("GpuRange1", "SetAutoRange", "uu", json!([1800, 300])),
        call("FanMode1", "SetFanMode", "s", json!(["turbo"])),
        call(
            "BatteryChargeBehaviour1",
            "SetChargeBehaviour",
            "s",
            json!(["inhibit-charge"]),
        ),
        call(
            "BatteryChargeBehaviour1",
            "SetChargeBehaviour",
            "s",
            json!(["force-discharge"]),
        ),
        call("Provider1", "Describe", "s", json!(["governor"])),
        call("Provider1", "Release", "as", json!([["tdp"]])),
        call("Provider1", "TakeControl", "as", json!([[]])),
    ];
    // in chunks: more than 20 calls a second from one sender are LimitsExceeded
    for chunk in steps.chunks(15) {
        let r = env.run(Who::Session, serde_json::Value::Array(chunk.to_vec()));
        for (step, res) in chunk.iter().zip(&r) {
            assert_eq!(err(res), INVALID, "{step} -> {res}");
        }
        tokio::time::sleep(Duration::from_millis(1100)).await;
    }
    assert!(env.tree.writes().is_empty(), "{:?}", env.tree.writes());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn not_applied_carries_the_observed_value() {
    let env = Env::oxp3().await;
    env.tree
        .rules(json!({"ignore": [env.tree.limit("intel-rapl-mmio:0", "short_term")]}));
    env.tree.set_watts("intel-rapl-mmio:0", "short_term", 52);
    let r = env.one(
        Who::Session,
        call("PowerLimits1", "SetBoost", "suu", json!(["manual", 40, 0])),
    );
    assert_eq!(
        (err(&r).as_str(), r["message"].as_str()),
        (
            "io.github.featherwolf.Bathysphere.Hw1.Error.NotApplied",
            Some("boost=52")
        ),
        "{r}"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn charge_limit_minus_one_is_100_and_the_limit_persists() {
    let mut env = Env::oxp3().await;
    let th = "sys/class/power_supply/BAT0/charge_control_end_threshold";
    env.tree.poke(th, 80);
    let set = |v: i32| {
        som_set(
            CHARGE_NAME,
            "BatteryChargeLimit1",
            "MaxChargeLevel",
            "v",
            json!({"t": "i", "v": v}),
        )
    };
    let r = env.run(
        Who::Session,
        json!([
            set(-1),
            som_get(CHARGE_NAME, "BatteryChargeLimit1", "MaxChargeLevel"),
            som_get(CHARGE_NAME, "BatteryChargeLimit1", "SuggestedMinimumLimit")
        ]),
    );
    assert!(r.iter().all(ok), "{r:?}");
    assert_eq!(env.tree.get(th), "100", "-1 is no limit: 100, never 0");
    assert_eq!((r[1]["value"].as_i64(), r[2]["value"].as_i64()), (Some(100), Some(50)));
    assert!(ok(&env.one(Who::Session, set(60))));
    // a restart (after a reboot reset the threshold) re-applies the persisted limit
    env.daemon.take();
    env.tree.poke(th, 100);
    env.start().await;
    assert_eq!(env.tree.get(th), "60");
}

// ---- leases ----------------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn leases_end_with_the_connection() {
    let env = Env::oxp3().await;
    let a = env.hold(
        Who::Session,
        json!([call("PowerLimits1", "SetBoost", "suu", json!(["manual", 40, 0]))]),
    );
    assert!(ok(&a.results[0]), "{:?}", a.results);
    assert_eq!(env.tree.watts("intel-rapl:0", "short_term"), 40.0);
    let b = env.one(
        Who::Session,
        call("PowerLimits1", "SetBoost", "suu", json!(["manual", 30, 0])),
    );
    assert_eq!(
        (err(&b).as_str(), b["message"].as_str().unwrap()),
        (BUSY, format!("lease:{}", a.name).as_str())
    );
    let d = env.one(Who::Session, call("Provider1", "Describe", "s", json!(["boost"])));
    assert_eq!(d["value"]["lease"].as_str(), Some(a.name.as_str()));
    a.drop_connection();
    assert!(
        env.until(2 * S, |t| t.watts("intel-rapl:0", "short_term") == 52.0)
            .await,
        "PL2 back to the firmware default"
    );
    assert!(env.changed("boost", "reverted-disconnect", S).await);
    let b = env.one(
        Who::Session,
        call("PowerLimits1", "SetBoost", "suu", json!(["manual", 30, 0])),
    );
    assert!(ok(&b), "{b}");
}

// ---- fan safety ------------------------------------------------------------------------------

fn fan(t: &common::fake::Tree) -> String {
    t.get("sys/class/hwmon/hwmon5/pwm1_enable")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn fan_returns_to_auto_on_temperature_sensor_sleep_and_disconnect() {
    let env = Env::oxp3().await;
    let set = |m: &str| call("FanMode1", "SetFanMode", "s", json!([m]));
    let a = env.hold(Who::Session, json!([set("quiet")]));
    assert!(ok(&a.results[0]), "{:?}\n{}", a.results, env.log());
    assert_eq!(fan(&env.tree), "1");
    assert_eq!(
        env.logind.st.lock().unwrap().inhibits.first().cloned(),
        Some((
            "sleep".into(),
            "oxp3-hw".into(),
            "fan back to auto".into(),
            "delay".into()
        ))
    );
    // 90 C: back to auto within the 1 s guard; quiet is then refused during the cooldown
    env.tree.set_temp(90_000);
    assert!(env.until(2 * S, |t| fan(t) == "2").await, "{}", env.log());
    assert!(env.changed("fan", "reverted-temperature", S).await);
    assert!(
        env.until(S, |_| env.logind.inhibitors_closed()).await,
        "the inhibitor is released"
    );
    env.tree.set_temp(50_000);
    let r = env.one(Who::Session, set("quiet"));
    assert_eq!(err(&r), BUSY);
    assert!(r["message"].as_str().unwrap().starts_with("cooldown:"), "{r}");
    let last: String = env
        .prop(PATH, &format!("{IF}.FanMode1"), "LastRevert")
        .await
        .try_into()
        .unwrap();
    assert_eq!(last, "temperature");
    drop(a);

    // an unreadable sensor
    let a = env.hold(Who::Session, json!([set("max")]));
    assert!(ok(&a.results[0]), "{:?}", a.results);
    assert_eq!(fan(&env.tree), "0");
    env.tree.remove("sys/class/hwmon/hwmon3/temp1_input");
    assert!(env.until(2 * S, |t| fan(t) == "2").await);
    assert!(env.changed("fan", "reverted-sensor", S).await);
    env.tree.put("sys/class/hwmon/hwmon3/temp1_input", 50_000, Grant::No);
    drop(a);

    // sleep: auto on PrepareForSleep(true), then the inhibitor goes
    let a = env.hold(Who::Session, json!([set("max")]));
    assert!(ok(&a.results[0]), "{:?}", a.results);
    env.logind.sleep(true).await;
    assert!(env.until(2 * S, |t| fan(t) == "2").await);
    assert!(env.changed("fan", "reverted-sleep", S).await);
    assert!(env.until(S, |_| env.logind.inhibitors_closed()).await);
    env.logind.sleep(false).await;

    // the lease holder's connection goes
    let r = env.run(Who::Session, json!([set("max")]));
    assert!(ok(&r[0]));
    assert!(
        env.until(2 * S, |t| fan(t) == "2").await,
        "the one-shot caller's connection closed"
    );
    assert!(env.changed("fan", "reverted-disconnect", S).await);
    drop(a);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn sigterm_restores_the_safe_state() {
    let mut env = Env::oxp3().await;
    let a = env.hold(
        Who::Session,
        json!([
            call("FanMode1", "SetFanMode", "s", json!(["max"])),
            call(
                "BatteryChargeBehaviour1",
                "SetChargeBehaviour",
                "s",
                json!(["inhibit-charge-awake"])
            ),
            call("PowerLimits1", "SetBoost", "suu", json!(["manual", 30, 0])),
        ]),
    );
    assert!(a.results[..3].iter().all(ok), "{:?}", a.results);
    let d = env.daemon.as_mut().unwrap();
    d.signal(libc::SIGTERM);
    let st = d.wait_exit(Duration::from_secs(5)).expect("exits on SIGTERM");
    assert!(st.success(), "{st}");
    assert_eq!(fan(&env.tree), "2");
    assert_eq!(
        env.tree.get("sys/class/power_supply/BAT0/charge_behaviour"),
        "[auto] inhibit-charge inhibit-charge-awake"
    );
    assert_eq!(env.tree.watts("intel-rapl:0", "short_term"), 52.0);
    assert!(env.signals().contains(&("fan".into(), "reverted-stop".into())));
    drop(a);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_stopped_provider_misses_the_watchdog_and_execstoppost_restores() {
    // the service manager's part, done by the test: NOTIFY_SOCKET, WatchdogSec, kill, ExecStopPost
    let mut env = Env::new(|t| t.oxp3(true)).await;
    let sock_path = env.bus.dir.join("notify");
    let sock = std::os::unix::net::UnixDatagram::bind(&sock_path).unwrap();
    std::fs::set_permissions(&sock_path, std::os::unix::fs::PermissionsExt::from_mode(0o777)).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(100))).unwrap();
    let mut cmd = env.command();
    cmd.env("NOTIFY_SOCKET", &sock_path);
    env.start_with(cmd).await;
    let a = env.hold(
        Who::Session,
        json!([
            call("FanMode1", "SetFanMode", "s", json!(["max"])),
            call(
                "BatteryChargeBehaviour1",
                "SetChargeBehaviour",
                "s",
                json!(["inhibit-charge-awake"])
            ),
        ]),
    );
    assert!(a.results[..2].iter().all(ok), "{:?}", a.results);
    let pings = |within: Duration| {
        let deadline = std::time::Instant::now() + within;
        let mut n = 0;
        let mut buf = [0u8; 256];
        while std::time::Instant::now() < deadline {
            if let Ok(k) = sock.recv(&mut buf) {
                if &buf[..k] == b"WATCHDOG=1" {
                    n += 1;
                }
            }
        }
        n
    };
    assert!(
        pings(Duration::from_millis(800)) >= 3,
        "the safety task feeds the watchdog"
    );
    // a hung provider (SIGSTOP): no more pings, so the manager kills it after WatchdogSec
    let d = env.daemon.as_mut().unwrap();
    d.signal(libc::SIGSTOP);
    let _ = pings(Duration::from_millis(150));
    assert_eq!(pings(Duration::from_millis(1000)), 0, "no ping while stopped");
    assert_eq!(fan(&env.tree), "0", "nothing restored the fan yet");
    d.signal(libc::SIGKILL);
    d.wait_exit(Duration::from_secs(5)).unwrap();
    // ExecStopPost: oxp3-hwd --restore-safe with the group grants only
    let mut post = Daemon::command(&env.tree, &env.bus, &env.users, SCALE);
    post.arg("--restore-safe");
    let st = post.status().unwrap();
    assert!(st.success());
    assert_eq!(fan(&env.tree), "2");
    assert_eq!(
        env.tree.get("sys/class/power_supply/BAT0/charge_behaviour"),
        "[auto] inhibit-charge inhibit-charge-awake"
    );
    drop(a);
}

// ---- held values -----------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn drift_stops_after_two_overrides_until_take_control() {
    let env = Env::oxp3().await;
    assert!(ok(&env.one(Who::Session, set_tdp(15))));
    env.clear_signals();
    // another writer (thermald is not running here) raises MSR PL1
    env.tree.set_watts("intel-rapl:0", "long_term", 25);
    assert!(env.changed("tdp", "overridden", 3 * S).await, "{}", env.log());
    env.tree.clear_writes();
    tokio::time::sleep(S).await;
    assert!(env.tree.writes().is_empty(), "never fights: {:?}", env.tree.writes());
    assert_eq!(env.tree.watts("intel-rapl:0", "long_term"), 25.0);
    let d = env.one(Who::Session, call("Provider1", "Describe", "s", json!(["tdp"])));
    assert_eq!(d["value"]["overridden"], true);
    assert!(ok(&env.one(
        Who::Session,
        call("Provider1", "TakeControl", "as", json!([["tdp"]]))
    )));
    assert_eq!(env.tree.watts("intel-rapl:0", "long_term"), 15.0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn ac_changes_and_resume_reassert_held_values() {
    let env = Env::oxp3().await;
    assert!(ok(&env.one(Who::Session, set_tdp(20))));
    // EPP is leased: the lease (and the value) lasts as long as the caller's connection
    let a = env.hold(
        Who::Session,
        json!([call(
            "CpuEnergy1",
            "SetEnergyPreference",
            "a{ss}",
            json!([{"p": "performance"}])
        )]),
    );
    assert!(ok(&a.results[0]), "{:?}", a.results);
    env.clear_signals();
    // the charger: firmware rewrites the MMIO limits to 35/52 W on every AC change
    env.tree.plug(true);
    assert!(
        env.until(3 * S, |t| t.watts("intel-rapl-mmio:0", "long_term") == 20.0)
            .await,
        "{}",
        env.log()
    );
    assert!(env.changed("tdp", "reasserted-ac", S).await);
    assert!(!env.signals().contains(&("tdp".into(), "overridden".into())));
    // resume: firmware's values back in both places
    env.logind.sleep(true).await;
    env.tree.set_watts("intel-rapl:0", "long_term", 25);
    env.tree.poke(
        "sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference",
        "balance_performance",
    );
    env.logind.sleep(false).await;
    assert!(
        env.until(3 * S, |t| t.watts("intel-rapl:0", "long_term") == 20.0).await,
        "{}",
        env.log()
    );
    assert!(
        env.until(S, |t| t
            .get("sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference")
            == "performance")
            .await,
        "{}",
        env.log()
    );
    assert!(env.changed("tdp", "reasserted-resume", S).await);
    assert!(env.changed("epp", "reasserted-resume", S).await);
    drop(a);
}

// ---- co-writers, rates, kill switch -----------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn co_writers_make_their_knobs_busy() {
    let env = Env::oxp3().await;
    let ppd = env.bus.conn().await;
    ppd.request_name("org.freedesktop.UPower.PowerProfiles").await.unwrap();
    assert!(env.changed("epp", "unavailable", 2 * S).await);
    let r = env.one(
        Who::Session,
        call("CpuEnergy1", "SetEnergyPreference", "a{ss}", json!([{"all": "power"}])),
    );
    assert_eq!(
        (err(&r).as_str(), r["message"].as_str()),
        (BUSY, Some("power-profiles-daemon"))
    );
    let d = env.one(Who::Session, call("Provider1", "Describe", "s", json!(["epp"])));
    assert_eq!(d["value"]["reason"], "busy:power-profiles-daemon");
    drop(ppd);
    assert!(env.changed("epp", "available", 2 * S).await);
    // PowerStation accepts unauthenticated writes: every power and GPU knob steps aside
    let ps = env.bus.conn().await;
    ps.request_name("org.shadowblip.PowerStation").await.unwrap();
    assert!(env.changed("tdp", "unavailable", 2 * S).await);
    assert!(env.until(S, |_| true).await);
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(!env.has_owner(TDP_NAME).await && !env.has_owner(GPU_NAME).await);
    let r = env.one(
        Who::Session,
        call("PowerLimits1", "SetBoost", "suu", json!(["manual", 40, 0])),
    );
    assert_eq!((err(&r).as_str(), r["message"].as_str()), (BUSY, Some("powerstation")));
    let d = env.one(Who::Session, call("Provider1", "Describe", "s", json!(["boost"])));
    assert_eq!(d["value"]["reason"], "busy:powerstation");
    drop(ps);
    assert!(env.changed("tdp", "available", 2 * S).await);
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(env.has_owner(TDP_NAME).await);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn more_than_20_calls_a_second_are_refused() {
    let env = Env::oxp3().await;
    let steps: Vec<_> = (0..25)
        .map(|_| call("Provider1", "Describe", "s", json!(["tdp"])))
        .collect();
    let r = env.run(Who::Session, serde_json::Value::Array(steps));
    assert!(r[..20].iter().all(ok));
    assert!(
        r[20..]
            .iter()
            .all(|x| err(x) == "org.freedesktop.DBus.Error.LimitsExceeded"),
        "{:?}",
        &r[20..]
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_burst_is_coalesced() {
    let env = Env::oxp3().await;
    assert!(ok(&env.one(Who::Session, set_tdp(10))));
    env.tree.clear_writes();
    // six callers at once: one write now, then the newest waiting value once the slot is due
    let children: Vec<_> = (0..6u32)
        .map(|i| {
            let mut c = std::process::Command::new(env!("CARGO_BIN_EXE_oxp3-hw-testcall"));
            if env.users.multi {
                c = std::process::Command::new("/usr/bin/setpriv");
                c.arg(format!("--reuid={}", env.users.session.uid))
                    .arg(format!("--regid={}", env.users.session.gid))
                    .arg("--clear-groups")
                    .arg("--")
                    .arg(env!("CARGO_BIN_EXE_oxp3-hw-testcall"));
            }
            c.arg(&env.bus.address).arg(json!([set_tdp(20 + i)]).to_string());
            c.stdout(std::process::Stdio::piped()).spawn().unwrap()
        })
        .collect();
    for c in children {
        let out = c.wait_with_output().unwrap();
        let r: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
        assert!(ok(&r), "{r}");
    }
    let pl1_writes = env
        .tree
        .names()
        .iter()
        .filter(|n| *n == "intel-rapl:0/long_term")
        .count();
    assert!(
        (1..=3).contains(&pl1_writes),
        "{pl1_writes} committed writes: {:?}",
        env.tree.names()
    );
    let w = env.tree.watts("intel-rapl:0", "long_term") as u32;
    assert!((20..26).contains(&w));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_kill_switch_restores_and_exits() {
    let mut env = Env::oxp3().await;
    let a = env.hold(
        Who::Session,
        json!([call("FanMode1", "SetFanMode", "s", json!(["max"]))]),
    );
    assert!(ok(&a.results[0]));
    std::fs::write(env.tree.path("etc/oxp3-hw/disabled"), "").unwrap();
    let st = env
        .daemon
        .as_mut()
        .unwrap()
        .wait_exit(Duration::from_secs(5))
        .expect("exits");
    assert!(st.success());
    assert_eq!(fan(&env.tree), "2");
    // and it does not start while the file exists
    let mut d = Daemon::spawn(env.command(), &env.bus);
    assert!(d.wait_exit(Duration::from_secs(5)).unwrap().success());
    assert!(!env.has_owner(DEST).await);
    drop(a);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn startup_refusals() {
    let env = Env::new(|t| t.oxp3(true)).await;
    // root
    if common::fake::root_test() {
        let out = std::process::Command::new(env!("CARGO_BIN_EXE_oxp3-hwd"))
            .args(["--sysfs-root"])
            .arg(&env.tree.root)
            .args(["--bus", &env.bus.address])
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(1));
        assert!(String::from_utf8_lossy(&out.stderr).contains("refusing to run as root"));
    }
    // a malformed or widening admin file: exit 78, nothing served
    for bad in ["sustained_max_w = 40\n", "fan = 1\n", "sustained_ac_max_w = 60\n"] {
        std::fs::write(env.tree.path("etc/oxp3-hw/limits.toml"), bad).unwrap();
        let st = Daemon::command(&env.tree, &env.bus, &env.users, SCALE)
            .output()
            .unwrap();
        assert_eq!(
            st.status.code(),
            Some(78),
            "{bad}: {}",
            String::from_utf8_lossy(&st.stderr)
        );
    }
    std::fs::remove_file(env.tree.path("etc/oxp3-hw/limits.toml")).unwrap();
    // another device: exports nothing
    env.tree.poke("sys/class/dmi/id/board_name", "ONEXPLAYER X1");
    let st = Daemon::command(&env.tree, &env.bus, &env.users, SCALE)
        .output()
        .unwrap();
    assert!(st.status.success());
    assert!(String::from_utf8_lossy(&st.stderr).contains("not an ONEXPLAYER 3"));
    assert!(!env.has_owner(DEST).await);
    assert!(env.tree.writes().is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn admin_ac_limit_is_published_and_capped_on_battery() {
    let mut env = Env::new(|t| t.oxp3(true)).await;
    std::fs::write(env.tree.path("etc/oxp3-hw/limits.toml"), "sustained_ac_max_w = 40\n").unwrap();
    env.start().await;
    let r = env.run(
        Who::Session,
        json!([som_get(TDP_NAME, "TdpLimit1", "TdpLimitMax"), set_tdp(40)]),
    );
    assert_eq!(r[0]["value"].as_u64(), Some(40));
    assert!(ok(&r[1]), "{r:?}");
    assert_eq!(env.tree.watts("intel-rapl:0", "long_term"), 35.0);
    assert!(env.changed("tdp", "capped-battery", S).await);
    let d = env.one(Who::Session, call("Provider1", "Describe", "s", json!(["tdp"])));
    assert_eq!(
        (d["value"]["value"].as_u64(), d["value"]["effective"].as_u64()),
        (Some(40), Some(35))
    );
    env.tree.plug(true);
    assert!(
        env.until(3 * S, |t| t.watts("intel-rapl:0", "long_term") == 40.0
            && t.watts("intel-rapl-mmio:0", "long_term") == 40.0)
            .await
    );
    env.tree.plug(false);
    assert!(env.until(3 * S, |t| t.watts("intel-rapl:0", "long_term") == 35.0).await);
    let _ = MW;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn gpu_remotes_and_the_auto_range() {
    let env = Env::oxp3().await;
    let f = "sys/class/drm/card1/device/tile0/gt0/freq0";
    let range = |t: &common::fake::Tree| (t.get(&format!("{f}/min_freq")), t.get(&format!("{f}/max_freq")));
    let level = |v: &str| {
        som_set(
            GPU_NAME,
            "GpuPerformanceLevel1",
            "GpuPerformanceLevel",
            "v",
            json!({"t": "s", "v": v}),
        )
    };
    let clock = |v: u32| {
        som_set(
            GPU_NAME,
            "GpuPerformanceLevel1",
            "ManualGpuClock",
            "v",
            json!({"t": "u", "v": v}),
        )
    };
    // a plugin's auto range, held by its connection
    let a = env.hold(
        Who::Session,
        json!([call("GpuRange1", "SetAutoRange", "uu", json!([300, 1800]))]),
    );
    assert!(ok(&a.results[0]), "{:?}", a.results);
    assert_eq!(range(&env.tree), ("300".into(), "1800".into()));
    // Steam's manual level pins min = max and wins
    let r = env.run(
        Who::Session,
        json!([
            som_get(GPU_NAME, "GpuPerformanceLevel1", "ManualGpuClockMin"),
            som_get(GPU_NAME, "GpuPerformanceLevel1", "ManualGpuClockMax"),
            level("manual"),
            clock(1200),
        ]),
    );
    assert!(r.iter().all(ok), "{r:?}");
    assert_eq!(
        (r[0]["value"].as_u64(), r[1]["value"].as_u64()),
        (Some(100), Some(2300)),
        "rpn..rp0, not rpe..rpa"
    );
    assert_eq!(range(&env.tree), ("1200".into(), "1200".into()));
    // another connection cannot change the range the plugin holds
    let r = env.one(
        Who::Session,
        call("GpuRange1", "SetAutoRange", "uu", json!([400, 2000])),
    );
    assert_eq!(
        (err(&r), r["message"].as_str().unwrap_or("")),
        (BUSY.to_string(), format!("lease:{}", a.name).as_str())
    );
    // back to auto: the held range applies again
    assert!(ok(&env.one(Who::Session, level("auto"))));
    assert_eq!(range(&env.tree), ("300".into(), "1800".into()));
    // the plugin goes: the boot range rpn..rp0 comes back
    a.drop_connection();
    assert!(env.until(2 * S, |t| range(t) == ("100".into(), "2300".into())).await);
    // the power profile remote
    let r = env.run(
        Who::Session,
        json!([
            som_get(PROFILE_NAME, "GpuPowerProfile1", "AvailableGpuPowerProfiles"),
            som_set(
                PROFILE_NAME,
                "GpuPowerProfile1",
                "GpuPowerProfile",
                "v",
                json!({"t": "s", "v": "power_saving"})
            ),
            som_get(PROFILE_NAME, "GpuPowerProfile1", "GpuPowerProfile"),
        ]),
    );
    assert!(r.iter().all(ok), "{r:?}");
    assert_eq!(r[0]["value"], json!(["base", "power_saving"]));
    assert_eq!(r[2]["value"], "power_saving");
    assert!(env.tree.writes().iter().all(|(p, _, _)| !p.contains("/gt1/")));
}
