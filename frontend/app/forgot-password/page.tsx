"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { Alert, AuthShell, Field } from "@/components/AuthShell";
import { useUi } from "@/components/Providers";
import { ApiError, post } from "@/lib/api";
import { errorKey } from "@/lib/errors";

export default function ForgotPasswordPage() {
  const { t } = useUi();
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post("/api/auth/forgot-password", { email: new FormData(e.currentTarget).get("email") });
      setDone(true);
    } catch (err) {
      setError(t(errorKey(err instanceof ApiError ? err.code : undefined)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title={t("auth.forgot.title")}>
      {done ? (
        <Alert tone="success">{t("auth.forgot.done")}</Alert>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4">
          <p className="text-sm text-slate-600 dark:text-slate-400">{t("auth.forgot.lead")}</p>
          <Field label={t("auth.email")} name="email" type="email" autoComplete="email" required />
          {error && <Alert tone="error">{error}</Alert>}
          <button className="btn-primary w-full" disabled={busy}>{t("auth.forgot.submit")}</button>
        </form>
      )}
      <Link href="/login" className="link mt-6 inline-block text-sm">{t("auth.backToLogin")}</Link>
    </AuthShell>
  );
}
