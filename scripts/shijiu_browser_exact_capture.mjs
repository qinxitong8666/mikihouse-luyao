#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";


const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "..");
const TARGET_FRAGMENT = "/shopapi/Goods/newAddGood";
const DEFAULT_REPORT = path.join(
  REPO_ROOT,
  "deliverables/shijiu_import/browser_exact_capture_readiness.json",
);
const DEFAULT_MIKI_REPORT = path.join(
  REPO_ROOT,
  "deliverables/shijiu_import/minimal_create_probe_report.json",
);
const DEFAULT_MIKI_DIFF = path.join(
  REPO_ROOT,
  "deliverables/shijiu_import/minimal_create_payload_diff.json",
);
const CAPTURE_CONFIRMATION = "SHIJIU_BROWSER_EXACT_HUMAN_SAVE_CAPTURE";
const PUBLIC_HEADER_VALUES = new Set([
  "accept",
  "accept-language",
  "content-type",
  "origin",
  "referer",
  "sec-ch-ua",
  "sec-ch-ua-mobile",
  "sec-ch-ua-platform",
  "sec-fetch-dest",
  "sec-fetch-mode",
  "sec-fetch-site",
  "user-agent",
]);
function parseArgs(argv) {
  const args = {
    mode: "preflight",
    cdpUrl: "http://127.0.0.1:9222",
    sanitizedReport: DEFAULT_REPORT,
    currentMikiReport: DEFAULT_MIKI_REPORT,
    currentMikiDiff: DEFAULT_MIKI_DIFF,
    timeoutMs: 15 * 60 * 1000,
    chromeExtensionStatus: "unknown",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (key === "--self-test") args.selfTest = true;
    else if (key === "--launch-private-profile") args.launchPrivateProfile = true;
    else if (key === "--mode") args.mode = argv[++index];
    else if (key === "--cdp-url") args.cdpUrl = argv[++index];
    else if (key === "--private-dir") args.privateDir = argv[++index];
    else if (key === "--sanitized-report") args.sanitizedReport = argv[++index];
    else if (key === "--historical-wawu-template") args.historicalWawuTemplate = argv[++index];
    else if (key === "--current-mikihouse-report") args.currentMikiReport = argv[++index];
    else if (key === "--current-mikihouse-diff") args.currentMikiDiff = argv[++index];
    else if (key === "--playwright-root") args.playwrightRoot = argv[++index];
    else if (key === "--start-url") args.startUrl = argv[++index];
    else if (key === "--timeout-ms") args.timeoutMs = Number(argv[++index]);
    else if (key === "--chrome-extension-status") args.chromeExtensionStatus = argv[++index];
    else if (key === "--confirm-capture") args.confirmCapture = argv[++index];
    else throw new Error(`unknown argument: ${key}`);
  }
  return args;
}


function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}


function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}


function writeJsonSecure(filePath, value, mode = 0o600) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode,
  });
  fs.chmodSync(filePath, mode);
}


function isInside(parent, candidate) {
  const relative = path.relative(path.resolve(parent), path.resolve(candidate));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}


function assertPrivatePath(privateDir) {
  if (!privateDir) throw new Error("--private-dir is required");
  if (isInside(REPO_ROOT, privateDir)) {
    throw new Error("private capture directory must be outside the Git worktree");
  }
  return path.resolve(privateDir);
}


function safeUrlShape(value) {
  const text = String(value || "");
  const match = text.match(/^([a-z]+:)?\/\/([^/]+)(.*)$/i);
  const scheme = match?.[1]?.replace(":", "") || "";
  const host = match?.[2] || "";
  const remainder = match?.[3] || text;
  const endpointPath = remainder
    .split(/[?&][^/]*=/, 1)[0]
    .replace(/^\/shijiu(?=\/shopapi\/)/, "");
  const queryNames = [...text.matchAll(/[?&]([^=&?#]+)=/g)].map((row) => row[1]);
  return {
    scheme,
    host,
    path: endpointPath,
    query_parameter_names: [...new Set(queryNames)].sort(),
    query_values_in_report: false,
  };
}


function normalizeHeaders(headers) {
  return Object.fromEntries(
    Object.entries(headers || {}).map(([key, value]) => [String(key).toLowerCase(), String(value)]),
  );
}


function headerShape(headers) {
  const normalized = normalizeHeaders(headers);
  return {
    names: Object.keys(normalized).sort(),
    cookie_present: Boolean(normalized.cookie),
    authorization_present: Boolean(normalized.authorization),
    public_value_sha256: Object.fromEntries(
      Object.entries(normalized)
        .filter(([name]) => PUBLIC_HEADER_VALUES.has(name))
        .map(([name, value]) => [name, sha256(value)])
        .sort(([left], [right]) => left.localeCompare(right)),
    ),
    sensitive_values_included: false,
  };
}


function valueType(value) {
  if (Array.isArray(value)) return "array";
  if (value === null) return "null";
  return typeof value;
}


function bodyShape(bodyText) {
  let parsed;
  try {
    parsed = typeof bodyText === "string" ? JSON.parse(bodyText) : bodyText;
  } catch {
    return { format: "unparsed", byte_count: Buffer.byteLength(String(bodyText || "")) };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { format: "json", root_type: valueType(parsed) };
  }
  const topTypes = Object.fromEntries(Object.entries(parsed).map(([key, value]) => [key, valueType(value)]));
  const firstSku = Array.isArray(parsed.sku_info) ? parsed.sku_info[0] : null;
  const firstSpec = Array.isArray(parsed.spec_name) ? parsed.spec_name[0] : null;
  return {
    format: "json",
    field_names_in_order: Object.keys(parsed),
    field_types: topTypes,
    sku_count: Array.isArray(parsed.sku_info) ? parsed.sku_info.length : 0,
    sku_field_types: firstSku && typeof firstSku === "object"
      ? Object.fromEntries(Object.entries(firstSku).map(([key, value]) => [key, valueType(value)]))
      : {},
    specification_count: Array.isArray(parsed.spec_name) ? parsed.spec_name.length : 0,
    specification_field_types: firstSpec && typeof firstSpec === "object"
      ? Object.fromEntries(Object.entries(firstSpec).map(([key, value]) => [key, valueType(value)]))
      : {},
    auth: {
      token_body_field_present: Object.hasOwn(parsed, "token"),
      secret_body_field_present: Object.hasOwn(parsed, "secret"),
      token_value_included: false,
      secret_value_included: false,
    },
    tenant_marker_presence: Object.fromEntries(
      ["pro_id", "tenant_id", "shop_id", "merchant_id"].map((key) => [key, Object.hasOwn(parsed, key)]),
    ),
  };
}


function isMikihousePayload(bodyText) {
  try {
    const payload = JSON.parse(String(bodyText || ""));
    const skuCodes = Array.isArray(payload.sku_info)
      ? payload.sku_info.map((row) => String(row?.sku_code || ""))
      : [];
    return (
      String(payload.good_type || "") === "294884"
      || String(payload.supplier || "").toUpperCase() === "MIKIHOUSE"
      || String(payload.description || "").includes("source_product_id=MIKIHOUSE")
      || skuCodes.some((code) => code.startsWith("MIKI-"))
    );
  } catch {
    return false;
  }
}


function shapeRequest(request) {
  return {
    method: request?.method || null,
    url: safeUrlShape(request?.url),
    headers: headerShape(request?.headers),
    content_type: normalizeHeaders(request?.headers)["content-type"] || null,
    body: bodyShape(request?.post_data || request?.body || ""),
  };
}


function shapeMikihouse(reportPath, diffPath) {
  const report = readJson(reportPath);
  const diff = readJson(diffPath);
  const request = report?.create_response?._native_request || {};
  const normalizeType = (value) => ({
    str: "string",
    list: "array",
    dict: "object",
    int: "number",
    float: "number",
    bool: "boolean",
    NoneType: "null",
  })[value] || value || "unknown";
  const fields = Object.fromEntries(
    (diff.field_comparison || []).map((row) => [row.field, normalizeType(row.minimal_probe?.type)]),
  );
  const skuFields = Object.fromEntries(
    (diff.sku_field_comparison || []).map((row) => [row.field, normalizeType(row.minimal_probe?.type)]),
  );
  const specFields = Object.fromEntries(
    (diff.specification_field_comparison || []).map((row) => [row.field, normalizeType(row.minimal_probe?.type)]),
  );
  fields.secret = "string";
  fields.token = "string";
  const urlShape = safeUrlShape(request.endpoint || "");
  if (request.serialization?.token_also_in_query) {
    urlShape.query_parameter_names = ["token"];
  }
  return {
    method: request.method || null,
    url: urlShape,
    headers: headerShape(request.headers || {}),
    content_type: request.content_type || null,
    body: {
      format: "json",
      field_names_in_order: [
        "secret",
        "token",
        ...(request.serialization?.body_key_order_after_auth || []),
      ],
      field_types: fields,
      sku_field_types: skuFields,
      specification_field_types: specFields,
      auth: {
        token_body_field_present: true,
        secret_body_field_present: true,
        token_value_included: false,
        secret_value_included: false,
      },
      tenant_marker_presence: {
        pro_id: false,
        tenant_id: false,
        shop_id: false,
        merchant_id: false,
      },
    },
  };
}


function setDifference(left, right) {
  const rightSet = new Set(right || []);
  return (left || []).filter((value) => !rightSet.has(value)).sort();
}


function typeDifferences(left, right) {
  const keys = [...new Set([...Object.keys(left || {}), ...Object.keys(right || {})])].sort();
  return keys
    .filter((key) => left?.[key] !== right?.[key])
    .map((key) => ({ field: key, left_type: left?.[key] ?? null, right_type: right?.[key] ?? null }));
}


function hashDifferences(left, right) {
  const keys = [...new Set([...Object.keys(left || {}), ...Object.keys(right || {})])].sort();
  return keys.filter((key) => left?.[key] !== right?.[key]);
}


function compareShapes(left, right, leftLabel, rightLabel) {
  if (!left || !right) return { available: false, left: leftLabel, right: rightLabel };
  return {
    available: true,
    left: leftLabel,
    right: rightLabel,
    method_equal: left.method === right.method,
    endpoint_path_equal: left.url?.path === right.url?.path,
    query_names_only_in_left: setDifference(left.url?.query_parameter_names, right.url?.query_parameter_names),
    query_names_only_in_right: setDifference(right.url?.query_parameter_names, left.url?.query_parameter_names),
    header_names_only_in_left: setDifference(left.headers?.names, right.headers?.names),
    header_names_only_in_right: setDifference(right.headers?.names, left.headers?.names),
    public_header_value_hash_differences: hashDifferences(
      left.headers?.public_value_sha256,
      right.headers?.public_value_sha256,
    ),
    cookie_presence: { left: Boolean(left.headers?.cookie_present), right: Boolean(right.headers?.cookie_present) },
    auth_presence: {
      left: left.body?.auth || null,
      right: right.body?.auth || null,
    },
    tenant_marker_presence: {
      left: left.body?.tenant_marker_presence || null,
      right: right.body?.tenant_marker_presence || null,
    },
    content_type_equal: left.content_type === right.content_type,
    body_field_order_equal: JSON.stringify(left.body?.field_names_in_order || [])
      === JSON.stringify(right.body?.field_names_in_order || []),
    body_fields_only_in_left: setDifference(left.body?.field_names_in_order, right.body?.field_names_in_order),
    body_fields_only_in_right: setDifference(right.body?.field_names_in_order, left.body?.field_names_in_order),
    body_type_differences: typeDifferences(left.body?.field_types, right.body?.field_types),
    sku_type_differences: typeDifferences(left.body?.sku_field_types, right.body?.sku_field_types),
    specification_type_differences: typeDifferences(
      left.body?.specification_field_types,
      right.body?.specification_field_types,
    ),
  };
}


function sanitizeExactCapture(raw) {
  const mergedHeaders = {
    ...(raw.playwright_request?.headers || {}),
    ...(raw.cdp_request_extra_info?.headers || {}),
  };
  return {
    captured_at: raw.captured_at,
    private_evidence_sha256: raw.private_evidence_sha256,
    request: shapeRequest({
      method: raw.playwright_request?.method,
      url: raw.playwright_request?.url,
      headers: mergedHeaders,
      post_data: raw.playwright_request?.post_data,
    }),
    response: {
      http_status: raw.response?.status,
      content_type: normalizeHeaders(raw.response?.headers)["content-type"] || null,
      body_sha256: raw.response?.body_text ? sha256(raw.response.body_text) : null,
      body_value_included: false,
    },
    readback: {
      product_id: raw.readback?.product_id || null,
      sku_ids: raw.readback?.sku_ids || [],
      goods_index_unique: Boolean(raw.readback?.goods_index_unique),
      get_format_info_verified: Boolean(raw.readback?.get_format_info_verified),
    },
    sensitive_values_included: false,
  };
}


function correctionConclusion(exact, historical, miki) {
  if (!exact) {
    return {
      state: "WAITING_FOR_BROWSER_EXACT_CAPTURE",
      proven_root_cause: null,
      next_fix: "capture a current successful native save before changing the MIKIHOUSE writer",
    };
  }
  const exactVsMiki = compareShapes(exact, miki, "browser_exact", "mikihouse_previous");
  const fixes = [];
  if (exact.headers?.cookie_present && !miki.headers?.cookie_present) fixes.push("LOAD_COOKIE_FROM_PRIVATE_RUNTIME_ONLY");
  if (exactVsMiki.header_names_only_in_left.length) fixes.push("ALIGN_BROWSER_REQUEST_HEADER_SET");
  if (exactVsMiki.public_header_value_hash_differences.length) fixes.push("ALIGN_BROWSER_PUBLIC_HEADER_VALUES");
  if (exactVsMiki.query_names_only_in_left.length || exactVsMiki.query_names_only_in_right.length) fixes.push("ALIGN_ENDPOINT_QUERY_SHAPE");
  if (
    exactVsMiki.body_fields_only_in_left.length
    || exactVsMiki.body_fields_only_in_right.length
    || !exactVsMiki.body_field_order_equal
  ) fixes.push("ALIGN_BODY_FIELD_SET_AND_ORDER");
  if (exactVsMiki.body_type_differences.length || exactVsMiki.sku_type_differences.length || exactVsMiki.specification_type_differences.length) fixes.push("ALIGN_BODY_VALUE_TYPES");
  if (!fixes.length) fixes.push("NO_TRANSPORT_DIFFERENCE_PROVEN_REVIEW_SERVER_BUSINESS_VALIDATION");
  return {
    state: exact.readback?.get_format_info_verified ? "BROWSER_EXACT_CAPTURE_VERIFIED" : "CAPTURED_BUT_READBACK_NOT_VERIFIED",
    fixes,
    historical_comparison_available: Boolean(historical),
    no_automatic_product_write_authorized: true,
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


async function checkCdp(cdpUrl) {
  try {
    const response = await fetch(`${String(cdpUrl).replace(/\/$/, "")}/json/version`, {
      signal: AbortSignal.timeout(1500),
    });
    if (!response.ok) return { available: false, http_status: response.status };
    const value = await response.json();
    return {
      available: true,
      browser: value.Browser || null,
      protocol_version: value["Protocol-Version"] || null,
      websocket_url_present: Boolean(value.webSocketDebuggerUrl),
    };
  } catch (error) {
    return { available: false, error_type: error?.name || "Error" };
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


function collectSkuIds(value, expectedCode, output = new Set()) {
  if (Array.isArray(value)) {
    for (const item of value) collectSkuIds(item, expectedCode, output);
  } else if (value && typeof value === "object") {
    const code = String(value.sku_code || value.code || "");
    if (!expectedCode || code === expectedCode) {
      const id = value.sku_id ?? value.goods_sku_id ?? value.good_sku_id;
      if (id !== undefined && id !== null && String(id)) output.add(String(id));
    }
    for (const child of Object.values(value)) collectSkuIds(child, expectedCode, output);
  }
  return [...output];
}


async function browserReadback(page, createRequest) {
  const payload = JSON.parse(createRequest.post_data);
  const token = String(payload.token || "");
  const secret = String(payload.secret || "");
  const goodName = String(payload.good_name || "");
  const skuCode = String(payload.sku_info?.[0]?.sku_code || "");
  if (!token || !secret || !goodName || !skuCode) {
    throw new Error("captured create body lacks token, secret, good_name, or sku_code");
  }
  const prefix = String(createRequest.url).split("/shopapi/")[0];
  const listUrl = `${prefix}/shopapi/Goods/index&token=${encodeURIComponent(token)}`;
  const listResult = await page.evaluate(async ({ url, tokenValue, secretValue, name, code }) => {
    const form = new URLSearchParams({
      secret: secretValue,
      token: tokenValue,
      page: "1",
      page_size: "100",
      good_type: "",
      father_type: "",
      recommend: "",
      good_name: name,
      good_code: code,
      push: "",
      status: "",
      update_start_time: "",
      update_end_time: "",
      create_start_time: "",
      create_end_time: "",
      group_id: "",
    });
    const response = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: {
        Accept: "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      },
      body: form.toString(),
    });
    return { status: response.status, json: await response.json() };
  }, { url: listUrl, tokenValue: token, secretValue: secret, name: goodName, code: skuCode });
  const exactRows = responseRows(listResult.json).filter((row) => String(row.good_name || "") === goodName);
  const productIds = [...new Set(exactRows.map((row) => row.id ?? row.good_id ?? row.goods_id).filter(Boolean).map(String))];
  if (productIds.length !== 1) {
    return { goods_index_unique: false, product_ids: productIds, sku_ids: [], get_format_info_verified: false, list_result: listResult };
  }
  const productId = productIds[0];
  const detailUrl = `${prefix}/shopapi/goods/getFormatInfo&token=${encodeURIComponent(token)}`;
  const detailResult = await page.evaluate(async ({ url, tokenValue, secretValue, id }) => {
    const form = new URLSearchParams({ secret: secretValue, token: tokenValue, id });
    const response = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: {
        Accept: "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      },
      body: form.toString(),
    });
    return { status: response.status, json: await response.json() };
  }, { url: detailUrl, tokenValue: token, secretValue: secret, id: productId });
  const skuIds = collectSkuIds(detailResult.json, skuCode);
  return {
    goods_index_unique: true,
    product_id: productId,
    sku_ids: skuIds,
    get_format_info_verified: skuIds.length === 1,
    list_result: listResult,
    detail_result: detailResult,
  };
}


async function selectPage(context, startUrl) {
  const pages = context.pages();
  let page = pages.find((candidate) => candidate.url().includes("shijiu.wfcorp.cn"));
  if (!page) page = pages[0] || await context.newPage();
  if (startUrl && !page.url().includes("shijiu.wfcorp.cn")) await page.goto(startUrl);
  await page.bringToFront();
  return page;
}


async function captureOneHumanSave(args, playwright) {
  const privateDir = assertPrivatePath(args.privateDir);
  fs.mkdirSync(privateDir, { recursive: true, mode: 0o700 });
  let context;
  let browser;
  let ownsContext = false;
  if (args.launchPrivateProfile) {
    const profileDir = path.join(privateDir, "chrome-profile");
    context = await playwright.chromium.launchPersistentContext(profileDir, {
      channel: "chrome",
      headless: false,
    });
    ownsContext = true;
  } else {
    browser = await playwright.chromium.connectOverCDP(args.cdpUrl);
    context = browser.contexts()[0];
    if (!context) throw new Error("CDP browser exposes no context");
  }
  try {
    const page = await selectPage(
      context,
      args.startUrl || "https://shijiu.wfcorp.cn/",
    );
    const cdp = await context.newCDPSession(page);
    await cdp.send("Network.enable");
    const targetRequestIds = new Map();
    const extraInfoById = new Map();
    cdp.on("Network.requestWillBeSent", (event) => {
      if (event.request?.url?.includes(TARGET_FRAGMENT)) targetRequestIds.set(event.requestId, event);
    });
    cdp.on("Network.requestWillBeSentExtraInfo", (event) => {
      extraInfoById.set(event.requestId, event);
    });
    let rejectMikihouse;
    const mikihouseGuard = new Promise((_, reject) => {
      rejectMikihouse = reject;
    });
    await page.route(`**${TARGET_FRAGMENT}**`, async (route) => {
      if (isMikihousePayload(route.request().postData())) {
        await route.abort("blockedbyclient");
        rejectMikihouse(new Error("MIKIHOUSE create payload was blocked before transmission"));
        return;
      }
      await route.continue();
    });
    process.stdout.write(
      "Browser capture armed. Log in if needed, manually add exactly one non-MIKIHOUSE disposable test product, then click Save once.\n",
    );
    const response = await Promise.race([
      page.waitForResponse(
        (candidate) => candidate.url().includes(TARGET_FRAGMENT),
        { timeout: args.timeoutMs },
      ),
      mikihouseGuard,
    ]);
    const request = response.request();
    const requestHeaders = await request.allHeaders();
    const responseHeaders = await response.allHeaders();
    const responseBody = await response.text();
    await page.waitForTimeout(2500);
    const cdpPair = [...targetRequestIds.entries()].find(([, event]) => event.request.url === request.url());
    const requestId = cdpPair?.[0];
    const raw = {
      schema_version: 1,
      captured_at: new Date().toISOString(),
      mode: "HUMAN_NATIVE_SAVE_CAPTURE",
      playwright_request: {
        method: request.method(),
        url: request.url(),
        resource_type: request.resourceType(),
        headers: requestHeaders,
        post_data: request.postData() || "",
      },
      cdp_request_will_be_sent: cdpPair?.[1] || null,
      cdp_request_extra_info: requestId ? extraInfoById.get(requestId) || null : null,
      response: {
        status: response.status(),
        headers: responseHeaders,
        body_text: responseBody,
      },
      readback: { state: "PENDING" },
    };
    const timestamp = raw.captured_at.replace(/[:.]/g, "-");
    const rawPath = path.join(privateDir, `shijiu-browser-exact-${timestamp}.private.json`);
    writeJsonSecure(rawPath, raw);
    try {
      raw.readback = {
        state: "COMPLETED",
        ...(await browserReadback(page, raw.playwright_request)),
      };
    } catch (error) {
      raw.readback = {
        state: "FAILED",
        error: { type: error?.name || "Error", message: error?.message || String(error) },
      };
    }
    writeJsonSecure(rawPath, raw);
    const privateFileSha256 = sha256(fs.readFileSync(rawPath));
    return { raw, rawPath, privateFileSha256 };
  } finally {
    if (ownsContext) await context.close().catch(() => {});
  }
}


function historicalShape(filePath) {
  if (!filePath || !fs.existsSync(filePath)) return null;
  const value = readJson(filePath);
  return shapeRequest({
    method: value.method,
    url: value.url,
    headers: value.headers,
    post_data: value.post_data,
  });
}


function buildReport(args, readiness, exactRaw = null) {
  const historical = historicalShape(args.historicalWawuTemplate);
  const miki = shapeMikihouse(args.currentMikiReport, args.currentMikiDiff);
  const exact = exactRaw ? sanitizeExactCapture(exactRaw) : null;
  const exactRequest = exact?.request || null;
  const state = exact
    ? (exact.readback.get_format_info_verified ? "BROWSER_EXACT_CAPTURE_VERIFIED" : "CAPTURED_BUT_READBACK_NOT_VERIFIED")
    : (readiness.playwright?.available && (readiness.cdp?.available || readiness.launch_private_profile_requested)
      ? "READY_FOR_ONE_HUMAN_NATIVE_SAVE_CAPTURE"
      : "BLOCKED_EXISTING_CHROME_NOT_ATTACHABLE");
  return {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    mode: args.mode,
    source: "MIKIHOUSE",
    target: "SHIJIU",
    state,
    safety: {
      mikihouse_product_write_requests: 0,
      mikihouse_payload_guard: "ABORT_BEFORE_TRANSMISSION",
      automatic_test_product_write_requests: 0,
      human_native_save_only: true,
      token_values_included: false,
      secret_values_included: false,
      cookie_values_included: false,
      body_values_included: false,
      private_capture_inside_git: false,
    },
    readiness,
    current_capture: exact,
    comparisons: {
      browser_exact_vs_historical_wawu: compareShapes(exactRequest, historical, "browser_exact", "historical_wawu"),
      browser_exact_vs_previous_mikihouse: compareShapes(exactRequest, miki, "browser_exact", "previous_mikihouse"),
      historical_wawu_vs_previous_mikihouse: compareShapes(historical, miki, "historical_wawu", "previous_mikihouse"),
    },
    conclusion: correctionConclusion(exactRequest ? { ...exactRequest, readback: exact.readback } : null, historical, miki),
    minimum_human_steps: exact ? [] : [
      "Install/enable the ChatGPT Chrome extension if Codex must inspect the existing Chrome tab; current Chrome cannot be attached through that bridge.",
      "Preferred standalone path: run capture mode with --launch-private-profile and a --private-dir outside Git, then log in to Shijiu in the opened dedicated Chrome profile.",
      "Alternative CDP path: start a dedicated non-default Chrome user-data directory with remote debugging, then pass its local --cdp-url. Chrome 152 cannot retroactively expose the already-running default profile.",
      "In the armed browser, manually create exactly one non-MIKIHOUSE disposable test product outside category 294884 and click Save once. The helper never fills or clicks Save and aborts MIKIHOUSE-shaped payloads before transmission.",
      "Keep the raw private JSON/profile outside Git. Commit only this sanitized report after checking unique Goods/index product_id and getFormatInfo sku_id.",
    ],
  };
}


async function selfTest() {
  const headers = headerShape({ Cookie: "private-cookie", Accept: "application/json" });
  const body = bodyShape(JSON.stringify({ secret: "private-secret", token: "private-token", state: "1", sku_info: [{ sku_price: "10.00" }] }));
  const serialized = JSON.stringify({ headers, body });
  if (serialized.includes("private-cookie") || serialized.includes("private-secret") || serialized.includes("private-token")) {
    throw new Error("sensitive self-test value leaked");
  }
  if (!headers.cookie_present || body.field_types.state !== "string") throw new Error("shape self-test failed");
  if (!isMikihousePayload(JSON.stringify({ good_type: 294884, sku_info: [] }))) throw new Error("MIKIHOUSE guard self-test failed");
  if (isMikihousePayload(JSON.stringify({ good_type: 123, sku_info: [{ sku_code: "TEST-1" }] }))) throw new Error("non-MIKIHOUSE guard self-test failed");
  process.stdout.write(`${JSON.stringify({ status: "PASS", sensitive_values_included: false })}\n`);
}


async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selfTest) return selfTest();
  if (!["preflight", "capture"].includes(args.mode)) throw new Error("--mode must be preflight or capture");
  const privateDir = assertPrivatePath(args.privateDir);
  let playwright;
  let playwrightStatus;
  try {
    playwright = await loadPlaywright(args.playwrightRoot);
    playwrightStatus = { available: true };
  } catch (error) {
    playwrightStatus = { available: false, error_type: error?.code || error?.name || "Error" };
  }
  const cdp = await checkCdp(args.cdpUrl);
  const readiness = {
    playwright: playwrightStatus,
    cdp,
    chrome_extension_status: args.chromeExtensionStatus,
    private_directory: {
      configured: true,
      outside_git_worktree: !isInside(REPO_ROOT, privateDir),
      value_in_report: false,
    },
    existing_chrome_attachable: Boolean(cdp.available),
    launch_private_profile_requested: Boolean(args.launchPrivateProfile),
  };
  if (args.mode === "preflight") {
    const report = buildReport(args, readiness);
    writeJsonSecure(path.resolve(args.sanitizedReport), report, 0o644);
    process.stdout.write(`${JSON.stringify({ status: report.state, shijiu_requests: 0, report: args.sanitizedReport })}\n`);
    return;
  }
  if (args.confirmCapture !== CAPTURE_CONFIRMATION) {
    throw new Error(`capture mode requires --confirm-capture ${CAPTURE_CONFIRMATION}`);
  }
  if (!playwright) throw new Error("Playwright is unavailable; run npm install or pass --playwright-root");
  if (!args.launchPrivateProfile && !cdp.available) {
    const report = buildReport(args, readiness);
    writeJsonSecure(path.resolve(args.sanitizedReport), report, 0o644);
    throw new Error("existing Chrome is not attachable; no capture or Shijiu request was started");
  }
  const result = await captureOneHumanSave(args, playwright);
  result.raw.private_evidence_sha256 = result.privateFileSha256;
  const report = buildReport(args, readiness, result.raw);
  writeJsonSecure(path.resolve(args.sanitizedReport), report, 0o644);
  process.stdout.write(`${JSON.stringify({ status: report.state, private_file_written: true, sanitized_report: args.sanitizedReport })}\n`);
}


main().catch((error) => {
  process.stderr.write(`${error?.name || "Error"}: ${error?.message || String(error)}\n`);
  process.exitCode = 2;
});
