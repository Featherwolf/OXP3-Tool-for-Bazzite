// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! The admin envelope: `/etc/oxp3-hw/limits.toml`, root-owned, read once at start.
//!
//! It may narrow any bound and may raise only `sustained_ac_max_w` (an AC-only sustained limit),
//! up to the device entry's `sustained_ac_admin_cap_w`. Anything else (an unknown key, a value of
//! the wrong type, a widening value, a malformed line) is an error and the provider does not
//! start: an ignored file that meant to narrow something would leave the bounds wider than the
//! admin asked for.
//!
//! The format is a strict subset of TOML: `key = integer` or `key = true|false` lines, `#`
//! comments and blank lines. No tables, strings, arrays or inline comments after values.

use crate::device::Device;

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Limits {
    pub sustained_min_w: Option<u32>,
    pub sustained_max_w: Option<u32>,
    pub sustained_ac_max_w: Option<u32>,
    pub boost_max_w: Option<u32>,
    pub boost_offset_max_w: Option<u32>,
    pub gpu_min_mhz: Option<u32>,
    pub gpu_max_mhz: Option<u32>,
    pub max_perf_pct_min: Option<u32>,
    pub min_perf_pct_ac_max: Option<u32>,
    pub soc_balance_min: Option<u32>,
    pub soc_balance_max: Option<u32>,
    pub soc_offset_max: Option<u32>,
    pub charge_min_percent: Option<u32>,
    pub fan_quiet: Option<bool>,
    pub fan_max: Option<bool>,
    pub charge_inhibit_awake: Option<bool>,
}

const MAX_FILE: usize = 16 * 1024;

/// Parse the file's text; `Err` names the line and the problem.
pub fn parse(text: &str) -> Result<Limits, String> {
    if text.len() > MAX_FILE {
        return Err(format!("larger than {MAX_FILE} bytes"));
    }
    let mut l = Limits::default();
    for (i, raw) in text.lines().enumerate() {
        let n = i + 1;
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (key, value) = line
            .split_once('=')
            .ok_or_else(|| format!("line {n}: expected key = value"))?;
        let key = key.trim();
        let value = value.trim();
        let int = || -> Result<u32, String> {
            if value.is_empty() || value.len() > 6 || !value.bytes().all(|b| b.is_ascii_digit()) {
                return Err(format!("line {n}: {key} needs a whole number"));
            }
            value
                .parse::<u32>()
                .map_err(|_| format!("line {n}: {key} needs a whole number"))
        };
        let boolean = || -> Result<bool, String> {
            match value {
                "true" => Ok(true),
                "false" => Ok(false),
                _ => Err(format!("line {n}: {key} needs true or false")),
            }
        };
        let slot_u = |s: &mut Option<u32>, v: u32| -> Result<(), String> {
            if s.replace(v).is_some() {
                return Err(format!("line {n}: {key} given twice"));
            }
            Ok(())
        };
        let slot_b = |s: &mut Option<bool>, v: bool| -> Result<(), String> {
            if s.replace(v).is_some() {
                return Err(format!("line {n}: {key} given twice"));
            }
            Ok(())
        };
        match key {
            "sustained_min_w" => slot_u(&mut l.sustained_min_w, int()?)?,
            "sustained_max_w" => slot_u(&mut l.sustained_max_w, int()?)?,
            "sustained_ac_max_w" => slot_u(&mut l.sustained_ac_max_w, int()?)?,
            "boost_max_w" => slot_u(&mut l.boost_max_w, int()?)?,
            "boost_offset_max_w" => slot_u(&mut l.boost_offset_max_w, int()?)?,
            "gpu_min_mhz" => slot_u(&mut l.gpu_min_mhz, int()?)?,
            "gpu_max_mhz" => slot_u(&mut l.gpu_max_mhz, int()?)?,
            "max_perf_pct_min" => slot_u(&mut l.max_perf_pct_min, int()?)?,
            "min_perf_pct_ac_max" => slot_u(&mut l.min_perf_pct_ac_max, int()?)?,
            "soc_balance_min" => slot_u(&mut l.soc_balance_min, int()?)?,
            "soc_balance_max" => slot_u(&mut l.soc_balance_max, int()?)?,
            "soc_offset_max" => slot_u(&mut l.soc_offset_max, int()?)?,
            "charge_min_percent" => slot_u(&mut l.charge_min_percent, int()?)?,
            "fan_quiet" => slot_b(&mut l.fan_quiet, boolean()?)?,
            "fan_max" => slot_b(&mut l.fan_max, boolean()?)?,
            "charge_inhibit_awake" => slot_b(&mut l.charge_inhibit_awake, boolean()?)?,
            _ => return Err(format!("line {n}: unknown key {key:?}")),
        }
    }
    Ok(l)
}

/// The session envelope before the kernel's ranges are known: the device entry intersected with
/// the admin file. GPU and CPU-frequency ranges are intersected with the kernel's at discovery.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Envelope {
    pub sustained_min_w: u32,
    /// The battery maximum (and the AC maximum without an admin AC limit).
    pub sustained_max_w: u32,
    /// An admin AC-only limit above `sustained_max_w`, if set.
    pub sustained_ac_max_w: Option<u32>,
    pub boost_min_w: u32,
    pub boost_max_w: u32,
    pub boost_default_w: u32,
    pub boost_offset_max_w: u32,
    pub gpu_min_mhz: Option<u32>,
    pub gpu_max_mhz: Option<u32>,
    pub max_perf_pct_min: u32,
    pub min_perf_pct_ac_max: u32,
    pub soc_balance: (u32, u32),
    pub soc_offset_max: u32,
    pub charge_min: u32,
    pub charge_max: u32,
    pub fan_modes: Vec<&'static str>,
    pub charge_behaviours: Vec<&'static str>,
}

impl Envelope {
    /// The sustained maximum `TdpLimitMax` publishes (constant).
    pub fn tdp_limit_max(&self) -> u32 {
        self.sustained_ac_max_w.unwrap_or(self.sustained_max_w)
    }
}

fn narrow(name: &str, lo: u32, hi: u32, v: Option<u32>) -> Result<Option<u32>, String> {
    match v {
        None => Ok(None),
        Some(x) if (lo..=hi).contains(&x) => Ok(Some(x)),
        Some(x) => Err(format!(
            "{name} = {x} is outside the device's {lo}..{hi} (it may only narrow)"
        )),
    }
}

/// Intersect the device entry with the admin limits.
pub fn envelope(d: &Device, l: &Limits) -> Result<Envelope, String> {
    let (smin, smax) = d.sustained_w;
    let sustained_min_w = narrow("sustained_min_w", smin, smax, l.sustained_min_w)?.unwrap_or(smin);
    let sustained_max_w = narrow("sustained_max_w", smin, smax, l.sustained_max_w)?.unwrap_or(smax);
    if sustained_min_w > sustained_max_w {
        return Err("sustained_min_w is above sustained_max_w".into());
    }
    let sustained_ac_max_w = match l.sustained_ac_max_w {
        None => None,
        Some(x) if x > sustained_max_w && x <= d.sustained_ac_admin_cap_w => Some(x),
        Some(x) if x <= sustained_max_w => {
            return Err(format!(
                "sustained_ac_max_w = {x} must be above the battery maximum {sustained_max_w}"
            ))
        }
        Some(x) => {
            return Err(format!(
                "sustained_ac_max_w = {x} is above this device's admin cap {}",
                d.sustained_ac_admin_cap_w
            ))
        }
    };
    let (bmin, bmax) = d.boost_w;
    let boost_max_w = narrow("boost_max_w", bmin, bmax, l.boost_max_w)?.unwrap_or(bmax);
    let top_sustained = sustained_ac_max_w.unwrap_or(sustained_max_w);
    if boost_max_w < top_sustained {
        return Err(format!(
            "boost_max_w = {boost_max_w} is below the sustained maximum {top_sustained}"
        ));
    }
    let boost_offset_max_w =
        narrow("boost_offset_max_w", 0, d.boost_offset_max_w, l.boost_offset_max_w)?.unwrap_or(d.boost_offset_max_w);
    let gpu_min_mhz = narrow("gpu_min_mhz", 1, 100_000, l.gpu_min_mhz)?;
    let gpu_max_mhz = narrow("gpu_max_mhz", 1, 100_000, l.gpu_max_mhz)?;
    if let (Some(a), Some(b)) = (gpu_min_mhz, gpu_max_mhz) {
        if a > b {
            return Err("gpu_min_mhz is above gpu_max_mhz".into());
        }
    }
    let max_perf_pct_min =
        narrow("max_perf_pct_min", d.max_perf_pct_min, 100, l.max_perf_pct_min)?.unwrap_or(d.max_perf_pct_min);
    let min_perf_pct_ac_max = narrow("min_perf_pct_ac_max", 1, d.min_perf_pct_ac_max, l.min_perf_pct_ac_max)?
        .unwrap_or(d.min_perf_pct_ac_max);
    let (blo, bhi) = d.soc_balance;
    let soc_lo = narrow("soc_balance_min", blo, bhi, l.soc_balance_min)?.unwrap_or(blo);
    let soc_hi = narrow("soc_balance_max", blo, bhi, l.soc_balance_max)?.unwrap_or(bhi);
    if soc_lo > soc_hi {
        return Err("soc_balance_min is above soc_balance_max".into());
    }
    let soc_offset_max = narrow("soc_offset_max", 0, d.soc_offset_max, l.soc_offset_max)?.unwrap_or(d.soc_offset_max);
    let (clo, chi) = d.charge_percent;
    let charge_min = narrow("charge_min_percent", clo, chi, l.charge_min_percent)?.unwrap_or(clo);
    let mut fan_modes = vec!["auto"];
    if l.fan_quiet != Some(false) {
        fan_modes.push("quiet");
    }
    if l.fan_max != Some(false) {
        fan_modes.push("max");
    }
    let charge_behaviours = d
        .charge_behaviours
        .iter()
        .copied()
        .filter(|b| *b != "inhibit-charge-awake" || l.charge_inhibit_awake != Some(false))
        .collect();
    Ok(Envelope {
        sustained_min_w,
        sustained_max_w,
        sustained_ac_max_w,
        boost_min_w: bmin,
        boost_max_w,
        boost_default_w: d.boost_default_w.min(boost_max_w),
        boost_offset_max_w,
        gpu_min_mhz,
        gpu_max_mhz,
        max_perf_pct_min,
        min_perf_pct_ac_max,
        soc_balance: (soc_lo, soc_hi),
        soc_offset_max,
        charge_min,
        charge_max: chi,
        fan_modes,
        charge_behaviours,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::device::ONEXPLAYER_3;

    #[test]
    fn empty_is_the_device_entry() {
        let e = envelope(&ONEXPLAYER_3, &parse("").unwrap()).unwrap();
        assert_eq!(
            (e.sustained_min_w, e.sustained_max_w, e.sustained_ac_max_w),
            (5, 35, None)
        );
        assert_eq!(e.tdp_limit_max(), 35);
        assert_eq!(
            (e.boost_min_w, e.boost_max_w, e.boost_default_w, e.boost_offset_max_w),
            (5, 52, 52, 47)
        );
        assert_eq!((e.charge_min, e.charge_max), (50, 100));
        assert_eq!(e.fan_modes, vec!["auto", "quiet", "max"]);
        assert_eq!(e.charge_behaviours, vec!["auto", "inhibit-charge-awake"]);
    }

    #[test]
    fn ac_limit_only_up_to_the_cap() {
        let e = envelope(&ONEXPLAYER_3, &parse("# AC\nsustained_ac_max_w = 40\n").unwrap()).unwrap();
        assert_eq!(
            (e.sustained_max_w, e.sustained_ac_max_w, e.tdp_limit_max()),
            (35, Some(40), 40)
        );
        assert!(envelope(&ONEXPLAYER_3, &parse("sustained_ac_max_w = 53").unwrap()).is_err());
        assert!(envelope(&ONEXPLAYER_3, &parse("sustained_ac_max_w = 30").unwrap()).is_err());
        assert!(envelope(&ONEXPLAYER_3, &parse("sustained_ac_max_w = 52").unwrap()).is_ok());
    }

    #[test]
    fn narrowing_only() {
        let e = envelope(&ONEXPLAYER_3, &parse("sustained_max_w = 25\nboost_max_w = 40\ncharge_min_percent = 60\nfan_quiet = false\ncharge_inhibit_awake = false").unwrap()).unwrap();
        assert_eq!(
            (e.sustained_max_w, e.boost_max_w, e.boost_default_w, e.charge_min),
            (25, 40, 40, 60)
        );
        assert_eq!(e.fan_modes, vec!["auto", "max"]);
        assert_eq!(e.charge_behaviours, vec!["auto"]);
        for bad in [
            "sustained_max_w = 36",
            "sustained_min_w = 4",
            "boost_max_w = 60",
            "boost_max_w = 30",
            "charge_min_percent = 40",
            "max_perf_pct_min = 20",
            "min_perf_pct_ac_max = 60",
            "soc_balance_max = 6",
            "soc_offset_max = 4",
            "sustained_min_w = 30\nsustained_max_w = 20",
        ] {
            assert!(envelope(&ONEXPLAYER_3, &parse(bad).unwrap()).is_err(), "{bad}");
        }
    }

    #[test]
    fn malformed_files_are_errors() {
        for bad in [
            "sustained_max_w",
            "sustained_max_w = \"30\"",
            "sustained_max_w = 30 # c",
            "sustained_max_w = -1",
            "sustained_max_w = 3.5",
            "sustained_max_w = 30\nsustained_max_w = 31",
            "[limits]",
            "unknown = 1",
            "fan_quiet = 1",
            "sustained_max_w = 99999999999",
        ] {
            assert!(parse(bad).is_err(), "{bad}");
        }
        assert!(parse(&"#".repeat(MAX_FILE + 1)).is_err());
    }

    #[test]
    fn fuzz_never_panics_and_never_widens() {
        // A small deterministic fuzzer: random lines of keys, values and noise.
        let keys = [
            "sustained_min_w",
            "sustained_max_w",
            "sustained_ac_max_w",
            "boost_max_w",
            "boost_offset_max_w",
            "gpu_min_mhz",
            "gpu_max_mhz",
            "max_perf_pct_min",
            "min_perf_pct_ac_max",
            "soc_balance_min",
            "soc_balance_max",
            "soc_offset_max",
            "charge_min_percent",
            "fan_quiet",
            "fan_max",
            "x",
            "",
        ];
        let values = [
            "0",
            "1",
            "5",
            "35",
            "40",
            "52",
            "53",
            "100",
            "4294967295",
            "true",
            "false",
            "",
            "=",
            "\"a\"",
            "-3",
        ];
        let mut seed: u64 = 0x5eed;
        let mut next = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        for _ in 0..20_000 {
            let mut text = String::new();
            for _ in 0..(next() % 5) {
                let k = keys[(next() % keys.len() as u64) as usize];
                let v = values[(next() % values.len() as u64) as usize];
                let sep = if next() % 7 == 0 { " " } else { " = " };
                text.push_str(&format!("{k}{sep}{v}\n"));
            }
            if let Ok(l) = parse(&text) {
                if let Ok(e) = envelope(&ONEXPLAYER_3, &l) {
                    assert!(e.sustained_min_w >= 5 && e.sustained_max_w <= 35);
                    assert!(e.tdp_limit_max() <= 52 && e.boost_max_w <= 52 && e.boost_max_w >= e.tdp_limit_max());
                    assert!(e.charge_min >= 50 && e.max_perf_pct_min >= 30 && e.min_perf_pct_ac_max <= 50);
                    assert!(e.soc_balance.0 >= 1 && e.soc_balance.1 <= 5 && e.soc_offset_max <= 3);
                }
            }
        }
    }
}
