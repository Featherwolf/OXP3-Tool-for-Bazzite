// Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
// One typed callable per backend method (main.py, DESIGN.md "Backend API"), one per line, so a test can check that
// the frontend and main.py name the same methods. The pages call them through backend.ts, which turns a rejected
// call into {ok: false, error}.
import { callable } from "@decky/api";

import type {
  ApplyOpts, CatalogRes, DriversRes, GameInfoRes, JobListRes, JobRes, JobStart, KernelRes, Overview, Oxp3UpdateCheck,
  PlanRes, Res, ShimRes, StatusRes, XessStateRes, XessUpdateCheck,
} from "./types";

// ---- overview ----------------------------------------------------------------------------------------------------
export const get_overview = callable<[], Overview>("get_overview");

// ---- oxp3-tool ---------------------------------------------------------------------------------------------------
export const oxp3_status = callable<[], StatusRes>("oxp3_status");
export const oxp3_catalog = callable<[], CatalogRes>("oxp3_catalog");
export const oxp3_plan = callable<[fixes: string[], opts: ApplyOpts], PlanRes>("oxp3_plan");
export const oxp3_apply = callable<[fixes: string[], opts: ApplyOpts], JobStart>("oxp3_apply");
export const oxp3_revert = callable<[fixes: string[]], JobStart>("oxp3_revert");
export const oxp3_kernel = callable<[], KernelRes>("oxp3_kernel");
export const oxp3_kernel_try = callable<[pkg: string], JobStart>("oxp3_kernel_try");
export const oxp3_kernel_keep = callable<[], JobStart>("oxp3_kernel_keep");
export const oxp3_diagnose = callable<[], JobStart>("oxp3_diagnose");
export const oxp3_update_check = callable<[], Oxp3UpdateCheck>("oxp3_update_check");
export const oxp3_install = callable<[source: "bundled" | "online"], JobStart>("oxp3_install");

// ---- xess-xmx-linux ----------------------------------------------------------------------------------------------
export const xess_state = callable<[], XessStateRes>("xess_state");
export const xess_check_update = callable<[], XessUpdateCheck>("xess_check_update");
export const xess_install = callable<[], JobStart>("xess_install");
export const xess_uninstall = callable<[], JobStart>("xess_uninstall");
export const xess_set_driver = callable<[prefix: string], DriversRes>("xess_set_driver");
export const xess_shim_system_protons = callable<[], ShimRes>("xess_shim_system_protons");
export const xess_remove_folder = callable<[path: string], Res>("xess_remove_folder");

// ---- per game ----------------------------------------------------------------------------------------------------
export const game_info = callable<[appid: string], GameInfoRes>("game_info");

// ---- jobs and misc -----------------------------------------------------------------------------------------------
export const job_status = callable<[id: string | null], JobRes>("job_status");
export const job_list = callable<[], JobListRes>("job_list");
export const reboot = callable<[], Res>("reboot");
export const get_setting = callable<[key: string], any>("get_setting");
export const set_setting = callable<[key: string, value: any], Res>("set_setting");
