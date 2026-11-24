// Global next-intl type augmentation — makes useTranslations() key-checked
// against the messages/en catalog at compile time (a typo'd or removed
// key fails typecheck). The UI is English only, so messages/en is the single catalog.
import type en from "../../messages/en";

declare module "next-intl" {
  interface AppConfig {
    Locale: "en";
    Messages: typeof en;
  }
}

export {};
