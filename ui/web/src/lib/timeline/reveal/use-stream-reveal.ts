"use client";

// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { RevealController } from "./controller";

export interface StreamReveal {
  text: string;
  revealing: boolean;
  caughtUp: boolean;
}

export function useStreamReveal(text: string, live: boolean, renderIntervalMs: number): StreamReveal {
  const [ctrl] = useState(() => new RevealController(text, live));
  const [shown, setShown] = useState(text);
  const [caughtUp, setCaughtUp] = useState(true);
  const intervalRef = useRef(renderIntervalMs);
  const frameRef = useRef<number | null>(null);
  const committedEndRef = useRef(text.length);
  const lastCommitRef = useRef(0);

  useEffect(() => {
    intervalRef.current = renderIntervalMs;
  }, [renderIntervalMs]);

  useLayoutEffect(() => {
    const commit = (now: number) => {
      const end = ctrl.shownEnd();
      if (end === committedEndRef.current) return;
      committedEndRef.current = end;
      lastCommitRef.current = now;
      setShown(ctrl.shown());
    };
    const tick = (ts: number) => {
      frameRef.current = null;
      ctrl.advance(ts);
      if (ctrl.done || ts - lastCommitRef.current >= intervalRef.current) commit(ts);
      setCaughtUp(ctrl.done);
      if (!ctrl.done || ctrl.shownEnd() !== committedEndRef.current) {
        frameRef.current = requestAnimationFrame(tick);
      }
    };
    const now = performance.now();
    ctrl.update(text, live, now);
    setCaughtUp(ctrl.done);
    if (document.hidden) {
      ctrl.snap();
      commit(now);
    } else if (!ctrl.done && frameRef.current === null) {
      ctrl.resume(now);
      frameRef.current = requestAnimationFrame(tick);
    } else if (ctrl.done) {
      commit(now);
    }
  }, [ctrl, text, live]);

  useEffect(() => {
    const onVisible = () => {
      if (document.hidden) return;
      ctrl.snap();
      committedEndRef.current = ctrl.shownEnd();
      setShown(ctrl.shown());
      setCaughtUp(true);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    };
  }, [ctrl]);

  const settled = !live && shown === text;
  return settled ? { text, revealing: false, caughtUp } : { text: shown, revealing: true, caughtUp };
}
