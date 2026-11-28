// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! Fake sysfs trees for oxp3-hwd's --sysfs-root (a port of OXP3 Deck's tests/powerfake.py).
//!
//! File permissions follow the package's grants: an attribute 70-oxp3-hw.rules or grants.conf
//! grants is writable by the provider, every other one is not, so a write outside the grants fails
//! with the kernel's EACCES exactly as on the device. Two modes:
//! - single-uid (a non-root test run): the provider runs as the test user; granted files are
//!   0644, the others 0444 (the owner cannot write them either), energy_uj 0440 or 0000;
//! - multi-uid (a root test run): the provider runs as another user (setpriv); granted files are
//!   root:<provider group> 0664 (0440 for energy_uj), the others root:root 0644 (0400).
//!
//! The kernel's store semantics (truncation, EINVAL, read-back) come from src/emu.rs.

use std::ffi::CString;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

pub const MW: u64 = 1_000_000;

static SEQ: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Grant {
    /// Not granted: the provider can read it but not write it.
    No,
    /// Granted read-write to the provider's group.
    Rw,
    /// The energy counter: readable by the provider's group only.
    Secret(bool),
}

#[derive(Clone, Copy, Debug)]
pub enum Owner {
    /// The provider runs as the test user.
    Single,
    /// The provider runs as this group's member (the test runs as root).
    Multi { gid: u32 },
}

pub struct Tree {
    pub root: PathBuf,
    pub owner: Owner,
}

fn chown(p: &Path, uid: u32, gid: u32) {
    let c = CString::new(p.as_os_str().as_bytes()).unwrap();
    // SAFETY: valid C string; lchown changes only this inode.
    let r = unsafe { libc::lchown(c.as_ptr(), uid, gid) };
    assert_eq!(r, 0, "chown {}", p.display());
}

pub fn root_test() -> bool {
    // SAFETY: plain getter
    unsafe { libc::geteuid() == 0 }
}

impl Drop for Tree {
    fn drop(&mut self) {
        if std::env::var_os("OXP3_HW_KEEP").is_none() {
            let _ = fs::remove_dir_all(&self.root);
        }
    }
}

impl Tree {
    pub fn new(owner: Owner) -> Tree {
        let root = std::env::temp_dir().join(format!(
            "oxp3-hw-test-{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
        let t = Tree { root, owner };
        let k = t.root.join(".kernel");
        fs::create_dir_all(&k).unwrap();
        fs::set_permissions(&k, fs::Permissions::from_mode(0o777)).unwrap();
        for f in ["writes.log", "rules.json"] {
            fs::write(k.join(f), if f == "rules.json" { "{}" } else { "" }).unwrap();
            fs::set_permissions(k.join(f), fs::Permissions::from_mode(0o666)).unwrap();
        }
        // the provider's own directories: /etc is root's, the state and runtime dirs its own
        t.dir("etc/oxp3-hw", false);
        t.dir("var/lib/oxp3-hw", true);
        t.dir("run/oxp3-hw", true);
        t
    }

    fn dir(&self, rel: &str, provider_owned: bool) {
        let p = self.root.join(rel);
        fs::create_dir_all(&p).unwrap();
        match (self.owner, provider_owned) {
            (Owner::Multi { gid }, true) => {
                chown(&p, gid, gid);
                fs::set_permissions(&p, fs::Permissions::from_mode(0o700)).unwrap();
            }
            _ => fs::set_permissions(&p, fs::Permissions::from_mode(0o755)).unwrap(),
        }
    }

    pub fn path(&self, rel: &str) -> PathBuf {
        self.root.join(rel.trim_start_matches('/'))
    }

    fn mkdirs(&self, p: &Path) {
        if let Some(parent) = p.parent() {
            fs::create_dir_all(parent).unwrap();
        }
    }

    /// Lay out an attribute with the permissions its grant gives.
    pub fn put(&self, rel: &str, text: impl std::fmt::Display, grant: Grant) -> PathBuf {
        let p = self.path(rel);
        self.mkdirs(&p);
        if p.exists() {
            fs::set_permissions(&p, fs::Permissions::from_mode(0o644)).unwrap();
        }
        fs::write(&p, format!("{text}\n")).unwrap();
        self.set_grant(&p, grant);
        p
    }

    pub fn set_grant(&self, p: &Path, grant: Grant) {
        let (mode, group) = match (self.owner, grant) {
            (Owner::Single, Grant::No) => (0o444, None),
            (Owner::Single, Grant::Rw) => (0o644, None),
            (Owner::Single, Grant::Secret(true)) => (0o440, None),
            (Owner::Single, Grant::Secret(false)) => (0o000, None),
            (Owner::Multi { .. }, Grant::No) => (0o644, Some(0)),
            (Owner::Multi { gid }, Grant::Rw) => (0o664, Some(gid)),
            (Owner::Multi { gid }, Grant::Secret(true)) => (0o440, Some(gid)),
            (Owner::Multi { .. }, Grant::Secret(false)) => (0o400, Some(0)),
        };
        if let Some(g) = group {
            chown(p, 0, g);
        }
        fs::set_permissions(p, fs::Permissions::from_mode(mode)).unwrap();
    }

    /// A hidden emulation state file (EC registers): the provider's writes change it.
    fn hidden(&self, rel: &str, text: impl std::fmt::Display) {
        let p = self.path(rel);
        self.mkdirs(&p);
        fs::write(&p, format!("{text}\n")).unwrap();
        fs::set_permissions(&p, fs::Permissions::from_mode(0o666)).unwrap();
    }

    /// Another writer (firmware, the test): change a file whatever its mode.
    pub fn poke(&self, rel: &str, text: impl std::fmt::Display) {
        let p = self.path(rel);
        let mode = fs::metadata(&p).map(|m| m.permissions().mode()).ok();
        if let Some(m) = mode {
            fs::set_permissions(&p, fs::Permissions::from_mode(m | 0o200)).unwrap();
        } else {
            self.mkdirs(&p);
        }
        fs::write(&p, format!("{text}\n")).unwrap();
        if let Some(m) = mode {
            fs::set_permissions(&p, fs::Permissions::from_mode(m)).unwrap();
        }
    }

    pub fn get(&self, rel: &str) -> String {
        fs::read_to_string(self.path(rel))
            .unwrap_or_default()
            .trim()
            .to_string()
    }

    pub fn remove(&self, rel: &str) {
        let _ = fs::remove_file(self.path(rel));
    }

    /// rules.json for src/emu.rs.
    pub fn rules(&self, v: serde_json::Value) {
        fs::write(self.path(".kernel/rules.json"), v.to_string()).unwrap();
    }

    /// Attempted writes: (path relative to the root, text, "ok" | "ignored" | errno name).
    pub fn writes(&self) -> Vec<(String, String, String)> {
        fs::read_to_string(self.path(".kernel/writes.log"))
            .unwrap_or_default()
            .lines()
            .filter_map(|l| {
                let mut it = l.splitn(3, '\t');
                Some((it.next()?.to_string(), it.next()?.to_string(), it.next()?.to_string()))
            })
            .collect()
    }

    pub fn clear_writes(&self) {
        fs::write(self.path(".kernel/writes.log"), "").unwrap();
    }

    /// Writes as short names: intel-rapl:0/long_term, gpu/min_freq, policy3/epp, ...
    pub fn names(&self) -> Vec<String> {
        self.writes()
            .into_iter()
            .map(|(p, _, _)| {
                if let Some(rest) = p.strip_prefix("sys/class/powercap/") {
                    let mut parts = rest.split('/');
                    let zone = parts.next().unwrap_or("").to_string();
                    let file = parts.next().unwrap_or("").to_string();
                    let n = file.split('_').nth(1).unwrap_or("0");
                    let cname = self.get(&format!("sys/class/powercap/{zone}/constraint_{n}_name"));
                    format!("{zone}/{cname}")
                } else if p.ends_with("min_freq") || p.ends_with("max_freq") {
                    format!("gpu/{}", Path::new(&p).file_name().unwrap().to_string_lossy())
                } else if p.ends_with("energy_performance_preference") {
                    format!(
                        "{}/epp",
                        Path::new(&p).parent().unwrap().file_name().unwrap().to_string_lossy()
                    )
                } else if p.ends_with("scaling_max_freq") {
                    format!(
                        "{}/cap",
                        Path::new(&p).parent().unwrap().file_name().unwrap().to_string_lossy()
                    )
                } else if p.contains("/platform-profile/") {
                    "profile".to_string()
                } else {
                    Path::new(&p).file_name().unwrap().to_string_lossy().into_owned()
                }
            })
            .collect()
    }

    // ---- devices (powerfake.py) -------------------------------------------------------------

    pub fn dmi(&self, vendor: &str, product: &str) {
        self.put("sys/class/dmi/id/sys_vendor", vendor, Grant::No);
        self.put("sys/class/dmi/id/product_name", product, Grant::No);
        self.put("sys/class/dmi/id/board_vendor", vendor, Grant::No);
        self.put("sys/class/dmi/id/board_name", product, Grant::No);
    }

    pub fn boot_id(&self, v: &str) {
        self.put("proc/sys/kernel/random/boot_id", v, Grant::No);
    }

    /// A RAPL zone with constraints in `order`; granted as 70-oxp3-hw.rules does (only
    /// intel-rapl:0 / intel-rapl-mmio:0 named package-0 with long_term then short_term).
    #[allow(clippy::too_many_arguments)]
    pub fn rapl(&self, zone: &str, pl1: u64, pl2: u64, pl4: u64, order: &[&str], base: u64, name: &str) -> PathBuf {
        let dir = format!("sys/class/powercap/{zone}");
        self.put(&format!("{dir}/name"), name, Grant::No);
        self.put(&format!("{dir}/enabled"), 1, Grant::No);
        let granted_zone = (zone == "intel-rapl:0" || zone == "intel-rapl-mmio:0")
            && name == "package-0"
            && order.first() == Some(&"long_term")
            && order.get(1) == Some(&"short_term");
        for (n, cname) in order.iter().enumerate() {
            let (limit, mx, window) = match *cname {
                "long_term" => (pl1, base, Some(27983872)),
                "short_term" => (pl2, 0, Some(976)),
                _ => (pl4, 0, None),
            };
            self.put(&format!("{dir}/constraint_{n}_name"), cname, Grant::No);
            let g = if granted_zone && n < 2 { Grant::Rw } else { Grant::No };
            self.put(&format!("{dir}/constraint_{n}_power_limit_uw"), limit * MW, g);
            self.put(&format!("{dir}/constraint_{n}_max_power_uw"), mx * MW, Grant::No);
            if let Some(w) = window {
                self.put(&format!("{dir}/constraint_{n}_time_window_us"), w, Grant::No);
            }
        }
        self.path(&dir)
    }

    pub fn limit(&self, zone: &str, which: &str) -> String {
        for n in 0..6 {
            if self.get(&format!("sys/class/powercap/{zone}/constraint_{n}_name")) == which {
                return format!("sys/class/powercap/{zone}/constraint_{n}_power_limit_uw");
            }
        }
        panic!("{zone} has no {which}");
    }

    pub fn watts(&self, zone: &str, which: &str) -> f64 {
        // NaN while another process is half-way through a write (the emulation is not atomic)
        self.get(&self.limit(zone, which))
            .parse::<f64>()
            .map(|v| v / MW as f64)
            .unwrap_or(f64::NAN)
    }

    pub fn set_watts(&self, zone: &str, which: &str, w: u64) {
        self.poke(&self.limit(zone, which), w * MW);
    }

    /// An xe card; returns the render GT's freq0 folder (relative).
    #[allow(clippy::too_many_arguments)]
    pub fn xe(
        &self,
        card: &str,
        gts: &[(&str, Option<&str>)],
        rpn: u64,
        rpe: u64,
        rp0: u64,
        minf: u64,
        maxf: u64,
        driver: &str,
    ) -> String {
        let dev = format!("sys/class/drm/{card}/device");
        self.put(&format!("{dev}/vendor"), "0x8086", Grant::No);
        let drv = self.path(&format!("sys/bus/pci/drivers/{driver}"));
        fs::create_dir_all(&drv).unwrap();
        let _ = std::os::unix::fs::symlink(&drv, self.path(&format!("{dev}/driver")));
        fs::create_dir_all(self.path(&format!("sys/class/drm/{card}-eDP-1"))).unwrap();
        let mut render = None;
        for (gt, idle) in gts {
            let media = idle.map(|i| i.ends_with("-mc")).unwrap_or(false);
            let f = format!("{dev}/tile0/{gt}/freq0");
            // grants.conf grants gt0's clocks and power profile only (the render GT here)
            let g = if *gt == "gt0" { Grant::Rw } else { Grant::No };
            self.put(&format!("{f}/rpn_freq"), rpn, Grant::No);
            self.put(&format!("{f}/rpe_freq"), if media { 500 } else { rpe }, Grant::No);
            self.put(&format!("{f}/rp0_freq"), if media { 1200 } else { rp0 }, Grant::No);
            self.put(&format!("{f}/min_freq"), if media { 600 } else { minf }, g);
            self.put(&format!("{f}/max_freq"), if media { 1200 } else { maxf }, g);
            self.put(&format!("{f}/act_freq"), if media { 0 } else { 1450 }, Grant::No);
            self.put(&format!("{f}/cur_freq"), if media { 0 } else { 1500 }, Grant::No);
            self.put(&format!("{f}/throttle/reasons"), "none", Grant::No);
            if let Some(i) = idle {
                self.put(&format!("{dev}/tile0/{gt}/gtidle/name"), i, Grant::No);
            }
            if !media && render.is_none() {
                render = Some(f);
            }
        }
        render.unwrap_or_default()
    }

    pub fn gpu_profile(&self, value: &str, card: &str, gt: &str) -> String {
        let p = format!("sys/class/drm/{card}/device/tile0/{gt}/freq0/power_profile");
        let shown = match value {
            "base" => "[base]    power_saving",
            "power_saving" => "base    [power_saving]",
            other => other,
        };
        let g = if gt == "gt0" { Grant::Rw } else { Grant::No };
        self.put(&p, shown, g);
        p
    }

    pub fn cpu(&self, policies: usize, epp: &str, governor: &str, no_turbo: u32) {
        let base = "sys/devices/system/cpu";
        self.put(&format!("{base}/intel_pstate/status"), "active", Grant::No);
        self.put(&format!("{base}/intel_pstate/no_turbo"), no_turbo, Grant::No);
        for i in 0..policies {
            let p = format!("{base}/cpufreq/policy{i}");
            self.put(&format!("{p}/scaling_governor"), governor, Grant::No);
            self.put(&format!("{p}/energy_performance_preference"), epp, Grant::Rw);
            self.put(
                &format!("{p}/energy_performance_available_preferences"),
                "default performance balance_performance balance_power power",
                Grant::No,
            );
            self.put(&format!("{p}/affected_cpus"), i, Grant::No);
            self.put(&format!("{p}/cpuinfo_max_freq"), 5_100_000, Grant::No);
            self.put(&format!("{p}/cpuinfo_min_freq"), 400_000, Grant::No);
            self.put(&format!("{p}/scaling_max_freq"), 5_100_000, Grant::Rw);
            self.put(&format!("{p}/scaling_min_freq"), 400_000, Grant::No);
            self.put(&format!("{p}/base_frequency"), 2_000_000, Grant::No);
            fs::create_dir_all(self.path(&format!("{base}/cpu{i}/cache/index3"))).unwrap();
        }
    }

    /// Hybrid core types: P cores `p`, atom cores `atom`, LP-E those in `no_l3` (no cache/index3).
    pub fn types(&self, p: &str, atom: &str, no_l3: &[usize], max_khz: [u64; 3], base_khz: [u64; 3]) {
        self.put("sys/devices/cpu_core/cpus", p, Grant::No);
        self.put("sys/devices/cpu_atom/cpus", atom, Grant::No);
        let list = |t: &str| -> Vec<usize> {
            let mut out = vec![];
            for part in t.split(',') {
                let (a, b) = part.split_once('-').unwrap_or((part, part));
                out.extend(a.parse::<usize>().unwrap()..=b.parse::<usize>().unwrap());
            }
            out
        };
        let cores = list(p);
        let atoms = list(atom);
        let base = "sys/devices/system/cpu";
        for i in cores.iter().chain(atoms.iter()) {
            let kind = if cores.contains(i) {
                0
            } else if no_l3.contains(i) {
                2
            } else {
                1
            };
            let pol = format!("{base}/cpufreq/policy{i}");
            if kind == 2 {
                let _ = fs::remove_dir_all(self.path(&format!("{base}/cpu{i}/cache/index3")));
            } else {
                fs::create_dir_all(self.path(&format!("{base}/cpu{i}/cache/index3"))).unwrap();
            }
            self.put(&format!("{pol}/affected_cpus"), i, Grant::No);
            self.put(&format!("{pol}/cpuinfo_max_freq"), max_khz[kind], Grant::No);
            self.put(&format!("{pol}/base_frequency"), base_khz[kind], Grant::No);
            self.put(&format!("{pol}/cpuinfo_min_freq"), 400_000, Grant::No);
            self.put(&format!("{pol}/scaling_max_freq"), max_khz[kind], Grant::Rw);
            self.put(&format!("{pol}/scaling_min_freq"), 400_000, Grant::No);
        }
    }

    pub fn perf(&self, max: u32, min: u32) {
        self.put("sys/devices/system/cpu/intel_pstate/max_perf_pct", max, Grant::Rw);
        self.put("sys/devices/system/cpu/intel_pstate/min_perf_pct", min, Grant::Rw);
    }

    pub fn soc(&self, balance: &str, offset: &str, profile: &str, name: &str, dev: &str, params: bool) -> String {
        if params {
            let base = "sys/module/processor_thermal_soc_slider/parameters";
            self.put(&format!("{base}/slider_balance"), balance, Grant::Rw);
            self.put(&format!("{base}/slider_offset"), offset, Grant::Rw);
        }
        let d = format!("sys/class/platform-profile/{dev}");
        self.put(&format!("{d}/name"), name, Grant::No);
        let g = if name == "SoC Power Slider" {
            Grant::Rw
        } else {
            Grant::No
        };
        self.put(&format!("{d}/profile"), profile, g);
        self.put(&format!("{d}/choices"), "low-power balanced performance", Grant::No);
        d
    }

    pub fn energy(&self, uj: u64, max_uj: u64) {
        self.put("sys/class/powercap/intel-rapl:0/energy_uj", uj, Grant::Secret(true));
        self.put("sys/class/powercap/intel-rapl:0/max_energy_range_uj", max_uj, Grant::No);
    }

    pub fn coretemp(&self, milli: i64) {
        let b = "sys/class/hwmon/hwmon3";
        self.put(&format!("{b}/name"), "coretemp", Grant::No);
        self.put(&format!("{b}/temp1_input"), milli, Grant::No);
        self.put(&format!("{b}/temp1_label"), "Package id 0", Grant::No);
        self.put(&format!("{b}/temp2_input"), 50_000, Grant::No);
        self.put(&format!("{b}/temp2_label"), "Core 0", Grant::No);
    }

    pub fn set_temp(&self, milli: i64) {
        self.poke("sys/class/hwmon/hwmon3/temp1_input", milli);
    }

    /// The oxpec hwmon (Linux 7.4+), fan on auto.
    pub fn oxp_ec(&self) -> String {
        let b = "sys/class/hwmon/hwmon5".to_string();
        self.put(&format!("{b}/name"), "oxp_ec", Grant::No);
        self.put(&format!("{b}/fan1_input"), 2400, Grant::No);
        self.put(&format!("{b}/pwm1_enable"), 2, Grant::Rw);
        self.put(&format!("{b}/pwm1"), 0, Grant::Rw);
        self.hidden(&format!("{b}/.ec_enable"), 0);
        self.hidden(&format!("{b}/.ec_pwm"), 0);
        b
    }

    /// A battery; `oxpec` adds the Linux 7.4 charge controls.
    pub fn battery(&self, name: &str, status: &str, capacity: u32, oxpec: bool) -> String {
        let b = format!("sys/class/power_supply/{name}");
        self.put(&format!("{b}/type"), "Battery", Grant::No);
        self.put(&format!("{b}/status"), status, Grant::No);
        self.put(&format!("{b}/capacity"), capacity, Grant::No);
        if oxpec {
            self.put(&format!("{b}/charge_control_end_threshold"), 100, Grant::Rw);
            self.put(
                &format!("{b}/charge_behaviour"),
                "[auto] inhibit-charge inhibit-charge-awake",
                Grant::Rw,
            );
        }
        b
    }

    pub fn ac(&self, online: bool, name: &str, kind: &str) {
        let b = format!("sys/class/power_supply/{name}");
        self.put(&format!("{b}/type"), kind, Grant::No);
        self.put(&format!("{b}/online"), if online { 1 } else { 0 }, Grant::No);
    }

    /// The charger: online changes and, as the OXP3 firmware does on every AC change, the MMIO
    /// limits are rewritten to 35/52 W.
    pub fn plug(&self, online: bool) {
        self.poke("sys/class/power_supply/ADP1/online", if online { 1 } else { 0 });
        self.poke(
            "sys/class/power_supply/BAT0/status",
            if online { "Charging" } else { "Discharging" },
        );
        self.set_watts("intel-rapl-mmio:0", "long_term", 35);
        self.set_watts("intel-rapl-mmio:0", "short_term", 52);
    }

    /// The owner's OXP3 at boot (BIOS 5.09): MSR 25/52/160 W, MMIO 35/52/160 W, xe card1 (render
    /// gt0 100..2300 MHz, media gt1), 14 policies (P 0-3, E 4-11, LP-E 12-13), intel_pstate caps
    /// 100/9, the SoC slider (03/00, balanced), gpu power profile base, package energy, coretemp
    /// 50 C, and on Linux 7.4 the oxpec fan and battery controls. On battery.
    pub fn oxp3(&self, linux74: bool) {
        self.dmi("ONE-NETBOOK", "ONEXPLAYER 3");
        self.boot_id("boot-1");
        self.rapl(
            "intel-rapl:0",
            25,
            52,
            160,
            &["long_term", "short_term", "peak_power"],
            25,
            "package-0",
        );
        self.rapl("intel-rapl:1", 0, 0, 0, &["long_term", "short_term"], 0, "psys");
        self.rapl("intel-rapl:0:0", 0, 0, 0, &["long_term"], 0, "core");
        self.rapl(
            "intel-rapl-mmio:0",
            35,
            52,
            160,
            &["long_term", "short_term", "peak_power"],
            25,
            "package-0",
        );
        self.xe(
            "card1",
            &[("gt0", Some("gt0-rc")), ("gt1", Some("gt1-mc"))],
            100,
            900,
            2300,
            850,
            2300,
            "xe",
        );
        self.gpu_profile("base", "card1", "gt0");
        self.cpu(14, "balance_performance", "powersave", 0);
        self.types(
            "0-3",
            "4-13",
            &[12, 13],
            [5_100_000, 3_800_000, 2_500_000],
            [2_000_000, 1_800_000, 1_200_000],
        );
        self.perf(100, 9);
        self.soc("03", "00", "balanced", "SoC Power Slider", "platform-profile-0", true);
        self.energy(1_000_000, 262_143_328_850);
        self.coretemp(50_000);
        self.ac(false, "ADP1", "Mains");
        self.battery("BAT0", "Discharging", 80, linux74);
        if linux74 {
            self.oxp_ec();
        }
    }
}
