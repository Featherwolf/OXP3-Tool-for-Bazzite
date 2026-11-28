// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! The compiled device table and hard limits (Bathysphere SPEC 11.3).
//!
//! Three tiers bound every value the provider writes:
//! 1. hard limits per knob kind (`hard`), which no device entry may exceed;
//! 2. the compiled DMI device entry (`ONEXPLAYER_3`), intersected with the kernel's and the
//!    firmware's own ranges at discovery;
//! 3. the admin envelope from the root-owned `/etc/oxp3-hw/limits.toml` (`limits.rs`), which may
//!    narrow anything and raise only the AC-only sustained limit, up to the entry's admin cap.
//!
//! Bounds never come from current register values, `constraint_N_max_power_uw`, the caller, the
//! network or any catalog. Every bound below cites where its number comes from.

/// Hard limits per knob kind. A device entry outside these fails the `entries_are_inside_hard_limits`
/// test, so it can never be compiled into a release.
pub mod hard {
    /// RAPL power limits in milliwatts: never below 3 W (a limit below one power unit truncates to
    /// 0 W and hangs the machine) and never above 60 W.
    pub const RAPL_MIN_MW: u32 = 3_000;
    pub const RAPL_MAX_MW: u32 = 60_000;
    /// The smallest RAPL power unit the kernel accepts as non-zero on Intel parts: 1/8 W.
    pub const RAPL_UNIT_UW: u64 = 125_000;
    /// Battery end-of-charge threshold in percent.
    pub const CHARGE_MIN: u32 = 20;
    pub const CHARGE_MAX: u32 = 100;
    /// A manual fan duty (pwm1, 0..255) never below this: the fan must keep moving air.
    pub const FAN_DUTY_MIN: u32 = 80;
    pub const FAN_DUTY_MAX: u32 = 255;
    /// intel_pstate max_perf_pct never below this.
    pub const MAX_PERF_PCT_MIN: u32 = 30;
    /// The fan returns to the firmware's automatic mode at this package temperature, at the
    /// latest.
    pub const FAN_REVERT_C_MAX: i32 = 95;
}

/// The DMI fields that must all match exactly (whitespace-trimmed). The udev rule matches
/// `board_vendor` and `board_name`; the kernel's oxpec driver matches the board name.
#[derive(Debug)]
pub struct Dmi {
    pub sys_vendor: &'static str,
    pub product_name: &'static str,
    pub board_vendor: &'static str,
    pub board_name: &'static str,
}

/// Fan modes and the safety rule of the oxpec hwmon `oxp_ec`.
#[derive(Debug)]
pub struct Fan {
    /// pwm1_enable values: 2 = the EC's automatic curve, 1 = manual duty, 0 = full speed.
    pub enable_auto: u32,
    pub enable_manual: u32,
    pub enable_max: u32,
    /// The pwm1 duty written for "quiet".
    pub quiet_pwm: u32,
    /// Back to auto at or above this package temperature (°C).
    pub revert_c: i32,
    /// "quiet" is refused this long after a temperature revert.
    pub cooldown_s: u64,
}

/// One compiled device entry. All ranges are inclusive.
#[derive(Debug)]
pub struct Device {
    /// The Hw1 `DeviceId`.
    pub id: &'static str,
    pub dmi: Dmi,
    /// Sustained power (RAPL PL1, `long_term`) in W, on battery and by default on AC.
    pub sustained_w: (u32, u32),
    /// The highest AC-only sustained power an admin may enable in /etc (W).
    pub sustained_ac_admin_cap_w: u32,
    /// Boost power (RAPL PL2, `short_term`) in W.
    pub boost_w: (u32, u32),
    /// PL2 in the "firmware" boost mode: the firmware's boot PL2, never read from registers.
    pub boost_default_w: u32,
    /// The largest "follow" offset above PL1 (W).
    pub boost_offset_max_w: u32,
    /// intel_pstate max_perf_pct lower bound (percent).
    pub max_perf_pct_min: u32,
    /// intel_pstate min_perf_pct upper bound while on AC (percent).
    pub min_perf_pct_ac_max: u32,
    /// SoC power slider balance and offset (processor_thermal_soc_slider).
    pub soc_balance: (u32, u32),
    pub soc_offset_max: u32,
    /// Battery end-of-charge threshold (percent) and its default ("no limit").
    pub charge_percent: (u32, u32),
    pub charge_default: u32,
    /// charge_behaviour words offered, in this order.
    pub charge_behaviours: &'static [&'static str],
    pub fan: Fan,
    /// Where each number comes from (shown in Describe's "source"; never parsed).
    pub sources: Sources,
}

#[derive(Debug)]
pub struct Sources {
    pub sustained: &'static str,
    pub sustained_ac: &'static str,
    pub boost: &'static str,
    pub gpu: &'static str,
    pub cpu_limits: &'static str,
    pub epp: &'static str,
    pub soc: &'static str,
    pub charge: &'static str,
    pub charge_behaviour: &'static str,
    pub fan: &'static str,
    pub package_power: &'static str,
}

/// The ONEXPLAYER 3 (Intel Panther Lake, BIOS 5.09 measurements from OXP3 Deck's DESIGN.md).
pub const ONEXPLAYER_3: Device = Device {
    id: "onexplayer-3",
    dmi: Dmi {
        sys_vendor: "ONE-NETBOOK",
        product_name: "ONEXPLAYER 3",
        board_vendor: "ONE-NETBOOK",
        board_name: "ONEXPLAYER 3",
    },
    sustained_w: (5, 35),
    sustained_ac_admin_cap_w: 52,
    boost_w: (5, 52),
    boost_default_w: 52,
    boost_offset_max_w: 47,
    max_perf_pct_min: 30,
    min_perf_pct_ac_max: 50,
    soc_balance: (1, 5),
    soc_offset_max: 3,
    charge_percent: (50, 100),
    charge_default: 100,
    charge_behaviours: &["auto", "inhibit-charge-awake"],
    fan: Fan {
        enable_auto: 2,
        enable_manual: 1,
        enable_max: 0,
        quiet_pwm: 102,
        revert_c: 90,
        cooldown_s: 600,
    },
    sources: Sources {
        sustained: "OEM presets 5/17/35 W and 35 W sustained spec (OXP3 Deck DESIGN.md, Power: Range)",
        sustained_ac: "admin opt-in in /etc/oxp3-hw/limits.toml, AC only, at most the firmware boot PL2 (52 W)",
        boost:
            "firmware boot PL2 52 W on both RAPL interfaces, BIOS 5.09 (OXP3 Deck DESIGN.md); never read from registers",
        gpu: "xe render GT rpn_freq..rp0_freq read at start (the kernel refuses values outside)",
        cpu_limits:
            "max_perf_pct >= 30 (hard limit); min_perf_pct <= 50 and above the boot value only on AC (OXP3 Deck rules)",
        epp: "the kernel's energy_performance_available_preferences",
        soc: "slider balance 1..5 (the kernel refuses 0 and 6), offset 0..3 (OXP3 Deck DESIGN.md, Power 0.4)",
        charge: "50..100 % (OXP3 Deck extras: charge limit), oxpec 0..100",
        charge_behaviour: "auto and inhibit-charge-awake only (OXP3 Deck extras: never inhibit-charge)",
        fan: "oxpec pwm1_enable 2/1/0, quiet duty 102, back to auto at 90 C (OXP3 Deck extras)",
        package_power: "intel-rapl:0 energy_uj, 1 s windows rounded to 100 mW (CVE-2020-8694)",
    },
};

/// Every compiled device entry. There is no generic entry: no exact match, no export.
pub const DEVICES: &[&Device] = &[&ONEXPLAYER_3];

/// The entry whose DMI fields all equal the trimmed values `read` returns, if any.
pub fn match_dmi(read: impl Fn(&str) -> Option<String>) -> Option<&'static Device> {
    DEVICES.iter().copied().find(|d| {
        let want = [
            ("sys_vendor", d.dmi.sys_vendor),
            ("product_name", d.dmi.product_name),
            ("board_vendor", d.dmi.board_vendor),
            ("board_name", d.dmi.board_name),
        ];
        want.iter()
            .all(|(field, value)| read(field).map(|v| v.trim() == *value).unwrap_or(false))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn w_ok(w: u32) -> bool {
        (hard::RAPL_MIN_MW..=hard::RAPL_MAX_MW).contains(&(w * 1000))
    }

    #[test]
    fn entries_are_inside_hard_limits() {
        for d in DEVICES {
            assert!(w_ok(d.sustained_w.0) && w_ok(d.sustained_w.1), "{}", d.id);
            assert!(d.sustained_w.0 <= d.sustained_w.1);
            assert!(w_ok(d.sustained_ac_admin_cap_w) && d.sustained_ac_admin_cap_w >= d.sustained_w.1);
            assert!(w_ok(d.boost_w.0) && w_ok(d.boost_w.1));
            assert!(d.boost_w.0 <= d.boost_default_w && d.boost_default_w <= d.boost_w.1);
            // PL2 >= PL1 must always be possible, also at the admin AC cap
            assert!(d.boost_w.1 >= d.sustained_w.1 && d.boost_w.1 >= d.sustained_ac_admin_cap_w);
            assert!(d.boost_offset_max_w <= d.boost_w.1 - d.sustained_w.0);
            assert!(d.max_perf_pct_min >= hard::MAX_PERF_PCT_MIN && d.max_perf_pct_min <= 100);
            assert!(d.min_perf_pct_ac_max >= 1 && d.min_perf_pct_ac_max <= 100);
            assert!(d.soc_balance.0 >= 1 && d.soc_balance.1 <= 5 && d.soc_balance.0 <= d.soc_balance.1);
            assert!(d.soc_offset_max <= 6);
            assert!(d.charge_percent.0 >= hard::CHARGE_MIN && d.charge_percent.1 <= hard::CHARGE_MAX);
            assert!((d.charge_percent.0..=d.charge_percent.1).contains(&d.charge_default));
            assert!(d
                .charge_behaviours
                .iter()
                .all(|b| ["auto", "inhibit-charge-awake"].contains(b)));
            assert_eq!(d.charge_behaviours.first(), Some(&"auto"));
            assert!((hard::FAN_DUTY_MIN..=hard::FAN_DUTY_MAX).contains(&d.fan.quiet_pwm));
            assert!(d.fan.revert_c <= hard::FAN_REVERT_C_MAX && d.fan.revert_c >= 60);
            assert!(d.fan.cooldown_s >= 60);
            assert_ne!(d.fan.enable_auto, d.fan.enable_manual);
        }
    }

    #[test]
    fn every_bound_cites_a_source() {
        for d in DEVICES {
            let s = &d.sources;
            for (name, text) in [
                ("sustained", s.sustained),
                ("sustained_ac", s.sustained_ac),
                ("boost", s.boost),
                ("gpu", s.gpu),
                ("cpu_limits", s.cpu_limits),
                ("epp", s.epp),
                ("soc", s.soc),
                ("charge", s.charge),
                ("charge_behaviour", s.charge_behaviour),
                ("fan", s.fan),
                ("package_power", s.package_power),
            ] {
                assert!(text.len() >= 20, "{}: source for {name} is missing", d.id);
            }
        }
    }

    #[test]
    fn ids_follow_the_hw1_pattern() {
        for d in DEVICES {
            let ok = d.id.len() <= 64
                && d.id
                    .chars()
                    .next()
                    .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
                && d.id
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-');
            assert!(ok, "{}", d.id);
        }
    }

    #[test]
    fn dmi_match_is_exact() {
        let oxp3 = |f: &str| {
            Some(
                match f {
                    "sys_vendor" | "board_vendor" => "ONE-NETBOOK\n",
                    _ => "ONEXPLAYER 3\n",
                }
                .to_string(),
            )
        };
        assert_eq!(match_dmi(oxp3).map(|d| d.id), Some("onexplayer-3"));
        let near = |f: &str| {
            Some(
                match f {
                    "sys_vendor" | "board_vendor" => "ONE-NETBOOK",
                    "board_name" => "ONEXPLAYER 3 Pro",
                    _ => "ONEXPLAYER 3",
                }
                .to_string(),
            )
        };
        assert!(match_dmi(near).is_none());
        let lower = |f: &str| {
            Some(
                match f {
                    "sys_vendor" | "board_vendor" => "one-netbook",
                    _ => "ONEXPLAYER 3",
                }
                .to_string(),
            )
        };
        assert!(match_dmi(lower).is_none());
        assert!(match_dmi(|_| None).is_none());
    }
}
