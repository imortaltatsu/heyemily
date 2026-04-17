const API = "/api";

export type TokenResponse = { access_token: string; token_type: string };

export async function authChallenge(address: string): Promise<{ message: string }> {
  const r = await fetch(`${API}/auth/challenge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function authVerify(
  address: string,
  message: string,
  signature: string,
): Promise<TokenResponse> {
  const r = await fetch(`${API}/auth/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address, message, signature }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export function authHeaders(token: string) {
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

export async function me(token: string) {
  const r = await fetch(`${API}/auth/me`, { headers: authHeaders(token) });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<{
    id: string;
    wallet_address: string;
    created_at: string;
    trading_custodial_address?: string | null;
  }>;
}

export async function listSessions(token: string) {
  const r = await fetch(`${API}/bots/sessions`, { headers: authHeaders(token) });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function createSession(token: string, name: string, config: object) {
  const r = await fetch(`${API}/bots/sessions`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ name, config }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function uploadKey(token: string, sessionId: string, privateKey: string) {
  const r = await fetch(`${API}/bots/sessions/${sessionId}/credentials`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ private_key: privateKey }),
  });
  if (!r.ok) throw new Error(await r.text());
  return (await r.json()) as { status: string; custodial_address?: string };
}

export async function spawnCustodialWallet(
  token: string,
  sessionId: string,
): Promise<{ address: string; session_id: string }> {
  const r = await fetch(`${API}/bots/sessions/${sessionId}/custodial-wallet`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function exportPrivateKey(
  token: string,
  sessionId: string,
): Promise<{ private_key: string; custodial_address: string; session_id: string }> {
  const r = await fetch(`${API}/bots/sessions/${sessionId}/export-private-key`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export type HyperliquidMarginSummary = {
  account_value: string;
  withdrawable: string;
  total_margin_used: string;
  total_ntl_pos: string;
  total_raw_usd: string;
  open_positions: number;
};

export type SpotBalanceRow = { coin: string; total: string; hold: string; entry_ntl: string };

export async function getHyperliquidBalance(token: string, sessionId: string) {
  const r = await fetch(`${API}/bots/sessions/${sessionId}/hyperliquid-balance`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<{
    address: string;
    testnet: boolean;
    margin: HyperliquidMarginSummary;
    spot_balances: SpotBalanceRow[];
  }>;
}

export type UsdClassTransferResponse = {
  amount: number;
  to_perp: boolean;
  hyperliquid: Record<string, unknown>;
};

/** Move USDC between HL spot and perp (`usdClassTransfer`). Omit amount + to_perp true = all free spot USDC → perps. */
export async function usdClassTransfer(
  token: string,
  sessionId: string,
  body: { to_perp?: boolean; amount?: number | null } = {},
): Promise<UsdClassTransferResponse> {
  const r = await fetch(`${API}/bots/sessions/${sessionId}/usd-class-transfer`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<UsdClassTransferResponse>;
}

export async function getHyperliquidAccountSnapshot(token: string, sessionId: string) {
  const r = await fetch(`${API}/bots/sessions/${sessionId}/hyperliquid-account`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<{
    address: string;
    testnet: boolean;
    margin: HyperliquidMarginSummary;
    clearinghouse_state: Record<string, unknown>;
    spot_clearinghouse_state: Record<string, unknown>;
    spot_balances: SpotBalanceRow[];
  }>;
}

export type SessionStartResponse = {
  /** running if worker spawned or auto-spawn disabled; stopped if spawn threw */
  status: string;
  worker_token: string;
  session_id: string;
  worker_spawn?: Record<string, unknown> | null;
  worker_spawn_error?: string | null;
  local_worker_autostart?: boolean;
};

export async function startSession(token: string, sessionId: string): Promise<SessionStartResponse> {
  const r = await fetch(`${API}/bots/sessions/${sessionId}/start`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<SessionStartResponse>;
}

export type SessionStopResponse = { status: string; worker?: Record<string, unknown> | null };

export async function stopSession(token: string, sessionId: string): Promise<SessionStopResponse> {
  const r = await fetch(`${API}/bots/sessions/${sessionId}/stop`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<SessionStopResponse>;
}

export type SessionCloseAllResponse = {
  status: string;
  session_id: string;
  cancelled_orders: number;
  attempted_position_closes: number;
  failed_order_cancels: number;
  failed_position_closes: number;
  details?: Record<string, unknown>;
};

export async function closeAllOrdersAndPositions(
  token: string,
  sessionId: string,
): Promise<SessionCloseAllResponse> {
  const r = await fetch(`${API}/bots/sessions/${sessionId}/close-all`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<SessionCloseAllResponse>;
}

export type SessionDeleteResponse = { status: string; session_id: string; wallet_conserved: boolean };

export async function deleteSession(token: string, sessionId: string): Promise<SessionDeleteResponse> {
  const r = await fetch(`${API}/bots/sessions/${sessionId}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<SessionDeleteResponse>;
}

/** Buffered telemetry for this session (same store as the WebSocket replay). */
export async function getSessionEvents(
  token: string,
  sessionId: string,
  limit = 500,
): Promise<Array<{ kind: string; ts: number; symbol?: string | null; data?: object }>> {
  const r = await fetch(`${API}/bots/sessions/${sessionId}/events?limit=${limit}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export function openTelemetryWs(token: string, sessionId: string, onMessage: (d: unknown) => void) {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host;
  const url = `${proto}//${host}/api/bots/sessions/${sessionId}/stream?token=${encodeURIComponent(token)}`;
  const ws = new WebSocket(url);
  ws.onmessage = (ev) => {
    try {
      onMessage(JSON.parse(ev.data as string));
    } catch {
      onMessage(ev.data);
    }
  };
  return ws;
}
