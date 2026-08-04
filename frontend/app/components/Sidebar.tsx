"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Role, SOURCE_SYSTEMS, getActor, getRole, setActor, setRole } from "@/lib/api";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

/**
 * minRole gates VISIBILITY only. The real enforcement is server-side in
 * rules.py -- hiding a nav link is a convenience, never a control.
 */
const NAV: { href: string; label: string; minRole: Role }[] = [
  { href: "/dashboard", label: "Dashboard", minRole: "viewer" },
  { href: "/rules", label: "Manage Rules", minRole: "owner" },
  { href: "/runs", label: "Runs", minRole: "owner" },
];

const RANK: Record<Role, number> = { viewer: 0, owner: 1, admin: 2 };

export default function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [role, setRoleState] = useState<Role>("admin");
  const [actor, setActorState] = useState("prabhat");
  const [source, setSourceState] = useState("Hybris");

  useEffect(() => {
    const saved = (localStorage.getItem("theme") as "light" | "dark") || null;
    const initial = saved || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    setTheme(initial);
    document.documentElement.setAttribute("data-theme", initial);
    setRoleState(getRole());
    setActorState(getActor());
    setSourceState(localStorage.getItem("source") || "Hybris");
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

  /** The dashboard reads this; a custom event avoids a full page reload. */
  function changeSource(next: string) {
    setSourceState(next);
    localStorage.setItem("source", next);
    window.dispatchEvent(new CustomEvent("dq-source", { detail: next }));
  }

  function changeRole(next: Role) {
    setRoleState(next);
    setRole(next);
    // a viewer standing on a page they can no longer see gets moved off it
    const current = NAV.find((n) => n.href === pathname);
    if (current && RANK[next] < RANK[current.minRole]) router.push("/dashboard");
    else router.refresh();
  }

  const visible = NAV.filter((n) => RANK[role] >= RANK[n.minRole]);

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="mark">DQ</span>
        <span>
          <div className="bt">Data Quality Framework</div>
        </span>
      </div>

      <div className="src-pick">
        <div className="nav-label">Data Source</div>
        <select value={source} onChange={(e) => changeSource(e.target.value)}>
          {SOURCE_SYSTEMS.map((s) => <option key={s}>{s}</option>)}
        </select>
      </div>

      <nav className="nav">
        <div className="nav-label">Workspace</div>
        {visible.map((item) => (
          <Link key={item.href} href={item.href} className={pathname === item.href ? "active" : ""}>
            {item.label}
          </Link>
        ))}
      </nav>

      <div className="side-foot">
        {/* Placeholder until Entra ID SSO lands -- then identity and role come
            from the token and this block disappears entirely. */}
        <div className="whoami">
          <div className="nav-label">Signed in as</div>
          <input className="txt sm" value={actor}
                 onChange={(e) => { setActorState(e.target.value); setActor(e.target.value); }} />
          <select className="sm" value={role} onChange={(e) => changeRole(e.target.value as Role)}>
            <option value="viewer">Viewer</option>
            <option value="owner">Owner</option>
            <option value="admin">Admin</option>
          </select>
          <div className="rolehint">
            {role === "viewer" && "Dashboard only"}
            {role === "owner" && "Can author rules & run"}
            {role === "admin" && "Can also approve rules"}
          </div>
        </div>

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
