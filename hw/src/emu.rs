// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! Test builds only (feature `test-sysfs`): the kernel's store semantics on a fake sysfs tree.
//!
//! A plain file accepts any text, so a fake tree alone cannot show what the kernel does with a
//! write. This module sits where the real `write(2)` would and does what the kernel source does
//! for each attribute the provider writes (Linux 7.2 to 7.4: powercap, xe, intel_pstate, cpufreq,
//! processor_thermal_soc_slider, platform_profile, power_supply with the oxpec extension, and the
//! oxpec hwmon). The open itself is the real one (`O_WRONLY | O_NOFOLLOW`), so file permissions,
//! missing files and symlinks behave as on the device.
//!
//! The test controls two files under `<root>/.kernel/`:
//! - `rules.json`: `{"fail": {"<path suffix>": "EACCES" | ["EAGAIN", "EAGAIN"]}, "ignore": ["<suffix>"],
//!   "min_perf_pct_min": 9}`. A list is consumed one entry per write (`null` lets the write through);
//!   `ignore` makes a write succeed without changing anything (a locked RAPL MMIO register).
//! - `writes.log`: one line per attempted write: `<path relative to root>\t<text>\t<ok|ignored|ERRNO>`.
//!
//! Nothing here is compiled into a release build.

use std::fs;
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

const RAPL_UNIT_UW: u64 = 125_000;
/// The powercap register field is 15 bits wide: larger values wrap (5000 W reads about 904 W).
const RAPL_FIELD_MASK: u64 = 0x7fff;

fn err(code: i32) -> io::Error {
    io::Error::from_raw_os_error(code)
}

fn errno_by_name(name: &str) -> Option<i32> {
    Some(match name {
        "EACCES" => libc::EACCES,
        "EAGAIN" => libc::EAGAIN,
        "EINVAL" => libc::EINVAL,
        "EBUSY" => libc::EBUSY,
        "EIO" => libc::EIO,
        "EPERM" => libc::EPERM,
        "ENODEV" => libc::ENODEV,
        "EOPNOTSUPP" => libc::EOPNOTSUPP,
        _ => return None,
    })
}

fn errno_name(code: i32) -> String {
    for n in [
        "EACCES",
        "EAGAIN",
        "EINVAL",
        "EBUSY",
        "EIO",
        "EPERM",
        "ENODEV",
        "EOPNOTSUPP",
    ] {
        if errno_by_name(n) == Some(code) {
            return n.to_string();
        }
    }
    match code {
        libc::ENOENT => "ENOENT".into(),
        libc::ELOOP => "ELOOP".into(),
        libc::EISDIR => "EISDIR".into(),
        _ => format!("E{code}"),
    }
}

fn kdir(root: &Path) -> PathBuf {
    root.join(".kernel")
}

fn log(root: &Path, rel: &str, text: &str, result: &str) {
    if let Ok(mut f) = fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(kdir(root).join("writes.log"))
    {
        let _ = writeln!(f, "{rel}\t{text}\t{result}");
    }
}

/// Reads are logged only while the test created `.kernel/reads.log` (to prove that the tick never
/// opens a file whose read wakes the GPU). While `.kernel/slow.json` (`{"<path suffix>": ms}`)
/// exists, a read of a matching file takes that long, as an embedded-controller transaction does
/// (oxpec's pwm1_enable, the battery's charge files).
pub fn log_read(root: &Path, path: &Path) {
    if let Ok(text) = fs::read_to_string(kdir(root).join("slow.json")) {
        if let Ok(serde_json::Value::Object(m)) = serde_json::from_str::<serde_json::Value>(&text) {
            let rel = path.strip_prefix(root).unwrap_or(path).to_string_lossy().into_owned();
            if let Some(ms) = m
                .iter()
                .find(|(k, _)| rel.ends_with(k.as_str()))
                .and_then(|(_, v)| v.as_u64())
            {
                std::thread::sleep(std::time::Duration::from_millis(ms));
            }
        }
    }
    let log = kdir(root).join("reads.log");
    if log.exists() {
        if let Ok(mut f) = fs::OpenOptions::new().append(true).open(log) {
            let rel = path.strip_prefix(root).unwrap_or(path);
            let _ = writeln!(f, "{}", rel.display());
        }
    }
}

/// Apply rules.json: Some(errno) to fail this write, Ok(true) to ignore it.
fn rules(root: &Path, rel: &str) -> (Option<i32>, bool, Option<u64>) {
    let path = kdir(root).join("rules.json");
    let Ok(text) = fs::read_to_string(&path) else {
        return (None, false, None);
    };
    let Ok(mut v) = serde_json::from_str::<serde_json::Value>(&text) else {
        return (None, false, None);
    };
    let mut fail = None;
    let mut changed = false;
    if let Some(map) = v.get_mut("fail").and_then(|f| f.as_object_mut()) {
        let key = map.keys().find(|k| rel.ends_with(k.as_str())).cloned();
        if let Some(k) = key {
            match map.get_mut(&k) {
                Some(serde_json::Value::String(s)) => fail = errno_by_name(s),
                Some(serde_json::Value::Array(list)) if !list.is_empty() => {
                    let first = list.remove(0);
                    changed = true;
                    if let Some(s) = first.as_str() {
                        fail = errno_by_name(s);
                    }
                }
                _ => {}
            }
        }
    }
    if changed {
        let _ = fs::write(&path, serde_json::to_vec_pretty(&v).unwrap_or_default());
    }
    let ignore = v
        .get("ignore")
        .and_then(|i| i.as_array())
        .map(|a| a.iter().filter_map(|s| s.as_str()).any(|s| rel.ends_with(s)))
        .unwrap_or(false);
    let min_min = v.get("min_perf_pct_min").and_then(|x| x.as_u64());
    (fail, ignore, min_min)
}

fn read(p: &Path) -> Option<String> {
    fs::read_to_string(p).ok().map(|s| s.trim().to_string())
}

fn read_u64(p: &Path) -> Option<u64> {
    read(p)?.split_whitespace().next()?.parse().ok()
}

/// kstrtou* with base 0: decimal, or 0x-prefixed hex.
fn kstrtou(text: &str) -> Result<u64, io::Error> {
    let t = text.trim();
    let r = if let Some(h) = t.strip_prefix("0x") {
        u64::from_str_radix(h, 16)
    } else {
        t.parse::<u64>()
    };
    r.map_err(|_| err(libc::EINVAL))
}

fn kstrtou_hex(text: &str) -> Result<u64, io::Error> {
    u64::from_str_radix(text.trim().trim_start_matches("0x"), 16).map_err(|_| err(libc::EINVAL))
}

/// What the kernel stores and shows after a write of `text` to `path`, or the errno its store
/// function returns. Side files (another attribute that changes with this one) are returned too.
fn semantics(root: &Path, path: &Path, text: &str, min_perf_min: u64) -> io::Result<Vec<(PathBuf, String)>> {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let dir = path.parent().unwrap_or(Path::new("/"));
    let t = text.trim();

    // powercap: power_limit_uw is truncated to the power unit and the 15-bit register field;
    // there is no range check (a value below one unit becomes 0 W).
    if name.starts_with("constraint_") && name.ends_with("_power_limit_uw") {
        let uw: u64 = t.parse().map_err(|_| err(libc::EINVAL))?;
        let units = (uw / RAPL_UNIT_UW) & RAPL_FIELD_MASK;
        return Ok(vec![(path.to_path_buf(), (units * RAPL_UNIT_UW).to_string())]);
    }
    // xe freq0: kstrtou32, then EINVAL outside [rpn, rp0]
    if (name == "min_freq" || name == "max_freq") && dir.join("rpn_freq").exists() {
        let mhz = kstrtou(t)?;
        let rpn = read_u64(&dir.join("rpn_freq")).unwrap_or(0);
        let rp0 = read_u64(&dir.join("rp0_freq")).unwrap_or(u64::MAX);
        if mhz < rpn || mhz > rp0 || mhz > u32::MAX as u64 {
            return Err(err(libc::EINVAL));
        }
        return Ok(vec![(path.to_path_buf(), mhz.to_string())]);
    }
    if name == "power_profile" && dir.join("rpn_freq").exists() {
        let shown = match t {
            "base" => "[base]    power_saving",
            "power_saving" => "base    [power_saving]",
            _ => return Err(err(libc::EINVAL)),
        };
        return Ok(vec![(path.to_path_buf(), shown.to_string())]);
    }
    // intel_pstate EPP: a listed word or a raw 0..255; EBUSY under the performance governor
    if name == "energy_performance_preference" {
        let avail = read(&dir.join("energy_performance_available_preferences")).unwrap_or_default();
        let word_ok = avail.split_whitespace().any(|w| w == t);
        let raw_ok = !t.is_empty()
            && t.len() <= 3
            && t.bytes().all(|b| b.is_ascii_digit())
            && t.parse::<u32>().map(|v| v <= 255).unwrap_or(false);
        if !word_ok && !raw_ok {
            return Err(err(libc::EINVAL));
        }
        if read(&dir.join("scaling_governor")).as_deref() == Some("performance") && t != "performance" {
            return Err(err(libc::EBUSY));
        }
        return Ok(vec![(path.to_path_buf(), t.to_string())]);
    }
    // cpufreq scaling_max_freq: the policy's effective maximum, clamped by the driver
    if name == "scaling_max_freq" {
        let khz = kstrtou(t)?;
        let lo = read_u64(&dir.join("scaling_min_freq")).unwrap_or(0);
        let mut hi = read_u64(&dir.join("cpuinfo_max_freq")).unwrap_or(u64::MAX);
        let no_turbo = dir
            .parent()
            .and_then(|p| p.parent())
            .map(|cpu| cpu.join("intel_pstate/no_turbo"));
        if no_turbo.and_then(|p| read(&p)).as_deref() == Some("1") {
            if let Some(base) = read_u64(&dir.join("base_frequency")) {
                hi = hi.min(base);
            }
        }
        return Ok(vec![(path.to_path_buf(), khz.clamp(lo, hi.max(lo)).to_string())]);
    }
    // intel_pstate global caps: clamp_t(max, min_perf_pct, 100) and clamp_t(min, minimum, max)
    if (name == "max_perf_pct" || name == "min_perf_pct") && dir.ends_with("intel_pstate") {
        let v = kstrtou(t)?;
        let cur_max = read_u64(&dir.join("max_perf_pct")).unwrap_or(100);
        let cur_min = read_u64(&dir.join("min_perf_pct")).unwrap_or(min_perf_min);
        let stored = if name == "max_perf_pct" {
            v.clamp(cur_min, 100)
        } else {
            v.clamp(min_perf_min, cur_max)
        };
        return Ok(vec![(path.to_path_buf(), stored.to_string())]);
    }
    // processor_thermal_soc_slider: hex u8; balance refuses 0 and 6 (and above) and shows the
    // value in use until a platform-profile write; offset refuses above 6
    if name == "slider_balance" {
        let v = kstrtou_hex(t)?;
        if v == 0 || v >= 6 {
            return Err(err(libc::EINVAL));
        }
        return Ok(vec![(kdir(root).join("slider_balance.pending"), format!("{v:02x}"))]);
    }
    if name == "slider_offset" {
        let v = kstrtou_hex(t)?;
        if v > 6 {
            return Err(err(libc::EINVAL));
        }
        return Ok(vec![(path.to_path_buf(), format!("{v:02x}"))]);
    }
    // platform_profile: a known profile name the handler offers; any write sends the slider's
    // pending balance to the firmware
    if name == "profile" && dir.join("choices").exists() {
        const NAMES: [&str; 7] = [
            "low-power",
            "cool",
            "quiet",
            "balanced",
            "balanced-performance",
            "performance",
            "custom",
        ];
        if !NAMES.contains(&t) || t == "custom" {
            return Err(err(libc::EINVAL));
        }
        let choices = read(&dir.join("choices")).unwrap_or_default();
        if !choices.split_whitespace().any(|c| c == t) {
            return Err(err(libc::EOPNOTSUPP));
        }
        let mut out = vec![(path.to_path_buf(), t.to_string())];
        let pending = kdir(root).join("slider_balance.pending");
        if let Some(b) = read(&pending) {
            let param = root.join("sys/module/processor_thermal_soc_slider/parameters/slider_balance");
            out.push((param, b));
            let _ = fs::remove_file(pending);
        }
        return Ok(out);
    }
    // power_supply with the oxpec extension
    if name == "charge_control_end_threshold" {
        let v: i64 = t.parse().map_err(|_| err(libc::EINVAL))?;
        if !(0..=100).contains(&v) {
            return Err(err(libc::EINVAL));
        }
        return Ok(vec![(path.to_path_buf(), v.to_string())]);
    }
    if name == "charge_behaviour" {
        const ALL: [&str; 3] = ["auto", "inhibit-charge", "inhibit-charge-awake"];
        if !ALL.contains(&t) {
            return Err(err(libc::EINVAL));
        }
        let shown: Vec<String> = ALL
            .iter()
            .map(|w| if *w == t { format!("[{w}]") } else { w.to_string() })
            .collect();
        return Ok(vec![(path.to_path_buf(), shown.join(" "))]);
    }
    // oxpec hwmon: pwm1_enable 1 manual, 2 auto, 0 manual at full duty; pwm1 0..255 scaled to
    // the EC's 0..184 and back; pwm1_enable reads 2 (auto), else 0 at full duty, else 1
    if name == "pwm1_enable" || name == "pwm1" {
        let ec_en = dir.join(".ec_enable");
        let ec_pwm = dir.join(".ec_pwm");
        let mut enable = read_u64(&ec_en).unwrap_or(0); // EC register: 0 auto, 1 manual
        let mut duty = read_u64(&ec_pwm).unwrap_or(0);
        let v: i64 = t.parse().map_err(|_| err(libc::EINVAL))?;
        if name == "pwm1_enable" {
            match v {
                1 => enable = 1,
                2 => enable = 0,
                0 => {
                    enable = 1;
                    duty = 255 * 184 / 255;
                }
                _ => return Err(err(libc::EINVAL)),
            }
        } else {
            if !(0..=255).contains(&v) {
                return Err(err(libc::EINVAL));
            }
            duty = v as u64 * 184 / 255;
        }
        let shown_pwm = duty * 255 / 184;
        let shown_en = if enable == 0 {
            2
        } else if shown_pwm == 255 {
            0
        } else {
            1
        };
        return Ok(vec![
            (ec_en, enable.to_string()),
            (ec_pwm, duty.to_string()),
            (dir.join("pwm1"), shown_pwm.to_string()),
            (dir.join("pwm1_enable"), shown_en.to_string()),
        ]);
    }
    Ok(vec![(path.to_path_buf(), t.to_string())])
}

/// The emulated write: the real permission and symlink checks, then the kernel's semantics.
pub fn write(root: &Path, path: &Path, text: &str) -> io::Result<()> {
    let rel = path
        .strip_prefix(root)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| path.to_string_lossy().into_owned());
    let result = (|| -> io::Result<&'static str> {
        // the real open: EACCES without a grant, ELOOP for a symlink, ENOENT for a missing file
        let f = fs::OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)?;
        let (fail, ignore, min_min) = rules(root, &rel);
        if let Some(code) = fail {
            return Err(err(code));
        }
        if ignore {
            return Ok("ignored");
        }
        let changes = semantics(root, path, text, min_min.unwrap_or(1))?;
        for (p, shown) in changes {
            if p == path {
                // new text first, then cut: a concurrent reader never sees an empty file
                use std::os::unix::fs::FileExt;
                let text = format!("{shown}\n");
                f.write_at(text.as_bytes(), 0)?;
                f.set_len(text.len() as u64)?;
            } else {
                // a side file the kernel changes with this one (hidden state, or another attribute)
                fs::write(&p, format!("{shown}\n"))?;
            }
        }
        Ok("ok")
    })();
    match &result {
        Ok(r) => log(root, &rel, text, r),
        Err(e) => log(root, &rel, text, &errno_name(e.raw_os_error().unwrap_or(0))),
    }
    result.map(|_| ())
}
