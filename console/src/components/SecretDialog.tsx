import { Check, Copy, TriangleAlert, X } from "lucide-react";
import { useState } from "react";
import { errorText, useI18n, type LocalizedError } from "../i18n";

/**
 * One-time plaintext credential dialog.
 *
 * The secret appears only in the issuance response; Control Plane stores its SHA-256 hash.
 * Keep it in React state only and never in sessionStorage, localStorage, cookies,
 * IndexedDB, URLs, document.title, or console logs.
 *
 * Do not add a “remember this key” convenience. Losing the value requires issuing a
 * replacement by design.
 */
export default function SecretDialog({
  title,
  subject,
  secret,
  note,
  onClose,
}: {
  title: string;
  subject: string;
  secret: string;
  note?: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<LocalizedError | null>(null);

  const copy = async () => {
    setCopyError(null);
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can fail in non-secure contexts (including HTTP NodePorts)
      // or when permission is denied. Explain manual selection because this is the
      // last opportunity to copy the secret.
      setCopyError({ key: "secret.clipboardDenied" });
    }
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={title}>
      <div className="modal">
        <div className="modal-header">
          <h2>{title}</h2>
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label={t("common.close")}
          >
            <X size={18} />
          </button>
        </div>

        <div className="secret-warning" role="alert">
          <TriangleAlert size={18} aria-hidden="true" />
          <div>
            <strong>{t("secret.onlyOnce")}</strong>
            <p>{t("secret.onlyOnceBody")}</p>
          </div>
        </div>

        <dl className="modal-meta">
          <div>
            <dt>{t("secret.subject")}</dt>
            <dd>{subject}</dd>
          </div>
        </dl>

        <code className="secret-value">{secret}</code>

        {note ? <p className="modal-note">{note}</p> : null}
        {copyError ? (
          <p className="modal-error" role="alert">{errorText(copyError, t)}</p>
        ) : null}

        <div className="modal-actions">
          <button type="button" className="button" onClick={() => void copy()}>
            {copied ? <Check size={16} /> : <Copy size={16} />}
            {copied ? t("common.copied") : t("common.copy")}
          </button>
          <button type="button" className="button button-primary" onClick={onClose}>
            {t("secret.saved")}
          </button>
        </div>
      </div>
    </div>
  );
}
