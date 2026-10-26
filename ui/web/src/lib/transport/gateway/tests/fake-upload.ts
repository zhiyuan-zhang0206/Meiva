import type { UploadRequest } from "../../transport";

// meiva:note
// The progress events of an XMLHttpRequest upload cannot be produced by a real
// local server on demand, so this drives them by hand: the test reports progress
// and then finishes the exchange. It records what the shell opened, headed and sent.
export class FakeUpload implements UploadRequest {
  status = 0;
  responseText = "";
  withCredentials = false;
  onload: UploadRequest["onload"] = null;
  onerror: UploadRequest["onerror"] = null;
  readonly upload = new EventTarget();
  readonly headers: [string, string][] = [];
  opened: [string, string] | null = null;
  sent: FormData | null = null;

  open(method: string, url: string): void {
    this.opened = [method, url];
  }

  setRequestHeader(name: string, value: string): void {
    this.headers.push([name, value]);
  }

  send(body: FormData): void {
    this.sent = body;
  }

  progress(loaded: number, total: number, lengthComputable: boolean): void {
    this.upload.dispatchEvent(new ProgressEvent("progress", { lengthComputable, loaded, total }));
  }

  finish(status: number, responseText: string): void {
    this.status = status;
    this.responseText = responseText;
    this.onload?.call(this as unknown as XMLHttpRequest, new Event("load") as ProgressEvent);
  }
}
