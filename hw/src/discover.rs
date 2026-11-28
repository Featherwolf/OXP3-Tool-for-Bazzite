// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! Discovery: every attribute the provider may touch, resolved once by name (never by index) under
//! /sys/class, /sys/devices and /sys/module. Ported from OXP3 Deck's powerhw.discover and
//! extras gates.
//!
//! - RAPL: the top-level `intel-rapl-mmio:N` and `intel-rapl:N` zones named `package-0`
//!   (lowest N of each interface), constraints by `constraint_N_name` (`long_term` = PL1,
//!   `short_term` = PL2). `peak_power` (PL4), time windows and `enabled` are never resolved.
//! - xe: the first `cardN` whose device vendor is 0x8086 and driver is `xe`; the render GT is the
//!   one whose `gtidle/name` ends in `-rc` (else the lowest that does not end in `-mc`).
//! - CPU: cpufreq `policyN` by number; core types from `cpu_core/cpus`, `cpu_atom/cpus` and
//!   `cache/index3` (LP-E cores have no L3).
//! - SoC slider: both module parameters and the platform-profile device named "SoC Power Slider".
//! - oxpec (Linux 7.4+): hwmon named `oxp_ec`; the first power_supply of type Battery with
//!   `charge_control_end_threshold` / `charge_behaviour`.
//! - coretemp "Package id 0" for the fan guard; power_supply `online` of Mains/USB for AC.

use std::collections::BTreeMap;
use std::path::PathBuf;

use crate::sysfs::Sys;

pub const POWERCAP: &str = "/sys/class/powercap";
pub const DRM: &str = "/sys/class/drm";
pub const CPUFREQ: &str = "/sys/devices/system/cpu/cpufreq";
pub const CPU_SYS: &str = "/sys/devices/system/cpu";
pub const PSTATE: &str = "/sys/devices/system/cpu/intel_pstate";
pub const CPU_DEVICES: &str = "/sys/devices";
pub const SOC_PARAMS: &str = "/sys/module/processor_thermal_soc_slider/parameters";
pub const PLATFORM_PROFILE: &str = "/sys/class/platform-profile";
pub const SOC_PROFILE_NAME: &str = "SoC Power Slider";
pub const HWMON: &str = "/sys/class/hwmon";
pub const POWER_SUPPLY: &str = "/sys/class/power_supply";
pub const DMI: &str = "/sys/class/dmi/id";
pub const BOOT_ID: &str = "/proc/sys/kernel/random/boot_id";

pub const CORE_TYPES: [&str; 3] = ["p", "e", "lpe"];

#[derive(Debug, Clone)]
pub struct Zone {
    pub id: String,
    pub mmio: bool,
    pub pl1: PathBuf,
    pub pl2: PathBuf,
}

#[derive(Debug, Clone)]
pub struct Gpu {
    pub card: String,
    pub rpn: u32,
    pub rp0: u32,
    pub min: PathBuf,
    pub max: PathBuf,
    pub profile: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct Policy {
    pub name: String,
    pub dir: PathBuf,
    pub kind: &'static str,
    /// cpuinfo_min_freq and cpuinfo_max_freq in kHz
    pub min_khz: u64,
    pub max_khz: u64,
    pub base_khz: Option<u64>,
}

impl Policy {
    pub fn epp(&self) -> PathBuf {
        self.dir.join("energy_performance_preference")
    }
    pub fn scaling_max(&self) -> PathBuf {
        self.dir.join("scaling_max_freq")
    }
    pub fn governor(&self) -> PathBuf {
        self.dir.join("scaling_governor")
    }
}

#[derive(Debug, Clone)]
pub struct Cpu {
    pub policies: Vec<Policy>,
    /// The core types present, in the order p, e, lpe.
    pub types: Vec<&'static str>,
    pub epp: bool,
    pub epp_available: Vec<String>,
    pub max_perf: Option<PathBuf>,
    pub min_perf: Option<PathBuf>,
    pub no_turbo: Option<PathBuf>,
}

impl Cpu {
    pub fn of_type<'a>(&'a self, kind: &'a str) -> impl Iterator<Item = &'a Policy> + 'a {
        self.policies.iter().filter(move |p| p.kind == kind)
    }
    /// (cpuinfo_min, cpuinfo_max) over the type's policies, in MHz.
    pub fn type_range_mhz(&self, kind: &str) -> Option<(u32, u32)> {
        let lo = self.of_type(kind).map(|p| p.min_khz).min()?;
        let hi = self.of_type(kind).map(|p| p.max_khz).max()?;
        Some(((lo / 1000) as u32, (hi / 1000) as u32))
    }
}

#[derive(Debug, Clone)]
pub struct Soc {
    pub balance: PathBuf,
    pub offset: PathBuf,
    pub profile: PathBuf,
}

#[derive(Debug, Clone)]
pub struct Fan {
    pub enable: PathBuf,
    pub pwm: PathBuf,
}

#[derive(Debug, Clone)]
pub struct Battery {
    pub name: String,
    pub threshold: Option<PathBuf>,
    pub behaviour: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct Energy {
    pub energy_uj: PathBuf,
    pub max_range_uj: PathBuf,
}

#[derive(Debug, Clone, Default)]
pub struct Discovery {
    /// MMIO first, then MSR (the write order).
    pub zones: Vec<Zone>,
    pub gpu: Option<Gpu>,
    pub cpu: Option<Cpu>,
    pub soc: Option<Soc>,
    pub fan: Option<Fan>,
    pub temp: Option<PathBuf>,
    pub battery: Option<Battery>,
    pub energy: Option<Energy>,
    /// `online` files of Mains supplies, else USB supplies.
    pub ac_online: Vec<PathBuf>,
    /// `status` of Battery supplies (the AC fallback).
    pub battery_status: Vec<PathBuf>,
}

fn num_suffix(name: &str, prefix: &str) -> Option<u32> {
    name.strip_prefix(prefix)?.parse().ok()
}

pub fn discover(sys: &Sys) -> Discovery {
    let mut d = Discovery {
        zones: rapl(sys),
        gpu: gpu(sys),
        cpu: cpu(sys),
        soc: soc(sys),
        ..Default::default()
    };
    let (fan, temp) = hwmon(sys);
    d.fan = fan;
    d.temp = temp;
    let (battery, online, status) = supplies(sys);
    d.battery = battery;
    d.ac_online = online;
    d.battery_status = status;
    d.energy = energy(sys);
    d
}

/// MMIO first, then MSR: firmware rewrites MMIO, and both end equal.
fn rapl(sys: &Sys) -> Vec<Zone> {
    let base = sys.p(POWERCAP);
    let mut out = Vec::new();
    for (prefix, mmio) in [("intel-rapl-mmio:", true), ("intel-rapl:", false)] {
        let mut ids: Vec<(u32, String)> = sys
            .entries(&base)
            .into_iter()
            .filter_map(|n| num_suffix(&n, prefix).map(|k| (k, n)))
            .collect();
        ids.sort();
        for (_, id) in ids {
            let dir = base.join(&id);
            if sys.read(&dir.join("name")).as_deref() != Some("package-0") {
                continue;
            }
            let mut pl1 = None;
            let mut pl2 = None;
            for n in 0..6 {
                match sys.read(&dir.join(format!("constraint_{n}_name"))).as_deref() {
                    Some("long_term") if pl1.is_none() => {
                        pl1 = Some(dir.join(format!("constraint_{n}_power_limit_uw")))
                    }
                    Some("short_term") if pl2.is_none() => {
                        pl2 = Some(dir.join(format!("constraint_{n}_power_limit_uw")))
                    }
                    _ => {}
                }
            }
            if let (Some(pl1), Some(pl2)) = (pl1, pl2) {
                out.push(Zone {
                    id: id.clone(),
                    mmio,
                    pl1,
                    pl2,
                });
                break; // the lowest-numbered package-0 zone of this interface
            }
        }
    }
    out
}

fn gpu(sys: &Sys) -> Option<Gpu> {
    let base = sys.p(DRM);
    let mut cards: Vec<(u32, String)> = sys
        .entries(&base)
        .into_iter()
        .filter_map(|n| num_suffix(&n, "card").map(|k| (k, n)))
        .collect();
    cards.sort();
    for (_, card) in cards {
        let dev = base.join(&card).join("device");
        if sys.read(&dev.join("vendor")).map(|v| v.to_ascii_lowercase()).as_deref() != Some("0x8086") {
            continue;
        }
        if sys.link_name(&dev.join("driver")).as_deref() != Some("xe") {
            continue;
        }
        let tile = dev.join("tile0");
        let mut gts: Vec<(u32, String)> = sys
            .entries(&tile)
            .into_iter()
            .filter_map(|n| num_suffix(&n, "gt").map(|k| (k, n)))
            .filter(|(_, n)| sys.is_dir(&tile.join(n).join("freq0")))
            .collect();
        gts.sort();
        let name = |g: &str| sys.read(&tile.join(g).join("gtidle/name")).unwrap_or_default();
        let render = gts
            .iter()
            .find(|(_, g)| name(g).ends_with("-rc"))
            .or_else(|| gts.iter().find(|(_, g)| !name(g).ends_with("-mc")))?;
        let freq0 = tile.join(&render.1).join("freq0");
        let rpn = sys.read_u64(&freq0.join("rpn_freq"))? as u32;
        let rp0 = sys.read_u64(&freq0.join("rp0_freq"))? as u32;
        if rpn == 0 || rpn > rp0 {
            return None;
        }
        let profile = freq0.join("power_profile");
        return Some(Gpu {
            card,
            min: freq0.join("min_freq"),
            max: freq0.join("max_freq"),
            profile: sys.is_file(&profile).then_some(profile),
            rpn,
            rp0,
        });
    }
    None
}

/// A kernel cpulist ("0-3,8") as a set; anything unreadable as empty.
pub fn cpulist(text: &str) -> std::collections::BTreeSet<u32> {
    let mut out = std::collections::BTreeSet::new();
    for part in text.replace('\n', ",").split(',') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        let (lo, hi) = match part.split_once('-') {
            Some((a, b)) => (a.parse::<u32>(), b.parse::<u32>()),
            None => (part.parse::<u32>(), part.parse::<u32>()),
        };
        let (Ok(a), Ok(b)) = (lo, hi) else {
            return Default::default();
        };
        if b < a || b - a > 4096 {
            return Default::default();
        }
        out.extend(a..=b);
    }
    out
}

fn cpu(sys: &Sys) -> Option<Cpu> {
    let base = sys.p(CPUFREQ);
    let mut names: Vec<(u32, String)> = sys
        .entries(&base)
        .into_iter()
        .filter_map(|n| num_suffix(&n, "policy").map(|k| (k, n)))
        .collect();
    names.sort();
    if names.is_empty() {
        return None;
    }
    let core = cpulist(
        &sys.read(&sys.p(&format!("{CPU_DEVICES}/cpu_core/cpus")))
            .unwrap_or_default(),
    );
    let atom = cpulist(
        &sys.read(&sys.p(&format!("{CPU_DEVICES}/cpu_atom/cpus")))
            .unwrap_or_default(),
    );
    let mut policies = Vec::new();
    for (_, name) in names {
        let dir = base.join(&name);
        let first: Option<u32> = sys
            .read(&dir.join("affected_cpus"))
            .and_then(|t| t.split_whitespace().next().and_then(|w| w.parse().ok()));
        let kind = match first {
            Some(c) if !core.contains(&c) && atom.contains(&c) => {
                if sys.is_dir(&sys.p(&format!("{CPU_SYS}/cpu{c}/cache/index3"))) {
                    "e"
                } else {
                    "lpe"
                }
            }
            _ => "p",
        };
        let (Some(min_khz), Some(max_khz)) = (
            sys.read_u64(&dir.join("cpuinfo_min_freq")),
            sys.read_u64(&dir.join("cpuinfo_max_freq")),
        ) else {
            continue;
        };
        policies.push(Policy {
            base_khz: sys.read_u64(&dir.join("base_frequency")),
            name,
            dir,
            kind,
            min_khz,
            max_khz,
        });
    }
    if policies.is_empty() {
        return None;
    }
    let types = CORE_TYPES
        .iter()
        .copied()
        .filter(|t| policies.iter().any(|p| p.kind == *t))
        .collect();
    let epp = policies.iter().all(|p| sys.is_file(&p.epp()));
    let epp_available: Vec<String> = sys
        .read(&policies[0].dir.join("energy_performance_available_preferences"))
        .map(|t| t.split_whitespace().map(String::from).collect())
        .unwrap_or_default();
    let file = |n: &str| {
        let p = sys.p(&format!("{PSTATE}/{n}"));
        sys.is_file(&p).then_some(p)
    };
    Some(Cpu {
        policies,
        types,
        epp: epp && !epp_available.is_empty(),
        epp_available,
        max_perf: file("max_perf_pct"),
        min_perf: file("min_perf_pct"),
        no_turbo: file("no_turbo"),
    })
}

fn soc(sys: &Sys) -> Option<Soc> {
    let params = sys.p(SOC_PARAMS);
    let balance = params.join("slider_balance");
    let offset = params.join("slider_offset");
    if !sys.is_file(&balance) || !sys.is_file(&offset) {
        return None;
    }
    let base = sys.p(PLATFORM_PROFILE);
    for dev in sys.entries(&base) {
        if num_suffix(&dev, "platform-profile-").is_none() {
            continue;
        }
        let d = base.join(&dev);
        if sys.read(&d.join("name")).as_deref() == Some(SOC_PROFILE_NAME) {
            return Some(Soc {
                balance,
                offset,
                profile: d.join("profile"),
            });
        }
    }
    None
}

fn hwmon(sys: &Sys) -> (Option<Fan>, Option<PathBuf>) {
    let base = sys.p(HWMON);
    let mut mons: Vec<(u32, String)> = sys
        .entries(&base)
        .into_iter()
        .filter_map(|n| num_suffix(&n, "hwmon").map(|k| (k, n)))
        .collect();
    mons.sort();
    let mut fan = None;
    let mut temp = None;
    for (_, m) in mons {
        let d = base.join(&m);
        match sys.read(&d.join("name")).as_deref() {
            Some("oxp_ec") if fan.is_none() => {
                let (enable, pwm) = (d.join("pwm1_enable"), d.join("pwm1"));
                if sys.is_file(&enable) && sys.is_file(&pwm) {
                    fan = Some(Fan { enable, pwm });
                }
            }
            Some("coretemp") if temp.is_none() => {
                for n in 1..=64 {
                    if sys.read(&d.join(format!("temp{n}_label"))).as_deref() == Some("Package id 0") {
                        temp = Some(d.join(format!("temp{n}_input")));
                        break;
                    }
                }
            }
            _ => {}
        }
    }
    (fan, temp)
}

/// The system's own supplies only: a supply whose `scope` reads `Device` powers a peripheral (the
/// battery of a Bluetooth or USB controller, mouse or headset, which anyone can plug in or pair).
/// Such a battery is never the one whose charging is controlled, and its `status` (a controller
/// charging over USB reads Charging) never says the handheld is on AC.
fn supplies(sys: &Sys) -> (Option<Battery>, Vec<PathBuf>, Vec<PathBuf>) {
    let base = sys.p(POWER_SUPPLY);
    let mut by_type: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for n in sys.entries(&base) {
        if sys.read(&base.join(&n).join("scope")).as_deref() == Some("Device") {
            continue;
        }
        let t = sys.read(&base.join(&n).join("type")).unwrap_or_default();
        by_type.entry(t).or_default().push(n);
    }
    let battery = by_type.get("Battery").and_then(|v| v.first()).map(|n| {
        let d = base.join(n);
        let th = d.join("charge_control_end_threshold");
        let be = d.join("charge_behaviour");
        Battery {
            name: n.clone(),
            threshold: sys.is_file(&th).then_some(th),
            behaviour: sys.is_file(&be).then_some(be),
        }
    });
    let mut online = Vec::new();
    for kind in ["Mains", "USB"] {
        if let Some(v) = by_type.get(kind) {
            online = v
                .iter()
                .map(|n| base.join(n).join("online"))
                .filter(|p| sys.is_file(p))
                .collect();
            if !online.is_empty() {
                break;
            }
        }
    }
    let status = by_type
        .get("Battery")
        .map(|v| v.iter().map(|n| base.join(n).join("status")).collect())
        .unwrap_or_default();
    (battery, online, status)
}

/// The MSR package zone's energy counter (never MMIO: counting both doubles it).
fn energy(sys: &Sys) -> Option<Energy> {
    let dir = sys.p(&format!("{POWERCAP}/intel-rapl:0"));
    if sys.read(&dir.join("name")).as_deref() != Some("package-0") {
        return None;
    }
    let e = dir.join("energy_uj");
    sys.is_file(&e).then(|| Energy {
        energy_uj: e,
        max_range_uj: dir.join("max_energy_range_uj"),
    })
}

/// On AC: any Mains `online` reads 1; with no Mains, any USB; with neither, a battery status
/// Charging or Full; nothing readable: None.
pub fn ac_online(sys: &Sys, d: &Discovery) -> Option<bool> {
    let vals: Vec<u64> = d.ac_online.iter().filter_map(|p| sys.read_u64(p)).collect();
    if !vals.is_empty() {
        return Some(vals.contains(&1));
    }
    let st: Vec<String> = d.battery_status.iter().filter_map(|p| sys.read(p)).collect();
    if !st.is_empty() {
        return Some(st.iter().any(|s| s == "Charging" || s == "Full"));
    }
    None
}

/// The bracketed word of a choice file ("[base]    power_saving" -> "base").
pub fn bracketed(text: &str) -> Option<String> {
    let start = text.find('[')?;
    let end = text[start..].find(']')? + start;
    let w = &text[start + 1..end];
    (!w.is_empty() && !w.contains(char::is_whitespace)).then(|| w.to_string())
}

/// The words of a choice file, brackets removed.
pub fn choice_words(text: &str) -> Vec<String> {
    text.split_whitespace()
        .map(|w| w.trim_matches(|c| c == '[' || c == ']').to_string())
        .collect()
}

/// The package temperature in whole degrees C, or None when unreadable.
pub fn package_temp_c(sys: &Sys, d: &Discovery) -> Option<i32> {
    let m = sys.read_i64(d.temp.as_ref()?)?;
    if !(-50_000..=150_000).contains(&m) {
        return None;
    }
    Some((m / 1000) as i32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cpulists() {
        assert_eq!(
            cpulist("0-3,8,10-11").into_iter().collect::<Vec<_>>(),
            vec![0, 1, 2, 3, 8, 10, 11]
        );
        assert!(cpulist("").is_empty());
        assert!(cpulist("x").is_empty());
        assert!(cpulist("5-2").is_empty());
        assert!(cpulist("0-99999").is_empty());
    }

    #[test]
    fn choice_files() {
        assert_eq!(bracketed("[base]    power_saving").as_deref(), Some("base"));
        assert_eq!(bracketed("base    [power_saving]").as_deref(), Some("power_saving"));
        assert_eq!(
            bracketed("auto [inhibit-charge-awake] inhibit-charge").as_deref(),
            Some("inhibit-charge-awake")
        );
        assert_eq!(bracketed("invalid"), None);
        assert_eq!(bracketed("[]"), None);
        assert_eq!(
            choice_words("[auto] inhibit-charge inhibit-charge-awake"),
            vec!["auto", "inhibit-charge", "inhibit-charge-awake"]
        );
    }
}
