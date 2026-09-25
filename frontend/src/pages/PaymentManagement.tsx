import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import {
  Activity, BadgeDollarSign, CheckCircle2, CircleAlert, CircleDollarSign, Clock3, CreditCard,
  KeyRound, LayoutDashboard, ListRestart, Loader2, MessageSquareText, Phone,
  Plus, RefreshCw, Search, Settings2, ShieldCheck, Smartphone, Trash2, UserRound,
  UsersRound, WalletCards, X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiFetch, cn } from "@/lib/utils";
import { useI18n } from "@/lib/i18n-context";
import DirectCardPayment from "@/pages/payments/DirectCardPayment";
import MomoPayment from "@/pages/payments/MomoPayment";
import "@/styles/module-polish.css";

type Row = Record<string, any>;
type GoPayView = "overview" | "register" | "pool" | "accounts" | "payment" | "settings";
type PaymentMethod = "gopay" | "momo" | "paypal" | "direct_card";
type RunAction = (key: string, action: () => Promise<any>, success: string, refresh?: () => Promise<void>) => Promise<boolean>;

const api = (path: string, options?: RequestInit) => apiFetch(`/payments/gopay${path}`, options);
const post = (path: string, body: Row = {}) => api(path, { method: "POST", body: JSON.stringify(body) });

function formatTime(value: unknown) {
  if (!value) return "-";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function statusLabel(value: unknown) {
  const key = String(value || "unknown");
  return ({ queued: "排队中", running: "进行中", awaiting_otp: "等待 OTP", validating_otp: "验证 OTP", awaiting_captcha: "等待验证", interrupted_unknown: "交易待核对", success_unreconciled: "成功·待收尾", fraud_denied: "风控拒绝", completed: "已完成", success: "成功", failed: "失败", cancelled: "已取消", cancelling: "取消中", done: "已完成", available: "可用", registered: "已注册", already_registered: "已存在", missing: "未设置", unknown: "未检测" } as Record<string, string>)[key] || key;
}

function Status({ value }: { value: unknown }) {
  const key = String(value || "unknown");
  return <span className={cn("gopay-status", `is-${key}`)}>{statusLabel(key)}</span>;
}

function Panel({ title, action, children, className }: { title: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={cn("gopay-panel", className)}><header><h3>{title}</h3>{action}</header>{children}</section>;
}

function Empty({ title, detail }: { title: string; detail?: string }) {
  return <div className="gopay-empty"><WalletCards /><strong>{title}</strong>{detail && <span>{detail}</span>}</div>;
}

function Modal({ title, subtitle, onClose, children }: { title: string; subtitle?: string; onClose: () => void; children: ReactNode }) {
  return <div className="gopay-modal-mask" role="presentation" onMouseDown={onClose}><section className="gopay-modal" role="dialog" aria-modal="true" aria-label={title} onMouseDown={(event) => event.stopPropagation()}><header><div><h3>{title}</h3>{subtitle && <p>{subtitle}</p>}</div><button className="round-tool" type="button" title="关闭" aria-label="关闭" onClick={onClose}><X className="h-4 w-4" /></button></header>{children}</section></div>;
}

export default function PaymentManagement() {
  const { language } = useI18n();
  const [method, setMethod] = useState<PaymentMethod>("gopay");
  const [view, setView] = useState<GoPayView>("overview");
  const [accounts, setAccounts] = useState<Row[]>([]);
  const [phones, setPhones] = useState<Row[]>([]);
  const [registerJobs, setRegisterJobs] = useState<Row[]>([]);
  const [paymentJobs, setPaymentJobs] = useState<Row[]>([]);
  const [paypalJobs, setPaypalJobs] = useState<Row[]>([]);
  const [paypalConfig, setPaypalConfig] = useState<Row>({});
  const [sms, setSms] = useState<Row>({});
  const [captcha, setCaptcha] = useState<Row>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState<{ type: "ok" | "error"; text: string } | null>(null);
  const [logJob, setLogJob] = useState<Row | null>(null);
  const [pinAccount, setPinAccount] = useState<Row | null>(null);
  const [selectedPaymentId, setSelectedPaymentId] = useState("");
  const [poolSearch, setPoolSearch] = useState("");
  const [accountSearch, setAccountSearch] = useState("");
  const [paymentFilter, setPaymentFilter] = useState("");

  const toast = useCallback((text: string, type: "ok" | "error" = "ok") => {
    setNotice({ text, type });
    window.setTimeout(() => setNotice((current) => current?.text === text ? null : current), 3200);
  }, []);

  const loadAccounts = useCallback(async () => { const data = await api("/accounts"); setAccounts(data.accounts || []); }, []);
  const loadPhones = useCallback(async () => { const data = await api("/phone-pool"); setPhones(data.phones || []); }, []);
  const loadJobs = useCallback(async () => {
    const [registerData, paymentData] = await Promise.all([api("/register-jobs"), api("/payment-jobs")]);
    setRegisterJobs(registerData.jobs || []);
    setPaymentJobs(paymentData.jobs || []);
  }, []);
  const loadSms = useCallback(async () => { setSms(await api("/sms-status")); }, []);
  const loadCaptcha = useCallback(async () => { setCaptcha(await api("/captcha-status")); }, []);
  const loadPaypal = useCallback(async () => {
    const [jobs, config] = await Promise.all([api("/paypal-jobs"), api("/paypal-config")]);
    setPaypalJobs(jobs.jobs || []);
    setPaypalConfig(config || {});
  }, []);
  const refreshAll = useCallback(async (showToast = false) => {
    try {
      await Promise.all([loadAccounts(), loadPhones(), loadJobs(), loadSms(), loadCaptcha(), loadPaypal()]);
      if (showToast) toast("GoPay 数据已刷新");
    } catch (error) { toast(error instanceof Error ? error.message : "GoPay 数据加载失败", "error"); }
    finally { setLoading(false); }
  }, [loadAccounts, loadPhones, loadJobs, loadSms, loadCaptcha, loadPaypal, toast]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void refreshAll(); }, 0);
    return () => window.clearTimeout(timer);
  }, [refreshAll]);
  useEffect(() => {
    const timer = window.setInterval(() => { void Promise.all([loadJobs(), loadPaypal()]).catch(() => undefined); }, 3000);
    return () => window.clearInterval(timer);
  }, [loadJobs, loadPaypal]);

  const run = useCallback(async (key: string, action: () => Promise<any>, success: string, refresh?: () => Promise<void>) => {
    setBusy(key);
    try { await action(); toast(success); if (refresh) await refresh(); return true; }
    catch (error) { toast(error instanceof Error ? error.message : "操作失败", "error"); return false; }
    finally { setBusy(""); }
  }, [toast]);

  const selectedPayment = paymentJobs.find((job) => String(job.id) === selectedPaymentId) || null;
  const stats = useMemo(() => {
    const jobs = [...registerJobs, ...paymentJobs];
    return {
      accounts: accounts.length,
      phones: phones.length,
      otp: jobs.filter((job) => job.status === "waiting_otp").length,
      running: jobs.filter((job) => job.status === "running").length,
    };
  }, [accounts, phones, registerJobs, paymentJobs]);

  const nav: Array<[GoPayView, string, ReactNode]> = [
    ["overview", "总览", <LayoutDashboard />], ["register", "注册与登录", <UsersRound />],
    ["pool", "号码池", <Smartphone />], ["accounts", "GoPay 账号", <WalletCards />],
    ["payment", "支付中心", <BadgeDollarSign />], ["settings", "系统配置", <Settings2 />],
  ];

  return <div className="payment-management">
    {notice && <div className={cn("gopay-toast", notice.type === "error" && "is-error")} role={notice.type === "error" ? "alert" : "status"}>{notice.type === "error" ? <CircleAlert /> : <CheckCircle2 />}{notice.text}</div>}
    <header className="payment-heading">
      <div><h1>{language === "en-US" ? "Payment Management" : "支付管理"}</h1><p>{language === "en-US" ? "Manage payment channels and their operational workflows." : "统一管理支付渠道与相关业务流程。"}</p></div>
      <Button variant="outline" onClick={() => void refreshAll(true)} disabled={loading || busy !== ""}><RefreshCw className={cn("mr-2 h-4 w-4", loading && "animate-spin")} />刷新数据</Button>
    </header>

    <nav className="payment-method-tabs" aria-label="支付方式">
      <button className={method === "gopay" ? "active" : ""} type="button" aria-pressed={method === "gopay"} onClick={() => setMethod("gopay")}><span className="gopay-method-mark">Go</span><span><strong>GoPay 支付</strong><small>Indonesia</small></span></button>
      <button className={method === "momo" ? "active" : ""} type="button" aria-pressed={method === "momo"} onClick={() => setMethod("momo")}><span className="momo-method-mark">M</span><span><strong>MOMO 支付</strong><small>Vietnam OAICS</small></span></button>
      <button className={method === "paypal" ? "active" : ""} type="button" aria-pressed={method === "paypal"} onClick={() => setMethod("paypal")}><CreditCard /><span><strong>PayPal 支付</strong><small>Billing Agreement</small></span></button>
      <button className={method === "direct_card" ? "active" : ""} type="button" aria-pressed={method === "direct_card"} onClick={() => setMethod("direct_card")}><span className="direct-card-method-mark"><CreditCard /></span><span><strong>直卡协议</strong><small>Card Protocol</small></span></button>
    </nav>

    <div className={cn("gopay-workspace", method !== "gopay" && "paypal-workspace")}>
      {method === "gopay" && <nav className="gopay-nav" aria-label="GoPay 功能">
        {nav.map(([key, label, icon]) => <button key={key} type="button" className={view === key ? "active" : ""} aria-pressed={view === key} onClick={() => setView(key)}>{icon}<span>{label}</span></button>)}
      </nav>}
      <main className="gopay-content">
        {method === "gopay" && <section className="gopay-summary" aria-label="GoPay 实时概览">
          <div><span><UserRound />GoPay 账号</span><strong>{stats.accounts}</strong></div>
          <div><span><Phone />号码池</span><strong>{stats.phones}</strong></div>
          <div><span><MessageSquareText />等待 OTP</span><strong>{stats.otp}</strong></div>
          <div><span><Activity />进行中</span><strong>{stats.running}</strong></div>
        </section>}

        {loading ? <div className="gopay-loading"><Loader2 className="animate-spin" />正在加载支付模块...</div> : <>
          {method === "momo" ? <MomoPayment /> : method === "paypal" ? <PayPalView key={`${paypalConfig.country || ""}-${paypalConfig.buyer_mode || ""}`} jobs={paypalJobs} config={paypalConfig} busy={busy} run={run} refresh={loadPaypal} /> : method === "direct_card" ? <DirectCardPayment /> : <>
            {view === "overview" && <Overview registerJobs={registerJobs} paymentJobs={paymentJobs} onView={setView} />}
            {view === "register" && <RegisterView jobs={registerJobs} busy={busy} run={run} refresh={loadJobs} onLogs={setLogJob} />}
            {view === "pool" && <PoolView phones={phones} search={poolSearch} setSearch={setPoolSearch} busy={busy} run={run} refresh={loadPhones} />}
            {view === "accounts" && <AccountsView accounts={accounts} search={accountSearch} setSearch={setAccountSearch} busy={busy} run={run} refresh={loadAccounts} onPin={setPinAccount} onRegister={() => setView("register")} />}
            {view === "payment" && <PaymentView accounts={accounts} jobs={paymentJobs} filter={paymentFilter} setFilter={setPaymentFilter} selected={selectedPayment} select={setSelectedPaymentId} busy={busy} run={run} refresh={loadJobs} onLogs={setLogJob} />}
            {view === "settings" && <SettingsView sms={sms} captcha={captcha} busy={busy} run={run} refresh={async () => { await Promise.all([loadSms(), loadCaptcha()]); }} />}
          </>}
        </>}
      </main>
    </div>
    {logJob && <TaskLogModal job={logJob} onClose={() => setLogJob(null)} />}
    {pinAccount && <PinModal account={pinAccount} busy={busy} run={run} refresh={loadAccounts} onClose={() => setPinAccount(null)} />}
  </div>;
}

function Overview({ registerJobs, paymentJobs, onView }: { registerJobs: Row[]; paymentJobs: Row[]; onView: (view: GoPayView) => void }) {
  const activity: Row[] = [...registerJobs.map((row): Row => ({ ...row, kind: row.login_existing ? "登录" : "注册" })), ...paymentJobs.map((row): Row => ({ ...row, kind: "支付" }))]
    .sort((a, b) => String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || ""))).slice(0, 8);
  return <div className="gopay-view"><div className="gopay-section-title"><div><h2>运行总览</h2><p>账号、号码与任务的实时状态</p></div></div><div className="gopay-two-column">
    <Panel title="最近活动">{activity.length ? <div className="gopay-activity">{activity.map((job, index) => <div key={`${job.kind}-${job.id || index}`}><Clock3 /><span><strong>{job.kind} · {job.phone || job.id || "-"}</strong><small>{job.message || "等待状态更新"}</small></span><Status value={job.status} /></div>)}</div> : <Empty title="暂无任务" detail="从注册与登录或支付中心创建任务" />}</Panel>
    <Panel title="快速操作"><div className="gopay-quick-actions"><button onClick={() => onView("register")}><UsersRound /><span><strong>注册或登录账号</strong><small>创建单个或批量任务</small></span></button><button onClick={() => onView("pool")}><Smartphone /><span><strong>导入号码</strong><small>维护手机号与短信接口</small></span></button><button onClick={() => onView("accounts")}><WalletCards /><span><strong>管理 GoPay 账号</strong><small>余额、PIN 与登录状态</small></span></button><button onClick={() => onView("payment")}><BadgeDollarSign /><span><strong>发起 Midtrans 支付</strong><small>处理 OTP 与付款流程</small></span></button></div></Panel>
  </div></div>;
}

function RegisterView({ jobs, busy, run, refresh, onLogs }: { jobs: Row[]; busy: string; run: RunAction; refresh: () => Promise<void>; onLogs: (job: Row) => void }) {
  const [mode, setMode] = useState("register");
  const [changePin, setChangePin] = useState(false);
  const [proxies, setProxies] = useState("");
  const [proxyResult, setProxyResult] = useState<Row | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const pin = String(data.get("pin") || "").trim();
    const newPin = String(data.get("new_pin") || "").trim();
    const confirmPin = String(data.get("confirm_pin") || "").trim();
    if (!/^\d{6}$/.test(pin)) return void window.alert("PIN 必须是 6 位数字");
    if (changePin && (!/^\d{6}$/.test(newPin) || newPin !== confirmPin || newPin === pin)) return void window.alert("请检查新 PIN，两次输入必须一致且不能与原 PIN 相同");
    await run("register", () => post("/batch-register", { source: data.get("source"), count: Number(data.get("count")), workers: Number(data.get("workers")), pin, login_existing: mode === "login", change_pin_after_login: mode === "login" && changePin, new_pin: newPin, proxies: proxies.split(/\r?\n/).map((item) => item.trim()).filter(Boolean) }), "批量任务已创建", refresh);
  }
  return <div className="gopay-view"><div className="gopay-section-title"><div><h2>注册与登录</h2><p>创建批量任务并处理短信验证码</p></div></div>
    <Panel title="新建批量任务"><form className="gopay-form" onSubmit={submit}><div className="gopay-form-grid">
      <label><span>号码来源</span><select name="source"><option value="pool">号码池</option><option value="smsbower">SMSBower</option><option value="smspool">SMSPool</option></select></label>
      <label><span>任务模式</span><select value={mode} onChange={(e) => { setMode(e.target.value); setChangePin(false); }}><option value="register">注册新号</option><option value="login">登录已有号</option></select></label>
      <label><span>数量</span><input name="count" type="number" min="1" max="500" defaultValue="1" /></label>
      <label><span>线程数</span><input name="workers" type="number" min="1" max="50" defaultValue="2" /></label>
      <label><span>{mode === "login" ? "原 PIN" : "设置新 PIN"}</span><input name="pin" type="password" inputMode="numeric" maxLength={6} autoComplete="off" placeholder="6 位数字" /></label>
      {mode === "login" && <label className="gopay-check wide"><input type="checkbox" checked={changePin} onChange={(e) => setChangePin(e.target.checked)} /><span><strong>登录后修改已有 PIN</strong><small>仅适用于已知原 PIN 的账号</small></span></label>}
      {mode === "login" && changePin && <><label><span>新 PIN</span><input name="new_pin" type="password" inputMode="numeric" maxLength={6} autoComplete="off" /></label><label><span>确认新 PIN</span><input name="confirm_pin" type="password" inputMode="numeric" maxLength={6} autoComplete="off" /></label></>}
      <label className="wide"><span>代理池（可选）</span><textarea value={proxies} onChange={(e) => { setProxies(e.target.value); setProxyResult(null); }} rows={4} placeholder="每行一条 HTTP、HTTPS 或 SOCKS5 代理" /><span className="gopay-field-foot"><small>{proxyResult ? `可用 ${proxyResult.available}/${proxyResult.total}` : "最多 100 条，空行与重复项自动忽略"}</small><Button type="button" size="sm" variant="outline" disabled={!proxies.trim() || busy !== ""} onClick={() => void run("proxy", async () => setProxyResult(await post("/proxies/check", { proxies: proxies.split(/\r?\n/) })), "代理检测完成")}><ShieldCheck className="mr-1 h-3.5 w-3.5" />检测代理</Button></span></label>
    </div><Button type="submit" disabled={busy !== ""}>{busy === "register" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Plus className="mr-2 h-4 w-4" />}开始批量任务</Button></form></Panel>
    <Panel title="最近任务" action={<Button size="sm" variant="outline" disabled={busy !== ""} onClick={() => void run("clear-register", () => post("/tasks/clear-finished", { scope: "register" }), "已清理注册历史", refresh)}><ListRestart className="mr-1 h-3.5 w-3.5" />清理已结束</Button>}><div className="gopay-table-wrap"><table><thead><tr><th>任务 ID</th><th>手机号</th><th>来源</th><th>模式</th><th>状态</th><th>消息</th><th>验证码</th><th>操作</th></tr></thead><tbody>{jobs.length ? jobs.map((job) => <tr key={job.id}><td className="mono">{job.id}</td><td>{job.phone || "-"}</td><td>{job.source || "-"}</td><td>{job.login_existing ? "登录" : "注册"}</td><td><Status value={job.status} /></td><td className="gopay-message">{job.message || "-"}</td><td>{job.status === "waiting_otp" ? <OtpSubmit onSubmit={(code) => run(`reg-otp-${job.id}`, () => post(`/register-jobs/${encodeURIComponent(job.id)}/otp`, { code }), "验证码已提交", refresh)} /> : "-"}</td><td><Button size="sm" variant="ghost" onClick={() => onLogs(job)}>日志</Button></td></tr>) : <tr><td colSpan={8}><Empty title="暂无注册任务" /></td></tr>}</tbody></table></div></Panel>
  </div>;
}

function OtpSubmit({ onSubmit }: { onSubmit: (code: string) => void }) {
  const [code, setCode] = useState("");
  return <span className="gopay-otp-inline"><input value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} inputMode="numeric" placeholder="4-6 位" /><Button size="sm" disabled={!/^\d{4,6}$/.test(code)} onClick={() => onSubmit(code)}>提交</Button></span>;
}

const paypalCountries = ["BR", "GB", "US", "JP", "TH", "ID", "PH", "TW", "MX", "AE", "AU", "CA"];
const paypalCallingCodes: Record<string, string> = { BR: "+55", GB: "+44", US: "+1", JP: "+81", TH: "+66", ID: "+62", PH: "+63", TW: "+886", MX: "+52", AE: "+971", AU: "+61", CA: "+1" };

function PayPalView({ jobs, config, busy, run, refresh }: { jobs: Row[]; config: Row; busy: string; run: RunAction; refresh: () => Promise<void> }) {
  const [country, setCountry] = useState(String(config.country || "BR"));
  const [buyerMode, setBuyerMode] = useState(String(config.buyer_mode || "identity_elevation"));
  const [proxyPool, setProxyPool] = useState("");
  const [baToken, setBaToken] = useState("");
  const [phone, setPhone] = useState("");

  async function saveConfig() {
    await run("paypal-config", () => post("/paypal-config", { country, buyer_mode: buyerMode, proxy_pool: proxyPool }), "PayPal 配置已保存", refresh);
  }
  async function createTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await run("paypal-start", () => post("/paypal-jobs", { ba_token: baToken, phone, country, buyer_mode: buyerMode }), "PayPal 协议任务已创建", refresh);
    setBaToken("");
    setPhone("");
  }
  return <div className="gopay-view paypal-view">
    <div className="gopay-section-title"><div><h2>PayPal 协议支付</h2><p>每个任务独立使用一个 BA 链接、手机号和代理出口，可同时运行多个任务</p></div><span className="paypal-active-count">{jobs.filter((job) => ["queued", "running", "awaiting_otp", "awaiting_captcha", "cancelling"].includes(String(job.status))).length} 个运行中</span></div>
    <div className="gopay-two-column paypal-config-layout">
      <Panel title="协议支付配置"><form className="gopay-form" onSubmit={(event) => { event.preventDefault(); void saveConfig(); }}><div className="gopay-form-grid"><label><span>账单国家</span><select value={country} onChange={(event) => setCountry(event.target.value)}>{paypalCountries.map((item) => <option key={item} value={item}>{item}</option>)}</select></label><label><span>Buyer 模式</span><select value={buyerMode} onChange={(event) => setBuyerMode(event.target.value)}><option value="identity_elevation">身份提升</option><option value="original">原始协议流程</option></select></label><label className="wide"><span>国家代理池</span><textarea value={proxyPool} onChange={(event) => setProxyPool(event.target.value)} rows={5} placeholder={config.proxy_count ? `已配置 ${config.proxy_count} 条代理，留空保持原配置` : "每行一条 host:port:user:password；需与账单国家匹配"} /></label></div><div className="gopay-field-foot"><small>{config.proxy_count ? `当前已配置 ${config.proxy_count} 条代理` : "尚未配置代理池"}</small><Button type="submit" size="sm" disabled={busy !== ""}><Settings2 className="mr-1 h-3.5 w-3.5" />保存配置</Button></div></form></Panel>
      <Panel title="创建协议任务"><form className="gopay-form" onSubmit={createTask}><label className="wide"><span>PayPal BA 链接或 Token</span><textarea value={baToken} onChange={(event) => setBaToken(event.target.value)} rows={3} required placeholder="https://www.paypal.com/agreements/approve?ba_token=BA-..." /></label><div className="gopay-form-grid"><label className="wide"><span>手机号（含国家码）</span><input value={phone} onChange={(event) => setPhone(event.target.value)} required placeholder={`${paypalCallingCodes[country] || "+"}...`} /></label></div><div className="gopay-warning"><ShieldCheck />每次提交都会创建独立任务；请为不同任务使用不同 BA 链接和手机号。</div><Button type="submit" disabled={busy !== ""}><Plus className="mr-2 h-4 w-4" />创建 PayPal 任务</Button></form></Panel>
    </div>
    <Panel title={`协议任务 · ${jobs.length}`} action={<Button size="sm" variant="outline" onClick={() => void refresh()}><RefreshCw className="mr-1 h-3.5 w-3.5" />刷新</Button>}>
      <div className="gopay-table-wrap"><table><thead><tr><th>任务 ID</th><th>BA 链接</th><th>手机号</th><th>国家</th><th>阶段</th><th>状态</th><th>消息</th><th>操作</th></tr></thead><tbody>
        {jobs.length ? (
          jobs.map((job) => <tr key={job.id}>
          <td className="mono">{job.id}</td><td className="mono">{job.ba_token || "-"}</td><td className="mono">{job.phone || "-"}</td><td>{job.country || "-"}</td><td>{job.stage || "-"}</td><td><Status value={job.status} /></td><td className="gopay-message">{job.error || job.awaiting_prompt || job.stage || "-"}</td>
          <td><div className="gopay-row-actions">
            {job.status === "awaiting_otp" && <OtpSubmit onSubmit={(code) => void run(`paypal-otp-${job.id}`, () => post(`/paypal-jobs/${encodeURIComponent(job.id)}/otp`, { value: code }), "PayPal 验证码已提交", refresh)} />}
            {["queued", "running", "awaiting_otp", "awaiting_captcha", "cancelling"].includes(String(job.status)) && <Button size="sm" variant="outline" onClick={() => void run(`paypal-cancel-${job.id}`, () => post(`/paypal-jobs/${encodeURIComponent(job.id)}/cancel`), "PayPal 任务已取消", refresh)}>取消</Button>}
          </div></td>
          </tr>)
        ) : (
          <tr><td colSpan={8}><Empty title="暂无 PayPal 协议任务" detail="配置国家代理池后，输入 BA 链接和手机号创建任务" /></td></tr>
        )}
      </tbody></table></div>
    </Panel>
  </div>;
}

function PoolView({ phones, search, setSearch, busy, run, refresh }: { phones: Row[]; search: string; setSearch: (value: string) => void; busy: string; run: RunAction; refresh: () => Promise<void> }) {
  const rows = phones.filter((row) => String(row.phone || "").includes(search.trim()));
  async function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); await run("pool-import", () => post("/phone-pool/import", { text: data.get("text") }), "号码已导入", refresh); form.reset(); }
  return <div className="gopay-view"><div className="gopay-section-title"><div><h2>号码池</h2><p>管理注册、重新登录与自动取码号码</p></div><Button size="sm" variant="destructive" disabled={!phones.length || busy !== ""} onClick={() => window.confirm("确定清空全部号码池吗？") && void run("pool-clear", () => post("/phone-pool/clear"), "号码池已清空", refresh)}><Trash2 className="mr-1 h-3.5 w-3.5" />清空</Button></div><div className="gopay-two-column pool-layout">
    <Panel title="导入号码"><form className="gopay-form" onSubmit={submit}><label><span>号码与短信接口</span><textarea name="text" rows={9} required placeholder={"+6281234567890----https://example.com/sms/123\n每行一个号码"} /></label><Button type="submit" disabled={busy !== ""}><Plus className="mr-2 h-4 w-4" />导入号码</Button></form></Panel>
    <Panel title={`号码明细 · ${rows.length}`}><div className="gopay-search"><Search /><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索手机号" /></div><div className="gopay-table-wrap"><table><thead><tr><th>手机号</th><th>短信接口</th><th>状态</th><th>操作</th></tr></thead><tbody>{rows.length ? rows.map((row) => <tr key={row.phone}><td className="mono">{row.phone}</td><td className="gopay-message">{row.sms_url || "-"}</td><td><Status value={row.status || "available"} /></td><td><Button size="sm" variant="ghost" title="删除号码" onClick={() => window.confirm(`确定删除 ${row.phone} 吗？`) && void run(`phone-${row.phone}`, () => post("/phone-pool/delete", { phone: row.phone }), "号码已删除", refresh)}><Trash2 className="h-4 w-4 text-red-500" /></Button></td></tr>) : <tr><td colSpan={4}><Empty title="号码池为空" /></td></tr>}</tbody></table></div></Panel>
  </div></div>;
}

function pinText(account: Row) {
  const setup = account.pin_setup_status || "unknown";
  const change = account.pin_change_status || "";
  let label = setup === "configured" ? "已设置" : setup === "missing" ? "未设置" : "未检测";
  if (change === "confirmed") label = "已修改并确认";
  else if (change === "changed_unconfirmed") label = "已修改，待确认";
  else if (change === "unverified") label = "已有 PIN，未验证";
  else if (["unknown", "setup_unknown"].includes(change)) label = "PIN 结果不确定";
  return `${label} · ${account.pin_saved ? "本机已保存" : "本机未保存"}`;
}

function AccountsView({ accounts, search, setSearch, busy, run, refresh, onPin, onRegister }: { accounts: Row[]; search: string; setSearch: (value: string) => void; busy: string; run: RunAction; refresh: () => Promise<void>; onPin: (row: Row) => void; onRegister: () => void }) {
  const needle = search.trim().toLowerCase();
  const rows = accounts.filter((row) => `${row.phone || ""} ${row.customer_id || ""}`.toLowerCase().includes(needle));
  return <div className="gopay-view"><div className="gopay-section-title"><div><h2>GoPay 账号</h2><p>余额、PIN、登录与短信激活状态</p></div><Button size="sm" variant="destructive" disabled={!accounts.length || busy !== ""} onClick={() => window.confirm("确定删除全部 GoPay 账号数据吗？") && void run("accounts-clear", () => post("/accounts/delete-all"), "账号数据已清空", refresh)}><Trash2 className="mr-1 h-3.5 w-3.5" />删除全部</Button></div>
    <Panel title={`账号列表 · ${rows.length}`} action={<Button size="sm" variant="outline" onClick={() => void refresh()}><RefreshCw className="mr-1 h-3.5 w-3.5" />刷新</Button>}><div className="gopay-search"><Search /><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索手机号或 Customer ID" /></div><div className="gopay-table-wrap"><table><thead><tr><th>手机号</th><th>本地号</th><th>余额</th><th>状态</th><th>PIN 状态</th><th>短信号码</th><th>Customer ID</th><th>操作</th></tr></thead><tbody>{rows.length ? rows.map((row) => <tr key={row.phone}><td className="mono">{row.phone}</td><td>{row.local || "-"}</td><td><strong>{Number(row.balance || 0).toLocaleString("zh-CN")} Rp</strong></td><td><Status value={row.use_status || "success"} /></td><td>{pinText(row)}</td><td>{row.sms_activation_label || "不可自动接码"}</td><td className="mono">{row.customer_id || "-"}</td><td><div className="gopay-row-actions"><Button size="sm" variant="outline" onClick={() => void run(`balance-${row.phone}`, () => post(`/accounts/${encodeURIComponent(row.phone)}/balance`), "余额已刷新", refresh)}>查余额</Button><Button size="sm" variant="outline" onClick={() => void run(`pin-${row.phone}`, () => post(`/accounts/${encodeURIComponent(row.phone)}/pin-status`), "PIN 状态已刷新", refresh)}>检测 PIN</Button><Button size="sm" variant="outline" onClick={() => onPin(row)}>修改 PIN</Button><Button size="sm" variant="outline" onClick={() => { const pin = window.prompt(`请输入 ${row.phone} 的原 PIN；没有 PIN 时请输入要设置的新 PIN`); if (pin && /^\d{6}$/.test(pin)) void run(`relogin-${row.phone}`, () => post(`/accounts/${encodeURIComponent(row.phone)}/relogin`, { pin }), "重新登录任务已创建", async () => { await refresh(); onRegister(); }); }}>重新登录</Button>{row.sms_activation_status === "active" && <Button size="sm" variant="outline" onClick={() => window.confirm("释放后将无法自动接收付款 OTP，确定继续吗？") && void run(`release-${row.phone}`, () => post(`/accounts/${encodeURIComponent(row.phone)}/release-sms`), "短信号码已释放", refresh)}>释放号码</Button>}<Button size="sm" variant="ghost" title="删除账号" onClick={() => window.confirm(`确定删除 ${row.phone} 吗？`) && void run(`delete-${row.phone}`, () => post(`/accounts/${encodeURIComponent(row.phone)}/delete`), "账号已删除", refresh)}><Trash2 className="h-4 w-4 text-red-500" /></Button></div></td></tr>) : <tr><td colSpan={8}><Empty title="暂无 GoPay 账号" detail="完成注册或登录后，账号会显示在这里" /></td></tr>}</tbody></table></div></Panel>
  </div>;
}

function paymentStage(job: Row) {
  const status = String(job.status || "");
  const phase = String(job.payment_phase || "");
  const text = `${(job.logs || []).map((row: Row) => row.message || "").join(" ")} ${job.message || ""}`;
  if (["success", "success_unreconciled"].includes(status) || job.charge_started_at || ["charge_started", "charged", "processing"].includes(phase)) return 4;
  if (/Step (?:9|1[0-4]):|Resume payment Step|charge challenge_ref=|Payment process OK|Transaction status:/.test(text)) return 4;
  if (/Step (?:6|7):|PIN verify \((?:MGUPA|GWC)\)|Step 7: validate-pin/.test(text)) return 3;
  if (["waiting_otp", "validating_otp"].includes(status) || /Step (?:4|5):|Waiting for OTP|OTP received/.test(text)) return 2;
  if (status === "awaiting_captcha" || /Step (?:1|2|3|8):|linking reference=|Linking complete|GoPay linked:/.test(text)) return 1;
  return 0;
}
const stageNames = ["检查账号", "绑定 Midtrans", "等待 OTP", "验证 PIN", "扣款结果"];

function PaymentView({ accounts, jobs, filter, setFilter, selected, select, busy, run, refresh, onLogs }: { accounts: Row[]; jobs: Row[]; filter: string; setFilter: (value: string) => void; selected: Row | null; select: (id: string) => void; busy: string; run: RunAction; refresh: () => Promise<void>; onLogs: (job: Row) => void }) {
  const rows = jobs.filter((row) => !filter || row.status === filter);
  const [ack, setAck] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); await run("payment", async () => { const result = await post("/payment", { phone: data.get("phone"), pin: data.get("pin"), proxy: data.get("proxy"), midtrans_url: data.get("midtrans_url") }); select(String(result.id || "")); }, "支付任务已创建", refresh); setAck(false); }
  const stage = selected ? paymentStage(selected) : 0;
  return <div className="gopay-view"><div className="gopay-section-title"><div><h2>支付中心</h2><p>绑定 GoPay 并完成 Midtrans 支付</p></div></div><div className="gopay-payment-layout"><div className="gopay-payment-main"><div className="gopay-two-column">
    <Panel title="发起支付任务"><form className="gopay-form" onSubmit={submit}><div className="gopay-form-grid"><label className="wide"><span>GoPay 账号</span><select name="phone" required><option value="">请选择账号</option>{accounts.map((row) => <option key={row.phone} value={row.phone}>{row.phone} · {Number(row.balance || 0).toLocaleString("zh-CN")} Rp</option>)}</select></label><label><span>付款 PIN（可选）</span><input name="pin" type="password" inputMode="numeric" maxLength={6} autoComplete="off" placeholder="留空使用已保存 PIN" /></label><label><span>代理（可选）</span><input name="proxy" placeholder="留空使用账号代理" /></label><label className="wide"><span>Midtrans 链接</span><input name="midtrans_url" type="url" required placeholder="https://app.midtrans.com/snap/v4/redirection/..." /></label></div><div className="gopay-warning"><ShieldCheck />此操作可能直接产生真实扣款，请核对账号、订单与金额。</div><label className="gopay-check"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /><span>我已核对订单并确认开始支付</span></label><Button type="submit" disabled={!ack || busy !== ""} className="w-full"><CircleDollarSign className="mr-2 h-4 w-4" />确认并开始支付</Button></form></Panel>
    <Panel title="支付流程" action={selected && <Status value={selected.status} />}><div className="gopay-flow">{stageNames.map((name, index) => <div key={name} className={cn(index <= stage && "active")}><span>{index + 1}</span><small>{name}</small></div>)}</div><div className="gopay-flow-copy"><strong>{selected ? stageNames[stage] : "准备开始"}</strong><span>{selected?.message || "创建或选择任务后显示当前阶段"}</span></div></Panel>
  </div><Panel title="支付任务" action={<div className="gopay-panel-actions"><select value={filter} onChange={(e) => setFilter(e.target.value)}><option value="">全部状态</option><option value="running">进行中</option><option value="awaiting_captcha">等待验证</option><option value="waiting_otp">等待 OTP</option><option value="interrupted_unknown">交易待核对</option><option value="success">成功</option><option value="success_unreconciled">成功·待收尾</option><option value="failed">失败</option></select><Button size="sm" variant="outline" onClick={() => void refresh()}><RefreshCw className="h-3.5 w-3.5" /></Button><Button size="sm" variant="outline" onClick={() => void run("clear-payment", () => post("/tasks/clear-finished", { scope: "payment" }), "支付历史已清理", refresh)}><ListRestart className="mr-1 h-3.5 w-3.5" />清理已结束</Button></div>}><div className="gopay-table-wrap"><table><thead><tr><th>任务 ID</th><th>手机号</th><th>当前阶段</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody>{rows.length ? rows.map((job) => <tr key={job.id} className={String(job.id) === String(selected?.id) ? "selected" : ""} onClick={() => select(String(job.id))}><td className="mono">{job.id}</td><td>{job.phone || "-"}</td><td>{stageNames[paymentStage(job)]}</td><td><Status value={job.status} /></td><td>{formatTime(job.updated_at)}</td><td><Button size="sm" variant="ghost" onClick={(event) => { event.stopPropagation(); onLogs(job); }}>日志</Button></td></tr>) : <tr><td colSpan={6}><Empty title="暂无支付任务" /></td></tr>}</tbody></table></div></Panel></div>
    <PaymentDetail job={selected} run={run} refresh={refresh} />
  </div></div>;
}

function PaymentDetail({ job, run, refresh }: { job: Row | null; run: RunAction; refresh: () => Promise<void> }) {
  if (!job) return <aside className="gopay-panel gopay-payment-detail"><Empty title="选择支付任务" detail="任务详情、日志和 OTP 输入会显示在这里" /></aside>;
  const meta = job.midtrans_meta || job.meta || job.metadata || job.result || {};
  return <aside className="gopay-panel gopay-payment-detail"><header><h3>任务详情</h3><Status value={job.status} /></header><div className="gopay-detail"><dl><dt>任务 ID</dt><dd className="mono">{job.id}</dd><dt>手机号</dt><dd>{job.phone || "-"}</dd><dt>订单</dt><dd>{meta.order_id || "-"}</dd><dt>金额</dt><dd>{meta.gross_amount || "-"} {meta.currency || ""}</dd><dt>创建时间</dt><dd>{formatTime(job.created_at)}</dd><dt>更新时间</dt><dd>{formatTime(job.updated_at)}</dd></dl>{job.status === "waiting_otp" && <div className="gopay-otp-box"><KeyRound /><strong>输入支付 OTP</strong><p>验证码发送至 {job.phone}</p><OtpSubmit onSubmit={(code) => run(`payment-otp-${job.id}`, () => post(`/payment-jobs/${encodeURIComponent(job.id)}/otp`, { code }), "支付 OTP 已提交", refresh)} /></div>}<h4>流程日志</h4><ol className="gopay-logs">{(job.logs || []).map((entry: Row, index: number) => <li key={index}><time>{formatTime(entry.at)}</time><span>{entry.message || "-"}</span></li>)}</ol></div></aside>;
}

function SettingsView({ sms, captcha, busy, run, refresh }: { sms: Row; captcha: Row; busy: string; run: RunAction; refresh: () => Promise<void> }) {
  const [section, setSection] = useState<"sms" | "captcha">("sms");
  const [provider, setProvider] = useState<"smsbower" | "smspool">("smsbower");
  const [captchaSecrets, setCaptchaSecrets] = useState({ solverify: "", twocaptcha: "" });
  const current = provider === "smspool" ? (sms.providers?.smspool || {}) : (sms.providers?.smsbower || sms);
  const twocaptchaConfigured = Boolean(captcha.twocaptcha_api_key_configured);
  const captchaConfigured = Boolean(captcha.configured || captcha.solverify_api_key_configured || twocaptchaConfigured);

  async function submitSms(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    await run("sms", () => post("/sms-config", {
      provider,
      api_key: data.get("api_key"),
      api_base_url: data.get("api_base_url"),
      service: data.get("service"),
      country: data.get("country"),
      pool: data.get("pool"),
      max_price: data.get("max_price"),
    }), `${provider === "smspool" ? "SMSPool" : "SMSBower"} 配置已保存`, refresh);
  }

  async function submitCaptcha(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const saved = await run("captcha", () => post("/captcha-config", {
      api_key: captchaSecrets.twocaptcha,
      poll_sec: data.get("poll_sec"),
      timeout_sec: data.get("timeout_sec"),
      max_attempts: data.get("max_attempts"),
      solverify_api_key: captchaSecrets.solverify,
      solverify_poll_sec: data.get("solverify_poll_sec"),
      solverify_timeout_sec: data.get("solverify_timeout_sec"),
      scene_id: data.get("scene_id"),
      prefix: data.get("prefix"),
      region: data.get("region"),
      api_get_lib: data.get("api_get_lib"),
    }), "人机验证配置已保存", refresh);
    if (saved) setCaptchaSecrets({ solverify: "", twocaptcha: "" });
  }

  return <div className="gopay-view">
    <div className="gopay-section-title"><div><h2>系统配置</h2><p>按支付方式维护号码供应商与 GoPay 人机验证</p></div></div>
    <div className="gopay-segmented" role="tablist" aria-label="系统配置类别">
      <button type="button" className={section === "sms" ? "active" : ""} onClick={() => setSection("sms")}>短信供应商</button>
      <button type="button" className={section === "captcha" ? "active" : ""} onClick={() => setSection("captcha")}>人机验证</button>
    </div>
    {section === "sms" ? <>
      <div className="gopay-segmented" role="tablist" aria-label="短信供应商"><button type="button" className={provider === "smsbower" ? "active" : ""} onClick={() => setProvider("smsbower")}>SMSBower</button><button type="button" className={provider === "smspool" ? "active" : ""} onClick={() => setProvider("smspool")}>SMSPool</button></div>
      <Panel title={`${provider === "smspool" ? "SMSPool" : "SMSBower"} 配置`} className="gopay-settings-panel" action={<Status value={current.api_key_configured ? "success" : "failed"} />}>
        <form className="gopay-form" onSubmit={submitSms}><div className="gopay-form-grid"><label className="wide"><span>API Key</span><input name="api_key" type="password" autoComplete="off" placeholder={current.api_key_configured ? `已配置 ${current.api_key || ""}，留空保持不变` : "请输入 API Key"} /></label><label className="wide"><span>Base URL</span><input name="api_base_url" type="url" defaultValue={current.api_base_url || (provider === "smspool" ? "https://api.smspool.net" : "https://smsbower.page")} /></label><label><span>服务代码</span><input name="service" defaultValue={current.service || (provider === "smspool" ? "392" : "ni")} /></label><label><span>国家代码</span><input name="country" defaultValue={current.country || (provider === "smspool" ? "9" : "6")} /></label>{provider === "smspool" && <><label><span>号码池（可选）</span><input name="pool" defaultValue={current.pool || ""} /></label><label><span>最高价格（可选）</span><input name="max_price" defaultValue={current.max_price || ""} placeholder="例如 0.01" /></label></>}</div><Button type="submit" disabled={busy !== ""}><Settings2 className="mr-2 h-4 w-4" />保存配置</Button></form>
      </Panel>
    </> : <Panel title="Midtrans Alibaba 人机验证" className="gopay-settings-panel" action={<Status value={captchaConfigured ? "success" : "failed"} />}>
      <form className="gopay-form" onSubmit={submitCaptcha}>
        <div className="gopay-warning"><ShieldCheck /><span>遇到验证时会在当前支付代理下打开 Midtrans 页面，获取动态参数、Cookie 与一次性令牌；未触发验证时不会启动浏览器。</span></div>
        <div className="gopay-form-grid">
          <label className="wide"><span>Solverify API Key</span><input name="solverify_api_key" type="password" autoComplete="new-password" value={captchaSecrets.solverify} onChange={(event) => setCaptchaSecrets((currentSecrets) => ({ ...currentSecrets, solverify: event.target.value }))} placeholder={captcha.solverify_api_key_configured ? `已配置 ${captcha.solverify_api_key || ""}，留空保持不变` : "可选，优先使用 Solverify"} /></label>
          <label><span>Solverify 轮询（秒）</span><input name="solverify_poll_sec" type="number" min="1" step="1" defaultValue={captcha.solverify_poll_sec || "3"} /></label>
          <label><span>Solverify 超时（秒）</span><input name="solverify_timeout_sec" type="number" min="10" step="1" defaultValue={captcha.solverify_timeout_sec || "130"} /></label>
          <label className="wide"><span>2Captcha API Key</span><input name="api_key" type="password" autoComplete="new-password" value={captchaSecrets.twocaptcha} onChange={(event) => setCaptchaSecrets((currentSecrets) => ({ ...currentSecrets, twocaptcha: event.target.value }))} placeholder={twocaptchaConfigured ? `已配置 ${captcha.api_key || ""}，留空保持不变` : "可选备用密钥"} /></label>
          <label><span>2Captcha 轮询（秒）</span><input name="poll_sec" type="number" min="1" step="1" defaultValue={captcha.poll_sec || "5"} /></label>
          <label><span>2Captcha 超时（秒）</span><input name="timeout_sec" type="number" min="10" step="1" defaultValue={captcha.timeout_sec || "180"} /></label>
          <label><span>最大重试次数</span><input name="max_attempts" type="number" min="1" max="5" step="1" defaultValue={captcha.max_attempts || "3"} /></label>
          <label><span>Scene ID</span><input name="scene_id" defaultValue={captcha.scene_id || "1mbz0gpl6"} /></label>
          <label><span>Prefix</span><input name="prefix" defaultValue={captcha.prefix || "y1rdnbp"} /></label>
          <label><span>Region</span><input name="region" defaultValue={captcha.region || "sgp"} /></label>
          <label className="wide"><span>AliyunCaptcha.js URL</span><input name="api_get_lib" type="url" defaultValue={captcha.api_get_lib || "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"} /></label>
        </div>
        <Button type="submit" disabled={busy !== ""}><Settings2 className="mr-2 h-4 w-4" />保存人机验证配置</Button>
      </form>
    </Panel>}
  </div>;
}

function TaskLogModal({ job, onClose }: { job: Row; onClose: () => void }) {
  const logs = job.logs || [];
  return <Modal title="任务日志" subtitle={String(job.id || "-")} onClose={onClose}><div className="gopay-modal-body"><dl className="gopay-log-summary"><dt>手机号</dt><dd>{job.phone || "-"}</dd><dt>状态</dt><dd><Status value={job.status} /></dd><dt>当前消息</dt><dd>{job.message || "-"}</dd><dt>更新时间</dt><dd>{formatTime(job.updated_at || job.created_at)}</dd></dl><ol className="gopay-logs">{logs.length ? logs.map((entry: Row, index: number) => <li key={index}><time>{formatTime(entry.at)}</time><span>{entry.message || "-"}</span></li>) : <li><span>暂无详细日志</span></li>}</ol></div></Modal>;
}

function PinModal({ account, busy, run, refresh, onClose }: { account: Row; busy: string; run: RunAction; refresh: () => Promise<void>; onClose: () => void }) {
  async function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const data = new FormData(event.currentTarget); const oldPin = String(data.get("old_pin") || ""); const newPin = String(data.get("new_pin") || ""); if (!/^\d{6}$/.test(oldPin) || !/^\d{6}$/.test(newPin) || oldPin === newPin) return void window.alert("原 PIN 与新 PIN 必须是不同的 6 位数字"); await run("pin-change", async () => { const task = await post("/pin-tasks", { mode: "known", phone: account.phone, old_pin: oldPin, new_pin: newPin }); for (let attempt = 0; attempt < 90; attempt += 1) { await new Promise((resolve) => window.setTimeout(resolve, 1000)); const state = await api(`/pin-tasks/${encodeURIComponent(task.id)}`); if (state.status === "success") return state; if (state.status === "failed") throw new Error(state.message || "PIN 修改失败"); } throw new Error("PIN 修改任务仍在执行，请稍后刷新账号状态"); }, "PIN 修改成功", refresh); onClose(); }
  return <Modal title="修改 GoPay PIN" subtitle={account.phone} onClose={onClose}><form className="gopay-form gopay-modal-body" onSubmit={submit}><div className="gopay-warning"><ShieldCheck />仅支持已知原 PIN 的账号；忘记 PIN 请使用 GoPay 官方找回流程。</div><label><span>原 PIN</span><input name="old_pin" type="password" inputMode="numeric" maxLength={6} required autoComplete="off" /></label><label><span>新 PIN</span><input name="new_pin" type="password" inputMode="numeric" maxLength={6} required autoComplete="off" /></label><div className="gopay-modal-actions"><Button type="button" variant="outline" onClick={onClose}>取消</Button><Button type="submit" disabled={busy !== ""}>验证并修改</Button></div></form></Modal>;
}
