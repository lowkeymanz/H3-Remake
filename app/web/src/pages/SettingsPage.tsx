// 页面 4（设置部分）：LLM/Painter API 配置、路径展示、预算默认值。
// api_key 掩码口径：后端只回 api_key_set；留空提交 = 保留现场密钥。

import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import type { Settings } from "../types";

interface SectionForm {
  model: string;
  base_url: string;
  api_key: string;
}

const EMPTY_SECTION: SectionForm = { model: "", base_url: "", api_key: "" };

export function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [llm, setLlm] = useState<SectionForm>(EMPTY_SECTION);
  const [painter, setPainter] = useState<SectionForm>(EMPTY_SECTION);
  const [budget, setBudget] = useState("24");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.settings().then((s) => {
      setSettings(s);
      setLlm({ model: s.llm.model, base_url: s.llm.base_url, api_key: "" });
      setPainter({ model: s.painter.model, base_url: s.painter.base_url, api_key: "" });
      setBudget(String(s.budget_default));
    }).catch((exc) => setError(exc instanceof Error ? exc.message : String(exc)));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    const maxImages = Number(budget);
    if (!Number.isInteger(maxImages) || maxImages < 1) {
      return setError("预算默认值须为正整数");
    }
    setBusy(true);
    try {
      const updated = await api.saveSettings({
        llm: { model: llm.model, base_url: llm.base_url, api_key: llm.api_key },
        painter: { model: painter.model, base_url: painter.base_url, api_key: painter.api_key },
        budget: { max_images: maxImages },
      });
      setSettings(updated);
      setLlm((prev) => ({ ...prev, api_key: "" }));
      setPainter((prev) => ({ ...prev, api_key: "" }));
      setNotice("设置已保存到 config.toml。");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  function sectionForm(title: string, value: SectionForm, setter: (v: SectionForm) => void,
                       keySet: boolean, prefix: string) {
    return (
      <section className="panel">
        <h2>{title}</h2>
        <label htmlFor={`${prefix}-model`}>模型</label>
        <input id={`${prefix}-model`} value={value.model}
               onChange={(e) => setter({ ...value, model: e.target.value })} />
        <label htmlFor={`${prefix}-base`}>Base URL</label>
        <input id={`${prefix}-base`} value={value.base_url}
               onChange={(e) => setter({ ...value, base_url: e.target.value })} />
        <label htmlFor={`${prefix}-key`}>API Key（{keySet ? "已配置，留空保留" : "未配置"}）</label>
        <input id={`${prefix}-key`} type="password" value={value.api_key}
               placeholder={keySet ? "••••••••（留空保留现场密钥）" : "填入 API key"}
               onChange={(e) => setter({ ...value, api_key: e.target.value })} />
      </section>
    );
  }

  if (!settings) {
    return <p className="muted">{error || "加载中…"}</p>;
  }

  return (
    <div>
      <h1>设置</h1>
      <form onSubmit={submit} aria-label="设置" noValidate>
        {sectionForm("LLM（多模态大模型）", llm, setLlm, settings.llm.api_key_set, "llm")}
        {sectionForm("Painter（关键帧绘制）", painter, setPainter, settings.painter.api_key_set, "painter")}
        <section className="panel">
          <h2>预算与路径</h2>
          <label htmlFor="budgetDefault">预算默认值（张，任务创建表单预填）</label>
          <input id="budgetDefault" type="number" min="1" value={budget}
                 onChange={(e) => setBudget(e.target.value)} />
          <label>REMAKE_HOME（config.toml [paths]，只读）</label>
          <input value={settings.remake_home || "（默认 ~/.viggle-remake）"} readOnly />
        </section>
        {error && <p className="error" role="alert">{error}</p>}
        {notice && <p className="ok" role="status">{notice}</p>}
        <button type="submit" disabled={busy}>{busy ? "保存中…" : "保存设置"}</button>
      </form>
    </div>
  );
}