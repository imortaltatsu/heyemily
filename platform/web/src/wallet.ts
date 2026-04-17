import EthereumProvider from "@walletconnect/ethereum-provider";
import { BrowserProvider, type Eip1193Provider } from "ethers";

type WCProvider = Awaited<ReturnType<typeof EthereumProvider.init>>;

let wcProvider: WCProvider | null = null;

export type WalletSession = {
  address: string;
  signMessage: (message: string) => Promise<string>;
};

/** WalletConnect / Reown relay requires a cloud project id — no self-hosted relay in this bundle. */
export function isWalletConnectConfigured(): boolean {
  const id = import.meta.env.VITE_WALLETCONNECT_PROJECT_ID as string | undefined;
  return Boolean(id?.trim());
}

function getInjectedEthereum(): Eip1193Provider {
  const w = typeof window !== "undefined" ? (window as unknown as { ethereum?: unknown }).ethereum : undefined;
  if (!w || typeof w !== "object") {
    throw new Error(
      "No injected wallet found (MetaMask, Rabby, Frame, …). Install one, or set VITE_WALLETCONNECT_PROJECT_ID for mobile / WalletConnect.",
    );
  }
  const eth = w as { providers?: unknown[] };
  if (Array.isArray(eth.providers) && eth.providers.length > 0) {
    return eth.providers[0] as Eip1193Provider;
  }
  return w as Eip1193Provider;
}

/** EIP-1193 browser extension — no Reown project id. */
export async function connectBrowserWallet(): Promise<WalletSession> {
  if (wcProvider) {
    await wcProvider.disconnect().catch(() => {});
    wcProvider = null;
  }
  const injected = getInjectedEthereum();
  const bp = new BrowserProvider(injected);
  const signer = await bp.getSigner();
  const address = await signer.getAddress();
  return {
    address,
    signMessage: (message: string) => signer.signMessage(message),
  };
}

/** WalletConnect modal (mobile wallets, QR) — requires VITE_WALLETCONNECT_PROJECT_ID. */
export async function connectWalletConnect(): Promise<WalletSession> {
  const projectId = import.meta.env.VITE_WALLETCONNECT_PROJECT_ID as string | undefined;
  if (!projectId?.trim()) {
    throw new Error(
      "WalletConnect needs VITE_WALLETCONNECT_PROJECT_ID (free at https://cloud.reown.com). Or use “Browser wallet” instead.",
    );
  }
  if (wcProvider) {
    await wcProvider.disconnect().catch(() => {});
    wcProvider = null;
  }
  wcProvider = await EthereumProvider.init({
    projectId: projectId.trim(),
    chains: [42161],
    showQrModal: true,
    methods: ["personal_sign", "eth_sendTransaction"],
  });
  await wcProvider.enable();
  const ethersProvider = new BrowserProvider(wcProvider);
  const signer = await ethersProvider.getSigner();
  const address = await signer.getAddress();
  return {
    address,
    signMessage: (message: string) => signer.signMessage(message),
  };
}

/** @deprecated Use connectWalletConnect or connectBrowserWallet */
export async function connectWallet(): Promise<WalletSession> {
  return connectWalletConnect();
}

export async function disconnectWallet(): Promise<void> {
  if (wcProvider) {
    await wcProvider.disconnect().catch(() => {});
    wcProvider = null;
  }
}
