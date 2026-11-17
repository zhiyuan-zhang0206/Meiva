import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { SavedScroll } from "@/lib/layout/scroll-memory";

export interface TimelinePositionControl {
  token: object;
  position: SavedScroll | null;
  ready: boolean;
  writable: boolean;
  remember(position: SavedScroll, item: BackendTimelineItem | null): void;
  navigated(): void;
  restored(): void;
}
