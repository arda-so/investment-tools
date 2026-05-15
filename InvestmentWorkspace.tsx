import React, { useMemo, useState } from "react";

type Stage = "Inbox" | "Deep Dive" | "Watchlist" | "Portfolio";
type ActionType = "Buy" | "Sell" | "Hold" | "Note";
type Emotion = "Neutral" | "Excited" | "Fearful";

type JournalEntry = {
  date: string;
  action: ActionType;
  emotion: Emotion;
  note: string;
};

type Idea = {
  ticker: string;
  stage: Stage;
  conviction: number;
  thesis: string;
  journal: JournalEntry[];
};

const STAGES: Stage[] = ["Inbox", "Deep Dive", "Watchlist", "Portfolio"];

const STAGE_STYLES: Record<Stage, string> = {
  Inbox: "border-neutral-600 bg-neutral-800 text-neutral-300",
  "Deep Dive": "border-blue-500 bg-blue-950/40 text-blue-200",
  Watchlist: "border-amber-500 bg-amber-950/30 text-amber-200",
  Portfolio: "border-emerald-500 bg-emerald-950/30 text-emerald-200",
};

const MOCK_PRICES: Record<string, number> = {
  AAPL: 221.31,
  MSFT: 419.74,
  NVDA: 137.82,
  MOH: 327.18,
};

const MOCK_MOAT: Record<string, number> = {
  AAPL: 8,
  MSFT: 9,
  NVDA: 7,
  MOH: 6,
};

const INITIAL_IDEAS: Idea[] = [
  {
    ticker: "AAPL",
    stage: "Portfolio",
    conviction: 8,
    thesis:
      "Premium ecosystem + durable pricing power.\n\nFocus: services mix shift and capital return discipline.",
    journal: [
      {
        date: "2026-02-10",
        action: "Buy",
        emotion: "Neutral",
        note: "Added on valuation reset. Services trend still intact.",
      },
    ],
  },
  {
    ticker: "MSFT",
    stage: "Deep Dive",
    conviction: 9,
    thesis:
      "Cloud + productivity moat remains dominant.\n\nNeed to validate AI monetization quality vs cost curve.",
    journal: [
      {
        date: "2026-02-12",
        action: "Note",
        emotion: "Excited",
        note: "Track Azure margin path and Copilot attach rates.",
      },
    ],
  },
  {
    ticker: "NVDA",
    stage: "Watchlist",
    conviction: 7,
    thesis: "Best-in-class AI compute platform, but valuation risk remains high.",
    journal: [],
  },
  {
    ticker: "MOH",
    stage: "Inbox",
    conviction: 6,
    thesis: "Healthcare managed-care screen candidate. Need policy sensitivity map.",
    journal: [],
  },
];

function fmtDateTime(d: Date): string {
  return d.toISOString().slice(0, 16).replace("T", " ");
}

export default function InvestmentWorkspace() {
  const [ideas, setIdeas] = useState<Idea[]>(INITIAL_IDEAS);
  const [activeTicker, setActiveTicker] = useState<string>(INITIAL_IDEAS[0].ticker);
  const [dragTicker, setDragTicker] = useState<string | null>(null);
  const [dragOverStage, setDragOverStage] = useState<Stage | null>(null);
  const [openStages, setOpenStages] = useState<Record<Stage, boolean>>({
    Inbox: true,
    "Deep Dive": true,
    Watchlist: true,
    Portfolio: true,
  });

  const [action, setAction] = useState<ActionType>("Buy");
  const [emotion, setEmotion] = useState<Emotion>("Neutral");
  const [journalNote, setJournalNote] = useState("");

  const activeIdea = useMemo(
    () => ideas.find((i) => i.ticker === activeTicker) ?? ideas[0],
    [ideas, activeTicker]
  );

  const grouped = useMemo(() => {
    const byStage: Record<Stage, Idea[]> = {
      Inbox: [],
      "Deep Dive": [],
      Watchlist: [],
      Portfolio: [],
    };
    ideas.forEach((i) => byStage[i.stage].push(i));
    return byStage;
  }, [ideas]);

  const activePrice = MOCK_PRICES[activeIdea.ticker] ?? 0;
  const activeMoat = MOCK_MOAT[activeIdea.ticker] ?? 5;

  function toggleStage(stage: Stage) {
    setOpenStages((prev) => ({ ...prev, [stage]: !prev[stage] }));
  }

  function updateThesis(next: string) {
    setIdeas((prev) =>
      prev.map((i) => (i.ticker === activeIdea.ticker ? { ...i, thesis: next } : i))
    );
  }

  function submitJournal() {
    const note = journalNote.trim();
    if (!note) return;
    const entry: JournalEntry = {
      date: fmtDateTime(new Date()),
      action,
      emotion,
      note,
    };
    setIdeas((prev) =>
      prev.map((i) =>
        i.ticker === activeIdea.ticker ? { ...i, journal: [entry, ...i.journal] } : i
      )
    );
    setJournalNote("");
  }

  function moveIdeaToStage(ticker: string, stage: Stage) {
    setIdeas((prev) =>
      prev.map((i) => (i.ticker === ticker ? { ...i, stage } : i))
    );
  }

  return (
    <div className="h-screen w-full bg-neutral-900 text-neutral-200">
      <div className="grid h-full grid-cols-12 gap-3 p-3">
        <aside className="col-span-3 overflow-hidden rounded-xl border border-neutral-700 bg-neutral-850 p-3">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-neutral-400">
            Pipeline
          </h2>
          <div className="space-y-2 overflow-y-auto pr-1">
            {STAGES.map((stage) => (
              <section
                key={stage}
                className={`rounded-lg border transition ${
                  dragOverStage === stage ? "border-blue-400 bg-blue-950/20" : "border-neutral-700"
                }`}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragOverStage(stage);
                }}
                onDragLeave={() => {
                  setDragOverStage((cur) => (cur === stage ? null : cur));
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  const dropped = e.dataTransfer.getData("text/plain") || dragTicker || "";
                  if (dropped) moveIdeaToStage(dropped, stage);
                  setDragTicker(null);
                  setDragOverStage(null);
                }}
              >
                <button
                  className="flex w-full items-center justify-between px-3 py-2 text-left text-sm"
                  onClick={() => toggleStage(stage)}
                >
                  <span>{stage}</span>
                  <span className="text-neutral-500">{openStages[stage] ? "−" : "+"}</span>
                </button>
                {openStages[stage] && (
                  <div className="space-y-2 border-t border-neutral-700 px-2 py-2">
                    {grouped[stage].map((idea) => (
                      <button
                        key={idea.ticker}
                        draggable
                        onClick={() => setActiveTicker(idea.ticker)}
                        onDragStart={(e) => {
                          setDragTicker(idea.ticker);
                          e.dataTransfer.setData("text/plain", idea.ticker);
                          e.dataTransfer.effectAllowed = "move";
                        }}
                        onDragEnd={() => {
                          setDragTicker(null);
                          setDragOverStage(null);
                        }}
                        className={`w-full rounded-md border px-2 py-2 text-left transition ${
                          STAGE_STYLES[stage]
                        } ${
                          activeIdea.ticker === idea.ticker ? "ring-1 ring-white/40" : ""
                        } ${dragTicker === idea.ticker ? "opacity-60" : ""}`}
                      >
                        <div className="flex items-center justify-between">
                          <span className="font-semibold">{idea.ticker}</span>
                          <span className="text-xs">Conv {idea.conviction}</span>
                        </div>
                      </button>
                    ))}
                    {grouped[stage].length === 0 && (
                      <div className="px-1 py-1 text-xs text-neutral-500">No ideas</div>
                    )}
                  </div>
                )}
              </section>
            ))}
          </div>
        </aside>

        <main className="col-span-6 overflow-hidden rounded-xl border border-neutral-700 bg-neutral-850 p-4">
          <header className="mb-4 border-b border-neutral-700 pb-3">
            <div className="flex items-end justify-between">
              <div>
                <h1 className="text-4xl font-bold tracking-tight">{activeIdea.ticker}</h1>
                <p className="mt-1 text-sm text-neutral-400">
                  ${activePrice.toFixed(2)} · Moat Score {activeMoat}/10
                </p>
              </div>
              <div
                className={`rounded-md border px-2 py-1 text-xs ${
                  STAGE_STYLES[activeIdea.stage]
                }`}
              >
                {activeIdea.stage}
              </div>
            </div>
          </header>

          <div className="h-[calc(100%-84px)]">
            <textarea
              value={activeIdea.thesis}
              onChange={(e) => updateThesis(e.target.value)}
              className="h-full w-full resize-none rounded-lg border border-neutral-700 bg-neutral-900 p-4 font-serif text-[15px] leading-relaxed outline-none ring-blue-500/40 placeholder:text-neutral-500 focus:ring"
              placeholder="Write investment thesis..."
            />
          </div>
        </main>

        <aside className="col-span-3 overflow-hidden rounded-xl border border-neutral-700 bg-neutral-850 p-3">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-neutral-400">
            Decision Journal
          </h2>

          <div className="mb-3 space-y-2 rounded-lg border border-neutral-700 p-3">
            <label className="block text-xs text-neutral-400">Log a Decision</label>
            <select
              value={action}
              onChange={(e) => setAction(e.target.value as ActionType)}
              className="w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-2 text-sm"
            >
              <option>Buy</option>
              <option>Sell</option>
              <option>Hold</option>
              <option>Note</option>
            </select>

            <div className="flex gap-2">
              {(["Neutral", "Excited", "Fearful"] as Emotion[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setEmotion(m)}
                  className={`rounded px-2 py-1 text-xs ${
                    emotion === m
                      ? "bg-blue-600 text-white"
                      : "bg-neutral-800 text-neutral-300 hover:bg-neutral-700"
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>

            <textarea
              value={journalNote}
              onChange={(e) => setJournalNote(e.target.value)}
              placeholder="Why?"
              className="h-20 w-full resize-none rounded border border-neutral-700 bg-neutral-900 px-2 py-2 text-sm"
            />

            <button
              onClick={submitJournal}
              className="w-full rounded bg-emerald-600 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-500"
            >
              Save Entry
            </button>
          </div>

          <div className="h-[calc(100%-220px)] overflow-y-auto pr-1">
            <div className="space-y-2">
              {activeIdea.journal.map((j, idx) => (
                <div key={`${j.date}-${idx}`} className="rounded-md border border-neutral-700 bg-neutral-900 p-2">
                  <div className="mb-1 flex items-center justify-between text-xs text-neutral-400">
                    <span>{j.date}</span>
                    <span>
                      {j.action} · {j.emotion}
                    </span>
                  </div>
                  <p className="text-sm text-neutral-200">{j.note}</p>
                </div>
              ))}
              {activeIdea.journal.length === 0 && (
                <div className="rounded-md border border-neutral-700 bg-neutral-900 p-2 text-sm text-neutral-500">
                  No journal entries yet.
                </div>
              )}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
