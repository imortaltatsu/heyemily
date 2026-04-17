import { useCallback, useEffect, useMemo, useState } from "react";
import type { HyperliquidMarginSummary, SpotBalanceRow } from "./api";
import {
  authChallenge,
  authVerify,
  createSession,
  closeAllOrdersAndPositions,
  deleteSession,
  exportPrivateKey,
  getHyperliquidAccountSnapshot,
  getHyperliquidBalance,
  listSessions,
  me,
  openTelemetryWs,
  spawnCustodialWallet,
  getSessionEvents,
  startSession,
  stopSession,
  uploadKey,
  usdClassTransfer,
} from "./api";
import { addressFromPrivateKey } from "./hlAddress";
import {
  connectBrowserWallet,
  connectWalletConnect,
  disconnectWallet,
  isWalletConnectConfigured,
} from "./wallet";

type RiskProfile = "conservative" | "balanced" | "aggressive";

const TOKEN_OPTIONS = ["BTC", "ETH", "SOL", "HYPE"] as const;

const RISK_PROFILES: Record<RiskProfile, { label: string; orderSizeUsd: number; maxOpenNotionalUsd: number }> = {
  conservative: { label: "Conservative", orderSizeUsd: 15, maxOpenNotionalUsd: 2500 },
  balanced: { label: "Balanced", orderSizeUsd: 15, maxOpenNotionalUsd: 7500 },
  aggressive: { label: "Aggressive", orderSizeUsd: 15, maxOpenNotionalUsd: 25000 },
};

function buildLiteConfig(
  symbol: string,
  testnet: boolean,
  profile: RiskProfile,
  orderSizeUsd: number,
  leverage: number,
): Record<string, unknown> {
  const p = RISK_PROFILES[profile];
  const safeOrderSizeUsd = Number.isFinite(orderSizeUsd) ? Math.max(10, orderSizeUsd) : p.orderSizeUsd;
  const safeLeverage = Number.isFinite(leverage) ? Math.max(1, Math.floor(leverage)) : 3;
  return {
    name: `lite_${symbol.toLowerCase()}_${profile}`,
    symbol,
    testnet,
    loop_interval_ms: 100,
    interval_buy_ms: 1000,
    interval_sell_ms: 1000,
    interval_buy_flat_only: false,
    hold_timeout_ms: 800,
    imbalance_threshold: 0.35,
    micro_gap_min_bps: 1.0,
    order_size_usd: safeOrderSizeUsd,
    leverage: safeLeverage,
    depth_levels: 5,
    cooldown_ms: 150,
    risk: {
      max_orders_per_second: 3,
      max_open_notional_usd: p.maxOpenNotionalUsd,
      max_position_per_symbol_usd: p.maxOpenNotionalUsd,
      max_consecutive_losses: 99,
      max_daily_realized_loss_usd: 100000,
      kill_switch: false,
    },
    telemetry: { buffer_max: 500, emit_tick_events: false },
  };
}

/** Legacy tab-scoped cache (migrated into localStorage). */
const LEGACY_TRADING_ADDR_SESSION_KEY = "hft_hl_trading_addresses";
/** Legacy global localStorage key before per-user namespacing. */
const LEGACY_TRADING_ADDR_LOCAL_KEY = "hft_hl_trading_addresses";

function tradingAddrStorageKey(userId: string): string {
  return `${LEGACY_TRADING_ADDR_LOCAL_KEY}:user:${userId}`;
}

function parseAddrMap(raw: string | null): Record<string, string> {
  if (!raw) return {};
  try {
    const o = JSON.parse(raw) as unknown;
    if (!o || typeof o !== "object") return {};
    return o as Record<string, string>;
  } catch {
    return {};
  }
}

function loadTradingAddressMap(userId: string | null): Record<string, string> {
  if (!userId) return {};
  try {
    const scoped = localStorage.getItem(tradingAddrStorageKey(userId));
    if (scoped) return parseAddrMap(scoped);
    const fromTab = sessionStorage.getItem(LEGACY_TRADING_ADDR_SESSION_KEY);
    if (fromTab) {
      localStorage.setItem(tradingAddrStorageKey(userId), fromTab);
      sessionStorage.removeItem(LEGACY_TRADING_ADDR_SESSION_KEY);
      return parseAddrMap(fromTab);
    }
    const oldGlobal = localStorage.getItem(LEGACY_TRADING_ADDR_LOCAL_KEY);
    if (oldGlobal) {
      try {
        const parsed = JSON.parse(oldGlobal) as unknown;
        if (parsed && typeof parsed === "object") {
          localStorage.setItem(tradingAddrStorageKey(userId), oldGlobal);
          localStorage.removeItem(LEGACY_TRADING_ADDR_LOCAL_KEY);
          return parsed as Record<string, string>;
        }
      } catch {
        /* ignore */
      }
    }
  } catch {
    return {};
  }
  return {};
}

function spotAvailable(total: string, hold: string): string {
  const a = Number(total) - Number(hold);
  if (!Number.isFinite(a)) return "—";
  return String(a);
}

function parseNumericLike(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function extractLivePnl(events: { kind: string; ts: number; data?: object }[]): number | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    const data = event.data as Record<string, unknown> | undefined;
    if (!data) continue;
    const candidates = [
      data.pnl_usd,
      data.pnl,
      data.realized_pnl,
      data.unrealized_pnl,
      data.total_pnl,
      data.totalPnlUsd,
    ];
    for (const candidate of candidates) {
      const n = parseNumericLike(candidate);
      if (n !== null) return n;
    }
  }
  return null;
}

export default function App() {
  const [signInBusy, setSignInBusy] = useState(false);
  const [token, setToken] = useState<string | null>(localStorage.getItem("hft_token"));
  const [user, setUser] = useState<{
    id: string;
    wallet_address: string;
    trading_custodial_address?: string | null;
  } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sessions, setSessions] = useState<
    { id: string; name: string; status: string; config: object; custodial_address?: string | null }[]
  >([]);
  const [sessionName, setSessionName] = useState("my_bot");
  const [tokenSymbol, setTokenSymbol] = useState<string>("BTC");
  const [market, setMarket] = useState<"testnet" | "mainnet">("testnet");
  const [riskProfile, setRiskProfile] = useState<RiskProfile>("balanced");
  const [orderSizeUsd, setOrderSizeUsd] = useState<number>(15);
  const [leverage, setLeverage] = useState<number>(3);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [pk, setPk] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [workerInfo, setWorkerInfo] = useState<string | null>(null);
  /** Secrets for clipboard only — never render JWT/shell in <pre>. */
  const [workerClip, setWorkerClip] = useState<{ jwt: string; shell: string } | null>(null);
  const [live, setLive] = useState<{ kind: string; ts: number; data?: object }[]>([]);
  const [tradingAddresses, setTradingAddresses] = useState<Record<string, string>>({});
  const [copyHint, setCopyHint] = useState<string | null>(null);
  const [provisionBusy, setProvisionBusy] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [hlSnapshot, setHlSnapshot] = useState<string | null>(null);
  const [hlMargin, setHlMargin] = useState<HyperliquidMarginSummary | null>(null);
  const [hlSpot, setHlSpot] = useState<SpotBalanceRow[]>([]);
  const [hlBusy, setHlBusy] = useState(false);
  const [hlBalanceBusy, setHlBalanceBusy] = useState(false);
  const [telemetryBusy, setTelemetryBusy] = useState(false);
  const [hlTransferBusy, setHlTransferBusy] = useState(false);
  const [closeAllBusy, setCloseAllBusy] = useState(false);
  const [perpToSpotAmount, setPerpToSpotAmount] = useState("");

  const refresh = useCallback(async () => {
    if (!token) return;
    const u = await me(token);
    setUser(u);
    const s = await listSessions(token);
    setSessions(s);
  }, [token]);

  useEffect(() => {
    if (token) {
      refresh().catch((e) => setErr(String(e)));
    }
  }, [token, refresh]);

  useEffect(() => {
    if (token && user?.id) {
      setTradingAddresses(loadTradingAddressMap(user.id));
    } else if (!token) {
      setTradingAddresses({});
    }
  }, [token, user?.id]);

  const logout = async () => {
    localStorage.removeItem("hft_token");
    sessionStorage.removeItem(LEGACY_TRADING_ADDR_SESSION_KEY);
    setToken(null);
    setUser(null);
    setSessions([]);
    setTradingAddresses({});
    setPk("");
    setSelected(null);
    setWorkerInfo(null);
    setWorkerClip(null);
    await disconnectWallet();
  };

  const completeWalletSignIn = async (session: { address: string; signMessage: (m: string) => Promise<string> }) => {
    const { message } = await authChallenge(session.address);
    const signature = await session.signMessage(message);
    const t = await authVerify(session.address, message, signature);
    localStorage.setItem("hft_token", t.access_token);
    setToken(t.access_token);
  };

  const onWalletConnect = async () => {
    setErr(null);
    setSignInBusy(true);
    try {
      await completeWalletSignIn(await connectWalletConnect());
    } catch (e) {
      setErr(String(e));
    } finally {
      setSignInBusy(false);
    }
  };

  const onBrowserWallet = async () => {
    setErr(null);
    setSignInBusy(true);
    try {
      await completeWalletSignIn(await connectBrowserWallet());
    } catch (e) {
      setErr(String(e));
    } finally {
      setSignInBusy(false);
    }
  };

  const onCreateSession = async () => {
    if (!token) return;
    setErr(null);
    try {
      const cfg = buildLiteConfig(tokenSymbol, market === "testnet", riskProfile, orderSizeUsd, leverage);
      const s = await createSession(token, sessionName, cfg);
      setSelected(s.id);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  };

  const onSaveKey = async () => {
    if (!token || !selected) return;
    setErr(null);
    const trimmed = pk.trim();
    const derived = addressFromPrivateKey(trimmed);
    try {
      const res = await uploadKey(token, selected, trimmed);
      const addr = res.custodial_address ?? derived;
      if (addr && user?.id) {
        setTradingAddresses((prev) => {
          const next = { ...prev, [selected]: addr };
          localStorage.setItem(tradingAddrStorageKey(user.id), JSON.stringify(next));
          return next;
        });
      }
      setPk("");
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  };

  const onProvisionCustodial = async () => {
    if (!token || !selected) return;
    setErr(null);
    setProvisionBusy(true);
    try {
      await spawnCustodialWallet(token, selected);
      await refresh();
      setHlSnapshot(null);
      setHlMargin(null);
      setHlSpot([]);
    } catch (e) {
      setErr(String(e));
    } finally {
      setProvisionBusy(false);
    }
  };

  const onExportPrivateKey = async () => {
    if (!token || !selected) return;
    setErr(null);
    setExportBusy(true);
    try {
      const out = await exportPrivateKey(token, selected);
      await copyWorkerSecret(out.private_key, "Private key copied");
    } catch (e) {
      setErr(String(e));
    } finally {
      setExportBusy(false);
    }
  };

  const onLookupHyperliquidBalance = async () => {
    if (!token || !selected) return;
    setErr(null);
    setHlBalanceBusy(true);
    try {
      const b = await getHyperliquidBalance(token, selected);
      setHlMargin(b.margin);
      setHlSpot(b.spot_balances ?? []);
    } catch (e) {
      setErr(String(e));
    } finally {
      setHlBalanceBusy(false);
    }
  };

  const onMoveSpotToPerp = async () => {
    if (!token || !selected) return;
    setErr(null);
    setHlTransferBusy(true);
    try {
      await usdClassTransfer(token, selected, { to_perp: true });
      await onLookupHyperliquidBalance();
    } catch (e) {
      setErr(String(e));
    } finally {
      setHlTransferBusy(false);
    }
  };

  const onMovePerpToSpot = async (amount: number) => {
    if (!token || !selected) return;
    if (!Number.isFinite(amount) || amount <= 0) {
      setErr("Enter a positive USDC amount to move from perps to spot.");
      return;
    }
    setErr(null);
    setHlTransferBusy(true);
    try {
      await usdClassTransfer(token, selected, { to_perp: false, amount });
      await onLookupHyperliquidBalance();
    } catch (e) {
      setErr(String(e));
    } finally {
      setHlTransferBusy(false);
    }
  };

  const onMovePerpToSpotMax = async () => {
    const w = hlMargin ? Number.parseFloat(hlMargin.withdrawable) : NaN;
    if (!Number.isFinite(w) || w <= 0) {
      setErr("Load balances first, or there is no withdrawable perp USDC to move.");
      return;
    }
    await onMovePerpToSpot(w);
  };

  const onMovePerpToSpotCustom = async () => {
    const w = Number.parseFloat(perpToSpotAmount);
    await onMovePerpToSpot(w);
  };

  const onFetchHyperliquidAccount = async () => {
    if (!token || !selected) return;
    setErr(null);
    setHlBusy(true);
    try {
      const snap = await getHyperliquidAccountSnapshot(token, selected);
      setHlMargin(snap.margin);
      setHlSpot(snap.spot_balances ?? []);
      setHlSnapshot(
        JSON.stringify({ perps: snap.clearinghouse_state, spot: snap.spot_clearinghouse_state }, null, 2),
      );
    } catch (e) {
      setErr(String(e));
    } finally {
      setHlBusy(false);
    }
  };

  const onStart = async () => {
    if (!token || !selected) return;
    setErr(null);
    try {
      const r = await startSession(token, selected);
      const ws = r.worker_spawn;
      const viteEnv = import.meta.env as { VITE_HFT_API_BASE?: string };
      const apiBase = (viteEnv.VITE_HFT_API_BASE && viteEnv.VITE_HFT_API_BASE.trim()) || "http://127.0.0.1:8000";
      const shell =
        `HFT_API_BASE=${apiBase} HFT_SESSION_ID=${selected} HFT_WORKER_TOKEN=${r.worker_token} ` +
        `uv run python src/run_lite_worker.py`;
      setWorkerClip({ jwt: r.worker_token, shell });
      if (ws?.spawned === true && typeof ws.pid === "number") {
        setWorkerInfo(
          `Lite worker auto-started on this machine (pid ${ws.pid}).\n\n` +
            `Use Copy worker JWT or Copy shell command — the token is not shown on screen (~24h TTL).\n\n` +
            `If you run the worker elsewhere, change HFT_API_BASE in the copied command (default ${apiBase}).\n\n` +
            `To disable auto-spawn on Start, set SPAWN_LOCAL_LITE_WORKER=false in the API .env file.`,
        );
      } else {
        const lines: string[] = [];
        if (r.local_worker_autostart === false) {
          lines.push("API auto-spawn is off (SPAWN_LOCAL_LITE_WORKER=false). Start the worker from a shell:");
        } else if (ws?.spawned === false) {
          lines.push(`Worker: ${typeof ws.detail === "string" ? ws.detail : "not started"}.`);
        }
        if (r.worker_spawn_error) {
          lines.push(`Auto-start error: ${r.worker_spawn_error}`);
        }
        if (!lines.length) {
          lines.push("Worker did not report a successful spawn. Run from repo root:");
        } else if (r.local_worker_autostart !== false) {
          lines.push("");
        }
        lines.push("Use Copy shell command (includes JWT). Disable auto-spawn: SPAWN_LOCAL_LITE_WORKER=false in API .env.");
        setWorkerInfo(lines.join("\n"));
      }
      if (r.worker_spawn_error) {
        setErr(r.worker_spawn_error);
      }
      await refresh();
    } catch (e) {
      setWorkerClip(null);
      setErr(String(e));
    }
  };

  const onStop = async () => {
    if (!token || !selected) return;
    setErr(null);
    try {
      await stopSession(token, selected);
      setWorkerClip(null);
      setWorkerInfo(null);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  };

  const onCloseAllOrders = async () => {
    if (!token || !selected) return;
    const ok = window.confirm(
      "Close all orders and flatten all open positions for this session now?\n\nThis sends reduce-only close orders for every non-zero position.",
    );
    if (!ok) return;
    setErr(null);
    setCloseAllBusy(true);
    try {
      const out = await closeAllOrdersAndPositions(token, selected);
      setWorkerInfo(
        `Close-all result:\n` +
          `Cancelled orders: ${out.cancelled_orders}\n` +
          `Position closes attempted: ${out.attempted_position_closes}\n` +
          `Failed order cancels: ${out.failed_order_cancels}\n` +
          `Failed position closes: ${out.failed_position_closes}`,
      );
      await onLookupHyperliquidBalance();
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setCloseAllBusy(false);
    }
  };

  const onDeleteSession = async (sessionId: string) => {
    if (!token) return;
    const ok = window.confirm(
      "Delete this bot session? This keeps your account-level custodial wallet shared across other bots.",
    );
    if (!ok) return;
    setErr(null);
    try {
      await deleteSession(token, sessionId);
      if (selected === sessionId) {
        setSelected(null);
        setWorkerClip(null);
        setWorkerInfo(null);
        setHlSnapshot(null);
        setHlMargin(null);
        setHlSpot([]);
      }
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  };

  const onRefreshTelemetry = useCallback(async () => {
    if (!token || !selected) return;
    setTelemetryBusy(true);
    setErr(null);
    try {
      const rows = await getSessionEvents(token, selected, 500);
      setLive(rows.slice(-200) as { kind: string; ts: number; data?: object }[]);
    } catch (e) {
      setErr(String(e));
    } finally {
      setTelemetryBusy(false);
    }
  }, [token, selected]);

  useEffect(() => {
    setHlSnapshot(null);
    setHlMargin(null);
    setHlSpot([]);
    setWorkerInfo(null);
    setWorkerClip(null);
  }, [selected]);

  useEffect(() => {
    if (!token || !selected) {
      setLive([]);
      return;
    }
    setLive([]);
    const ws = openTelemetryWs(token, selected, (msg) => {
      setLive((prev) => [...prev.slice(-200), msg as { kind: string; ts: number; data?: object }]);
    });
    return () => ws.close();
  }, [token, selected]);

  const statusLine = useMemo(() => {
    if (!user) return "Not signed in";
    return `Signed in as ${user.wallet_address}`;
  }, [user]);

  const selectedSession = useMemo(
    () => sessions.find((s) => s.id === selected) ?? null,
    [sessions, selected],
  );

  const sessionUsesTestnet = useMemo(() => {
    if (!selectedSession?.config || typeof selectedSession.config !== "object") {
      return market === "testnet";
    }
    const c = selectedSession.config as { testnet?: boolean };
    return c.testnet !== false;
  }, [selectedSession, market]);

  const derivedFromPk = useMemo(() => addressFromPrivateKey(pk), [pk]);
  const tradingAddressForSession =
    selected &&
    (derivedFromPk ||
      selectedSession?.custodial_address ||
      tradingAddresses[selected] ||
      null);

  const addressSourceLabel = useMemo(() => {
    if (!tradingAddressForSession) return null;
    if (derivedFromPk === tradingAddressForSession) {
      return "Preview from key field (save to persist on server)";
    }
    if (selectedSession?.custodial_address === tradingAddressForSession) {
      return "Custodial trading address (server)";
    }
    return "Trading address (local browser copy — server row is source of truth after refresh)";
  }, [tradingAddressForSession, selectedSession?.custodial_address, derivedFromPk]);

  const copyTradingAddress = async (addr: string) => {
    try {
      await navigator.clipboard.writeText(addr);
      setCopyHint("Copied");
      setTimeout(() => setCopyHint(null), 2000);
    } catch {
      setCopyHint("Copy failed — select the address manually");
      setTimeout(() => setCopyHint(null), 4000);
    }
  };

  const copyWorkerSecret = async (text: string, ok: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopyHint(ok);
      setTimeout(() => setCopyHint(null), 2500);
    } catch {
      setCopyHint("Copy failed");
      setTimeout(() => setCopyHint(null), 4000);
    }
  };

  const hlAppOrigin = sessionUsesTestnet ? "https://app.hyperliquid-testnet.xyz" : "https://app.hyperliquid.xyz";
  const livePnlUsd = useMemo(() => extractLivePnl(live), [live]);
  const marginAccountValueUsd = useMemo(
    () => (hlMargin ? Number.parseFloat(hlMargin.account_value) : Number.NaN),
    [hlMargin],
  );
  const pnlDisplayValue =
    livePnlUsd !== null
      ? livePnlUsd
      : Number.isFinite(marginAccountValueUsd)
        ? marginAccountValueUsd
        : null;
  const pnlLabel = livePnlUsd !== null ? "Live PnL" : "Account Value";
  const selectedStatus = selectedSession?.status ?? "Not selected";
  const onboardingSteps = [
    { label: "Sign in", done: Boolean(token), required: true },
    { label: "Create a bot session", done: sessions.length > 0, required: true },
    { label: "Fund trading address", done: Boolean(tradingAddressForSession), required: true },
    { label: "Start worker", done: selectedSession?.status === "running", required: true },
    { label: "Load balances (recommended)", done: Boolean(hlMargin) || hlSpot.length > 0, required: false },
  ];
  const requiredSteps = onboardingSteps.filter((step) => step.required);
  const onboardingProgress = Math.round(
    (requiredSteps.filter((step) => step.done).length / requiredSteps.length) * 100,
  );

  return (
    <div className="app-shell">
      <div className="pnl-ribbon" role="status" aria-live="polite">
        <div className="pnl-ribbon__main">
          <span className="pnl-ribbon__label">{pnlLabel}</span>
          <strong className={pnlDisplayValue !== null && pnlDisplayValue < 0 ? "neg" : "pos"}>
            {pnlDisplayValue === null ? "—" : `${pnlDisplayValue >= 0 ? "+" : ""}${pnlDisplayValue.toFixed(2)} USDC`}
          </strong>
        </div>
        <span className="pnl-ribbon__meta">Session: {selectedStatus}</span>
        <span className="pnl-ribbon__meta">{token ? statusLine : "Sign in to stream live telemetry"}</span>
      </div>

      <main className="app-frame">
        <header className="hero">
          <h1>heyemily</h1>
          <p>Clean execution UI with guided onboarding, balance visibility, and always-on live performance context.</p>
        </header>
        {err && <p className="error">{err}</p>}

        {!token && (
          <div className="card">
          <h2>Sign in</h2>
          <p style={{ fontSize: 14, opacity: 0.85, marginTop: 0 }}>
            Sign one short message with an EVM wallet. The backend verifies it and issues a dashboard JWT (no
            password). <strong>WalletConnect</strong> uses Reown’s relay and needs a free project id;{" "}
            <strong>browser wallet</strong> uses MetaMask / Rabby / etc. and needs no extra config.
          </p>
          <div style={{ marginTop: 12, display: "flex", flexWrap: "wrap", gap: 8 }}>
            <button type="button" disabled={signInBusy} onClick={() => void onBrowserWallet()}>
              {signInBusy ? "Connecting…" : "Browser wallet (injected)"}
            </button>
            <button
              type="button"
              disabled={signInBusy || !isWalletConnectConfigured()}
              onClick={() => void onWalletConnect()}
              title={
                isWalletConnectConfigured()
                  ? "WalletConnect (QR / mobile)"
                  : "Set VITE_WALLETCONNECT_PROJECT_ID in platform/web/.env"
              }
            >
              {signInBusy ? "Connecting…" : "WalletConnect"}
            </button>
          </div>
          {!isWalletConnectConfigured() && (
            <p style={{ fontSize: 13, opacity: 0.8, marginTop: 12, marginBottom: 0 }}>
              WalletConnect is optional here: add{" "}
              <code style={{ fontSize: 12 }}>VITE_WALLETCONNECT_PROJECT_ID</code> from{" "}
              <a href="https://cloud.reown.com" target="_blank" rel="noreferrer">
                cloud.reown.com
              </a>{" "}
              for QR / mobile wallets. There is no separate self-hosted WalletConnect relay in this app — Reown’s
              network is the standard infra for WC v2.
            </p>
          )}
          </div>
        )}

        {token && (
          <>
            <div className="card card-inline">
              <button type="button" className="ghost" onClick={() => void logout()}>
                Logout
              </button>
            </div>

            <div className="card">
              <h2>Onboarding progress</h2>
              <p className="muted">Complete these steps to get a live bot in production safely.</p>
              <div className="progress-wrap" aria-hidden>
                <div className="progress-bar" style={{ width: `${onboardingProgress}%` }} />
              </div>
              <p className="muted">{onboardingProgress}% complete (required steps)</p>
              <ol className="onboarding-steps">
                {onboardingSteps.map((step) => (
                  <li key={step.label} className={step.done ? "done" : "pending"}>
                    <span aria-hidden>{step.done ? "✓" : "•"}</span> {step.label}
                  </li>
                ))}
              </ol>
            </div>

            <div className="card">
              <h2>New bot session</h2>
              <p style={{ fontSize: 14, opacity: 0.85, marginTop: 0 }}>
                Minimal setup: choose token + market, then create and start. Strategy execution runs automatically.
              </p>
              <label>Bot name</label>
              <input value={sessionName} onChange={(e) => setSessionName(e.target.value)} />
              <div style={{ marginTop: 10, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                <label>
                  Token
                  <select
                    value={tokenSymbol}
                    onChange={(e) => {
                      const next = e.target.value.toUpperCase();
                      setTokenSymbol(next);
                      setSessionName(`bot_${next.toLowerCase()}`);
                    }}
                    style={{ display: "block", marginTop: 6 }}
                  >
                    {TOKEN_OPTIONS.map((sym) => (
                      <option key={sym} value={sym}>
                        {sym}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Market
                  <select
                    value={market}
                    onChange={(e) => setMarket(e.target.value as "testnet" | "mainnet")}
                    style={{ display: "block", marginTop: 6 }}
                  >
                    <option value="testnet">Testnet</option>
                    <option value="mainnet">Mainnet</option>
                  </select>
                </label>
              </div>
              <div style={{ marginTop: 10 }}>
                <button type="button" className="ghost" onClick={() => setShowAdvanced((v) => !v)}>
                  {showAdvanced ? "Hide advanced" : "Advanced settings"}
                </button>
              </div>
              {showAdvanced && (
                <div
                  style={{
                    marginTop: 10,
                    padding: 10,
                    border: "1px solid rgba(255,255,255,0.15)",
                    borderRadius: 8,
                  }}
                >
                  <label>
                    Risk profile
                    <select
                      value={riskProfile}
                      onChange={(e) => setRiskProfile(e.target.value as RiskProfile)}
                      style={{ display: "block", marginTop: 6 }}
                    >
                      {Object.entries(RISK_PROFILES).map(([key, profile]) => (
                        <option key={key} value={key}>
                          {profile.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label style={{ marginTop: 10, display: "block" }}>
                    Order amount (USD)
                    <input
                      type="number"
                      min={10}
                      step="1"
                      value={orderSizeUsd}
                      onChange={(e) => setOrderSizeUsd(Number(e.target.value))}
                      style={{ display: "block", marginTop: 6 }}
                    />
                  </label>
                  <label style={{ marginTop: 10, display: "block" }}>
                    Leverage (x)
                    <input
                      type="number"
                      min={1}
                      step="1"
                      value={leverage}
                      onChange={(e) => setLeverage(Number(e.target.value))}
                      style={{ display: "block", marginTop: 6 }}
                    />
                  </label>
                  <p style={{ fontSize: 12, opacity: 0.8, marginBottom: 0 }}>
                    Selected: {RISK_PROFILES[riskProfile].label} · order size {Math.max(10, orderSizeUsd)} USDC ·
                    leverage {Math.max(1, Math.floor(leverage || 1))}x · max symbol notional{" "}
                    {RISK_PROFILES[riskProfile].maxOpenNotionalUsd} USDC
                  </p>
                  <p style={{ fontSize: 12, opacity: 0.8, marginTop: 6, marginBottom: 0 }}>
                    Hyperliquid minimum order value is 10 USDC; lower amounts are auto-clamped.
                  </p>
                </div>
              )}
              <div style={{ marginTop: 12 }}>
                <button type="button" className="primary" onClick={onCreateSession}>
                  Create session
                </button>
              </div>
            </div>

          <div className="card">
            <h2>Sessions</h2>
            <ul style={{ listStyle: "none", padding: 0 }}>
              {sessions.map((s) => (
                <li key={s.id} style={{ marginBottom: 8 }}>
                  <button type="button" onClick={() => setSelected(s.id)}>
                    {s.name}
                  </button>{" "}
                  <code style={{ opacity: 0.8 }}>{s.id.slice(0, 8)}…</code> — {s.status}
                  {s.custodial_address ? (
                    <span style={{ fontSize: 12, opacity: 0.75, marginLeft: 6 }}>
                      · HL <code>{s.custodial_address.slice(0, 6)}…{s.custodial_address.slice(-4)}</code>
                    </span>
                  ) : null}
                  <button
                    type="button"
                    style={{ marginLeft: 8 }}
                    onClick={() => void onDeleteSession(s.id)}
                    title="Delete this bot session (wallet is preserved)"
                  >
                    Delete
                  </button>
                </li>
              ))}
            </ul>
          </div>

          {selected && (
            <>
              <div className="card" style={{ borderLeft: "4px solid #2a9d8f" }}>
                <h2 style={{ marginTop: 0 }}>Fund this bot on Hyperliquid</h2>
                <p style={{ fontSize: 14, opacity: 0.9, marginTop: 0 }}>
                  The worker trades with the <strong>custodial key provisioned on the server</strong> or a key you paste
                  below. Deposits must go to <em>this</em> trading address on Hyperliquid
                  {sessionUsesTestnet ? " testnet" : ""}. Your dashboard sign-in wallet is unrelated.
                </p>
                <p style={{ fontSize: 13, opacity: 0.82, marginBottom: 0 }}>
                  Balances: <strong>Perps</strong> = perpetual margin (clearinghouse). <strong>Spot</strong> = token
                  balances — new USDC often lands in <strong>spot</strong> first; if perps show 0, open the HL app and
                  transfer to perps / check you used the same network (testnet vs mainnet) as this session&apos;s
                  config.
                </p>
                <p style={{ fontSize: 13, opacity: 0.82, marginTop: 10, marginBottom: 0 }}>
                  If you used Hyperliquid <strong>Send Tokens</strong> with destination <strong>Trading Account</strong>,
                  that credits USDC <em>inside</em> HL for whichever wallet you had selected in the app. The recipient
                  must be the <strong>same 0x… address</strong> as below (this bot&apos;s trading key). Then click{" "}
                  <strong>Lookup balance</strong> — USDC usually appears under <strong>Spot</strong> until you move it
                  to perps.
                </p>
                {tradingAddressForSession ? (
                  <>
                    {addressSourceLabel && (
                      <p style={{ fontSize: 13, opacity: 0.85, marginBottom: 4 }}>{addressSourceLabel}:</p>
                    )}
                    <code
                      style={{
                        display: "block",
                        wordBreak: "break-all",
                        background: "#0b0c0f",
                        padding: 10,
                        borderRadius: 6,
                        fontSize: 13,
                      }}
                    >
                      {tradingAddressForSession}
                    </code>
                    <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                      <button type="button" onClick={() => void copyTradingAddress(tradingAddressForSession)}>
                        Copy address
                      </button>
                      {copyHint && <span style={{ fontSize: 13, opacity: 0.85 }}>{copyHint}</span>}
                      <a href={hlAppOrigin} target="_blank" rel="noreferrer">
                        Open Hyperliquid app
                      </a>
                      {sessionUsesTestnet && (
                        <>
                          <a href="https://app.hyperliquid-testnet.xyz/drip" target="_blank" rel="noreferrer">
                            Testnet drip
                          </a>
                          <a
                            href="https://hyperliquid.gitbook.io/hyperliquid-docs/onboarding/testnet-faucet"
                            target="_blank"
                            rel="noreferrer"
                          >
                            Faucet docs
                          </a>
                        </>
                      )}
                    </div>
                    {sessionUsesTestnet && (
                      <p style={{ fontSize: 13, opacity: 0.85, marginBottom: 0 }}>
                        ~50 USD notional in test USDC is plenty for the default config before you start the worker.
                      </p>
                    )}
                    <div style={{ marginTop: 12, display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                      <button type="button" disabled={hlBalanceBusy} onClick={() => void onLookupHyperliquidBalance()}>
                        {hlBalanceBusy ? "Loading…" : "Lookup balance (clearinghouse)"}
                      </button>
                      <button type="button" disabled={hlBusy} onClick={() => void onFetchHyperliquidAccount()}>
                        {hlBusy ? "Loading…" : "Fetch raw clearinghouse JSON"}
                      </button>
                    </div>
                    <p style={{ fontSize: 12, opacity: 0.75, marginTop: 6, marginBottom: 0 }}>
                      Uses Hyperliquid public <code>info</code> (<code>clearinghouseState</code> +{" "}
                      <code>spotClearinghouseState</code>) for this session&apos;s trading address only — no private key
                      is sent.
                    </p>
                    <div
                      style={{
                        marginTop: 14,
                        padding: 12,
                        borderRadius: 6,
                        border: "1px solid rgba(255,255,255,0.12)",
                        background: "rgba(0,0,0,0.2)",
                      }}
                    >
                      <h4 style={{ margin: "0 0 6px", fontSize: 14 }}>Move USDC (spot ↔ perps)</h4>
                      <p style={{ fontSize: 12, opacity: 0.82, margin: "0 0 10px" }}>
                        Signed on the server with your custodial key (<code>usdClassTransfer</code>). Run{" "}
                        <strong>Lookup balance</strong> first so limits match Hyperliquid.
                      </p>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                        <button type="button" disabled={hlTransferBusy} onClick={() => void onMoveSpotToPerp()}>
                          {hlTransferBusy ? "…" : "Spot → perps (all free USDC)"}
                        </button>
                        <button
                          type="button"
                          disabled={hlTransferBusy || !hlMargin}
                          onClick={() => void onMovePerpToSpotMax()}
                        >
                          Perps → spot (withdrawable max)
                        </button>
                        <label style={{ fontSize: 12, opacity: 0.85, display: "flex", gap: 6, alignItems: "center" }}>
                          <span>Perps → spot</span>
                          <input
                            type="number"
                            inputMode="decimal"
                            min={0}
                            step="any"
                            placeholder="amount"
                            value={perpToSpotAmount}
                            onChange={(e) => setPerpToSpotAmount(e.target.value)}
                            style={{ width: 110 }}
                          />
                          <button
                            type="button"
                            disabled={hlTransferBusy}
                            onClick={() => void onMovePerpToSpotCustom()}
                          >
                            Go
                          </button>
                        </label>
                      </div>
                    </div>
                    {hlMargin && (
                      <>
                        <h3 style={{ fontSize: 15, marginTop: 14, marginBottom: 6 }}>Perps (margin)</h3>
                        <dl
                          style={{
                            marginTop: 0,
                            marginBottom: 0,
                            display: "grid",
                            gridTemplateColumns: "auto 1fr",
                            gap: "6px 16px",
                            fontSize: 14,
                            alignItems: "baseline",
                          }}
                        >
                          <dt style={{ opacity: 0.75 }}>Account value</dt>
                          <dd style={{ margin: 0, fontVariantNumeric: "tabular-nums" }}>
                            {hlMargin.account_value} USDC
                          </dd>
                          <dt style={{ opacity: 0.75 }}>Withdrawable</dt>
                          <dd style={{ margin: 0, fontVariantNumeric: "tabular-nums" }}>
                            {hlMargin.withdrawable} USDC
                          </dd>
                          <dt style={{ opacity: 0.75 }}>Margin used</dt>
                          <dd style={{ margin: 0, fontVariantNumeric: "tabular-nums" }}>{hlMargin.total_margin_used}</dd>
                          <dt style={{ opacity: 0.75 }}>Notional positions</dt>
                          <dd style={{ margin: 0, fontVariantNumeric: "tabular-nums" }}>{hlMargin.total_ntl_pos}</dd>
                          <dt style={{ opacity: 0.75 }}>Open positions</dt>
                          <dd style={{ margin: 0 }}>{hlMargin.open_positions}</dd>
                        </dl>
                        <h3 style={{ fontSize: 15, marginTop: 16, marginBottom: 6 }}>Spot (tokens)</h3>
                        {hlSpot.length > 0 ? (
                          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                            <thead>
                              <tr style={{ textAlign: "left", opacity: 0.75 }}>
                                <th style={{ padding: "4px 8px 4px 0" }}>Coin</th>
                                <th style={{ padding: 4 }}>Total</th>
                                <th style={{ padding: 4 }}>Hold</th>
                                <th style={{ padding: 4 }}>Avail (total−hold)</th>
                              </tr>
                            </thead>
                            <tbody>
                              {hlSpot.map((row) => (
                                <tr key={row.coin}>
                                  <td style={{ padding: "4px 8px 4px 0", fontVariantNumeric: "tabular-nums" }}>
                                    {row.coin}
                                  </td>
                                  <td style={{ padding: 4, fontVariantNumeric: "tabular-nums" }}>{row.total}</td>
                                  <td style={{ padding: 4, fontVariantNumeric: "tabular-nums" }}>{row.hold}</td>
                                  <td style={{ padding: 4, fontVariantNumeric: "tabular-nums" }}>
                                    {spotAvailable(row.total, row.hold)}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        ) : (
                          <p style={{ fontSize: 13, opacity: 0.8, margin: 0 }}>
                            No spot token rows returned — if you just deposited, confirm the HL app shows funds on{" "}
                            <strong>this address</strong> and try again after the deposit confirms.
                          </p>
                        )}
                        {hlSpot.some(
                          (row) => row.coin === "USDC" && Number(row.total) > 0 && Number(hlMargin.account_value) === 0,
                        ) && (
                          <p
                            style={{
                              marginTop: 12,
                              marginBottom: 0,
                              padding: 10,
                              borderRadius: 6,
                              background: "#132418",
                              border: "1px solid rgba(42, 157, 143, 0.45)",
                              fontSize: 13,
                              lineHeight: 1.45,
                            }}
                          >
                            <strong>Spot USDC is funded.</strong> Perps still show 0 until you move collateral — use{" "}
                            <strong>Spot → perps (all free USDC)</strong> above, or the Hyperliquid app for this address.
                            Then <strong>Lookup balance</strong> again; perp account value should rise so the worker can
                            use margin.
                          </p>
                        )}
                      </>
                    )}
                    {hlSnapshot && (
                      <pre
                        style={{
                          marginTop: 10,
                          maxHeight: 220,
                          overflow: "auto",
                          fontSize: 11,
                          background: "#0b0c0f",
                          padding: 10,
                          borderRadius: 6,
                        }}
                      >
                        {hlSnapshot}
                      </pre>
                    )}
                  </>
                ) : (
                  <p style={{ fontSize: 14, opacity: 0.85, marginBottom: 0 }}>
                    Use <strong>Provision custodial wallet</strong> below, or paste a key — the Hyperliquid address
                    appears here (paste is previewed locally until you save).
                  </p>
                )}
              </div>

              <div className="card">
                <h2>Credentials (encrypted at rest)</h2>
                <p style={{ fontSize: 14, opacity: 0.85 }}>
                  Either let the platform <strong>spawn a custodial EVM wallet</strong> (Hyperliquid API account) and
                  encrypt it with the master key, or paste your own key once.
                </p>
                {!selectedSession?.custodial_address ? (
                  <div style={{ marginBottom: 12 }}>
                    <button type="button" disabled={provisionBusy} onClick={() => void onProvisionCustodial()}>
                      {provisionBusy ? "Provisioning…" : "Provision custodial wallet (server)"}
                    </button>
                    <p style={{ fontSize: 12, opacity: 0.75, marginTop: 6, marginBottom: 0 }}>
                      Creates a new keypair; private material never appears in the browser. Use a new session if you
                      need another wallet (existing keys cannot be overwritten from here).
                    </p>
                  </div>
                ) : (
                  <p style={{ fontSize: 13, opacity: 0.85 }}>
                    This session already has a wallet on file. To replace it, create a new bot session.
                  </p>
                )}
                <label style={{ fontSize: 13, opacity: 0.9 }}>Or paste your own private key</label>
                <input
                  type="password"
                  placeholder="0x… private key"
                  value={pk}
                  onChange={(e) => setPk(e.target.value)}
                  style={{ marginTop: 6 }}
                />
                <div style={{ marginTop: 8 }}>
                  <button type="button" onClick={onSaveKey}>
                    Save encrypted key
                  </button>
                  <button
                    type="button"
                    style={{ marginLeft: 8 }}
                    disabled={exportBusy}
                    onClick={() => void onExportPrivateKey()}
                  >
                    {exportBusy ? "Exporting…" : "Copy exported private key"}
                  </button>
                </div>
                <p style={{ fontSize: 12, opacity: 0.75, marginTop: 6, marginBottom: 0 }}>
                  Export uses your authenticated session and copies the decrypted key directly to clipboard.
                </p>
              </div>

              <div className="card">
                <h2>Control</h2>
                <p style={{ fontSize: 13, opacity: 0.85, marginTop: 0 }}>
                  <strong>Start</strong> marks the session running, issues a worker JWT, and (by default) launches the
                  lite worker process on the same machine as the API. <strong>Stop</strong> ends the session and
                  terminates that supervised process when possible.
                </p>
                <button type="button" onClick={onStart}>
                  Start (issue worker token)
                </button>{" "}
                <button type="button" onClick={onStop}>
                  Stop
                </button>
                <button
                  type="button"
                  className="danger"
                  disabled={closeAllBusy}
                  onClick={() => void onCloseAllOrders()}
                  style={{ marginLeft: 8 }}
                  title="Cancel all open orders and submit reduce-only closes for all open positions"
                >
                  {closeAllBusy ? "Closing…" : "Close all orders"}
                </button>
                {workerInfo && (
                  <pre
                    style={{
                      marginTop: 12,
                      whiteSpace: "pre-wrap",
                      background: "#0b0c0f",
                      padding: 12,
                      borderRadius: 8,
                    }}
                  >
                    {workerInfo}
                  </pre>
                )}
                {workerClip && (
                  <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                    <button type="button" onClick={() => void copyWorkerSecret(workerClip.jwt, "Worker JWT copied")}>
                      Copy worker JWT
                    </button>
                    <button type="button" onClick={() => void copyWorkerSecret(workerClip.shell, "Shell command copied")}>
                      Copy shell command
                    </button>
                    {copyHint && <span style={{ fontSize: 13, opacity: 0.85 }}>{copyHint}</span>}
                  </div>
                )}
              </div>

              <div className="card">
                <h2 style={{ display: "inline-block", marginRight: 12 }}>Live telemetry</h2>
                <button type="button" disabled={telemetryBusy} onClick={() => void onRefreshTelemetry()}>
                  {telemetryBusy ? "Loading…" : "Load event log"}
                </button>
                <p style={{ fontSize: 13, opacity: 0.82, marginTop: 8 }}>
                  The worker POSTs each event to <code>/api/internal/telemetry</code> with the <strong>worker</strong>{" "}
                  JWT. This WebSocket only uses your <strong>login</strong> token. Past events are replayed on connect;
                  use the button to pull the same buffer from the REST API if the socket missed anything.
                </p>
                <div style={{ maxHeight: 320, overflow: "auto", fontSize: 12 }}>
                  {live.length === 0 ? (
                    <p style={{ opacity: 0.75, margin: 0 }}>
                      No events yet — ensure the supervised worker is running, then click <strong>Load event log</strong>
                      . If still empty, confirm the worker can reach the API base URL (same host as{" "}
                      <code>HFT_API_BASE</code> in the shell command) and watch the API log for{" "}
                      <code>POST /api/internal/telemetry</code> (401 means token or session id mismatch).
                    </p>
                  ) : (
                    live.map((x, i) => (
                      <div key={`${x.ts}-${i}`} style={{ opacity: 0.9 }}>
                        [{x.kind}] {new Date((x.ts || 0) * 1000).toISOString()} {JSON.stringify(x)}
                      </div>
                    ))
                  )}
                </div>
              </div>
            </>
          )}
        </>
      )}
      </main>
    </div>
  );
}
