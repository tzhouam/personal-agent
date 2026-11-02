import { EnvHttpProxyAgent, install, setGlobalDispatcher } from "undici";

/** Mirror Pi CLI proxy initialization, which createAgentSession does not run. */
export function configureHttpProxy() {
  if (process.env.NODE_USE_ENV_PROXY === "0") return undefined;
  if (!["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"].some((name) => process.env[name])) return undefined;
  const dispatcher = new EnvHttpProxyAgent({
    proxyTunnel: true,
    allowH2: false,
    bodyTimeout: 300000,
    headersTimeout: 300000,
    connect: { autoSelectFamilyAttemptTimeout: 2000 },
  });
  // Keep fetch and its dispatcher on the same implementation across supported Node releases.
  // The agent reads NO_PROXY and inherits Node's configured CA trust; TLS remains verified.
  setGlobalDispatcher(dispatcher);
  install();
  return dispatcher;
}
