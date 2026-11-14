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

/** Links da área autenticada. Em ecrãs pequenos ficam num menu que abre e fecha. */
export function AppNav({ me }: { me: Me | null }) {
  const { t } = useUi();
  const router = useRouter();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  // fecha o menu ao mudar de página
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  async function logout() {
    try {
      await post("/api/auth/logout");
    } finally {
      router.replace("/login");
    }
  }

  const link = (href: string, label: string) => {
    const active = pathname?.startsWith(href);
    return (
      <Link
        key={href}
        href={href}
        aria-current={active ? "page" : undefined}
        className={`btn-ghost w-full justify-start sm:w-auto sm:justify-center ${active ? "border-brand-500 text-brand-600 dark:text-brand-400" : ""}`}
      >
        {label}
      </Link>
    );
  };

  const links = (
    <>
      {link("/dashboard", t("nav.dashboard"))}
      {link("/chat", t("nav.chat"))}
      {link("/conectores", t("nav.connectors"))}
      {link("/definicoes", t("nav.settings"))}
      {me?.is_admin && link("/admin", t("nav.admin"))}
      <button type="button" className="btn-ghost w-full justify-start sm:w-auto sm:justify-center" onClick={logout}>
        {t("nav.logout")}
      </button>
    </>
  );

  return (
    <nav className="relative" aria-label={t("nav.menu")}>
      <div className="hidden flex-wrap items-center gap-2 sm:flex">{links}</div>
      <button
        type="button"
        className="btn-ghost sm:hidden"
        aria-expanded={open}
        aria-controls="app-nav-mobile"
        aria-label={open ? t("nav.close") : t("nav.open")}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "✕" : "☰"} <span className="ml-1">{t("nav.menu")}</span>
      </button>
      {open && (
        <div
          id="app-nav-mobile"
          className="absolute left-0 z-20 mt-2 flex w-56 flex-col gap-2 rounded-xl border border-slate-200 bg-white p-3 shadow-lg dark:border-slate-800 dark:bg-slate-900 sm:hidden"
        >
          {links}
        </div>
      )}
    </nav>
  );
}
