import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import {
  Activity, ArrowDown, ArrowRight, Check, CheckCircle2, ChevronRight,
  CreditCard, Database, Eye, EyeOff, GitBranch, KeyRound, Languages,
  LayoutDashboard, Link2, LoaderCircle, LockKeyhole, LogIn, Mail,
  Moon, Network, PhoneCall, Route, ScrollText, ShieldCheck, Sun, X,
} from "lucide-react";
import { API } from "@/lib/utils";
import { useI18n } from "@/lib/i18n-context";
import "./PublicLanding.css";

gsap.registerPlugin(useGSAP);

const GITHUB_URL = "https://github.com/pxygit/SunnyRegister";
const COPY = {
  "zh-CN": {
    sub: "账户与自动化工作台", github: "查看 GitHub 开源仓库", language: "Switch to English",
    login: "登录", capabilities: "功能概览", workflow: "任务流程", eyebrow: "自托管 · 一体化账户工作台",
    title: "让账户工作流，", titleAccent: "有序运行。",
    lead: "从邮箱资源、自动注册到凭证维护与支付任务，SunnyRegister 将分散的操作汇聚成清晰、可追踪的工作流。",
    openConsole: "进入工作台", source: "查看源代码", privacy: "自托管部署", taskState: "任务状态可恢复", bilingual: "中英文 · 明暗主题",
    preview: "工作台示意", previewNote: "流程示意，非实时账户数据", previewTitle: "一条任务，完整视野",
    previewSub: "按账户查看进度与执行结果", previewMode: "按阶段执行", previewFlow: ["注册 / 登录", "手机号验证", "反代导入"],
    previewState: ["保存会话", "按需启用", "sub2api"], detailTitle: "账户与凭证", detailDesc: "Session、AT / RT、密码与 2FA 集中维护",
    logTitle: "执行记录", logs: ["配置执行方式与目标阶段", "按账户展示进度与日志", "保存已完成的阶段结果"],
    sectionEyebrow: "一个控制台，贯穿整个账户生命周期", sectionTitle: "任务有进度，资源有条理。",
    sectionDesc: "常用操作集中处理，关键状态随时可查。",
    features: [
      { title: "自动任务工作台", description: "批量注册或登录，按任务选择执行方式、并发与目标阶段。", tags: ["协议 / 浏览器", "阶段检查点", "实时进度"], label: "运行任务" },
      { title: "账户与凭证管理", description: "管理 Session、AT / RT、密码与 2FA，集中完成账户检测、续期与导出。", tags: ["Token 维护", "订阅检测", "邮箱换绑"], label: "维护账户" },
      { title: "Checkout 与支付", description: "从计划选择、批量提链到支付任务，在同一工作台跟进执行结果。", tags: ["多计划提链", "多支付渠道", "结果导出"], label: "管理支付" },
    ],
    resourcesTitle: "把所需资源连接起来", resourcesDesc: "独立配置，按任务需要使用。",
    resources: [
      ["邮箱与接码", "多邮箱渠道、邮件查询、自建号码池与外部接码服务。"],
      ["代理与路由", "用途标签、可用性检测、国家信息与流量统计。"],
      ["sub2api 联动", "配置远端分组、代理与导入参数，跟进导入结果。"],
      ["日志与审计", "按账户、类别与结果筛选，追踪任务和操作详情。"],
    ],
    flowEyebrow: "从配置到结果", flowTitle: "流程清楚，操作从容。",
    flowDesc: "为每次任务选择需要的阶段，已完成结果及时保存，后续可继续维护。",
    flow: [["配置资源", "准备邮箱、代理与可选接码渠道"], ["创建任务", "选择账户、执行方式与目标阶段"], ["观察进度", "查看分账户状态、日志与检查点"], ["管理结果", "维护凭证、导入反代或继续提链"]],
    finalTitle: "准备好开始下一项任务了吗？", finalDesc: "登录后进入你的 SunnyRegister 工作台。",
    footer: "自托管的账户、资源与支付工作流控制台", drawerTitle: "欢迎回到工作台", drawerDesc: "使用管理员凭据登录 SunnyRegister。",
    username: "用户名", password: "密码", submit: "登录工作台", checking: "正在验证…", failed: "登录失败，请检查用户名和密码",
    tooMany: "登录尝试过于频繁，请稍后再试", networkError: "暂时无法连接服务，请检查网络后重试", close: "关闭登录面板",
    protected: "受保护的管理入口", secureDesc: "登录成功后加载业务数据与管理操作。", logoutSuccess: "已安全退出登录",
    showPassword: "显示密码", hidePassword: "隐藏密码", dark: "切换深色主题", light: "切换浅色主题",
  },
  "en-US": {
    sub: "Account automation workspace", github: "View the GitHub repository", language: "切换到中文",
    login: "Sign in", capabilities: "Capabilities", workflow: "Workflow", eyebrow: "Self-hosted · Unified workspace",
    title: "Account workflows,", titleAccent: "beautifully in order.",
    lead: "From mailbox resources and registration to credentials and payment tasks, SunnyRegister brings every step into a clear, traceable workflow.",
    openConsole: "Open workspace", source: "View source", privacy: "Self-hosted", taskState: "Resumable task status", bilingual: "Two languages · Light & dark",
    preview: "Workspace preview", previewNote: "Illustration · Not live account data", previewTitle: "One task. The full picture.",
    previewSub: "Follow progress and results by account", previewMode: "Stage-based tasks", previewFlow: ["Register / sign in", "Phone verification", "Platform import"],
    previewState: ["Save session", "Optional", "sub2api"], detailTitle: "Accounts & credentials", detailDesc: "Manage Sessions, AT / RT, passwords and 2FA together",
    logTitle: "Activity", logs: ["Set an execution mode and target stage", "Follow progress and logs by account", "Save results at completed checkpoints"],
    sectionEyebrow: "One console for the account lifecycle", sectionTitle: "Clear tasks. Organized resources.",
    sectionDesc: "Keep everyday operations together and essential status within reach.",
    features: [
      { title: "Task workspace", description: "Register or sign in in batches, with execution modes, concurrency and stages you can choose.", tags: ["Protocol / browser", "Checkpoints", "Live progress"], label: "Run tasks" },
      { title: "Accounts & credentials", description: "Manage Sessions, AT / RT, passwords and 2FA alongside account checks, renewals and exports.", tags: ["Token maintenance", "Subscription checks", "Email changes"], label: "Maintain accounts" },
      { title: "Checkout & payments", description: "Follow plan selection, batch Checkout links and payment tasks from the same workspace.", tags: ["Multiple plans", "Payment channels", "Result exports"], label: "Manage payments" },
    ],
    resourcesTitle: "Connect the resources you need", resourcesDesc: "Configure independently. Use as needed.",
    resources: [
      ["Email & SMS", "Mailbox channels, mail queries, self-managed phone pools and SMS providers."],
      ["Proxies & routing", "Purpose tags, availability checks, country details and traffic statistics."],
      ["sub2api integration", "Set remote groups, proxies and import options, then follow the results."],
      ["Logs & audit", "Filter by account, category and result to inspect task and operation details."],
    ],
    flowEyebrow: "From setup to results", flowTitle: "A clear path through every task.",
    flowDesc: "Choose the stages each task needs. Save completed results and continue account maintenance afterward.",
    flow: [["Set up resources", "Prepare email, proxies and optional SMS"], ["Create a task", "Choose accounts, execution mode and stages"], ["Follow progress", "Review account status, logs and checkpoints"], ["Manage results", "Maintain credentials, import or create Checkout links"]],
    finalTitle: "Ready for your next task?", finalDesc: "Sign in to your SunnyRegister workspace.",
    footer: "A self-hosted workspace for accounts, resources and payments", drawerTitle: "Welcome to your workspace", drawerDesc: "Sign in to SunnyRegister with your administrator credentials.",
    username: "Username", password: "Password", submit: "Sign in to workspace", checking: "Verifying…", failed: "Sign-in failed. Check your username and password.",
    tooMany: "Too many sign-in attempts. Please try again later.", networkError: "Unable to connect. Check your network and try again.", close: "Close sign-in panel",
    protected: "Protected administration", secureDesc: "Business data and management actions load after sign-in.", logoutSuccess: "Signed out securely",
    showPassword: "Show password", hidePassword: "Hide password", dark: "Switch to dark theme", light: "Switch to light theme",
  },
} as const;

const FEATURE_ICONS = [LayoutDashboard, KeyRound, CreditCard];
const RESOURCE_ICONS = [Mail, Route, Network, ScrollText];
const FLOW_ICONS = [Mail, PhoneCall, Network];

type PublicLandingProps = {
  onLogin: () => void;
  logoutNotice?: boolean;
  onNoticeDone?: () => void;
  theme?: string;
  onToggleTheme?: () => void;
};

export default function PublicLanding({ onLogin, logoutNotice = false, onNoticeDone, theme, onToggleTheme }: PublicLandingProps) {
  const { language, toggleLanguage } = useI18n();
  const c = COPY[language];
  const rootRef = useRef<HTMLDivElement | null>(null);
  const drawerRef = useRef<HTMLElement | null>(null);
  const usernameRef = useRef<HTMLInputElement | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const closeDrawer = useCallback(() => {
    if (loading) return;
    setDrawerOpen(false);
    setError("");
    setUsername("");
    setPassword("");
    setShowPassword(false);
  }, [loading]);

  useGSAP(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    gsap.from(".sr-landing-hero-copy > *, .sr-landing-preview", {
      autoAlpha: 0, y: 18, duration: 0.65, stagger: 0.08, ease: "power3.out",
    });
  }, { scope: rootRef });

  useGSAP(() => {
    if (!drawerOpen || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    gsap.fromTo(".sr-landing-mask", { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.18 });
    gsap.fromTo(".sr-landing-drawer", { x: "100%" }, { x: 0, duration: 0.32, ease: "power3.out" });
  }, { scope: rootRef, dependencies: [drawerOpen], revertOnUpdate: true });

  useEffect(() => {
    if (!drawerOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    usernameRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      triggerRef.current?.focus();
    };
  }, [drawerOpen]);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDrawer();
      }
      if (event.key !== "Tab") return;
      const focusable = drawerRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), a[href], [tabindex='0']");
      if (!focusable?.length) {
        event.preventDefault();
        drawerRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === drawerRef.current)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [drawerOpen, closeDrawer]);

  useEffect(() => {
    if (!logoutNotice) return;
    const timer = window.setTimeout(() => onNoticeDone?.(), 3200);
    return () => window.clearTimeout(timer);
  }, [logoutNotice, onNoticeDone]);

  function openDrawer() {
    triggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setError("");
    setDrawerOpen(true);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!username.trim() || !password || loading) return;
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`${API}/auth/login`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) throw new Error(response.status === 429 ? c.tooMany : c.failed);
      setPassword("");
      onLogin();
    } catch (reason) {
      setPassword("");
      setError(reason instanceof TypeError ? c.networkError : reason instanceof Error && reason.message ? reason.message : c.failed);
    } finally { setLoading(false); }
  }

  return (
    <div ref={rootRef} className="sr-landing">
      {logoutNotice && <div className="sr-landing-toast" role="status"><CheckCircle2 aria-hidden="true" />{c.logoutSuccess}</div>}
      <div inert={drawerOpen}>
        <header className="sr-landing-nav sr-landing-container">
          <a className="sr-landing-brand" href="#top" aria-label="SunnyRegister">
            <span className="sr-landing-mark"><Link2 aria-hidden="true" /></span>
            <span><strong>SunnyRegister</strong><small>{c.sub}</small></span>
          </a>
          <nav className="sr-landing-links" aria-label={c.capabilities}><a href="#capabilities">{c.capabilities}</a><a href="#workflow">{c.workflow}</a></nav>
          <div className="sr-landing-nav-actions">
            <button type="button" className="sr-landing-tool" onClick={toggleLanguage} title={c.language} aria-label={c.language}><Languages aria-hidden="true" /></button>
            {onToggleTheme && <button type="button" className="sr-landing-tool" onClick={onToggleTheme} title={theme === "light" ? c.dark : c.light} aria-label={theme === "light" ? c.dark : c.light}>{theme === "light" ? <Moon aria-hidden="true" /> : <Sun aria-hidden="true" />}</button>}
            <button type="button" className="sr-landing-button sr-landing-button-small" onClick={openDrawer}><LogIn aria-hidden="true" />{c.login}</button>
          </div>
        </header>

        <main id="top">
          <section className="sr-landing-hero sr-landing-container" aria-labelledby="sr-landing-title">
            <div className="sr-landing-hero-copy">
              <span className="sr-landing-eyebrow"><span className="sr-landing-dot" />{c.eyebrow}</span>
              <h1 id="sr-landing-title">{c.title}<br /><em>{c.titleAccent}</em></h1>
              <p className="sr-landing-lead">{c.lead}</p>
              <div className="sr-landing-actions">
                <button type="button" className="sr-landing-button" onClick={openDrawer}>{c.openConsole}<ArrowRight aria-hidden="true" /></button>
                <a className="sr-landing-button sr-landing-button-secondary" href={GITHUB_URL} target="_blank" rel="noreferrer"><GitBranch aria-hidden="true" />{c.source}</a>
              </div>
              <ul className="sr-landing-traits"><li><ShieldCheck aria-hidden="true" />{c.privacy}</li><li><CheckCircle2 aria-hidden="true" />{c.taskState}</li><li><Sun aria-hidden="true" />{c.bilingual}</li></ul>
            </div>

            <figure className="sr-landing-preview">
              <div className="sr-landing-preview-bar"><span><LayoutDashboard aria-hidden="true" /> SunnyRegister</span><span className="sr-landing-preview-label">{c.preview}</span></div>
              <div className="sr-landing-preview-content">
                <div className="sr-landing-preview-title"><div><h2>{c.previewTitle}</h2><p>{c.previewSub}</p></div><Activity aria-hidden="true" /></div>
                <div className="sr-landing-preview-caption"><span>{c.previewMode}</span><span>WORKFLOW</span></div>
                <div className="sr-landing-preview-flow">
                  {c.previewFlow.map((label, index) => {
                    const Icon = FLOW_ICONS[index];
                    return <div key={label} className="sr-landing-preview-step"><Icon aria-hidden="true" /><strong>{label}</strong><small>{c.previewState[index]}</small>{index < 2 && <ChevronRight className="sr-landing-step-arrow" aria-hidden="true" />}</div>;
                  })}
                </div>
                <div className="sr-landing-preview-account"><span className="sr-landing-preview-icon"><KeyRound aria-hidden="true" /></span><div><strong>{c.detailTitle}</strong><p>{c.detailDesc}</p></div><Database aria-hidden="true" /></div>
                <div className="sr-landing-preview-logs"><strong><ScrollText aria-hidden="true" />{c.logTitle}</strong>{c.logs.map((log, index) => <div key={log}><span>{String(index + 1).padStart(2, "0")}</span><Check aria-hidden="true" /><span>{log}</span></div>)}</div>
              </div>
              <figcaption><span className="sr-landing-dot" />{c.previewNote}</figcaption>
            </figure>
          </section>

          <section id="capabilities" className="sr-landing-capabilities sr-landing-container" aria-labelledby="sr-capabilities-title">
            <div className="sr-landing-section-heading"><span className="sr-landing-kicker">{c.sectionEyebrow}</span><h2 id="sr-capabilities-title">{c.sectionTitle}</h2><p>{c.sectionDesc}</p></div>
            <div className="sr-landing-feature-grid">
              {c.features.map((feature, index) => {
                const Icon = FEATURE_ICONS[index];
                return <article className="sr-landing-feature" key={feature.title}><div className="sr-landing-feature-top"><span className="sr-landing-feature-icon"><Icon aria-hidden="true" /></span><span>{feature.label}</span></div><h3>{feature.title}</h3><p>{feature.description}</p><ul>{feature.tags.map(tag => <li key={tag}><Check aria-hidden="true" />{tag}</li>)}</ul></article>;
              })}
            </div>
            <div className="sr-landing-resource-heading"><h3>{c.resourcesTitle}</h3><p>{c.resourcesDesc}</p></div>
            <div className="sr-landing-resource-grid">{c.resources.map(([title, description], index) => { const Icon = RESOURCE_ICONS[index]; return <article className="sr-landing-resource" key={title}><Icon aria-hidden="true" /><h4>{title}</h4><p>{description}</p></article>; })}</div>
          </section>

          <section id="workflow" className="sr-landing-workflow" aria-labelledby="sr-workflow-title">
            <div className="sr-landing-container">
              <div className="sr-landing-section-heading"><span className="sr-landing-kicker">{c.flowEyebrow}</span><h2 id="sr-workflow-title">{c.flowTitle}</h2><p>{c.flowDesc}</p></div>
              <ol className="sr-landing-flow">{c.flow.map(([title, description], index) => <li key={title}><div className="sr-landing-flow-marker"><span>{String(index + 1).padStart(2, "0")}</span>{index < c.flow.length - 1 && <ArrowRight aria-hidden="true" />}</div><h3>{title}</h3><p>{description}</p></li>)}</ol>
            </div>
          </section>

          <section className="sr-landing-bottom sr-landing-container"><div><h2>{c.finalTitle}</h2><p>{c.finalDesc}</p></div><button type="button" className="sr-landing-button" onClick={openDrawer}>{c.openConsole}<ArrowRight aria-hidden="true" /></button></section>
        </main>
        <footer className="sr-landing-footer sr-landing-container"><div><strong>SunnyRegister</strong><span>{c.footer}</span></div><a href={GITHUB_URL} target="_blank" rel="noreferrer" aria-label={c.github}>GitHub <ArrowDown aria-hidden="true" /></a></footer>
      </div>

      {drawerOpen && <div className="sr-landing-mask" onMouseDown={event => { if (event.target === event.currentTarget) closeDrawer(); }}>
        <aside ref={drawerRef} className="sr-landing-drawer" role="dialog" aria-modal="true" aria-labelledby="sr-login-title" aria-describedby="sr-login-desc" tabIndex={-1}>
          <div className="sr-landing-drawer-header"><span className="sr-landing-brand"><span className="sr-landing-mark"><Link2 aria-hidden="true" /></span><strong>SunnyRegister</strong></span><button type="button" className="sr-landing-tool" onClick={closeDrawer} disabled={loading} title={c.close} aria-label={c.close}><X aria-hidden="true" /></button></div>
          <div className="sr-landing-drawer-body"><span className="sr-landing-login-symbol"><LockKeyhole aria-hidden="true" /></span><h2 id="sr-login-title">{c.drawerTitle}</h2><p id="sr-login-desc">{c.drawerDesc}</p>
            <form onSubmit={submit} className="sr-landing-login-form" aria-busy={loading}>
              <div><label htmlFor="sr-login-username">{c.username}</label><input id="sr-login-username" ref={usernameRef} value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" spellCheck={false} required readOnly={loading} aria-invalid={Boolean(error)} aria-describedby={error ? "sr-login-error" : undefined} /></div>
              <div><label htmlFor="sr-login-password">{c.password}</label><div className="sr-landing-password"><input id="sr-login-password" type={showPassword ? "text" : "password"} value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" required readOnly={loading} aria-invalid={Boolean(error)} aria-describedby={error ? "sr-login-error" : undefined} /><button type="button" className="sr-landing-tool" onClick={() => setShowPassword(value => !value)} aria-label={showPassword ? c.hidePassword : c.showPassword} title={showPassword ? c.hidePassword : c.showPassword} aria-pressed={showPassword}>{showPassword ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}</button></div></div>
              {error && <p id="sr-login-error" className="sr-landing-login-error" role="alert">{error}</p>}
              <button type="submit" className="sr-landing-button sr-landing-submit" disabled={loading || !username.trim() || !password}>{loading ? <LoaderCircle className="sr-landing-spinner" aria-hidden="true" /> : <LogIn aria-hidden="true" />}{loading ? c.checking : c.submit}</button>
            </form>
          </div>
          <div className="sr-landing-security"><ShieldCheck aria-hidden="true" /><div><strong>{c.protected}</strong><p>{c.secureDesc}</p></div></div>
        </aside>
      </div>}
    </div>
  );
}
