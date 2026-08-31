import { expect, type Page, type Request, type Route } from "@playwright/test";

export const APP_ORIGIN = "http://127.0.0.1:5173";

const API_ROOTS = new Set([
  "health",
  "ready",
  "capabilities",
  "documents",
  "intakes",
  "review",
  "bills",
  "query",
  "openapi.json",
]);

export interface MockResponse {
  status?: number;
  json?: unknown;
  body?: string | Buffer;
  contentType?: string;
  headers?: Record<string, string>;
}

export type ApiResolver = (
  request: Request,
  url: URL,
) => MockResponse | undefined | Promise<MockResponse | undefined>;

export interface NetworkAudit {
  apiRequests: string[];
  externalRequests: string[];
  externalWebSockets: string[];
  pageErrors: string[];
  resolverErrors: string[];
  unhandledApiRequests: string[];
}

function isApiPath(pathname: string): boolean {
  return API_ROOTS.has(pathname.split("/").filter(Boolean)[0] ?? "");
}

async function fulfill(route: Route, response: MockResponse): Promise<void> {
  const status = response.status ?? 200;
  if (response.json !== undefined) {
    await route.fulfill({
      status,
      body: JSON.stringify(response.json),
      contentType: response.contentType ?? "application/json; charset=utf-8",
      headers: response.headers,
    });
    return;
  }
  await route.fulfill({
    status,
    body: response.body ?? "",
    contentType: response.contentType,
    headers: response.headers,
  });
}

export async function installLocalApiMock(page: Page, resolver: ApiResolver): Promise<NetworkAudit> {
  const audit: NetworkAudit = {
    apiRequests: [],
    externalRequests: [],
    externalWebSockets: [],
    pageErrors: [],
    resolverErrors: [],
    unhandledApiRequests: [],
  };

  page.on("pageerror", (error) => audit.pageErrors.push(error.message));
  page.on("websocket", (socket) => {
    const url = new URL(socket.url());
    if (url.hostname !== "127.0.0.1") audit.externalWebSockets.push(socket.url());
  });

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      await route.continue();
      return;
    }
    if (url.origin !== APP_ORIGIN) {
      audit.externalRequests.push(`${request.method()} ${request.url()}`);
      await route.abort("blockedbyclient");
      return;
    }
    if (!isApiPath(url.pathname)) {
      await route.continue();
      return;
    }

    const requestLabel = `${request.method()} ${url.pathname}${url.search}`;
    audit.apiRequests.push(requestLabel);
    if (request.method() === "GET" && url.pathname === "/health") {
      await fulfill(route, { json: { status: "ok" } });
      return;
    }
    if (request.method() === "GET" && url.pathname === "/ready") {
      await fulfill(route, {
        json: {
          status: "ready",
          intake_ready: true,
          review_ready: true,
          processing_ready: true,
          universal_processing_ready: true,
        },
      });
      return;
    }
    if (request.method() === "GET" && url.pathname === "/capabilities") {
      await fulfill(route, {
        json: {
          schema: "clerksan.universal-intake-capabilities",
          version: 1,
          process: ["csv", "xlsx"],
          sandbox_verified: true,
          registry_digest: "1".repeat(64),
          capabilities_digest: "2".repeat(64),
        },
      });
      return;
    }
    if (request.method() === "GET" && url.pathname === "/intakes") {
      await fulfill(route, { json: [] });
      return;
    }

    try {
      const response = await resolver(request, url);
      if (response) {
        await fulfill(route, response);
        return;
      }
    } catch (error) {
      audit.resolverErrors.push(error instanceof Error ? error.message : String(error));
      await fulfill(route, {
        status: 500,
        json: { code: "e2e_mock_failure", message: "The E2E API resolver failed." },
      });
      return;
    }

    audit.unhandledApiRequests.push(requestLabel);
    await fulfill(route, {
      status: 501,
      json: { code: "e2e_unhandled_api", message: `Unhandled E2E API request: ${requestLabel}` },
    });
  });

  return audit;
}

export function requestJson<T>(request: Request): T {
  const body = request.postData();
  if (!body) throw new Error(`Expected JSON body for ${request.method()} ${request.url()}.`);
  return JSON.parse(body) as T;
}

export function multipartText(request: Request): string {
  const body = request.postDataBuffer();
  if (!body) throw new Error(`Expected multipart body for ${request.method()} ${request.url()}.`);
  return body.toString("utf8");
}

export async function expectNoUnexpectedTraffic(audit: NetworkAudit): Promise<void> {
  await expect.poll(() => audit.resolverErrors).toEqual([]);
  expect(audit.unhandledApiRequests).toEqual([]);
  expect(audit.externalRequests).toEqual([]);
  expect(audit.externalWebSockets).toEqual([]);
  expect(audit.pageErrors).toEqual([]);
}

export async function expectViewportContained(page: Page): Promise<void> {
  await expect.poll(() => page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )).toBeLessThanOrEqual(1);
}
