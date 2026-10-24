export interface AuthResult {
  authenticated: boolean;
}

type Scripted = () => Promise<AuthResult>;

// meiva:note
// A session check whose answers a test scripts: a standing answer, answers for
// the next probes in order (including a probe that fails because the gateway is
// unreachable), and probes whose answer the test releases later. It counts probes
// so a test can show that only the leader asks.
export class ScriptedAuth {
  probes = 0;
  private standing: AuthResult = { authenticated: true };
  private queue: Scripted[] = [];

  always(result: AuthResult): void {
    this.standing = result;
  }

  once(result: AuthResult): void {
    this.queue.push(() => Promise.resolve(result));
  }

  onceFailing(error: Error): void {
    this.queue.push(() => Promise.reject(error));
  }

  onceDeferred(): { release: (result: AuthResult) => void } {
    let release!: (result: AuthResult) => void;
    const pending = new Promise<AuthResult>((resolve) => {
      release = resolve;
    });
    this.queue.push(() => pending);
    return { release };
  }

  checkAuth = (): Promise<AuthResult> => {
    this.probes += 1;
    const next = this.queue.shift();
    return next ? next() : Promise.resolve(this.standing);
  };
}
