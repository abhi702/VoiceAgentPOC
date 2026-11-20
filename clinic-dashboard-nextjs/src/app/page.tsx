"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { PhoneAudio } from "@/lib/audio";

const ORCHESTRATOR = process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ?? "http://localhost:8000";
const CLINIC = process.env.NEXT_PUBLIC_NESTJS_API_URL ?? "http://localhost:3001";

interface Slot { slot_id: string; doctor_name: string; slot_date: string; slot_time: string; status: string }
interface Transcript { role: "user" | "assistant"; text: string; turn: number; interrupted?: boolean }
interface Metrics { ttfa_ms: number | null; first_text_ms: number | null; total_ms: number; turn_id: number }
type AgentEvent =
  | { type: "connected"; message: string }
  | { type: "user"; text: string; turn_id: number }
  | { type: "assistant_sentence"; text: string; turn_id: number }
  | { type: "interim"; text: string }
  | { type: "interrupt"; turn_id: number }
  | { type: "turn_start" | "turn_done"; turn_id: number }
  | { type: "red_flag"; category: string }
  | { type: "error" | "state"; message: string }
  | ({ type: "metrics" } & Metrics);

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  return response.json() as Promise<T>;
}

export default function Page() {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"text" | "voice">("text");
  const [status, setStatus] = useState("Ready to start");
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const [transcript, setTranscript] = useState<Transcript[]>([]);
  const [interim, setInterim] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [redFlag, setRedFlag] = useState("");
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [levels, setLevels] = useState<number[]>(Array(24).fill(0));
  const [telemetryStatus, setTelemetryStatus] = useState("Not connected");
  const [clinicEventsStatus, setClinicEventsStatus] = useState("Connecting");
  const socket = useRef<WebSocket | null>(null);
  const audio = useRef<PhoneAudio | null>(null);
  const telemetry = useRef<EventSource | null>(null);
  const sessionVersion = useRef(0);
  const connectionTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scroll = useRef<HTMLDivElement | null>(null);
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => json<{ status: string; speech_configured: boolean; model_configured: boolean }>(`${ORCHESTRATOR}/health`),
    refetchInterval: 15000,
  });
  const slots = useQuery({ queryKey: ["slots"], queryFn: () => json<Slot[]>(`${CLINIC}/bookings/available`), refetchInterval: 15000 });
  const bookings = useQuery({ queryKey: ["bookings"], queryFn: () => json<Slot[]>(`${CLINIC}/bookings`), refetchInterval: 15000 });

  const addError = useCallback((message: string) => setErrors((previous) => [...previous.slice(-4), message]), []);

  const stop = useCallback(() => {
    sessionVersion.current++;
    if (connectionTimer.current) clearTimeout(connectionTimer.current);
    socket.current?.close();
    socket.current = null;
    telemetry.current?.close();
    telemetry.current = null;
    const oldAudio = audio.current;
    audio.current = null;
    if (oldAudio) void oldAudio.close().catch((error: unknown) => addError(`Audio cleanup failed: ${String(error)}`));
    setConnected(false);
    setConnecting(false);
    setBusy(false);
    setLevels(Array(24).fill(0));
    setInterim("");
    setTelemetryStatus("Not connected");
    setStatus("Call ended");
  }, [addError]);

  useEffect(() => () => {
    socket.current?.close();
    telemetry.current?.close();
    if (audio.current) void audio.current.close().catch(console.error);
  }, []);
  useEffect(() => { scroll.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }); }, [transcript, interim]);
  useEffect(() => {
    const events = new EventSource(`${CLINIC}/events`);
    events.onopen = () => setClinicEventsStatus("Live");
    events.onmessage = () => {
      void queryClient.invalidateQueries({ queryKey: ["slots"] });
      void queryClient.invalidateQueries({ queryKey: ["bookings"] });
    };
    events.onerror = () => setClinicEventsStatus("Reconnecting; polling active");
    return () => events.close();
  }, [queryClient]);

  function handle(event: AgentEvent, ws: WebSocket, version: number): void {
    if (version !== sessionVersion.current) return;
    switch (event.type) {
      case "connected":
        if (connectionTimer.current) clearTimeout(connectionTimer.current);
        setConnected(true); setConnecting(false); setStatus(event.message); break;
      case "user":
        setInterim("");
        setTranscript((previous) => [...previous, { role: "user", text: event.text, turn: event.turn_id }]);
        break;
      case "assistant_sentence":
        setTranscript((previous) => {
          const last = previous[previous.length - 1];
          return last?.role === "assistant" && last.turn === event.turn_id && !last.interrupted
            ? [...previous.slice(0, -1), { ...last, text: `${last.text} ${event.text}` }]
            : [...previous, { role: "assistant", text: event.text, turn: event.turn_id }];
        });
        break;
      case "interim": setInterim(event.text); break;
      case "turn_start":
        setBusy(true); setStatus("Receptionist is responding"); break;
      case "turn_done":
        if (audio.current) {
          audio.current.afterPlayback(() => {
            if (version !== sessionVersion.current) return;
            if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "playback_done", turn_id: event.turn_id }));
            setBusy(false); setStatus("Listening");
          });
        } else { setBusy(false); setStatus("Ready for your next message"); }
        break;
      case "interrupt":
        audio.current?.interrupt(); setBusy(false);
        setTranscript((previous) => previous.map((item) => item.role === "assistant" && item.turn === event.turn_id ? { ...item, interrupted: true } : item));
        setStatus("Interrupted; listening"); break;
      case "red_flag": setRedFlag(event.category); break;
      case "error": addError(event.message); setBusy(false); break;
      case "state": setStatus(event.message); break;
      case "metrics": setMetrics(event); break;
    }
  }

  async function start(): Promise<void> {
    if (connecting || connected) return;
    const version = ++sessionVersion.current;
    setConnecting(true); setErrors([]); setTranscript([]); setMetrics(null); setRedFlag("");
    setStatus("Connecting to local services");
    try {
      if (mode === "voice") {
        if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone requires localhost or HTTPS");
        audio.current = new PhoneAudio();
        await audio.current.prepare();
      }
      const session = await json<{ call_id: string; token: string }>(`${ORCHESTRATOR}/api/sessions`, { method: "POST" });
      if (version !== sessionVersion.current) return;
      const params = new URLSearchParams({ ...session, voice: String(mode === "voice") });
      const url = new URL("/ws/voice", ORCHESTRATOR);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      url.search = params.toString();
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      socket.current = ws;
      connectionTimer.current = setTimeout(() => {
        if (version === sessionVersion.current) { addError("Call startup timed out. Inspect orchestrator logs."); stop(); }
      }, 60000);
      ws.onmessage = (message: MessageEvent<string | ArrayBuffer>) => {
        if (version !== sessionVersion.current) return;
        if (message.data instanceof ArrayBuffer) audio.current?.play(message.data);
        else {
          try { handle(JSON.parse(message.data) as AgentEvent, ws, version); }
          catch (error: unknown) { addError(`Invalid server event: ${String(error)}`); }
        }
      };
      ws.onopen = () => {
        const events = new EventSource(`${ORCHESTRATOR}/api/telemetry?${new URLSearchParams(session)}`);
        telemetry.current = events;
        events.onopen = () => setTelemetryStatus("Live SSE");
        events.onerror = () => setTelemetryStatus("SSE reconnecting; call events still on WebSocket");
        events.onmessage = (message: MessageEvent<string>) => {
          try {
            const event: { type: string } & Partial<Metrics> = JSON.parse(message.data);
            if (event.type === "metrics" && typeof event.total_ms === "number" && typeof event.turn_id === "number") {
              setMetrics({
                total_ms: event.total_ms, turn_id: event.turn_id,
                ttfa_ms: event.ttfa_ms ?? null, first_text_ms: event.first_text_ms ?? null,
              });
            }
          } catch (error: unknown) { addError(`Invalid telemetry event: ${String(error)}`); }
        };
        if (mode === "voice" && audio.current) {
          void audio.current.startMicrophone(
            (pcm) => {
              if (ws.readyState === WebSocket.OPEN && ws.bufferedAmount < 64000) ws.send(pcm);
              else if (ws.readyState === WebSocket.OPEN) {
                addError("Audio connection cannot keep up; ending call rather than dropping speech.");
                stop();
              }
            },
            setLevels,
          ).catch((error: unknown) => { addError(`Microphone failed: ${String(error)}`); stop(); });
        }
      };
      ws.onerror = () => addError("WebSocket connection failed; check service URLs and orchestrator logs.");
      ws.onclose = (event) => {
        if (version !== sessionVersion.current) return;
        if (event.code !== 1000) addError(`Call disconnected (${event.code}): ${event.reason || "see orchestrator logs"}`);
        stop();
      };
    } catch (error: unknown) {
      addError(`Cannot start call: ${String(error)}`);
      stop();
    }
  }

  function sendText(event: FormEvent): void {
    event.preventDefault();
    if (!text.trim() || socket.current?.readyState !== WebSocket.OPEN) return;
    audio.current?.interrupt();
    socket.current.send(JSON.stringify({ type: "text", text: text.trim() }));
    setText("");
  }

  const serviceError = health.error ?? slots.error ?? bookings.error;
  return (
    <main className="mx-auto max-w-7xl px-4 py-8 md:px-8">
      <header className="mb-7 flex flex-wrap items-start justify-between gap-4">
        <div><p className="mb-2 text-xs uppercase tracking-[.25em] text-teal-300">Local exploration / synthetic data</p>
          <h1 className="text-3xl font-semibold">Voice Clinic</h1>
          <p className="muted mt-2">A receptionist demonstration. Not an NHS service or a clinical decision system.</p></div>
        <span className="badge">{health.data?.status === "ok" ? "Local services ready" : "Services not ready"}</span>
      </header>
      <div className="mb-5 rounded-xl border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-100">
        Use fictional patient details only. Voice and conversation are sent to your configured cloud providers.
        In a real emergency, call 999; do not use this demo.
      </div>
      {redFlag && <div role="alert" className="mb-5 rounded-xl border border-red-500 bg-red-950 p-5">
        <strong>Emergency override: {redFlag}</strong><p>Routine transactions are disabled for this call. Call 999 for a real emergency.</p>
      </div>}
      {(errors.length > 0 || serviceError) && <div role="alert" className="mb-5 rounded-xl border border-rose-700 bg-rose-950/50 p-4 text-sm">
        {serviceError && <p>Local API unavailable: {serviceError.message}</p>}
        {errors.map((error, i) => <p key={i}>{error}</p>)}
        <button className="mt-2 underline" onClick={() => setErrors([])}>Dismiss call errors</button>
      </div>}
      <div className="grid gap-6 lg:grid-cols-[.9fr_1.1fr]">
        <section className="panel p-6" aria-labelledby="phone-title">
          <div className="flex items-center justify-between gap-3"><h2 id="phone-title" className="text-lg font-semibold">Patient web phone</h2><span className="badge">{connected ? "Connected" : connecting ? "Connecting" : "Offline"}</span></div>
          <label className="muted mt-6 block text-sm" htmlFor="mode">Interaction mode</label>
          <select id="mode" value={mode} onChange={(event) => setMode(event.target.value as "text" | "voice")} disabled={connected || connecting} className="mt-2 w-full rounded-lg border border-slate-600 bg-slate-900 p-3">
            <option value="text">Text (no Speech key required)</option><option value="voice">Microphone + Azure Speech</option>
          </select>
          <div className="meter my-5" aria-label={connected && mode === "voice" ? "Microphone level" : "Microphone inactive"}>
            {levels.map((level, index) => <span key={index} style={{ height: `${Math.max(4, level * 65)}px` }} />)}
          </div>
          <p role="status" className="muted mb-5 min-h-12 text-center text-sm">{status}</p>
          <div className="flex flex-wrap justify-center gap-3">
            <button className="button" disabled={connected || connecting} onClick={() => void start()}>Start {mode === "voice" ? "voice call" : "text session"}</button>
            <button className="button danger" disabled={!connected && !connecting} onClick={stop}>End call</button>
            <button className="button secondary" disabled={!connected || !busy || !!redFlag} onClick={() => socket.current?.send(JSON.stringify({ type: "interrupt" }))}>Interrupt</button>
          </div>
          <form onSubmit={sendText} className="mt-7">
            <label htmlFor="message" className="muted text-sm">Type a message (also available during voice calls)</label>
            <div className="mt-2 flex gap-2"><input id="message" maxLength={2000} value={text} onChange={(event) => setText(event.target.value)} disabled={!connected} autoComplete="off" placeholder="Check the next available appointments" className="min-w-0 flex-1 rounded-lg border border-slate-600 bg-slate-900 p-3" /><button className="button" disabled={!connected || !text.trim()}>Send</button></div>
          </form>
          <div className="mt-7 rounded-xl bg-slate-900 p-4 text-sm"><h3 className="font-semibold">Try the synthetic patient</h3>
            <p className="muted mt-2">Alex Demo · DOB 1990-01-15 · Metformin</p>
            <p className="muted mt-2">Ask for a slot, supply these details, choose a time, then confirm the repeated details with “yes”. Holds expire after 30 seconds.</p>
          </div>
        </section>
        <section className="panel p-6" aria-labelledby="dashboard-title">
          <div className="flex items-center justify-between gap-3"><h2 id="dashboard-title" className="text-lg font-semibold">Receptionist dashboard</h2><span className="badge">{telemetryStatus}</span></div>
          <div className="my-5 grid grid-cols-3 gap-3">
            {[["First audio", metrics?.ttfa_ms], ["First sentence", metrics?.first_text_ms], ["Turn total", metrics?.total_ms]].map(([label, value]) =>
              <div key={String(label)} className="rounded-xl bg-slate-900 p-3"><p className="muted text-xs">{label}</p><p className={`mt-1 text-lg font-semibold ${label === "First audio" && typeof value === "number" && value >= 800 ? "text-amber-300" : "text-teal-200"}`}>{typeof value === "number" ? `${value} ms` : "--"}</p></div>)}
          </div>
          <p className="muted mb-3 text-xs">Server timings from final recognised text; first audio excludes browser/network playback. 800 ms is a target, not a guarantee.</p>
          <div className="h-80 space-y-3 overflow-auto rounded-xl border border-slate-700 p-3" aria-label="Live transcript" aria-live="polite">
            {!transcript.length && <p className="muted p-4 text-sm">Start a session to see the live conversation.</p>}
            {transcript.map((item, index) => <div key={index} className={`message ${item.role}`}>
              <p className="muted mb-1 text-xs">{item.role === "user" ? "Caller" : "Receptionist"}{item.interrupted ? " · interrupted" : ""}</p>{item.text}
            </div>)}
            {interim && <p className="muted px-2 text-sm italic">Hearing: {interim}</p>}<div ref={scroll} />
          </div>
          <h3 className="mb-2 mt-6 font-semibold">Available appointments</h3>
          <div className="overflow-x-auto"><table><thead><tr><th>Date / time</th><th>Doctor</th><th>Slot</th></tr></thead><tbody>
            {slots.data?.map((slot) => <tr key={slot.slot_id}><td>{slot.slot_date}<br />{slot.slot_time.slice(0, 5)}</td><td>{slot.doctor_name}</td><td>{slot.slot_id}</td></tr>)}
          </tbody></table>{slots.data?.length === 0 && <p className="muted p-3 text-sm">No available demo slots.</p>}</div>
          <div className="mt-6 flex items-center justify-between"><h3 className="font-semibold">Confirmed bookings</h3><span className="muted text-xs">{clinicEventsStatus}</span></div>
          <div className="mt-2 overflow-x-auto"><table><thead><tr><th>Date / time</th><th>Doctor</th><th>Status</th></tr></thead><tbody>
            {bookings.data?.map((slot) => <tr key={slot.slot_id}><td>{slot.slot_date} {slot.slot_time.slice(0, 5)}</td><td>{slot.doctor_name}</td><td className="text-teal-300">{slot.status}</td></tr>)}
          </tbody></table>{bookings.data?.length === 0 && <p className="muted p-3 text-sm">No bookings yet. Only committed database bookings appear here.</p>}</div>
        </section>
      </div>
    </main>
  );
}
