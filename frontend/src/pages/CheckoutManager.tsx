import { useEffect, useMemo, useRef, useState } from "react";
import QRCode from "qrcode";
import { ChevronDown, ChevronUp, Clipboard, CreditCard, Download, ExternalLink, FileText, Filter, ListChecks, Loader2, Play, RefreshCw, ScrollText, Trash2, X } from "lucide-react";
import { API_BASE, apiFetch, triggerBrowserDownload } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import "@/styles/module-polish.css";

type AnyRow = Record<string, any>;
type Provider = { value: string; label: string; hint: string; country: string; currency: string };

const fallbackProviders: Provider[] = [
  ["hosted", "Hosted", "官方支付长链", "US", "USD"], ["ph_short", "菲律宾短链", "US Checkout / TR 优惠", "PH", "PHP"], ["paypal", "PayPal", "Approve 跳转", "US", "USD"],
  ["ideal", "iDEAL", "荷兰银行支付", "NL", "EUR"], ["upi", "UPI", "印度二维码", "IN", "INR"], ["pix", "PIX", "巴西即时支付", "BR", "BRL"],
  ["twint", "TWINT", "瑞士移动支付", "CH", "CHF"], ["momo", "MoMo", "越南电子钱包", "VN", "VND"], ["gcash", "GCash", "菲律宾电子钱包", "PH", "PHP"], ["gopay", "GoPay", "印尼 Midtrans 跳转", "ID", "IDR"], ["blik", "BLIK", "波兰银行动态码支付", "PL", "PLN"], ["kakao", "Kakao Pay", "韩国 Nicepay 跳转", "KR", "KRW"],
].map(([value, label, hint, country, currency]) => ({ value, label, hint, country, currency }));

const planOptions = [{ value: "plus", label: "Plus", hint: "个人订阅" }, { value: "pro", label: "Pro", hint: "专业计划" }, { value: "team", label: "Team", hint: "工作空间" }, { value: "codex_low", label: "Codex", hint: "低价空间" }];
const countryNames: Record<string, string> = {
  US: "美国", GB: "英国", JP: "日本", CN: "中国", HK: "中国香港", TW: "中国台湾", KR: "韩国",
  IN: "印度", BR: "巴西", AU: "澳大利亚", CA: "加拿大", NZ: "新西兰", SG: "新加坡", MY: "马来西亚",
  TH: "泰国", ID: "印度尼西亚", PH: "菲律宾", VN: "越南", TR: "土耳其", IL: "以色列", AE: "阿联酋",
  SA: "沙特阿拉伯", QA: "卡塔尔", KW: "科威特", BH: "巴林", OM: "阿曼", ZA: "南非", EG: "埃及",
  NG: "尼日利亚", KE: "肯尼亚", MX: "墨西哥", AR: "阿根廷", CL: "智利", CO: "哥伦比亚", PE: "秘鲁",
  UY: "乌拉圭", PY: "巴拉圭", BO: "玻利维亚", CR: "哥斯达黎加", DO: "多米尼加共和国", CH: "瑞士",
  SE: "瑞典", NO: "挪威", DK: "丹麦", PL: "波兰", CZ: "捷克", HU: "匈牙利", RO: "罗马尼亚",
  BG: "保加利亚", IS: "冰岛", RS: "塞尔维亚", UA: "乌克兰", GE: "格鲁吉亚", KZ: "哈萨克斯坦",
  DE: "德国", FR: "法国", IE: "爱尔兰", NL: "荷兰", ES: "西班牙", IT: "意大利", AT: "奥地利",
  BE: "比利时", FI: "芬兰", PT: "葡萄牙", GR: "希腊", LU: "卢森堡", SK: "斯洛伐克", SI: "斯洛文尼亚",
  EE: "爱沙尼亚", LV: "拉脱维亚", LT: "立陶宛", CY: "塞浦路斯", MT: "马耳他", HR: "克罗地亚",
};
const currencyByCountry: Record<string, string> = {
  US: "USD", GB: "GBP", JP: "JPY", CN: "CNY", HK: "HKD", TW: "TWD", KR: "KRW",
  IN: "INR", BR: "BRL", AU: "AUD", CA: "CAD", NZ: "NZD", SG: "SGD", MY: "MYR",
  TH: "THB", ID: "IDR", PH: "PHP", VN: "VND", TR: "TRY", IL: "ILS", AE: "AED",
  SA: "SAR", QA: "QAR", KW: "KWD", BH: "BHD", OM: "OMR", ZA: "ZAR", EG: "EGP",
  NG: "NGN", KE: "KES", MX: "MXN", AR: "ARS", CL: "CLP", CO: "COP", PE: "PEN",
  UY: "UYU", PY: "PYG", BO: "BOB", CR: "CRC", DO: "DOP", CH: "CHF", SE: "SEK",
  NO: "NOK", DK: "DKK", PL: "PLN", CZ: "CZK", HU: "HUF", RO: "RON", BG: "BGN",
  IS: "ISK", RS: "RSD", UA: "UAH", GE: "GEL", KZ: "KZT", DE: "EUR", FR: "EUR",
  IE: "EUR", NL: "EUR", ES: "EUR", IT: "EUR", AT: "EUR", BE: "EUR", FI: "EUR",
  PT: "EUR", GR: "EUR", LU: "EUR", SK: "EUR", SI: "EUR", EE: "EUR", LV: "EUR",
  LT: "EUR", CY: "EUR", MT: "EUR", HR: "EUR",
};
const sessionStatuses = ["未注册", "已注册", "已接码", "已反代", "已封禁", "需二验", "登录刷新", "失败"];
const sessionPlans = ["free", "plus", "k12", "team", "pro"];
const checkoutPreferencesStorageKey = "sunnyregister.checkout.preferences.v1";
const checkoutTaskStorageKey = "sunnyregister.checkout.last-task-id.v1";
const checkoutProxyPoolsStorageKey = "sunnyregister.checkout.proxy-pools-by-path.v1";
const checkoutAccountTableWidthsStorageKey = "sunnyregister.checkout.account-table-widths.v1";

type CheckoutTableColumn = { width: number; minWidth: number; maxWidth?: number };
const checkoutAccountTableColumns: CheckoutTableColumn[] = [
  { width: 58, minWidth: 52, maxWidth: 88 }, { width: 190, minWidth: 150, maxWidth: 420 },
  { width: 190, minWidth: 150, maxWidth: 420 }, { width: 130, minWidth: 100, maxWidth: 320 },
  { width: 100, minWidth: 88, maxWidth: 220 }, { width: 86, minWidth: 76, maxWidth: 180 },
  { width: 112, minWidth: 96, maxWidth: 360 }, { width: 128, minWidth: 108, maxWidth: 260 },
  { width: 118, minWidth: 100, maxWidth: 240 }, { width: 330, minWidth: 220, maxWidth: 900 },
  { width: 86, minWidth: 76, maxWidth: 220 }, { width: 150, minWidth: 120, maxWidth: 360 },
];

function readCheckoutAccountTableWidths() {
  const defaults = checkoutAccountTableColumns.map((column) => column.width);
  if (typeof window === "undefined") return defaults;
  try {
    const stored = JSON.parse(window.localStorage.getItem(checkoutAccountTableWidthsStorageKey) || "[]");
    if (!Array.isArray(stored) || stored.length !== defaults.length) return defaults;
    return stored.map((value, index) => index === 0 ? defaults[0] : Math.max(checkoutAccountTableColumns[index].minWidth, Math.min(checkoutAccountTableColumns[index].maxWidth || 900, Number(value) || defaults[index])));
  } catch { return defaults; }
}

function ResizableCheckoutAccountTable({ headers, children }: { headers: React.ReactNode[]; children: React.ReactNode }) {
  const [widths, setWidths] = useState<number[]>(readCheckoutAccountTableWidths);
  const cleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    try { window.localStorage.setItem(checkoutAccountTableWidthsStorageKey, JSON.stringify(widths)); } catch { /* private browsing may disable storage */ }
  }, [widths]);
  useEffect(() => () => cleanupRef.current?.(), []);
  const setColumnWidth = (index: number, targetWidth: number) => setWidths((current) => {
    const column = checkoutAccountTableColumns[index];
    if (!column || index === 0) return current;
    const next = [...current];
    next[index] = Math.max(column.minWidth, Math.min(column.maxWidth || 900, Math.round(targetWidth)));
    return next;
  });
  const startResize = (event: React.PointerEvent<HTMLSpanElement>, index: number, direction: 1 | -1 = 1) => {
    event.preventDefault();
    event.stopPropagation();
    cleanupRef.current?.();
    const startX = event.clientX;
    const startWidth = widths[index];
    const onMove = (moveEvent: PointerEvent) => setColumnWidth(index, startWidth + direction * (moveEvent.clientX - startX));
    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", cleanup);
      window.removeEventListener("pointercancel", cleanup);
      document.body.classList.remove("sr-column-resizing");
      cleanupRef.current = null;
    };
    cleanupRef.current = cleanup;
    document.body.classList.add("sr-column-resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", cleanup);
    window.addEventListener("pointercancel", cleanup);
  };
  const tableWidth = widths.reduce((sum, width) => sum + width, 0);
  const resizeTitle = "拖动调整列宽，双击恢复默认宽度";
  return <table className="sr-account-table sr-resizable-table checkout-account-table" style={{ width: tableWidth, minWidth: tableWidth, maxWidth: "none", ["--checkout-checkbox-width" as string]: `${widths[0]}px` }}>
    <colgroup>{widths.map((width, index) => <col key={index} style={{ width }} />)}</colgroup>
    <thead><tr>{headers.map((header, index) => <th key={index} className={index === 0 || index === 1 ? "checkout-sticky checkout-sticky-left" : index === headers.length - 1 ? "checkout-sticky checkout-sticky-right" : undefined} style={index === 0 ? { left: 0 } : index === 1 ? { left: widths[0] } : undefined}>
      <span className="sr-table-header-content">{header}</span>
      {index > 0 && <span className={`sr-column-resizer ${index === headers.length - 1 ? "is-last" : ""}`} role="separator" aria-orientation="vertical" tabIndex={0} title={resizeTitle} onPointerDown={(event) => startResize(event, index, index === headers.length - 1 ? -1 : 1)} onDoubleClick={() => setColumnWidth(index, checkoutAccountTableColumns[index].width)} onKeyDown={(event) => { if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); const delta = event.key === "ArrowRight" ? 12 : -12; setColumnWidth(index, widths[index] + (index === headers.length - 1 ? -delta : delta)); } else if (event.key === "Home") { event.preventDefault(); setColumnWidth(index, checkoutAccountTableColumns[index].width); } }} />}
    </th>)}</tr></thead>
    {children}
  </table>;
}

type ProxyPoolSnapshot = { checkout: string; promotion: string };
type ProxyPoolsByPath = Record<string, ProxyPoolSnapshot>;

type CheckoutPreferences = {
  checkoutProxies?: string;
  promotionProxies?: string;
  systemAT?: boolean;
  externalText?: string;
  query?: string;
  group?: string;
  status?: string;
  planFilter?: string;
  trialFilter?: string;
  checkoutFilter?: string;
  paymentMethods?: string[];
  plan?: string;
  linkType?: string;
  country?: string;
  currency?: string;
  retryCount?: number;
  concurrency?: number;
  usePromo?: boolean;
  promoCampaign?: string;
  promoCode?: string;
  promoCountry?: string;
  idealBank?: string;
  workspaceName?: string;
  workspaceId?: string;
  seatQuantity?: number;
  priceInterval?: string;
  creditQuantity?: number;
  pixTaxID?: string;
  pixAutoKind?: string;
  pageSize?: number;
  logOpen?: boolean;
  sortBy?: string;
  sortOrder?: "asc" | "desc";
  rebindEmailFilter?: "" | "present" | "missing";
  trialCountryFilters?: string[];
};

const statusLabels: Record<string, string> = {
  unregistered: "未注册", registered: "已注册", phone_bound: "已接码", reverse_proxied: "已反代",
  banned: "已封禁", needs_2fa: "需二验", refreshing: "登录刷新", failed: "失败",
  pending: "待处理", valid: "有效", invalid: "无效", expired: "已过期", "待检测": "待检测", "格式无效": "格式无效",
};
const planLabels: Record<string, string> = { free: "Free", plus: "Plus", k12: "K12", team: "Team", pro: "Pro" };
const trialLabels: Record<string, string> = { eligible: "有0元试用", ineligible: "无0元试用", unknown: "未检测" };
const checkoutLabels: Record<string, string> = { oaics: "OAICS", cs_live: "CS Live", cs_test: "CS Test", unknown: "未检测" };
const pathLabels: Record<string, string> = {
  hosted: "官方长链", ph_short: "菲律宾短链", paypal: "PayPal", ideal: "iDEAL", upi: "UPI",
  pix: "PIX", twint: "TWINT", momo: "MoMo", gcash: "GCash", gopay: "GoPay", blik: "BLIK", kakao: "Kakao Pay",
};
const paymentMethodOptions = ["paypal", "card", "link", "gcash", "gopay", "kakao_pay", "nicepay", "ideal", "momo", "twint", "pix", "upi", "paynow", "grabpay", "fpx", "promptpay", "paypay", "konbini", "boleto", "blik", "p24", "mb_way"];
const paymentMethodLabels: Record<string, string> = { paypal: "PayPal", card: "Card", link: "Link", gcash: "GCash", gopay: "GoPay", kakao_pay: "Kakao Pay", nicepay: "Nicepay", ideal: "iDEAL", momo: "MoMo", twint: "TWINT", pix: "PIX", upi: "UPI", paynow: "PayNow", grabpay: "GrabPay", fpx: "FPX", promptpay: "PromptPay", paypay: "PayPay", konbini: "Konbini", boleto: "Boleto", blik: "BLIK", p24: "P24", mb_way: "MB WAY" };

type BadgeTone = "slate" | "blue" | "green" | "cyan" | "red" | "amber" | "violet" | "rose";
const badgeTones: Record<BadgeTone, string> = {
  slate: "border-slate-200 bg-slate-100 text-slate-600 dark:border-slate-500/20 dark:bg-slate-400/10 dark:text-slate-300",
  blue: "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-400/20 dark:bg-blue-400/10 dark:text-blue-300",
  green: "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-400/20 dark:bg-emerald-400/10 dark:text-emerald-300",
  cyan: "border-cyan-200 bg-cyan-50 text-cyan-700 dark:border-cyan-400/20 dark:bg-cyan-400/10 dark:text-cyan-300",
  red: "border-red-200 bg-red-50 text-red-700 dark:border-red-400/20 dark:bg-red-400/10 dark:text-red-300",
  amber: "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-400/20 dark:bg-amber-400/10 dark:text-amber-300",
  violet: "border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-400/20 dark:bg-violet-400/10 dark:text-violet-300",
  rose: "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-400/20 dark:bg-rose-400/10 dark:text-rose-300",
};

function normalized(value: unknown) { return String(value || "").trim().toLowerCase(); }
function labelFor(value: unknown, labels: Record<string, string>, fallback = "-") { const key = normalized(value); return labels[key] || (key ? String(value) : fallback); }
function paymentMethodLabel(value: unknown) {
  const key = normalized(value);
  return paymentMethodLabels[key] || key.replace(/_/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
}
function PaymentMethodFilter({ value, options, onChange }: { value: string[]; options: string[]; onChange: (value: string[]) => void }) {
  const toggle = (method: string) => onChange(value.includes(method) ? value.filter((item) => item !== method) : [...value, method]);
  const allSelected = options.length > 0 && value.length === options.length;
  return <details className="sr-payment-filter">
    <summary className="sr-payment-filter-trigger">
      <CreditCard className="sr-payment-filter-icon" />
      {value.length === 0 ? <span className="sr-payment-filter-placeholder">全部支付方式</span> : <span className="sr-payment-filter-chips">
        {value.slice(0, 2).map((method) => <span className="sr-payment-filter-chip" key={method}>{paymentMethodLabel(method)}</span>)}
        {value.length > 2 && <span className="sr-payment-filter-more">+{value.length - 2}</span>}
      </span>}
      <ChevronDown className="sr-payment-filter-chevron" />
    </summary>
    <div className="sr-payment-filter-menu">
      <div className="sr-payment-filter-menu-head">
        <span>支付方式筛选（同时满足）</span>
        <div className="sr-payment-filter-menu-actions"><button type="button" className="sr-payment-filter-action" onClick={() => onChange(allSelected ? [] : options)}><ListChecks className="h-3.5 w-3.5" />{allSelected ? "清除筛选" : "全选"}</button></div>
      </div>
      <div className="sr-payment-filter-options">
        {options.map((method) => <label key={method} className={`sr-payment-filter-option ${value.includes(method) ? "is-selected" : ""}`}><input type="checkbox" checked={value.includes(method)} onChange={() => toggle(method)} /><span>{paymentMethodLabel(method)}</span>{value.includes(method) && <span className="sr-payment-filter-check">✓</span>}</label>)}
      </div>
    </div>
  </details>;
}
type PresenceFilterValue = "" | "present" | "missing";
function CheckoutPresenceFilter({ label, value, onChange, title }: { label: string; value: PresenceFilterValue; onChange: (value: PresenceFilterValue) => void; title: string }) {
  const filterLabel = value === "present" ? "已换绑" : value === "missing" ? "未换绑" : "全部";
  return <div className="sr-login-secret-header"><span>{label}</span><button type="button" className={`sr-login-secret-filter ${value ? "active" : ""}`} onClick={() => onChange(value === "" ? "present" : value === "present" ? "missing" : "")} title={title} aria-label={`${title}: ${filterLabel}`}><Filter className="h-3.5 w-3.5" /><span>{filterLabel}</span></button></div>;
}
function CheckoutTrialCountryFilter({ value, options, onChange }: { value: string[]; options: string[]; onChange: (value: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const countries = Array.from(new Set([...options, ...value].map((item) => String(item).trim().toUpperCase()).filter(Boolean))).sort();
  const label = value.length === 0 ? "全部" : value.length <= 2 ? value.join(",") : String(value.length);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => { if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  const toggle = (country: string) => onChange(value.includes(country) ? value.filter((item) => item !== country) : [...value, country].sort());
  return <div ref={rootRef} className="sr-trial-country-header"><span>试用资格</span><button type="button" className={`sr-login-secret-filter ${value.length ? "active" : ""}`} onClick={() => setOpen((current) => !current)} title="筛选有试用资格的国家" aria-expanded={open} aria-label={`筛选有试用资格的国家: ${label}`}><Filter className="h-3.5 w-3.5" /><span>{label}</span></button>{open && <div className="sr-trial-country-filter-menu"><div className="sr-trial-country-filter-head"><strong>筛选有试用资格的国家</strong>{value.length > 0 && <button type="button" onClick={() => onChange([])}>清除</button>}</div><div className="sr-trial-country-filter-options">{countries.length ? countries.map((country) => <label key={country} className={`sr-trial-country-filter-option ${value.includes(country) ? "is-selected" : ""}`}><input type="checkbox" checked={value.includes(country)} onChange={() => toggle(country)} /><span>{country}</span></label>) : <span className="sr-trial-country-filter-empty">暂无已检测国家</span>}</div><p>多选时需同时具有所选国家的试用资格</p></div>}</div>;
}
function CompactBadge({ label, tone = "slate" }: { label: string; tone?: BadgeTone }) {
  return <span className={`inline-flex whitespace-nowrap rounded-md border px-2 py-0.5 text-[11px] font-semibold ${badgeTones[tone]}`}>{label}</span>;
}
function accountStatusTone(value: unknown) {
  const key = normalized(value);
  if (["已注册", "registered", "valid"].includes(key)) return "blue";
  if (["已接码", "phone_bound"].includes(key)) return "green";
  if (["已反代", "reverse_proxied"].includes(key)) return "cyan";
  if (["已封禁", "banned", "失败", "failed", "invalid", "expired", "已过期", "格式无效"].includes(key)) return "red";
  if (["需二验", "登录刷新", "refreshing", "pending", "待检测"].includes(key)) return "amber";
  return "gray";
}
function AccountStatusBadge({ value }: { value: unknown }) { return <span className={`sr-status sr-status-${accountStatusTone(value)}`}>{labelFor(value, statusLabels)}</span>; }
function AccountPlanBadge({ value }: { value: unknown }) { const key = normalized(value); return key ? <span className={`sr-plan-badge sr-plan-${sessionPlans.includes(key) ? key : "default"}`}>{labelFor(value, planLabels)}</span> : <span className="text-slate-400">-</span>; }
function accountCommerceCheckable(row: AnyRow) { return Boolean(row.token) || (["已注册", "registered"].includes(String(row.status || "")) && normalized(row.plan_type) === "free"); }
function AccountTrialValue({ row }: { row: AnyRow }) { const key = normalized(row.trial_eligibility); if (!accountCommerceCheckable(row)) return <span className="text-slate-400">-</span>; const results = row.trial_country_results && typeof row.trial_country_results === "object" ? row.trial_country_results as Record<string, string> : {}; const eligible = Object.entries(results).filter(([, value]) => value === "eligible").map(([country]) => country).sort(); const ineligible = Object.entries(results).filter(([, value]) => value === "ineligible").map(([country]) => country).sort(); if (eligible.length || ineligible.length) return <span className="inline-flex flex-col items-start gap-0.5 font-semibold leading-4 whitespace-nowrap"><span className={eligible.length ? "text-emerald-600 dark:text-emerald-400" : "hidden"}>试用：{eligible.join(",")}</span><span className={ineligible.length ? "text-red-500" : "hidden"}>无试用：{ineligible.join(",")}</span></span>; if (key === "eligible") return <span className="font-semibold text-emerald-600 dark:text-emerald-400">{trialLabels.eligible}</span>; if (key === "ineligible") return <span className="font-semibold text-red-500">{trialLabels.ineligible}</span>; return <span className="text-slate-400">-</span>; }
function AccountCheckoutValue({ row }: { row: AnyRow }) { const key = normalized(row.checkout_kind); return accountCommerceCheckable(row) && key && key !== "unknown" ? <span className="font-semibold text-sky-600 dark:text-sky-400">{labelFor(row.checkout_kind, checkoutLabels)}</span> : <span className="text-slate-400">-</span>; }
function pathTone(value: unknown): BadgeTone { return ({ hosted: "blue", ph_short: "cyan", paypal: "violet", ideal: "green", upi: "amber", pix: "green", twint: "red", momo: "rose", gcash: "blue", gopay: "green", blik: "red", kakao: "amber" } as Record<string, BadgeTone>)[normalized(value)] || "slate"; }

function taskStatusLabel(value: unknown) { return ({ pending: "等待中", claimed: "已领取", running: "运行中", succeeded: "已完成", failed: "失败", cancelled: "已停止", cancel_requested: "停止中" } as Record<string, string>)[normalized(value)] || String(value || "未开始"); }

function CheckoutLogFloat({ open, onToggle, task, logs, scrollRef }: { open: boolean; onToggle: () => void; task: AnyRow | null; logs: AnyRow[]; scrollRef: React.RefObject<HTMLDivElement | null> }) {
  const [size, setSize] = useState(() => {
    if (typeof window === "undefined") return { width: 430, height: 256 };
    try {
      const saved = JSON.parse(window.localStorage.getItem("checkout.log.size") || "{}");
      return { width: Math.max(320, Math.min(720, Number(saved.width) || 430)), height: Math.max(180, Math.min(640, Number(saved.height) || 256)) };
    } catch { return { width: 430, height: 256 }; }
  });
  useEffect(() => { try { window.localStorage.setItem("checkout.log.size", JSON.stringify(size)); } catch { /* storage may be disabled */ } }, [size]);
  function beginResize(event: React.PointerEvent<HTMLButtonElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startY = event.clientY;
    const start = size;
    const move = (current: PointerEvent) => setSize({
      width: Math.max(320, Math.min(720, start.width + startX - current.clientX)),
      height: Math.max(180, Math.min(640, start.height + startY - current.clientY)),
    });
    const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  }
  const progress = task?.progress_detail || {};
  const total = Number(progress.total || 0);
  const current = Number(progress.current || 0);
  const percent = total > 0 ? Math.min(100, Math.round(current * 100 / total)) : 0;
  return <div className="fixed bottom-5 right-5 z-[450] flex flex-col items-end gap-2">
    {open && <div className="relative flex max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-card)] shadow-2xl" style={{ width: size.width, height: size.height }}>
      <div className="flex items-center justify-between border-b border-[var(--border)] px-3 py-2.5"><div className="flex min-w-0 items-center gap-2"><ScrollText className="h-4 w-4 shrink-0 text-[var(--accent)]" /><span className="text-sm font-bold">提链日志</span><span className="truncate text-[11px] text-[var(--text-muted)]">{taskStatusLabel(task?.status)}</span></div><button className="round-tool h-7 w-7" title="隐藏日志" onClick={onToggle}><ChevronDown className="h-4 w-4" /></button></div>
      {task && <div className="border-b border-[var(--border)] px-3 py-2"><div className="mb-1.5 flex justify-between text-[11px] text-[var(--text-muted)]"><span>{task.progress || "0/0"}</span><span>{percent}%</span></div><div className="h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"><div className="h-full rounded-full bg-[var(--accent)] transition-[width]" style={{ width: `${percent}%` }} /></div></div>}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto bg-[var(--bg-main)] p-3 font-mono text-[11px] leading-5 text-[var(--text-secondary)]">{logs.length ? logs.map((item, index) => <div key={item.id || index} className="grid grid-cols-[62px_8px_minmax(0,1fr)] gap-2"><span className="text-[var(--text-muted)]">{String(item.created_at || "").slice(11, 19) || "--:--:--"}</span><span className={item.level === "error" ? "text-red-400" : item.level === "warning" ? "text-amber-400" : "text-emerald-400"}>●</span><span className="break-words">{item.message || item.line}</span></div>) : <div className="flex h-full items-center justify-center text-[var(--text-muted)]">暂无提链日志</div>}</div>
      <button type="button" aria-label="调整日志窗口大小" title="拖动调整日志窗口大小" className="absolute left-0 top-0 h-4 w-4 cursor-nwse-resize opacity-60 hover:opacity-100" onPointerDown={beginResize}><span className="absolute left-1 top-1 h-2 w-2 border-l-2 border-t-2 border-[var(--accent)]" /></button>
    </div>}
    <button className="inline-flex h-10 items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-shell)] px-3 text-sm font-semibold shadow-lg hover:border-[var(--accent)]" title={open ? "隐藏提链日志" : "显示提链日志"} onClick={onToggle}><ScrollText className="h-4 w-4 text-[var(--accent)]" />日志{open ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}</button>
  </div>;
}

function splitLines(value: string) { return value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean); }
function readBrowserText(key: string) {
  if (typeof window === "undefined") return "";
  try { return window.localStorage.getItem(key) || ""; } catch { return ""; }
}
function readCheckoutPreferences(): CheckoutPreferences {
  if (typeof window === "undefined") return {};
  try {
    const parsed = JSON.parse(window.localStorage.getItem(checkoutPreferencesStorageKey) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch { return {}; }
}
function readProxyPoolsByPath(): ProxyPoolsByPath {
  if (typeof window === "undefined") return {};
  try {
    const parsed = JSON.parse(window.localStorage.getItem(checkoutProxyPoolsStorageKey) || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).flatMap(([path, value]) => {
      if (!value || typeof value !== "object" || Array.isArray(value)) return [];
      const snapshot = value as Record<string, unknown>;
      return [[path, {
        checkout: typeof snapshot.checkout === "string" ? snapshot.checkout : "",
        promotion: typeof snapshot.promotion === "string" ? snapshot.promotion : "",
      }]];
    }));
  } catch { return {}; }
}
function writeProxyPoolsForPath(path: string, checkout: string, promotion: string) {
  if (typeof window === "undefined" || !path) return;
  try {
    const saved = readProxyPoolsByPath();
    saved[path] = { checkout, promotion };
    window.localStorage.setItem(checkoutProxyPoolsStorageKey, JSON.stringify(saved));
  } catch { /* private browsing may disable local storage */ }
}
function savedNumber(value: unknown, fallback: number, minimum: number, maximum: number) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.min(maximum, Math.max(minimum, parsed)) : fallback;
}
function resultError(item: AnyRow) { return String(item.error || item.checkout_error || item.message || "").trim(); }
function resultDisplayLink(item: AnyRow) { return String(item.payment_link || item.short_link || item.verification_url || item.provider_redirect_url || item.paypal_link || item.checkout_url || "").trim(); }
function resultQrImage(item: AnyRow) { return String(item.qr_image || item.qr_image_png || item.qr_image_svg || "").trim(); }
function resultQrData(item: AnyRow) {
  const value = String(item.qr_data || "").trim();
  if (value) return value;
  // GCash's m.gcash.com page requires login and is not a scanner payload.
  return normalized(item.link_type) === "gcash" ? "" : resultDisplayLink(item);
}
function externalATInfo(token: string) {
  const parts = token.split(".");
  if (parts.length < 2) return { status: "格式无效", expires_at: "", email: "" };
  try {
    const payload = JSON.parse(atob(parts[1].replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(parts[1].length / 4) * 4, "=")));
    const profile = payload["https://api.openai.com/profile"] || {};
    const exp = Number(payload.exp || 0);
    return { status: exp > 0 && exp * 1000 <= Date.now() ? "已过期" : "待检测", expires_at: exp ? new Date(exp * 1000).toISOString() : "", email: payload.email || profile.email || "" };
  } catch { return { status: "待检测", expires_at: "", email: "" }; }
}

function parseExternalRows(value: string) {
  return splitLines(value).map((token, index) => {
    const info = externalATInfo(token);
    return { index, token, email: (token.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i) || [info.email || ""])[0], at_status: token.startsWith("eyJ") || token.includes(".") ? info.status : "格式无效", expires_at: info.expires_at, selected: false };
  });
}

function checkoutSelectionKey(row: AnyRow, systemAT: boolean) {
  return Number(systemAT ? row.id : row.index);
}

function checkoutPageCount(total: number, pageSize: number) {
  return Math.max(1, Math.ceil(Math.max(0, total) / Math.max(1, pageSize)));
}

function checkoutPaginationTokens(page: number, pages: number): Array<number | "..."> {
  if (pages <= 7) return Array.from({ length: pages }, (_, index) => index + 1);
  const tokens: Array<number | "..."> = [1];
  const start = Math.max(2, page - 1);
  const end = Math.min(pages - 1, page + 1);
  if (start > 2) tokens.push("...");
  for (let value = start; value <= end; value += 1) tokens.push(value);
  if (end < pages - 1) tokens.push("...");
  tokens.push(pages);
  return tokens;
}

function QRThumb({ value, onClick }: { value: string; onClick: () => void }) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    let active = true;
    void QRCode.toDataURL(value, { width: 96, margin: 1, errorCorrectionLevel: "M" }).then((data) => { if (active) setSrc(data); }).catch(() => setSrc(""));
    return () => { active = false; };
  }, [value]);
  return <button className="rounded-lg border border-[var(--border)] bg-white p-1" title="查看二维码" onClick={onClick}>{src ? <img className="h-10 w-10" src={src} alt="支付二维码" /> : <span className="block h-10 w-10 animate-pulse bg-slate-100" />}</button>;
}

function QRImageThumb({ src, onClick }: { src: string; onClick: () => void }) {
  return <button className="rounded-lg border border-[var(--border)] bg-white p-1" title="查看二维码" onClick={onClick}><img className="h-10 w-10 object-contain" src={src} alt="支付二维码" /></button>;
}

function QRExpiryLabel({ expiresAt, status }: { expiresAt: number; status: string }) {
  const [expired, setExpired] = useState(status === "expired");
  useEffect(() => {
    let timer = 0;
    queueMicrotask(() => setExpired(status === "expired"));
    if (status !== "expired") {
      timer = window.setTimeout(() => setExpired(true), Math.max(0, expiresAt * 1000 - Date.now()));
    }
    return () => window.clearTimeout(timer);
  }, [expiresAt, status]);
  return <div className="mt-1 text-[10px] text-[var(--text-muted)]">{expired ? "二维码已过期" : `有效至 ${new Date(expiresAt * 1000).toLocaleTimeString()}`}</div>;
}

function QRModal({ value, image, onClose }: { value: string; image: string; onClose: () => void }) {
  const [src, setSrc] = useState("");
  useEffect(() => { let active = true; if (image) { setSrc(image); return () => { active = false; }; } void QRCode.toDataURL(value, { width: 520, margin: 2, errorCorrectionLevel: "M" }).then((data) => { if (active) setSrc(data); }); return () => { active = false; }; }, [image, value]);
  async function copy() { if (image && navigator.clipboard?.write && typeof ClipboardItem !== "undefined") { const blob = await fetch(src).then((response) => response.blob()); await navigator.clipboard.write([new ClipboardItem({ [blob.type || "image/png"]: blob })]); return; } await navigator.clipboard?.writeText(value); }
  function download() { if (!src) return; const byte = atob(src.split(",")[1]); const arr = Uint8Array.from(byte, (c) => c.charCodeAt(0)); triggerBrowserDownload(new Blob([arr], { type: "image/png" }), "payment-qr.png"); }
  return <div className="fixed inset-0 z-[600] flex items-center justify-center bg-black/60 p-4" onClick={onClose}><div className="w-full max-w-md rounded-2xl border border-[var(--border)] bg-[var(--bg-shell)] p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}><div className="mb-4 flex items-center justify-between"><h3 className="text-lg font-bold">支付二维码</h3><button className="round-tool" onClick={onClose}><X className="h-4 w-4" /></button></div>{src ? <img className="mx-auto aspect-square w-full max-w-[320px] rounded-xl bg-white p-3 object-contain" src={src} alt="支付二维码" /> : <div className="flex h-80 items-center justify-center"><Loader2 className="animate-spin" /></div>}{value && <p className="mt-3 break-all text-xs text-[var(--text-muted)]">{value}</p>}<div className="mt-4 flex justify-end gap-2"><Button variant="outline" onClick={() => void copy()}><Clipboard className="mr-2 h-4 w-4" />{image ? "复制图片" : "复制内容"}</Button><Button onClick={download} disabled={!src}><Download className="mr-2 h-4 w-4" />下载二维码</Button></div></div></div>;
}

type CheckoutLiveState = { progress: number; message: string; status: string; result?: AnyRow; logs: AnyRow[] };
type CheckoutSuccessResult = { key: string; email: string; path: string; link: string; qrData: string; qrImage: string; qrExpiresAt: number; qrStatus: string; gcashOrderId: string; callbackToken: string };

function checkoutSuccessResult(result: AnyRow, identity: AnyRow = {}): CheckoutSuccessResult | null {
  const link = resultDisplayLink(result);
  if (!link) return null;
  const status = normalized(result?.status);
  if (status && !["succeeded", "success", "done"].includes(status)) return null;
  const email = String(result.email || identity.email || "").trim();
  const accountID = Number(result.account_id || identity.account_id || 0);
  const rowIndex = Number(result.index ?? identity.index ?? -1);
  const key = email ? `email:${normalized(email)}` : accountID > 0 ? `account:${accountID}` : rowIndex >= 0 ? `index:${rowIndex}` : `link:${link}`;
  return {
    key,
    email: email || "未知邮箱",
    path: String(result.link_type || identity.link_type || ""),
    link,
    qrData: resultQrData(result),
    qrImage: resultQrImage(result),
    qrExpiresAt: Number(result.qr_expires_at || 0),
    qrStatus: String(result.qr_status || ""),
    gcashOrderId: String(result.gcash_order_id || ""),
    callbackToken: String(result.callback_token || ""),
  };
}

function mergeCheckoutSuccessResults(previous: CheckoutSuccessResult[], incoming: CheckoutSuccessResult[]) {
  const merged = new Map(previous.map((item) => [item.key, item]));
  for (const item of incoming) merged.set(item.key, item);
  return Array.from(merged.values());
}

function checkoutLiveKey(value: AnyRow) {
  const email = normalized(value.email);
  if (email) return `email:${email}`;
  if (value.account_id != null && Number(value.account_id) > 0) return `account:${Number(value.account_id)}`;
  return `index:${Number(value.index ?? -1)}`;
}
function CheckoutAccountDetail({ email, state, onClose }: { email: string; state?: CheckoutLiveState; onClose: () => void }) {
  const logs = state?.logs || [];
  return <div className="fixed inset-0 z-[620] flex items-center justify-center bg-black/60 p-4" onClick={onClose}><div className="flex max-h-[min(680px,calc(100vh-2rem))] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-shell)] shadow-2xl" onClick={(event) => event.stopPropagation()}><div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3"><div className="min-w-0"><h3 className="flex items-center gap-2 text-base font-bold"><FileText className="h-4 w-4 text-[var(--accent)]" />账户提链详情</h3><p className="mt-1 truncate text-xs text-[var(--text-muted)]">{email || "未知账户"} · {state?.status === "running" ? "进行中" : state?.status === "succeeded" ? "已成功" : state?.status === "failed" ? "已失败" : "待处理"}</p></div><button className="round-tool" title="关闭详情" onClick={onClose}><X className="h-4 w-4" /></button></div>{state && <div className="border-b border-[var(--border)] px-4 py-3"><div className="mb-1 flex justify-between text-[11px] text-[var(--text-muted)]"><span>{state.message || "等待提链日志"}</span><span>{Math.round(state.progress)}%</span></div><div className="h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"><div className={`h-full rounded-full transition-[width] ${state.status === "failed" ? "bg-red-500" : "bg-[var(--accent)]"}`} style={{ width: `${Math.max(0, Math.min(100, state.progress))}%` }} /></div></div>}<div className="min-h-0 flex-1 overflow-y-auto bg-[var(--bg-main)] p-4 font-mono text-[11px] leading-5 text-[var(--text-secondary)]">{logs.length ? logs.map((item, index) => <div key={item.id || index} className="grid grid-cols-[62px_8px_minmax(0,1fr)] gap-2"><span className="text-[var(--text-muted)]">{String(item.created_at || "").slice(11, 19) || "--:--:--"}</span><span className={item.level === "error" || item.type === "checkout_result" && item.detail?.result?.status === "failed" ? "text-red-400" : "text-emerald-400"}>●</span><span className="break-words">{item.message || item.detail?.current_log || item.line}</span></div>) : <div className="flex h-full min-h-36 items-center justify-center text-[var(--text-muted)]">暂无该账户提链日志</div>}</div></div></div>;
}

export default function CheckoutManager() {
  const savedPreferences = useMemo(() => readCheckoutPreferences(), []);
  const savedTaskID = useMemo(() => readBrowserText(checkoutTaskStorageKey), []);
  const initialLinkType = savedPreferences.linkType ?? "hosted";
  const savedProxyPools = useMemo(() => readProxyPoolsByPath(), []);
  const initialProxyPools = savedProxyPools[initialLinkType] ?? {
    checkout: savedPreferences.checkoutProxies ?? readBrowserText("pay153.proxy_pool_2"),
    promotion: savedPreferences.promotionProxies ?? readBrowserText("pay153.proxy_pool_1"),
  };
  const [providers, setProviders] = useState<Provider[]>(fallbackProviders);
  const [countries, setCountries] = useState<Record<string, string>>(currencyByCountry);
  const [checkoutProxies, setCheckoutProxies] = useState(initialProxyPools.checkout);
  const [promotionProxies, setPromotionProxies] = useState(initialProxyPools.promotion);
  const [systemAT, setSystemAT] = useState(savedPreferences.systemAT ?? true);
  const [sessions, setSessions] = useState<AnyRow[]>([]);
  const [groups, setGroups] = useState<AnyRow[]>([]);
  const [externalText, setExternalText] = useState(savedPreferences.externalText ?? "");
  const [externalRows, setExternalRows] = useState<AnyRow[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [query, setQuery] = useState(savedPreferences.query ?? "");
  const [group, setGroup] = useState(savedPreferences.group ?? "");
  const [status, setStatus] = useState(savedPreferences.status ?? "已注册");
  const [planFilter, setPlanFilter] = useState(savedPreferences.planFilter ?? "free");
  const [trialFilter, setTrialFilter] = useState(savedPreferences.trialFilter ?? "eligible");
  const [rebindEmailFilter, setRebindEmailFilter] = useState<PresenceFilterValue>(savedPreferences.rebindEmailFilter ?? "");
  const [trialCountryFilters, setTrialCountryFilters] = useState<string[]>(() => Array.isArray(savedPreferences.trialCountryFilters) ? savedPreferences.trialCountryFilters : []);
  const [trialCountryOptions, setTrialCountryOptions] = useState<string[]>([]);
  const [checkoutFilter, setCheckoutFilter] = useState(savedPreferences.checkoutFilter ?? "");
  const [paymentMethods, setPaymentMethods] = useState<string[]>(() => Array.isArray(savedPreferences.paymentMethods) ? savedPreferences.paymentMethods : []);
  const [plan, setPlan] = useState(savedPreferences.plan ?? "plus");
  const [linkType, setLinkType] = useState(initialLinkType);
  const [country, setCountry] = useState(savedPreferences.country ?? "US");
  const [currency, setCurrency] = useState(savedPreferences.currency ?? "USD");
  const [retryCount, setRetryCount] = useState(() => savedNumber(savedPreferences.retryCount, 10, 0, 50));
  const [concurrency, setConcurrency] = useState(() => savedNumber(savedPreferences.concurrency, 3, 1, 100));
  const [usePromo, setUsePromo] = useState(savedPreferences.usePromo ?? true);
  const [promoCampaign, setPromoCampaign] = useState(savedPreferences.promoCampaign ?? "plus-1-month-free");
  const [promoCode, setPromoCode] = useState(savedPreferences.promoCode ?? "");
  const [promoCountry, setPromoCountry] = useState(savedPreferences.promoCountry ?? "");
  const [idealBank, setIdealBank] = useState(savedPreferences.idealBank ?? "");
  const [workspaceName, setWorkspaceName] = useState(savedPreferences.workspaceName ?? "Codex Workspace");
  const [workspaceId, setWorkspaceId] = useState(savedPreferences.workspaceId ?? "");
  const [seatQuantity, setSeatQuantity] = useState(() => savedNumber(savedPreferences.seatQuantity, 5, 1, 100));
  const [priceInterval, setPriceInterval] = useState(savedPreferences.priceInterval ?? "month");
  const [creditQuantity, setCreditQuantity] = useState(() => savedNumber(savedPreferences.creditQuantity, 13, 1, 10000));
  const [pixTaxID, setPixTaxID] = useState(savedPreferences.pixTaxID ?? "");
  const [pixAutoKind, setPixAutoKind] = useState(savedPreferences.pixAutoKind ?? "cpf");
  const [precheckBusy, setPrecheckBusy] = useState(false);
  const [checkoutBusy, setCheckoutBusy] = useState(Boolean(savedTaskID));
  const [listLoading, setListLoading] = useState(false);
  const [selectingAll, setSelectingAll] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(() => savedNumber(savedPreferences.pageSize, 20, 10, 100));
  const [sortBy] = useState(savedPreferences.sortBy ?? "");
  const [sortOrder] = useState<"asc" | "desc">(savedPreferences.sortOrder === "asc" ? "asc" : "desc");
  const [total, setTotal] = useState(0);
  const [refreshKey, setRefreshKey] = useState(0);
  const [task, setTask] = useState<AnyRow | null>(null);
  const [activeTaskID, setActiveTaskID] = useState(savedTaskID);
  const [notice, setNotice] = useState("");
  const [qrValue, setQrValue] = useState("");
  const [qrImage, setQrImage] = useState("");
  const [logOpen, setLogOpen] = useState(savedPreferences.logOpen ?? Boolean(savedTaskID));
  const [taskLogs, setTaskLogs] = useState<AnyRow[]>([]);
  const [checkoutLive, setCheckoutLive] = useState<Record<string, CheckoutLiveState>>({});
  const [checkoutSuccesses, setCheckoutSuccesses] = useState<CheckoutSuccessResult[]>([]);
  const [successListExpanded, setSuccessListExpanded] = useState(false);
  const [detailKey, setDetailKey] = useState("");
  const [cancelBusy, setCancelBusy] = useState(false);
  const logScrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    try {
      const preferences: CheckoutPreferences = {
        checkoutProxies, promotionProxies, systemAT, externalText, query, group, status, planFilter, trialFilter,
        checkoutFilter, paymentMethods, plan, linkType, country, currency, retryCount, concurrency, usePromo, promoCampaign,
        promoCode, promoCountry, idealBank, workspaceName, workspaceId, seatQuantity, priceInterval,
        creditQuantity, pixTaxID, pixAutoKind, pageSize, logOpen, sortBy, sortOrder, rebindEmailFilter, trialCountryFilters,
      };
      window.localStorage.setItem(checkoutPreferencesStorageKey, JSON.stringify(preferences));
      // Keep the legacy keys synchronized for older SunnyRegister builds.
      window.localStorage.setItem("pay153.proxy_pool_2", checkoutProxies);
      window.localStorage.setItem("pay153.proxy_pool_1", promotionProxies);
    } catch { /* private browsing may disable local storage */ }
  }, [checkoutFilter, checkoutProxies, concurrency, country, creditQuantity, currency, externalText, group, idealBank, linkType, logOpen, pageSize, paymentMethods, pixAutoKind, pixTaxID, plan, planFilter, priceInterval, promoCampaign, promoCode, promoCountry, promotionProxies, query, retryCount, seatQuantity, sortBy, sortOrder, status, systemAT, trialFilter, usePromo, workspaceId, workspaceName]);
  useEffect(() => {
    try {
      if (activeTaskID) window.localStorage.setItem(checkoutTaskStorageKey, activeTaskID);
      else window.localStorage.removeItem(checkoutTaskStorageKey);
    } catch { /* private browsing may disable local storage */ }
  }, [activeTaskID]);
  useEffect(() => { void apiFetch("/sunny/checkout/providers").then((data) => { if (data.items?.length) setProviders(data.items); if (data.countries) setCountries(data.countries); }).catch(() => {}); }, []);
  useEffect(() => { void apiFetch("/sunny/mailbox-groups").then((data) => setGroups(data.items || [])).catch(() => setGroups([])); }, []);
  useEffect(() => {
    if (!checkoutSuccesses.some((item) => item.path === "gcash" && item.qrExpiresAt > 0)) return;
    let active = true;
    const refreshExpired = async () => {
      const expired = checkoutSuccesses.filter((item) => item.path === "gcash" && item.qrExpiresAt > 0 && item.qrExpiresAt * 1000 <= Date.now() && item.gcashOrderId && item.callbackToken);
      for (const item of expired) {
        try {
          const data = await apiFetch(`/sunny/checkout/gcash-orders/${encodeURIComponent(item.gcashOrderId)}/qr`, {
            method: "POST",
            headers: { "X-GCash-Callback-Token": item.callbackToken },
          });
          const result = data?.result && typeof data.result === "object" ? data.result : {};
          if (!active || !result.qr_data) continue;
          setCheckoutSuccesses((old) => old.map((current) => current.key === item.key ? { ...current, qrData: String(result.qr_data), qrExpiresAt: Number(result.qr_expires_at || 0), qrStatus: String(result.qr_status || "ready") } : current));
        } catch { /* The next interval retries without interrupting the task view. */ }
      }
    };
    const timer = window.setInterval(() => { void refreshExpired(); }, 15000);
    void refreshExpired();
    return () => { active = false; window.clearInterval(timer); };
  }, [checkoutSuccesses]);
  useEffect(() => {
    if (activeTaskID) return;
    let mounted = true;
    void apiFetch("/tasks?page=1&page_size=100&platform=chatgpt").then((data) => {
      if (!mounted) return;
      const latest = (data.items || []).find((item: AnyRow) => item.type === "sunny_checkout_link" && !item.terminal);
      if (!latest?.id) return;
      setTask(latest);
      setCheckoutSuccesses([]);
      setSuccessListExpanded(false);
      setCheckoutBusy(true);
      setLogOpen(true);
      setActiveTaskID(String(latest.id));
    }).catch(() => {});
    return () => { mounted = false; };
  }, [activeTaskID]);
  useEffect(() => {
    if (!activeTaskID) return;
    let mounted = true;
    let timer = 0;
    let eventCursor = 0;
    let stream: EventSource | null = null;
    let streamFailures = 0;
    let streamDone = false;

    const waitForNextPoll = () => new Promise<void>((resolve) => { timer = window.setTimeout(resolve, 1000); });
    const applyEvents = (items: AnyRow[]) => {
      const ordered = items.filter((item) => Number(item?.id || 0) > eventCursor).sort((a, b) => Number(a.id || 0) - Number(b.id || 0));
      if (!ordered.length) return;
      eventCursor = Math.max(eventCursor, ...ordered.map((item) => Number(item.id || 0)));
      const visibleEvents = ordered.filter((item) => !(item.type === "log" && item.detail?.action === "checkout.progress"));
      setTaskLogs((old) => { const known = new Set(old.map((item) => Number(item.id || 0))); return [...old, ...visibleEvents.filter((item) => !known.has(Number(item.id || 0)))]; });
      const successfulResults = ordered.flatMap((item) => {
        const detail = item.detail || {};
        const result = item.type === "checkout_result" && detail.result && typeof detail.result === "object" ? detail.result : null;
        const success = result ? checkoutSuccessResult(result, { email: item.email || detail.email, account_id: item.account_id || detail.account_id, index: detail.index }) : null;
        return success ? [success] : [];
      });
      if (successfulResults.length) setCheckoutSuccesses((old) => mergeCheckoutSuccessResults(old, successfulResults));
      setCheckoutLive((old) => {
        const next = { ...old };
        for (const item of ordered) {
          const detail = item.detail || {};
          if (item.type === "log" && detail.action === "checkout.progress") continue;
          if (item.type !== "checkout_progress" && item.type !== "checkout_result" && !detail.email && detail.index == null) continue;
          const key = checkoutLiveKey({ email: item.email || detail.email, account_id: item.account_id || detail.account_id, index: detail.index });
          const previous = next[key] || { progress: 0, message: "等待提链任务", status: "running", logs: [] };
          const result = item.type === "checkout_result" && detail.result && typeof detail.result === "object" ? detail.result : item.type === "checkout_result" ? previous.result : undefined;
          const startsNewAttempt = item.type !== "checkout_result" && previous.status !== "running";
          const logs = startsNewAttempt ? [item] : [...previous.logs, item].slice(-200);
          next[key] = { progress: item.type === "checkout_result" ? 100 : Math.max(startsNewAttempt ? 0 : previous.progress, Number(detail.progress || 0)), message: String(detail.current_log || item.message || previous.message), status: item.type === "checkout_result" ? (result?.status === "succeeded" ? "succeeded" : "failed") : "running", result, logs };
        }
        return next;
      });
    };
    const readEvents = async () => {
      const collected: AnyRow[] = [];
      let readCursor = eventCursor;
      for (let eventPage = 0; eventPage < 5 && mounted; eventPage += 1) {
        const data = await apiFetch(`/tasks/${encodeURIComponent(activeTaskID)}/events?since=${readCursor}&limit=200`);
        const next = data.items || [];
        if (!next.length) break;
        readCursor = Number(next[next.length - 1].id || readCursor);
        collected.push(...next);
        if (next.length < 200) break;
      }
      if (!mounted || !collected.length) return;
      applyEvents(collected);
    };
    const openStream = () => {
      if (stream || streamDone) return;
      const apiBase = String(API_BASE || "/api").replace(/\/$/, "");
      const source = new EventSource(`${apiBase}/tasks/${encodeURIComponent(activeTaskID)}/logs/stream?since=${eventCursor}`, { withCredentials: true });
      stream = source;
      source.onopen = () => { streamFailures = 0; };
      source.onmessage = (message) => { try { const payload = JSON.parse(message.data || "{}"); if (payload.done) { streamDone = true; source.close(); stream = null; return; } applyEvents([payload]); } catch { /* Ignore malformed event and keep polling. */ } };
      source.onerror = () => { source.close(); if (stream === source) stream = null; streamFailures += 1; };
    };
    const watchTask = async () => {
      let consecutiveFailures = 0;
      while (mounted) {
        try {
          openStream();
          const current = await apiFetch(`/tasks/${encodeURIComponent(activeTaskID)}`);
          if (!mounted) return;
          setTask(current);
          const completedSuccesses = (Array.isArray(current?.result?.items) ? current.result.items : []).flatMap((item: AnyRow) => {
            const success = checkoutSuccessResult(item);
            return success ? [success] : [];
          });
          if (completedSuccesses.length) setCheckoutSuccesses((old) => mergeCheckoutSuccessResults(old, completedSuccesses));
          await readEvents().catch(() => {});
          consecutiveFailures = 0;
          if (current.terminal) {
            await readEvents().catch(() => {});
            setCheckoutBusy(false);
            setRefreshKey((value) => value + 1);
            setNotice(current.status === "succeeded" ? "提链任务完成" : "提链任务结束，请查看结果");
            return;
          }
        } catch (error: any) {
          if (!mounted) return;
          const message = error?.message || String(error);
          if (message.toLowerCase().includes("task not found")) {
            setTask(null);
            setCheckoutSuccesses([]);
            setSuccessListExpanded(false);
            setCheckoutBusy(false);
            setActiveTaskID("");
            return;
          }
          consecutiveFailures += 1;
          if (consecutiveFailures === 1) setNotice("提链任务状态暂时读取失败，正在继续重试");
        }
        if (!stream && !streamDone && streamFailures > 0) await readEvents().catch(() => {});
        await waitForNextPoll();
      }
    };
    void watchTask();
    return () => {
      mounted = false;
      window.clearTimeout(timer);
      stream?.close();
    };
  }, [activeTaskID]);
  useEffect(() => {
    if (!systemAT) return;
    let active = true;
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (sortBy) { params.set("sort_by", sortBy); params.set("sort_order", sortOrder); }
    if (query) params.set("q", query); if (group) params.set("group_id", group); if (status) params.set("status", status); if (planFilter) params.set("plan_type", planFilter); if (trialFilter) params.set("trial_eligibility", trialFilter); if (checkoutFilter) params.set("checkout_kind", checkoutFilter); if (rebindEmailFilter) params.set("rebind_email", rebindEmailFilter); if (trialCountryFilters.length) params.set("trial_countries", trialCountryFilters.join(",")); if (paymentMethods.length) params.set("payment_methods", paymentMethods.join(","));
    setListLoading(true);
    void apiFetch(`/sunny/sessions?${params}`).then((data) => {
      if (!active) return;
      const nextTotal = Number(data.total || 0);
      const lastPage = Math.max(1, Math.ceil(nextTotal / pageSize));
      if (page > lastPage) {
        setPage(lastPage);
        return;
      }
      setSessions(data.items || []);
      setTotal(nextTotal);
      setTrialCountryOptions(Array.isArray(data.trial_country_options) ? data.trial_country_options.map((item: any) => String(item).toUpperCase()) : []);
    }).catch(() => { if (active) { setSessions([]); setTotal(0); } }).finally(() => { if (active) setListLoading(false); });
    return () => { active = false; };
  }, [systemAT, query, group, status, planFilter, trialFilter, checkoutFilter, rebindEmailFilter, trialCountryFilters, paymentMethods, sortBy, sortOrder, page, pageSize, refreshKey]);
  useEffect(() => { setExternalRows(parseExternalRows(externalText)); }, [externalText]);
  useEffect(() => { if (logOpen && logScrollRef.current) logScrollRef.current.scrollTop = logScrollRef.current.scrollHeight; }, [logOpen, taskLogs]);
  const visibleExternalRows = useMemo(() => externalRows.slice((page - 1) * pageSize, page * pageSize), [externalRows, page, pageSize]);
  const baseRows = systemAT ? sessions : visibleExternalRows;
  const rows = useMemo(() => {
    const taskItems = Array.isArray(task?.result?.items) ? task.result.items : [];
    return baseRows.map((row) => {
      const live = checkoutLive[checkoutLiveKey(row)];
      const found = taskItems.find((item: AnyRow) => (item.email && item.email === row.email) || (item.index != null && Number(item.index) === Number(row.index)));
      const terminalLive = found && task?.terminal ? {
        progress: 100,
        message: found.status === "succeeded" ? "提链任务已完成" : (resultError(found) || "提链任务已结束"),
        status: normalized(found.status) === "succeeded" ? "succeeded" : "failed",
        result: found,
        logs: live?.logs || [],
      } : undefined;
      const effectiveLive = terminalLive || live;
      const persisted = row.checkout_result && typeof row.checkout_result === "object" ? row.checkout_result : undefined;
      const result = effectiveLive?.result || found || persisted;
      return result || live ? { ...row, checkout_result: result, checkout_live: effectiveLive } : row;
    });
  }, [baseRows, checkoutLive, task]);
  const listTotal = systemAT ? total : externalRows.length;
  const pageCount = checkoutPageCount(listTotal, pageSize);
  const pageFrom = listTotal ? (page - 1) * pageSize + 1 : 0;
  const pageTo = Math.min(page * pageSize, listTotal);
  const availablePaymentMethods = useMemo(() => Array.from(new Set([...paymentMethodOptions, ...paymentMethods, ...sessions.flatMap((item) => Array.isArray(item.payment_methods) ? item.payment_methods : [])])), [paymentMethods, sessions]);
  const selectedCount = selected.length;
  const allCurrentSelected = rows.length > 0 && rows.every((row) => selected.includes(checkoutSelectionKey(row, systemAT)));
  const selectedExternalRows = useMemo(
    () => externalRows.filter((row) => selected.includes(checkoutSelectionKey(row, false))),
    [externalRows, selected],
  );
  function switchMode(value: boolean) {
    setSystemAT(value);
    setSelected([]);
    setPage(1);
    if (value) {
      setStatus("已注册");
      setPlanFilter("free");
      setTrialFilter("eligible");
    }
  }
  function changeFilter(setter: (value: string) => void, value: string) { setter(value); setPage(1); }
  function toggleRow(row: AnyRow) {
    const key = checkoutSelectionKey(row, systemAT);
    setSelected((old) => old.includes(key) ? old.filter((value) => value !== key) : [...old, key]);
  }
  function toggleCurrentPage() {
    const pageKeys = rows.map((row) => checkoutSelectionKey(row, systemAT));
    setSelected((old) => allCurrentSelected
      ? old.filter((value) => !pageKeys.includes(value))
      : Array.from(new Set([...old, ...pageKeys])));
  }
  function changePage(value: number) { setPage(Math.min(pageCount, Math.max(1, value))); }
  function changePageSize(value: number) { setPageSize(value); setPage(1); }
  function refreshRows() { if (systemAT) setRefreshKey((value) => value + 1); else setExternalRows(parseExternalRows(externalText)); setNotice("账户列表已刷新"); window.setTimeout(() => setNotice(""), 1800); }
  async function selectAllRows() {
    setSelectingAll(true);
    try {
      if (!systemAT) {
        if (selectedExternalRows.length === externalRows.length) {
          setNotice("当前账户已全部选择，请使用清除选择");
          return;
        }
        setSelected(externalRows.map((row) => checkoutSelectionKey(row, false)));
        setNotice(`已选择 ${externalRows.length} 个账户`);
        return;
      }
      const params = new URLSearchParams({ selection: "all" });
      if (query) params.set("q", query);
      if (group) params.set("group_id", group);
      if (status) params.set("status", status);
      if (planFilter) params.set("plan_type", planFilter);
      if (trialFilter) params.set("trial_eligibility", trialFilter);
      if (checkoutFilter) params.set("checkout_kind", checkoutFilter);
      if (rebindEmailFilter) params.set("rebind_email", rebindEmailFilter);
      if (trialCountryFilters.length) params.set("trial_countries", trialCountryFilters.join(","));
      if (paymentMethods.length) params.set("payment_methods", paymentMethods.join(","));
      const data = await apiFetch(`/sunny/sessions?${params.toString()}`);
      const ids = Array.from(new Set<number>((data.ids || []).map((value: any) => Number(value)).filter((value: number) => value > 0)));
      setSelected(ids);
      setNotice(ids.length ? `已选择 ${ids.length} 个账户` : "当前筛选没有可选账户");
    } catch (error: any) {
      setNotice(error.message || String(error));
    } finally {
      setSelectingAll(false);
    }
  }
  function updateCheckoutProxies(value: string) {
    setCheckoutProxies(value);
    writeProxyPoolsForPath(linkType, value, promotionProxies);
  }
  function updatePromotionProxies(value: string) {
    setPromotionProxies(value);
    writeProxyPoolsForPath(linkType, checkoutProxies, value);
  }
  function updatePath(value: string) {
    if (value !== linkType) {
      writeProxyPoolsForPath(linkType, checkoutProxies, promotionProxies);
      const targetPools = readProxyPoolsByPath()[value] ?? { checkout: "", promotion: "" };
      setCheckoutProxies(targetPools.checkout);
      setPromotionProxies(targetPools.promotion);
      setLinkType(value);
    }
    const provider = providers.find((item) => item.value === value);
    if (provider) {
      setCountry(provider.country);
      setCurrency(provider.currency);
      setPromoCountry(provider.country);
    }
  }
  async function precheck() {
    const promoRequested = plan === "plus" && usePromo;
    const promotionRequired = promoRequested && linkType !== "gcash";
    const effectivePromotionProxies = linkType === "gcash" || !promoRequested ? checkoutProxies : promotionProxies;
    if (!splitLines(checkoutProxies).length || (promotionRequired && !splitLines(effectivePromotionProxies).length) || !selected.length) {
      setNotice(linkType === "gcash" ? "请先填写 PH Checkout 代理池并勾选账户" : promotionRequired ? "请先填写两个代理池并勾选账户" : "请先填写 Checkout 代理池并勾选账户");
      return;
    }
    if (!promoRequested) { setNotice("当前未开启 Plus 优惠，无需进行试用资格检测"); return; }
    setPrecheckBusy(true);
    try {
      const data = await apiFetch("/sunny/checkout/precheck", { method: "POST", body: JSON.stringify({ system_at: systemAT, session_ids: systemAT ? selected : [], external_ats: systemAT ? [] : selectedExternalRows.map((x) => x.token), checkout_proxies: checkoutProxies, promotion_proxies: effectivePromotionProxies, use_promo: promoRequested, country, currency }) });
      const byEmail = new Map((data.items || []).map((item: AnyRow) => [item.email, item]));
      const apply = (old: AnyRow[]) => old.map((row) => {
        const found = byEmail.get(row.email) as AnyRow | undefined;
        if (!found) return row;
        return {
          ...row,
          trial_eligibility: found.trial_eligibility,
          trial_message: found.trial_message,
          checkout_kind: found.checkout_kind,
          payment_methods: found.payment_methods,
          checkout_error: found.checkout_error,
          commerce_check_error: found.check_error || found.checkout_error,
        };
      });
      if (systemAT) setSessions(apply); else setExternalRows(apply);
      setNotice("试用资格与 Checkout 检测完成");
    } catch (error: any) { setNotice(error.message || String(error)); } finally { setPrecheckBusy(false); }
  }
  async function copy(value: string) { await navigator.clipboard?.writeText(value); setNotice("支付链接已复制"); window.setTimeout(() => setNotice(""), 1800); }
  async function cancelTask() {
    if (!task?.id || task.terminal || cancelBusy) return;
    setCancelBusy(true);
    try {
      await apiFetch(`/tasks/${encodeURIComponent(String(task.id))}/cancel`, { method: "POST" });
      setNotice("已请求停止提链任务");
      const current = await apiFetch(`/tasks/${encodeURIComponent(String(task.id))}`);
      setTask(current);
    } catch (error: any) { setNotice(error.message || String(error)); } finally { setCancelBusy(false); }
  }
  async function start() {
    const promoRequested = plan === "plus" && usePromo;
    const promotionRequired = promoRequested && linkType !== "gcash";
    const effectivePromotionProxies = linkType === "gcash" || !promoRequested ? checkoutProxies : promotionProxies;
    if (!splitLines(checkoutProxies).length || (promotionRequired && !splitLines(effectivePromotionProxies).length)) {
      setNotice(linkType === "gcash" ? "GCash 必须填写 PH Checkout 代理池" : promotionRequired ? "Checkout 代理池和 Promotion 代理池都必须填写" : "请先填写 Checkout 代理池");
      return;
    }
    if (!selected.length) { setNotice("请先勾选需要提链的账户"); return; }
    setCheckoutBusy(true); setTask(null); setTaskLogs([]); setCheckoutSuccesses([]); setSuccessListExpanded(false); setDetailKey(""); setLogOpen(true);
    setCheckoutLive((old) => {
      const visibleRows = systemAT ? sessions : externalRows;
      const currentBatchKeys = new Set(visibleRows.filter((row) => selected.includes(checkoutSelectionKey(row, systemAT))).map((row) => checkoutLiveKey(row)));
      if (!currentBatchKeys.size) return old;
      return Object.fromEntries(Object.entries(old).filter(([key]) => !currentBatchKeys.has(key)));
    });
    try {
      const response = await apiFetch("/sunny/checkout", { method: "POST", body: JSON.stringify({ system_at: systemAT, session_ids: systemAT ? selected : [], external_ats: systemAT ? [] : selectedExternalRows.map((x) => x.token), checkout_kinds: systemAT ? [] : selectedExternalRows.map((x) => normalized(x.checkout_kind) || "unknown"), checkout_proxies: checkoutProxies, promotion_proxies: effectivePromotionProxies, plan, link_type: linkType, country, currency, retry_count: retryCount, concurrency, use_promo: promoRequested, promo_campaign: promoRequested ? promoCampaign : "", promo_country: linkType === "gcash" ? "PH" : promoCountry, promo_code: promoCode, ideal_bank: idealBank, workspace_name: workspaceName, workspace_id: workspaceId, seat_quantity: seatQuantity, price_interval: priceInterval, credit_quantity: creditQuantity, pix_tax_id: pixTaxID, pix_auto_kind: pixAutoKind }) });
      const taskID = String(response.id || response.task_id);
      setTask(response);
      setActiveTaskID(taskID);
      setNotice("提链任务已提交，任务状态和日志将在后台持续同步");
    } catch (error: any) {
      setCheckoutBusy(false);
      setNotice(error.message || String(error));
    }
  }
  const statusLabel = task ? `${task.status} · ${task.progress || ""}` : selectedCount ? `已选择 ${selectedCount} 个账户` : "未选择账户";
  const detailRow = detailKey ? rows.find((row) => checkoutLiveKey(row) === detailKey) : undefined;
  const detailState = detailKey ? checkoutLive[detailKey] : undefined;
  return <div className="checkout-manager space-y-5">
    {notice && <div className="checkout-toast fixed right-5 top-20 z-[500] rounded-xl bg-[var(--accent)] px-4 py-3 text-sm font-semibold text-white shadow-xl" role="status">{notice}</div>}
    <section className="checkout-heading rounded-2xl border border-[var(--border)] bg-[var(--bg-shell)] p-5 shadow-sm"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-bold uppercase tracking-[0.16em] text-[var(--accent)]">PAYMENT ROUTER</p><h1 className="mt-1 text-2xl font-black">提链管理</h1><p className="mt-2 text-sm text-[var(--text-secondary)]">为已注册 ChatGPT 账户批量提取支付链接、跳转地址和支付二维码。</p></div><div className="rounded-full border border-[var(--border)] px-3 py-1 text-xs text-[var(--text-muted)]">{statusLabel}</div></div></section>
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--bg-shell)] p-5"><div className={`grid gap-4 ${linkType === "gcash" ? "md:grid-cols-1" : "md:grid-cols-2"}`}><label><span className="mb-2 block text-sm font-semibold">{linkType === "gcash" ? "PH Checkout 代理池" : "Checkout 代理池"} <b className="text-red-500">*</b></span><textarea className="min-h-28 w-full rounded-xl border border-[var(--border)] bg-transparent p-3 text-sm outline-none focus:border-[var(--accent)]" value={checkoutProxies} onChange={(e) => updateCheckoutProxies(e.target.value)} placeholder="每行一个代理，支持 http://、https://、socks5://" /></label>{linkType !== "gcash" && <label><span className="mb-2 block text-sm font-semibold">Promotion 代理池 <b className="text-red-500">*</b></span><textarea className="min-h-28 w-full rounded-xl border border-[var(--border)] bg-transparent p-3 text-sm outline-none focus:border-[var(--accent)]" value={promotionProxies} onChange={(e) => updatePromotionProxies(e.target.value)} placeholder="每行一个代理，支持 http://、https://、socks5://" /></label>}</div><p className="mt-3 text-xs text-[var(--text-muted)]">{linkType === "gcash" ? "GCash 每次尝试选用一个 PH 代理，并复用同一会话完成 Checkout、taxes、confirm 与 start。" : "每个代理池最多 500 条。每轮重试会重新选择代理组合；Checkout 创建与支付处理使用 Checkout 代理池，试用检查与优惠更新使用 Promotion 代理池。"}</p></section>
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--bg-shell)] p-5"><h2 className="mb-3 text-sm font-semibold">订阅 / 空间</h2><div className="grid grid-cols-2 gap-2 md:grid-cols-4">{planOptions.map((item) => <button key={item.value} type="button" onClick={() => setPlan(item.value)} className={`rounded-xl border p-3 text-left transition ${plan === item.value ? "border-[var(--accent)] bg-[var(--accent)]/10" : "border-[var(--border)] hover:border-[var(--accent)]/60"}`}><b className="block">{item.label}</b><small className="text-xs text-[var(--text-muted)]">{item.hint}</small></button>)}</div>{plan === "team" && <div className="mt-4 grid gap-3 md:grid-cols-4"><input className="rounded-xl border border-[var(--border)] bg-transparent px-3 py-2 text-sm" value={workspaceName} onChange={(e) => setWorkspaceName(e.target.value)} placeholder="空间名称" /><input className="rounded-xl border border-[var(--border)] bg-transparent px-3 py-2 text-sm" value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)} placeholder="已有空间 ID" /><input className="rounded-xl border border-[var(--border)] bg-transparent px-3 py-2 text-sm" type="number" min={2} value={seatQuantity} onChange={(e) => setSeatQuantity(Number(e.target.value))} placeholder="席位数量" /><select className="rounded-xl border border-[var(--border)] bg-transparent px-3 py-2 text-sm" value={priceInterval} onChange={(e) => setPriceInterval(e.target.value)}><option value="month">按月</option><option value="year">按年</option></select></div>}{plan === "codex_low" && <div className="mt-4 grid gap-3 md:grid-cols-2"><input className="rounded-xl border border-[var(--border)] bg-transparent px-3 py-2 text-sm" value={workspaceName} onChange={(e) => setWorkspaceName(e.target.value)} placeholder="Codex 空间名称" /><input className="rounded-xl border border-[var(--border)] bg-transparent px-3 py-2 text-sm" type="number" min={1} value={creditQuantity} onChange={(e) => setCreditQuantity(Number(e.target.value))} placeholder="积分数量" /></div>}</section>
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--bg-shell)] p-5">
      <h2 className="mb-3 text-sm font-semibold">支付路径</h2>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">{providers.map((item) => <button key={item.value} type="button" onClick={() => updatePath(item.value)} className={`rounded-xl border px-3 py-2 text-left ${linkType === item.value ? "border-[var(--accent)] bg-[var(--accent)]/10" : "border-[var(--border)]"}`}><b className="block text-sm">{item.label}</b><small className="block truncate text-[11px] text-[var(--text-muted)]">{item.hint}</small></button>)}</div>
      <div className="mt-4 flex flex-wrap items-end gap-3">
        <label className="w-56 max-w-full"><span className="mb-1 block text-xs text-[var(--text-muted)]">国家 / 地区</span><select className="h-10 w-full rounded-xl border border-[var(--border)] bg-transparent px-3 text-sm" value={country} onChange={(e) => { setCountry(e.target.value); setCurrency(countries[e.target.value] || currencyByCountry[e.target.value] || "USD"); }}>{Object.keys(countries).map((key) => <option key={key} value={key}>{key} · {countryNames[key] || key}</option>)}</select></label>
        <label className="w-36 max-w-full"><span className="mb-1 block text-xs text-[var(--text-muted)]">币种</span><select className="h-10 w-full rounded-xl border border-[var(--border)] bg-transparent px-3 text-sm" value={currency} onChange={(e) => setCurrency(e.target.value)}>{Object.values(countries).filter((x, i, a) => a.indexOf(x) === i).map((value) => <option key={value}>{value}</option>)}</select></label>
        <label className="w-56 max-w-full"><span className="mb-1 block text-xs text-[var(--text-muted)]">Promotion 国家 / 地区</span><select className="h-10 w-full rounded-xl border border-[var(--border)] bg-transparent px-3 text-sm" value={promoCountry} onChange={(e) => setPromoCountry(e.target.value)}><option value="">按支付路径默认</option>{Object.keys(countries).map((key) => <option key={key} value={key}>{key} · {countryNames[key] || key}</option>)}{!countries.TR && <option value="TR">TR · 土耳其</option>}</select></label>
        {linkType === "ideal" && <label className="w-60 max-w-full"><span className="mb-1 block text-xs text-[var(--text-muted)]">iDEAL 银行</span><select className="h-10 w-full rounded-xl border border-[var(--border)] bg-transparent px-3 text-sm" value={idealBank} onChange={(e) => setIdealBank(e.target.value)}><option value="">在 iDEAL 支付页面选择</option></select></label>}
      </div>
      {linkType === "pix" && <div className="mt-3 grid gap-3 md:grid-cols-2"><select className="rounded-xl border border-[var(--border)] bg-transparent px-3 py-2 text-sm" value={pixAutoKind} onChange={(e) => setPixAutoKind(e.target.value)}><option value="cpf">主要生成 CPF</option><option value="mixed">CPF / CNPJ 交替</option><option value="cnpj">仅生成 CNPJ</option></select><input className="rounded-xl border border-[var(--border)] bg-transparent px-3 py-2 text-sm" value={pixTaxID} onChange={(e) => setPixTaxID(e.target.value)} placeholder="固定 CPF / CNPJ（选填）" /></div>}
    </section>
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--bg-shell)] p-5">
      <div className="flex flex-wrap items-end gap-3">
        <label className="w-64 max-w-full"><span className="mb-1 flex h-5 items-center gap-2 text-xs text-[var(--text-muted)]"><span>优惠配置</span><button type="button" className={`sr-switch-only scale-90 ${usePromo ? "on" : ""}`} aria-label="启用优惠配置" onClick={(event) => { event.preventDefault(); setUsePromo(!usePromo); }}><span /></button></span><input className="h-10 w-full rounded-xl border border-[var(--border)] bg-transparent px-3 text-sm" value={promoCampaign} onChange={(e) => setPromoCampaign(e.target.value)} disabled={!usePromo || plan !== "plus"} placeholder="Plus Campaign" /></label>
        <label className="w-52 max-w-full"><span className="mb-1 flex h-5 items-center text-xs text-[var(--text-muted)]">优惠码（Team）</span><input className="h-10 w-full rounded-xl border border-[var(--border)] bg-transparent px-3 text-sm" value={promoCode} onChange={(e) => setPromoCode(e.target.value)} disabled={plan !== "team"} placeholder="选填" /></label>
        <label><span className="mb-1 flex h-5 items-center text-xs text-[var(--text-muted)]">失败重试次数</span><input className="h-10 w-24 rounded-xl border border-[var(--border)] bg-transparent px-3 text-sm" type="number" min={0} max={50} value={retryCount} onChange={(e) => setRetryCount(Number(e.target.value))} /></label>
        <label><span className="mb-1 flex h-5 items-center text-xs text-[var(--text-muted)]">提链并发</span><input className="h-10 w-24 rounded-xl border border-[var(--border)] bg-transparent px-3 text-sm" type="number" min={1} max={100} value={concurrency} onChange={(e) => setConcurrency(Number(e.target.value))} /></label>
        <Button variant="outline" disabled={precheckBusy || selectedCount === 0} onClick={() => void precheck()}>{precheckBusy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{precheckBusy ? "检测中..." : "检测资格 / Checkout"}</Button>
        <Button className="ml-auto" disabled={checkoutBusy || selectedCount === 0} onClick={() => void start()}>{checkoutBusy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}{checkoutBusy ? "提链中..." : "开始提链"}</Button>
      </div>
      {task && <div className="mt-3 rounded-lg border border-[var(--border)] bg-[var(--bg-main)]/40 p-3">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><div className="text-sm font-semibold">最近提链任务</div><div className="mt-1 text-xs text-[var(--text-muted)]">状态：{taskStatusLabel(task.status)} · 进度：{task.progress || "0/0"} · 成功 {task.success ?? task.success_count ?? 0} · 失败 {task.error_count ?? 0}</div></div>{!task.terminal && <Button variant="outline" disabled={cancelBusy || task.status === "cancel_requested"} onClick={() => void cancelTask()}>{cancelBusy || task.status === "cancel_requested" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <X className="mr-2 h-4 w-4" />}{cancelBusy || task.status === "cancel_requested" ? "停止中..." : "停止提链"}</Button>}</div>
        {task.terminal && <div className={`mt-2 text-xs ${task.status === "succeeded" ? "text-emerald-600" : "text-red-500"}`}>{task.status === "succeeded" ? `任务完成：成功 ${task.success ?? task.success_count ?? 0}，失败 ${task.error_count ?? 0}` : `任务结束：${task.error || "请查看下方账户结果"}`}</div>}
        <div className="mt-3 border-t border-[var(--border)] pt-3">
          <button type="button" className="flex w-full items-center gap-2 text-left text-xs font-semibold disabled:cursor-default" aria-expanded={successListExpanded} disabled={!checkoutSuccesses.length} onClick={() => setSuccessListExpanded((value) => !value)}><span>成功账户</span><span className="rounded-md bg-emerald-500/10 px-1.5 py-0.5 text-[11px] text-emerald-600 dark:text-emerald-400" aria-live="polite">{checkoutSuccesses.length}</span>{checkoutSuccesses.length > 0 && <span className="ml-auto inline-flex items-center gap-1 text-[var(--text-muted)]">{successListExpanded ? "收起" : "展开"}{successListExpanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}</span>}</button>
          {successListExpanded && checkoutSuccesses.length > 0 && <div className="mt-2 overflow-x-auto"><table className="w-full min-w-[860px] table-fixed text-left text-xs"><colgroup><col className="w-[210px]" /><col className="w-[120px]" /><col /><col className="w-[120px]" /><col className="w-[82px]" /></colgroup><thead className="border-y border-[var(--border)] text-[var(--text-muted)]"><tr><th className="p-2">邮箱</th><th className="p-2">支付路径</th><th className="p-2">支付链接</th><th className="p-2">支付二维码</th><th className="p-2">操作</th></tr></thead><tbody>{checkoutSuccesses.map((item) => <tr key={item.key} className="border-b border-[var(--border)]/60"><td className="p-2"><div className="truncate font-medium" title={item.email}>{item.email}</div></td><td className="p-2"><CompactBadge label={labelFor(item.path, pathLabels)} tone={pathTone(item.path)} /></td><td className="p-2"><button className="block w-full truncate text-left font-medium text-[var(--accent)] underline decoration-[var(--accent)]/40 underline-offset-2" title={`${item.link}\n点击复制支付链接`} onClick={() => void copy(item.link)}>{item.link}</button></td><td className="p-2">{item.qrImage ? <QRImageThumb src={item.qrImage} onClick={() => { setQrValue(item.qrData); setQrImage(item.qrImage); }} /> : item.qrData ? <div><QRThumb value={item.qrData} onClick={() => { setQrValue(item.qrData); setQrImage(""); }} />{item.qrExpiresAt > 0 && <QRExpiryLabel expiresAt={item.qrExpiresAt} status={item.qrStatus} />}</div> : <span className="text-[var(--text-muted)]">上游未提供免登录二维码</span>}</td><td className="p-2"><button className="sr-link inline-flex items-center gap-1 whitespace-nowrap" title="复制支付链接" onClick={() => void copy(item.link)}><Clipboard className="h-3 w-3" />复制</button></td></tr>)}</tbody></table></div>}
        </div>
      </div>}
    </section>
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--bg-shell)] p-5">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-3"><span className="text-sm font-semibold">账户 AT</span><button type="button" className={`sr-switch-only ${systemAT ? "on" : ""}`} onClick={() => switchMode(!systemAT)}><span /></button><span className="text-xs text-[var(--text-muted)]">使用系统 AT</span></div>{!systemAT && <div className="flex gap-2"><button className="sr-text-btn" disabled={!selected.length} onClick={() => { const doomed = new Set(selected); const tokens = splitLines(externalText).filter((_, index) => !doomed.has(index)); setExternalText(tokens.join("\n")); setSelected([]); setPage(Math.min(page, Math.max(1, checkoutPageCount(tokens.length, pageSize)))); }}><Trash2 className="h-4 w-4" />删除选中</button><button className="sr-text-btn" onClick={() => { setExternalText(""); setExternalRows([]); setSelected([]); setPage(1); }}><Trash2 className="h-4 w-4" />全部清空</button></div>}</div>
      {!systemAT && <textarea className="mb-3 min-h-28 w-full rounded-xl border border-[var(--border)] bg-transparent p-3 text-sm" value={externalText} onChange={(e) => { setExternalText(e.target.value); setSelected([]); setPage(1); }} placeholder="每行一个 AT，支持任意数量；内容仅保存在当前浏览器本地" />}
      <div className="checkout-filter-toolbar mb-3 flex flex-nowrap items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-main)]/40 p-2">
        {systemAT && <div className="checkout-filter-controls flex min-w-0 flex-1 flex-wrap gap-2">
          <input className="h-9 w-52 shrink-0 rounded-lg border border-[var(--border)] bg-transparent px-3 text-xs outline-none focus:border-[var(--accent)]" value={query} onChange={(e) => changeFilter(setQuery, e.target.value)} placeholder="搜索邮箱/换绑邮箱" />
          <select aria-label="分组筛选" className="h-9 w-40 shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-shell)] px-2 text-xs" value={group} onChange={(e) => changeFilter(setGroup, e.target.value)}><option value="">全部分组</option>{groups.map((item) => <option key={item.id} value={String(item.id)}>{item.name || `分组 ${item.id}`}</option>)}</select>
          <select aria-label="状态筛选" className="h-9 w-32 shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-shell)] px-2 text-xs" value={status} onChange={(e) => changeFilter(setStatus, e.target.value)}><option value="">全部状态</option>{sessionStatuses.map((value) => <option key={value} value={value}>{value}</option>)}</select>
          <select aria-label="套餐筛选" className="h-9 w-32 shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-shell)] px-2 text-xs" value={planFilter} onChange={(e) => changeFilter(setPlanFilter, e.target.value)}><option value="">全部套餐</option>{sessionPlans.map((value) => <option key={value} value={value}>{planLabels[value]}</option>)}</select>
          <select aria-label="试用资格筛选" className="h-9 w-36 shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-shell)] px-2 text-xs" value={trialFilter} onChange={(e) => changeFilter(setTrialFilter, e.target.value)}><option value="">全部试用资格</option><option value="eligible">有0元试用</option><option value="ineligible">无0元试用</option><option value="unknown">未检测</option></select>
          <select aria-label="Checkout 类型筛选" className="h-9 w-40 shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-shell)] px-2 text-xs" value={checkoutFilter} onChange={(e) => changeFilter(setCheckoutFilter, e.target.value)}><option value="">全部 Checkout</option><option value="oaics">OAICS</option><option value="cs_live">CS Live</option><option value="cs_test">CS Test</option><option value="unknown">未检测</option></select>
          <PaymentMethodFilter value={paymentMethods} options={availablePaymentMethods} onChange={(value) => { setPaymentMethods(value); setPage(1); }} />
        </div>}
        <div className="sr-selection-summary shrink-0" aria-live="polite"><button type="button" className="sr-select-all" disabled={selectingAll || listTotal <= 0} onClick={() => void selectAllRows()}>{selectingAll ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ListChecks className="h-3.5 w-3.5" />}<span>全选</span></button>{selectedCount > 0 && <><span className="sr-selected-count">已选择 {selectedCount} 项</span><button type="button" className="sr-clear-selection" onClick={() => setSelected([])}>清除选择</button></>}</div>
        <button type="button" className="sr-text-btn ml-auto" disabled={listLoading} onClick={refreshRows}>{listLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}刷新列表</button>
      </div>
      <div className="checkout-account-table-scroll relative overflow-x-auto">{listLoading && <div className="absolute inset-0 z-30 flex items-center justify-center bg-[var(--bg-shell)]/70"><Loader2 className="h-5 w-5 animate-spin text-[var(--accent)]" /></div>}<ResizableCheckoutAccountTable headers={[<input type="checkbox" aria-label="本页全选" checked={allCurrentSelected} disabled={!rows.length} onChange={toggleCurrentPage} />, "邮箱", <CheckoutPresenceFilter label="换绑邮箱" value={rebindEmailFilter} onChange={(value) => { setRebindEmailFilter(value); setPage(1); }} title="筛选换绑邮箱" />, "分组", "状态", "套餐", <CheckoutTrialCountryFilter value={trialCountryFilters} options={trialCountryOptions} onChange={(value) => { setTrialCountryFilters(value); setPage(1); }} />, "Checkout 类型", "支付路径", "支付链接", "支付二维码", "操作"]}><tbody>{rows.length ? rows.map((row, index) => { const result = row.checkout_result || {}; const live = row.checkout_live as CheckoutLiveState | undefined; const link = resultDisplayLink(result); const error = resultError(result); const failed = normalized(result.status) === "failed"; const statusValue = row.at_status || row.status; const planValue = row.plan_type; const pathValue = result.link_type; const detail = [result.plan && `套餐 ${labelFor(result.plan, planLabels)}`, result.country && `地区 ${result.country}`, result.currency && `币种 ${result.currency}`, result.checkout_amount != null && `金额 ${result.checkout_amount}`, result.payment_methods?.length && `支付方式 ${result.payment_methods.join(", ")}`, result.checkout_session_id && `会话 ${result.checkout_session_id}`, result.promo_requested != null && `优惠 ${result.promo_applied === true ? "已生效" : result.promo_applied === false ? "未生效" : "待确认"}`].filter(Boolean).join(" · "); const qrImage = resultQrImage(result); const rowKey = checkoutSelectionKey(row, systemAT); return <tr key={`${row.id || row.index || index}`} className="border-b border-[var(--border)]/60"><td className="checkout-sticky checkout-sticky-left p-2"><input type="checkbox" checked={selected.includes(rowKey)} onChange={() => toggleRow(row)} /></td><td className="checkout-sticky checkout-sticky-left p-2" style={{ left: "var(--checkout-checkbox-width)" }}><div className="truncate" title={row.email}>{row.email || "未知邮箱"}</div>{live && <div className="mt-1 w-full"><div className="mb-0.5 flex justify-between gap-2 text-[10px] text-[var(--text-muted)]"><span className="truncate">{live.message || "提链处理中"}</span><span>{Math.round(live.progress)}%</span></div><div className="h-1 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"><div className={`h-full rounded-full transition-[width] ${live.status === "failed" ? "bg-red-500" : live.status === "succeeded" ? "bg-emerald-500" : "bg-[var(--accent)]"}`} style={{ width: `${Math.max(0, Math.min(100, live.progress))}%` }} /></div></div>}</td><td className="p-2"><div className="truncate" title={row.rebind_email || "-"}>{row.rebind_email || "-"}</div></td><td className="p-2"><div className="truncate" title={row.group_name || "-"}>{row.group_name || "-"}</div></td><td className="p-2"><AccountStatusBadge value={statusValue} /></td><td className="p-2"><AccountPlanBadge value={planValue} /></td><td className="p-2"><AccountTrialValue row={row} /></td><td className="p-2"><AccountCheckoutValue row={row} /></td><td className="p-2">{pathValue ? <CompactBadge label={labelFor(pathValue, pathLabels)} tone={pathTone(pathValue)} /> : <span className="text-[var(--text-muted)]">-</span>}</td><td className="p-2">{link ? <button className="block w-full truncate text-left font-medium text-[var(--accent)] underline decoration-[var(--accent)]/40 underline-offset-2 hover:decoration-[var(--accent)]" title={`${link}\n${detail}\n点击复制支付链接`} onClick={() => void copy(link)}>{link}</button> : error || failed ? <div className="truncate font-medium text-red-600 dark:text-red-400" title={`提链失败：${error || "任务未返回支付链接"}`}>提链失败：{error || "任务未返回支付链接"}</div> : live?.status === "running" ? <span className="text-[var(--text-muted)]">提链中...</span> : <span className="text-[var(--text-muted)]">-</span>}</td><td className="p-2">{qrImage ? <QRImageThumb src={qrImage} onClick={() => { setQrValue(result.qr_data || ""); setQrImage(qrImage); }} /> : result.qr_data ? <QRThumb value={result.qr_data} onClick={() => { setQrValue(result.qr_data); setQrImage(""); }} /> : <span className="text-[var(--text-muted)]">-</span>}</td><td className="checkout-sticky checkout-sticky-right p-2"><div className="flex items-center gap-1"><button className="sr-link inline-flex items-center gap-1 whitespace-nowrap" title="查看当前账户提链日志" onClick={() => setDetailKey(checkoutLiveKey(row))}><FileText className="h-3 w-3" />详情</button>{link && <><button className="sr-link inline-flex items-center gap-1 whitespace-nowrap" title="复制支付链接" onClick={() => void copy(link)}><Clipboard className="h-3 w-3" />复制</button><button className="sr-link inline-flex items-center gap-1 whitespace-nowrap" title="在新窗口打开支付链接" onClick={() => window.open(link, "_blank", "noopener,noreferrer")}><ExternalLink className="h-3 w-3" />打开</button></>}</div></td></tr>; }) : <tr><td colSpan={12} className="p-10 text-center text-sm text-[var(--text-muted)]">暂无账户，请先导入或选择 AT。</td></tr>}</tbody></ResizableCheckoutAccountTable></div>
      <div className="sr-pagination">
        <div className="sr-pagination-left"><span className="sr-pagination-range">显示 {pageFrom} 至 {pageTo}，共 {listTotal} 条</span><span className="sr-page-size-label">每页:</span><select className="sr-page-size-select" value={pageSize} onChange={(e) => changePageSize(Number(e.target.value))}>{[10, 20, 50, 100].map((value) => <option key={value} value={value}>{value}</option>)}</select></div>
        <div className="sr-pagination-actions" aria-label="pagination"><button type="button" className="sr-page-nav" title="上一页" disabled={page <= 1 || listLoading} onClick={() => changePage(page - 1)}>‹</button>{checkoutPaginationTokens(page, pageCount).map((token, index) => token === "..." ? <span key={`ellipsis-${index}`} className="sr-page-ellipsis">...</span> : <button type="button" key={token} className={`sr-page-number ${token === page ? "active" : ""}`} onClick={() => changePage(token)}>{token}</button>)}<button type="button" className="sr-page-nav" title="下一页" disabled={page >= pageCount || listLoading} onClick={() => changePage(page + 1)}>›</button></div>
      </div>
    </section>
    {(qrValue || qrImage) && <QRModal value={qrValue} image={qrImage} onClose={() => { setQrValue(""); setQrImage(""); }} />}
    {detailKey && <CheckoutAccountDetail email={detailRow?.email || detailKey.replace(/^email:/, "")} state={detailState} onClose={() => setDetailKey("")} />}
    <CheckoutLogFloat open={logOpen} onToggle={() => setLogOpen((value) => !value)} task={task} logs={taskLogs} scrollRef={logScrollRef} />
  </div>;
}
