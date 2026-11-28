// SPDX-FileCopyrightText: 2026 Featherwolf
// SPDX-License-Identifier: MIT

//! Test builds only (feature test-sysfs): a D-Bus caller the integration tests run under other
//! uids (setpriv), so the bus daemon reports that uid to the provider. It runs a script of calls on
//! one connection and prints one JSON line per step:
//! `{"ok":true,"value":...}` or `{"ok":false,"error":"<D-Bus error name>","message":"..."}`.
//!
//! usage: oxp3-hw-testcall ADDRESS 'JSON SCRIPT'
//! steps: {"op":"name"} | {"op":"call","dest","path","iface","method","sig","args":[...]}
//!        {"op":"get","dest","path","iface","prop"} | {"op":"set","dest","path","iface","prop","sig","value"}
//!        {"op":"ping","dest","path"} | {"op":"own","name"} | {"op":"sleep","ms"} | {"op":"wait_stdin"}
//!        {"op":"signal","dest"?,"path","iface","member","sig","args":[...]} (a broadcast, or a
//!        unicast signal to "dest": how a hostile local process would forge another service's signal)
//! Variant arguments ("v", and the values of "a{sv}") are written {"t": "<signature>", "v": <json>}.

use std::io::Read;

use serde_json::{json, Value as J};
use zbus::zvariant::{Array, Dict, Signature, StructureBuilder, Value};

fn split_sig(sig: &str) -> Result<Vec<String>, String> {
    let b = sig.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        let start = i;
        let mut depth = 0i32;
        loop {
            if i >= b.len() {
                return Err(format!("bad signature {sig:?}"));
            }
            let c = b[i];
            i += 1;
            match c {
                b'a' => continue,
                b'(' | b'{' => depth += 1,
                b')' | b'}' => depth -= 1,
                _ => {}
            }
            if depth == 0 {
                break;
            }
        }
        out.push(sig[start..i].to_string());
    }
    Ok(out)
}

fn to_value(sig: &str, j: &J) -> Result<Value<'static>, String> {
    let bad = || format!("{j} does not fit {sig}");
    Ok(match sig {
        "s" => Value::from(j.as_str().ok_or_else(bad)?.to_string()),
        "u" => Value::from(u32::try_from(j.as_u64().ok_or_else(bad)?).map_err(|_| bad())?),
        "i" => Value::from(i32::try_from(j.as_i64().ok_or_else(bad)?).map_err(|_| bad())?),
        "b" => Value::from(j.as_bool().ok_or_else(bad)?),
        "v" => {
            let t = j.get("t").and_then(|t| t.as_str()).ok_or_else(bad)?;
            Value::Value(Box::new(to_value(t, j.get("v").ok_or_else(bad)?)?))
        }
        _ if sig.starts_with("a{") && sig.ends_with('}') => {
            let inner = &sig[2..sig.len() - 1];
            let parts = split_sig(inner)?;
            if parts.len() != 2 || parts[0] != "s" {
                return Err(format!("unsupported dict {sig}"));
            }
            let vs = Signature::try_from(parts[1].as_str()).map_err(|e| e.to_string())?;
            let mut d = Dict::new(&Signature::Str, &vs);
            for (k, v) in j.as_object().ok_or_else(bad)? {
                let val = to_value(&parts[1], v)?;
                d.append(Value::from(k.clone()), val).map_err(|e| e.to_string())?;
            }
            Value::Dict(d)
        }
        _ if sig.starts_with('a') => {
            let es = &sig[1..];
            let s = Signature::try_from(es).map_err(|e| e.to_string())?;
            let mut a = Array::new(&s);
            for v in j.as_array().ok_or_else(bad)? {
                a.append(to_value(es, v)?).map_err(|e| e.to_string())?;
            }
            Value::Array(a)
        }
        _ => return Err(format!("unsupported signature {sig}")),
    })
}

fn to_json(v: &Value<'_>) -> J {
    match v {
        Value::U8(x) => json!(x),
        Value::Bool(x) => json!(x),
        Value::I16(x) => json!(x),
        Value::U16(x) => json!(x),
        Value::I32(x) => json!(x),
        Value::U32(x) => json!(x),
        Value::I64(x) => json!(x),
        Value::U64(x) => json!(x),
        Value::F64(x) => json!(x),
        Value::Str(s) => json!(s.as_str()),
        Value::Signature(s) => json!(s.to_string()),
        Value::ObjectPath(p) => json!(p.as_str()),
        Value::Value(b) => to_json(b),
        Value::Array(a) => J::Array(a.inner().iter().map(to_json).collect()),
        Value::Dict(d) => {
            let mut m = serde_json::Map::new();
            for (k, v) in d.iter() {
                let key = match k {
                    Value::Str(s) => s.to_string(),
                    other => to_json(other).to_string(),
                };
                m.insert(key, to_json(v));
            }
            J::Object(m)
        }
        Value::Structure(s) => J::Array(s.fields().iter().map(to_json).collect()),
        #[allow(unreachable_patterns)]
        _ => json!(format!("{v:?}")),
    }
}

fn err_json(e: zbus::Error) -> J {
    match e {
        zbus::Error::MethodError(name, desc, _) => {
            json!({"ok": false, "error": name.as_str(), "message": desc.unwrap_or_default()})
        }
        zbus::Error::FDO(f) => {
            let name = zbus::DBusError::name(f.as_ref()).to_string();
            let desc = zbus::DBusError::description(f.as_ref()).unwrap_or("").to_string();
            json!({"ok": false, "error": name, "message": desc})
        }
        other => json!({"ok": false, "error": "transport", "message": other.to_string()}),
    }
}

fn s<'a>(step: &'a J, k: &str) -> Result<&'a str, String> {
    step.get(k)
        .and_then(|v| v.as_str())
        .ok_or_else(|| format!("step needs {k}"))
}

async fn run(addr: &str, script: &J) -> Result<(), String> {
    let conn = zbus::connection::Builder::address(addr)
        .map_err(|e| e.to_string())?
        .build()
        .await
        .map_err(|e| e.to_string())?;
    for step in script.as_array().ok_or("the script is a JSON array")? {
        let out = match s(step, "op")? {
            "name" => json!({"ok": true, "value": conn.unique_name().map(|n| n.to_string())}),
            "sleep" => {
                tokio::time::sleep(std::time::Duration::from_millis(
                    step.get("ms").and_then(|m| m.as_u64()).unwrap_or(0),
                ))
                .await;
                json!({"ok": true})
            }
            "wait_stdin" => {
                let _ = tokio::task::spawn_blocking(|| {
                    let mut buf = Vec::new();
                    let _ = std::io::stdin().read_to_end(&mut buf);
                })
                .await;
                json!({"ok": true})
            }
            "own" => match conn
                .request_name_with_flags(s(step, "name")?, zbus::fdo::RequestNameFlags::DoNotQueue.into())
                .await
            {
                Ok(r) => json!({"ok": true, "value": format!("{r:?}")}),
                Err(e) => err_json(e),
            },
            "signal" => {
                let sig = step.get("sig").and_then(|v| v.as_str()).unwrap_or("");
                let args = step.get("args").cloned().unwrap_or(J::Array(vec![]));
                let parts = split_sig(sig)?;
                let list = args.as_array().ok_or("args is an array")?;
                if parts.len() != list.len() {
                    return Err(format!("{} args for signature {sig:?}", list.len()));
                }
                let dest = step.get("dest").and_then(|v| v.as_str());
                let (path, iface, member) = (s(step, "path")?, s(step, "iface")?, s(step, "member")?);
                let r = if parts.is_empty() {
                    conn.emit_signal(dest, path, iface, member, &()).await
                } else {
                    let mut b = StructureBuilder::new();
                    for (p, a) in parts.iter().zip(list) {
                        b = b.append_field(to_value(p, a)?);
                    }
                    let body = b.build().map_err(|e| e.to_string())?;
                    conn.emit_signal(dest, path, iface, member, &body).await
                };
                match r {
                    Ok(()) => json!({"ok": true}),
                    Err(e) => err_json(e),
                }
            }
            "ping" => {
                let r = conn
                    .call_method(
                        Some(s(step, "dest")?),
                        s(step, "path")?,
                        Some("org.freedesktop.DBus.Peer"),
                        "Ping",
                        &(),
                    )
                    .await;
                match r {
                    Ok(_) => json!({"ok": true}),
                    Err(e) => err_json(e),
                }
            }
            "get" => {
                let r = conn
                    .call_method(
                        Some(s(step, "dest")?),
                        s(step, "path")?,
                        Some("org.freedesktop.DBus.Properties"),
                        "Get",
                        &(s(step, "iface")?, s(step, "prop")?),
                    )
                    .await;
                match r {
                    Ok(m) => match m.body().deserialize::<Value<'_>>() {
                        Ok(v) => json!({"ok": true, "value": to_json(&v)}),
                        Err(e) => json!({"ok": false, "error": "decode", "message": e.to_string()}),
                    },
                    Err(e) => err_json(e),
                }
            }
            "set" => {
                // the Set argument is itself a variant: pass the inner value, which serializes as one
                let v = match to_value(s(step, "sig")?, step.get("value").ok_or("set needs value")?)? {
                    Value::Value(inner) => *inner,
                    other => other,
                };
                let r = conn
                    .call_method(
                        Some(s(step, "dest")?),
                        s(step, "path")?,
                        Some("org.freedesktop.DBus.Properties"),
                        "Set",
                        &(s(step, "iface")?, s(step, "prop")?, v),
                    )
                    .await;
                match r {
                    Ok(_) => json!({"ok": true}),
                    Err(e) => err_json(e),
                }
            }
            "call" => {
                let sig = step.get("sig").and_then(|v| v.as_str()).unwrap_or("");
                let args = step.get("args").cloned().unwrap_or(J::Array(vec![]));
                let parts = split_sig(sig)?;
                let list = args.as_array().ok_or("args is an array")?;
                if parts.len() != list.len() {
                    return Err(format!("{} args for signature {sig:?}", list.len()));
                }
                let dest = s(step, "dest")?;
                let path = s(step, "path")?;
                let iface = s(step, "iface")?;
                let method = s(step, "method")?;
                let r = if parts.is_empty() {
                    conn.call_method(Some(dest), path, Some(iface), method, &()).await
                } else {
                    let mut b = StructureBuilder::new();
                    for (p, a) in parts.iter().zip(list) {
                        b = b.append_field(to_value(p, a)?);
                    }
                    let body = b.build().map_err(|e| e.to_string())?;
                    conn.call_method(Some(dest), path, Some(iface), method, &body).await
                };
                match r {
                    Ok(m) => {
                        let body = m.body();
                        let value = match body.signature().to_string().as_str() {
                            "" => J::Null,
                            _ => match body.deserialize::<zbus::zvariant::Structure<'_>>() {
                                Ok(st) => {
                                    let f: Vec<J> = st.fields().iter().map(to_json).collect();
                                    if f.len() == 1 {
                                        f.into_iter().next().unwrap_or(J::Null)
                                    } else {
                                        J::Array(f)
                                    }
                                }
                                Err(_) => match body.deserialize::<Value<'_>>() {
                                    Ok(v) => to_json(&v),
                                    Err(e) => json!(format!("undecodable: {e}")),
                                },
                            },
                        };
                        json!({"ok": true, "value": value})
                    }
                    Err(e) => err_json(e),
                }
            }
            other => return Err(format!("unknown op {other:?}")),
        };
        // the test may close the pipe once it has what it needs: never panic on EPIPE
        use std::io::Write;
        let mut so = std::io::stdout().lock();
        let _ = writeln!(so, "{out}").and_then(|_| so.flush());
    }
    Ok(())
}

fn main() -> std::process::ExitCode {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 {
        eprintln!("usage: oxp3-hw-testcall ADDRESS 'JSON SCRIPT'");
        return std::process::ExitCode::from(2);
    }
    let script: J = match serde_json::from_str(&args[2]) {
        Ok(j) => j,
        Err(e) => {
            eprintln!("bad script: {e}");
            return std::process::ExitCode::from(2);
        }
    };
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime");
    match rt.block_on(run(&args[1], &script)) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("oxp3-hw-testcall: {e}");
            std::process::ExitCode::FAILURE
        }
    }
}
