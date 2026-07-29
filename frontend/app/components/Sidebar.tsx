"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

const NAV = [
  {
    href: "/dashboard",
    label: "Dashboard",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="9" rx="1" /><rect x="14" y="3" width="7" height="5" rx="1" />
        <rect x="14" y="12" width="7" height="9" rx="1" /><rect x="3" y="16" width="7" height="5" rx="1" />
      </svg>
    ),
  },
  {
    href: "/rules",
    label: "Manage Rules",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
      </svg>
    ),
  },
  {
    href: "/runs",
    label: "Runs",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 12a9 9 0 1 0 9-9" /><polyline points="3 3 3 9 9 9" /><path d="M12 7v5l3 2" />
      </svg>
    ),
  },
];

export default function Sidebar() {
  const pathname = usePathname();
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [healthy, setHealthy] = useState<boolean | null>(null);

  useEffect(() => {
    const saved = (localStorage.getItem("theme") as "light" | "dark") || null;
    const initial = saved || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    setTheme(initial);
    document.documentElement.setAttribute("data-theme", initial);
  }, []);

  useEffect(() => {
    const ping = () =>
      fetch(`${API_BASE}/api/health`)
        .then((r) => setHealthy(r.ok))
        .catch(() => setHealthy(false));
    ping();
    const id = setInterval(ping, 15000);
    return () => clearInterval(id);
  }, []);

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    localStorage.setItem("theme", next);
    document.documentElement.setAttribute("data-theme", next);
  }

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="mark">DQ</span>
        <span>
          <div className="bt">SMTC Validation</div>
          <div className="bs">Data Quality Framework</div>
        </span>
      </div>

      <nav className="nav">
        <div className="nav-label">Workspace</div>
        {NAV.map((item) => (
          <Link key={item.href} href={item.href} className={pathname === item.href ? "active" : ""}>
            {item.icon}
            {item.label}
          </Link>
        ))}
      </nav>

      <div className="side-foot">
        <div className="health">
          <span className={`dot ${healthy === false ? "off" : ""}`} />
          {healthy === null ? "Checking API…" : healthy ? "API connected" : "API offline"}
        </div>
        <button className="theme-toggle" onClick={toggleTheme}>
          {theme === "dark" ? (
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <circle cx="12" cy="12" r="4" />
              <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
            </svg>
          )}
          {theme === "dark" ? "Light mode" : "Dark mode"}
        </button>
      </div>
    </aside>
  );
}
