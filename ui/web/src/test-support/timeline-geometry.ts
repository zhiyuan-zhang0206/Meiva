// meiva:note
// A layout for the test DOM, which has none: inside a root carrying `root` (spread it on the element
// wrapping a TimelineView), the viewport is 600px tall, row `n.r` occupies [(n-1)*300, n*300) of
// the content and the content is `height()` px tall. The element prototypes provide the layout
// from the first commit: the timeline's own layout effects already read it, as they read a real browser's.
interface Layout {
  top: () => number;
  setTop: (value: number) => void;
  height: () => number;
}

const layouts = new Map<string, Layout>();
let created = 0;
let installed = false;

const VIEWPORT = '[data-slot="scroll-area-viewport"]';
const VIEWPORT_PX = 600;
const ROW_PX = 300;

function layoutOf(element: Element): Layout | undefined {
  const root = element.closest<HTMLElement>("[data-timeline-geometry]");
  return root ? layouts.get(root.dataset.timelineGeometry ?? "") : undefined;
}

function rect(top: number, height: number): DOMRect {
  return { top, bottom: top + height, height, left: 0, right: 400, width: 400, x: 0, y: top, toJSON: () => ({}) };
}

function accessor<T>(name: string): { get: (element: HTMLElement) => T; set: (element: HTMLElement, value: T) => void } {
  for (let at: object | null = HTMLElement.prototype; at; at = Object.getPrototypeOf(at) as object | null) {
    const found = Object.getOwnPropertyDescriptor(at, name);
    if (found?.get) return { get: (element) => found.get?.call(element) as T, set: (element, value) => found.set?.call(element, value) };
  }
  throw new Error(`no ${name} on HTMLElement`);
}

function install() {
  if (installed) return;
  installed = true;
  const clientHeight = accessor<number>("clientHeight");
  const scrollHeight = accessor<number>("scrollHeight");
  const offsetHeight = accessor<number>("offsetHeight");
  const scrollTop = accessor<number>("scrollTop");
  Object.defineProperties(HTMLElement.prototype, {
    clientHeight: { configurable: true, get(this: HTMLElement) {
      return layoutOf(this) ? VIEWPORT_PX : clientHeight.get(this);
    } },
    scrollHeight: { configurable: true, get(this: HTMLElement) {
      const layout = layoutOf(this);
      if (!layout) return scrollHeight.get(this);
      if (this.matches(VIEWPORT)) return Math.max(VIEWPORT_PX, layout.height());
      return this.querySelector(".timeline-item") ? layout.height() : VIEWPORT_PX;
    } },
    offsetHeight: { configurable: true, get(this: HTMLElement) {
      return layoutOf(this) ? Number.parseFloat(this.style.height) || 0 : offsetHeight.get(this);
    } },
    scrollTop: {
      configurable: true,
      get(this: HTMLElement) {
        const layout = layoutOf(this);
        return layout && this.matches(VIEWPORT) ? layout.top() : scrollTop.get(this);
      },
      set(this: HTMLElement, value: number) {
        const layout = layoutOf(this);
        if (!layout || !this.matches(VIEWPORT)) {
          scrollTop.set(this, value);
          return;
        }
        layout.setTop(Math.max(0, Math.min(value, this.scrollHeight - VIEWPORT_PX)));
      },
    },
    getBoundingClientRect: { configurable: true, writable: true, value(this: HTMLElement): DOMRect {
      const layout = layoutOf(this);
      if (!layout) return Element.prototype.getBoundingClientRect.call(this);
      if (this.matches(VIEWPORT)) return rect(0, VIEWPORT_PX);
      if (this.getAttribute("role") === "log") return rect(-layout.top(), layout.height());
      if (this.dataset.itemId) return rect((Number.parseInt(this.dataset.itemId) - 1) * ROW_PX - layout.top(), ROW_PX);
      return rect(0, ROW_PX);
    } },
  });
}

export function timelineGeometry(read: {
  top: () => number;
  setTop: (value: number) => void;
  height: () => number;
  resizeCallbacks: Set<() => void>;
}) {
  install();
  const id = `geometry-${++created}`;
  layouts.set(id, read);
  const root = { "data-timeline-geometry": id } as const;
  const observer = (callback: ResizeObserverCallback): ResizeObserver => {
    const deliver = () => callback([], instance);
    const instance: ResizeObserver = {
      observe: (target) => { if (target.getAttribute("data-slot") === "scroll-area-viewport") read.resizeCallbacks.add(deliver); },
      unobserve: () => read.resizeCallbacks.delete(deliver),
      disconnect: () => read.resizeCallbacks.delete(deliver),
    };
    return instance;
  };
  return { root, observer };
}
