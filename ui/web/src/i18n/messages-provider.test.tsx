import { cleanup, render, screen } from "@testing-library/react";
import { useTranslations } from "next-intl";
import { afterEach, describe, expect, it } from "vitest";

import { MessagesProvider } from "./messages-provider";

function Probe() {
  const t = useTranslations("spawn");
  return <span>{t("spawnAgent")}</span>;
}

afterEach(cleanup);

describe("MessagesProvider", () => {
  it("renders the English catalog", () => {
    render(
      <MessagesProvider>
        <Probe />
      </MessagesProvider>,
    );
    expect(screen.getByText("Spawn agent")).toBeTruthy();
  });
});
