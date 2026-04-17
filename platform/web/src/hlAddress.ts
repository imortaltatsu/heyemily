import { Wallet } from "ethers";

/** Derive the EVM address Hyperliquid uses for this API private key (client-side only). */
export function addressFromPrivateKey(pk: string): string | null {
  const t = pk.trim();
  if (!t) return null;
  try {
    const normalized = /^0x/i.test(t) ? t : `0x${t}`;
    if (normalized.length < 66) return null;
    return new Wallet(normalized).address;
  } catch {
    return null;
  }
}
