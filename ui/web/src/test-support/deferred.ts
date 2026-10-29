// meiva:note
// A promise a test settles by hand. The assertion then runs on a counted fact -- the call
// happened, the reply landed -- instead of on a wall clock, so a loaded machine cannot turn
// the wait into a failure and a real regression cannot hide behind a longer timeout.
export interface Deferred<T> {
  readonly promise: Promise<T>;
  resolve(value: T | PromiseLike<T>): void;
  reject(reason?: unknown): void;
}

export function deferred<T>(): Deferred<T> {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
