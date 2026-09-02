/**
 * Browser-side storage boundary for Control Plane credentials.
 *
 * Read and write the key in sessionStorage; validation and requests belong
 * elsewhere. The original operator-only console let nginx inject control-plane-token,
 * so the browser held no credential. The same console now serves platform
 * operators and tenants, and a server-fixed static token cannot represent both
 * identities. Callers must therefore bring credentials. Storage rules:
 *   - Use sessionStorage only. localStorage spans tabs and persists on disk; a
 *     tenant key controls that tenant's workspaces, while an admin key is
 *     equivalent to sandbox-cluster administration. Closing the tab must expire it.
 *   - Never place credentials in URLs. Queries enter browser history, Referer
 *     headers, and server access logs.
 *   - Logout calls clear() rather than removing one key, so any future one-time
 *     plaintext value accidentally placed in this storage area is also removed.
 * AI-LOCK: Do not move this to localStorage or cookies to survive a page refresh.
 * Reauthentication is cheaper than persisting cluster-administrator credentials.
 */

const TOKEN_KEY = "sandbox-console-token";

export function loadToken(): string {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    // Privacy modes and disabled storage can throw. Treat this as logged out so
    // the login page still renders instead of white-screening during mount.
    return "";
  }
}

export function saveToken(token: string): void {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Keep the in-memory session if storage fails. A refresh then requires login.
  }
}

export function clearToken(): void {
  try {
    window.sessionStorage.clear();
  } catch {
    // If storage is unavailable, it is already effectively clear.
  }
}
