// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! The single reader and writer of kernel attribute files.
//!
//! Every write opens the file with `O_WRONLY | O_NOFOLLOW | O_CLOEXEC` and makes exactly one
//! `write(2)` of the ASCII text plus a newline; the caller then reads the file back. Nothing here
//! creates, renames, chmods or deletes a file, and nothing follows a symlink at the last path
//! component. Paths are built by the caller from names found at discovery.
//!
//! Test builds (feature `test-sysfs`) can prefix every absolute path with a fake root and route
//! writes through `emu`, which emulates the kernel's store semantics on that tree.

use std::ffi::CString;
use std::io::{self, Read};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

/// Attribute files are small; anything longer is not one of ours.
const MAX_READ: u64 = 64 * 1024;

#[derive(Debug, Clone)]
pub struct Sys {
    root: PathBuf,
    #[cfg(feature = "test-sysfs")]
    emu: bool,
}

impl Sys {
    /// The real filesystem.
    pub fn real() -> Sys {
        Sys {
            root: PathBuf::from("/"),
            #[cfg(feature = "test-sysfs")]
            emu: false,
        }
    }

    /// A fake tree whose writes follow the kernel's store semantics (test builds only).
    #[cfg(feature = "test-sysfs")]
    pub fn emulated(root: PathBuf) -> Sys {
        Sys { root, emu: true }
    }

    /// A fake tree written as plain files, without the kernel emulation (test builds only: for a
    /// conformance suite that answers every write with its own kernel model).
    #[cfg(feature = "test-sysfs")]
    pub fn rooted(root: PathBuf) -> Sys {
        Sys { root, emu: false }
    }

    /// The path of absolute path `abs` (which must start with '/') in this filesystem.
    pub fn p(&self, abs: &str) -> PathBuf {
        debug_assert!(abs.starts_with('/'));
        if self.root == Path::new("/") {
            PathBuf::from(abs)
        } else {
            self.root.join(abs.trim_start_matches('/'))
        }
    }

    /// The file's text, trimmed; `None` on any error.
    pub fn read(&self, path: &Path) -> Option<String> {
        self.read_err(path).ok()
    }

    pub fn read_err(&self, path: &Path) -> io::Result<String> {
        #[cfg(feature = "test-sysfs")]
        if self.emu {
            crate::emu::log_read(&self.root, path);
        }
        let f = std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)?;
        let mut buf = Vec::new();
        f.take(MAX_READ).read_to_end(&mut buf)?;
        Ok(String::from_utf8_lossy(&buf).trim().to_string())
    }

    /// The first whitespace-separated word as an unsigned number.
    pub fn read_u64(&self, path: &Path) -> Option<u64> {
        self.read(path)?.split_whitespace().next()?.parse().ok()
    }

    pub fn read_i64(&self, path: &Path) -> Option<i64> {
        self.read(path)?.split_whitespace().next()?.parse().ok()
    }

    /// One write of `text` + "\n", never through a symlink.
    pub fn write(&self, path: &Path, text: &str) -> io::Result<()> {
        if !text.is_ascii() || text.contains('\n') || text.is_empty() || text.len() > 64 {
            return Err(io::Error::from_raw_os_error(libc::EINVAL));
        }
        #[cfg(feature = "test-sysfs")]
        if self.emu {
            return crate::emu::write(&self.root, path, text);
        }
        write_raw(path, text)
    }

    /// Whether this process may open `path` for writing (permission bits and ACLs as the kernel
    /// checks them for the real uid and groups).
    pub fn can_write(&self, path: &Path) -> bool {
        access(path, libc::W_OK)
    }

    pub fn can_read(&self, path: &Path) -> bool {
        access(path, libc::R_OK)
    }

    pub fn is_file(&self, path: &Path) -> bool {
        std::fs::symlink_metadata(path).map(|m| m.is_file()).unwrap_or(false)
    }

    pub fn is_dir(&self, path: &Path) -> bool {
        std::fs::metadata(path).map(|m| m.is_dir()).unwrap_or(false)
    }

    /// Directory entry names, sorted; empty on any error.
    pub fn entries(&self, dir: &Path) -> Vec<String> {
        let mut out: Vec<String> = match std::fs::read_dir(dir) {
            Ok(it) => it
                .filter_map(|e| e.ok())
                .filter_map(|e| e.file_name().into_string().ok())
                .collect(),
            Err(_) => Vec::new(),
        };
        out.sort();
        out
    }

    /// The last component of a symlink's target (a driver link's name, for example).
    pub fn link_name(&self, path: &Path) -> Option<String> {
        let t = std::fs::read_link(path).ok()?;
        Some(t.file_name()?.to_string_lossy().into_owned())
    }

    /// The root this filesystem is rooted at ("/" in production).
    pub fn root(&self) -> &Path {
        &self.root
    }
}

pub fn write_raw(path: &Path, text: &str) -> io::Result<()> {
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)?;
    let line = format!("{text}\n");
    let n = io::Write::write(&mut f, line.as_bytes())?;
    if n != line.len() {
        return Err(io::Error::from_raw_os_error(libc::EIO));
    }
    Ok(())
}

fn access(path: &Path, mode: libc::c_int) -> bool {
    let Ok(c) = CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    // SAFETY: c is a valid NUL-terminated string for the duration of the call.
    unsafe { libc::access(c.as_ptr(), mode) == 0 }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn tmp() -> PathBuf {
        let d = std::env::temp_dir().join(format!("oxp3-hw-sysfs-{}-{}", std::process::id(), rand_suffix()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn rand_suffix() -> u64 {
        use std::time::{SystemTime, UNIX_EPOCH};
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos() as u64
    }

    #[test]
    fn no_symlink_is_followed() {
        let d = tmp();
        let victim = d.join("victim");
        std::fs::write(&victim, "keep\n").unwrap();
        let link = d.join("constraint_0_power_limit_uw");
        std::os::unix::fs::symlink(&victim, &link).unwrap();
        let s = Sys::real();
        let e = s.write(&link, "15000000").unwrap_err();
        assert_eq!(e.raw_os_error(), Some(libc::ELOOP));
        assert_eq!(std::fs::read_to_string(&victim).unwrap(), "keep\n");
        assert!(s.read(&link).is_none(), "reads do not follow a final symlink either");
        let plain = d.join("plain");
        std::fs::write(&plain, "0\n").unwrap();
        s.write(&plain, "15000000").unwrap();
        assert_eq!(s.read(&plain).as_deref(), Some("15000000"));
        std::fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn one_ascii_line_only() {
        let d = tmp();
        let f = d.join("f");
        std::fs::write(&f, "x\n").unwrap();
        let s = Sys::real();
        for bad in ["", "a\nb", "\u{e9}", &"9".repeat(65)] {
            assert_eq!(
                s.write(&f, bad).unwrap_err().raw_os_error(),
                Some(libc::EINVAL),
                "{bad:?}"
            );
        }
        assert_eq!(std::fs::read_to_string(&f).unwrap(), "x\n");
        std::fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn missing_files_and_permissions() {
        let d = tmp();
        let s = Sys::real();
        assert!(s.read(&d.join("nope")).is_none());
        assert!(s.write(&d.join("nope"), "1").is_err(), "never creates a file");
        assert!(!d.join("nope").exists());
        let ro = d.join("ro");
        std::fs::write(&ro, "1\n").unwrap();
        std::fs::set_permissions(&ro, std::fs::Permissions::from_mode(0o444)).unwrap();
        // root bypasses permission bits, so this half only holds for a non-root test run
        if unsafe { libc::geteuid() } != 0 {
            assert!(!s.can_write(&ro));
            assert_eq!(s.write(&ro, "2").unwrap_err().raw_os_error(), Some(libc::EACCES));
        }
        assert!(s.can_read(&ro));
        std::fs::remove_dir_all(&d).unwrap();
    }
}
