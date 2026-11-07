/**
 * Which agent server to use: the one on this computer, or RupeeRail's hosted one if the person chooses it.
 * The hosted server is open for the demo, so there's no sign-in here.
 */

const LOCAL = "http://127.0.0.1:8790";
const HOSTED = "https://rupee-rail.indiasouthcentral.cloudapp.azure.com/agent-api"; // /agent-api is in apps/web/deploy/Caddyfile

export interface Connection {
  server: string;
  socket: string;
  hosted: boolean;
}

const socketUrl = (server: string) => server.replace(/^http/, "ws");

export async function getConnection(): Promise<Connection> {
  const { useHosted, hostedServer } = await chrome.storage.local.get(["useHosted", "hostedServer"]) as { useHosted?: boolean; hostedServer?: string };
  if (!useHosted) return { server: LOCAL, socket: socketUrl(LOCAL), hosted: false };
  const server = hostedServer ?? HOSTED; // only the browser tests set hostedServer, which the panel offers no way to change
  return { server, socket: socketUrl(server), hosted: true };
}

export const useHostedServer = () => chrome.storage.local.set({ useHosted: true });
export const useLocalServer = () => chrome.storage.local.remove("useHosted");
