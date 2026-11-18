export function timelineGeometry(read: {
  top: () => number;
  setTop: (value: number) => void;
  height: () => number;
  resizeCallbacks: Set<() => void>;
  itemTop?: (itemId: string) => number;
}) {
  const prepare = (root: HTMLElement) => {
    for (const element of [root, ...root.querySelectorAll<HTMLElement>("*")]) {
      Object.defineProperties(element, {
        clientHeight: { configurable: true, get: () => 600 },
        scrollHeight: { configurable: true, get: () => element.querySelectorAll(".timeline-item").length ? read.height() : 600 },
        offsetHeight: { configurable: true, get: () => Number.parseFloat(element.style.height) || 0 },
        scrollTop: { configurable: true, get: read.top, set: (value: number) => {
          read.setTop(Math.max(0, Math.min(value, element.scrollHeight - element.clientHeight)));
        } },
        getBoundingClientRect: { configurable: true, value: () => {
          const y = element.dataset.itemId
            ? (read.itemTop?.(element.dataset.itemId) ?? (Number.parseInt(element.dataset.itemId) - 1) * 300) - read.top() : 0;
          return { top: y, bottom: y + 300, height: 300, left: 0, right: 400, width: 400 } as DOMRect;
        } },
      });
    }
  };
  const observer = (callback: ResizeObserverCallback): ResizeObserver => {
    const deliver = () => callback([], instance);
    const instance: ResizeObserver = {
      observe: (target) => { if (target.getAttribute("data-slot") === "scroll-area-viewport") read.resizeCallbacks.add(deliver); },
      unobserve: () => read.resizeCallbacks.delete(deliver),
      disconnect: () => read.resizeCallbacks.delete(deliver),
    };
    return instance;
  };
  return { prepare, observer };
}
