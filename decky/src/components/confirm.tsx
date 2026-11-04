// Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
import { ConfirmModal, showModal } from "@decky/ui";
import { ReactNode } from "react";

export type ConfirmOpts = {
  title: string;
  text?: ReactNode;          // what happens, and when it takes effect
  lines?: string[];          // e.g. oxp3-tool's dry-run plan
  linesTitle?: string;
  ok?: string;
  destructive?: boolean;
};

/**
 * Ask before anything that changes the system. Resolves true only on OK; Cancel, B or closing the dialog any other
 * way gives false.
 */
export const confirm = (o: ConfirmOpts): Promise<boolean> => new Promise((resolve) => {
  let settled = false;
  const done = (v: boolean) => { if (!settled) { settled = true; resolve(v); } };
  const body = (
    <div style={{ whiteSpace: "normal" }}>
      {o.text}
      {!!o.lines?.length && (
        <>
          <div style={{ marginTop: 12, fontWeight: "bold" }}>{o.linesTitle ?? "Planned changes:"}</div>
          <div style={{ maxHeight: "40vh", overflowY: "auto", fontFamily: "monospace", fontSize: "0.85em" }}>
            {o.lines.map((l, i) => <div key={i} style={{ wordBreak: "break-word" }}>{l}</div>)}
          </div>
        </>
      )}
    </div>
  );
  try {
    const m = showModal(
      <ConfirmModal strTitle={o.title} strDescription={body} strOKButtonText={o.ok ?? "Continue"}
        strCancelButtonText="Cancel" bDestructiveWarning={o.destructive}
        onOK={() => done(true)} onCancel={() => done(false)} />,
    );
    // older Decky (3.2) has no ClosedPromise; there onCancel covers B and the close button
    m?.ClosedPromise?.then(() => done(false), () => done(false));
  } catch {
    done(false);
  }
});
