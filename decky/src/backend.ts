// The backend calls of api.ts, made never to reject: Decky rejects a call when the method raised or the backend is
// gone, and drops the Python message on the way (DESIGN.md "Rules"). Every page imports these, not api.ts.
import * as raw from "./api";

const reason = (e: unknown): string => {
  const m = e instanceof Error ? e.message : typeof e === "string" ? e : "";
  return m || "the backend did not answer (see the plugin log)";
};

type Raw = typeof raw;
type ResName = Exclude<keyof Raw, "get_setting">;

/** raw[name] with a rejection (or a non-object answer) turned into {ok: false, error: "name: reason"}. */
const guard = <K extends ResName>(name: K): Raw[K] => {
  const fn = raw[name] as unknown as (...args: unknown[]) => Promise<unknown>;
  return (async (...args: unknown[]) => {
    try {
      const r = await fn(...args);
      return r && typeof r === "object" ? r : { ok: false, error: `${name}: no answer` };
    } catch (e) {
      return { ok: false, error: `${name}: ${reason(e)}` };
    }
  }) as unknown as Raw[K];
};

export const get_overview = guard("get_overview");
export const oxp3_status = guard("oxp3_status");
export const oxp3_catalog = guard("oxp3_catalog");
export const oxp3_plan = guard("oxp3_plan");
export const oxp3_apply = guard("oxp3_apply");
export const oxp3_revert = guard("oxp3_revert");
export const oxp3_kernel = guard("oxp3_kernel");
export const oxp3_kernel_try = guard("oxp3_kernel_try");
export const oxp3_kernel_keep = guard("oxp3_kernel_keep");
export const oxp3_diagnose = guard("oxp3_diagnose");
export const oxp3_update_check = guard("oxp3_update_check");
export const oxp3_install = guard("oxp3_install");
export const xess_state = guard("xess_state");
export const xess_check_update = guard("xess_check_update");
export const xess_install = guard("xess_install");
export const xess_uninstall = guard("xess_uninstall");
export const xess_set_driver = guard("xess_set_driver");
export const xess_shim_system_protons = guard("xess_shim_system_protons");
export const xess_remove_folder = guard("xess_remove_folder");
export const game_info = guard("game_info");
export const job_status = guard("job_status");
export const job_list = guard("job_list");
export const reboot = guard("reboot");
export const set_setting = guard("set_setting");

/** The stored value, or null when the call fails. */
export const get_setting = async (key: string): Promise<any> => {
  try { return await raw.get_setting(key); } catch { return null; }
};
