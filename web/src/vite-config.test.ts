import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("Vite local proxy", () => {
  it("keeps the development proxy loopback-only and rewrites Host for API validation", () => {
    const config = readFileSync(resolve(process.cwd(), "vite.config.ts"), "utf8");
    expect(config).toContain('"http://127.0.0.1:8000"');
    expect(config).toContain("changeOrigin: true");
    expect(config).toContain('"/review": proxy');
  });
});
