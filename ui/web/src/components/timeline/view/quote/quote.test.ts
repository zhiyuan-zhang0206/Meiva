import { describe, expect, it } from "vitest";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { quoteFromRange } from "./quote";

const item: BackendTimelineItem = {
  item_id: "4.0", kind: "agent_chat", payload: "hello exact\nselection", source: null,
  created_at: "2026-11-17T10:00:00Z", inbound_id: null, source_message_id: "durable-message",
  source_block_idx: 0, show_timestamp: true, user_facing: true,
};
function fixture() {
  const root = document.createElement("div");
  root.innerHTML = '<div class="timeline-item" data-item-id="4.0"><div data-quote-body>hello exact\nselection</div></div><div class="timeline-item" data-item-id="5.0"><div data-quote-body>other message</div></div><textarea>draft</textarea>';
  const text = root.querySelector("[data-quote-body]")!.firstChild!;
  const range = document.createRange(); range.setStart(text, 6); range.setEnd(text, text.textContent!.length);
  return { root, range };
}
describe("selected text quotes", () => {
  it("retains exact selected text and durable provenance", () => {
    const { root, range } = fixture();
    expect(quoteFromRange(range, root, [item], 12)).toMatchObject({ text: "exact\nselection", source_agent_id: 12, source_message_id: "durable-message", source_block_idx: 0 });
  });
  it("quotes a marker body fragment with stable provenance while excluding its title", () => {
    const { root, range } = fixture();
    const marker: BackendTimelineItem = { ...item, kind: "system_marker", source: "system_note", source_message_id: "durable-marker" };
    root.querySelector(".timeline-item")!.insertAdjacentHTML("afterbegin", "<button>Error title</button>");
    expect(quoteFromRange(range, root, [marker], 12)).toMatchObject({ text: "exact\nselection", source_message_id: "durable-marker", source_block_idx: 0 });
    range.selectNodeContents(root.querySelector("button")!);
    expect(quoteFromRange(range, root, [marker], 12)).toBeNull();
  });
  it("rejects cross-message, input and partial ranges", () => {
    const { root, range } = fixture();
    expect(quoteFromRange(range, root, [{ ...item, partial: true }], 12)).toBeNull();
    expect(quoteFromRange(range, root, [{ ...item, source_message_id: null }], 12)?.text).toBe("exact\nselection");
    range.setEnd(root.querySelectorAll("[data-quote-body]")[1].firstChild!, 3);
    expect(quoteFromRange(range, root, [item], 12)).toBeNull();
    range.selectNodeContents(root.querySelector("textarea")!);
    expect(quoteFromRange(range, root, [item], 12)).toBeNull();
  });
});
