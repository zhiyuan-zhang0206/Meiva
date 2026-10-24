// meiva:note
// A single fetch completion that deliberately violates AbortSignal. A real fetch
// rejects after abort, so only this fault needs a double: the caller must still
// reject a late result from a transport that ignored its cancellation.
export class UncancellableFetch {
  private complete!: (response: Response) => void;
  private readonly response = new Promise<Response>((resolve) => { this.complete = resolve; });
  readonly fetch: typeof fetch = () => this.response;

  respond(body: unknown): Response {
    const response = new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
    this.complete(response);
    return response;
  }
}
