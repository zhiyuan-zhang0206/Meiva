"use client";

import { Loader2, AlertCircle, Paperclip } from "lucide-react";
import { useRef, useCallback } from "react";

import { cn } from "@/lib/format/utils";

interface Props {
  agentId: number | null;
  onUpload: (files: File[]) => Promise<void>;
  uploading: boolean;
  progress: number;
  // Number of files in the in-flight upload batch; count > 1 surfaces a
  // "N files" prefix next to the percentage.
  count: number;
  error: string | null;
}

export function UploadButton({ agentId, onUpload, uploading, progress, count, error }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);

  const handleClick = useCallback(() => {
    inputRef.current?.click();
  }, []);

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = e.target.files;
      if (files && files.length > 0) {
        void onUpload(Array.from(files));
        e.target.value = "";
      }
    },
    [onUpload],
  );

  // While uploading render the spinner — no new upload allowed (input not in
  // DOM). For a multi-file batch prefix the file count ("3 files · 42%").
  if (uploading) {
    return (
      <span
        className="inline-flex h-9 shrink-0 items-center gap-1 px-1 text-[10px] text-muted-foreground"
        aria-label={count > 1 ? `${count} files uploading, ${progress}%` : `Uploading, ${progress}%`}
      >
        <Loader2 className="size-4 animate-spin" />
        {count > 1 ? `${count} · ${progress}%` : `${progress}%`}
      </span>
    );
  }

  // Default and error states share the same input + button. On error
  // swap the icon to AlertCircle and color it red, but keep the button
  // clickable — clicking re-triggers the file picker, and once the user
  // selects a new file the uploadHandler calls setUploadError(null) to
  // clear the error naturally. Without this retry path the user would
  // be stuck on the error icon (input not in DOM, no way to trigger a
  // new upload).
  return (
    <>
      <input
        ref={inputRef}
        type="file"
        multiple
        className="sr-only"
        onChange={handleChange}
        disabled={agentId == null}
      />
      <button
        type="button"
        aria-label={error ? `Upload failed: ${error} (click to retry)` : "Upload file"}
        title={error ?? undefined}
        onClick={handleClick}
        disabled={agentId == null}
        className={cn(
          "inline-flex size-9 shrink-0 items-center justify-center rounded-full transition-colors",
          "disabled:pointer-events-none disabled:opacity-30",
          error
            ? "text-destructive hover:bg-destructive/10"
            : "text-muted-foreground hover:bg-accent hover:text-foreground",
        )}
      >
        {error ? <AlertCircle className="size-4" /> : <Paperclip className="size-4" />}
      </button>
    </>
  );
}
