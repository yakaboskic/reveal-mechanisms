"use client";

import React, { useEffect, useRef, useState, type ReactNode } from "react";
import { AssessmentAutoCheck, AssessmentController, assessmentBinding, assessmentReady, assessmentResult, idleAssessment, visibleAssessment, type AssessmentComposer, type AssessmentDraft, type CfdeAssessment } from "../lib/cfde-assessment";
import { CfdeAssessmentFeedback } from "./CfdeAssessmentView";

/** The launch control stays usable throughout automatic support checks. */
export function CfdeAssessment({ draft, composer, disabled, children }: {
  draft: AssessmentDraft | null; composer: AssessmentComposer; disabled: boolean;
  children: (assessment: CfdeAssessment | null, assessing: boolean) => ReactNode;
}) {
  const [state, setState] = useState(idleAssessment);
  const controller = useRef<AssessmentController | null>(null);
  if (!controller.current) controller.current = new AssessmentController(setState);
  const binding = assessmentBinding(draft, composer);
  const ready = assessmentReady(draft, composer, disabled);
  const latest = useRef({ binding, ready, draft, composer });
  latest.current = { binding, ready, draft, composer };
  const automatic = useRef<AssessmentAutoCheck | null>(null);
  if (!automatic.current) automatic.current = new AssessmentAutoCheck(expected => {
    const input = latest.current;
    if (!input.ready || input.binding !== expected) return false;
    controller.current!.bind(input.draft, input.composer);
    void controller.current!.run();
    return true;
  });
  useEffect(() => {
    const current = controller.current!;
    current.bind(draft, composer);
    return () => current.cancel();
    // The canonical binding includes every draft version and composer input.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [binding]);
  useEffect(() => {
    const current = automatic.current!;
    current.queue(binding, ready);
    return () => current.cancel();
  }, [binding, ready]);
  const visible = visibleAssessment(state, binding);
  return <div className="cfde-research-action">
    {children(assessmentResult(visible), visible.phase === "starting" || visible.phase === "polling")}
    <CfdeAssessmentFeedback state={visible} disabled={!ready} onCheck={() => {
      if (!ready) return;
      automatic.current!.manual(binding);
      controller.current!.bind(draft, composer);
      void controller.current!.run();
    }} />
  </div>;
}
