import { BrowserRouter, NavLink, useLocation } from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, Languages, Link2, LogOut, Menu, Moon, Sun } from "lucide-react";
import { API, cn } from "@/lib/utils";
import { I18nProvider, useI18n } from "@/lib/i18n-context";
import { useTopBarGsap } from "@/lib/useSunnyGsap";
import { CachedPage } from "@/lib/page-cache";
import { usePageScrollCache, useVisitedPageKeys } from "@/lib/page-cache-hooks";
import SunnyRegister, { clearSunnyRegisterTaskHistory } from "@/pages/SunnyRegister";
import PublicLanding from "@/pages/PublicLanding";
import AuditLogPage from "@/pages/AuditLogPage";
import CheckoutManager from "@/pages/CheckoutManager";
import PaymentManagement from "@/pages/PaymentManagement";

function words(language: string) {
  return language === "en-US"
    ? { app: "SunnyRegister", sub: "GPT account registration manager", home: "Studio", settings: "Settings", loginTitle: "Welcome back", loginDesc: "Enter your administrator credentials.", user: "Username", pass: "Password", submit: "Sign in", checking: "Checking...", failed: "Login failed", loading: "Loading...", logout: "Sign out" }
    : { app: "SunnyRegister", sub: "GPT 账号注册与管理", home: "工作台", settings: "设置", loginTitle: "欢迎回来", loginDesc: "请输入管理员账号与密码。", user: "用户名", pass: "密码", submit: "登录", checking: "验证中...", failed: "登录失败", loading: "加载中...", logout: "退出登录" };
}

function TopBar({ theme, setTheme, onLogout }: { theme: string; setTheme: (v: string) => void; onLogout: () => Promise<void> }) {
  const { language, toggleLanguage } = useI18n();
  const c = words(language);
  const location = useLocation();
  const headerRef = useRef<HTMLElement | null>(null);
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);
  const [openMenuPath, setOpenMenuPath] = useState<string | null>(null);
  const menuOpen = openMenuPath === location.pathname;
  useEffect(() => {
    if (!menuOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpenMenuPath(null);
      menuButtonRef.current?.focus();
    };
    const closeOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !headerRef.current?.contains(event.target)) setOpenMenuPath(null);
    };
    document.addEventListener("keydown", closeOnEscape);
    document.addEventListener("pointerdown", closeOutside);
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
      document.removeEventListener("pointerdown", closeOutside);
    };
  }, [menuOpen]);
  useTopBarGsap(headerRef, `${location.pathname}:${language}`);
  const menus = language === "en-US"
    ? [["/", "Workbench"], ["/mailbox", "Mailbox"], ["/phone", "SMS"], ["/sub2api", "Reverse"], ["/proxy", "Proxy"], ["/session", "Account Management"], ["/checkout", "Checkout Links"], ["/payments", "Payments"], ["/audit", "Audit Logs"]]
    : [["/", "工作台"], ["/mailbox", "邮箱配置"], ["/phone", "接码配置"], ["/sub2api", "反代配置"], ["/proxy", "代理配置"], ["/session", "账户管理"], ["/checkout", "提链管理"], ["/payments", "支付管理"], ["/audit", "日志管理"]];
  const isActive = (to: string) => to === "/" ? location.pathname === "/" : location.pathname.startsWith(to);
  const currentLabel = menus.find(([to]) => isActive(to))?.[1] || c.home;
  const themeLabel = language === "en-US" ? (theme === "light" ? "Switch to dark theme" : "Switch to light theme") : (theme === "light" ? "切换深色主题" : "切换浅色主题");
  const languageLabel = language === "en-US" ? "切换到中文" : "Switch to English";
  const navigationLabel = language === "en-US" ? "Main navigation" : "主导航";
  return (
    <header ref={headerRef} className="app-topbar">
      <div className="app-shell app-topbar-inner">
        <NavLink to="/" className="app-brand" aria-label={c.app} onClick={() => setOpenMenuPath(null)}>
          <span className="brand-mark"><Link2 className="h-5 w-5" aria-hidden="true" /></span>
          <span className="app-brand-copy"><strong>{c.app}</strong><small>{c.sub}</small></span>
        </NavLink>
        <button ref={menuButtonRef} type="button" className="app-mobile-menu" aria-label={navigationLabel} aria-expanded={menuOpen} aria-controls="app-navigation" onClick={() => setOpenMenuPath(menuOpen ? null : location.pathname)}>
          <Menu aria-hidden="true" /><span>{currentLabel}</span><ChevronDown aria-hidden="true" />
        </button>
        <nav id="app-navigation" aria-label={navigationLabel} className={cn("app-navigation", menuOpen && "is-open")}>
          {menus.map(([to, label]) => {
            const active = isActive(to);
            return <NavLink key={to} to={to} end={to === "/"} data-sunny-nav-active={active ? "true" : undefined} className={cn("app-nav-link", active && "is-active")} onClick={() => setOpenMenuPath(null)}>{label}</NavLink>;
          })}
        </nav>
        <div className="app-topbar-actions">
          <button type="button" className="round-tool" onClick={() => setTheme(theme === "light" ? "dark" : "light")} title={themeLabel} aria-label={themeLabel}>{theme === "light" ? <Moon className="h-4 w-4" aria-hidden="true" /> : <Sun className="h-4 w-4" aria-hidden="true" />}</button>
          <button type="button" className="round-tool px-3 text-xs font-bold" onClick={toggleLanguage} title={languageLabel} aria-label={languageLabel}><Languages className="h-4 w-4" aria-hidden="true" />{language === "zh-CN" ? "中" : "EN"}</button>
          <button type="button" className="round-tool" title={c.logout} aria-label={c.logout} onClick={onLogout}><LogOut className="h-4 w-4" aria-hidden="true" /></button>
        </div>
      </div>
    </header>
  );
}

type ShellPage = "sunny" | "checkout" | "payments" | "audit";

function shellPage(pathname: string): ShellPage {
  if (pathname.startsWith("/audit")) return "audit";
  if (pathname.startsWith("/checkout")) return "checkout";
  if (pathname.startsWith("/payments")) return "payments";
  return "sunny";
}

function menuPage(pathname: string) {
  const segment = pathname.split("/").filter(Boolean)[0];
  return segment || "workbench";
}

function CachedShellPages() {
  const location = useLocation();
  const activePage = shellPage(location.pathname);
  const visitedPages = useVisitedPageKeys(activePage);
  usePageScrollCache(menuPage(location.pathname));

  return (
    <main id="app-content" tabIndex={-1} className="app-shell app-content mx-auto py-6">
      <CachedPage active={activePage === "sunny"}>{visitedPages.has("sunny") && <SunnyRegister />}</CachedPage>
      <CachedPage active={activePage === "checkout"}>{visitedPages.has("checkout") && <CheckoutManager />}</CachedPage>
      <CachedPage active={activePage === "payments"}>{visitedPages.has("payments") && <PaymentManagement />}</CachedPage>
      <CachedPage active={activePage === "audit"}>{visitedPages.has("audit") && <AuditLogPage />}</CachedPage>
    </main>
  );
}

function Shell({ theme, setTheme, onLogout }: { theme: string; setTheme: (v: string) => void; onLogout: () => Promise<void> }) {
  const { language } = useI18n();
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-[var(--bg-base)]">
        <a href="#app-content" className="app-skip-link">{language === "en-US" ? "Skip to content" : "跳转到页面内容"}</a>
        <TopBar theme={theme} setTheme={setTheme} onLogout={onLogout} />
        <CachedShellPages />
      </div>
    </BrowserRouter>
  );
}

function AppContent() {
  const { language } = useI18n();
  const c = words(language);
  const [theme, setTheme] = useState(() => localStorage.getItem("theme") === "dark" ? "dark" : "light");
  const [authState, setAuthState] = useState<"loading" | "open" | "locked" | "authed">("loading");
  const [logoutNotice, setLogoutNotice] = useState(false);
  useEffect(() => {
    document.documentElement.classList.toggle("light", theme === "light");
    document.documentElement.classList.toggle("dark", theme === "dark");
    localStorage.setItem("theme", theme);
  }, [theme]);
  useEffect(() => { fetch(API + "/auth/check", { credentials: "include", cache: "no-store" }).then((r) => r.json()).then((data) => { if (!data.required) setAuthState("open"); else if (data.authenticated) setAuthState("authed"); else setAuthState("locked"); }).catch(() => setAuthState("locked")); }, []);
  const logout = useCallback(async () => {
    let completed = false;
    try {
      const response = await fetch(API + "/auth/logout", { method: "POST", credentials: "include", cache: "no-store" });
      completed = response.ok;
    } finally {
      window.history.replaceState(null, "", "/");
      setAuthState("locked");
      setLogoutNotice(completed);
    }
  }, []);
  if (authState === "loading") return <div className="flex h-screen items-center justify-center bg-[var(--bg-base)] text-sm text-[var(--text-muted)]">{c.loading}</div>;
  if (authState === "locked") return <PublicLanding theme={theme} onToggleTheme={() => setTheme(theme === "light" ? "dark" : "light")} onLogin={() => { clearSunnyRegisterTaskHistory(); setLogoutNotice(false); setAuthState("authed"); }} logoutNotice={logoutNotice} onNoticeDone={() => setLogoutNotice(false)} />;
  return <Shell theme={theme} setTheme={setTheme} onLogout={logout} />;
}

export default function App() { return <I18nProvider><AppContent /></I18nProvider>; }
