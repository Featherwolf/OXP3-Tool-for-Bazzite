// The four face keys: one dropdown each (oxp3-tool list --json: button_keys, button_actions), the frame-limit value
// when a key uses fps-limit, and "Apply layout", which applies the buttons fix (plus xbox-key when this unit's
// Xbox key needs oxp3-tool's helper) after showing oxp3-tool's plan.
import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow, SliderField, ToggleField } from "@decky/ui";
import { FC, useMemo, useState } from "react";

import { applyFixes, blockedBy, useAction } from "../actions";
import { explain, Note, Problem, Status } from "../components/Hint";
import { JobView, TaskSection } from "../components/JobView";
import type { ApplyOpts, ButtonKey, Oxp3Fix } from "../types";
import { NeedsOxp3, useOxp3 } from "./Fixes";

const KEY_LABEL: Record<string, string> = { onex: "ONEX key", keyboard: "Keyboard key", home: "Home key", xbox: "Xbox key" };
const ACTION_LABEL: Record<string, string> = {
  guide: "Steam menu", "quick-access": "Quick Access", keyboard: "On-screen keyboard", "keyboard-chord": "Keyboard (Steam+X)",
  home: "Home screen", "fps-limit": "Frame limit on/off", screenshot: "Screenshot", "ogui-menu": "OpenGamepadUI menu",
};
const actionLabel = (a: string | undefined) => (a ? ACTION_LABEL[a] ?? a : "?");
const OPT: Record<ButtonKey, keyof ApplyOpts> = { onex: "onex_key", keyboard: "keyboard_key", home: "home", xbox: "xbox_key" };

/**
 * The Xbox key needs xbox-key's helper when the helper is installed already (keep it in step) or oxp3-tool found
 * that InputPlumber cannot read the key on this unit (state recommended: it logged vendor button 0x25).
 */
const xboxNeedsHelper = (f: Oxp3Fix | undefined) =>
  !!f && (f.state === "applied" || f.state === "damaged" || f.state === "incomplete" || f.state === "recommended");

export const ButtonsTab: FC = () => {
  const { ov, usable, running, status, catalog, st } = useOxp3();
  const act = useAction();
  const [pick, setPick] = useState<Partial<Record<ButtonKey, string>>>({});
  const [fps, setFps] = useState<number | null>(null);
  const [restart, setRestart] = useState(true);
  const buttons = status?.fixes.find((f) => f.id === "buttons");
  const xboxFix = status?.fixes.find((f) => f.id === "xbox-key");
  const applied = buttons?.state === "applied" || buttons?.state === "damaged";
  const saved: Record<string, string> = (applied && buttons?.options?.actions) || {};
  // `?? {}` / `?.[k]`: a catalog from an older oxp3-tool may have no button_keys (value("xbox") is read below anyway).
  const keys = (catalog ? Object.keys(catalog.button_keys ?? {}) : []) as ButtonKey[];
  const value = (k: ButtonKey) => pick[k] ?? saved[k] ?? catalog?.button_keys?.[k]?.default ?? "guide";
  const options = useMemo(() => Object.keys(catalog?.button_actions ?? {}).map((a) => ({ data: a, label: actionLabel(a) })), [catalog]);
  const usesFps = keys.some((k) => value(k) === "fps-limit");
  const savedFps = Number(buttons?.options?.fps_limit ?? catalog?.fps_limit.default ?? 72);
  const fpsValue = fps ?? savedFps;
  const helper = xboxNeedsHelper(xboxFix);
  const loaded = `${catalog ? "c" : ""}${status ? "s" : ""}-${JSON.stringify(saved)}`;

  if (!usable) return <NeedsOxp3 ov={ov.data} />;
  if (!status || !catalog || !buttons) {
    return (
      <PanelSection title="Buttons">
        <PanelSectionRow>
          <Status ok={null} label={running ? "The layout is read when the running task has finished." : "Reading oxp3-tool's status…"} />
          <Problem what="oxp3-tool" error={(st.data && !st.data.ok ? st.data.error : null)} />
        </PanelSectionRow>
      </PanelSection>
    );
  }
  const changeable = status.can_change && buttons.state !== "n/a" && buttons.state !== "unknown";
  const apply = () => act.run("apply", async () => {
    const b = blockedBy();
    if (b) return b;
    const opts: ApplyOpts = {};
    for (const k of keys) (opts as Record<string, unknown>)[OPT[k]] = value(k);
    if (usesFps) opts.fps_limit = fpsValue;
    if (!restart) opts.no_restart = true;
    return applyFixes(helper && xboxFix ? [buttons, xboxFix] : [buttons], opts, "Apply the button layout");
  });
  return (
    <>
      <TaskSection onDone={() => { setPick({}); setFps(null); }} />
      <PanelSection title="Buttons">
        <PanelSectionRow>
          <Status ok={applied ? true : null} label={applied ? "oxp3-tool's layout is applied" : "Stock layout (the buttons fix is not applied)"}
            detail={buttons.pending === "inputplumber-restart" ? "The last layout loads at the next reboot (InputPlumber was not restarted)." : buttons.summary} />
          {!!buttons.notes.length && <Note>{buttons.notes.map((n, i) => <div key={i}>{n}</div>)}</Note>}
          {!changeable && <Status ok={false} label="The layout cannot be changed here" detail={buttons.notes[0] ?? "oxp3-tool does not support this system."} />}
        </PanelSectionRow>
        <PanelSectionRow>
          {keys.map((k) => {
            const meta = catalog.button_keys[k];
            const v = value(k);
            const now = applied ? actionLabel(saved[k] ?? meta.default) : `${actionLabel(meta.stock)} (stock)`;
            return (
              <DropdownItem key={`${k}-${loaded}`} label={KEY_LABEL[k] ?? meta.label} menuLabel={KEY_LABEL[k] ?? meta.label}
                layout="below" childrenContainerWidth="max" bottomSeparator="none" disabled={!changeable || running}
                rgOptions={options} selectedOption={v} onChange={(o) => setPick((p) => ({ ...p, [k]: o.data }))}
                description={explain(`${meta.label}: ${catalog.button_actions[v] ?? v}.`,
                  `Now: ${now} · oxp3-tool's default: ${actionLabel(meta.default)}. Changes when you tap Apply layout.`)} />
            );
          })}
          {usesFps && (
            <SliderField label="Frame limit" value={fpsValue} min={catalog.fps_limit.min} max={catalog.fps_limit.max} step={1}
              showValue valueSuffix=" fps" disabled={!changeable || running} onChange={setFps}
              description={explain(`What the Frame limit key switches Steam's frame limit to (default ${catalog.fps_limit.default}); a second press turns it off.`,
                "Changes when you tap Apply layout.")} />
          )}
          {xboxNote(helper, value("xbox"))}
          <ToggleField label="Restart the controller now" checked={restart} disabled={!changeable || running} onChange={setRestart}
            description={explain("On: InputPlumber restarts and the layout works at once (the controller stops for a few seconds). Off: the layout loads at the next reboot.",
              "If the buttons act like an Xbox controller afterwards, reboot.")} />
          <ButtonItem layout="below" disabled={!changeable || running || !!act.busy} onClick={apply}
            description={explain(`Applies the buttons fix${helper ? " and the Xbox key helper (xbox-key)" : ""} with the layout above. You see oxp3-tool's plan first.`,
              `Takes effect ${buttons.takes_effect}.`)}>
            {act.busy ? "Reading the plan…" : "Apply layout"}
          </ButtonItem>
          <Problem what="Apply layout" error={act.error} />
        </PanelSectionRow>
        {act.job && <JobView job={act.job} lines={6} onDone={() => { setPick({}); setFps(null); }} />}
      </PanelSection>
    </>
  );
};

/** Why the Xbox key goes through a helper on this unit (and what that cannot do). */
const xboxNote = (helper: boolean, xbox: string) => helper ? (
  <Note label="Xbox key">
    On this unit InputPlumber cannot read the Xbox key, so oxp3-tool's xbox-key helper does what you choose for it; Apply layout
    applies xbox-key too. The helper can only tap: holding the key for Steam shortcuts does not work{xbox === "guide" ? " (keep Steam menu on Home for those)" : ""}.
  </Note>
) : null;
