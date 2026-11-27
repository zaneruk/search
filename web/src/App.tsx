import React, { useEffect, useState } from 'react';
import {
  BarChart3,
  Bot,
  Cpu,
  KeyRound,
  Search,
  ServerCrash,
  ShieldCheck,
} from 'lucide-react';
import {
  fetchReadyz,
  getStoredAdminToken,
  setStoredAdminToken,
} from './api/client';
import type { ReadyzResponse } from './api/types';
import { AgentContentPage } from './pages/AgentContentPage';
import { EcomHeadPage } from './pages/EcomHeadPage';
import { OpsCostPage } from './pages/OpsCostPage';
import { StorefrontPage } from './pages/StorefrontPage';
import { cn } from './utils/format';

type TabId = 'storefront' | 'ecom' | 'agent' | 'ops';

const TABS: Array<{ id: TabId; label: string; roleHint: string; icon: React.ComponentType<{ className?: string }> }> = [
  { id: 'storefront', label: '1. Витрина + Trace', roleHint: 'Покупатель / Демо', icon: Search },
  { id: 'ecom', label: '2. E-com & Head', roleHint: 'Менеджер / Руководитель', icon: BarChart3 },
  { id: 'agent', label: '3. Агент и Каталог', roleHint: 'Контент / Продукт', icon: Bot },
  { id: 'ops', label: '4. Ops & Cost', roleHint: 'SLA / Деградация / Стоимость', icon: Cpu },
];

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<TabId>('storefront');
  const [adminToken, setAdminToken] = useState<string>(() => getStoredAdminToken());
  const [showTokenInput, setShowTokenInput] = useState(false);
  const [readyState, setReadyState] = useState<ReadyzResponse | null>(null);
  const [readyOffline, setReadyOffline] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchReadyz(ctrl.signal)
      .then((res) => {
        setReadyState(res);
        setReadyOffline(false);
      })
      .catch((err) => {
        if ((err as Error).name !== 'AbortError') {
          setReadyOffline(true);
        }
      });
    return () => ctrl.abort();
  }, [activeTab]);

  const handleTokenChange = (val: string) => {
    setAdminToken(val);
    setStoredAdminToken(val);
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      {/* Верхняя навигационная панель */}
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-2.5 sm:px-6">
          {/* Бренд */}
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand-600 font-bold text-white shadow-sm">
              SL
            </div>
            <div>
              <div className="flex items-center gap-2 text-sm font-bold text-slate-900">
                Softline Smart Search
                <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-slate-600">
                  MVP 2.0
                </span>
              </div>
              <div className="text-[11px] text-slate-500">
                B2B поиск ПО и ИТ-оборудования (store.softline.ru)
              </div>
            </div>
          </div>

          {/* Навигация по 4 экранам */}
          <nav className="flex flex-wrap items-center gap-1 rounded-xl bg-slate-100 p-1">
            {TABS.map((tab) => {
              const Icon = tab.icon;
              const isActive = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setActiveTab(tab.id)}
                  title={tab.roleHint}
                  className={cn(
                    'inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition-all',
                    isActive
                      ? 'bg-white text-brand-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900',
                  )}
                >
                  <Icon className="h-3.5 w-3.5" />
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </nav>

          {/* Статус /readyz и X-Admin-Token */}
          <div className="flex items-center gap-2">
            {readyState ? (
              <div
                title={`index=${readyState.index_version}, config=v${readyState.config_version}`}
                className="hidden items-center gap-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-800 sm:inline-flex"
              >
                <ShieldCheck className="h-3.5 w-3.5" />
                <span className="font-mono">
                  {readyState.degradation} · v{readyState.config_version}
                </span>
              </div>
            ) : readyOffline ? (
              <div className="hidden items-center gap-1.5 rounded-lg border border-rose-200 bg-rose-50 px-2.5 py-1 text-[11px] font-semibold text-rose-800 sm:inline-flex">
                <ServerCrash className="h-3.5 w-3.5" />
                API offline
              </div>
            ) : null}

            <button
              type="button"
              onClick={() => setShowTokenInput((v) => !v)}
              className={cn(
                'inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-xs font-medium',
                adminToken
                  ? 'border-slate-300 bg-white text-slate-700'
                  : 'border-amber-300 bg-amber-50 text-amber-900',
              )}
              title="Настроить X-Admin-Token для экранов 2–4"
            >
              <KeyRound className="h-3.5 w-3.5" />
              {adminToken ? 'Admin Token ✓' : 'Указать Token'}
            </button>
          </div>
        </div>

        {/* Выпадающая полоса ввода X-Admin-Token */}
        {showTokenInput && (
          <div className="border-t border-slate-200 bg-slate-50 px-4 py-2 sm:px-6">
            <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-2 text-xs">
              <span className="text-slate-600">
                Заголовок <code className="rounded bg-slate-200 px-1">X-Admin-Token</code> для
                вкладок 2–4 (значение <code className="rounded bg-slate-200 px-1">ADMIN_TOKEN</code>{' '}
                из <code className="rounded bg-slate-200 px-1">.env</code>):
              </span>
              <div className="flex items-center gap-2">
                <input
                  type="password"
                  value={adminToken}
                  onChange={(e) => handleTokenChange(e.target.value)}
                  placeholder="например: change-me-admin"
                  className="w-60 rounded-lg border border-slate-300 bg-white px-2.5 py-1 font-mono text-xs focus:border-brand-600 focus:outline-none"
                />
                <button
                  type="button"
                  onClick={() => setShowTokenInput(false)}
                  className="rounded-lg bg-slate-900 px-3 py-1 font-semibold text-white hover:bg-slate-800"
                >
                  Готово
                </button>
              </div>
            </div>
          </div>
        )}
      </header>

      {/* Основная рабочая область */}
      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6">
        {activeTab === 'storefront' && <StorefrontPage />}
        {activeTab === 'ecom' && <EcomHeadPage adminToken={adminToken} />}
        {activeTab === 'agent' && <AgentContentPage adminToken={adminToken} />}
        {activeTab === 'ops' && <OpsCostPage adminToken={adminToken} />}
      </main>
    </div>
  );
};

export default App;