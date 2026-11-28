// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! OXP3 Deck's fake-sysfs power tests (tests/test_powerhw.py, 55 tests), ported to oxp3-hwd: the
//! provider's own state machine on a --sysfs-root tree whose writes follow the kernel's store
//! semantics (src/emu.rs). Each test names the OXP3 Deck test it ports; where the provider's rules
//! differ (it refuses instead of clamping, it never writes turbo, it serves package-0 zones only)
//! the test checks the provider's rule and says so. Permission (grant) semantics need a non-root
//! process and are covered by tests/bus.rs, which runs the daemon under another uid.

mod common;

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use common::fake::{Owner, Tree, MW};
use oxp3_hw::device::ONEXPLAYER_3;
use oxp3_hw::discover;
use oxp3_hw::error::HwError;
use oxp3_hw::hw::{Ctx, Ev, Hw, Paths, Reason, Timing};
use oxp3_hw::limits;
use oxp3_hw::sysfs::Sys;

fn tree() -> Tree {
    Tree::new(Owner::Single)
}

fn make_with(t: &Tree, limits_text: &str) -> Hw {
    let sys = Sys::emulated(t.root.clone());
    let env = limits::envelope(&ONEXPLAYER_3, &limits::parse(limits_text).unwrap()).unwrap();
    let d = discover::discover(&sys);
    let paths = Paths {
        etc: t.path("etc/oxp3-hw"),
        state: t.path("var/lib/oxp3-hw"),
        run: t.path("run/oxp3-hw"),
    };
    Hw::new(sys, &ONEXPLAYER_3, env, d, Timing { scale: 0.01 }, paths)
}

fn make(t: &Tree) -> Hw {
    make_with(t, "")
}

fn ctx() -> Ctx {
    Ctx {
        uid: Some(1000),
        sender: Some(":1.42".into()),
    }
}

fn other() -> Ctx {
    Ctx {
        uid: Some(1000),
        sender: Some(":1.43".into()),
    }
}

fn name(r: Result<impl std::fmt::Debug, HwError>) -> (&'static str, String) {
    let e = r.expect_err("expected an error");
    (e.name_str(), e.message().to_string())
}

const INVALID: &str = "org.freedesktop.DBus.Error.InvalidArgs";
const BUSY: &str = "io.github.featherwolf.Bathysphere.Hw1.Error.Busy";
const NOT_APPLIED: &str = "io.github.featherwolf.Bathysphere.Hw1.Error.NotApplied";
const LOCKED: &str = "io.github.featherwolf.Bathysphere.Hw1.Error.Locked";
const NOT_SUPPORTED: &str = "io.github.featherwolf.Bathysphere.Hw1.Error.NotSupported";

fn changed(hw: &mut Hw) -> Vec<(String, String)> {
    std::mem::take(&mut hw.events)
        .into_iter()
        .filter_map(|e| match e {
            Ev::Changed(k, r) => Some((k.to_string(), r)),
            _ => None,
        })
        .collect()
}

/// A stand-in for logind's inhibitor fd.
fn inhibitor() -> std::os::fd::OwnedFd {
    std::fs::File::open("/dev/null").unwrap().into()
}

// ---- DiscoverTest ---------------------------------------------------------------------------

#[test]
fn the_oxp3() {
    // test_the_oxp3
    let t = tree();
    t.oxp3(true);
    let hw = make(&t);
    let z: Vec<_> = hw.d.zones.iter().map(|z| (z.id.as_str(), z.mmio)).collect();
    assert_eq!(z, vec![("intel-rapl-mmio:0", true), ("intel-rapl:0", false)]);
    assert!(hw.d.zones[1].pl1.ends_with("constraint_0_power_limit_uw"));
    assert!(hw.d.zones[1].pl2.ends_with("constraint_1_power_limit_uw"));
    let g = hw.d.gpu.as_ref().unwrap();
    assert_eq!((g.card.as_str(), g.rpn, g.rp0), ("card1", 100, 2300));
    assert!(g.min.to_string_lossy().contains("/tile0/gt0/freq0/"));
    let c = hw.d.cpu.as_ref().unwrap();
    assert_eq!(c.policies.len(), 14);
    assert_eq!(c.policies[2].name, "policy2");
    assert_eq!(c.policies.last().unwrap().name, "policy13", "by number, not by name");
    assert_eq!(
        c.epp_available,
        vec![
            "default",
            "performance",
            "balance_performance",
            "balance_power",
            "power"
        ]
    );
    assert_eq!(discover::ac_online(&hw.sys, &hw.d), Some(false));
    assert_eq!(
        hw.knobs(),
        vec![
            "tdp",
            "gpu",
            "gpuPowerProfile",
            "chargeLimit",
            "boost",
            "epp",
            "cpuLimits",
            "socBalance",
            "gpuRange",
            "fan",
            "chargeBehaviour",
            "packagePower"
        ]
    );
}

#[test]
fn one_interface_or_none() {
    // test_one_interface_or_none: the provider needs both package zones for tdp and boost
    for (msr, mmio) in [(true, false), (false, true)] {
        let t = tree();
        t.dmi("ONE-NETBOOK", "ONEXPLAYER 3");
        if msr {
            t.rapl(
                "intel-rapl:0",
                25,
                52,
                160,
                &["long_term", "short_term", "peak_power"],
                25,
                "package-0",
            );
        }
        if mmio {
            t.rapl(
                "intel-rapl-mmio:0",
                35,
                52,
                160,
                &["long_term", "short_term", "peak_power"],
                25,
                "package-0",
            );
        }
        let mut hw = make(&t);
        assert_eq!(hw.d.zones.len(), 1);
        assert_eq!(hw.reason("tdp"), Reason::Kernel);
        assert_eq!(
            name(hw.set_tdp(&ctx(), 20, true)),
            (NOT_SUPPORTED, "kernel".to_string())
        );
        assert!(t.writes().is_empty());
    }
    let t = tree();
    let hw = make(&t);
    assert!(hw.d.zones.is_empty() && hw.d.gpu.is_none() && hw.d.cpu.is_none());
    assert!(hw.knobs().is_empty());
}

#[test]
fn constraints_by_name_and_zones_without_long_term() {
    // test_constraints_by_name_and_zones_without_long_term
    let t = tree();
    t.rapl(
        "intel-rapl:0",
        25,
        52,
        160,
        &["peak_power", "short_term", "long_term"],
        25,
        "package-0",
    );
    t.rapl(
        "intel-rapl-mmio:0",
        35,
        52,
        160,
        &["short_term", "peak_power"],
        25,
        "package-0",
    );
    let hw = make(&t);
    assert_eq!(hw.d.zones.len(), 1);
    let z = &hw.d.zones[0];
    assert_eq!(z.id, "intel-rapl:0");
    assert!(z.pl1.ends_with("constraint_2_power_limit_uw"));
    assert!(z.pl2.ends_with("constraint_1_power_limit_uw"));
}

#[test]
fn psys_child_and_other_package_zones_are_never_written() {
    // test_psys_and_child_zones_are_ignored_and_a_tpmi_zone_is_kept: the provider resolves only
    // the package-0 zone of each interface (the udev grant covers exactly those)
    let t = tree();
    t.oxp3(true);
    t.rapl(
        "intel-rapl:2",
        25,
        52,
        160,
        &["long_term", "short_term", "peak_power"],
        25,
        "package-1",
    );
    let mut hw = make(&t);
    assert_eq!(
        hw.d.zones.iter().map(|z| z.id.as_str()).collect::<Vec<_>>(),
        vec!["intel-rapl-mmio:0", "intel-rapl:0"]
    );
    hw.set_tdp(&ctx(), 15, true).unwrap();
    assert_eq!(t.watts("intel-rapl:0", "long_term"), 15.0);
    assert_eq!(t.watts("intel-rapl:2", "long_term"), 25.0);
    assert_eq!(t.watts("intel-rapl:1", "long_term"), 0.0);
    assert!(t
        .writes()
        .iter()
        .all(|(p, _, _)| !p.contains("intel-rapl:1") && !p.contains("intel-rapl:2") && !p.contains("intel-rapl:0:0")));
}

// ---- GpuTest -------------------------------------------------------------------------------

#[test]
fn render_gt_is_picked() {
    let t = tree();
    t.xe(
        "card1",
        &[("gt0", Some("gt0-mc")), ("gt1", Some("gt1-rc"))],
        100,
        900,
        2300,
        850,
        2300,
        "xe",
    );
    let hw = make(&t);
    assert!(hw.d.gpu.as_ref().unwrap().min.to_string_lossy().contains("/gt1/"));
}

#[test]
fn without_gtidle_the_lowest() {
    let t = tree();
    t.xe(
        "card1",
        &[("gt1", None), ("gt0", None)],
        100,
        900,
        2300,
        850,
        2300,
        "xe",
    );
    let hw = make(&t);
    assert!(hw.d.gpu.as_ref().unwrap().min.to_string_lossy().contains("/gt0/"));
}

#[test]
fn i915_is_not_used() {
    let t = tree();
    t.xe("card0", &[("gt0", Some("gt0-rc"))], 100, 900, 2300, 850, 2300, "i915");
    let hw = make(&t);
    assert!(hw.d.gpu.is_none());
    assert_eq!(hw.reason("gpu"), Reason::Kernel);
}

#[test]
fn the_tick_never_opens_a_gpu_clock_file() {
    // test_reading_without_the_gpu_opens_no_clock_file: reading min/max/act/cur wakes the GPU
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_gpu_level(&ctx(), "manual", true).unwrap();
    hw.set_range(&ctx(), 300, 1800, true).unwrap();
    std::fs::write(t.path(".kernel/reads.log"), "").unwrap();
    let t0 = Instant::now();
    for i in 0..20 {
        hw.tick(t0 + Duration::from_millis(30 * i));
    }
    let reads = std::fs::read_to_string(t.path(".kernel/reads.log")).unwrap();
    assert!(!reads.is_empty(), "the tick reads something");
    for clock in ["min_freq", "max_freq", "act_freq", "cur_freq"] {
        assert!(
            !reads.lines().any(|l| l.contains("/drm/") && l.ends_with(clock)),
            "{clock} read by the tick"
        );
    }
}

#[test]
fn rp_files_and_the_media_gt_never_change() {
    // test_rp_files_and_the_media_gt_never_change
    let t = tree();
    t.oxp3(true);
    let tile = t.path("sys/class/drm/card1/device/tile0");
    let snap = || {
        let mut out = BTreeMap::new();
        for e in walk(&tile) {
            let s = e.to_string_lossy().to_string();
            if s.contains("/gt1/") || e.file_name().unwrap().to_string_lossy().starts_with("rp") {
                out.insert(s, std::fs::read(&e).unwrap());
            }
        }
        out
    };
    let before = snap();
    let mut hw = make(&t);
    hw.set_range(&ctx(), 300, 1800, true).unwrap();
    hw.set_range(&ctx(), 2000, 2300, true).unwrap();
    hw.set_gpu_level(&ctx(), "manual", true).unwrap();
    hw.set_gpu_clock(&ctx(), 1200, true).unwrap();
    hw.set_gpu_profile(&ctx(), "power_saving", true).unwrap();
    assert_eq!(snap(), before);
    assert!(t
        .writes()
        .iter()
        .all(|(p, _, _)| !p.contains("/gt1/") && !p.rsplit('/').next().unwrap().starts_with("rp")));
}

fn walk(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
    let mut out = vec![];
    for e in std::fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            out.extend(walk(&p));
        } else {
            out.push(p);
        }
    }
    out
}

// ---- OrderTest -----------------------------------------------------------------------------

fn order_tree() -> Tree {
    let t = tree();
    t.oxp3(true);
    for z in ["intel-rapl:0", "intel-rapl-mmio:0"] {
        t.set_watts(z, "long_term", 15);
        t.set_watts(z, "short_term", 20);
    }
    t
}

#[test]
fn raising_writes_pl2_first_and_mmio_first() {
    let t = order_tree();
    let mut hw = make(&t);
    hw.set_boost(&ctx(), "manual", 40, 0, true).unwrap();
    t.clear_writes();
    hw.set_tdp(&ctx(), 30, true).unwrap();
    // PL2 already reads 40 on both zones (set above), so only PL1 rises
    assert_eq!(t.names(), vec!["intel-rapl-mmio:0/long_term", "intel-rapl:0/long_term"]);
    let t = order_tree();
    let mut hw = make(&t);
    hw.set_tdp(&ctx(), 30, true).unwrap();
    // firmware boost (52 W) and PL1 30 W: PL2 before PL1 on each zone, MMIO first
    assert_eq!(
        t.names(),
        vec![
            "intel-rapl-mmio:0/short_term",
            "intel-rapl-mmio:0/long_term",
            "intel-rapl:0/short_term",
            "intel-rapl:0/long_term"
        ]
    );
    assert_eq!(
        (
            t.watts("intel-rapl:0", "long_term"),
            t.watts("intel-rapl:0", "short_term")
        ),
        (30.0, 52.0)
    );
}

#[test]
fn lowering_writes_pl1_first() {
    let t = order_tree();
    let mut hw = make(&t);
    hw.set_tdp(&ctx(), 10, true).unwrap();
    assert_eq!(
        &t.names()[..2],
        &["intel-rapl-mmio:0/long_term", "intel-rapl-mmio:0/short_term"]
    );
}

#[test]
fn an_equal_value_is_not_written() {
    let t = order_tree();
    for z in ["intel-rapl:0", "intel-rapl-mmio:0"] {
        t.poke(&t.limit(z, "long_term"), 15 * MW + 100_000);
        t.set_watts(z, "short_term", 52);
    }
    let mut hw = make(&t);
    hw.set_tdp(&ctx(), 15, true).unwrap();
    assert!(t.writes().is_empty(), "{:?}", t.writes());
    // the same value again only re-checks
    hw.set_tdp(&ctx(), 15, true).unwrap();
    assert!(t.writes().is_empty());
}

#[test]
fn pl2_below_pl1_is_raised() {
    let t = order_tree();
    let mut hw = make(&t);
    hw.set_boost(&ctx(), "manual", 10, 0, true).unwrap();
    changed(&mut hw);
    hw.set_tdp(&ctx(), 30, true).unwrap();
    assert_eq!(t.watts("intel-rapl:0", "short_term"), 30.0);
    assert_eq!(hw.boost_effective(), 30);
    assert!(changed(&mut hw).contains(&("boost".into(), "raised-with-sustained".into())));
}

#[test]
fn gpu_order() {
    let t = order_tree();
    let mut hw = make(&t);
    t.clear_writes();
    hw.set_range(&ctx(), 1000, 2000, true).unwrap(); // raising the minimum: max first
    assert_eq!(t.names(), vec!["gpu/max_freq", "gpu/min_freq"]);
    t.clear_writes();
    hw.set_range(&ctx(), 300, 600, true).unwrap();
    assert_eq!(t.names(), vec!["gpu/min_freq", "gpu/max_freq"]);
}

#[test]
fn never_written() {
    let t = order_tree();
    let mut hw = make(&t);
    hw.set_tdp(&ctx(), 30, true).unwrap();
    hw.set_boost(&ctx(), "manual", 40, 0, true).unwrap();
    hw.set_tdp(&ctx(), 10, true).unwrap();
    hw.set_boost(&ctx(), "follow", 0, 2, true).unwrap();
    hw.set_range(&ctx(), 300, 600, true).unwrap();
    hw.set_epp(&ctx(), &BTreeMap::from([("all".into(), "power".into())]), true)
        .unwrap();
    for (p, _, _) in t.writes() {
        let f = p.rsplit('/').next().unwrap().to_string();
        assert!(!f.contains("time_window") && f != "enabled" && f != "no_turbo", "{p}");
        assert!(!p.contains("intel-rapl:1") && !p.contains("intel-rapl:0:0"), "{p}");
    }
    assert!(
        !t.names().iter().any(|n| n.ends_with("/peak_power")),
        "PL4 written: {:?}",
        t.names()
    );
}

// ---- RefusalTest ---------------------------------------------------------------------------

#[test]
fn a_locked_msr_limit() {
    let t = tree();
    t.oxp3(true);
    t.rules(serde_json::json!({"fail": {t.limit("intel-rapl:0", "long_term"): "EACCES"}}));
    t.set_watts("intel-rapl:0", "short_term", 40); // so PL2 (52 W, the firmware boost) is written too
    let mut hw = make(&t);
    let (n, m) = name(hw.set_tdp(&ctx(), 15, true));
    assert_eq!((n, m.as_str()), (LOCKED, "intel-rapl:0/pl1"));
    assert_eq!(
        t.watts("intel-rapl-mmio:0", "long_term"),
        15.0,
        "the other interface is still written"
    );
    assert!(
        t.names().contains(&"intel-rapl:0/short_term".to_string()),
        "the other constraint is still tried"
    );
    // locked for this boot: tdp and boost become unavailable, and the lock is remembered
    assert_eq!(hw.reason("tdp"), Reason::Locked);
    assert!(changed(&mut hw).contains(&("tdp".into(), "unavailable".into())));
    let snap: serde_json::Value = serde_json::from_str(&t.get("run/oxp3-hw/snapshot.json")).unwrap();
    assert_eq!(snap["locked"], serde_json::json!(["intel-rapl:0/pl1"]));
}

#[test]
fn a_locked_id_is_skipped() {
    let t = tree();
    t.oxp3(true);
    t.rules(serde_json::json!({"fail": {t.limit("intel-rapl:0", "long_term"): ["EACCES"]}}));
    let mut hw = make(&t);
    let _ = hw.set_tdp(&ctx(), 15, true);
    t.clear_writes();
    // a restart in the same boot keeps the lock: nothing is tried again
    let mut hw = make(&t);
    assert_eq!(hw.reason("tdp"), Reason::Locked);
    assert_eq!(name(hw.set_tdp(&ctx(), 16, true)).0, NOT_SUPPORTED);
    assert!(t.writes().is_empty());
    // a new boot forgets it
    t.boot_id("boot-2");
    let hw = make(&t);
    assert_eq!(hw.reason("tdp"), Reason::Ok);
}

#[test]
fn an_mmio_write_that_is_ignored() {
    let t = tree();
    t.oxp3(true);
    t.rules(serde_json::json!({"ignore": [t.limit("intel-rapl-mmio:0", "long_term")]}));
    let mut hw = make(&t);
    assert_eq!(name(hw.set_tdp(&ctx(), 15, true)), (NOT_APPLIED, "tdp=35".to_string()));
    assert_eq!(t.watts("intel-rapl:0", "long_term"), 15.0);
}

#[test]
fn gpu_eagain_then_success_and_einval() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    t.rules(serde_json::json!({"fail": {"min_freq": ["EAGAIN", "EAGAIN", null]}}));
    hw.set_range(&ctx(), 300, 2300, true).unwrap();
    assert_eq!(t.names().iter().filter(|n| *n == "gpu/min_freq").count(), 3);
    t.rules(serde_json::json!({"fail": {"min_freq": "EINVAL"}}));
    assert_eq!(name(hw.set_range(&ctx(), 400, 2300, true)).0, NOT_APPLIED);
    t.rules(serde_json::json!({"fail": {"min_freq": "EAGAIN"}}));
    t.clear_writes();
    assert_eq!(name(hw.set_range(&ctx(), 500, 2300, true)).0, NOT_APPLIED);
    assert_eq!(
        t.names().iter().filter(|n| *n == "gpu/min_freq").count(),
        4,
        "1 try + 3 retries"
    );
    // outside rpn..rp0 is refused before anything is written
    t.clear_writes();
    assert_eq!(name(hw.set_range(&ctx(), 50, 2300, true)).0, INVALID);
    assert_eq!(name(hw.set_range(&ctx(), 300, 3000, true)).0, INVALID);
    assert_eq!(name(hw.set_range(&ctx(), 1800, 300, true)).0, INVALID);
    assert!(t.writes().is_empty());
}

fn all(v: &str) -> BTreeMap<String, String> {
    BTreeMap::from([("all".to_string(), v.to_string())])
}

#[test]
fn epp() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_epp(&ctx(), &all("balance_power"), true).unwrap();
    assert_eq!(
        t.get("sys/devices/system/cpu/cpufreq/policy13/energy_performance_preference"),
        "balance_power"
    );
    t.clear_writes();
    hw.set_epp(&ctx(), &all("balance_power"), true).unwrap();
    assert!(t.writes().is_empty(), "nothing differs");
}

#[test]
fn epp_refusals() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    assert_eq!(name(hw.set_epp(&ctx(), &all("turbo"), true)).0, INVALID);
    assert_eq!(name(hw.set_epp(&ctx(), &BTreeMap::new(), true)).0, INVALID);
    let mixed = BTreeMap::from([
        ("all".to_string(), "power".to_string()),
        ("p".to_string(), "power".to_string()),
    ]);
    assert_eq!(name(hw.set_epp(&ctx(), &mixed, true)).0, INVALID);
    assert_eq!(
        name(hw.set_epp(&ctx(), &BTreeMap::from([("x".to_string(), "power".to_string())]), true)).0,
        INVALID
    );
    for i in 0..14 {
        t.poke(
            &format!("sys/devices/system/cpu/cpufreq/policy{i}/scaling_governor"),
            "performance",
        );
    }
    assert_eq!(
        name(hw.set_epp(&ctx(), &all("power"), true)),
        (BUSY, "governor".to_string())
    );
    assert!(t.writes().is_empty());
}

#[test]
fn epp_busy_on_one_policy() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    t.rules(serde_json::json!({"fail": {"policy3/energy_performance_preference": "EBUSY"}}));
    assert_eq!(name(hw.set_epp(&ctx(), &all("power"), true)).0, NOT_APPLIED);
    assert_eq!(
        t.get("sys/devices/system/cpu/cpufreq/policy4/energy_performance_preference"),
        "power"
    );
    assert_eq!(
        t.get("sys/devices/system/cpu/cpufreq/policy3/energy_performance_preference"),
        "balance_performance"
    );
}

#[test]
fn epp_per_core_type() {
    // test_epp_per_policy: the provider sets per core type
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_epp(
        &ctx(),
        &BTreeMap::from([
            ("p".to_string(), "performance".to_string()),
            ("lpe".to_string(), "power".to_string()),
        ]),
        true,
    )
    .unwrap();
    let epp = |i: u32| {
        t.get(&format!(
            "sys/devices/system/cpu/cpufreq/policy{i}/energy_performance_preference"
        ))
    };
    assert_eq!(
        (epp(0), epp(3), epp(4), epp(11), epp(12), epp(13)),
        (
            "performance".into(),
            "performance".into(),
            "balance_performance".into(),
            "balance_performance".into(),
            "power".into(),
            "power".into()
        )
    );
    assert_eq!(hw.epp_by_type().get("lpe").map(String::as_str), Some("power"));
}

#[test]
fn turbo_is_never_written() {
    // test_turbo: steamos-manager's CpuBoost1 owns intel_pstate/no_turbo; the provider never
    // writes it, and with no_turbo=1 a per-type cap reads back as the kernel clamps it
    let t = tree();
    t.oxp3(true);
    t.poke("sys/devices/system/cpu/intel_pstate/no_turbo", 1);
    let mut hw = make(&t);
    hw.set_cpu_limits(&ctx(), None, None, Some(BTreeMap::from([("p".into(), 4000)])), true)
        .unwrap();
    assert_eq!(
        t.get("sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq"),
        "2000000"
    );
    assert!(t.writes().iter().all(|(p, _, _)| !p.ends_with("no_turbo")));
}

#[test]
fn missing_pieces() {
    let t = tree();
    t.dmi("ONE-NETBOOK", "ONEXPLAYER 3");
    let mut hw = make(&t);
    assert_eq!(
        name(hw.set_range(&ctx(), 300, 600, true)),
        (NOT_SUPPORTED, "kernel".into())
    );
    assert_eq!(name(hw.set_epp(&ctx(), &all("power"), true)).0, NOT_SUPPORTED);
    assert_eq!(name(hw.set_fan(&ctx(), "max", true)).0, NOT_SUPPORTED);
    assert_eq!(name(hw.set_charge_limit(&ctx(), 80, true)).0, NOT_SUPPORTED);
    assert_eq!(name(hw.sample()).0, NOT_SUPPORTED);
    assert!(t.writes().is_empty());
}

// ---- SupplyTest ----------------------------------------------------------------------------

#[test]
fn ac_online() {
    let t = tree();
    let ac = |t: &Tree| {
        let sys = Sys::emulated(t.root.clone());
        let d = discover::discover(&sys);
        discover::ac_online(&sys, &d)
    };
    assert_eq!(ac(&t), None);
    t.battery("BAT0", "Charging", 50, false);
    assert_eq!(ac(&t), Some(true));
    t.poke("sys/class/power_supply/BAT0/status", "Discharging");
    assert_eq!(ac(&t), Some(false));
    t.ac(true, "ucsi-source-psy-USBC000:001", "USB");
    assert_eq!(ac(&t), Some(true));
    t.ac(false, "ADP1", "Mains");
    assert_eq!(ac(&t), Some(false), "Mains wins over USB");
    t.ac(true, "ADP1", "Mains");
    assert_eq!(ac(&t), Some(true));
}

// ---- WriterTest ----------------------------------------------------------------------------

#[test]
fn no_symlink_is_followed() {
    let t = tree();
    t.oxp3(true);
    let victim = t.put("victim", "keep", common::fake::Grant::Rw);
    let link = t.path(&t.limit("intel-rapl:0", "long_term"));
    std::fs::remove_file(&link).unwrap();
    std::os::unix::fs::symlink(&victim, &link).unwrap();
    let mut hw = make(&t);
    assert!(hw.set_tdp(&ctx(), 15, true).is_err());
    assert_eq!(t.get("victim"), "keep");
    assert!(t.writes().iter().any(|(_, _, r)| r == "ELOOP"));
}

// ---- OwnerReadingsTest ---------------------------------------------------------------------

#[test]
fn owner_readings_both_interfaces_written() {
    // the owner's OXP3 with SimpleDeckyTDP installed: MSR 25/52/160, MMIO 35/35/160, card0
    let t = tree();
    t.oxp3(true);
    std::fs::remove_dir_all(t.path("sys/class/drm/card1")).unwrap();
    t.set_watts("intel-rapl-mmio:0", "short_term", 35);
    t.xe(
        "card0",
        &[("gt0", Some("gt0-rc")), ("gt1", Some("gt1-mc"))],
        100,
        900,
        2300,
        900,
        2300,
        "xe",
    );
    let mut hw = make(&t);
    assert_eq!(hw.d.gpu.as_ref().unwrap().card, "card0");
    hw.set_boost(&ctx(), "manual", 45, 0, true).unwrap();
    hw.set_tdp(&ctx(), 30, true).unwrap();
    hw.set_range(&ctx(), 300, 2300, true).unwrap();
    for z in ["intel-rapl:0", "intel-rapl-mmio:0"] {
        assert_eq!(
            (
                t.watts(z, "long_term"),
                t.watts(z, "short_term"),
                t.watts(z, "peak_power")
            ),
            (30.0, 45.0, 160.0)
        );
    }
    assert!(t.writes().iter().all(|(p, _, _)| !p.contains("/gt1/")));
}

// ---- CoreTypesTest -------------------------------------------------------------------------

#[test]
fn p_e_and_lp_e_by_l3() {
    let t = tree();
    t.cpu(12, "balance_performance", "powersave", 0);
    t.types(
        "0-3",
        "4-11",
        &[8, 9, 10, 11],
        [5_100_000, 3_800_000, 2_500_000],
        [2_000_000, 1_800_000, 1_200_000],
    );
    let hw = make(&t);
    let c = hw.d.cpu.as_ref().unwrap();
    let names = |k: &str| c.of_type(k).map(|p| p.name.clone()).collect::<Vec<_>>();
    assert_eq!(names("p"), (0..4).map(|i| format!("policy{i}")).collect::<Vec<_>>());
    assert_eq!(names("e"), (4..8).map(|i| format!("policy{i}")).collect::<Vec<_>>());
    assert_eq!(names("lpe"), (8..12).map(|i| format!("policy{i}")).collect::<Vec<_>>());
    assert_eq!(c.type_range_mhz("p"), Some((400, 5100)));
    assert_eq!(c.type_range_mhz("e"), Some((400, 3800)));
    assert_eq!(c.type_range_mhz("lpe"), Some((400, 2500)));
    assert_eq!(hw.core_types(), vec!["p", "e", "lpe"]);
}

#[test]
fn not_hybrid_is_all_p_and_a_cpu_in_neither_list_is_p() {
    let t = tree();
    t.cpu(4, "balance_performance", "powersave", 0);
    let hw = make(&t);
    assert_eq!(hw.core_types(), vec!["p"]);
    t.put("sys/devices/cpu_core/cpus", "0-1", common::fake::Grant::No);
    t.put("sys/devices/cpu_atom/cpus", "2", common::fake::Grant::No);
    let hw = make(&t);
    let c = hw.d.cpu.as_ref().unwrap();
    assert_eq!(
        c.of_type("p").map(|p| p.name.as_str()).collect::<Vec<_>>(),
        vec!["policy0", "policy1", "policy3"]
    );
    assert_eq!(
        c.of_type("e").map(|p| p.name.as_str()).collect::<Vec<_>>(),
        vec!["policy2"]
    );
}

#[test]
fn cpulist() {
    assert_eq!(
        discover::cpulist("0-3,8,10-11").into_iter().collect::<Vec<_>>(),
        vec![0, 1, 2, 3, 8, 10, 11]
    );
    assert!(discover::cpulist("").is_empty());
    assert!(discover::cpulist("x").is_empty());
}

#[test]
fn perf_pct_needs_both_files() {
    let t = tree();
    t.cpu(2, "balance_performance", "powersave", 0);
    let hw = make(&t);
    assert_eq!(hw.reason("cpuLimits"), Reason::Kernel);
    t.perf(100, 9);
    let hw = make(&t);
    assert_eq!(hw.reason("cpuLimits"), Reason::Ok);
    assert_eq!(hw.perf_read(), (Some(100), Some(9)));
}

// ---- SocTest -------------------------------------------------------------------------------

#[test]
fn soc_found_by_name_among_two_devices() {
    let t = tree();
    t.soc("03", "00", "balanced", "asus-wmi", "platform-profile-0", false);
    let d = t.soc("03", "00", "balanced", "SoC Power Slider", "platform-profile-1", true);
    let hw = make(&t);
    assert_eq!(hw.d.soc.as_ref().unwrap().profile, t.path(&format!("{d}/profile")));
}

#[test]
fn soc_none_when_the_name_differs_or_a_param_is_missing() {
    let t = tree();
    t.soc("03", "00", "balanced", "SoC power slider", "platform-profile-0", true);
    assert!(make(&t).d.soc.is_none());
    let t = tree();
    t.soc("03", "00", "balanced", "SoC Power Slider", "platform-profile-0", true);
    t.remove("sys/module/processor_thermal_soc_slider/parameters/slider_offset");
    assert!(make(&t).d.soc.is_none());
}

#[test]
fn soc_hex_read_and_write() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    assert_eq!(hw.soc_state(), (3, 0, true));
    hw.set_soc(&ctx(), 4, 2, true).unwrap();
    let w = t.writes();
    assert_eq!(
        (w[0].0.as_str(), w[0].1.as_str()),
        ("sys/module/processor_thermal_soc_slider/parameters/slider_offset", "02")
    );
    assert_eq!(
        (w[1].0.as_str(), w[1].1.as_str()),
        (
            "sys/module/processor_thermal_soc_slider/parameters/slider_balance",
            "04"
        )
    );
    assert_eq!(w[2].1, "balanced");
    assert_eq!(
        t.get("sys/module/processor_thermal_soc_slider/parameters/slider_balance"),
        "04"
    );
}

#[test]
fn soc_not_balanced_writes_the_params_only() {
    let t = tree();
    t.oxp3(true);
    t.poke("sys/class/platform-profile/platform-profile-0/profile", "performance");
    let mut hw = make(&t);
    hw.set_soc(&ctx(), 4, 0, true).unwrap();
    assert_eq!(
        t.names(),
        vec!["slider_balance"],
        "offset already 00, never the profile"
    );
    assert_eq!(
        t.get("sys/class/platform-profile/platform-profile-0/profile"),
        "performance"
    );
    assert!(!hw.soc_state().2, "Active is false");
    // the kernel shows the old balance until a profile write; power-profiles-daemon's next
    // "balanced" picks the held value up
    assert_eq!(
        t.get("sys/module/processor_thermal_soc_slider/parameters/slider_balance"),
        "03"
    );
}

#[test]
fn soc_einval_and_bounds() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    for (b, o) in [(0, 0), (6, 0), (3, 4)] {
        assert_eq!(name(hw.set_soc(&ctx(), b, o, true)).0, INVALID, "{b} {o}");
    }
    assert!(t.writes().is_empty());
    t.rules(serde_json::json!({"fail": {"slider_balance": "EINVAL"}}));
    assert_eq!(name(hw.set_soc(&ctx(), 4, 0, true)).0, NOT_APPLIED);
}

#[test]
fn soc_never_another_profile_or_the_legacy_file() {
    let t = tree();
    t.oxp3(true);
    t.put(
        "sys/firmware/acpi/platform_profile",
        "balanced",
        common::fake::Grant::Rw,
    );
    let mut hw = make(&t);
    for b in [1, 5, 3] {
        hw.set_soc(&ctx(), b, 1, true).unwrap();
    }
    for (p, text, _) in t.writes() {
        assert!(!["performance", "low-power", "custom", "quiet"].contains(&text.as_str()));
        assert!(!p.ends_with("acpi/platform_profile"), "{p}");
    }
}

// ---- PerfTest ------------------------------------------------------------------------------

#[test]
fn raising_writes_the_cap_first_lowering_the_floor_first() {
    let t = tree();
    t.oxp3(true);
    t.poke("sys/devices/system/cpu/intel_pstate/max_perf_pct", 60);
    t.poke("sys/class/power_supply/ADP1/online", 1); // a floor above the boot value needs AC
    let mut hw = make(&t);
    hw.set_cpu_limits(&ctx(), Some(100), Some(40), None, true).unwrap();
    assert_eq!(&t.names()[..2], &["max_perf_pct", "min_perf_pct"]);
    t.clear_writes();
    hw.set_cpu_limits(&ctx(), Some(50), Some(9), None, true).unwrap();
    assert_eq!(&t.names()[..2], &["min_perf_pct", "max_perf_pct"]);
    assert_eq!(hw.perf_read(), (Some(50), Some(9)));
}

#[test]
fn floor_above_cap_is_refused() {
    // OXP3 Deck lowered the floor to the cap; the provider never clamps
    let t = tree();
    t.oxp3(true);
    t.poke("sys/class/power_supply/ADP1/online", 1);
    let mut hw = make(&t);
    assert_eq!(
        name(hw.set_cpu_limits(&ctx(), Some(40), Some(45), None, true)).0,
        INVALID
    );
    assert!(t.writes().is_empty());
    hw.set_cpu_limits(&ctx(), Some(70), Some(40), None, true).unwrap();
    t.clear_writes();
    hw.set_cpu_limits(&ctx(), Some(70), Some(40), None, true).unwrap();
    assert!(
        t.names().iter().all(|n| n.ends_with("/cap")),
        "equal values not written: {:?}",
        t.names()
    );
}

#[test]
fn floor_above_the_boot_value_needs_ac() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    assert_eq!(
        name(hw.set_cpu_limits(&ctx(), None, Some(30), None, true)),
        (BUSY, "battery".into())
    );
    assert!(t.writes().is_empty());
    t.poke("sys/class/power_supply/ADP1/online", 1);
    let mut hw = make(&t);
    hw.set_cpu_limits(&ctx(), None, Some(30), None, true).unwrap();
    assert_eq!(hw.perf_read().1, Some(30));
    assert_eq!(name(hw.set_cpu_limits(&ctx(), None, Some(51), None, true)).0, INVALID);
}

#[test]
fn never_zero() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    assert_eq!(name(hw.set_cpu_limits(&ctx(), Some(0), None, None, true)).0, INVALID);
    assert_eq!(name(hw.set_cpu_limits(&ctx(), Some(29), None, None, true)).0, INVALID);
    assert_eq!(name(hw.set_cpu_limits(&ctx(), None, Some(0), None, true)).0, INVALID);
    assert_eq!(name(hw.set_cpu_limits(&ctx(), Some(101), None, None, true)).0, INVALID);
    assert_eq!(name(hw.set_cpu_limits(&ctx(), None, None, None, true)).0, INVALID);
    assert!(t.writes().is_empty());
}

#[test]
fn caps_clamped_by_the_kernel_and_read_back_within_tolerance() {
    let t = tree();
    t.oxp3(true);
    t.poke("sys/devices/system/cpu/cpufreq/policy0/scaling_min_freq", 800_000);
    let mut hw = make(&t);
    // 500 MHz is inside the type's range; the kernel clamps policy0 to its scaling_min_freq
    hw.set_cpu_limits(&ctx(), None, None, Some(BTreeMap::from([("p".into(), 500)])), true)
        .unwrap();
    assert_eq!(
        t.get("sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq"),
        "800000"
    );
    assert_eq!(
        t.get("sys/devices/system/cpu/cpufreq/policy1/scaling_max_freq"),
        "500000"
    );
    assert_eq!(
        name(hw.set_cpu_limits(&ctx(), None, None, Some(BTreeMap::from([("p".into(), 6000)])), true)).0,
        INVALID
    );
    assert_eq!(
        name(hw.set_cpu_limits(&ctx(), None, None, Some(BTreeMap::from([("x".into(), 1000)])), true)).0,
        INVALID
    );
    hw.set_cpu_limits(&ctx(), None, None, Some(BTreeMap::from([("lpe".into(), 2000)])), true)
        .unwrap();
    t.poke("sys/devices/system/cpu/cpufreq/policy12/scaling_max_freq", 2_050_000); // HWP rounding
    t.clear_writes();
    hw.set_cpu_limits(&ctx(), None, None, Some(BTreeMap::from([("lpe".into(), 2000)])), true)
        .unwrap();
    assert!(
        t.writes().iter().all(|(p, _, _)| !p.contains("policy12/")),
        "within 100 MHz: not written again"
    );
}

#[test]
fn cap_einval_on_one_policy() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    t.rules(serde_json::json!({"fail": {"policy4/scaling_max_freq": "EINVAL"}}));
    assert_eq!(
        name(hw.set_cpu_limits(&ctx(), None, None, Some(BTreeMap::from([("e".into(), 2000)])), true)).0,
        NOT_APPLIED
    );
    assert_eq!(
        t.get("sys/devices/system/cpu/cpufreq/policy5/scaling_max_freq"),
        "2000000"
    );
}

#[test]
fn scaling_min_freq_is_never_written() {
    let t = tree();
    t.oxp3(true);
    t.poke("sys/class/power_supply/ADP1/online", 1);
    let mut hw = make(&t);
    hw.set_cpu_limits(
        &ctx(),
        Some(40),
        Some(20),
        Some(BTreeMap::from([("p".into(), 1500), ("e".into(), 1500)])),
        true,
    )
    .unwrap();
    hw.set_epp(
        &ctx(),
        &BTreeMap::from([("p".into(), "power".into()), ("e".into(), "performance".into())]),
        true,
    )
    .unwrap();
    assert!(t.writes().iter().all(|(p, _, _)| !p.ends_with("scaling_min_freq")));
}

// ---- GpuProfileTest ------------------------------------------------------------------------

#[test]
fn gpu_profile_parsing() {
    let t = tree();
    t.oxp3(true);
    let hw = make(&t);
    let f = "sys/class/drm/card1/device/tile0/gt0/freq0/power_profile";
    for (text, want) in [
        ("[base]    power_saving", Some("base")),
        ("base    [power_saving]", Some("power_saving")),
        ("invalid", None),
    ] {
        t.poke(f, text);
        assert_eq!(hw.gpu_profile().as_deref(), want);
    }
}

#[test]
fn gpu_profile_written_and_read_back() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_gpu_profile(&ctx(), "power_saving", true).unwrap();
    assert_eq!(
        t.get("sys/class/drm/card1/device/tile0/gt0/freq0/power_profile"),
        "base    [power_saving]"
    );
    t.clear_writes();
    hw.set_gpu_profile(&ctx(), "power_saving", true).unwrap();
    assert!(t.writes().is_empty(), "the write wakes the GPU: only when it differs");
    t.rules(serde_json::json!({"ignore": ["power_profile"]}));
    assert_eq!(
        name(hw.set_gpu_profile(&ctx(), "base", true)),
        (NOT_APPLIED, "gpuPowerProfile=power_saving".into())
    );
    assert_eq!(name(hw.set_gpu_profile(&ctx(), "turbo", true)).0, INVALID);
}

#[test]
fn gpu_profile_eio_and_eagain() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    t.rules(serde_json::json!({"fail": {"power_profile": "EIO"}}));
    assert_eq!(name(hw.set_gpu_profile(&ctx(), "power_saving", true)).0, NOT_APPLIED);
    t.rules(serde_json::json!({"fail": {"power_profile": ["EAGAIN", "EAGAIN", "EAGAIN", "EAGAIN"]}}));
    t.clear_writes();
    assert_eq!(name(hw.set_gpu_profile(&ctx(), "power_saving", true)).0, NOT_APPLIED);
    assert_eq!(t.writes().len(), 4, "1 + 3 tries");
}

#[test]
fn gpu_profile_media_gt_and_clock_files_untouched() {
    let t = tree();
    t.oxp3(true);
    t.gpu_profile("base", "card1", "gt1");
    let mut hw = make(&t);
    hw.set_gpu_profile(&ctx(), "power_saving", true).unwrap();
    assert_eq!(t.writes().len(), 1);
    assert!(t.writes()[0].0.contains("/gt0/freq0/power_profile"));
}

// ---- TimeWindowsBatteryTest ----------------------------------------------------------------

#[test]
fn time_windows_are_never_written() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_tdp(&ctx(), 15, true).unwrap();
    hw.set_boost(&ctx(), "manual", 25, 0, true).unwrap();
    assert!(!t.writes().is_empty());
    assert!(t.writes().iter().all(|(p, _, _)| !p.contains("time_window")));
}

#[test]
fn a_zone_without_boost() {
    let t = tree();
    t.rapl("intel-rapl:0", 25, 52, 160, &["long_term"], 25, "package-0");
    t.rapl(
        "intel-rapl-mmio:0",
        35,
        52,
        160,
        &["long_term", "short_term"],
        25,
        "package-0",
    );
    let hw = make(&t);
    assert_eq!(hw.d.zones.len(), 1, "a zone without short_term cannot keep PL2 >= PL1");
    assert_eq!(hw.reason("tdp"), Reason::Kernel);
}

#[test]
fn battery_controls_need_linux_7_4() {
    let t = tree();
    t.oxp3(false);
    let hw = make(&t);
    assert_eq!(
        (hw.reason("chargeLimit"), hw.reason("chargeBehaviour"), hw.reason("fan")),
        (Reason::Kernel, Reason::Kernel, Reason::Kernel)
    );
    let t = tree();
    t.oxp3(true);
    let hw = make(&t);
    assert_eq!(
        (hw.reason("chargeLimit"), hw.reason("chargeBehaviour"), hw.reason("fan")),
        (Reason::Ok, Reason::Ok, Reason::Ok)
    );
}

// ---- provider rules beyond OXP3 Deck ------------------------------------------------------

#[test]
fn tdp_bounds_are_refused_not_clamped() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    for w in [0, 4, 36, 60, 5000, u32::MAX] {
        assert_eq!(name(hw.set_tdp(&ctx(), w, true)).0, INVALID, "{w}");
    }
    assert!(t.writes().is_empty());
    hw.set_tdp(&ctx(), 5, true).unwrap();
    hw.set_tdp(&ctx(), 35, true).unwrap();
}

#[test]
fn the_emulated_kernel_truncates_rapl_limits() {
    // powercap has no range check: the value is cut to the power unit and the 15-bit field, which
    // is why the provider bounds every value and reads every write back
    let t = tree();
    t.oxp3(true);
    let sys = Sys::emulated(t.root.clone());
    let p = t.path(&t.limit("intel-rapl:0", "long_term"));
    sys.write(&p, &(5000 * MW).to_string()).unwrap();
    assert_eq!(t.get(&t.limit("intel-rapl:0", "long_term")), "904000000");
    sys.write(&p, "100000").unwrap();
    assert_eq!(
        t.get(&t.limit("intel-rapl:0", "long_term")),
        "0",
        "below one unit is 0 W (a hang)"
    );
    sys.write(&p, "15100000").unwrap();
    assert_eq!(t.get(&t.limit("intel-rapl:0", "long_term")), "15000000");
    assert_eq!(sys.write(&p, "x").unwrap_err().raw_os_error(), Some(libc::EINVAL));
}

#[test]
fn admin_ac_limit_is_capped_on_battery() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make_with(&t, "sustained_ac_max_w = 40\n");
    assert_eq!(hw.tdp_bounds(), (5, 40));
    hw.set_tdp(&ctx(), 40, true).unwrap();
    assert_eq!(
        t.watts("intel-rapl:0", "long_term"),
        35.0,
        "on battery the battery maximum applies"
    );
    assert!(changed(&mut hw).contains(&("tdp".into(), "capped-battery".into())));
    assert_eq!(hw.tdp_value(), 40, "the target is held");
    t.plug(true);
    let t0 = Instant::now();
    for i in 0..40 {
        hw.tick(t0 + Duration::from_millis(10 * i));
    }
    assert_eq!(t.watts("intel-rapl:0", "long_term"), 40.0);
    assert_eq!(t.watts("intel-rapl-mmio:0", "long_term"), 40.0);
    let c = changed(&mut hw);
    assert!(c.contains(&("tdp".into(), "reasserted-ac".into())), "{c:?}");
}

#[test]
fn boost_modes() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_tdp(&ctx(), 20, true).unwrap();
    assert_eq!(hw.set_boost(&ctx(), "follow", 0, 10, true).unwrap(), 30);
    assert_eq!(t.watts("intel-rapl:0", "short_term"), 30.0);
    hw.set_tdp(&ctx(), 30, true).unwrap();
    assert_eq!(
        t.watts("intel-rapl:0", "short_term"),
        40.0,
        "follow recomputes on every PL1 change"
    );
    hw.set_tdp(&ctx(), 35, true).unwrap();
    assert_eq!(t.watts("intel-rapl:0", "short_term"), 45.0);
    assert_eq!(
        hw.set_boost(&ctx(), "follow", 0, 47, true).unwrap(),
        52,
        "capped by BoostLimitMax"
    );
    assert_eq!(
        hw.set_boost(&ctx(), "manual", 20, 0, true).unwrap(),
        35,
        "never below PL1"
    );
    assert_eq!(hw.set_boost(&ctx(), "firmware", 0, 0, true).unwrap(), 52);
    assert_eq!(hw.lease_of("boost"), None, "firmware is the default: the lease ends");
    for (m, l, o) in [
        ("firmware", 52, 0),
        ("firmware", 0, 1),
        ("follow", 5, 0),
        ("follow", 0, 48),
        ("manual", 53, 0),
        ("manual", 4, 0),
        ("manual", 40, 1),
        ("turbo", 0, 0),
    ] {
        assert_eq!(name(hw.set_boost(&ctx(), m, l, o, true)).0, INVALID, "{m} {l} {o}");
    }
}

#[test]
fn leases_release_and_disconnect() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_boost(&ctx(), "manual", 40, 0, true).unwrap();
    assert_eq!(hw.lease_of("boost").as_deref(), Some(":1.42"));
    assert_eq!(
        name(hw.set_boost(&other(), "manual", 30, 0, true)),
        (BUSY, "lease::1.42".into())
    );
    assert_eq!(
        name(hw.take_control(&other(), &["boost".into()])),
        (BUSY, "lease::1.42".into())
    );
    // Release by someone else does nothing; by the holder writes the default
    hw.release(&other(), &[]).unwrap();
    assert_eq!(t.watts("intel-rapl:0", "short_term"), 40.0);
    hw.release(&ctx(), &["boost".into()]).unwrap();
    assert_eq!(t.watts("intel-rapl:0", "short_term"), 52.0);
    assert!(changed(&mut hw).contains(&("boost".into(), "released".into())));
    assert_eq!(
        name(hw.release(&ctx(), &["tdp".into()])).0,
        INVALID,
        "tdp is not leased"
    );
    // a vanished holder: back to the defaults
    hw.set_range(&other(), 300, 600, true).unwrap();
    hw.set_soc(&other(), 5, 1, true).unwrap();
    hw.disconnected(":1.43");
    let c = changed(&mut hw);
    assert!(c.contains(&("gpuRange".into(), "reverted-disconnect".into())));
    assert!(c.contains(&("socBalance".into(), "reverted-disconnect".into())));
    assert_eq!(
        (
            t.get("sys/class/drm/card1/device/tile0/gt0/freq0/min_freq"),
            t.get("sys/class/drm/card1/device/tile0/gt0/freq0/max_freq")
        ),
        ("100".into(), "2300".into())
    );
    assert_eq!(
        t.get("sys/module/processor_thermal_soc_slider/parameters/slider_offset"),
        "00"
    );
}

#[test]
fn fan_modes_and_guards() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    let fan = "sys/class/hwmon/hwmon5";
    // never non-auto without the sleep inhibitor
    assert_eq!(name(hw.set_fan(&ctx(), "quiet", true)).0, NOT_APPLIED);
    hw.fan_inhibitor = Some(inhibitor());
    hw.set_fan(&ctx(), "quiet", true).unwrap();
    assert_eq!(
        (t.get(&format!("{fan}/pwm1_enable")), t.get(&format!("{fan}/pwm1"))),
        ("1".into(), "101".into()),
        "102 through the EC's 184 steps"
    );
    assert_eq!(hw.fan_state().0, "quiet");
    // 90 C: back to auto, the inhibitor released, quiet refused during the cooldown
    t.set_temp(90_000);
    assert!(hw.fan_guard());
    assert_eq!(t.get(&format!("{fan}/pwm1_enable")), "2");
    assert!(hw.fan_inhibitor.is_none());
    assert_eq!(hw.fan_state(), ("auto".into(), "temperature".into()));
    assert!(changed(&mut hw).contains(&("fan".into(), "reverted-temperature".into())));
    hw.fan_inhibitor = Some(inhibitor());
    let (n, m) = name(hw.set_fan(&ctx(), "quiet", true));
    assert_eq!(n, BUSY);
    assert!(
        m.starts_with("cooldown:") && m[9..].parse::<u64>().unwrap() > 590,
        "{m}"
    );
    hw.set_fan(&ctx(), "max", true).unwrap();
    assert_eq!(t.get(&format!("{fan}/pwm1_enable")), "0");
    // an unreadable sensor reverts too
    t.set_temp(50_000);
    t.remove("sys/class/hwmon/hwmon3/temp1_input");
    assert!(hw.fan_guard());
    assert_eq!(hw.fan_state(), ("auto".into(), "sensor".into()));
    for m in ["turbo", "Auto", ""] {
        assert_eq!(name(hw.set_fan(&ctx(), m, true)).0, INVALID);
    }
}

#[test]
fn fan_write_failure_goes_back_to_auto() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.fan_inhibitor = Some(inhibitor());
    t.rules(serde_json::json!({"fail": {"hwmon5/pwm1": "EIO"}}));
    assert_eq!(name(hw.set_fan(&ctx(), "quiet", true)).0, NOT_APPLIED);
    assert_eq!(t.get("sys/class/hwmon/hwmon5/pwm1_enable"), "2");
    assert!(hw.fan_inhibitor.is_none() && hw.lease_of("fan").is_none());
}

#[test]
fn charge_rules() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    let th = "sys/class/power_supply/BAT0/charge_control_end_threshold";
    for v in [-2, 0, 19, 49, 101, i32::MIN, i32::MAX] {
        assert_eq!(name(hw.set_charge_limit(&ctx(), v, true)).0, INVALID, "{v}");
    }
    assert!(t.writes().is_empty());
    hw.set_charge_limit(&ctx(), 80, true).unwrap();
    assert_eq!(t.get(th), "80");
    assert_eq!(t.get("var/lib/oxp3-hw/persist.json"), r#"{"charge_limit":80}"#);
    hw.set_charge_limit(&ctx(), -1, true).unwrap();
    assert_eq!(t.get(th), "100", "-1 means no limit: 100, never 0");
    hw.set_charge_limit(&ctx(), 60, true).unwrap();
    // persisted: a restart re-applies it
    t.poke(th, 100);
    let mut hw = make(&t);
    hw.apply_persisted();
    assert_eq!(t.get(th), "60");
    // charge behaviour: auto and inhibit-charge-awake only, leased
    let be = "sys/class/power_supply/BAT0/charge_behaviour";
    assert_eq!(name(hw.set_behaviour(&ctx(), "inhibit-charge", true)).0, INVALID);
    assert_eq!(name(hw.set_behaviour(&ctx(), "force-discharge", true)).0, INVALID);
    hw.set_behaviour(&ctx(), "inhibit-charge-awake", true).unwrap();
    assert_eq!(t.get(be), "auto inhibit-charge [inhibit-charge-awake]");
    assert_eq!(name(hw.set_behaviour(&other(), "auto", true)).0, BUSY);
    hw.disconnected(":1.42");
    assert_eq!(t.get(be), "[auto] inhibit-charge inhibit-charge-awake");
    assert!(t
        .writes()
        .iter()
        .all(|(_, text, _)| text != "inhibit-charge" && text != "force-discharge"));
}

#[test]
fn drift_stops_after_two_overrides() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_tdp(&ctx(), 15, true).unwrap();
    changed(&mut hw);
    let t0 = Instant::now() + Duration::from_secs(1);
    // another writer sets MSR PL1 to 25 W: two mismatches in a row, then overridden
    t.set_watts("intel-rapl:0", "long_term", 25);
    hw.tick(t0);
    hw.tick(t0 + Duration::from_millis(30));
    assert!(changed(&mut hw).contains(&("tdp".into(), "overridden".into())));
    t.clear_writes();
    for i in 2..30 {
        hw.tick(t0 + Duration::from_millis(30 * i));
    }
    assert!(t.writes().is_empty(), "never fights: {:?}", t.writes());
    // TakeControl writes the held value once and clears it
    hw.take_control(&ctx(), &["tdp".into()]).unwrap();
    assert_eq!(t.watts("intel-rapl:0", "long_term"), 15.0);
}

#[test]
fn ac_change_reasserts_inside_the_grace_window() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_tdp(&ctx(), 20, true).unwrap();
    changed(&mut hw);
    let t0 = Instant::now() + Duration::from_secs(1);
    hw.tick(t0);
    t.plug(true); // firmware rewrites MMIO to 35/52 W
    for i in 1..40 {
        hw.tick(t0 + Duration::from_millis(10 * i));
    }
    assert_eq!(t.watts("intel-rapl-mmio:0", "long_term"), 20.0);
    let c = changed(&mut hw);
    assert!(c.contains(&("tdp".into(), "reasserted-ac".into())), "{c:?}");
    assert!(!c.contains(&("tdp".into(), "overridden".into())));
}

#[test]
fn resume_reasserts() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_tdp(&ctx(), 20, true).unwrap();
    hw.set_epp(&ctx(), &all("power"), true).unwrap();
    changed(&mut hw);
    let t0 = Instant::now() + Duration::from_secs(1);
    hw.tick(t0);
    // after resume the firmware's values are back
    t.set_watts("intel-rapl:0", "long_term", 25);
    t.poke(
        "sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference",
        "balance_performance",
    );
    hw.resumed(t0);
    for i in 1..40 {
        hw.tick(t0 + Duration::from_millis(10 * i));
    }
    assert_eq!(t.watts("intel-rapl:0", "long_term"), 20.0);
    assert_eq!(
        t.get("sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference"),
        "power"
    );
    let c = changed(&mut hw);
    assert!(c.contains(&("tdp".into(), "reasserted-resume".into())), "{c:?}");
    assert!(c.contains(&("epp".into(), "reasserted-resume".into())), "{c:?}");
}

#[test]
fn ppd_makes_epp_busy() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_epp(&ctx(), &all("power"), true).unwrap();
    hw.cow.ppd = Some("power-profiles-daemon");
    hw.refresh_avail();
    assert_eq!(hw.reason("epp"), Reason::Busy("power-profiles-daemon"));
    assert_eq!(hw.lease_of("epp"), None, "the co-writer owns it now");
    assert_eq!(
        name(hw.set_epp(&ctx(), &all("performance"), true)),
        (BUSY, "power-profiles-daemon".into())
    );
    hw.cow = Default::default();
    hw.cow.powerstation = true;
    hw.refresh_avail();
    assert_eq!(name(hw.set_tdp(&ctx(), 20, true)), (BUSY, "powerstation".into()));
    assert_eq!(
        name(hw.set_range(&ctx(), 300, 600, true)),
        (BUSY, "powerstation".into())
    );
    assert_eq!(
        hw.reason("fan"),
        Reason::Ok,
        "the fan is not one of PowerStation's knobs"
    );
}

#[test]
fn describe_keys() {
    let t = tree();
    t.oxp3(true);
    let hw = make(&t);
    let d = hw.describe("tdp").unwrap();
    for k in [
        "available",
        "reason",
        "unit",
        "min",
        "max",
        "value",
        "effective",
        "overridden",
        "source",
        "persistence",
    ] {
        assert!(d.contains_key(k), "tdp: {k}");
    }
    let d = hw.describe("boost").unwrap();
    for k in ["default", "lease", "values", "effective"] {
        assert!(d.contains_key(k), "boost: {k}");
    }
    assert_eq!(u32::try_from(d["effective"].try_clone().unwrap()).unwrap(), 52);
    assert_eq!(name(hw.describe("governor")).0, INVALID);
    for k in oxp3_hw::hw::KNOBS {
        let d = hw.describe(k).unwrap();
        assert!(bool::try_from(d["available"].try_clone().unwrap()).unwrap(), "{k}");
    }
}

#[test]
fn package_power_sample() {
    let t = tree();
    t.oxp3(true);
    t.poke("sys/class/powercap/intel-rapl:0/energy_uj", 1_000_000);
    let mut hw = make(&t);
    let start = Instant::now();
    std::thread::sleep(Duration::from_millis(1000));
    t.poke("sys/class/powercap/intel-rapl:0/energy_uj", 1_000_000 + 12_340_000);
    let (mw, win) = hw.sample().unwrap();
    let secs = start.elapsed().as_secs_f64();
    assert!(win >= 1000, "{win}");
    assert_eq!(mw % 100, 0, "100 mW steps");
    let expect = 12_340.0 / secs;
    assert!((mw as f64 - expect).abs() <= 1500.0, "{mw} vs {expect}");
    assert_eq!(
        hw.sample().unwrap(),
        (mw, win),
        "one fresh sample per second across callers"
    );
    // the counter wraps at max_energy_range_uj
    t.poke(
        "sys/class/powercap/intel-rapl:0/energy_uj",
        262_143_328_850u64 - 2_000_000,
    );
    let mut hw = make(&t);
    std::thread::sleep(Duration::from_millis(1000));
    t.poke("sys/class/powercap/intel-rapl:0/energy_uj", 3_000_000);
    let (mw, _) = hw.sample().unwrap();
    assert!(
        (3_000..=5_200).contains(&mw),
        "wrapped delta of 5 J over about 1 s: {mw}"
    );
}

#[test]
fn snapshot_is_kept_per_boot() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.set_epp(&ctx(), &all("power"), true).unwrap();
    // a restart in the same boot keeps the first snapshot, not the provider's own value
    let mut hw = make(&t);
    hw.release(&ctx(), &[]).unwrap();
    hw.disconnected(":1.42");
    hw.set_epp(&ctx(), &all("performance"), true).unwrap();
    hw.release(&ctx(), &["epp".into()]).unwrap();
    assert_eq!(
        t.get("sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference"),
        "balance_performance"
    );
}

#[test]
fn stop_restores_the_safe_state() {
    let t = tree();
    t.oxp3(true);
    let mut hw = make(&t);
    hw.fan_inhibitor = Some(inhibitor());
    hw.set_fan(&ctx(), "max", true).unwrap();
    hw.set_behaviour(&ctx(), "inhibit-charge-awake", true).unwrap();
    hw.set_boost(&ctx(), "manual", 30, 0, true).unwrap();
    hw.stop();
    assert_eq!(t.get("sys/class/hwmon/hwmon5/pwm1_enable"), "2");
    assert_eq!(
        t.get("sys/class/power_supply/BAT0/charge_behaviour"),
        "[auto] inhibit-charge inhibit-charge-awake"
    );
    assert_eq!(t.watts("intel-rapl:0", "short_term"), 52.0);
    let c = changed(&mut hw);
    assert!(c.contains(&("fan".into(), "reverted-stop".into())), "{c:?}");
    assert!(c.contains(&("chargeBehaviour".into(), "reverted-stop".into())), "{c:?}");
}
