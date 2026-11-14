"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { Alert, AuthShell, Field } from "@/components/AuthShell";
import { useUi } from "@/components/Providers";
import { ApiError, post } from "@/lib/api";
import { errorKey, passwordProblems } from "@/lib/errors";

export default function RegisterPage() {
  const { t } = useUi();
  const [error, setError] = useState<string | null>(null);
  const [doneFor, setDoneFor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const email = String(form.get("email") ?? "");
    const password = String(form.get("password") ?? "");
    if (passwordProblems(password).length) {
      setError(`${t("error.weak_password")} ${t("auth.password.rules")}`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const displayName = String(form.get("display_name") ?? "").trim();
      await post("/api/auth/register", { email, password, display_name: displayName || null });
      setDoneFor(email);
    } catch (err) {
      setError(t(errorKey(err instanceof ApiError ? err.code : undefined)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title={t("auth.register.title")}>
      {doneFor ? (
        <div className="space-y-4">
          <Alert tone="success">{t("auth.register.done", { email: doneFor })}</Alert>
          <Link href="/login" className="link text-sm">{t("auth.backToLogin")}</Link>
        </div>
      ) : (
        <>
          <form onSubmit={onSubmit} className="space-y-4">
            <Field label={t("auth.displayName")} name="display_name" autoComplete="name" maxLength={120} />
            <Field label={t("auth.email")} name="email" type="email" autoComplete="email" required />
            <Field label={t("auth.password")} name="password" type="password" autoComplete="new-password" required minLength={10} />
            <p className="text-xs text-slate-500">{t("auth.password.rules")}</p>
            {error && <Alert tone="error">{error}</Alert>}
            <button className="btn-primary w-full" disabled={busy}>{t("auth.register.submit")}</button>
          </form>
          <p className="mt-6 text-sm text-slate-500">
            {t("auth.register.haveAccount")} <Link href="/login" className="link">{t("landing.cta.login")}</Link>
          </p>
        </>
      )}
    </AuthShell>
  );
}
