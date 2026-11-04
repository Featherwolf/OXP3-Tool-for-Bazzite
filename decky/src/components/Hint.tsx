// Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
import { Field } from "@decky/ui";
import { FC, ReactNode } from "react";

/**
 * The explanation every control carries: what it does, and when it takes effect. It goes in the control's own
 * description, so moving the D-pad onto a control scrolls its explanation into view with it.
 */
export const explain = (does: ReactNode, when?: ReactNode, note?: ReactNode): ReactNode => (
  <span style={{ whiteSpace: "normal" }}>
    {does}
    {when && <><br />{when}</>}
    {note && <><br />{note}</>}
  </span>
);

const MARK = { ok: "✓", bad: "✗", info: "" };
export const RED = "#f44336";
const COLOR = { ok: "#4caf50", bad: RED, info: "#9aa4b1" };

/** A read-only status line the controller can land on: a ✓ / ✗ mark, a label and an optional detail. */
export const Status: FC<{ ok: boolean | null; label: ReactNode; detail?: ReactNode; red?: boolean }> =
  ({ ok, label, detail, red }) => {
    const k = ok === null ? "info" : ok ? "ok" : "bad";
    return (
      <Field focusable bottomSeparator="none" description={detail ? <span style={{ whiteSpace: "normal" }}>{detail}</span> : undefined}
        label={<span style={red ? { color: RED, fontWeight: "bold" } : undefined}>
          {MARK[k] && <span style={{ color: COLOR[k], marginRight: 8 }}>{MARK[k]}</span>}{label}</span>} />
    );
  };

/** The last action that did not work, in red under the control it belongs to, with the reason. */
export const Problem: FC<{ error: string | null | undefined; what?: string }> = ({ error, what }) =>
  error ? <Status ok={false} red label={what ? `${what}: did not work` : "Did not work"} detail={error} /> : null;

/** Plain information, focusable so the D-pad scrolls through it. */
export const Note: FC<{ children: ReactNode; label?: ReactNode }> = ({ children, label }) => (
  <Field focusable bottomSeparator="none" label={label}
    description={<span style={{ whiteSpace: "normal", wordBreak: "break-word" }}>{children}</span>} />
);

/** Text the user may have to type somewhere (launch options, paths): monospace, wraps anywhere. */
export const Code: FC<{ children: ReactNode }> = ({ children }) => (
  <span style={{ fontFamily: "monospace", wordBreak: "break-all", whiteSpace: "pre-wrap" }}>{children}</span>
);
