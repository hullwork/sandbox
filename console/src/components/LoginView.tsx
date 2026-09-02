import { CircleAlert, KeyRound, LoaderCircle, LogIn } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ControlPlaneError, USE_MOCK } from "../api";
import {
  errorText,
  LanguageSwitcher,
  useI18n,
  type LocalizedError,
} from "../i18n";
import type { AuthMethodsView, WhoamiView } from "../types";

/**
 * Login screen: single sign-on, an API key, or both.
 *
 * A candidate credential is validated through `/v1/whoami` before it is stored in
 * sessionStorage. Reporting an invalid key here prevents every page from later
 * surfacing the same 401 and making the console appear broken.
 *
 * The whoami result is passed to the application shell because capabilities already
 * arrived with the successful login and determine which tabs to render.
 *
 * 🔴 The key field is rendered whatever `/v1/auth/methods` says. Keys issued by
 * the control plane are revocable, attributable and expiring, and they keep
 * working alongside an identity provider - they are what callers are meant to
 * migrate *to*. The flag that endpoint returns describes the static break-glass
 * token, which is refused by Control Plane itself rather than by this screen: hiding a
 * field is not a security control, and treating it as one is the classic way
 * this gets lost.
 */
export default function LoginView({
  onAuthenticated,
}: {
  onAuthenticated: (token: string, whoami: WhoamiView) => void;
}) {
  const { t } = useI18n();
  const [token, setToken] = useState("");
  const [error, setError] = useState<LocalizedError | null>(null);
  const [pending, setPending] = useState(false);
  const [methods, setMethods] = useState<AuthMethodsView | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api.authMethods()
      .then((available) => {
        if (!cancelled) setMethods(available);
      })
      .catch(() => {
        // An older Control Plane has no such endpoint. Fall back to the key field,
        // which every Control Plane accepts; a missing SSO button is recoverable,
        // a login screen with no way in is not.
        if (!cancelled) setMethods({ local_login: true, oidc: false });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const candidate = token.trim();
    if (!candidate) {
      setError({ key: "login.required" });
      return;
    }
    setPending(true);
    setError(null);
    try {
      const whoami = await api.whoami(candidate);
      onAuthenticated(candidate, whoami);
    } catch (cause) {
      if (cause instanceof ControlPlaneError && cause.status === 401) {
        setError({ key: "login.invalid" });
      } else if (cause instanceof ControlPlaneError && cause.status === 404) {
        // A running Control Plane without /v1/whoami is an older image, not a bad key.
        setError({ key: "login.endpointMissing" });
      } else if (cause instanceof ControlPlaneError && cause.status === 503) {
        // Control Plane deliberately reports store failure as 503. The credential may be
        // valid; changing it would send troubleshooting in the wrong direction.
        setError({
          key: "login.storeUnavailable",
          values: { message: cause.message },
        });
      } else if (cause instanceof ControlPlaneError && cause.status === 0) {
        setError({
          key: "api.networkError",
          values: { message: cause.message },
        });
      } else {
        setError({
          message: cause instanceof Error ? cause.message : String(cause),
        });
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="login-page">
      <LanguageSwitcher className="login-language" />
      <form className="login-card" onSubmit={(event) => void submit(event)}>
        <span className="login-icon" aria-hidden="true"><KeyRound size={22} /></span>
        <h1>{t("login.title")}</h1>
        <p className="login-subtitle">{t("login.subtitle")}</p>

        {methods?.oidc ? (
          <>
            <a className="button button-primary login-sso" href="/v1/auth/oidc/login">
              <LogIn size={16} aria-hidden="true" />
              {t("login.sso")}
            </a>
            <p className="login-note">{t("login.ssoNote")}</p>
          </>
        ) : null}

        <label className="field">
          <span>{t("login.apiKey")}</span>
          <input
            type="password"
            value={token}
            autoComplete="off"
            spellCheck={false}
            placeholder="sk_<random>_admin_… / sk_<random>_<tenant>_…"
            onChange={(event) => setToken(event.target.value)}
          />
        </label>

        {error ? (
          <p className="login-error" role="alert">
            <CircleAlert size={16} aria-hidden="true" />
            {errorText(error, t)}
          </p>
        ) : null}

        <button type="submit" className="button button-primary" disabled={pending}>
          {pending ? <LoaderCircle className="spin" size={16} /> : null}
          {pending ? t("login.verifying") : t("login.submit")}
        </button>

        <p className="login-note">{t("login.storageNote")}</p>
        {methods && !methods.local_login ? (
          <p className="login-note">{t("login.breakGlassOff")}</p>
        ) : null}
        {USE_MOCK ? (
          <p className="login-note login-note-mock">{t("login.mockNote")}</p>
        ) : null}
      </form>
    </div>
  );
}
