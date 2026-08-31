import { defineConfig } from "@playwright/test";

const appUrl = "http://127.0.0.1:5173";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: "list",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  outputDir: ".playwright-results",
  use: {
    baseURL: appUrl,
    browserName: "chromium",
    channel: "chrome",
    locale: "en-US",
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "desktop-chrome",
      use: { viewport: { width: 1280, height: 900 } },
    },
    {
      name: "narrow-320-chrome",
      use: { viewport: { width: 320, height: 800 } },
    },
  ],
  webServer: {
    command: "npm run dev",
    url: appUrl,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
