// 页面 4（角色库部分）：上传/浏览/删除角色 sheet 与 photo。
// 落位口径与 MCP sheet(source="lib") 一致：chars/NAME_sheet.png + NAME_photo.png。

import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import type { CharEntry } from "../types";

export function CharsPage() {
  const [chars, setChars] = useState<CharEntry[]>([]);
  const [name, setName] = useState("");
  const [sheet, setSheet] = useState<File | null>(null);
  const [desc, setDesc] = useState("");
  const [outfit, setOutfit] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = () => api.chars().then(setChars).catch(() => undefined);
  useEffect(() => {
    refresh();
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (!name.trim()) return setError("请填写角色名");
    if (!sheet) return setError("请选择角色 sheet 图片");
    setBusy(true);
    try {
      await api.uploadSheet(name.trim(), sheet, desc.trim(), outfit.trim());
      setNotice(`角色 ${name.trim()} 已入库。`);
      setName("");
      setSheet(null);
      setDesc("");
      setOutfit("");
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  async function uploadPhoto(charName: string, file: File) {
    setError("");
    try {
      await api.uploadPhoto(charName, file);
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function remove(charName: string) {
    setError("");
    try {
      await api.deleteChar(charName);
      setNotice(`角色 ${charName} 已删除。`);
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  return (
    <div>
      <h1>角色库</h1>
      <form className="panel" onSubmit={submit} aria-label="上传角色" noValidate>
        <div className="row">
          <div>
            <label htmlFor="charNameInput">角色名</label>
            <input id="charNameInput" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div>
            <label htmlFor="sheetFile">角色 sheet（设定图）</label>
            <input id="sheetFile" type="file" accept="image/*"
                   onChange={(e) => setSheet(e.target.files?.[0] ?? null)} />
          </div>
        </div>
        <div className="row">
          <div>
            <label htmlFor="charDesc">角色描述（可选）</label>
            <input id="charDesc" value={desc} onChange={(e) => setDesc(e.target.value)} />
          </div>
          <div>
            <label htmlFor="charOutfit">服装描述（可选）</label>
            <input id="charOutfit" value={outfit} onChange={(e) => setOutfit(e.target.value)} />
          </div>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        {notice && <p className="ok" role="status">{notice}</p>}
        <button type="submit" disabled={busy}>{busy ? "上传中…" : "上传角色"}</button>
      </form>

      {chars.length === 0 && <p className="muted">角色库为空</p>}
      <div className="grid">
        {chars.map((c) => (
          <figure key={c.name} className="charcard" data-testid={`char-${c.name}`}>
            <img src={api.charFileUrl(c.sheet)} alt={`${c.name} sheet`} />
            {c.photo && <img src={api.charFileUrl(c.photo)} alt={`${c.name} photo`} />}
            <figcaption>
              <strong>{c.name}</strong>
              {c.desc && <div className="muted">{c.desc}</div>}
              {c.outfit && <div className="muted">服装：{c.outfit}</div>}
            </figcaption>
            <label htmlFor={`photo-${c.name}`}>补充 photo</label>
            <input id={`photo-${c.name}`} type="file" accept="image/*"
                   onChange={(e) => {
                     const file = e.target.files?.[0];
                     if (file) uploadPhoto(c.name, file);
                   }} />
            <button type="button" className="danger" onClick={() => remove(c.name)}>
              删除
            </button>
          </figure>
        ))}
      </div>
    </div>
  );
}