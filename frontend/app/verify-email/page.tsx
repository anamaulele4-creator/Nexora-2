"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState, type FormEvent } from "react";

import { Alert, AuthShell, Field } from "@/components/AuthShell";
import { useUi } from "@/components/Providers";
import { ApiError, post } from "@/lib/api";
import { errorKey } from "@/lib/errors";

type State = "working" | "ok" | "error" | "missing";

function Verify() {
  const { t } = useUi();
  const token = useSearchParams().get("token");
  const [state, setState] = useState<State>(token ? "working" : "missing");
  const [error, setError] = useState<string | null>(null);
  const [resent, setResent] = useState(false);
  const started = useRef(false);

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true; // o token é de uso único: nunca o enviar duas vezes
    post("/api/auth/verify-email", { token })
      .then(() => setState("ok"))
      .catch((err) => {
        setError(t(errorKey(err instanceof ApiError ? err.code : undefined)));
        setState("error");
      });
  }, [token, t]);

  async function resend(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const email = new FormData(e.currentTarget).get("email");
    try {
      await post("/api/auth/resend-verification", { email });
      setResent(true);
    } catch (err) {
      setError(t(errorKey(err instanceof ApiError ? err.code : undefined)));
    }
  }

  return (
    <AuthShell title={t("auth.verify.title")}>
      <div className="space-y-4">
        {state === "working" && <Alert tone="info">{t("auth.verify.working")}</Alert>}
        {state === "ok" && (
          <>
            <Alert tone="success">{t("auth.verify.ok")}</Alert>
            <Link href="/login" className="btn-primary w-full">{t("auth.login.submit")}</Link>
          </>
        )}
        {(state === "error" || state === "missing") && (
          <>
            <Alert tone="error">{state === "missing" ? t("auth.verify.missing") : error}</Alert>
            {resent ? (
              <Alert tone="success">{t("auth.verify.resent")}</Alert>
            ) : (
              <form onSubmit={resend} className="space-y-3">
                <Field label={t("auth.email")} name="email" type="email" autoComplete="email" required />
                <button className="btn-primary w-full">{t("auth.verify.resend")}</button>
              </form>
            )}
          </>
        )}
      </div>
    </AuthShell>
  );
}

export default function VerifyEmailPage() {
  return (
    <Suspense>
      <Verify />
    </Suspense>
  );
}
