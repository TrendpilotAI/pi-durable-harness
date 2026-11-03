// Fetch shim loaded into the harness child via NODE_OPTIONS=--import.
//
// The child runs under a seccomp filter that denies socket(AF_INET/AF_INET6),
// so the only way out is the service's unix-socket LLM proxy. This replaces
// globalThis.fetch with undici's fetch bound to an Agent that connects over
// that socket, rewriting https: -> http: so no TLS is attempted on it.
//
// Two things make this robust against pi's own startup:
//  - pi's configureHttpDispatcher() calls undici.install() on its BUNDLED
//    undici copy, which assigns globalThis.fetch. The property below is an
//    accessor whose setter swallows that assignment (observed 3x per start).
//  - The dispatcher is passed per request, so pi's setGlobalDispatcher() on
//    the shared globalThis symbol is irrelevant to our fetch.
//
// The undici here is /opt/harness's own pinned copy (package.json), never
// Node's bundled one: mixing copies breaks dispatcher typing.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const undici = require("undici");

const socketPath = process.env.LLM_PROXY_SOCKET;
if (!socketPath) {
  process.stderr.write("[harness-shim] LLM_PROXY_SOCKET not set; refusing to start\n");
  process.exit(97);
}

const agent = new undici.Agent({ connect: { socketPath } });
const debug = process.env.HARNESS_SHIM_DEBUG === "1";

function rewrite(input) {
  const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const url = new URL(raw);
  if (url.protocol === "https:") url.protocol = "http:";
  return url.toString();
}

function proxiedFetch(input, init = {}) {
  const url = rewrite(input);
  if (debug) process.stderr.write(`[harness-shim] ${init.method ?? "GET"} ${url}\n`);
  return undici.fetch(url, { ...init, dispatcher: agent });
}

Object.defineProperty(globalThis, "fetch", {
  configurable: true,
  enumerable: true,
  get() {
    return proxiedFetch;
  },
  set() {
    if (debug) process.stderr.write("[harness-shim] ignored globalThis.fetch reassignment\n");
  },
});
