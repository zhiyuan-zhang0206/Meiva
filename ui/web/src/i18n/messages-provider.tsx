"use client";

import { NextIntlClientProvider } from "next-intl";

import messages from "../../messages/en";

export function MessagesProvider({ children }: { children: React.ReactNode }) {
  return (
    <NextIntlClientProvider locale="en" messages={messages}>
      {children}
    </NextIntlClientProvider>
  );
}
