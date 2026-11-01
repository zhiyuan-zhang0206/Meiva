import type { AgentMessageEnqueued } from "../../contracts/types";
import {
  messageInit,
  MessageDeliveryUnknownError,
  nextDeliveryStep,
  type DeliveryStep,
} from "./plans";
import type { Http } from "./http";

export function sendMessageWithReconciliation(
  http: Http,
  agentId: number,
  content: string,
  clientMessageId: string,
): Promise<AgentMessageEnqueued> {
  const path = `/api/agents/${agentId}/messages`;
  const init = messageInit(content, clientMessageId);
  const runStep = (step: DeliveryStep): Promise<AgentMessageEnqueued> => {
    const { action } = step;
    switch (action.kind) {
      case "post":
      case "reconcile": {
        const target = action.kind === "post" ? path : `${path}/reconcile`;
        return http.jsonTimed<AgentMessageEnqueued>(target, init, action.timeoutMs).then(
          (enqueued): Promise<AgentMessageEnqueued> => {
            const next = nextDeliveryStep(step.state, { kind: "succeeded" });
            if (next.action.kind === "done") return Promise.resolve(enqueued);
            return runStep(next);
          },
          (error: unknown): Promise<AgentMessageEnqueued> =>
            runStep(nextDeliveryStep(step.state, { kind: "failed", error })),
        );
      }
      case "wait":
        return http
          .wait(action.ms)
          .then(() => runStep(nextDeliveryStep(step.state, { kind: "waited" })));
      case "fail":
        return new Promise<never>(() => {
          throw action.error;
        });
      case "unconfirmed":
        return Promise.reject(new MessageDeliveryUnknownError(clientMessageId));
      case "done":
        return Promise.reject(new Error("delivery finished without a receipt"));
    }
  };
  return runStep(nextDeliveryStep({ stage: "start" }, { kind: "begin" }));
}
