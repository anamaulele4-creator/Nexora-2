"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ApiError, api, post, type Me } from "@/lib/api";

import { useUi } from "./Providers";

/** Utilizador atual (ou null). Redireciona para o login se a sessão não for válida. */
export function useMe(nextPath: string) {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      setMe(await api<Me>("/api/auth/me"));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
        return;
      }
      throw err;
    } finally {
      setLoading(false);
    }
  }, [router, nextPath]);

  useEffect(() => {
    void reload().catch(() => undefined);
  }, [reload]);

  return { me, loading, reload };
}

/** Links da área autenticada: Dashboard, Chat, Admin (só admins) e Sair. */
export function AppNav({ me }: { me: Me | null }) {
  const { t } = useUi();
  const router = useRouter();
  const pathname = usePathname();

  async function logout() {
    try {
      await post("/api/auth/logout");
    } finally {
      router.replace("/login");
    }
  }

  const link = (href: string, label: string) => (
    <Link
      key={href}
      href={href}
      aria-current={pathname?.startsWith(href) ? "page" : undefined}
      className={`btn-ghost ${pathname?.startsWith(href) ? "border-brand-500 text-brand-600 dark:text-brand-400" : ""}`}
    >
      {label}
    </Link>
  );

  return (
    <nav className="flex flex-wrap items-center gap-2" aria-label={t("nav.menu")}>
      {link("/dashboard", t("nav.dashboard"))}
      {link("/chat", t("nav.chat"))}
      {me?.is_admin && link("/admin", t("nav.admin"))}
      <button type="button" className="btn-ghost" onClick={logout}>{t("nav.logout")}</button>
    </nav>
  );
}
