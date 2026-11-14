"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";

import { Alert, AuthShell, Field } from "@/components/AuthShell";
import { useUi } from "@/components/Providers";
import { ApiError, post } from "@/lib/api";
import { errorKey, passwordProblems } from "@/lib/errors";

function ResetForm() {
  const { t } = useUi();
  const token = useSearchParams().get("token");
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(token ? null : t("error.invalid_or_expired_token"));
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const pw = String(new FormData(e.currentTarget).get("new_password") ?? "");
    if (passwordProblems(pw).length) {
      setError(`${t("error.weak_password")} ${t("auth.password.rules")}`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await post("/api/auth/reset-password", { token, new_password: pw });
      setDone(true);
    } catch (err) {
      setError(t(errorKey(err instanceof ApiError ? err.code : undefined)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title={t("auth.reset.title")}>
      {done ? (
        <div className="space-y-4">
          <Alert tone="success">{t("auth.reset.done")}</Alert>
          <Link href="/login" className="btn-primary w-full">{t("auth.login.submit")}</Link>
        </div>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4">
          <Field label={t("auth.newPassword")} name="new_password" type="password" autoComplete="new-password"
                 required minLength={10} disabled={!token} />
          <p className="text-xs text-slate-500">{t("auth.password.rules")}</p>
          {error && <Alert tone="error">{error}</Alert>}
          <button className="btn-primary w-full" disabled={busy || !token}>{t("auth.reset.submit")}</button>
        </form>
      )}
    </AuthShell>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense>
      <ResetForm />
    </Suspense>
  );
}
