// The running game, or the last one seen: the same XeSS options as the quick panel, with the full advice and the
// last run's details.
import { PanelSection, PanelSectionRow } from "@decky/ui";
import { FC } from "react";

import { GameControls } from "../components/GameControls";
import { Note, Status } from "../components/Hint";
import { lastGame, useRunningApp } from "../steam";

export const GamesTab: FC = () => {
  const app = useRunningApp();
  const game = app ?? lastGame();
  if (!game) {
    return (
      <PanelSection title="Games">
        <PanelSectionRow>
          <Note>Start a game: this page then shows its XeSS options (written into its Steam launch options), what xess-xmx-linux saw in its
            last run, and advice for its XeSS version. The last game stays here after it exits.</Note>
        </PanelSectionRow>
      </PanelSection>
    );
  }
  return (
    <PanelSection title="Game">
      <PanelSectionRow>
        <Status ok={null} label={game.name} detail={app ? `Running now (app ${game.appid}). Changes apply at its next start.` : `Not running; the last game seen (app ${game.appid}).`} />
      </PanelSectionRow>
      <GameControls key={game.appid} app={game} running={!!app} />
    </PanelSection>
  );
};
