import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import AppShell from "@/components/app-shell";
import { I18nProvider, useI18n } from "@/lib/i18n";

function LocaleSwitches(): React.ReactElement {
  const { setLocale } = useI18n();
  return <><button onClick={() => setLocale("vi")}>VI</button><button onClick={() => setLocale("ja")}>JA</button></>;
}

describe("AppShell branding", () => {
  afterEach(cleanup);

  it("uses project-owned Clerk-san lockup and compact mark assets", () => {
    const { container } = render(
      <I18nProvider><AppShell route="review" onRouteChange={() => undefined} status="ready">Workspace</AppShell></I18nProvider>,
    );

    const lockup = container.querySelector(".brand-lockup-image");
    const markSource = container.querySelector(".brand-mark img")?.getAttribute("src") ?? "";
    expect(lockup).toHaveAttribute("src", expect.stringContaining("clerksan-lockup"));
    expect(lockup).toHaveAccessibleName("Clerk-san");
    expect(markSource).toContain("clerksan-mark");
    expect(markSource).not.toMatch(/^data:/);
    expect(screen.getByRole("combobox", { name: "Language" })).toBeInTheDocument();
  });

  it("uses the project-owned Clerk Pivot navigation icon set", () => {
    render(
      <I18nProvider><AppShell route="review" onRouteChange={() => undefined} status="ready">Workspace</AppShell></I18nProvider>,
    );

    const expectedIcons = [
      ["Intake", "intake-document"],
      ["Review", "human-review"],
      ["Documents", "verified-record"],
      ["Bills", "recurring-bill"],
      ["Search", "search-evidence"],
    ] as const;

    for (const [label, assetName] of expectedIcons) {
      const icon = screen.getByRole("button", { name: label }).querySelector<HTMLElement>(".nav-icon");
      expect(icon).not.toBeNull();
      expect(icon).toHaveAttribute("aria-hidden", "true");
      expect(icon?.style.getPropertyValue("--nav-icon")).toContain(assetName);
    }
  });

  it("localizes the workspace navigation landmark", () => {
    render(
      <I18nProvider><LocaleSwitches /><AppShell route="review" onRouteChange={() => undefined} status="ready">Workspace</AppShell></I18nProvider>,
    );

    expect(screen.getByRole("navigation", { name: "Workspace sections" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "VI" }));
    expect(screen.getByRole("navigation", { name: "Các mục trong không gian làm việc" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "JA" }));
    expect(screen.getByRole("navigation", { name: "ワークスペースのセクション" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Workspace sections" })).not.toBeInTheDocument();
  });
});
