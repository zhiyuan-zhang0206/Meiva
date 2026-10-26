import {
  NextIntlClientProvider,
  createTranslator,
  useTranslations as useActualTranslations,
  type Messages,
  type NamespaceKeys,
  type NestedKeyOf,
} from "next-intl";

import en from "../../messages/en";

export { NextIntlClientProvider, createTranslator };

export function useTranslations<
  NestedKey extends NamespaceKeys<Messages, NestedKeyOf<Messages>> = never,
>(namespace?: NestedKey): ReturnType<typeof createTranslator<Messages, NestedKey>> {
  try {
    return useActualTranslations(namespace);
  } catch {
    return createTranslator<Messages, NestedKey>({
      locale: "en",
      messages: en,
      namespace,
      onError: () => undefined,
      getMessageFallback: ({ key }) => key,
    });
  }
}
