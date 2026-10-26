import type { createServer, IncomingMessage, ServerResponse } from "node:http";
import type { AddressInfo, Socket } from "node:net";

export interface GatewayRequest {
  method: string;
  path: string;
  headers: Readonly<Record<string, string>>;
  body: string;
}

export type GatewayReply =
  | { kind: "json"; body: unknown; status?: number; headers?: Readonly<Record<string, string>> }
  | { kind: "text"; body: string; status: number }
  | { kind: "hang" }
  | { kind: "headers-then-hang" }
  | { kind: "drop" };

export interface TestGateway {
  readonly origin: string;
  readonly requests: GatewayRequest[];
  readonly closedRequests: GatewayRequest[];
  close(): Promise<void>;
}

export const OK_JSON: GatewayReply = { kind: "json", body: {} };

function readBody(request: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    request.on("error", reject);
  });
}

function flatten(headers: IncomingMessage["headers"]): Record<string, string> {
  const flat: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers)) {
    if (value !== undefined) flat[name] = Array.isArray(value) ? value.join(", ") : value;
  }
  return flat;
}

function corsHeaders(request: IncomingMessage): Record<string, string> {
  const origin = request.headers.origin;
  if (origin === undefined) return {};
  const requested = request.headers["access-control-request-headers"];
  return {
    "access-control-allow-origin": origin,
    "access-control-allow-credentials": "true",
    "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, HEAD",
    "access-control-allow-headers": typeof requested === "string" ? requested : "",
  };
}

function send(response: ServerResponse, reply: GatewayReply, cors: Record<string, string>): void {
  switch (reply.kind) {
    case "json":
      response.writeHead(reply.status ?? 200, {
        "content-type": "application/json",
        ...cors,
        ...reply.headers,
      });
      response.end(JSON.stringify(reply.body));
      return;
    case "text":
      response.writeHead(reply.status, { "content-type": "text/plain", ...cors });
      response.end(reply.body);
      return;
    case "headers-then-hang":
      response.writeHead(200, { "content-type": "application/json", ...cors });
      response.write("{");
      return;
    case "drop":
      response.socket?.destroy();
      return;
    case "hang":
      return;
  }
}

// meiva:note
// A real HTTP server on an OS-assigned port that records every request and
// answers it from `answer`. Replies cover the outcomes a client must survive: a
// JSON or text answer, no answer at all, headers without a finished body, and a
// dropped connection. Browser-origin requests (XMLHttpRequest from the test DOM)
// get matching CORS headers and preflight answers.
export async function startGateway(
  serverFactory: typeof createServer,
  answer: (request: GatewayRequest) => GatewayReply | Promise<GatewayReply>,
): Promise<TestGateway> {
  const requests: GatewayRequest[] = [];
  const closedRequests: GatewayRequest[] = [];
  const sockets = new Set<Socket>();
  const server = serverFactory((request, response) => {
    const cors = corsHeaders(request);
    if (request.method === "OPTIONS") {
      response.writeHead(204, cors);
      response.end();
      return;
    }
    void readBody(request).then(async (body) => {
      const recorded: GatewayRequest = {
        method: request.method ?? "",
        path: request.url ?? "",
        headers: flatten(request.headers),
        body,
      };
      requests.push(recorded);
      response.once("close", () => closedRequests.push(recorded));
      const reply = await answer(recorded);
      if (!response.destroyed) send(response, reply, cors);
    });
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return {
    origin: `http://127.0.0.1:${port}`,
    requests,
    closedRequests,
    close: () =>
      new Promise<void>((resolve) => {
        for (const socket of sockets) socket.destroy();
        server.close(() => resolve());
      }),
  };
}
