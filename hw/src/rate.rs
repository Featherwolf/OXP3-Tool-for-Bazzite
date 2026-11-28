// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! Rate limits (schemas/hw1.xml "Rate"): at most 20 calls per second per sender, and at most 2
//! committed writes per second per knob. A burst is coalesced: while a knob's next write slot is
//! not due, only the newest waiting call of each member is written, and every older waiting call
//! of that member gets the reply of that write.

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

use crate::error::HwError;

pub const CALLS_PER_SECOND: usize = 20;
pub const COMMIT_INTERVAL: Duration = Duration::from_millis(500);

#[derive(Default)]
pub struct SenderRate {
    calls: HashMap<String, VecDeque<Instant>>,
}

impl SenderRate {
    pub fn check(&mut self, sender: &str, now: Instant) -> Result<(), HwError> {
        if self.calls.len() > 4096 {
            self.calls.retain(|_, q| {
                q.back()
                    .map(|t| now.duration_since(*t) < Duration::from_secs(1))
                    .unwrap_or(false)
            });
        }
        let q = self.calls.entry(sender.to_string()).or_default();
        while q
            .front()
            .map(|t| now.duration_since(*t) >= Duration::from_secs(1))
            .unwrap_or(false)
        {
            q.pop_front();
        }
        if q.len() >= CALLS_PER_SECOND {
            return Err(HwError::LimitsExceeded(format!(
                "more than {CALLS_PER_SECOND} calls per second"
            )));
        }
        q.push_back(now);
        Ok(())
    }

    pub fn forget(&mut self, sender: &str) {
        self.calls.remove(sender);
    }
}

#[derive(Default)]
struct Gate {
    last: Option<Instant>,
    gen: HashMap<&'static str, u64>,
    done: HashMap<&'static str, (u64, Result<u32, HwError>)>,
}

/// Per-knob write slots.
#[derive(Default)]
pub struct Gates {
    knobs: HashMap<&'static str, Gate>,
}

pub enum Turn {
    /// Write now (the slot is claimed).
    Go,
    /// Wait this long, then ask again.
    Wait(Duration),
    /// A newer call of the same member took over: its result will answer this one.
    Superseded,
}

impl Gates {
    /// Register a call; returns its generation.
    pub fn enter(&mut self, knob: &'static str, member: &'static str) -> u64 {
        let g = self.knobs.entry(knob).or_default();
        let n = g.gen.entry(member).or_insert(0);
        *n += 1;
        *n
    }

    pub fn turn(&mut self, knob: &'static str, member: &'static str, my: u64, now: Instant) -> Turn {
        let g = self.knobs.entry(knob).or_default();
        if g.gen.get(member).copied().unwrap_or(0) != my {
            return Turn::Superseded;
        }
        if let Some(last) = g.last {
            let due = last + COMMIT_INTERVAL;
            if now < due {
                return Turn::Wait(due - now);
            }
        }
        g.last = Some(now);
        Turn::Go
    }

    pub fn publish(&mut self, knob: &'static str, member: &'static str, my: u64, r: Result<u32, HwError>) {
        let g = self.knobs.entry(knob).or_default();
        g.done.insert(member, (my, r));
    }

    /// The result a superseded call `my` gets, once a newer one has been written.
    pub fn result(&self, knob: &'static str, member: &'static str, my: u64) -> Option<Result<u32, HwError>> {
        let (gen, r) = self.knobs.get(knob)?.done.get(member)?;
        (*gen >= my).then(|| r.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn twenty_calls_per_second_per_sender() {
        let mut r = SenderRate::default();
        let t0 = Instant::now();
        for _ in 0..20 {
            r.check(":1.5", t0).unwrap();
        }
        assert_eq!(
            r.check(":1.5", t0).unwrap_err().name_str(),
            "org.freedesktop.DBus.Error.LimitsExceeded"
        );
        r.check(":1.6", t0).unwrap();
        r.check(":1.5", t0 + Duration::from_millis(1001)).unwrap();
    }

    #[test]
    fn slots_and_supersession() {
        let mut g = Gates::default();
        let t0 = Instant::now();
        let a = g.enter("tdp", "TdpLimit");
        assert!(matches!(g.turn("tdp", "TdpLimit", a, t0), Turn::Go));
        let b = g.enter("tdp", "TdpLimit");
        let c = g.enter("tdp", "TdpLimit");
        assert!(matches!(g.turn("tdp", "TdpLimit", b, t0), Turn::Superseded));
        match g.turn("tdp", "TdpLimit", c, t0 + Duration::from_millis(100)) {
            Turn::Wait(d) => assert_eq!(d, Duration::from_millis(400)),
            _ => panic!("expected a wait"),
        }
        assert!(matches!(
            g.turn("tdp", "TdpLimit", c, t0 + Duration::from_millis(500)),
            Turn::Go
        ));
        g.publish("tdp", "TdpLimit", c, Ok(20));
        assert_eq!(g.result("tdp", "TdpLimit", b), Some(Ok(20)));
        // another member of the same knob shares the slot but is never superseded by it
        let m = g.enter("tdp", "Other");
        assert!(matches!(
            g.turn("tdp", "Other", m, t0 + Duration::from_millis(600)),
            Turn::Wait(_)
        ));
    }
}
