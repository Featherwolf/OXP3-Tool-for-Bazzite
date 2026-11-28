// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! The hardware state: held targets, leases, availability, read-back, re-assertion and drift.
//!
//! Everything here is synchronous and runs under one lock (dbus.rs holds it around each call).
//! Methods never clamp input: a value outside the published bounds is `InvalidArgs` and nothing
//! is written. Every write is read back; a mismatch is `NotApplied` and there is no rollback,
//! because every value that passes validation is safe.

use std::collections::{BTreeMap, BTreeSet};
use std::os::fd::OwnedFd;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::device::{hard, Device};
use crate::discover::{self, bracketed, choice_words, Discovery};
use crate::error::HwError;
use crate::limits::Envelope;
use crate::sysfs::Sys;

/// Knob ids in the order of Bathysphere SPEC 11.2 (the ones a provider serves).
pub const KNOBS: [&str; 12] = [
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
    "packagePower",
];
pub const LEASED: [&str; 7] = [
    "boost",
    "epp",
    "cpuLimits",
    "socBalance",
    "gpuRange",
    "fan",
    "chargeBehaviour",
];
pub const BOOST_MODES: [&str; 3] = ["firmware", "follow", "manual"];
pub const GPU_LEVELS: [&str; 2] = ["auto", "manual"];
pub const GPU_PROFILES: [&str; 2] = ["base", "power_saving"];

const RAPL_TOL_UW: u64 = 250_000;
const GPU_TOL_MHZ: u64 = 25;
const CAP_TOL_KHZ: u64 = 100_000;
/// oxpec scales pwm1 0..255 to the EC's 0..184 and back, so a duty reads back up to 2 lower.
const PWM_TOL: u64 = 2;
const XE_TRIES: u32 = 3;
const GRACE_REASSERTS: u8 = 3;

/// The interfaces whose properties a change touches (dbus.rs emits PropertiesChanged for them).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Iface {
    Provider,
    PowerLimits,
    CpuEnergy,
    CpuLimits,
    SocBalance,
    GpuRange,
    FanMode,
    ChargeBehaviour,
    TdpLimit,
    GpuLevel,
    GpuProfile,
    ChargeLimit,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Ev {
    Changed(&'static str, String),
    Props(Iface),
}

/// Who asked: the caller's uid and unique name, or the provider itself (re-assertion, safety).
#[derive(Debug, Clone, Default)]
pub struct Ctx {
    pub uid: Option<u32>,
    pub sender: Option<String>,
}

impl Ctx {
    pub fn provider() -> Ctx {
        Ctx::default()
    }
    fn who(&self) -> String {
        self.uid.map(|u| u.to_string()).unwrap_or_else(|| "provider".into())
    }
    fn sender(&self) -> String {
        self.sender.clone().unwrap_or_default()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reason {
    Ok,
    Kernel,
    Grants,
    Locked,
    Device,
    Disabled,
    Busy(&'static str),
}

impl Reason {
    pub fn word(&self) -> String {
        match self {
            Reason::Ok => String::new(),
            Reason::Kernel => "kernel".into(),
            Reason::Grants => "grants".into(),
            Reason::Locked => "locked".into(),
            Reason::Device => "device".into(),
            Reason::Disabled => "disabled".into(),
            Reason::Busy(w) => format!("busy:{w}"),
        }
    }
}

/// Event timing. Production always uses scale 1; test builds may shorten it (`--time-scale`).
#[derive(Debug, Clone, Copy)]
pub struct Timing {
    pub scale: f64,
}

impl Timing {
    fn d(&self, s: f64) -> Duration {
        Duration::from_secs_f64(s * self.scale)
    }
    pub fn safety_tick(&self) -> Duration {
        self.d(1.0)
    }
    pub fn ac_poll(&self) -> Duration {
        self.d(2.0)
    }
    pub fn ac_debounce(&self) -> Duration {
        self.d(2.0)
    }
    pub fn grace(&self) -> Duration {
        self.d(20.0)
    }
    pub fn resume_delay(&self) -> Duration {
        self.d(3.0)
    }
    pub fn drift_every(&self) -> Duration {
        self.d(2.0)
    }
    pub fn rediscover(&self) -> Duration {
        self.d(10.0)
    }
    pub fn cooldown(&self, s: u64) -> Duration {
        self.d(s as f64)
    }
    pub fn xe_retry(&self) -> Duration {
        self.d(0.3)
    }
}

#[derive(Debug, Clone, Default)]
struct Ctl {
    lease: Option<String>,
    overridden: bool,
    mism: u8,
    /// Written at least once since start (or since its last release): what drift checks cover.
    applied: bool,
}

#[derive(Debug, Clone)]
pub struct Paths {
    /// /etc/oxp3-hw (root-owned: limits.toml and the kill switch `disabled`)
    pub etc: PathBuf,
    /// /var/lib/oxp3-hw (StateDirectory: persist.json)
    pub state: PathBuf,
    /// /run/oxp3-hw (RuntimeDirectory: snapshot.json)
    pub run: PathBuf,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct Snapshot {
    pub boot_id: String,
    pub epp: BTreeMap<String, String>,
    pub max_perf: Option<u32>,
    pub min_perf: Option<u32>,
    /// scaling_max_freq per policy, kHz
    pub caps: BTreeMap<String, u64>,
    pub soc_balance: Option<u32>,
    pub soc_offset: Option<u32>,
    pub gpu_min: Option<u32>,
    pub gpu_max: Option<u32>,
    pub gpu_profile: Option<String>,
    /// RAPL constraints that refused a write with EACCES this boot ("intel-rapl:0/pl1").
    pub locked: BTreeSet<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct Persist {
    charge_limit: Option<u32>,
}

/// Other writers of the same hardware, from bus-name owners and unit state.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CoWriters {
    /// "power-profiles-daemon" or "tuned-ppd" while org.freedesktop.UPower.PowerProfiles has an owner.
    pub ppd: Option<&'static str>,
    pub hhd: bool,
    pub powerstation: bool,
    pub thermald: bool,
}

fn w_text(uw: Option<u64>) -> String {
    match uw {
        None => "unreadable".into(),
        Some(v) if v % 1_000_000 == 0 => (v / 1_000_000).to_string(),
        Some(v) => {
            let s = format!("{:.3}", v as f64 / 1e6);
            s.trim_end_matches('0').trim_end_matches('.').to_string()
        }
    }
}

fn close(a: Option<u64>, b: u64, tol: u64) -> bool {
    a.map(|a| a.abs_diff(b) <= tol).unwrap_or(false)
}

fn errno(e: &std::io::Error) -> i32 {
    e.raw_os_error().unwrap_or(0)
}

pub struct Hw {
    pub sys: Sys,
    pub dev: &'static Device,
    pub env: Envelope,
    pub d: Discovery,
    pub t: Timing,
    pub paths: Paths,
    pub snap: Snapshot,
    pub cow: CoWriters,
    pub disabled: bool,
    /// Between logind's PrepareForSleep(true) and (false): only fan "auto" is accepted.
    pub sleeping: bool,
    pub events: Vec<Ev>,
    writes: u64,

    tdp_target: Option<u32>,
    tdp_capped: bool,
    tdp: Ctl,
    boost_mode: &'static str,
    boost_limit: u32,
    boost_offset: u32,
    boost: Ctl,
    gpu_level: &'static str,
    gpu_clock: u32,
    gpu: Ctl,
    gprof_target: Option<&'static str>,
    gprof: Ctl,
    range: (u32, u32),
    range_ctl: Ctl,
    epp_target: Option<BTreeMap<String, String>>,
    epp: Ctl,
    lim_max: Option<u32>,
    lim_min: Option<u32>,
    lim_caps: BTreeMap<String, u32>,
    lim_floor_capped: bool,
    lim: Ctl,
    soc_target: Option<(u32, u32)>,
    soc: Ctl,
    fan_mode: &'static str,
    fan_last_revert: String,
    fan_cooldown_until: Option<Instant>,
    /// The logind delay inhibitor, held while the fan is not "auto" (dbus.rs takes it).
    pub fan_inhibitor: Option<OwnedFd>,
    fan: Ctl,
    charge_target: Option<u32>,
    charge: Ctl,
    beh_value: &'static str,
    beh: Ctl,
    pkg_base: Option<(u64, Instant)>,
    pkg_last: Option<(u32, u32, Instant)>,

    pub ac: Option<bool>,
    next_ac_poll: Instant,
    reassert_at: Option<(Instant, &'static str)>,
    grace_until: Option<Instant>,
    grace_kind: &'static str,
    grace_reasserts: u8,
    next_drift: Instant,
    next_rediscover: Instant,
    avail: BTreeMap<&'static str, Reason>,
}

impl Hw {
    pub fn new(sys: Sys, dev: &'static Device, env: Envelope, d: Discovery, t: Timing, paths: Paths) -> Hw {
        let now = Instant::now();
        let gpu_env = |d: &Discovery| {
            d.gpu.as_ref().map(|g| {
                (
                    g.rpn.max(env.gpu_min_mhz.unwrap_or(0)),
                    g.rp0.min(env.gpu_max_mhz.unwrap_or(u32::MAX)),
                )
            })
        };
        let range = gpu_env(&d).unwrap_or((0, 0));
        let mut hw = Hw {
            boost_limit: env.boost_default_w,
            sys,
            dev,
            env,
            d,
            t,
            paths,
            snap: Snapshot::default(),
            cow: CoWriters::default(),
            disabled: false,
            sleeping: false,
            events: Vec::new(),
            writes: 0,
            tdp_target: None,
            tdp_capped: false,
            tdp: Ctl::default(),
            boost_mode: "firmware",
            boost_offset: 0,
            boost: Ctl::default(),
            gpu_level: "auto",
            gpu_clock: range.1,
            gpu: Ctl::default(),
            gprof_target: None,
            gprof: Ctl::default(),
            range,
            range_ctl: Ctl::default(),
            epp_target: None,
            epp: Ctl::default(),
            lim_max: None,
            lim_min: None,
            lim_caps: BTreeMap::new(),
            lim_floor_capped: false,
            lim: Ctl::default(),
            soc_target: None,
            soc: Ctl::default(),
            fan_mode: "auto",
            fan_last_revert: String::new(),
            fan_cooldown_until: None,
            fan_inhibitor: None,
            fan: Ctl::default(),
            charge_target: None,
            charge: Ctl::default(),
            beh_value: "auto",
            beh: Ctl::default(),
            pkg_base: None,
            pkg_last: None,
            ac: None,
            next_ac_poll: now,
            reassert_at: None,
            grace_until: None,
            grace_kind: "ac",
            grace_reasserts: 0,
            next_drift: now,
            next_rediscover: now,
            avail: BTreeMap::new(),
        };
        hw.load_snapshot();
        if let Some(c) = hw.gpu_clock_from_snapshot() {
            hw.gpu_clock = c;
        }
        hw.ac = discover::ac_online(&hw.sys, &hw.d);
        // fan and charge behaviour start as "auto" held: a manual state another tool left is not
        // adopted (no lease holder exists for it); FanMode and ChargeBehaviour read the hardware
        hw.avail = hw.reasons();
        if let Some(e) = &hw.d.energy {
            if let Some(v) = hw.sys.read_u64(&e.energy_uj) {
                hw.pkg_base = Some((v, now));
            }
        }
        hw.events.clear();
        hw
    }

    // ---- snapshot and persistence ---------------------------------------------------------------

    fn boot_id(&self) -> String {
        self.sys.read(&self.sys.p(discover::BOOT_ID)).unwrap_or_default()
    }

    /// The boot snapshot: what the provider's leased knobs return to. Taken once per boot (a
    /// restart within the same boot reads it back, so it never captures the provider's own values).
    fn load_snapshot(&mut self) {
        let boot = self.boot_id();
        let path = self.paths.run.join("snapshot.json");
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Ok(s) = serde_json::from_str::<Snapshot>(&text) {
                if !boot.is_empty() && s.boot_id == boot {
                    self.snap = s;
                    return;
                }
            }
        }
        let mut s = Snapshot {
            boot_id: boot,
            ..Default::default()
        };
        if let Some(c) = &self.d.cpu {
            for p in &c.policies {
                if let Some(v) = self.sys.read(&p.epp()) {
                    s.epp.insert(p.name.clone(), v);
                }
                if let Some(v) = self.sys.read_u64(&p.scaling_max()) {
                    s.caps.insert(p.name.clone(), v);
                }
            }
            s.max_perf = c.max_perf.as_ref().and_then(|p| self.sys.read_u64(p)).map(|v| v as u32);
            s.min_perf = c.min_perf.as_ref().and_then(|p| self.sys.read_u64(p)).map(|v| v as u32);
        }
        if let Some(soc) = &self.d.soc {
            s.soc_balance = self.read_hex(&soc.balance);
            s.soc_offset = self.read_hex(&soc.offset);
        }
        if let Some(g) = &self.d.gpu {
            s.gpu_min = self.sys.read_u64(&g.min).map(|v| v as u32);
            s.gpu_max = self.sys.read_u64(&g.max).map(|v| v as u32);
            s.gpu_profile = g
                .profile
                .as_ref()
                .and_then(|p| self.sys.read(p))
                .and_then(|t| bracketed(&t));
        }
        self.snap = s;
        self.save_snapshot();
    }

    fn save_snapshot(&self) {
        let path = self.paths.run.join("snapshot.json");
        write_atomic(&path, &serde_json::to_vec_pretty(&self.snap).unwrap_or_default());
    }

    fn gpu_clock_from_snapshot(&self) -> Option<u32> {
        let (lo, hi) = self.gpu_bounds()?;
        self.snap.gpu_max.map(|m| m.clamp(lo, hi))
    }

    /// Re-apply the persisted charge threshold (the only persisted value) at start.
    pub fn apply_persisted(&mut self) {
        let path = self.paths.state.join("persist.json");
        let Ok(text) = std::fs::read_to_string(&path) else {
            return;
        };
        let Ok(p) = serde_json::from_str::<Persist>(&text) else {
            eprintln!("oxp3-hwd: ignoring unreadable {}", path.display());
            return;
        };
        let Some(v) = p.charge_limit else { return };
        if !(self.env.charge_min..=self.env.charge_max).contains(&v) || self.reason("chargeLimit") != Reason::Ok {
            return;
        }
        let ctx = Ctx {
            uid: None,
            sender: Some("persist".into()),
        };
        if let Err(e) = self.charge_limit_apply(&ctx, v) {
            eprintln!("oxp3-hwd: persisted charge limit {v} not applied: {e}");
        } else {
            self.charge_target = Some(v);
            self.charge.applied = true;
        }
        self.events.clear();
    }

    /// At start nobody holds the fan or the charge behaviour. A manual fan or a charging pause
    /// that something else left (this provider killed before ExecStopPost ran, an EC that keeps
    /// the mode, another tool) would otherwise run unguarded: the fan guard only watches a mode
    /// the provider set. Both go back to auto, as --restore-safe does.
    pub fn reset_unheld(&mut self) {
        let ctx = Ctx {
            uid: None,
            sender: Some("start".into()),
        };
        if let Some(f) = self.d.fan.clone() {
            if self.sys.read_u64(&f.enable) != Some(self.dev.fan.enable_auto as u64) {
                match self.fan_write(&ctx, "auto") {
                    Ok(()) => eprintln!("oxp3-hwd: the fan was left manual and nobody holds it: back to auto"),
                    Err(got) => eprintln!("oxp3-hwd: the fan was left manual ({got}) and does not take auto"),
                }
            }
        }
        if self.d.battery.as_ref().and_then(|b| b.behaviour.as_ref()).is_some() {
            if let Some(cur) = self.beh_read().filter(|b| b != "auto") {
                match self.beh_apply(&ctx, "auto") {
                    Ok(()) => eprintln!("oxp3-hwd: charge behaviour was left {cur} and nobody holds it: back to auto"),
                    Err(e) => eprintln!("oxp3-hwd: charge behaviour was left {cur}: {e}"),
                }
            }
        }
        self.events.clear();
    }

    fn persist_charge(&self, v: u32) {
        let p = Persist { charge_limit: Some(v) };
        write_atomic(
            &self.paths.state.join("persist.json"),
            &serde_json::to_vec(&p).unwrap_or_default(),
        );
    }

    // ---- availability -------------------------------------------------------------------------

    fn busy_power(&self) -> Option<&'static str> {
        if self.cow.powerstation {
            Some("powerstation")
        } else if self.cow.hhd {
            Some("hhd")
        } else {
            None
        }
    }

    pub fn reason(&self, knob: &str) -> Reason {
        if self.disabled {
            return Reason::Disabled;
        }
        let s = &self.sys;
        let busy = self.busy_power().map(Reason::Busy);
        let r = match knob {
            "tdp" | "boost" => {
                let z = &self.d.zones;
                if z.len() != 2 || !z.iter().any(|z| z.mmio) || !z.iter().any(|z| !z.mmio) {
                    Reason::Kernel
                } else if z.iter().any(|z| !s.can_write(&z.pl1) || !s.can_write(&z.pl2)) {
                    Reason::Grants
                } else if z.iter().any(|z| {
                    self.snap.locked.contains(&format!("{}/pl1", z.id))
                        || self.snap.locked.contains(&format!("{}/pl2", z.id))
                }) {
                    Reason::Locked
                } else {
                    busy.unwrap_or(Reason::Ok)
                }
            }
            "gpu" | "gpuRange" => match &self.d.gpu {
                None => Reason::Kernel,
                Some(g) if !s.can_write(&g.min) || !s.can_write(&g.max) => Reason::Grants,
                Some(_) => busy.unwrap_or(Reason::Ok),
            },
            "gpuPowerProfile" => match self.d.gpu.as_ref().and_then(|g| g.profile.as_ref()) {
                None => Reason::Kernel,
                Some(p) if !s.can_write(p) => Reason::Grants,
                Some(_) => busy.unwrap_or(Reason::Ok),
            },
            "epp" => match &self.d.cpu {
                Some(c) if c.epp => {
                    if c.policies.iter().any(|p| !s.can_write(&p.epp())) {
                        Reason::Grants
                    } else if let Some(w) = self.cow.ppd {
                        Reason::Busy(w)
                    } else {
                        busy.unwrap_or(Reason::Ok)
                    }
                }
                _ => Reason::Kernel,
            },
            "cpuLimits" => match &self.d.cpu {
                Some(c) => match (&c.max_perf, &c.min_perf) {
                    (Some(mx), Some(mn)) => {
                        if !s.can_write(mx)
                            || !s.can_write(mn)
                            || c.policies.iter().any(|p| !s.can_write(&p.scaling_max()))
                        {
                            Reason::Grants
                        } else {
                            busy.unwrap_or(Reason::Ok)
                        }
                    }
                    _ => Reason::Kernel,
                },
                None => Reason::Kernel,
            },
            "socBalance" => match &self.d.soc {
                None => Reason::Kernel,
                Some(soc) if !s.can_write(&soc.balance) || !s.can_write(&soc.offset) || !s.can_write(&soc.profile) => {
                    Reason::Grants
                }
                Some(_) => Reason::Ok,
            },
            "fan" => match (&self.d.fan, &self.d.temp) {
                (Some(f), Some(_)) => {
                    if !s.can_write(&f.enable) || !s.can_write(&f.pwm) {
                        Reason::Grants
                    } else if self.env.fan_modes.len() < 2 {
                        Reason::Device
                    } else {
                        Reason::Ok
                    }
                }
                _ => Reason::Kernel,
            },
            "chargeLimit" => match self.d.battery.as_ref().and_then(|b| b.threshold.as_ref()) {
                None => Reason::Kernel,
                Some(p) if !s.can_write(p) => Reason::Grants,
                Some(_) => Reason::Ok,
            },
            "chargeBehaviour" => match self.d.battery.as_ref().and_then(|b| b.behaviour.as_ref()) {
                None => Reason::Kernel,
                Some(p) if !s.can_write(p) => Reason::Grants,
                Some(_) if self.behaviour_choices().len() < 2 => Reason::Device,
                Some(_) => Reason::Ok,
            },
            "packagePower" => match &self.d.energy {
                None => Reason::Kernel,
                Some(e) if !s.can_read(&e.energy_uj) => Reason::Grants,
                Some(_) => Reason::Ok,
            },
            _ => Reason::Device,
        };
        r
    }

    pub fn reasons(&self) -> BTreeMap<&'static str, Reason> {
        KNOBS.iter().map(|k| (*k, self.reason(k))).collect()
    }

    /// The knob ids available now, in KNOBS order (the Knobs property).
    pub fn knobs(&self) -> Vec<String> {
        KNOBS
            .iter()
            .filter(|k| self.avail.get(*k) == Some(&Reason::Ok))
            .map(|k| k.to_string())
            .collect()
    }

    pub fn available(&self, knob: &str) -> bool {
        self.avail.get(knob) == Some(&Reason::Ok)
    }

    /// Recompute availability; emit Changed(available/unavailable) for every change and drop the
    /// lease of a knob that became unavailable (a co-writer owns it now; nothing is written).
    pub fn refresh_avail(&mut self) {
        let now = self.reasons();
        let mut changed = false;
        for k in KNOBS {
            let was = self.avail.get(k).cloned().unwrap_or(Reason::Device);
            let is = now.get(k).cloned().unwrap_or(Reason::Device);
            if (was == Reason::Ok) != (is == Reason::Ok) {
                changed = true;
                let word = if is == Reason::Ok { "available" } else { "unavailable" };
                eprintln!("oxp3-hwd: {k} {word} ({})", is.word());
                self.events.push(Ev::Changed(k, word.into()));
                if is != Reason::Ok {
                    if let Some(c) = self.ctl_mut(k) {
                        c.lease = None;
                    }
                }
            }
        }
        self.avail = now;
        if changed {
            self.events.push(Ev::Props(Iface::Provider));
        }
    }

    fn need(&self, knob: &str) -> Result<(), HwError> {
        if self.disabled {
            return Err(HwError::Disabled("disabled".into()));
        }
        match self.reason(knob) {
            Reason::Ok => Ok(()),
            Reason::Disabled => Err(HwError::Disabled("disabled".into())),
            // a co-writer (power-profiles-daemon, tuned-ppd, hhd, powerstation): Busy naming it
            Reason::Busy(w) => Err(HwError::Busy(w.into())),
            r => Err(HwError::NotSupported(r.word())),
        }
    }

    fn ctl_mut(&mut self, knob: &str) -> Option<&mut Ctl> {
        Some(match knob {
            "tdp" => &mut self.tdp,
            "boost" => &mut self.boost,
            "gpu" => &mut self.gpu,
            "gpuPowerProfile" => &mut self.gprof,
            "gpuRange" => &mut self.range_ctl,
            "epp" => &mut self.epp,
            "cpuLimits" => &mut self.lim,
            "socBalance" => &mut self.soc,
            "fan" => &mut self.fan,
            "chargeLimit" => &mut self.charge,
            "chargeBehaviour" => &mut self.beh,
            _ => return None,
        })
    }

    fn ctl(&self, knob: &str) -> Option<&Ctl> {
        Some(match knob {
            "tdp" => &self.tdp,
            "boost" => &self.boost,
            "gpu" => &self.gpu,
            "gpuPowerProfile" => &self.gprof,
            "gpuRange" => &self.range_ctl,
            "epp" => &self.epp,
            "cpuLimits" => &self.lim,
            "socBalance" => &self.soc,
            "fan" => &self.fan,
            "chargeLimit" => &self.charge,
            "chargeBehaviour" => &self.beh,
            _ => return None,
        })
    }

    pub fn lease_of(&self, knob: &str) -> Option<String> {
        self.ctl(knob).and_then(|c| c.lease.clone())
    }

    fn check_lease(&self, knob: &str, ctx: &Ctx) -> Result<(), HwError> {
        match self.ctl(knob).and_then(|c| c.lease.as_ref()) {
            Some(h) if ctx.sender.as_deref() != Some(h.as_str()) => Err(HwError::Busy(format!("lease:{h}"))),
            _ => Ok(()),
        }
    }

    // ---- writes -------------------------------------------------------------------------------

    fn audit(&self, ctx: &Ctx, knob: &str, file: &Path, old: &str, new: &str, read: &str) {
        let rel = file.strip_prefix(self.sys.root()).unwrap_or(file);
        eprintln!(
            "oxp3-hwd: audit uid={} knob={knob} file=/{} old={old} new={new} read={read}",
            ctx.who(),
            rel.display().to_string().trim_start_matches('/')
        );
    }

    /// One write, retried on EAGAIN (xe while the GT resets or resumes) when `retry`.
    fn write(&mut self, path: &Path, text: &str, retry: bool) -> std::io::Result<()> {
        let tries = if retry { XE_TRIES + 1 } else { 1 };
        let mut last = None;
        for i in 0..tries {
            match self.sys.write(path, text) {
                Ok(()) => {
                    self.writes += 1;
                    return Ok(());
                }
                Err(e) if errno(&e) == libc::EAGAIN && i + 1 < tries => {
                    std::thread::sleep(self.t.xe_retry());
                    last = Some(e);
                }
                Err(e) => return Err(e),
            }
        }
        Err(last.unwrap_or_else(|| std::io::Error::from_raw_os_error(libc::EAGAIN)))
    }

    fn read_hex(&self, p: &Path) -> Option<u32> {
        let t = self.sys.read(p)?;
        u32::from_str_radix(t.split_whitespace().next()?, 16).ok()
    }

    // ---- RAPL: tdp (PL1, Steam's) and boost (PL2, leased) -------------------------------------

    pub fn tdp_bounds(&self) -> (u32, u32) {
        (self.env.sustained_min_w, self.env.tdp_limit_max())
    }

    /// PL1 to apply for a target: on battery (or AC unknown) an admin AC-only value is capped to
    /// the battery maximum.
    fn tdp_effective(&self, target: u32) -> (u32, bool) {
        if self.ac != Some(true) && target > self.env.sustained_max_w {
            (self.env.sustained_max_w, true)
        } else {
            (target, false)
        }
    }

    /// The highest PL1 the zones read now, in whole W (the floor PL2 must keep).
    fn zones_pl1_w(&self) -> u32 {
        self.d
            .zones
            .iter()
            .filter_map(|z| self.sys.read_u64(&z.pl1))
            .max()
            .map(|v| v.div_ceil(1_000_000) as u32)
            .unwrap_or(0)
    }

    fn pl1_basis(&self) -> u32 {
        match self.tdp_target {
            Some(t) => self.tdp_effective(t).0,
            None => self.zones_pl1_w(),
        }
    }

    /// PL2 for a PL1, inside the boost envelope. Only a PL1 from the registers (nothing set by
    /// Steam yet) can be above the envelope; PL2 then stays at the envelope's top and
    /// apply_power refuses rather than write a register-derived bound.
    fn pl2_for(&self, pl1: u32) -> u32 {
        let e = &self.env;
        let v = match self.boost_mode {
            "follow" => (pl1 + self.boost_offset).min(e.boost_max_w),
            "manual" => self.boost_limit,
            _ => e.boost_default_w,
        };
        v.max(pl1).min(e.boost_max_w)
    }

    pub fn boost_effective(&self) -> u32 {
        self.pl2_for(self.pl1_basis())
    }

    /// Write PL1 (when given) and PL2 on both zones, MMIO first; per zone PL2 first when PL1
    /// rises, else PL1 first. Each step is checked against what the zone reads at that moment,
    /// because firmware may accept a write without taking it: PL1 is raised only up to the PL2
    /// the zone really has, and PL2 is lowered only down to the PL1 it really has (lowering PL1
    /// and raising PL2 are always safe). So no step ever leaves PL1 above PL2. Equal values
    /// (within 250 mW) are not written. Then everything is read back.
    fn apply_power(&mut self, ctx: &Ctx, pl1: Option<u32>, pl2: u32) -> Result<(), HwError> {
        // the last guards: never outside the hard limits or the envelope, whatever computed them
        for w in pl1.into_iter().chain(std::iter::once(pl2)) {
            let mw = w.saturating_mul(1000);
            if !(hard::RAPL_MIN_MW..=hard::RAPL_MAX_MW).contains(&mw) || (w as u64) * 1_000_000 < hard::RAPL_UNIT_UW {
                return Err(HwError::InvalidArgs(format!("{w} W is outside the hard limit")));
            }
        }
        let e = &self.env;
        if pl1.is_some_and(|p| p < e.sustained_min_w || p > e.tdp_limit_max())
            || !(e.boost_min_w..=e.boost_max_w).contains(&pl2)
        {
            return Err(HwError::InvalidArgs(format!(
                "PL1 {pl1:?} W / PL2 {pl2} W is outside the envelope"
            )));
        }
        if let Some(p) = pl1 {
            if pl2 < p {
                return Err(HwError::InvalidArgs("PL2 below PL1".into()));
            }
        }
        let mut first: Option<HwError> = None;
        let mut checks: Vec<(String, PathBuf, u64, &'static str)> = Vec::new();
        let zones = self.d.zones.clone();
        let mut locked_new = false;
        for z in &zones {
            let cur1 = self.sys.read_u64(&z.pl1);
            let mut steps: Vec<(&str, &PathBuf, u32, &'static str)> = Vec::new();
            match pl1 {
                Some(p) if cur1.map(|c| p as u64 * 1_000_000 > c).unwrap_or(true) => {
                    steps.push(("pl2", &z.pl2, pl2, "boost"));
                    steps.push(("pl1", &z.pl1, p, "tdp"));
                }
                Some(p) => {
                    steps.push(("pl1", &z.pl1, p, "tdp"));
                    steps.push(("pl2", &z.pl2, pl2, "boost"));
                }
                None => steps.push(("pl2", &z.pl2, pl2, "boost")),
            }
            for (which, path, w, knob) in steps {
                let id = format!("{}/{which}", z.id);
                if self.snap.locked.contains(&id) {
                    first.get_or_insert(HwError::Locked(id));
                    continue;
                }
                let want = w as u64 * 1_000_000;
                let cur = self.sys.read_u64(path);
                // the other limit of this zone as it reads now
                let (other, other_knob) = if which == "pl1" {
                    (&z.pl2, "boost")
                } else {
                    (&z.pl1, "tdp")
                };
                let other_now = self.sys.read_u64(other);
                let unsafe_step = match (which, cur) {
                    // raising PL1 (or PL1 unreadable): only up to the zone's real PL2
                    ("pl1", c) if c.is_none_or(|c| want > c) => other_now.is_none_or(|o| want > o + RAPL_TOL_UW),
                    // lowering PL2 (or PL2 unreadable): only down to the zone's real PL1
                    ("pl2", c) if c.is_none_or(|c| want < c) => other_now.is_none_or(|o| want + RAPL_TOL_UW < o),
                    _ => false,
                };
                if unsafe_step {
                    eprintln!(
                        "oxp3-hwd: {id}: not written: {w} W would leave PL1 above PL2 ({other_knob} reads {})",
                        w_text(other_now)
                    );
                    first.get_or_insert(HwError::NotApplied(format!("{other_knob}={}", w_text(other_now))));
                    continue;
                }
                if !close(cur, want, RAPL_TOL_UW) {
                    match self.write(path, &want.to_string(), false) {
                        Ok(()) => {
                            let got = self.sys.read_u64(path);
                            self.audit(ctx, knob, path, &w_text(cur), &w.to_string(), &w_text(got));
                        }
                        Err(e) if errno(&e) == libc::EACCES || errno(&e) == libc::EPERM => {
                            eprintln!("oxp3-hwd: {id} refused the write ({e}): locked for this boot");
                            self.snap.locked.insert(id.clone());
                            locked_new = true;
                            first.get_or_insert(HwError::Locked(id));
                            continue;
                        }
                        Err(e) => {
                            eprintln!("oxp3-hwd: {id}: {e}");
                            first.get_or_insert(HwError::NotApplied(format!("{knob}={}", w_text(cur))));
                            continue;
                        }
                    }
                }
                checks.push((id, path.clone(), want, knob));
            }
            // a zone that still reads PL1 above PL2 did not get what was asked: another writer
            // set it, or (nothing set by Steam yet) the registers' PL1 is above the boost envelope
            if let (Some(a), Some(b)) = (self.sys.read_u64(&z.pl1), self.sys.read_u64(&z.pl2)) {
                if a > b + RAPL_TOL_UW {
                    first.get_or_insert(HwError::NotApplied(format!("tdp={}", w_text(Some(a)))));
                }
            }
        }
        for (_id, path, want, knob) in checks {
            let got = self.sys.read_u64(&path);
            if !close(got, want, RAPL_TOL_UW) {
                first.get_or_insert(HwError::NotApplied(format!("{knob}={}", w_text(got))));
            }
        }
        if locked_new {
            self.save_snapshot();
            self.refresh_avail();
        }
        match first {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }

    pub fn tdp_value(&self) -> u32 {
        self.tdp_target.unwrap_or_else(|| {
            // nothing set yet: the lower PL1 of the two zones, which is what the hardware enforces
            self.d
                .zones
                .iter()
                .filter_map(|z| self.sys.read_u64(&z.pl1))
                .min()
                .map(|v| (v / 1_000_000) as u32)
                .unwrap_or(0)
        })
    }

    pub fn set_tdp(&mut self, ctx: &Ctx, w: u32, commit: bool) -> Result<u32, HwError> {
        self.need("tdp")?;
        let (lo, hi) = self.tdp_bounds();
        if !(lo..=hi).contains(&w) {
            return Err(HwError::InvalidArgs(format!("TdpLimit {w} W is outside {lo}..{hi} W")));
        }
        if !commit {
            return Ok(0);
        }
        let old_pl2 = self.boost_effective();
        self.tdp_target = Some(w);
        self.tdp.overridden = false;
        self.tdp.mism = 0;
        let (eff, capped) = self.tdp_effective(w);
        let pl2 = self.pl2_for(eff);
        let r = self.apply_power(ctx, Some(eff), pl2);
        self.tdp.applied = true;
        self.tdp_capped = capped;
        // PL2 was written with PL1 (pl2_for), so the boost is held from now on
        self.boost.applied = true;
        self.events.push(Ev::Props(Iface::TdpLimit));
        r?;
        self.events
            .push(Ev::Changed("tdp", if capped { "capped-battery" } else { "set" }.into()));
        if pl2 != old_pl2 {
            self.events.push(Ev::Props(Iface::PowerLimits));
            let why = if eff > old_pl2 { "raised-with-sustained" } else { "set" };
            self.events.push(Ev::Changed("boost", why.into()));
        }
        Ok(w)
    }

    pub fn boost_state(&self) -> (&'static str, u32, u32) {
        (self.boost_mode, self.boost_limit, self.boost_offset)
    }

    pub fn set_boost(&mut self, ctx: &Ctx, mode: &str, limit: u32, offset: u32, commit: bool) -> Result<u32, HwError> {
        self.need("boost")?;
        self.check_lease("boost", ctx)?;
        let e = &self.env;
        let mode: &'static str = match BOOST_MODES.iter().find(|m| **m == mode) {
            Some(m) => m,
            None => return Err(HwError::InvalidArgs(format!("unknown boost mode {mode:?}"))),
        };
        match mode {
            "firmware" if limit != 0 || offset != 0 => {
                return Err(HwError::InvalidArgs(
                    "firmware takes no limit and no offset (both 0)".into(),
                ))
            }
            "follow" if limit != 0 => return Err(HwError::InvalidArgs("follow takes no limit (0)".into())),
            "follow" if offset > e.boost_offset_max_w => {
                return Err(HwError::InvalidArgs(format!(
                    "offset {offset} W is outside 0..{} W",
                    e.boost_offset_max_w
                )))
            }
            "manual" if offset != 0 => return Err(HwError::InvalidArgs("manual takes no offset (0)".into())),
            "manual" if !(e.boost_min_w..=e.boost_max_w).contains(&limit) => {
                return Err(HwError::InvalidArgs(format!(
                    "limit {limit} W is outside {}..{} W",
                    e.boost_min_w, e.boost_max_w
                )))
            }
            _ => {}
        }
        if !commit {
            return Ok(0);
        }
        self.boost_mode = mode;
        match mode {
            "manual" => self.boost_limit = limit,
            "follow" => self.boost_offset = offset,
            _ => {}
        }
        self.boost.lease = (mode != "firmware").then(|| ctx.sender());
        self.boost.overridden = false;
        self.boost.mism = 0;
        let pl2 = self.boost_effective();
        let r = self.apply_power(ctx, None, pl2);
        self.boost.applied = true;
        self.events.push(Ev::Props(Iface::PowerLimits));
        r?;
        self.events.push(Ev::Changed("boost", "set".into()));
        Ok(pl2)
    }

    // ---- GPU: level and clock (Steam's), range (leased), power profile (Steam's) --------------

    /// The render GT's range intersected with the admin envelope.
    pub fn gpu_bounds(&self) -> Option<(u32, u32)> {
        self.d.gpu.as_ref().map(|g| {
            (
                g.rpn.max(self.env.gpu_min_mhz.unwrap_or(0)),
                g.rp0.min(self.env.gpu_max_mhz.unwrap_or(u32::MAX)),
            )
        })
    }

    pub fn gpu_state(&self) -> (&'static str, u32, (u32, u32)) {
        (self.gpu_level, self.gpu_clock, self.range)
    }

    fn write_gpu(&mut self, ctx: &Ctx, knob: &'static str, lo: u32, hi: u32) -> Result<(), HwError> {
        let Some(g) = self.d.gpu.clone() else {
            return Err(HwError::NotSupported("kernel".into()));
        };
        let cur_min = self.sys.read_u64(&g.min);
        let order: [(&PathBuf, u32); 2] = if cur_min.map(|c| lo as u64 > c).unwrap_or(false) {
            [(&g.max, hi), (&g.min, lo)]
        } else {
            [(&g.min, lo), (&g.max, hi)]
        };
        let mut first = None;
        for (path, want) in order {
            let cur = self.sys.read_u64(path);
            if close(cur, want as u64, GPU_TOL_MHZ) {
                continue;
            }
            match self.write(path, &want.to_string(), true) {
                Ok(()) => {
                    let got = self.sys.read_u64(path);
                    self.audit(ctx, knob, path, &fmt_opt(cur), &want.to_string(), &fmt_opt(got));
                }
                Err(e) => {
                    eprintln!("oxp3-hwd: {}: {e}", path.display());
                    first.get_or_insert(());
                }
            }
        }
        let (a, b) = (self.sys.read_u64(&g.min), self.sys.read_u64(&g.max));
        if first.is_some() || !close(a, lo as u64, GPU_TOL_MHZ) || !close(b, hi as u64, GPU_TOL_MHZ) {
            return Err(HwError::NotApplied(format!("{knob}={}..{}", fmt_opt(a), fmt_opt(b))));
        }
        Ok(())
    }

    fn gpu_wanted(&self) -> (u32, u32) {
        if self.gpu_level == "manual" {
            (self.gpu_clock, self.gpu_clock)
        } else {
            self.range
        }
    }

    pub fn set_gpu_level(&mut self, ctx: &Ctx, level: &str, commit: bool) -> Result<(), HwError> {
        self.need("gpu")?;
        let level: &'static str = match GPU_LEVELS.iter().find(|l| **l == level) {
            Some(l) => l,
            None => return Err(HwError::InvalidArgs(format!("unknown GPU level {level:?}"))),
        };
        if !commit {
            return Ok(());
        }
        self.gpu_level = level;
        self.gpu.applied = true;
        self.gpu.overridden = false;
        self.gpu.mism = 0;
        let (lo, hi) = self.gpu_wanted();
        let r = self.write_gpu(ctx, "gpu", lo, hi);
        self.events.push(Ev::Props(Iface::GpuLevel));
        r?;
        self.events.push(Ev::Changed("gpu", "set".into()));
        Ok(())
    }

    pub fn set_gpu_clock(&mut self, ctx: &Ctx, mhz: u32, commit: bool) -> Result<(), HwError> {
        self.need("gpu")?;
        let (lo, hi) = self.gpu_bounds().unwrap_or((0, 0));
        if !(lo..=hi).contains(&mhz) {
            return Err(HwError::InvalidArgs(format!(
                "ManualGpuClock {mhz} MHz is outside {lo}..{hi} MHz"
            )));
        }
        if !commit {
            return Ok(());
        }
        self.gpu_clock = mhz;
        self.events.push(Ev::Props(Iface::GpuLevel));
        if self.gpu_level == "manual" {
            self.gpu.applied = true;
            self.gpu.overridden = false;
            self.gpu.mism = 0;
            self.write_gpu(ctx, "gpu", mhz, mhz)?;
        }
        self.events.push(Ev::Changed("gpu", "set".into()));
        Ok(())
    }

    pub fn set_range(&mut self, ctx: &Ctx, lo: u32, hi: u32, commit: bool) -> Result<(), HwError> {
        self.need("gpuRange")?;
        self.check_lease("gpuRange", ctx)?;
        let (flo, fhi) = self.gpu_bounds().unwrap_or((0, 0));
        if !(flo <= lo && lo <= hi && hi <= fhi) {
            return Err(HwError::InvalidArgs(format!(
                "range {lo}..{hi} MHz is not inside {flo}..{fhi} MHz with min <= max"
            )));
        }
        if !commit {
            return Ok(());
        }
        self.range = (lo, hi);
        self.range_ctl.lease = ((lo, hi) != (flo, fhi)).then(|| ctx.sender());
        self.range_ctl.overridden = false;
        self.range_ctl.mism = 0;
        self.events.push(Ev::Props(Iface::GpuRange));
        if self.gpu_level == "auto" {
            self.range_ctl.applied = true;
            self.write_gpu(ctx, "gpuRange", lo, hi)?;
        }
        self.events.push(Ev::Changed("gpuRange", "set".into()));
        Ok(())
    }

    pub fn gpu_profile(&self) -> Option<String> {
        let p = self.d.gpu.as_ref()?.profile.as_ref()?;
        bracketed(&self.sys.read(p)?).filter(|w| GPU_PROFILES.contains(&w.as_str()))
    }

    fn gprof_apply(&mut self, ctx: &Ctx, want: &'static str) -> Result<(), HwError> {
        let path = self
            .d
            .gpu
            .as_ref()
            .and_then(|g| g.profile.clone())
            .ok_or_else(|| HwError::NotSupported("kernel".into()))?;
        let cur = self.gpu_profile();
        if cur.as_deref() != Some(want) {
            if let Err(e) = self.write(&path, want, true) {
                eprintln!("oxp3-hwd: {}: {e}", path.display());
            }
            let got = self.gpu_profile();
            self.audit(
                ctx,
                "gpuPowerProfile",
                &path,
                cur.as_deref().unwrap_or("unreadable"),
                want,
                got.as_deref().unwrap_or("unreadable"),
            );
        }
        match self.gpu_profile() {
            Some(g) if g == want => Ok(()),
            got => Err(HwError::NotApplied(format!(
                "gpuPowerProfile={}",
                got.unwrap_or_else(|| "unreadable".into())
            ))),
        }
    }

    pub fn set_gpu_profile(&mut self, ctx: &Ctx, p: &str, commit: bool) -> Result<(), HwError> {
        self.need("gpuPowerProfile")?;
        let p: &'static str = match GPU_PROFILES.iter().find(|x| **x == p) {
            Some(x) => x,
            None => return Err(HwError::InvalidArgs(format!("unknown GPU power profile {p:?}"))),
        };
        if !commit {
            return Ok(());
        }
        self.gprof_target = Some(p);
        self.gprof.applied = true;
        self.gprof.overridden = false;
        self.gprof.mism = 0;
        let r = self.gprof_apply(ctx, p);
        self.events.push(Ev::Props(Iface::GpuProfile));
        r?;
        self.events.push(Ev::Changed("gpuPowerProfile", "set".into()));
        Ok(())
    }

    // ---- CPU energy preference (leased) -------------------------------------------------------

    pub fn core_types(&self) -> Vec<&'static str> {
        self.d.cpu.as_ref().map(|c| c.types.clone()).unwrap_or_default()
    }

    pub fn epp_available(&self) -> Vec<String> {
        self.d.cpu.as_ref().map(|c| c.epp_available.clone()).unwrap_or_default()
    }

    /// For each core type, the preference its first policy reads back.
    pub fn epp_by_type(&self) -> BTreeMap<String, String> {
        let mut out = BTreeMap::new();
        if let Some(c) = &self.d.cpu {
            for t in &c.types {
                if let Some(p) = c.of_type(t).next() {
                    if let Some(v) = self.sys.read(&p.epp()) {
                        out.insert(t.to_string(), v);
                    }
                }
            }
        }
        out
    }

    fn epp_default_by_type(&self) -> BTreeMap<String, String> {
        let mut out = BTreeMap::new();
        if let Some(c) = &self.d.cpu {
            for t in &c.types {
                if let Some(v) = c.of_type(t).next().and_then(|p| self.snap.epp.get(&p.name)) {
                    out.insert(t.to_string(), v.clone());
                }
            }
        }
        out
    }

    fn governor_performance(&self) -> bool {
        self.d
            .cpu
            .as_ref()
            .and_then(|c| c.policies.first())
            .and_then(|p| self.sys.read(&p.governor()))
            .as_deref()
            == Some("performance")
    }

    fn epp_apply(&mut self, ctx: &Ctx, target: &BTreeMap<String, String>) -> Result<(), HwError> {
        let Some(cpu) = self.d.cpu.clone() else {
            return Err(HwError::NotSupported("kernel".into()));
        };
        let mut bad = None;
        for p in &cpu.policies {
            let Some(want) = target.get(&p.name) else { continue };
            let cur = self.sys.read(&p.epp());
            if cur.as_deref() != Some(want.as_str()) {
                match self.write(&p.epp(), want, false) {
                    Ok(()) => {
                        let got = self.sys.read(&p.epp());
                        self.audit(
                            ctx,
                            "epp",
                            &p.epp(),
                            cur.as_deref().unwrap_or("unreadable"),
                            want,
                            got.as_deref().unwrap_or("unreadable"),
                        );
                    }
                    Err(e) => eprintln!("oxp3-hwd: {}: {e}", p.epp().display()),
                }
            }
            let got = self.sys.read(&p.epp());
            if got.as_deref() != Some(want.as_str()) && bad.is_none() {
                bad = Some(format!("epp={}", got.unwrap_or_else(|| "unreadable".into())));
            }
        }
        match bad {
            Some(m) => Err(HwError::NotApplied(m)),
            None => Ok(()),
        }
    }

    pub fn set_epp(&mut self, ctx: &Ctx, prefs: &BTreeMap<String, String>, commit: bool) -> Result<(), HwError> {
        if let Some(w) = self.cow.ppd {
            if !self.disabled {
                return Err(HwError::Busy(w.into()));
            }
        }
        self.need("epp")?;
        self.check_lease("epp", ctx)?;
        let cpu = self
            .d
            .cpu
            .clone()
            .ok_or_else(|| HwError::NotSupported("kernel".into()))?;
        if prefs.is_empty() {
            return Err(HwError::InvalidArgs("no preference given".into()));
        }
        if prefs.contains_key("all") && prefs.len() > 1 {
            return Err(HwError::InvalidArgs(
                "\"all\" cannot be combined with per-type keys".into(),
            ));
        }
        for (k, v) in prefs {
            if k != "all" && !cpu.types.contains(&k.as_str()) {
                return Err(HwError::InvalidArgs(format!("unknown core type {k:?}")));
            }
            if !cpu.epp_available.iter().any(|a| a == v) {
                return Err(HwError::InvalidArgs(format!("{v:?} is not offered by this CPU")));
            }
        }
        let mut target = self.epp_target.clone().unwrap_or_else(|| self.snap.epp.clone());
        for p in &cpu.policies {
            if let Some(v) = prefs.get("all").or_else(|| prefs.get(p.kind)) {
                target.insert(p.name.clone(), v.clone());
            }
        }
        let changes = cpu
            .policies
            .iter()
            .any(|p| target.get(&p.name) != self.sys.read(&p.epp()).as_ref());
        if changes && self.governor_performance() {
            return Err(HwError::Busy("governor".into()));
        }
        if !commit {
            return Ok(());
        }
        self.epp.lease = (target != self.snap.epp).then(|| ctx.sender());
        self.epp.overridden = false;
        self.epp.mism = 0;
        self.epp.applied = true;
        let r = self.epp_apply(ctx, &target);
        self.epp_target = Some(target);
        self.events.push(Ev::Props(Iface::CpuEnergy));
        r?;
        self.events.push(Ev::Changed("epp", "set".into()));
        Ok(())
    }

    // ---- CPU limits (leased) ------------------------------------------------------------------

    pub fn perf_read(&self) -> (Option<u32>, Option<u32>) {
        let c = self.d.cpu.as_ref();
        let rd = |p: Option<&PathBuf>| p.and_then(|p| self.sys.read_u64(p)).map(|v| v as u32);
        (
            rd(c.and_then(|c| c.max_perf.as_ref())),
            rd(c.and_then(|c| c.min_perf.as_ref())),
        )
    }

    pub fn caps_by_type(&self) -> BTreeMap<String, u32> {
        let mut out = BTreeMap::new();
        if let Some(c) = &self.d.cpu {
            for t in &c.types {
                if let Some(v) = c.of_type(t).next().and_then(|p| self.sys.read_u64(&p.scaling_max())) {
                    out.insert(t.to_string(), (v / 1000) as u32);
                }
            }
        }
        out
    }

    pub fn type_ranges(&self) -> BTreeMap<String, (u32, u32)> {
        let mut out = BTreeMap::new();
        if let Some(c) = &self.d.cpu {
            for t in &c.types {
                if let Some(r) = c.type_range_mhz(t) {
                    out.insert(t.to_string(), r);
                }
            }
        }
        out
    }

    fn lim_effective_min(&self) -> Option<u32> {
        let m = self.lim_min?;
        match self.snap.min_perf {
            Some(s) if m > s && self.ac != Some(true) => Some(s),
            _ => Some(m),
        }
    }

    /// The kernel's view of a requested scaling_max_freq: clamped to [scaling_min_freq, ceiling],
    /// the ceiling being base_frequency while no_turbo reads 1.
    fn cap_expected(&self, p: &discover::Policy, khz: u64) -> u64 {
        let lo = self.sys.read_u64(&p.dir.join("scaling_min_freq")).unwrap_or(0);
        let mut hi = p.max_khz;
        let no_turbo = self
            .d
            .cpu
            .as_ref()
            .and_then(|c| c.no_turbo.as_ref())
            .and_then(|n| self.sys.read(n));
        if no_turbo.as_deref() == Some("1") {
            if let Some(b) = p.base_khz {
                hi = hi.min(b);
            }
        }
        khz.clamp(lo, hi.max(lo))
    }

    fn lim_apply(&mut self, ctx: &Ctx) -> Result<(), HwError> {
        let Some(cpu) = self.d.cpu.clone() else {
            return Err(HwError::NotSupported("kernel".into()));
        };
        let (Some(mxp), Some(mnp)) = (cpu.max_perf.clone(), cpu.min_perf.clone()) else {
            return Err(HwError::NotSupported("kernel".into()));
        };
        let want_max = self.lim_max.or(self.snap.max_perf);
        if self.lim_max.is_some_and(|m| m < hard::MAX_PERF_PCT_MIN) {
            return Err(HwError::InvalidArgs("max_perf_pct below the hard limit".into()));
        }
        let want_min = self.lim_effective_min().or(self.snap.min_perf);
        let (cur_max, _) = self.perf_read();
        let rising = match (want_max, cur_max) {
            (Some(w), Some(c)) => w >= c,
            _ => true,
        };
        let order: Vec<(&PathBuf, Option<u32>, &str)> = if rising {
            vec![(&mxp, want_max, "max_perf_pct"), (&mnp, want_min, "min_perf_pct")]
        } else {
            vec![(&mnp, want_min, "min_perf_pct"), (&mxp, want_max, "max_perf_pct")]
        };
        let mut bad = None;
        for (path, want, _) in &order {
            let Some(want) = want else { continue };
            let cur = self.sys.read_u64(path);
            if cur != Some(*want as u64) {
                match self.write(path, &want.to_string(), false) {
                    Ok(()) => {
                        let got = self.sys.read_u64(path);
                        self.audit(ctx, "cpuLimits", path, &fmt_opt(cur), &want.to_string(), &fmt_opt(got));
                    }
                    Err(e) => eprintln!("oxp3-hwd: {}: {e}", path.display()),
                }
            }
        }
        for (path, want, _) in &order {
            if let Some(w) = want {
                let got = self.sys.read_u64(path);
                if got != Some(*w as u64) && bad.is_none() {
                    bad = Some(format!("cpuLimits={}", fmt_opt(got)));
                }
            }
        }
        for p in &cpu.policies {
            let want_khz = match self.lim_caps.get(p.kind) {
                Some(mhz) => *mhz as u64 * 1000,
                None => match self.snap.caps.get(&p.name) {
                    Some(k) => *k,
                    None => continue,
                },
            };
            let expect = self.cap_expected(p, want_khz);
            let cur = self.sys.read_u64(&p.scaling_max());
            if !close(cur, expect, CAP_TOL_KHZ) {
                match self.write(&p.scaling_max(), &want_khz.to_string(), false) {
                    Ok(()) => {
                        let got = self.sys.read_u64(&p.scaling_max());
                        self.audit(
                            ctx,
                            "cpuLimits",
                            &p.scaling_max(),
                            &fmt_opt(cur),
                            &want_khz.to_string(),
                            &fmt_opt(got),
                        );
                    }
                    Err(e) => eprintln!("oxp3-hwd: {}: {e}", p.scaling_max().display()),
                }
            }
            let got = self.sys.read_u64(&p.scaling_max());
            if !close(got, expect, CAP_TOL_KHZ) && bad.is_none() {
                bad = Some(format!(
                    "cpuLimits={}",
                    got.map(|g| (g / 1000).to_string())
                        .unwrap_or_else(|| "unreadable".into())
                ));
            }
        }
        match bad {
            Some(m) => Err(HwError::NotApplied(m)),
            None => Ok(()),
        }
    }

    pub fn set_cpu_limits(
        &mut self,
        ctx: &Ctx,
        max: Option<u32>,
        min: Option<u32>,
        caps: Option<BTreeMap<String, u32>>,
        commit: bool,
    ) -> Result<(), HwError> {
        self.need("cpuLimits")?;
        self.check_lease("cpuLimits", ctx)?;
        if max.is_none() && min.is_none() && caps.is_none() {
            return Err(HwError::InvalidArgs("no limit given".into()));
        }
        let e = &self.env;
        let new_max = max.or(self.lim_max).or(self.snap.max_perf).unwrap_or(100);
        if let Some(m) = max {
            if !(e.max_perf_pct_min..=100).contains(&m) {
                return Err(HwError::InvalidArgs(format!(
                    "max_perf_pct {m} is outside {}..100",
                    e.max_perf_pct_min
                )));
            }
        }
        let new_min = min.or(self.lim_min).or(self.snap.min_perf);
        if let Some(m) = min {
            if m == 0 || m > e.min_perf_pct_ac_max {
                return Err(HwError::InvalidArgs(format!(
                    "min_perf_pct {m} is outside 1..{}",
                    e.min_perf_pct_ac_max
                )));
            }
        }
        if let Some(m) = new_min {
            if m > new_max {
                return Err(HwError::InvalidArgs(format!(
                    "min_perf_pct {m} is above max_perf_pct {new_max}"
                )));
            }
        }
        let ranges = self.type_ranges();
        if let Some(c) = &caps {
            if c.is_empty() {
                return Err(HwError::InvalidArgs("core_type_max_freq is empty".into()));
            }
            for (t, mhz) in c {
                let Some((lo, hi)) = ranges.get(t) else {
                    return Err(HwError::InvalidArgs(format!("unknown core type {t:?}")));
                };
                if !(*lo..=*hi).contains(mhz) {
                    return Err(HwError::InvalidArgs(format!(
                        "{t}: {mhz} MHz is outside {lo}..{hi} MHz"
                    )));
                }
            }
        }
        if let (Some(m), Some(s)) = (min, self.snap.min_perf) {
            if m > s && self.ac != Some(true) {
                return Err(HwError::Busy("battery".into()));
            }
        }
        if !commit {
            return Ok(());
        }
        if max.is_some() {
            self.lim_max = max;
        }
        if min.is_some() {
            self.lim_min = min;
        }
        if let Some(c) = caps {
            self.lim_caps.extend(c);
        }
        let is_default = self.lim_max.is_none_or(|m| Some(m) == self.snap.max_perf)
            && self.lim_min.is_none_or(|m| Some(m) == self.snap.min_perf)
            && self.lim_caps_default();
        self.lim.lease = (!is_default).then(|| ctx.sender());
        self.lim.overridden = false;
        self.lim.mism = 0;
        self.lim.applied = true;
        self.lim_floor_capped = false;
        let r = self.lim_apply(ctx);
        self.events.push(Ev::Props(Iface::CpuLimits));
        r?;
        self.events.push(Ev::Changed("cpuLimits", "set".into()));
        Ok(())
    }

    fn lim_caps_default(&self) -> bool {
        let Some(cpu) = &self.d.cpu else { return true };
        self.lim_caps.iter().all(|(t, mhz)| {
            cpu.of_type(t).all(|p| {
                self.snap
                    .caps
                    .get(&p.name)
                    .map(|k| k.abs_diff(*mhz as u64 * 1000) <= CAP_TOL_KHZ)
                    .unwrap_or(false)
            })
        })
    }

    // ---- SoC slider (leased) ------------------------------------------------------------------

    pub fn soc_state(&self) -> (u32, u32, bool) {
        let (b, o) = self
            .soc_target
            .unwrap_or((self.snap.soc_balance.unwrap_or(0), self.snap.soc_offset.unwrap_or(0)));
        (b, o, self.soc_active())
    }

    pub fn soc_active(&self) -> bool {
        self.d.soc.as_ref().and_then(|s| self.sys.read(&s.profile)).as_deref() == Some("balanced")
    }

    fn soc_apply(&mut self, ctx: &Ctx, balance: u32, offset: u32) -> Result<(), HwError> {
        let Some(soc) = self.d.soc.clone() else {
            return Err(HwError::NotSupported("kernel".into()));
        };
        let mut failed = false;
        let cur_o = self.read_hex(&soc.offset);
        if cur_o != Some(offset) {
            match self.write(&soc.offset, &format!("{offset:02x}"), false) {
                Ok(()) => self.audit(
                    ctx,
                    "socBalance",
                    &soc.offset,
                    &fmt_opt(cur_o.map(u64::from)),
                    &offset.to_string(),
                    &fmt_opt(self.read_hex(&soc.offset).map(u64::from)),
                ),
                Err(e) => {
                    eprintln!("oxp3-hwd: {}: {e}", soc.offset.display());
                    failed = true;
                }
            }
        }
        let cur_b = self.read_hex(&soc.balance);
        // the kernel shows the balance in use until a profile write, so it is always written
        match self.write(&soc.balance, &format!("{balance:02x}"), false) {
            Ok(()) => self.audit(
                ctx,
                "socBalance",
                &soc.balance,
                &fmt_opt(cur_b.map(u64::from)),
                &balance.to_string(),
                "pending",
            ),
            Err(e) => {
                eprintln!("oxp3-hwd: {}: {e}", soc.balance.display());
                failed = true;
            }
        }
        let mut profile_written = false;
        if self.soc_active() {
            match self.write(&soc.profile, "balanced", false) {
                Ok(()) => profile_written = true,
                Err(e) => {
                    eprintln!("oxp3-hwd: {}: {e}", soc.profile.display());
                    failed = true;
                }
            }
        }
        let got_o = self.read_hex(&soc.offset);
        let got_b = self.read_hex(&soc.balance);
        if failed || got_o != Some(offset) || (profile_written && got_b != Some(balance)) {
            return Err(HwError::NotApplied(format!(
                "socBalance={}..{}",
                fmt_opt(got_b.map(u64::from)),
                fmt_opt(got_o.map(u64::from))
            )));
        }
        Ok(())
    }

    pub fn set_soc(&mut self, ctx: &Ctx, balance: u32, offset: u32, commit: bool) -> Result<(), HwError> {
        self.need("socBalance")?;
        self.check_lease("socBalance", ctx)?;
        let (lo, hi) = self.env.soc_balance;
        if !(lo..=hi).contains(&balance) {
            return Err(HwError::InvalidArgs(format!("balance {balance} is outside {lo}..{hi}")));
        }
        if offset > self.env.soc_offset_max {
            return Err(HwError::InvalidArgs(format!(
                "offset {offset} is outside 0..{}",
                self.env.soc_offset_max
            )));
        }
        if !commit {
            return Ok(());
        }
        self.soc_target = Some((balance, offset));
        let default = (self.snap.soc_balance, self.snap.soc_offset) == (Some(balance), Some(offset));
        self.soc.lease = (!default).then(|| ctx.sender());
        self.soc.overridden = false;
        self.soc.mism = 0;
        self.soc.applied = true;
        let r = self.soc_apply(ctx, balance, offset);
        self.events.push(Ev::Props(Iface::SocBalance));
        r?;
        self.events.push(Ev::Changed("socBalance", "set".into()));
        Ok(())
    }

    // ---- fan (leased; safety reverts) ---------------------------------------------------------

    pub fn fan_read_word(&self) -> Option<String> {
        let f = self.d.fan.as_ref()?;
        Some(
            match self.sys.read_u64(&f.enable)? {
                2 => "auto",
                0 => "max",
                1 => "quiet",
                _ => return None,
            }
            .to_string(),
        )
    }

    pub fn fan_state(&self) -> (String, String) {
        (
            self.fan_read_word().unwrap_or_else(|| self.fan_mode.to_string()),
            self.fan_last_revert.clone(),
        )
    }

    pub fn fan_mode(&self) -> &'static str {
        self.fan_mode
    }

    /// Write a mode and read pwm1_enable (and the duty) back: Err(observed word) on any failure.
    fn fan_write(&mut self, ctx: &Ctx, mode: &str) -> Result<(), String> {
        let Some(f) = self.d.fan.clone() else {
            return Err("unreadable".into());
        };
        let fan = &self.dev.fan;
        let cur = self.fan_read_word().unwrap_or_else(|| "unreadable".into());
        let steps: Vec<(&PathBuf, u32)> = match mode {
            "auto" => vec![(&f.enable, fan.enable_auto)],
            "quiet" if (hard::FAN_DUTY_MIN..=hard::FAN_DUTY_MAX).contains(&fan.quiet_pwm) => {
                vec![(&f.enable, fan.enable_manual), (&f.pwm, fan.quiet_pwm)]
            }
            "max" => vec![(&f.enable, fan.enable_max)],
            _ => return Err(cur),
        };
        for (path, v) in steps {
            if let Err(e) = self.write(path, &v.to_string(), false) {
                eprintln!("oxp3-hwd: {}: {e}", path.display());
                return Err(self.fan_read_word().unwrap_or_else(|| "unreadable".into()));
            }
        }
        let en = self.sys.read_u64(&f.enable);
        let ok = match mode {
            "auto" => en == Some(fan.enable_auto as u64),
            "quiet" => {
                en == Some(fan.enable_manual as u64) && close(self.sys.read_u64(&f.pwm), fan.quiet_pwm as u64, PWM_TOL)
            }
            // the driver reports 0 only at full duty, else 1
            _ => en == Some(fan.enable_max as u64) || en == Some(fan.enable_manual as u64),
        };
        let got = self.fan_read_word().unwrap_or_else(|| "unreadable".into());
        self.audit(ctx, "fan", &f.enable, &cur, mode, &got);
        if ok {
            Ok(())
        } else {
            Err(got)
        }
    }

    /// Validate a fan mode without writing (dbus.rs takes the inhibitor between check and commit).
    pub fn set_fan(&mut self, ctx: &Ctx, mode: &str, commit: bool) -> Result<(), HwError> {
        self.need("fan")?;
        self.check_lease("fan", ctx)?;
        let mode: &'static str = match self.env.fan_modes.iter().find(|m| **m == mode) {
            Some(m) => m,
            None => return Err(HwError::InvalidArgs(format!("unknown fan mode {mode:?}"))),
        };
        if mode == "quiet" {
            if let Some(until) = self.fan_cooldown_until {
                let now = Instant::now();
                if now < until {
                    let left = (until - now).as_secs_f64() / self.t.scale;
                    return Err(HwError::Busy(format!("cooldown:{}", left.ceil() as u64)));
                }
            }
        }
        // checked again at commit: logind may announce sleep while the call waits for its slot
        if mode != "auto" && self.sleeping {
            return Err(HwError::Busy("sleep".into()));
        }
        if !commit {
            return Ok(());
        }
        if mode != "auto" && self.fan_inhibitor.is_none() {
            // never non-auto without the sleep guard
            return Err(HwError::NotApplied("fan=auto".into()));
        }
        match self.fan_write(ctx, mode) {
            Ok(()) => {
                self.fan_mode = mode;
                self.fan.lease = (mode != "auto").then(|| ctx.sender());
                self.fan.overridden = false;
                self.fan.mism = 0;
                self.fan.applied = mode != "auto";
                if mode != "auto" {
                    self.fan_last_revert.clear();
                } else {
                    self.fan_inhibitor = None;
                }
                self.events.push(Ev::Props(Iface::FanMode));
                self.events.push(Ev::Changed("fan", "set".into()));
                Ok(())
            }
            Err(got) => {
                // anything that did not take: back to auto once more, and drop the hold
                let _ = self.fan_write(ctx, "auto");
                self.fan_mode = "auto";
                self.fan.lease = None;
                self.fan.applied = false;
                self.fan_inhibitor = None;
                self.events.push(Ev::Props(Iface::FanMode));
                Err(HwError::NotApplied(format!("fan={got}")))
            }
        }
    }

    /// The safety revert: fan back to auto, the hold and the inhibitor dropped.
    pub fn revert_fan(&mut self, why: &str) {
        let ctx = Ctx::provider();
        if self.fan_write(&ctx, "auto").is_err() {
            eprintln!("oxp3-hwd: fan revert ({why}): pwm1_enable does not read 2");
        }
        eprintln!("oxp3-hwd: fan back to auto ({why})");
        self.fan_mode = "auto";
        self.fan.lease = None;
        self.fan.applied = false;
        self.fan.overridden = false;
        self.fan_last_revert = why.to_string();
        if why == "temperature" {
            self.fan_cooldown_until = Some(Instant::now() + self.t.cooldown(self.dev.fan.cooldown_s));
        }
        self.fan_inhibitor = None;
        self.events.push(Ev::Props(Iface::FanMode));
        self.events.push(Ev::Changed("fan", format!("reverted-{why}")));
    }

    /// The 1 s fan guard: back to auto at or above the revert temperature or when the package
    /// temperature cannot be read. Returns true when it reverted.
    pub fn fan_guard(&mut self) -> bool {
        if self.fan_mode == "auto" {
            return false;
        }
        match discover::package_temp_c(&self.sys, &self.d) {
            None => {
                self.revert_fan("sensor");
                true
            }
            Some(c) if c >= self.dev.fan.revert_c.min(hard::FAN_REVERT_C_MAX) => {
                self.revert_fan("temperature");
                true
            }
            Some(_) => false,
        }
    }

    // ---- battery: charge limit (Steam's, persisted) and charge behaviour (leased) -------------

    pub fn charge_bounds(&self) -> (u32, u32) {
        (self.env.charge_min, self.env.charge_max)
    }

    pub fn charge_read(&self) -> Option<i64> {
        self.sys.read_i64(self.d.battery.as_ref()?.threshold.as_ref()?)
    }

    fn charge_limit_apply(&mut self, ctx: &Ctx, v: u32) -> Result<(), HwError> {
        if !(hard::CHARGE_MIN..=hard::CHARGE_MAX).contains(&v) {
            return Err(HwError::InvalidArgs(format!("{v} % is outside the hard limit")));
        }
        let path = self
            .d
            .battery
            .as_ref()
            .and_then(|b| b.threshold.clone())
            .ok_or_else(|| HwError::NotSupported("kernel".into()))?;
        let cur = self.charge_read();
        if cur != Some(v as i64) {
            if let Err(e) = self.write(&path, &v.to_string(), false) {
                eprintln!("oxp3-hwd: {}: {e}", path.display());
            }
            self.audit(
                ctx,
                "chargeLimit",
                &path,
                &fmt_opt_i(cur),
                &v.to_string(),
                &fmt_opt_i(self.charge_read()),
            );
        }
        match self.charge_read() {
            Some(g) if g == v as i64 => Ok(()),
            g => Err(HwError::NotApplied(format!("chargeLimit={}", fmt_opt_i(g)))),
        }
    }

    pub fn set_charge_limit(&mut self, ctx: &Ctx, level: i32, commit: bool) -> Result<(), HwError> {
        self.need("chargeLimit")?;
        let (lo, hi) = self.charge_bounds();
        // -1 means "no limit": 100, never 0 (which blocks charging on this hardware)
        let v = if level == -1 {
            self.dev.charge_default as i64
        } else {
            level.max(-2) as i64
        };
        if !(lo as i64..=hi as i64).contains(&v) {
            return Err(HwError::InvalidArgs(format!(
                "MaxChargeLevel {level} is outside {lo}..{hi} (or -1)"
            )));
        }
        let v = v as u32;
        if !commit {
            return Ok(());
        }
        self.charge_target = Some(v);
        self.charge.applied = true;
        self.charge.overridden = false;
        self.charge.mism = 0;
        let r = self.charge_limit_apply(ctx, v);
        if r.is_ok() {
            self.persist_charge(v);
        }
        self.events.push(Ev::Props(Iface::ChargeLimit));
        r?;
        self.events.push(Ev::Changed("chargeLimit", "set".into()));
        Ok(())
    }

    /// The device's behaviours that the kernel also lists, in the device's order.
    pub fn behaviour_choices(&self) -> Vec<&'static str> {
        let listed = self
            .d
            .battery
            .as_ref()
            .and_then(|b| b.behaviour.as_ref())
            .and_then(|p| self.sys.read(p))
            .map(|t| choice_words(&t))
            .unwrap_or_default();
        self.env
            .charge_behaviours
            .iter()
            .copied()
            .filter(|b| listed.iter().any(|l| l == b))
            .collect()
    }

    pub fn beh_read(&self) -> Option<String> {
        let p = self.d.battery.as_ref()?.behaviour.as_ref()?;
        bracketed(&self.sys.read(p)?)
    }

    fn beh_apply(&mut self, ctx: &Ctx, b: &str) -> Result<(), HwError> {
        let path = self
            .d
            .battery
            .as_ref()
            .and_then(|x| x.behaviour.clone())
            .ok_or_else(|| HwError::NotSupported("kernel".into()))?;
        // there is no code path for inhibit-charge or force-discharge
        if b != "auto" && b != "inhibit-charge-awake" {
            return Err(HwError::InvalidArgs(format!("{b:?} is never written")));
        }
        let cur = self.beh_read();
        if cur.as_deref() != Some(b) {
            if let Err(e) = self.write(&path, b, false) {
                eprintln!("oxp3-hwd: {}: {e}", path.display());
            }
            self.audit(
                ctx,
                "chargeBehaviour",
                &path,
                cur.as_deref().unwrap_or("unreadable"),
                b,
                self.beh_read().as_deref().unwrap_or("unreadable"),
            );
        }
        match self.beh_read() {
            Some(g) if g == b => Ok(()),
            g => Err(HwError::NotApplied(format!(
                "chargeBehaviour={}",
                g.unwrap_or_else(|| "unreadable".into())
            ))),
        }
    }

    pub fn beh_value(&self) -> &'static str {
        self.beh_value
    }

    pub fn set_behaviour(&mut self, ctx: &Ctx, b: &str, commit: bool) -> Result<(), HwError> {
        self.need("chargeBehaviour")?;
        self.check_lease("chargeBehaviour", ctx)?;
        let choices = self.behaviour_choices();
        let b: &'static str = match choices.iter().find(|c| **c == b) {
            Some(c) => c,
            None => return Err(HwError::InvalidArgs(format!("charge behaviour {b:?} is not offered"))),
        };
        if !commit {
            return Ok(());
        }
        let r = self.beh_apply(ctx, b);
        if r.is_ok() || b == "auto" {
            self.beh_value = b;
            self.beh.lease = (b != "auto").then(|| ctx.sender());
            self.beh.overridden = false;
            self.beh.mism = 0;
            self.beh.applied = b != "auto";
        }
        self.events.push(Ev::Props(Iface::ChargeBehaviour));
        r?;
        self.events.push(Ev::Changed("chargeBehaviour", "set".into()));
        Ok(())
    }

    // ---- package power ------------------------------------------------------------------------

    /// One fresh sample per second across all callers; a window of at least 1 s; 100 mW steps.
    pub fn sample(&mut self) -> Result<(u32, u32), HwError> {
        self.need("packagePower")?;
        let e = self
            .d
            .energy
            .clone()
            .ok_or_else(|| HwError::NotSupported("kernel".into()))?;
        let now = Instant::now();
        if let Some((mw, win, at)) = self.pkg_last {
            if now.duration_since(at) < Duration::from_secs(1) {
                return Ok((mw, win));
            }
        }
        let base = match self.pkg_base {
            Some(b) => b,
            None => {
                let v = self
                    .sys
                    .read_u64(&e.energy_uj)
                    .ok_or_else(|| HwError::NotSupported("grants".into()))?;
                (v, now)
            }
        };
        let elapsed = now.duration_since(base.1);
        if elapsed < Duration::from_secs(1) {
            std::thread::sleep(Duration::from_secs(1) - elapsed);
        }
        let now = Instant::now();
        let v = self
            .sys
            .read_u64(&e.energy_uj)
            .ok_or_else(|| HwError::NotSupported("grants".into()))?;
        let range = self.sys.read_u64(&e.max_range_uj).unwrap_or(0);
        let delta = if v >= base.0 {
            v - base.0
        } else {
            v + range.saturating_sub(base.0)
        };
        let us = now.duration_since(base.1).as_micros().max(1) as u64;
        let mw = (delta as u128 * 1000 / us as u128) as u64;
        let mw = (mw + 50) / 100 * 100;
        let win = (us / 1000) as u32;
        let mw = mw.min(u32::MAX as u64) as u32;
        self.pkg_base = Some((v, now));
        self.pkg_last = Some((mw, win, now));
        Ok((mw, win))
    }

    // ---- Describe, Release, TakeControl, disconnects -----------------------------------------

    pub fn describe(&self, knob: &str) -> Result<BTreeMap<&'static str, zbus::zvariant::OwnedValue>, HwError> {
        use zbus::zvariant::{OwnedValue, Value};
        fn ov<'a, V: Into<Value<'a>>>(v: V) -> OwnedValue {
            OwnedValue::try_from(v.into()).expect("no file descriptors in Describe")
        }
        if !KNOBS.contains(&knob) {
            return Err(HwError::InvalidArgs(format!("unknown knob {knob:?}")));
        }
        // the same view as the Knobs property (refreshed on every availability change)
        let r = self.avail.get(knob).cloned().unwrap_or_else(|| self.reason(knob));
        let mut m = BTreeMap::new();
        m.insert("available", ov(r == Reason::Ok));
        m.insert("reason", ov(r.word()));
        m.insert("overridden", ov(self.ctl(knob).map(|c| c.overridden).unwrap_or(false)));
        if LEASED.contains(&knob) {
            m.insert("lease", ov(self.lease_of(knob).unwrap_or_default()));
            m.insert("persistence", ov("lease"));
        } else {
            m.insert(
                "persistence",
                ov(if knob == "chargeLimit" { "persisted" } else { "volatile" }),
            );
        }
        let src = &self.dev.sources;
        match knob {
            "tdp" => {
                let (lo, hi) = self.tdp_bounds();
                m.insert("unit", ov("W"));
                m.insert("min", ov(lo));
                m.insert("max", ov(hi));
                m.insert("value", ov(self.tdp_value()));
                m.insert(
                    "effective",
                    ov(self
                        .tdp_target
                        .map(|t| self.tdp_effective(t).0)
                        .unwrap_or_else(|| self.tdp_value())),
                );
                m.insert(
                    "source",
                    ov(if self.env.sustained_ac_max_w.is_some() {
                        src.sustained_ac
                    } else {
                        src.sustained
                    }),
                );
            }
            "gpu" => {
                let (lo, hi) = self.gpu_bounds().unwrap_or((0, 0));
                m.insert("unit", ov("MHz"));
                m.insert("min", ov(lo));
                m.insert("max", ov(hi));
                m.insert("values", ov(GPU_LEVELS.to_vec()));
                m.insert("value", ov(self.gpu_level));
                m.insert("source", ov(src.gpu));
            }
            "gpuPowerProfile" => {
                m.insert("unit", ov("enum"));
                m.insert("values", ov(GPU_PROFILES.to_vec()));
                if let Some(p) = self.gpu_profile() {
                    m.insert("value", ov(p));
                }
                m.insert("source", ov(src.gpu));
            }
            "chargeLimit" => {
                let (lo, hi) = self.charge_bounds();
                m.insert("unit", ov("percent"));
                m.insert("min", ov(lo));
                m.insert("max", ov(hi));
                if let Some(v) = self.charge_read() {
                    m.insert("value", ov(v.clamp(0, 100) as u32));
                }
                m.insert("source", ov(src.charge));
            }
            "boost" => {
                m.insert("unit", ov("W"));
                m.insert("min", ov(self.env.boost_min_w));
                m.insert("max", ov(self.env.boost_max_w));
                m.insert("values", ov(BOOST_MODES.to_vec()));
                m.insert("value", ov(self.boost_mode));
                m.insert("effective", ov(self.boost_effective()));
                m.insert("default", ov("firmware"));
                m.insert("source", ov(src.boost));
            }
            "epp" => {
                m.insert("unit", ov("enum"));
                m.insert("values", ov(self.epp_available()));
                m.insert(
                    "value",
                    ov(self
                        .epp_by_type()
                        .into_iter()
                        .collect::<std::collections::HashMap<_, _>>()),
                );
                m.insert(
                    "default",
                    ov(self
                        .epp_default_by_type()
                        .into_iter()
                        .collect::<std::collections::HashMap<_, _>>()),
                );
                m.insert("source", ov(src.epp));
            }
            "cpuLimits" => {
                m.insert("unit", ov("percent"));
                m.insert("min", ov(self.env.max_perf_pct_min));
                m.insert("max", ov(100u32));
                if let Some(v) = self.perf_read().0 {
                    m.insert("value", ov(v));
                }
                m.insert("default", ov(self.snap.max_perf.unwrap_or(100)));
                m.insert("source", ov(src.cpu_limits));
            }
            "socBalance" => {
                let (b, o, _) = self.soc_state();
                m.insert("unit", ov("level"));
                m.insert("min", ov(self.env.soc_balance.0));
                m.insert("max", ov(self.env.soc_balance.1));
                m.insert("value", ov((b, o)));
                m.insert(
                    "default",
                    ov((self.snap.soc_balance.unwrap_or(0), self.snap.soc_offset.unwrap_or(0))),
                );
                m.insert("source", ov(src.soc));
            }
            "gpuRange" => {
                let (lo, hi) = self.gpu_bounds().unwrap_or((0, 0));
                m.insert("unit", ov("MHz"));
                m.insert("min", ov(lo));
                m.insert("max", ov(hi));
                m.insert("value", ov(self.range));
                m.insert("default", ov((lo, hi)));
                m.insert("source", ov(src.gpu));
            }
            "fan" => {
                m.insert("unit", ov("enum"));
                m.insert("values", ov(self.env.fan_modes.clone()));
                m.insert("value", ov(self.fan_state().0));
                m.insert("default", ov("auto"));
                m.insert("source", ov(src.fan));
            }
            "chargeBehaviour" => {
                m.insert("unit", ov("enum"));
                m.insert("values", ov(self.behaviour_choices()));
                if let Some(v) = self.beh_read() {
                    m.insert("value", ov(v));
                }
                m.insert("default", ov("auto"));
                m.insert("source", ov(src.charge_behaviour));
            }
            "packagePower" => {
                m.insert("unit", ov("mW"));
                m.insert("value", ov(self.pkg_last.map(|p| p.0).unwrap_or(0)));
                m.insert("source", ov(src.package_power));
            }
            _ => {}
        }
        Ok(m)
    }

    /// Write a leased knob's default (Release, a lease end, stop).
    fn write_default(&mut self, ctx: &Ctx, knob: &str) {
        let r = match knob {
            "boost" => {
                self.boost_mode = "firmware";
                self.boost_offset = 0;
                self.boost_limit = self.env.boost_default_w;
                let pl2 = self.boost_effective();
                if self.reason("boost") == Reason::Ok {
                    self.apply_power(ctx, None, pl2)
                } else {
                    Ok(())
                }
            }
            "epp" => {
                let t = self.snap.epp.clone();
                self.epp_target = None;
                if self.reason("epp") == Reason::Ok {
                    self.epp_apply(ctx, &t)
                } else {
                    Ok(())
                }
            }
            "cpuLimits" => {
                self.lim_max = None;
                self.lim_min = None;
                self.lim_caps.clear();
                if self.reason("cpuLimits") == Reason::Ok {
                    self.lim_apply(ctx)
                } else {
                    Ok(())
                }
            }
            "socBalance" => {
                self.soc_target = None;
                match (self.snap.soc_balance, self.snap.soc_offset) {
                    (Some(b), Some(o)) if self.reason("socBalance") == Reason::Ok => self.soc_apply(ctx, b, o),
                    _ => Ok(()),
                }
            }
            "gpuRange" => {
                self.range = self.gpu_bounds().unwrap_or((0, 0));
                if self.gpu_level == "auto" && self.range_ctl.applied && self.reason("gpuRange") == Reason::Ok {
                    let (lo, hi) = self.range;
                    self.write_gpu(ctx, "gpuRange", lo, hi)
                } else {
                    Ok(())
                }
            }
            "fan" => {
                self.fan_mode = "auto";
                self.fan_inhibitor = None;
                self.fan_write(ctx, "auto")
                    .map_err(|g| HwError::NotApplied(format!("fan={g}")))
            }
            "chargeBehaviour" => {
                self.beh_value = "auto";
                if self.d.battery.as_ref().and_then(|b| b.behaviour.as_ref()).is_some() {
                    self.beh_apply(ctx, "auto")
                } else {
                    Ok(())
                }
            }
            _ => Ok(()),
        };
        if let Err(e) = r {
            eprintln!("oxp3-hwd: {knob}: default not applied: {e}");
        }
        if let Some(c) = self.ctl_mut(knob) {
            c.applied = false;
            c.mism = 0;
        }
        self.events.push(Ev::Props(iface_of(knob)));
    }

    fn end_lease(&mut self, ctx: &Ctx, knob: &'static str, reason: &str) {
        let overridden = self.ctl(knob).map(|c| c.overridden).unwrap_or(false);
        if let Some(c) = self.ctl_mut(knob) {
            c.lease = None;
        }
        // fan and charge behaviour always return to their defaults (safety); the others only
        // while the provider still owns the value
        if !overridden || knob == "fan" || knob == "chargeBehaviour" {
            self.write_default(ctx, knob);
        }
        if let Some(c) = self.ctl_mut(knob) {
            c.overridden = false;
        }
        if knob == "fan" && reason == "reverted-disconnect" {
            self.fan_last_revert = "disconnect".into();
            self.events.push(Ev::Props(Iface::FanMode));
        }
        self.events.push(Ev::Changed(knob, reason.into()));
    }

    pub fn release(&mut self, ctx: &Ctx, knobs: &[String]) -> Result<(), HwError> {
        let mut list: Vec<&'static str> = Vec::new();
        for k in knobs {
            match LEASED.iter().find(|l| **l == k) {
                Some(l) => list.push(l),
                None => return Err(HwError::InvalidArgs(format!("{k:?} is not a leased knob"))),
            }
        }
        if knobs.is_empty() {
            list = LEASED.to_vec();
        }
        for k in list {
            if self.lease_of(k).is_some() && self.lease_of(k) == ctx.sender {
                self.end_lease(ctx, k, "released");
            }
        }
        Ok(())
    }

    pub fn take_control(&mut self, ctx: &Ctx, knobs: &[String]) -> Result<(), HwError> {
        if knobs.is_empty() {
            return Err(HwError::InvalidArgs("no knob given".into()));
        }
        let mut list: Vec<&'static str> = Vec::new();
        for k in knobs {
            match KNOBS.iter().find(|x| **x == k && **x != "packagePower") {
                Some(x) => list.push(x),
                None => return Err(HwError::InvalidArgs(format!("unknown knob {k:?}"))),
            }
        }
        for k in &list {
            self.check_lease(k, ctx)?;
        }
        for k in list {
            if let Some(c) = self.ctl_mut(k) {
                c.overridden = false;
                c.mism = 0;
            }
            let before = self.writes;
            if let Err(e) = self.reassert_knob(ctx, k) {
                eprintln!("oxp3-hwd: take control of {k}: {e}");
            }
            if self.writes != before {
                self.events.push(Ev::Changed(k, "set".into()));
            }
            self.events.push(Ev::Props(iface_of(k)));
        }
        Ok(())
    }

    /// A unique name vanished: every lease it held ends and its knob returns to the default.
    pub fn disconnected(&mut self, name: &str) {
        let ctx = Ctx::provider();
        for k in LEASED {
            if self.lease_of(k).as_deref() == Some(name) {
                eprintln!("oxp3-hwd: {name} left: {k} back to its default");
                self.end_lease(&ctx, k, "reverted-disconnect");
            }
        }
    }

    /// SIGTERM and the kill switch: fan and charge behaviour back to auto, every lease ended.
    pub fn stop(&mut self) {
        let ctx = Ctx::provider();
        if self.fan_mode != "auto" || self.fan.lease.is_some() {
            self.revert_fan("stop");
        }
        for k in LEASED {
            if k == "fan" {
                continue;
            }
            if self.lease_of(k).is_some() || (k == "chargeBehaviour" && self.beh_value != "auto") {
                self.end_lease(&ctx, k, "reverted-stop");
            }
        }
    }

    // ---- held values: re-assertion and drift --------------------------------------------------

    /// Write a knob's held value again (only what differs is written).
    fn reassert_knob(&mut self, ctx: &Ctx, knob: &str) -> Result<(), HwError> {
        match knob {
            "tdp" => {
                if let Some(t) = self.tdp_target {
                    let (eff, capped) = self.tdp_effective(t);
                    let pl2 = self.pl2_for(eff);
                    if capped != self.tdp_capped {
                        self.tdp_capped = capped;
                        self.events.push(Ev::Changed(
                            "tdp",
                            if capped { "capped-battery" } else { "reasserted-ac" }.into(),
                        ));
                    }
                    return self.apply_power(ctx, Some(eff), pl2);
                }
                Ok(())
            }
            "boost" if self.boost.applied || self.boost.lease.is_some() => {
                let pl2 = self.boost_effective();
                self.apply_power(ctx, None, pl2)
            }
            "gpu" if self.gpu.applied => {
                let (lo, hi) = self.gpu_wanted();
                self.write_gpu(ctx, "gpu", lo, hi)
            }
            "gpuRange" if self.range_ctl.applied && self.gpu_level == "auto" => {
                let (lo, hi) = self.range;
                self.write_gpu(ctx, "gpuRange", lo, hi)
            }
            "gpuPowerProfile" => match self.gprof_target {
                Some(p) => self.gprof_apply(ctx, p),
                None => Ok(()),
            },
            "epp" => match self.epp_target.clone() {
                Some(t) if self.cow.ppd.is_none() => self.epp_apply(ctx, &t),
                _ => Ok(()),
            },
            "cpuLimits" if self.lim.applied => {
                let capped =
                    matches!((self.lim_min, self.snap.min_perf), (Some(m), Some(s)) if m > s && self.ac != Some(true));
                if capped != self.lim_floor_capped {
                    self.lim_floor_capped = capped;
                    self.events.push(Ev::Changed(
                        "cpuLimits",
                        if capped { "capped-battery" } else { "reasserted-ac" }.into(),
                    ));
                    self.events.push(Ev::Props(Iface::CpuLimits));
                }
                self.lim_apply(ctx)
            }
            "socBalance" => match self.soc_target {
                Some((b, o)) => self.soc_apply(ctx, b, o),
                None => Ok(()),
            },
            "fan" if self.fan_mode != "auto" => {
                let m = self.fan_mode;
                self.fan_write(ctx, m)
                    .map_err(|g| HwError::NotApplied(format!("fan={g}")))
            }
            "chargeLimit" => match self.charge_target {
                Some(v) => self.charge_limit_apply(ctx, v),
                None => Ok(()),
            },
            "chargeBehaviour" if self.beh_value != "auto" => {
                let b = self.beh_value;
                self.beh_apply(ctx, b)
            }
            _ => Ok(()),
        }
    }

    /// After an AC change (kind "ac") or a resume ("resume"): every held, not overridden value is
    /// written again where it differs.
    fn reassert(&mut self, kind: &'static str) {
        let ctx = Ctx::provider();
        let order = [
            "tdp",
            "boost",
            "gpu",
            "gpuRange",
            "gpuPowerProfile",
            "epp",
            "cpuLimits",
            "socBalance",
            "fan",
            "chargeLimit",
            "chargeBehaviour",
        ];
        for k in order {
            if self.ctl(k).map(|c| c.overridden).unwrap_or(true) || self.reason(k) != Reason::Ok {
                continue;
            }
            if k == "chargeLimit" && kind != "resume" {
                continue;
            }
            let before = self.writes;
            if let Err(e) = self.reassert_knob(&ctx, k) {
                eprintln!("oxp3-hwd: re-assert {k} after {kind}: {e}");
            }
            if self.writes != before {
                self.events.push(Ev::Changed(k, format!("reasserted-{kind}")));
                self.events.push(Ev::Props(iface_of(k)));
            }
        }
    }

    /// Whether a held knob reads what the provider holds. None: nothing held or not checkable.
    fn matches(&self, knob: &str) -> Option<bool> {
        let s = &self.sys;
        match knob {
            "tdp" => {
                let t = self.tdp_target?;
                let eff = self.tdp_effective(t).0 as u64 * 1_000_000;
                Some(self.d.zones.iter().all(|z| close(s.read_u64(&z.pl1), eff, RAPL_TOL_UW)))
            }
            "boost" if self.boost.applied => {
                let pl2 = self.boost_effective() as u64 * 1_000_000;
                Some(self.d.zones.iter().all(|z| close(s.read_u64(&z.pl2), pl2, RAPL_TOL_UW)))
            }
            // never polled: reading min_freq/max_freq takes a runtime-PM reference and wakes the
            // GPU (xe_gt_freq.c), and xe keeps the user's range across GT resets and resume. They
            // are re-asserted after AC changes and resume, and read back after every write.
            "gpu" | "gpuRange" => None,
            "gpuPowerProfile" => Some(self.gpu_profile().as_deref() == Some(self.gprof_target?)),
            "epp" => {
                let t = self.epp_target.as_ref()?;
                let c = self.d.cpu.as_ref()?;
                Some(c.types.iter().all(|ty| {
                    match c.of_type(ty).next() {
                        Some(p) => t
                            .get(&p.name)
                            .map(|w| s.read(&p.epp()).as_ref() == Some(w))
                            .unwrap_or(true),
                        None => true,
                    }
                }))
            }
            "cpuLimits" if self.lim.applied => {
                let (mx, mn) = self.perf_read();
                let ok_max = self
                    .lim_max
                    .or(self.snap.max_perf)
                    .map(|w| mx == Some(w))
                    .unwrap_or(true);
                let ok_min = self
                    .lim_effective_min()
                    .or(self.snap.min_perf)
                    .map(|w| mn == Some(w))
                    .unwrap_or(true);
                let c = self.d.cpu.as_ref()?;
                let ok_caps = c.types.iter().all(|ty| {
                    let Some(p) = c.of_type(ty).next() else { return true };
                    let want = match self.lim_caps.get(*ty) {
                        Some(m) => *m as u64 * 1000,
                        None => match self.snap.caps.get(&p.name) {
                            Some(k) => *k,
                            None => return true,
                        },
                    };
                    close(s.read_u64(&p.scaling_max()), self.cap_expected(p, want), CAP_TOL_KHZ)
                });
                Some(ok_max && ok_min && ok_caps)
            }
            "socBalance" => {
                let (b, o) = self.soc_target?;
                let soc = self.d.soc.as_ref()?;
                let ok_o = self.read_hex(&soc.offset) == Some(o);
                let ok_b = !self.soc_active() || self.read_hex(&soc.balance) == Some(b);
                Some(ok_o && ok_b)
            }
            "fan" if self.fan_mode != "auto" => Some(self.fan_read_word().as_deref() != Some("auto")),
            "chargeLimit" => Some(self.charge_read() == Some(self.charge_target? as i64)),
            "chargeBehaviour" if self.beh_value != "auto" => Some(self.beh_read().as_deref() == Some(self.beh_value)),
            _ => None,
        }
    }

    /// thermald may lower RAPL limits; such a lower PL1 is never fought.
    fn thermald_lowered(&self) -> bool {
        if !self.cow.thermald {
            return false;
        }
        let Some(t) = self.tdp_target else { return false };
        let eff = self.tdp_effective(t).0 as u64 * 1_000_000;
        self.d.zones.iter().any(|z| {
            self.sys
                .read_u64(&z.pl1)
                .map(|v| v + RAPL_TOL_UW < eff)
                .unwrap_or(false)
        })
    }

    fn mark_overridden(&mut self, knob: &'static str, by: &str) {
        if let Some(c) = self.ctl_mut(knob) {
            c.overridden = true;
            c.mism = 0;
        }
        eprintln!("oxp3-hwd: {knob} overridden by another writer{by}: not re-asserted until TakeControl or a new set");
        self.events.push(Ev::Changed(knob, "overridden".into()));
        self.events.push(Ev::Props(iface_of(knob)));
    }

    fn drift(&mut self, now: Instant) {
        let in_grace = self.grace_until.map(|g| now < g).unwrap_or(false);
        let ctx = Ctx::provider();
        for k in [
            "tdp",
            "boost",
            "gpu",
            "gpuRange",
            "gpuPowerProfile",
            "epp",
            "cpuLimits",
            "socBalance",
            "fan",
            "chargeLimit",
            "chargeBehaviour",
        ] {
            if self.ctl(k).map(|c| c.overridden).unwrap_or(true) || self.reason(k) != Reason::Ok {
                continue;
            }
            match self.matches(k) {
                None | Some(true) => {
                    if let Some(c) = self.ctl_mut(k) {
                        c.mism = 0;
                    }
                }
                Some(false) => {
                    if k == "tdp" && self.thermald_lowered() {
                        self.mark_overridden(k, " (thermald is active)");
                        continue;
                    }
                    if in_grace && self.grace_reasserts < GRACE_REASSERTS && k != "fan" {
                        self.grace_reasserts += 1;
                        let before = self.writes;
                        let _ = self.reassert_knob(&ctx, k);
                        if self.writes != before {
                            let kind = self.grace_kind;
                            self.events.push(Ev::Changed(k, format!("reasserted-{kind}")));
                            self.events.push(Ev::Props(iface_of(k)));
                        }
                        continue;
                    }
                    let c = self.ctl_mut(k).expect("held knob");
                    c.mism += 1;
                    if c.mism >= 2 {
                        self.mark_overridden(k, "");
                        if k == "fan" {
                            // another writer set auto: nothing to guard any more
                            self.fan_inhibitor = None;
                        }
                    }
                }
            }
        }
    }

    pub fn resumed(&mut self, now: Instant) {
        self.sleeping = false;
        self.reassert_at = Some((now + self.t.resume_delay(), "resume"));
        self.grace_until = Some(now + self.t.grace());
        self.grace_kind = "resume";
        self.grace_reasserts = 0;
    }

    /// The 1 s tick: fan guard, AC, scheduled re-assertion, drift, availability. Returns false when
    /// the kill switch appeared (the caller then stops).
    pub fn tick(&mut self, now: Instant) -> bool {
        if self.paths.etc.join("disabled").exists() {
            self.disabled = true;
            return false;
        }
        self.fan_guard();
        if now >= self.next_ac_poll {
            self.next_ac_poll = now + self.t.ac_poll();
            let ac = discover::ac_online(&self.sys, &self.d);
            if ac != self.ac {
                eprintln!("oxp3-hwd: power source changed: ac={ac:?}");
                self.ac = ac;
                // the debounce runs from the last change: a flapping charger gives one re-assert
                self.reassert_at = Some((now + self.t.ac_debounce(), "ac"));
                self.grace_until = Some(now + self.t.grace());
                self.grace_kind = "ac";
                self.grace_reasserts = 0;
            }
        }
        match self.reassert_at {
            Some((at, kind)) if now >= at => {
                self.reassert_at = None;
                self.reassert(kind);
            }
            Some(_) => {}
            None => {
                if now >= self.next_drift {
                    self.next_drift = now + self.t.drift_every();
                    self.drift(now);
                }
            }
        }
        if now >= self.next_rediscover {
            self.next_rediscover = now + self.t.rediscover();
            self.rediscover();
            self.refresh_avail();
        }
        true
    }

    /// Parts that were missing at start (an oxpec battery extension registered late, for example)
    /// are looked for again; parts already resolved keep their paths.
    fn rediscover(&mut self) {
        let need = self
            .d
            .battery
            .as_ref()
            .map(|b| b.threshold.is_none() || b.behaviour.is_none())
            .unwrap_or(true)
            || self.d.fan.is_none()
            || self.d.temp.is_none();
        if !need {
            return;
        }
        let fresh = discover::discover(&self.sys);
        if self.d.fan.is_none() {
            self.d.fan = fresh.fan;
        }
        if self.d.temp.is_none() {
            self.d.temp = fresh.temp;
        }
        let fill = match (&self.d.battery, &fresh.battery) {
            (None, Some(_)) => true,
            (Some(a), Some(b)) => a.name == b.name && (a.threshold.is_none() || a.behaviour.is_none()),
            _ => false,
        };
        if fill {
            self.d.battery = fresh.battery;
        }
    }
}

pub fn iface_of(knob: &str) -> Iface {
    match knob {
        "tdp" => Iface::TdpLimit,
        "boost" => Iface::PowerLimits,
        "gpu" => Iface::GpuLevel,
        "gpuPowerProfile" => Iface::GpuProfile,
        "gpuRange" => Iface::GpuRange,
        "epp" => Iface::CpuEnergy,
        "cpuLimits" => Iface::CpuLimits,
        "socBalance" => Iface::SocBalance,
        "fan" => Iface::FanMode,
        "chargeLimit" => Iface::ChargeLimit,
        "chargeBehaviour" => Iface::ChargeBehaviour,
        _ => Iface::Provider,
    }
}

fn fmt_opt(v: Option<u64>) -> String {
    v.map(|v| v.to_string()).unwrap_or_else(|| "unreadable".into())
}

fn fmt_opt_i(v: Option<i64>) -> String {
    v.map(|v| v.to_string()).unwrap_or_else(|| "unreadable".into())
}

/// Write a provider-owned state file (never sysfs): temporary file, then rename.
pub fn write_atomic(path: &Path, data: &[u8]) {
    let Some(dir) = path.parent() else { return };
    let tmp = dir.join(format!(
        ".{}.tmp",
        path.file_name().and_then(|n| n.to_str()).unwrap_or("state")
    ));
    let r = std::fs::write(&tmp, data).and_then(|_| std::fs::rename(&tmp, path));
    if let Err(e) = r {
        eprintln!("oxp3-hwd: cannot write {}: {e}", path.display());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn watts_text() {
        assert_eq!(w_text(Some(35_000_000)), "35");
        assert_eq!(w_text(Some(34_875_000)), "34.875");
        assert_eq!(w_text(Some(904_000_000)), "904");
        assert_eq!(w_text(Some(250_000)), "0.25");
        assert_eq!(w_text(None), "unreadable");
    }

    #[test]
    fn reasons_words() {
        assert_eq!(
            Reason::Busy("power-profiles-daemon").word(),
            "busy:power-profiles-daemon"
        );
        assert_eq!(Reason::Ok.word(), "");
        assert_eq!(Reason::Grants.word(), "grants");
    }
}
