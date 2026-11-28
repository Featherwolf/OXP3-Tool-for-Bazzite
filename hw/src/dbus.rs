// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! The Hw1 interfaces (schemas/hw1.xml, ApiVersion 1) and the steamos-manager remotes, on the
//! system bus. Each call: per-sender rate limit, authorization (setters, Release, TakeControl,
//! remote property sets, Sample), the kill switch, validation, the knob's write slot, the write
//! under the hardware lock, then signals and PropertiesChanged after the lock is released.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use futures_util::StreamExt;
use zbus::fdo;
use zbus::message::Header;
use zbus::object_server::SignalEmitter;
use zbus::zvariant::OwnedValue;
use zbus::{interface, Connection};

use crate::error::HwError;
use crate::hw::{Ctx, Ev, Hw, Iface, GPU_LEVELS, GPU_PROFILES};
use crate::rate::{Gates, SenderRate, Turn};

pub const BUS_NAME: &str = "io.github.featherwolf.Bathysphere.Hw1";
pub const PATH: &str = "/io/github/featherwolf/Bathysphere/Hw1";
pub const SOM_PATH: &str = "/io/github/featherwolf/Bathysphere/Hw1/SteamOSManager";
pub const API_VERSION: u32 = 1;
pub const PROVIDER_NAME: &str = "oxp3-hw";

/// Remote interface -> (knob, bus name). Each name is claimed only while its knob is available.
pub const REMOTES: [(&str, &str); 4] = [
    ("tdp", "io.github.featherwolf.Bathysphere.Hw1.TdpLimit1"),
    ("gpu", "io.github.featherwolf.Bathysphere.Hw1.GpuPerformanceLevel1"),
    (
        "gpuPowerProfile",
        "io.github.featherwolf.Bathysphere.Hw1.GpuPowerProfile1",
    ),
    (
        "chargeLimit",
        "io.github.featherwolf.Bathysphere.Hw1.BatteryChargeLimit1",
    ),
];

pub const PPD_NAME: &str = "org.freedesktop.UPower.PowerProfiles";
pub const PPD_LEGACY: &str = "net.hadess.PowerProfiles";
pub const TUNED_NAME: &str = "com.redhat.tuned";
pub const POWERSTATION_NAME: &str = "org.shadowblip.PowerStation";
pub const THERMALD_NAME: &str = "org.freedesktop.thermald";

tokio::task_local! {
    /// Set while the provider emits PropertiesChanged (its getters then skip the read gate).
    static EMITTING: ();
}

pub struct Shared {
    pub hw: tokio::sync::Mutex<Hw>,
    pub conn: std::sync::OnceLock<Connection>,
    gates: std::sync::Mutex<Gates>,
    gate_notify: tokio::sync::Notify,
    rate: std::sync::Mutex<SenderRate>,
    /// One open read at a time (see `read`).
    reads: tokio::sync::Semaphore,
    names: tokio::sync::Mutex<BTreeSet<&'static str>>,
    /// Set once the main name is owned: no remote name is claimed before that.
    pub serving: std::sync::atomic::AtomicBool,
    etc: PathBuf,
}

impl Shared {
    pub fn new(hw: Hw) -> Arc<Shared> {
        let etc = hw.paths.etc.clone();
        Arc::new(Shared {
            hw: tokio::sync::Mutex::new(hw),
            conn: std::sync::OnceLock::new(),
            gates: Default::default(),
            gate_notify: tokio::sync::Notify::new(),
            rate: Default::default(),
            reads: tokio::sync::Semaphore::new(1),
            names: Default::default(),
            serving: Default::default(),
            etc,
        })
    }

    pub fn conn(&self) -> &Connection {
        self.conn.get().expect("connection set before serving")
    }

    fn sender(hdr: &Header<'_>) -> Result<String, HwError> {
        hdr.sender()
            .map(|s| s.to_string())
            .ok_or_else(|| HwError::AccessDenied("no sender".into()))
    }

    /// Rate limit only (open calls such as Describe).
    fn open(&self, hdr: &Header<'_>) -> Result<String, HwError> {
        let s = Self::sender(hdr)?;
        self.rate.lock().expect("rate lock").check(&s, Instant::now())?;
        Ok(s)
    }

    /// Rate limit, authorization, kill switch.
    async fn begin(&self, hdr: &Header<'_>) -> Result<Ctx, HwError> {
        let s = self.open(hdr)?;
        let dbus = fdo::DBusProxy::new(self.conn()).await.map_err(HwError::from)?;
        let uid = crate::auth::authorize(self.conn(), &dbus, &s).await?;
        if self.etc.join("disabled").exists() {
            return Err(HwError::Disabled("disabled".into()));
        }
        Ok(Ctx {
            uid: Some(uid),
            sender: Some(s),
        })
    }

    /// begin() for a property set (zbus hands setters an optional header).
    async fn begin_prop(&self, hdr: Option<Header<'_>>) -> Result<Ctx, HwError> {
        match hdr {
            Some(h) => self.begin(&h).await,
            None => Err(HwError::AccessDenied("no sender".into())),
        }
    }

    /// Open reads (property getters, Describe): one at a time in front of the hardware lock.
    /// Any local user may read, and a read can be as slow as an embedded-controller transaction;
    /// without this gate a flood of readers queues on the lock ahead of the safety task (the fan
    /// guard and the only WATCHDOG=1 sender) and of every write.
    /// The provider's own PropertiesChanged emission reads through the same getters and skips
    /// the gate, so a reply or the safety task's events never wait behind the readers.
    pub async fn read<T>(&self, f: impl FnOnce(&Hw) -> T) -> T {
        let _turn = match EMITTING.try_with(|_| ()) {
            Ok(()) => None,
            Err(_) => Some(self.reads.acquire().await.expect("the read gate is never closed")),
        };
        let hw = self.hw.lock().await;
        tokio::task::block_in_place(|| f(&hw))
    }

    /// Run `f` under the hardware lock, then emit what it changed.
    pub async fn run<T>(&self, f: impl FnOnce(&mut Hw) -> T) -> T {
        let (r, events) = {
            let mut g = self.hw.lock().await;
            let r = tokio::task::block_in_place(|| f(&mut g));
            (r, std::mem::take(&mut g.events))
        };
        self.emit(events).await;
        r
    }

    /// Validate, wait for the knob's write slot (coalescing a burst), then write.
    async fn commit(
        &self,
        knob: &'static str,
        member: &'static str,
        ctx: &Ctx,
        op: impl Fn(&mut Hw, &Ctx, bool) -> Result<u32, HwError>,
    ) -> Result<u32, HwError> {
        self.run(|hw| op(hw, ctx, false)).await?;
        let my = self.gates.lock().expect("gates").enter(knob, member);
        loop {
            let notified = self.gate_notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            let turn = self.gates.lock().expect("gates").turn(knob, member, my, Instant::now());
            match turn {
                Turn::Go => break,
                Turn::Wait(d) => {
                    tokio::select! {
                        _ = tokio::time::sleep(d) => {}
                        _ = &mut notified => {}
                    }
                }
                Turn::Superseded => {
                    if let Some(r) = self.gates.lock().expect("gates").result(knob, member, my) {
                        return r;
                    }
                    notified.await;
                }
            }
        }
        let r = self.run(|hw| op(hw, ctx, true)).await;
        self.gates.lock().expect("gates").publish(knob, member, my, r.clone());
        self.gate_notify.notify_waiters();
        r
    }

    /// Emit Changed and PropertiesChanged, then claim or release the remote names.
    pub async fn emit(&self, events: Vec<Ev>) {
        EMITTING.scope((), self.emit_now(events)).await
    }

    async fn emit_now(&self, events: Vec<Ev>) {
        let Some(conn) = self.conn.get() else { return };
        let mut props: BTreeSet<Iface> = BTreeSet::new();
        for e in events {
            match e {
                Ev::Changed(knob, reason) => {
                    if let Ok(em) = SignalEmitter::new(conn, PATH) {
                        let _ = Provider1::changed(&em, knob, &reason).await;
                    }
                }
                Ev::Props(i) => {
                    props.insert(i);
                }
            }
        }
        for i in props {
            if let Err(e) = emit_props(conn, i).await {
                eprintln!("oxp3-hwd: PropertiesChanged: {e}");
            }
        }
        self.sync_names().await;
    }

    /// Claim each remote's bus name while its knob works; release it otherwise.
    pub async fn sync_names(&self) {
        let Some(conn) = self.conn.get() else { return };
        if !self.serving.load(std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        let want: BTreeSet<&'static str> = {
            let hw = self.hw.lock().await;
            REMOTES
                .iter()
                .filter(|(k, _)| hw.available(k))
                .map(|(_, n)| *n)
                .collect()
        };
        let mut have = self.names.lock().await;
        for n in want.difference(&have.clone()) {
            match conn
                .request_name_with_flags(*n, zbus::fdo::RequestNameFlags::DoNotQueue.into())
                .await
            {
                Ok(_) => {
                    eprintln!("oxp3-hwd: claimed {n}");
                    have.insert(n);
                }
                Err(e) => eprintln!("oxp3-hwd: cannot claim {n}: {e}"),
            }
        }
        for n in have.clone().difference(&want) {
            let _ = conn.release_name(*n).await;
            eprintln!("oxp3-hwd: released {n}");
            have.remove(n);
        }
    }

    pub async fn release_all_names(&self) {
        let Some(conn) = self.conn.get() else { return };
        let mut have = self.names.lock().await;
        for n in have.iter() {
            let _ = conn.release_name(*n).await;
        }
        have.clear();
        let _ = conn.release_name(BUS_NAME).await;
    }

    pub fn forget_sender(&self, name: &str) {
        self.rate.lock().expect("rate lock").forget(name);
    }
}

async fn emit_props(conn: &Connection, i: Iface) -> zbus::Result<()> {
    let os = conn.object_server();
    macro_rules! props {
        ($ty:ty, $path:expr, [$($m:ident),*]) => {{
            let r = os.interface::<_, $ty>($path).await?;
            let em = r.signal_emitter().clone();
            let g = r.get().await;
            $( g.$m(&em).await?; )*
        }};
    }
    match i {
        Iface::Provider => props!(Provider1, PATH, [knobs_changed]),
        Iface::PowerLimits => props!(
            PowerLimits1,
            PATH,
            [
                boost_mode_changed,
                boost_limit_changed,
                boost_offset_changed,
                effective_boost_limit_changed
            ]
        ),
        Iface::CpuEnergy => props!(CpuEnergy1, PATH, [energy_preference_changed]),
        Iface::CpuLimits => props!(
            CpuLimits1,
            PATH,
            [max_perf_pct_changed, min_perf_pct_changed, core_type_max_freq_changed]
        ),
        Iface::SocBalance => props!(SocBalance1, PATH, [balance_changed, offset_changed, active_changed]),
        Iface::GpuRange => props!(GpuRange1, PATH, [auto_min_freq_changed, auto_max_freq_changed]),
        Iface::FanMode => props!(FanMode1, PATH, [fan_mode_changed, last_revert_changed]),
        Iface::ChargeBehaviour => props!(BatteryChargeBehaviour1, PATH, [charge_behaviour_changed]),
        Iface::TdpLimit => props!(TdpLimit1, SOM_PATH, [tdp_limit_changed]),
        Iface::GpuLevel => props!(
            GpuPerformanceLevel1,
            SOM_PATH,
            [gpu_performance_level_changed, manual_gpu_clock_changed]
        ),
        Iface::GpuProfile => props!(GpuPowerProfile1, SOM_PATH, [gpu_power_profile_changed]),
        Iface::ChargeLimit => props!(BatteryChargeLimit1, SOM_PATH, [max_charge_level_changed]),
    }
    Ok(())
}

fn fdo_err(e: HwError) -> fdo::Error {
    e.to_fdo()
}

// Remote property setters take the variant as sent, so a value of the wrong type is InvalidArgs
// (zbus's own conversion would answer with its private error name).
fn want_u32(v: &OwnedValue, prop: &str) -> Result<u32, HwError> {
    u32::try_from(v).map_err(|_| HwError::InvalidArgs(format!("{prop} needs type u")))
}

fn want_i32(v: &OwnedValue, prop: &str) -> Result<i32, HwError> {
    i32::try_from(v).map_err(|_| HwError::InvalidArgs(format!("{prop} needs type i")))
}

fn want_string(v: &OwnedValue, prop: &str) -> Result<String, HwError> {
    match &**v {
        zbus::zvariant::Value::Str(s) => Ok(s.to_string()),
        _ => Err(HwError::InvalidArgs(format!("{prop} needs type s"))),
    }
}

// ---- io.github.featherwolf.Bathysphere.Hw1.Provider1 ------------------------------------------

pub struct Provider1 {
    pub s: Arc<Shared>,
    pub device_id: &'static str,
}

#[interface(name = "io.github.featherwolf.Bathysphere.Hw1.Provider1")]
impl Provider1 {
    #[zbus(property(emits_changed_signal = "const"))]
    fn api_version(&self) -> u32 {
        API_VERSION
    }

    #[zbus(property(emits_changed_signal = "const"))]
    fn provider_name(&self) -> String {
        PROVIDER_NAME.into()
    }

    #[zbus(property(emits_changed_signal = "const"))]
    fn provider_version(&self) -> String {
        env!("CARGO_PKG_VERSION").into()
    }

    #[zbus(property(emits_changed_signal = "const"))]
    fn device_id(&self) -> String {
        self.device_id.into()
    }

    #[zbus(property)]
    async fn knobs(&self) -> Vec<String> {
        self.s.read(|hw| hw.knobs()).await
    }

    #[zbus(out_args("description"))]
    async fn describe(
        &self,
        #[zbus(header)] hdr: Header<'_>,
        knob: String,
    ) -> Result<HashMap<String, OwnedValue>, HwError> {
        self.s.open(&hdr)?;
        let m = self.s.read(|hw| hw.describe(&knob)).await?;
        Ok(m.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }

    async fn release(&self, #[zbus(header)] hdr: Header<'_>, knobs: Vec<String>) -> Result<(), HwError> {
        let ctx = self.s.begin(&hdr).await?;
        self.s.run(|hw| hw.release(&ctx, &knobs)).await
    }

    async fn take_control(&self, #[zbus(header)] hdr: Header<'_>, knobs: Vec<String>) -> Result<(), HwError> {
        let ctx = self.s.begin(&hdr).await?;
        self.s.run(|hw| hw.take_control(&ctx, &knobs)).await
    }

    #[zbus(signal)]
    pub async fn changed(emitter: &SignalEmitter<'_>, knob: &str, reason: &str) -> zbus::Result<()>;
}

// ---- …PowerLimits1 ----------------------------------------------------------------------------

pub struct PowerLimits1 {
    pub s: Arc<Shared>,
}

#[interface(name = "io.github.featherwolf.Bathysphere.Hw1.PowerLimits1")]
impl PowerLimits1 {
    #[zbus(property(emits_changed_signal = "const"))]
    fn available_boost_modes(&self) -> Vec<String> {
        crate::hw::BOOST_MODES.iter().map(|s| s.to_string()).collect()
    }

    #[zbus(property)]
    async fn boost_mode(&self) -> String {
        self.s.read(|hw| hw.boost_state().0.into()).await
    }

    #[zbus(property)]
    async fn boost_limit(&self) -> u32 {
        self.s.read(|hw| hw.boost_state().1).await
    }

    #[zbus(property)]
    async fn boost_offset(&self) -> u32 {
        self.s.read(|hw| hw.boost_state().2).await
    }

    #[zbus(property)]
    async fn effective_boost_limit(&self) -> u32 {
        self.s.read(|hw| hw.boost_effective()).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn boost_limit_min(&self) -> u32 {
        self.s.read(|hw| hw.env.boost_min_w).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn boost_limit_max(&self) -> u32 {
        self.s.read(|hw| hw.env.boost_max_w).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn boost_offset_max(&self) -> u32 {
        self.s.read(|hw| hw.env.boost_offset_max_w).await
    }

    #[zbus(out_args("effective_w"))]
    async fn set_boost(
        &self,
        #[zbus(header)] hdr: Header<'_>,
        mode: String,
        limit_w: u32,
        offset_w: u32,
    ) -> Result<u32, HwError> {
        let ctx = self.s.begin(&hdr).await?;
        self.s
            .commit("boost", "SetBoost", &ctx, |hw, c, go| {
                hw.set_boost(c, &mode, limit_w, offset_w, go)
            })
            .await
    }
}

// ---- …CpuEnergy1 ------------------------------------------------------------------------------

pub struct CpuEnergy1 {
    pub s: Arc<Shared>,
}

#[interface(name = "io.github.featherwolf.Bathysphere.Hw1.CpuEnergy1")]
impl CpuEnergy1 {
    #[zbus(property(emits_changed_signal = "const"))]
    async fn core_types(&self) -> Vec<String> {
        let mut v = vec!["all".to_string()];
        v.extend(self.s.read(|hw| hw.core_types()).await.into_iter().map(String::from));
        v
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn available_energy_preferences(&self) -> Vec<String> {
        self.s.read(|hw| hw.epp_available()).await
    }

    #[zbus(property)]
    async fn energy_preference(&self) -> HashMap<String, String> {
        self.s.read(|hw| hw.epp_by_type().into_iter().collect()).await
    }

    async fn set_energy_preference(
        &self,
        #[zbus(header)] hdr: Header<'_>,
        preferences: HashMap<String, String>,
    ) -> Result<(), HwError> {
        let ctx = self.s.begin(&hdr).await?;
        let prefs: BTreeMap<String, String> = preferences.into_iter().collect();
        self.s
            .commit("epp", "SetEnergyPreference", &ctx, |hw, c, go| {
                hw.set_epp(c, &prefs, go).map(|_| 0)
            })
            .await
            .map(|_| ())
    }
}

// ---- …CpuLimits1 ------------------------------------------------------------------------------

pub struct CpuLimits1 {
    pub s: Arc<Shared>,
}

fn take_u32(v: &OwnedValue, key: &str) -> Result<u32, HwError> {
    u32::try_from(v).map_err(|_| HwError::InvalidArgs(format!("{key} needs type u")))
}

#[interface(name = "io.github.featherwolf.Bathysphere.Hw1.CpuLimits1")]
impl CpuLimits1 {
    #[zbus(property)]
    async fn max_perf_pct(&self) -> u32 {
        self.s.read(|hw| hw.perf_read().0.unwrap_or(0)).await
    }

    #[zbus(property)]
    async fn min_perf_pct(&self) -> u32 {
        self.s.read(|hw| hw.perf_read().1.unwrap_or(0)).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn max_perf_pct_min(&self) -> u32 {
        self.s.read(|hw| hw.env.max_perf_pct_min).await
    }

    #[zbus(property)]
    async fn core_type_max_freq(&self) -> HashMap<String, u32> {
        self.s.read(|hw| hw.caps_by_type().into_iter().collect()).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn core_type_freq_range(&self) -> HashMap<String, (u32, u32)> {
        self.s.read(|hw| hw.type_ranges().into_iter().collect()).await
    }

    async fn set_cpu_limits(
        &self,
        #[zbus(header)] hdr: Header<'_>,
        limits: HashMap<String, OwnedValue>,
    ) -> Result<(), HwError> {
        let ctx = self.s.begin(&hdr).await?;
        let mut max = None;
        let mut min = None;
        let mut caps = None;
        for (k, v) in &limits {
            match k.as_str() {
                "max_perf_pct" => max = Some(take_u32(v, k)?),
                "min_perf_pct" => min = Some(take_u32(v, k)?),
                "core_type_max_freq" => {
                    let m: HashMap<String, u32> = v
                        .try_clone()
                        .map_err(|_| HwError::InvalidArgs("core_type_max_freq".into()))?
                        .try_into()
                        .map_err(|_| HwError::InvalidArgs("core_type_max_freq needs type a{su}".into()))?;
                    caps = Some(m.into_iter().collect::<BTreeMap<_, _>>());
                }
                _ => return Err(HwError::InvalidArgs(format!("unknown key {k:?}"))),
            }
        }
        self.s
            .commit("cpuLimits", "SetCpuLimits", &ctx, |hw, c, go| {
                hw.set_cpu_limits(c, max, min, caps.clone(), go).map(|_| 0)
            })
            .await
            .map(|_| ())
    }
}

// ---- …SocBalance1 -----------------------------------------------------------------------------

pub struct SocBalance1 {
    pub s: Arc<Shared>,
}

#[interface(name = "io.github.featherwolf.Bathysphere.Hw1.SocBalance1")]
impl SocBalance1 {
    #[zbus(property)]
    async fn balance(&self) -> u32 {
        self.s.read(|hw| hw.soc_state().0).await
    }

    #[zbus(property)]
    async fn offset(&self) -> u32 {
        self.s.read(|hw| hw.soc_state().1).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn balance_min(&self) -> u32 {
        self.s.read(|hw| hw.env.soc_balance.0).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn balance_max(&self) -> u32 {
        self.s.read(|hw| hw.env.soc_balance.1).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn offset_max(&self) -> u32 {
        self.s.read(|hw| hw.env.soc_offset_max).await
    }

    #[zbus(property)]
    async fn active(&self) -> bool {
        self.s.read(|hw| hw.soc_active()).await
    }

    async fn set_soc_balance(&self, #[zbus(header)] hdr: Header<'_>, balance: u32, offset: u32) -> Result<(), HwError> {
        let ctx = self.s.begin(&hdr).await?;
        self.s
            .commit("socBalance", "SetSocBalance", &ctx, |hw, c, go| {
                hw.set_soc(c, balance, offset, go).map(|_| 0)
            })
            .await
            .map(|_| ())
    }
}

// ---- …GpuRange1 -------------------------------------------------------------------------------

pub struct GpuRange1 {
    pub s: Arc<Shared>,
}

#[interface(name = "io.github.featherwolf.Bathysphere.Hw1.GpuRange1")]
impl GpuRange1 {
    #[zbus(property)]
    async fn auto_min_freq(&self) -> u32 {
        self.s.read(|hw| hw.gpu_state().2 .0).await
    }

    #[zbus(property)]
    async fn auto_max_freq(&self) -> u32 {
        self.s.read(|hw| hw.gpu_state().2 .1).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn freq_min(&self) -> u32 {
        self.s.read(|hw| hw.gpu_bounds().map(|b| b.0).unwrap_or(0)).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn freq_max(&self) -> u32 {
        self.s.read(|hw| hw.gpu_bounds().map(|b| b.1).unwrap_or(0)).await
    }

    async fn set_auto_range(&self, #[zbus(header)] hdr: Header<'_>, min_mhz: u32, max_mhz: u32) -> Result<(), HwError> {
        let ctx = self.s.begin(&hdr).await?;
        self.s
            .commit("gpuRange", "SetAutoRange", &ctx, |hw, c, go| {
                hw.set_range(c, min_mhz, max_mhz, go).map(|_| 0)
            })
            .await
            .map(|_| ())
    }
}

// ---- …FanMode1 --------------------------------------------------------------------------------

pub struct FanMode1 {
    pub s: Arc<Shared>,
}

/// logind's delay inhibitor, held while the fan is not "auto".
async fn take_inhibitor(conn: &Connection) -> Result<std::os::fd::OwnedFd, HwError> {
    let reply = conn
        .call_method(
            Some("org.freedesktop.login1"),
            "/org/freedesktop/login1",
            Some("org.freedesktop.login1.Manager"),
            "Inhibit",
            &("sleep", PROVIDER_NAME, "fan back to auto", "delay"),
        )
        .await
        .map_err(|e| {
            eprintln!("oxp3-hwd: no sleep inhibitor: {e}");
            HwError::NotApplied("fan=auto".into())
        })?;
    let fd: zbus::zvariant::OwnedFd = reply
        .body()
        .deserialize()
        .map_err(|_| HwError::NotApplied("fan=auto".into()))?;
    Ok(fd.into())
}

#[interface(name = "io.github.featherwolf.Bathysphere.Hw1.FanMode1")]
impl FanMode1 {
    #[zbus(property(emits_changed_signal = "const"))]
    async fn available_fan_modes(&self) -> Vec<String> {
        self.s
            .read(|hw| hw.env.fan_modes.iter().map(|s| s.to_string()).collect())
            .await
    }

    #[zbus(property)]
    async fn fan_mode(&self) -> String {
        self.s.read(|hw| hw.fan_state().0).await
    }

    #[zbus(property)]
    async fn last_revert(&self) -> String {
        self.s.read(|hw| hw.fan_state().1).await
    }

    async fn set_fan_mode(&self, #[zbus(header)] hdr: Header<'_>, mode: String) -> Result<(), HwError> {
        let ctx = self.s.begin(&hdr).await?;
        self.s.run(|hw| hw.set_fan(&ctx, &mode, false)).await?;
        if mode != "auto" {
            let have = self.s.hw.lock().await.fan_inhibitor.is_some();
            if !have {
                let fd = take_inhibitor(self.s.conn()).await?;
                let mut hw = self.s.hw.lock().await;
                if hw.fan_inhibitor.is_none() {
                    hw.fan_inhibitor = Some(fd);
                }
            }
        }
        let r = self
            .s
            .commit("fan", "SetFanMode", &ctx, |hw, c, go| {
                hw.set_fan(c, &mode, go).map(|_| 0)
            })
            .await;
        // whatever answered (an error, or a newer coalesced "auto"): no inhibitor while auto
        {
            let mut hw = self.s.hw.lock().await;
            if hw.fan_mode() == "auto" {
                hw.fan_inhibitor = None;
            }
        }
        r.map(|_| ())
    }
}

// ---- …BatteryChargeBehaviour1 -----------------------------------------------------------------

pub struct BatteryChargeBehaviour1 {
    pub s: Arc<Shared>,
}

#[interface(name = "io.github.featherwolf.Bathysphere.Hw1.BatteryChargeBehaviour1")]
impl BatteryChargeBehaviour1 {
    #[zbus(property(emits_changed_signal = "const"))]
    async fn available_charge_behaviours(&self) -> Vec<String> {
        self.s
            .read(|hw| hw.env.charge_behaviours.iter().map(|s| s.to_string()).collect())
            .await
    }

    #[zbus(property)]
    async fn charge_behaviour(&self) -> String {
        self.s
            .read(|hw| hw.beh_read().unwrap_or_else(|| hw.beh_value().to_string()))
            .await
    }

    async fn set_charge_behaviour(&self, #[zbus(header)] hdr: Header<'_>, behaviour: String) -> Result<(), HwError> {
        let ctx = self.s.begin(&hdr).await?;
        self.s
            .commit("chargeBehaviour", "SetChargeBehaviour", &ctx, |hw, c, go| {
                hw.set_behaviour(c, &behaviour, go).map(|_| 0)
            })
            .await
            .map(|_| ())
    }
}

// ---- …PackagePower1 ---------------------------------------------------------------------------

pub struct PackagePower1 {
    pub s: Arc<Shared>,
}

#[interface(name = "io.github.featherwolf.Bathysphere.Hw1.PackagePower1")]
impl PackagePower1 {
    #[zbus(out_args("milliwatts", "window_ms"))]
    async fn sample(&self, #[zbus(header)] hdr: Header<'_>) -> Result<(u32, u32), HwError> {
        let _ctx = self.s.begin(&hdr).await?;
        self.s.run(|hw| hw.sample()).await
    }
}

// ---- steamos-manager remotes (object …/Hw1/SteamOSManager) ----------------------------------

pub struct TdpLimit1 {
    pub s: Arc<Shared>,
}

#[interface(name = "com.steampowered.SteamOSManager1.TdpLimit1")]
impl TdpLimit1 {
    #[zbus(property)]
    async fn tdp_limit(&self) -> u32 {
        self.s.read(|hw| hw.tdp_value()).await
    }

    #[zbus(property)]
    async fn set_tdp_limit(&self, value: OwnedValue, #[zbus(header)] hdr: Option<Header<'_>>) -> fdo::Result<()> {
        let ctx = self.s.begin_prop(hdr).await.map_err(fdo_err)?;
        let value = want_u32(&value, "TdpLimit").map_err(fdo_err)?;
        self.s
            .commit("tdp", "TdpLimit", &ctx, |hw, c, go| hw.set_tdp(c, value, go))
            .await
            .map(|_| ())
            .map_err(fdo_err)
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn tdp_limit_min(&self) -> u32 {
        self.s.read(|hw| hw.tdp_bounds().0).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn tdp_limit_max(&self) -> u32 {
        self.s.read(|hw| hw.tdp_bounds().1).await
    }
}

pub struct GpuPerformanceLevel1 {
    pub s: Arc<Shared>,
}

#[interface(name = "com.steampowered.SteamOSManager1.GpuPerformanceLevel1")]
impl GpuPerformanceLevel1 {
    #[zbus(property(emits_changed_signal = "const"))]
    fn available_gpu_performance_levels(&self) -> Vec<String> {
        GPU_LEVELS.iter().map(|s| s.to_string()).collect()
    }

    #[zbus(property)]
    async fn gpu_performance_level(&self) -> String {
        self.s.read(|hw| hw.gpu_state().0.into()).await
    }

    #[zbus(property)]
    async fn set_gpu_performance_level(
        &self,
        value: OwnedValue,
        #[zbus(header)] hdr: Option<Header<'_>>,
    ) -> fdo::Result<()> {
        let ctx = self.s.begin_prop(hdr).await.map_err(fdo_err)?;
        let value = want_string(&value, "GpuPerformanceLevel").map_err(fdo_err)?;
        self.s
            .commit("gpu", "GpuPerformanceLevel", &ctx, |hw, c, go| {
                hw.set_gpu_level(c, &value, go).map(|_| 0)
            })
            .await
            .map(|_| ())
            .map_err(fdo_err)
    }

    #[zbus(property)]
    async fn manual_gpu_clock(&self) -> u32 {
        self.s.read(|hw| hw.gpu_state().1).await
    }

    #[zbus(property)]
    async fn set_manual_gpu_clock(
        &self,
        value: OwnedValue,
        #[zbus(header)] hdr: Option<Header<'_>>,
    ) -> fdo::Result<()> {
        let ctx = self.s.begin_prop(hdr).await.map_err(fdo_err)?;
        let value = want_u32(&value, "ManualGpuClock").map_err(fdo_err)?;
        self.s
            .commit("gpu", "ManualGpuClock", &ctx, |hw, c, go| {
                hw.set_gpu_clock(c, value, go).map(|_| 0)
            })
            .await
            .map(|_| ())
            .map_err(fdo_err)
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn manual_gpu_clock_min(&self) -> u32 {
        self.s.read(|hw| hw.gpu_bounds().map(|b| b.0).unwrap_or(0)).await
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn manual_gpu_clock_max(&self) -> u32 {
        self.s.read(|hw| hw.gpu_bounds().map(|b| b.1).unwrap_or(0)).await
    }
}

pub struct GpuPowerProfile1 {
    pub s: Arc<Shared>,
}

#[interface(name = "com.steampowered.SteamOSManager1.GpuPowerProfile1")]
impl GpuPowerProfile1 {
    #[zbus(property(emits_changed_signal = "const"))]
    fn available_gpu_power_profiles(&self) -> Vec<String> {
        GPU_PROFILES.iter().map(|s| s.to_string()).collect()
    }

    #[zbus(property)]
    async fn gpu_power_profile(&self) -> String {
        self.s.read(|hw| hw.gpu_profile().unwrap_or_default()).await
    }

    #[zbus(property)]
    async fn set_gpu_power_profile(
        &self,
        value: OwnedValue,
        #[zbus(header)] hdr: Option<Header<'_>>,
    ) -> fdo::Result<()> {
        let ctx = self.s.begin_prop(hdr).await.map_err(fdo_err)?;
        let value = want_string(&value, "GpuPowerProfile").map_err(fdo_err)?;
        self.s
            .commit("gpuPowerProfile", "GpuPowerProfile", &ctx, |hw, c, go| {
                hw.set_gpu_profile(c, &value, go).map(|_| 0)
            })
            .await
            .map(|_| ())
            .map_err(fdo_err)
    }
}

pub struct BatteryChargeLimit1 {
    pub s: Arc<Shared>,
}

#[interface(name = "com.steampowered.SteamOSManager1.BatteryChargeLimit1")]
impl BatteryChargeLimit1 {
    #[zbus(property)]
    async fn max_charge_level(&self) -> i32 {
        self.s
            .read(|hw| hw.charge_read().map(|v| v.clamp(-1, 100) as i32).unwrap_or(-1))
            .await
    }

    #[zbus(property)]
    async fn set_max_charge_level(
        &self,
        value: OwnedValue,
        #[zbus(header)] hdr: Option<Header<'_>>,
    ) -> fdo::Result<()> {
        let ctx = self.s.begin_prop(hdr).await.map_err(fdo_err)?;
        let value = want_i32(&value, "MaxChargeLevel").map_err(fdo_err)?;
        self.s
            .commit("chargeLimit", "MaxChargeLevel", &ctx, |hw, c, go| {
                hw.set_charge_limit(c, value, go).map(|_| 0)
            })
            .await
            .map(|_| ())
            .map_err(fdo_err)
    }

    #[zbus(property(emits_changed_signal = "const"))]
    async fn suggested_minimum_limit(&self) -> i32 {
        self.s.read(|hw| hw.charge_bounds().0).await as i32
    }
}

// ---- serving and watching ---------------------------------------------------------------------

pub async fn serve(conn: &Connection, s: &Arc<Shared>, device_id: &'static str) -> zbus::Result<()> {
    let os = conn.object_server();
    os.at(
        PATH,
        Provider1 {
            s: s.clone(),
            device_id,
        },
    )
    .await?;
    os.at(PATH, PowerLimits1 { s: s.clone() }).await?;
    os.at(PATH, CpuEnergy1 { s: s.clone() }).await?;
    os.at(PATH, CpuLimits1 { s: s.clone() }).await?;
    os.at(PATH, SocBalance1 { s: s.clone() }).await?;
    os.at(PATH, GpuRange1 { s: s.clone() }).await?;
    os.at(PATH, FanMode1 { s: s.clone() }).await?;
    os.at(PATH, BatteryChargeBehaviour1 { s: s.clone() }).await?;
    os.at(PATH, PackagePower1 { s: s.clone() }).await?;
    os.at(SOM_PATH, TdpLimit1 { s: s.clone() }).await?;
    os.at(SOM_PATH, GpuPerformanceLevel1 { s: s.clone() }).await?;
    os.at(SOM_PATH, GpuPowerProfile1 { s: s.clone() }).await?;
    os.at(SOM_PATH, BatteryChargeLimit1 { s: s.clone() }).await?;
    Ok(())
}

async fn has_owner(dbus: &fdo::DBusProxy<'_>, name: &'static str) -> bool {
    match zbus::names::BusName::try_from(name) {
        Ok(n) => dbus.name_has_owner(n).await.unwrap_or(false),
        Err(_) => false,
    }
}

/// Co-writers by bus-name owner, and hhd by its systemd unit state (read-only).
pub async fn cowriters(conn: &Connection) -> crate::hw::CoWriters {
    let mut c = crate::hw::CoWriters::default();
    let Ok(dbus) = fdo::DBusProxy::new(conn).await else {
        return c;
    };
    if has_owner(&dbus, PPD_NAME).await || has_owner(&dbus, PPD_LEGACY).await {
        c.ppd = Some(if has_owner(&dbus, TUNED_NAME).await {
            "tuned-ppd"
        } else {
            "power-profiles-daemon"
        });
    }
    c.powerstation = has_owner(&dbus, POWERSTATION_NAME).await;
    c.thermald = has_owner(&dbus, THERMALD_NAME).await;
    c.hhd = hhd_active(conn).await;
    c
}

async fn hhd_active(conn: &Connection) -> bool {
    let r = conn
        .call_method(
            Some("org.freedesktop.systemd1"),
            "/org/freedesktop/systemd1",
            Some("org.freedesktop.systemd1.Manager"),
            "ListUnitsByPatterns",
            &(
                vec!["active", "activating", "reloading"],
                vec!["hhd.service", "hhd@*.service", "hhd_local@*.service"],
            ),
        )
        .await;
    #[allow(clippy::type_complexity)]
    let Ok(reply) = r
    else {
        return false;
    };
    type Unit = (
        String,
        String,
        String,
        String,
        String,
        String,
        zbus::zvariant::OwnedObjectPath,
        u32,
        String,
        zbus::zvariant::OwnedObjectPath,
    );
    reply
        .body()
        .deserialize::<Vec<Unit>>()
        .map(|v| !v.is_empty())
        .unwrap_or(false)
}

pub async fn update_cowriters(s: &Arc<Shared>) {
    let c = cowriters(s.conn()).await;
    s.run(|hw| {
        if hw.cow != c {
            eprintln!("oxp3-hwd: co-writers now {c:?}");
            hw.cow = c;
        }
        hw.refresh_avail();
    })
    .await;
}

/// NameOwnerChanged: lease holders that vanish, and co-writer names.
pub async fn watch_names(s: Arc<Shared>) -> zbus::Result<()> {
    let dbus = fdo::DBusProxy::new(s.conn()).await?;
    let mut stream = dbus.receive_name_owner_changed().await?;
    while let Some(sig) = stream.next().await {
        let Ok(args) = sig.args() else { continue };
        let name = args.name().to_string();
        if name.starts_with(':') {
            if args.new_owner().is_none() {
                s.forget_sender(&name);
                s.run(|hw| hw.disconnected(&name)).await;
            }
            continue;
        }
        if [PPD_NAME, PPD_LEGACY, TUNED_NAME, POWERSTATION_NAME, THERMALD_NAME].contains(&name.as_str()) {
            update_cowriters(&s).await;
        }
    }
    Ok(())
}

/// logind PrepareForSleep: the fan returns to auto before sleep (then the inhibitor goes), and
/// held values are re-asserted after resume.
///
/// Only a signal from the connection that owns org.freedesktop.login1 counts. Any local process
/// may send a signal, and a unicast one (destination: this provider) is delivered whatever the
/// match rules say; zbus's match-rule stream cannot check a well-known sender, so the proxy's
/// signal stream, which tracks the name's owner, does it.
pub async fn watch_sleep(s: Arc<Shared>) -> zbus::Result<()> {
    let login1: zbus::Proxy<'_> = zbus::proxy::Builder::new(s.conn())
        .destination("org.freedesktop.login1")?
        .path("/org/freedesktop/login1")?
        .interface("org.freedesktop.login1.Manager")?
        .cache_properties(zbus::proxy::CacheProperties::No)
        .build()
        .await?;
    let mut stream = login1.receive_signal("PrepareForSleep").await?;
    while let Some(msg) = stream.next().await {
        let Ok(going) = msg.body().deserialize::<bool>() else {
            continue;
        };
        if going {
            eprintln!("oxp3-hwd: preparing for sleep");
            s.run(|hw| {
                // until PrepareForSleep(false) only "auto" is accepted, so a request still on its
                // way (inhibitor, write slot) cannot leave the fan manual through the sleep
                hw.sleeping = true;
                if hw.fan_mode() != "auto" {
                    hw.revert_fan("sleep");
                }
                hw.fan_inhibitor = None;
            })
            .await;
        } else {
            eprintln!("oxp3-hwd: resumed");
            s.run(|hw| hw.resumed(Instant::now())).await;
        }
    }
    Ok(())
}
