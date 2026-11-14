"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";

import { Alert, AuthShell, Field } from "@/components/AuthShell";
import { useUi } from "@/components/Providers";
import { ApiError, post } from "@/lib/api";
import { errorKey } from "@/lib/errors";

function safeNext(value: string | null): string {
  // só caminhos internos (evita redirecionamento aberto)
  return value && value.startsWith("/") && !value.startsWith("//") ? value : "/dashboard";
}

function LoginForm() {
  const { t } = useUi();
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    try {
      await post("/api/auth/login", { email: form.get("email"), password: form.get("password") });
      router.replace(safeNext(params.get("next")));
    } catch (err) {
      setError(t(errorKey(err instanceof ApiError ? err.code : undefined)));
      setBusy(false);
    }
  }

  return (
    <AuthShell title={t("auth.login.title")}>
      <form onSubmit={onSubmit} className="space-y-4">
        <Field label={t("auth.email")} name="email" type="email" autoComplete="email" required />
        <Field label={t("auth.password")} name="password" type="password" autoComplete="current-password" required />
        {error && <Alert tone="error">{error}</Alert>}
        <button className="btn-primary w-full" disabled={busy}>{t("auth.login.submit")}</button>
      </form>
      <div className="mt-6 flex flex-col gap-2 text-sm">
        <Link href="/forgot-password" className="link">{t("auth.login.forgot")}</Link>
        <span className="text-slate-500">
          {t("auth.login.noAccount")} <Link href="/register" className="link">{t("landing.cta.register")}</Link>
        </span>
      </div>
    </AuthShell>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
