#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";


const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "..");
const TARGET_NAME = "ヘアゴム（2個セット）";
const TARGET_CATEGORY = "294884";
const EXPECTED_SKU = "MIKI-36-2001-57200039999";
const LIST_FRAGMENT = "/shopapi/Goods/index";
const DETAIL_FRAGMENT = "/shopapi/goods/getFormatInfo";
const MUTATION_FRAGMENTS = [
  "/shopapi/Goods/newAddGood",
  "/v1/cos/upload",
  "/shopapi/Goods/edit",
  "/shopapi/Goods/delete",
  "/shopapi/Goods/del",
  "/shopapi/Goods/shelf",
  "/shopapi/Goods/batch",
];


function parseArgs(argv) {
  const args = { timeoutMs: 60000 };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (key === "--private-dir") args.privateDir = argv[++index];
    else if (key === "--playwright-root") args.playwrightRoot = argv[++index];
    else if (key === "--timeout-ms") args.timeoutMs = Number(argv[++index]);
    else if (key === "--self-test") args.selfTest = true;
    else throw new Error(`unknown argument: ${key}`);
  }
  return args;
}


function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}


function isInside(parent, candidate) {
  const relative = path.relative(path.resolve(parent), path.resolve(candidate));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}


function assertPrivateDir(value) {
  if (!value) throw new Error("--private-dir is required");
  const resolved = path.resolve(value);
  if (isInside(REPO_ROOT, resolved)) {
    throw new Error("private evidence directory must be outside the Git worktree");
  }
  return resolved;
}


function writePrivateJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  fs.chmodSync(filePath, 0o600);
}


function latestBrowserCreateCapture(privateDir) {
  const files = fs.readdirSync(privateDir)
    .filter((name) => name.startsWith("shijiu-browser-exact-") && name.endsWith(".private.json"))
    .map((name) => {
      const filePath = path.join(privateDir, name);
      return { filePath, modified: fs.statSync(filePath).mtimeMs };
    })
    .sort((left, right) => right.modified - left.modified);
  if (!files.length) throw new Error("no browser-exact private CREATE capture found");
  const rawBytes = fs.readFileSync(files[0].filePath);
  const raw = JSON.parse(rawBytes.toString("utf8"));
  const createPayload = JSON.parse(raw.playwright_request?.post_data || "{}");
  if (Number(createPayload.id || 0) > 0 || !createPayload.good_name) {
    throw new Error("latest browser-exact evidence is not a CREATE capture");
  }
  return {
    filePath: files[0].filePath,
    sha256: sha256(rawBytes),
    createPayload,
  };
}


async function loadPlaywright(playwrightRoot) {
  try {
    return await import("playwright");
  } catch (firstError) {
    if (!playwrightRoot) throw firstError;
    const requireFromRoot = createRequire(path.join(path.resolve(playwrightRoot), "package.json"));
    return requireFromRoot("playwright");
  }
}


function responseRows(value) {
  if (Array.isArray(value)) return value.filter((row) => row && typeof row === "object");
  if (!value || typeof value !== "object") return [];
  for (const key of ["data", "list", "rows", "items", "records"]) {
    const rows = responseRows(value[key]);
    if (rows.length) return rows;
  }
  return [];
}


function responseCount(value) {
  for (const container of [value, value?.data]) {
    if (!container || typeof container !== "object" || Array.isArray(container)) continue;
    const parsed = Number(container.count);
    if (Number.isInteger(parsed) && parsed >= 0) return parsed;
  }
  const parsed = Number(value?.count);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
}


function rowId(row) {
  return String(row?.id ?? row?.good_id ?? row?.goods_id ?? "");
}


function rowName(row) {
  return String(row?.good_name ?? row?.goods_name ?? row?.name ?? "");
}


function orderedFormObject(params) {
  return Object.fromEntries([...params.entries()]);
}


function cloneHeaders(headers, bodyText) {
  const result = { ...headers };
  delete result.host;
  delete result["content-length"];
  // Playwright recalculates transport headers. Keep every browser-supplied
  // semantic/session header, including Cookie when present.
  result["content-length"] = String(Buffer.byteLength(bodyText));
  return result;
}


function assertReadOnlyUrl(value) {
  const url = String(value || "");
  if (!url.includes(LIST_FRAGMENT) && !url.includes(DETAIL_FRAGMENT)) {
    throw new Error(`blocked non-read endpoint: ${new URL(url).pathname}`);
  }
  if (MUTATION_FRAGMENTS.some((fragment) => url.includes(fragment))) {
    throw new Error("blocked Shijiu mutation endpoint");
  }
}


async function browserContextPost(context, url, headers, fields, timeoutMs) {
  assertReadOnlyUrl(url);
  const body = new URLSearchParams(fields).toString();
  const response = await context.request.fetch(url, {
    method: "POST",
    headers: cloneHeaders(headers, body),
    data: body,
    timeout: timeoutMs,
  });
  const text = await response.text();
  return {
    status: response.status(),
    headers: response.headers(),
    body_text: text,
    json: JSON.parse(text),
  };
}


async function exactNameQuery(context, base, goodType, timeoutMs) {
  const firstForm = new URLSearchParams(base.postData);
  const original = orderedFormObject(firstForm);
  firstForm.set("good_name", TARGET_NAME);
  firstForm.set("good_type", goodType);
  firstForm.set("page", "1");
  const pageSize = Math.max(1, Number(firstForm.get("page_size") || 20));
  const responses = [];
  const first = await browserContextPost(
    context, base.url, base.headers, orderedFormObject(firstForm), timeoutMs,
  );
  responses.push(first);
  const declaredCount = responseCount(first.json);
  const pageCount = Math.min(100, Math.max(1, Math.ceil((declaredCount || 0) / pageSize)));
  for (let page = 2; page <= pageCount; page += 1) {
    const pageForm = new URLSearchParams(firstForm);
    pageForm.set("page", String(page));
    responses.push(await browserContextPost(
      context, base.url, base.headers, orderedFormObject(pageForm), timeoutMs,
    ));
  }
  const rows = responses.flatMap((response) => responseRows(response.json));
  const exactRows = rows.filter((row) => rowName(row) === TARGET_NAME && rowId(row));
  const changedFields = Object.keys(orderedFormObject(firstForm)).filter(
    (key) => original[key] !== firstForm.get(key),
  );
  return {
    label: goodType ? "category_294884" : "all_categories",
    good_type: goodType,
    changed_fields_from_ui_request: changedFields,
    request_form: orderedFormObject(firstForm),
    declared_count: declaredCount,
    page_size: pageSize,
    pages_read: responses.length,
    exact_rows: exactRows,
    responses,
  };
}


async function run(args) {
  const privateDir = assertPrivateDir(args.privateDir);
  const playwright = await loadPlaywright(args.playwrightRoot);
  const capture = latestBrowserCreateCapture(privateDir);
  const profileDir = path.join(privateDir, "chrome-profile");
  const context = await playwright.chromium.launchPersistentContext(profileDir, {
    channel: "chrome",
    headless: true,
  });
  const blockedMutations = [];
  try {
    await context.route("**/*", async (route) => {
      const url = route.request().url();
      if (MUTATION_FRAGMENTS.some((fragment) => url.includes(fragment))) {
        blockedMutations.push({ method: route.request().method(), path: new URL(url).pathname });
        await route.abort("blockedbyclient");
        return;
      }
      await route.continue();
    });
    const page = await context.newPage();
    const listResponsePromise = context.waitForEvent("response", {
      predicate: (response) => response.url().includes(LIST_FRAGMENT),
      timeout: args.timeoutMs,
    });
    await page.goto("https://shijiu.wfcorp.cn/wf/admin/shop/newshop_list", {
      waitUntil: "domcontentloaded",
      timeout: args.timeoutMs,
    });
    const listResponse = await listResponsePromise;
    const listRequest = listResponse.request();
    if (listRequest.method() !== "POST") throw new Error("real Goods.index request is not POST");
    const listHeaders = await listRequest.allHeaders();
    const listPostData = listRequest.postData() || "";
    const baseForm = new URLSearchParams(listPostData);
    const urlToken = String(listRequest.url()).match(/[?&]token=([^&#]+)/)?.[1] || "";
    const formToken = baseForm.get("token") || "";
    const formSecret = baseForm.get("secret") || "";
    if (!urlToken || !formSecret || (formToken && decodeURIComponent(urlToken) !== formToken)) {
      throw new Error("real Goods.index request lacks consistent UI auth context");
    }
    const base = {
      method: listRequest.method(),
      url: listRequest.url(),
      headers: listHeaders,
      postData: listPostData,
    };
    const categoryQuery = await exactNameQuery(context, base, TARGET_CATEGORY, args.timeoutMs);
    const unscopedQuery = await exactNameQuery(context, base, "", args.timeoutMs);
    const candidateRows = new Map();
    for (const query of [categoryQuery, unscopedQuery]) {
      for (const row of query.exact_rows) candidateRows.set(rowId(row), row);
    }
    const prefix = String(listRequest.url()).split("/shopapi/")[0];
    const detailUrl = `${prefix}${DETAIL_FRAGMENT}&token=${urlToken}`;
    const details = [];
    for (const [productId, listRow] of candidateRows) {
      const detailForm = { secret: formSecret };
      if (formToken) detailForm.token = formToken;
      detailForm.id = productId;
      const response = await browserContextPost(
        context, detailUrl, listHeaders, detailForm, args.timeoutMs,
      );
      details.push({ product_id: productId, list_row: listRow, request_form: detailForm, response });
    }
    const readCount = categoryQuery.responses.length + unscopedQuery.responses.length + details.length;
    const raw = {
      schema_version: 1,
      captured_at: new Date().toISOString(),
      mode: "MIKIHOUSE_UI_CONTEXT_STRICT_READ_ONLY_RECONCILIATION",
      target_name: TARGET_NAME,
      target_category_id: Number(TARGET_CATEGORY),
      expected_backend_sku_code: EXPECTED_SKU,
      browser_create_capture_sha256: capture.sha256,
      browser_create_business_payload: capture.createPayload,
      ui_goods_index_request: {
        method: base.method,
        url: base.url,
        headers: base.headers,
        post_data: base.postData,
      },
      queries: [categoryQuery, unscopedQuery],
      details,
      safety: {
        read_only_request_count: readCount,
        target_mutation_requests_sent: 0,
        blocked_mutation_request_count: blockedMutations.length,
        blocked_mutations: blockedMutations,
        allowed_paths: [LIST_FRAGMENT, DETAIL_FRAGMENT],
      },
    };
    const timestamp = raw.captured_at.replace(/[:.]/g, "-");
    const outputPath = path.join(privateDir, `shijiu-ui-context-reconciliation-${timestamp}.private.json`);
    writePrivateJson(outputPath, raw);
    process.stdout.write(`${JSON.stringify({
      status: candidateRows.size ? "UI_CONTEXT_CANDIDATES_CAPTURED" : "UI_CONTEXT_NO_EXACT_NAME_MATCH",
      candidate_product_count: candidateRows.size,
      read_only_requests: readCount,
      target_mutation_requests_sent: 0,
      private_evidence_sha256: sha256(fs.readFileSync(outputPath)),
      private_file_written: true,
    })}\n`);
  } finally {
    await context.close().catch(() => {});
  }
}


function selfTest() {
  const base = new URLSearchParams("secret=s&token=t&page=1&page_size=20&good_type=&good_name=&status=0");
  const changed = new URLSearchParams(base);
  changed.set("good_name", TARGET_NAME);
  changed.set("good_type", TARGET_CATEGORY);
  const changedFields = [...changed.keys()].filter((key) => base.get(key) !== changed.get(key));
  if (JSON.stringify(changedFields) !== JSON.stringify(["good_type", "good_name"])) {
    throw new Error("UI form preservation self-test failed");
  }
  for (const pathValue of [LIST_FRAGMENT, DETAIL_FRAGMENT]) {
    assertReadOnlyUrl(`https://shijiu.wfcorp.cn${pathValue}&token=test`);
  }
  let mutationBlocked = false;
  try {
    assertReadOnlyUrl("https://shijiu.wfcorp.cn/shopapi/Goods/newAddGood&token=test");
  } catch {
    mutationBlocked = true;
  }
  if (!mutationBlocked) throw new Error("mutation guard self-test failed");
  process.stdout.write(`${JSON.stringify({ status: "PASS", target_mutation_requests_sent: 0 })}\n`);
}


const args = parseArgs(process.argv.slice(2));
if (args.selfTest) selfTest();
else run(args).catch((error) => {
  process.stderr.write(`${error?.name || "Error"}: ${error?.message || String(error)}\n`);
  process.exitCode = 2;
});
