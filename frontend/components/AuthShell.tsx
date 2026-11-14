"use client";

import type { ReactNode } from "react";

import { Header } from "./Header";

export function AuthShell({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="min-h-screen">
      <Header />
      <main className="mx-auto flex max-w-md flex-col px-6 py-16">
        <div className="card">
          <h1 className="mb-6 text-2xl font-semibold tracking-tight">{title}</h1>
          {children}
        </div>
      </main>
    </div>
  );
}

export function Field({
  label, ...props
}: { label: string } & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className="block space-y-1.5">
      <span className="text-sm font-medium text-slate-700 dark:text-slate-300">{label}</span>
      <input className="input" {...props} />
    </label>
  );
}

export function Alert({ tone, children }: { tone: "error" | "success" | "info"; children: ReactNode }) {
  const styles = {
    error: "border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200",
    success: "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/50 dark:text-emerald-200",
    info: "border-slate-300 bg-slate-50 text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300",
  }[tone];
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`rounded-lg border px-3 py-2 text-sm ${styles}`}>
      {children}
    </div>
  );
}
