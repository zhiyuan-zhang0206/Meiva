"use client";

// A small modal that collects an initial prompt for a lifecycle action
// (fork / resurrect) and calls back with the trimmed, non-empty text.
// Submit is disabled while the textarea is blank — the "quick" no-prompt
// path is a separate menu item, so this dialog always carries a prompt.

import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { AutoExpandTextarea } from "@/components/ui/auto-expand-textarea";

export interface PromptDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  placeholder?: string;
  submitLabel: string;
  /** Called with the trimmed prompt (never empty — submit is gated on it). */
  onSubmit: (prompt: string) => void;
}

// meiva:note
// A prompt is a message: the field sends the way every other send-type field does
// (Enter sends, Shift+Enter breaks the line). Closing the dialog resets the text,
// so the next open starts blank.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md
export function PromptDialog({
  open,
  onOpenChange,
  title,
  description,
  placeholder,
  submitLabel,
  onSubmit,
}: PromptDialogProps) {
  const t = useTranslations("common");
  const [text, setText] = useState("");
  const trimmed = text.trim();

  const submit = () => {
    if (trimmed === "") return;
    onSubmit(trimmed);
    onOpenChange(false);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setText("");
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? (
            <DialogDescription>{description}</DialogDescription>
          ) : null}
        </DialogHeader>
        <AutoExpandTextarea
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={placeholder}
          onSend={submit}
          className="min-h-28 font-mono text-xs"
        />
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            {t("cancel")}
          </Button>
          <Button size="sm" onClick={submit} disabled={trimmed === ""}>
            {submitLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
