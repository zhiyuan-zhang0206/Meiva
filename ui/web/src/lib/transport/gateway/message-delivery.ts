import type { AgentMessageEnqueued, ContentBlock } from "../../contracts/types";
import {
  messageInit,
  MessageDeliveryUnknownError,
  nextDeliveryStep,
  type DeliveryEvent,
  type DeliveryStep,
} from "./plans";
import type { Http } from "./http";

const wait = (milliseconds: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

export async function sendMessageWithReconciliation(
  http: Http,
  agentId: number,
  content: string | ContentBlock[],
  clientMessageId: string,
): Promise<AgentMessageEnqueued> {
  const path = `/api/agents/${agentId}/messages`;
  const init = messageInit(content, clientMessageId);
  let step: DeliveryStep = nextDeliveryStep({ stage: "start" }, { kind: "begin" });
  for (;;) {
    const { action } = step;
    switch (action.kind) {
      case "post":
      case "reconcile": {
        const target = action.kind === "post" ? path : `${path}/reconcile`;
        let event: DeliveryEvent;
        let enqueued: AgentMessageEnqueued | undefined;
        try {
          enqueued = await http.jsonTimed<AgentMessageEnqueued>(target, init, action.timeoutMs);
          event = { kind: "succeeded" };
        } catch (error: unknown) {
          event = { kind: "failed", error };
        }
        step = nextDeliveryStep(step.state, event);
        if (step.action.kind === "done" && enqueued !== undefined) return enqueued;
        break;
      }
      case "wait":
        await wait(action.ms);
        step = nextDeliveryStep(step.state, { kind: "waited" });
        break;
      case "fail":
        throw action.error;
      case "unconfirmed":
        throw new MessageDeliveryUnknownError(clientMessageId);
      case "done":
        throw new Error("delivery finished without a receipt");
    }
  }
}
