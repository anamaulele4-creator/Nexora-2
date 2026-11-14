"use client";

import Link from "next/link";

import { Header } from "@/components/Header";
import { useUi } from "@/components/Providers";

export default function Landing() {
  const { t } = useUi();
  const pillars = [
    { title: t("landing.p1.title"), body: t("landing.p1.body") },
    { title: t("landing.p2.title"), body: t("landing.p2.body") },
    { title: t("landing.p3.title"), body: t("landing.p3.body") },
  ];
  return (
    <div className="min-h-screen">
      <Header>
        <Link href="/login" className="btn-ghost">{t("landing.cta.login")}</Link>
      </Header>
      <main className="mx-auto max-w-6xl px-6 py-20">
        <section className="max-w-3xl">
          <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">{t("brand.tagline")}</h1>
          <p className="mt-6 text-lg text-slate-600 dark:text-slate-400">{t("landing.lead")}</p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link href="/register" className="btn-primary">{t("landing.cta.register")}</Link>
            <Link href="/login" className="btn-ghost px-4 py-2">{t("landing.cta.login")}</Link>
          </div>
          <p className="mt-6 text-sm text-slate-500">{t("landing.stage")}</p>
        </section>
        <section className="mt-16 grid gap-4 sm:grid-cols-3">
          {pillars.map((p) => (
            <div key={p.title} className="card">
              <h2 className="font-semibold">{p.title}</h2>
              <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">{p.body}</p>
            </div>
          ))}
        </section>
      </main>
    </div>
  );
}
