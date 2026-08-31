const origin = process.env.CLERKSAN_API_ORIGIN ?? "http://127.0.0.1:8000";
if (origin !== "http://127.0.0.1:8000") {
  throw new Error("CLERKSAN_API_ORIGIN must remain a documented loopback API origin.");
}

const response = await fetch(`${origin}/openapi.json`);
if (!response.ok) throw new Error(`OpenAPI request failed with ${response.status}.`);
const document = await response.json();

function refName(schema) {
  const ref = schema?.$ref;
  const prefix = "#/components/schemas/";
  return typeof ref === "string" && ref.startsWith(prefix) ? ref.slice(prefix.length) : null;
}

function requestSchema(operation) {
  return operation?.requestBody?.content?.["application/json"]?.schema;
}

function responseSchema(operation, status) {
  return operation?.responses?.[String(status)]?.content?.["application/json"]?.schema;
}

function parameter(name, location, required) {
  return { name, location, required };
}

const pathParameter = (name) => parameter(name, "path", true);
const queryParameter = (name, required = false) => parameter(name, "query", required);

const legacyOperations = [
  ["post", "/documents"],
  ["get", "/documents/{document_id}/status"],
  ["get", "/documents/{document_id}/original"],
  ["get", "/review"],
  ["post", "/review/approve"],
  ["post", "/review/reject"],
  ["get", "/bills"],
  ["post", "/query"],
];

for (const [method, path] of legacyOperations) {
  if (!document.paths?.[path]?.[method]) {
    throw new Error(`OpenAPI contract is missing legacy ${method.toUpperCase()} ${path}.`);
  }
}

const contracts = [
  { method: "get", path: "/capabilities", response: { status: 200, ref: "CapabilityOut" } },
  {
    method: "get",
    path: "/intakes",
    response: { status: 200, arrayOf: "SourceIntakeDetail" },
    parameters: [queryParameter("limit")],
  },
  {
    method: "get",
    path: "/intakes/{intake_id}",
    response: { status: 200, ref: "SourceIntakeDetail" },
    parameters: [pathParameter("intake_id")],
  },
  {
    method: "post",
    path: "/intakes/{intake_id}/retry",
    requestRef: "SourceIntakeActionIn",
    response: { status: 202, ref: "ReprocessAccepted" },
    parameters: [pathParameter("intake_id")],
  },
  {
    method: "post",
    path: "/intakes/{intake_id}/reprocess",
    requestRef: "SourceIntakeActionIn",
    response: { status: 202, ref: "ReprocessAccepted" },
    parameters: [pathParameter("intake_id")],
  },
  {
    method: "get",
    path: "/documents/{document_id}/schema-descriptors",
    response: { status: 200, ref: "SchemaDescriptorsOut" },
    parameters: [pathParameter("document_id")],
  },
  {
    method: "get",
    path: "/documents/{document_id}/mappings",
    response: { status: 200, ref: "MappingsOut" },
    parameters: [pathParameter("document_id")],
  },
  {
    method: "post",
    path: "/documents/{document_id}/mappings",
    requestRef: "MappingCreateIn",
    response: { status: 201, ref: "MappingOut" },
    parameters: [pathParameter("document_id")],
  },
  {
    method: "post",
    path: "/documents/{document_id}/mapping-sets/preview",
    requestRef: "MappingSetDraftIn",
    response: { status: 200, ref: "MappingSetPreviewOut" },
    parameters: [pathParameter("document_id")],
  },
  {
    method: "post",
    path: "/documents/{document_id}/mapping-sets",
    requestRef: "MappingSetDraftIn",
    response: { status: 201, ref: "MappingSetOut" },
    parameters: [pathParameter("document_id")],
  },
  {
    method: "post",
    path: "/documents/{document_id}/mapping-sets/{mapping_set_id}/apply",
    requestRef: "MappingSetApplyIn",
    response: { status: 201, ref: "ExtractionBatchOut" },
    parameters: [pathParameter("document_id"), pathParameter("mapping_set_id")],
  },
  {
    method: "get",
    path: "/review/batches",
    response: { status: 200, ref: "ReviewBatchPageOut" },
    parameters: [queryParameter("limit"), queryParameter("offset"), queryParameter("lifecycle")],
  },
  {
    method: "get",
    path: "/review/batches/{batch_id}/candidates",
    response: { status: 200, ref: "ReviewCandidatePageOut" },
    parameters: [
      pathParameter("batch_id"),
      queryParameter("limit"),
      queryParameter("offset"),
      queryParameter("exceptions_only"),
    ],
  },
  {
    method: "post",
    path: "/review/batches/{batch_id}/decisions",
    requestRef: "ReviewBatchDecisionsIn",
    response: { status: 201, ref: "ReviewBatchDecisionResultOut" },
    parameters: [pathParameter("batch_id")],
  },
  {
    method: "get",
    path: "/review/batches/{batch_id}/activation-preview",
    response: { status: 200, ref: "ReviewActivationPreviewOut" },
    parameters: [pathParameter("batch_id")],
  },
  {
    method: "post",
    path: "/review/batches/{batch_id}/activate",
    requestRef: "ReviewBatchActivateIn",
    response: { status: 200, ref: "ReviewBatchActivationOut" },
    parameters: [pathParameter("batch_id")],
  },
  {
    method: "post",
    path: "/review/batches/{batch_id}/reject-and-reprocess",
    requestRef: "ReviewBatchRejectAndReprocessIn",
    response: { status: 202, ref: "ReviewBatchReprocessOut" },
    parameters: [pathParameter("batch_id")],
  },
  {
    method: "get",
    path: "/documents/{document_id}/sources/{source_file_id}/pdf-preview",
    response: { status: 200, ref: "PdfPreviewManifest" },
    parameters: [
      pathParameter("document_id"),
      pathParameter("source_file_id"),
      queryParameter("version", true),
      queryParameter("sha256", true),
    ],
  },
  {
    method: "get",
    path: "/documents/{document_id}/sources/{source_file_id}/pdf-preview/pages/{page_number}",
    response: { status: 200, mediaType: "image/png", binary: true },
    parameters: [
      pathParameter("document_id"),
      pathParameter("source_file_id"),
      pathParameter("page_number"),
      queryParameter("version", true),
      queryParameter("sha256", true),
    ],
  },
];

for (const contract of contracts) {
  const { method, path, requestRef, parameters = [], response: expectedResponse } = contract;
  const operation = document.paths?.[path]?.[method];
  const label = `${method.toUpperCase()} ${path}`;
  if (!operation) throw new Error(`OpenAPI contract is missing ${label}.`);

  if (requestRef && refName(requestSchema(operation)) !== requestRef) {
    throw new Error(`${label} request must reference ${requestRef}.`);
  }

  const declaredResponse = operation.responses?.[String(expectedResponse.status)];
  if (!declaredResponse) {
    throw new Error(`${label} must declare response ${expectedResponse.status}.`);
  }
  const schema = responseSchema(operation, expectedResponse.status);
  if (expectedResponse.ref && refName(schema) !== expectedResponse.ref) {
    throw new Error(`${label} response ${expectedResponse.status} must reference ${expectedResponse.ref}.`);
  }
  if (expectedResponse.arrayOf && refName(schema?.items) !== expectedResponse.arrayOf) {
    throw new Error(`${label} response ${expectedResponse.status} must be an array of ${expectedResponse.arrayOf}.`);
  }
  if (expectedResponse.mediaType) {
    const declaredContent = declaredResponse.content ?? {};
    if (!Object.hasOwn(declaredContent, expectedResponse.mediaType)) {
      throw new Error(`${label} response ${expectedResponse.status} must declare ${expectedResponse.mediaType} content.`);
    }
    const mediaSchema = declaredContent[expectedResponse.mediaType]?.schema;
    if (expectedResponse.binary && (mediaSchema.type !== "string" || mediaSchema.format !== "binary")) {
      throw new Error(`${label} response ${expectedResponse.status} ${expectedResponse.mediaType} content must be binary.`);
    }
  }

  for (const expectedParameter of parameters) {
    const declared = (operation.parameters ?? []).find(
      (item) => item.name === expectedParameter.name && item.in === expectedParameter.location,
    );
    if (!declared || Boolean(declared.required) !== expectedParameter.required) {
      const requirement = expectedParameter.required ? "required" : "optional";
      throw new Error(
        `${label} must declare ${expectedParameter.name} as an ${requirement} ${expectedParameter.location} parameter.`,
      );
    }
  }
}

process.stdout.write("OpenAPI method, schema, and parameter contract passed.\n");
