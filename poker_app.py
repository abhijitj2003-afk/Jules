
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import pyperclip


BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# ---------- palette ----------
BG = "#0b1220"
PANEL = "#111a2b"
PANEL_2 = "#162238"
BORDER = "#263552"
TEXT = "#edf3fb"
MUTED = "#94a3b8"
ACCENT = "#60a5fa"
GREEN = "#34d399"
RED = "#fb7185"
AMBER = "#fbbf24"
WHITE = "#ffffff"


def session_stamp(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")


def safe_label(label: str) -> str:
    label = re.sub(r"[^A-Za-z0-9 _.-]+", "_", label.strip())
    label = re.sub(r"\s+", "_", label)
    return label[:60].strip("_") or "session"


def create_session(label: str = "") -> Path:
    stamp = session_stamp()
    suffix = f"_{safe_label(label)}" if label.strip() else ""
    folder = SESSIONS_DIR / f"{stamp}{suffix}"
    counter = 1
    while folder.exists():
        folder = SESSIONS_DIR / f"{stamp}{suffix}_{counter}"
        counter += 1

    folder.mkdir(parents=True)
    (folder / "hands.txt").write_text("", encoding="utf-8")
    metadata = {
        "session_id": folder.name,
        "label": label.strip(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "ended_at": None,
        "hands_captured": 0,
        "status": "active",
    }
    (folder / "session.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return folder


def update_metadata(folder: Path, **changes) -> None:
    path = folder / "session.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {"session_id": folder.name}
    data.update(changes)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_metadata(folder: Path) -> dict:
    try:
        return json.loads((folder / "session.json").read_text(encoding="utf-8"))
    except Exception:
        return {"session_id": folder.name, "label": ""}


def session_folders() -> list[Path]:
    return sorted(
        [p for p in SESSIONS_DIR.iterdir() if p.is_dir() and (p / "hands.txt").exists()],
        key=lambda p: p.name,
        reverse=True,
    )


def load_hands(folder: Path) -> list[str]:
    path = folder / "hands.txt"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"(?=^Hand ID:)", text, flags=re.MULTILINE)
    return [p.strip() for p in parts if p.strip() and "Hand ID:" in p]


def extract_hand_id(hand: str) -> str:
    m = re.search(r"Hand ID:\s*([A-Za-z0-9_]+)", hand)
    return m.group(1) if m else "unknown"


def append_hand(folder: Path, hand: str) -> int:
    with (folder / "hands.txt").open("a", encoding="utf-8") as f:
        f.write(hand.rstrip() + "\n\n")
    count = len(load_hands(folder))
    update_metadata(folder, hands_captured=count)
    return count


def classify_action(action: str) -> str:
    return {
        "folds": "Fold",
        "calls": "Call",
        "raises": "Raise / 3-Bet",
        "checks": "Check",
        "bets": "Bet",
        "allin": "All-in",
        "none": "No action",
    }.get(action, action.title() if action else "No action")


def street_display(audit) -> tuple[str, str, str]:
    action = classify_action(audit.hero_action)
    if audit.hero_amount:
        action += f" {audit.hero_amount}"
    observed = action
    expected = classify_action(audit.expected) if audit.expected else "No specific action"
    return observed, expected, audit.hand_class


def explain_breach(message: str, audit=None) -> dict[str, str]:
    """
    Convert a technical engine violation into a human explanation.
    The wording stays faithful to the PDF's decision rules and terminology.
    """
    low = message.lower()

    if "iso-raise" in low or "limper" in low:
        return {
            "title": "You used the wrong pre-flop response to limpers.",
            "what": "Your action did not follow the PDF's isolation / limper rules for this hand.",
            "rule": "When the table has limpers, the strategy changes. An authorized hand should use an isolation raise sized at 3× the big blind plus 1× per limper. The Stress Test also removes weak offsuit aces, marginal offsuit broadways, and small pocket pairs from the raise group.",
            "fix": "First count the limpers, then apply the Stress Test before deciding whether to isolate, overcall, or fold.",
        }

    if "set-min" in low or "stack too shallow" in low:
        return {
            "title": "You called without enough implied-odds protection.",
            "what": "The call was made with too little effective stack behind.",
            "rule": "The PDF requires the effective stack to be large enough relative to the call. Small/medium pairs use the 10×–12× guideline, suited broadways/connectors use 15×, and suited one-gappers use the stricter Rule of 20.",
            "fix": "Use the smaller stack between you and the relevant opponent. Compare it with the amount you must call before putting chips in.",
        }

    if "bottom two pair" in low:
        return {
            "title": "Your Bottom Two Pair was overvalued against the re-raise.",
            "what": "The strategy treats Bottom Two Pair as vulnerable when an opponent comes back over your value raise.",
            "rule": "On a massive re-raise, Bottom Two Pair is a mandatory fold because sets and Top Two Pair dominate it, and higher board cards can counterfeit it.",
            "fix": "Separate Top Two Pair from Bottom Two Pair. They are not interchangeable in this strategy.",
        }

    if "dry board" in low and "fold" in low:
        return {
            "title": "You continued too strongly on a dry-board re-raise.",
            "what": "Your Two Pair faced a re-raise on a board with little obvious draw coverage.",
            "rule": "The PDF says a Two Pair value raise followed by a re-raise on a dry board should fold because the opponent's range becomes heavily weighted toward sets / stronger made hands.",
            "fix": "When the board is dry, take the re-raise much more seriously than on a draw-heavy wet board.",
        }

    if "wet board" in low and "call" in low:
        return {
            "title": "You folded a Two Pair that the strategy allows you to call.",
            "what": "Your Two Pair faced a re-raise on a wet board.",
            "rule": "On a wet board, the PDF expands the opponent's re-raising range to include high-equity combo draws. It therefore authorizes calling with sufficiently strong Two Pair.",
            "fix": "Distinguish dry from wet boards before reacting to the re-raise.",
        }

    if "multi-way" in low or "bluff" in low:
        return {
            "title": "You showed too much aggression in a multi-way pot.",
            "what": "The strategy sharply reduces bluffing and marginal aggression when three or more players remain.",
            "rule": "In multi-way pots, bluffing authorization is zero. Single Pair is automatically downgraded to Showdown Value, and strong draws are played mainly according to the allowed price rather than by driving the action.",
            "fix": "When the pot is crowded, tighten the aggression. Check more, control the pot with one pair, and reserve major commitment for Two Pair, a Set, or better.",
        }

    if "river" in low and "air" in low:
        return {
            "title": "You paid to see a river with a hand the strategy considers dead.",
            "what": "The draw did not complete, so there is no implied-odds value left.",
            "rule": "On the river, missed draws / high cards are Air. The PDF says Air must fold when facing a bet.",
            "fix": "Once the final card is dealt, stop treating a missed draw as a drawing hand. There is nothing left to draw to.",
        }

    if "triple barrel" in low:
        return {
            "title": "You paid off a river triple barrel with too little showdown strength.",
            "what": "The opponent bet the flop, turn, and river, creating a consistent story of strength.",
            "rule": "The PDF specifically says a single pair almost never beats a player firing all three streets and should fold to that river pressure.",
            "fix": "On the river, consider the whole betting story, not just your pair.",
        }

    if "sudden wake" in low:
        return {
            "title": "The river bet was treated as stronger than the strategy says.",
            "what": "The opponent checked flop and turn, then suddenly bet the river.",
            "rule": "The PDF identifies this pattern as a classic missed-draw bluffing opportunity and authorizes a call with Showdown Value, assuming the board did not create an obvious made hand.",
            "fix": "Use the opponent's betting history on the river, not only the final bet size.",
        }

    if "scary-card" in low or "scary card" in low or "downgrade" in low:
        return {
            "title": "The river changed the value of your hand.",
            "what": "A completed flush / four-card straight texture means the hand is no longer as strong as it was earlier.",
            "rule": "The PDF explicitly downgrades Two Pair or Three of a Kind to Showdown Value when a scary river card completes a major draw.",
            "fix": "Re-evaluate the hand after the river. Do not blindly carry the earlier street's hand category forward.",
        }

    if "top pair" in low and "fold" in low:
        return {
            "title": "Your single pair reached a point where the strategy says to let it go.",
            "what": "The hand had Showdown Value, but the price or betting line was too strong.",
            "rule": "The PDF treats Top / Middle Pair as a bluff-catcher and gives strict pot-size limits, especially on wet boards and against heavy river bets.",
            "fix": "Compare the opponent's bet with the current pot and check the betting story before calling.",
        }

    return {
        "title": "This action did not match the strategy rule.",
        "what": message,
        "rule": "The auditor flagged a direct mismatch with one of the rules in the supplied Poker Strategy PDF.",
        "fix": "Use the street-by-street details below to see the situation, required action, and applicable threshold.",
    }


def plain_preflop_explanation(result: dict) -> str:
    pre = result["preflop"]
    observed = classify_action(pre.get("observed", ""))
    expected = classify_action(pre.get("expected", ""))
    reason = pre.get("reason", "")
    if pre.get("breaches"):
        return (
            f"You had {result.get('hole', 'unknown')} in {result.get('position', 'unknown')} with "
            f"{result.get('stack', 0)} chips. You chose **{observed}**, but the strategy required "
            f"**{expected}**.\n\n"
            f"Why: {reason}.\n\n"
            "This is the pre-flop decision that caused the breach. Because the strategy is "
            "position- and context-dependent, the key is the situation first, then the hand."
        )
    return (
        f"Pre-flop: **{observed}** was correct. The strategy expected **{expected}**.\n\n"
        f"Reason: {reason}."
    )


class PokerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Poker Strategy Auditor")
        self.geometry("1280x820")
        self.minsize(1080, 700)
        self.configure(bg=BG)

        self.engine = None
        self.active_session: Path | None = None
        self.logger_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.ui_queue: queue.Queue = queue.Queue()
        self.last_clipboard = ""

        self.player_var = tk.StringVar(value="AbhijitJain")
        self.style_var = tk.StringVar(value="unknown")
        self.session_name_var = tk.StringVar()
        self.status_var = tk.StringVar(value="No active session")
        self.hand_count_var = tk.StringVar(value="0")
        self.selected_session_var = tk.StringVar()
        self.auto_audit_var = tk.BooleanVar(value=False)
        self.score_var = tk.StringVar(value="No audit yet")
        self.session_hint_var = tk.StringVar(value="")

        self._load_engine()
        self._configure_styles()
        self._build_ui()
        self._refresh_sessions()
        self.after(100, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_engine(self):
        import importlib.util
        path = BASE_DIR / "analyser.py"
        spec = importlib.util.spec_from_file_location("poker_analyser_engine", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load analyser.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.engine = module

    def _configure_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("Card.TFrame", background=PANEL_2)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Muted.TLabel", background=BG, foreground=MUTED)
        s.configure("Title.TLabel", background=BG, foreground=WHITE, font=("Segoe UI Semibold", 22))
        s.configure("H1.TLabel", background=BG, foreground=WHITE, font=("Segoe UI Semibold", 15))
        s.configure("CardTitle.TLabel", background=PANEL_2, foreground=TEXT, font=("Segoe UI Semibold", 11))
        s.configure("Metric.TLabel", background=PANEL_2, foreground=WHITE, font=("Segoe UI Semibold", 20))
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(18, 9), font=("Segoe UI Semibold", 10))
        s.map("TNotebook.Tab", background=[("selected", PANEL_2)], foreground=[("selected", WHITE)])
        s.configure("TButton", background=PANEL_2, foreground=TEXT, borderwidth=0, padding=(14, 9), font=("Segoe UI Semibold", 10))
        s.map("TButton", background=[("active", BORDER), ("pressed", BORDER)])
        s.configure("Accent.TButton", background=ACCENT, foreground="#07101f", padding=(15, 10), font=("Segoe UI Semibold", 10))
        s.map("Accent.TButton", background=[("active", "#93c5fd"), ("pressed", "#3b82f6")])
        s.configure("Danger.TButton", background=RED, foreground="#1b0a0f", padding=(14, 9), font=("Segoe UI Semibold", 10))
        s.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER)
        s.configure("TCombobox", fieldbackground=PANEL_2, foreground=TEXT, selectbackground=BORDER, selectforeground=TEXT)
        s.configure("TCheckbutton", background=BG, foreground=TEXT)
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, borderwidth=0, rowheight=34, font=("Segoe UI", 10))
        s.configure("Treeview.Heading", background=PANEL_2, foreground=MUTED, font=("Segoe UI Semibold", 9))
        s.map("Treeview", background=[("selected", "#1e3a5f")], foreground=[("selected", WHITE)])

    def _build_ui(self):
        root = ttk.Frame(self, padding=(18, 16))
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="Poker Strategy Auditor", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="PDF-based decisions, session tracking, and hand-by-hand explanations",
            style="Muted.TLabel",
        ).pack(side="left", padx=(16, 0), pady=(8, 0))

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)

        self.live_tab = ttk.Frame(notebook)
        self.audit_tab = ttk.Frame(notebook)
        self.helper_tab = ttk.Frame(notebook)
        notebook.add(self.live_tab, text="  Live Session  ")
        notebook.add(self.audit_tab, text="  Hand Review  ")
        notebook.add(self.helper_tab, text="  Decision Helper  ")

        self._build_live_tab()
        self._build_review_tab()
        self._build_helper_tab()

    def _card(self, parent, title=None):
        frame = tk.Frame(parent, bg=PANEL_2, highlightbackground=BORDER, highlightthickness=1, bd=0)
        if title:
            tk.Label(frame, text=title, bg=PANEL_2, fg=TEXT, font=("Segoe UI Semibold", 11)).pack(
                anchor="w", padx=16, pady=(12, 8)
            )
        return frame

    def _build_live_tab(self):
        top = ttk.Frame(self.live_tab, padding=(0, 12))
        top.pack(fill="x")

        left = ttk.Frame(top)
        left.pack(side="left")
        ttk.Label(left, text="Session name", style="Muted.TLabel").pack(anchor="w")
        ttk.Entry(left, textvariable=self.session_name_var, width=28).pack(anchor="w", pady=(5, 0))

        ttk.Button(top, text="Start New Session", style="Accent.TButton", command=self.start_session).pack(
            side="left", padx=(16, 8), pady=(17, 0)
        )
        ttk.Button(top, text="Stop Logger", style="Danger.TButton", command=self.stop_logger).pack(
            side="left", padx=8, pady=(17, 0)
        )
        ttk.Button(top, text="Open Sessions Folder", command=lambda: self._open_path(SESSIONS_DIR)).pack(
            side="left", padx=8, pady=(17, 0)
        )

        stats = ttk.Frame(self.live_tab)
        stats.pack(fill="x", pady=(0, 14))
        for title, var in [
            ("STATUS", self.status_var),
            ("HANDS CAPTURED", self.hand_count_var),
            ("AUDIT", self.score_var),
        ]:
            card = self._card(stats)
            card.pack(side="left", fill="x", expand=True, padx=(0, 10))
            tk.Label(card, text=title, bg=PANEL_2, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=16, pady=(12, 2))
            tk.Label(card, textvariable=var, bg=PANEL_2, fg=WHITE, font=("Segoe UI Semibold", 16), wraplength=330).pack(anchor="w", padx=16, pady=(0, 14))

        controls = self._card(self.live_tab, "Audit settings")
        controls.pack(fill="x", pady=(0, 14))
        row = ttk.Frame(controls)
        row.pack(fill="x", padx=16, pady=(0, 14))
        ttk.Label(row, text="Hero:").pack(side="left")
        ttk.Entry(row, textvariable=self.player_var, width=22).pack(side="left", padx=(6, 20))
        ttk.Label(row, text="Opponent style:").pack(side="left")
        ttk.Combobox(
            row,
            textvariable=self.style_var,
            values=["unknown", "tight", "loose", "aggressive", "loose/aggressive"],
            state="readonly",
            width=18,
        ).pack(side="left", padx=(6, 20))
        ttk.Button(row, text="Audit Current Session", style="Accent.TButton", command=self.audit_current).pack(side="left")
        ttk.Checkbutton(row, text="Audit automatically after each hand", variable=self.auto_audit_var).pack(side="left", padx=18)

        activity = self._card(self.live_tab, "Live activity")
        activity.pack(fill="both", expand=True)
        self.live_text = tk.Text(
            activity, bg=PANEL, fg=MUTED, insertbackground=TEXT, relief="flat",
            font=("Segoe UI", 10), wrap="word", padx=14, pady=12
        )
        self.live_text.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        self._log_ui("Ready. Start a session and copy poker hands normally.")

    def _build_review_tab(self):
        # Top toolbar: session + the individual-hand audit action are deliberately
        # visible without requiring the user to scroll or hunt through the UI.
        toolbar = tk.Frame(self.audit_tab, bg=BG)
        toolbar.pack(fill="x", pady=(12, 10))

        tk.Label(
            toolbar, text="Review a Session", bg=BG, fg=WHITE,
            font=("Segoe UI Semibold", 19)
        ).pack(side="left")

        self.session_combo = ttk.Combobox(
            toolbar,
            textvariable=self.selected_session_var,
            state="readonly",
            width=52,
        )
        self.session_combo.pack(side="left", padx=(18, 8))
        self.session_combo.bind("<<ComboboxSelected>>", lambda _: self._on_session_change())

        ttk.Button(toolbar, text="Refresh", command=self._refresh_sessions).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Run Session Audit", style="Accent.TButton", command=self.audit_selected).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Open Folder", command=self.open_selected_folder).pack(side="left", padx=4)

        # Selected-hand strip.
        hand_strip = tk.Frame(
            self.audit_tab, bg=PANEL_2, highlightbackground=BORDER, highlightthickness=1
        )
        hand_strip.pack(fill="x", pady=(0, 12))

        tk.Label(
            hand_strip, text="INDIVIDUAL HAND", bg=PANEL_2, fg=MUTED,
            font=("Segoe UI Semibold", 9)
        ).pack(side="left", padx=(16, 10), pady=14)

        self.hand_combo = ttk.Combobox(
            hand_strip, state="readonly", width=46
        )
        self.hand_combo.pack(side="left", padx=(0, 10), pady=10)
        self.hand_combo.bind("<<ComboboxSelected>>", lambda _: self._select_tree_hand())

        # Large, obvious action button. This was previously buried below the
        # main pane, which is exactly where an important action should never be.
        ttk.Button(
            hand_strip,
            text="AUDIT THIS HAND",
            style="Accent.TButton",
            command=self.audit_selected_hand
        ).pack(side="left", padx=5, pady=9)

        self.hand_status_label = tk.Label(
            hand_strip, text="Select a hand",
            bg=PANEL_2, fg=MUTED, font=("Segoe UI", 10)
        )
        self.hand_status_label.pack(side="left", padx=12)

        body = tk.PanedWindow(
            self.audit_tab, orient="horizontal",
            bg=BG, sashwidth=8, bd=0, relief="flat"
        )
        body.pack(fill="both", expand=True, pady=(0, 10))

        left = self._card(self.audit_tab, "Hands")
        right = tk.Frame(self.audit_tab, bg=BG)

        body.add(left, minsize=300)
        body.add(right, minsize=650)

        self.hand_tree = ttk.Treeview(
            left,
            columns=("hand", "result"),
            show="headings",
            selectmode="browse"
        )
        self.hand_tree.heading("hand", text="Hand")
        self.hand_tree.heading("result", text="Result")
        self.hand_tree.column("hand", width=235, anchor="w")
        self.hand_tree.column("result", width=110, anchor="center")
        self.hand_tree.pack(side="left", fill="both", expand=True, padx=12, pady=(0, 12))
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.hand_tree.yview)
        scroll.pack(side="right", fill="y", pady=(0, 12), padx=(0, 8))
        self.hand_tree.configure(yscrollcommand=scroll.set)
        self.hand_tree.bind("<<TreeviewSelect>>", lambda _: self._select_tree_hand())
        self.hand_tree.bind("<Double-1>", lambda _: self.audit_selected_hand())

        self.hand_title_card = self._card(right)
        self.hand_title_card.pack(fill="x", pady=(0, 10))

        self.review_title_label = tk.Label(
            self.hand_title_card,
            text="Select a hand to begin",
            bg=PANEL_2, fg=WHITE,
            font=("Segoe UI Semibold", 18)
        )
        self.review_title_label.pack(anchor="w", padx=18, pady=(15, 4))

        self.review_meta_label = tk.Label(
            self.hand_title_card,
            text="",
            bg=PANEL_2, fg=MUTED,
            font=("Segoe UI", 10)
        )
        self.review_meta_label.pack(anchor="w", padx=18, pady=(0, 15))

        self.explanation_card = self._card(right, "Hand Review")
        self.explanation_card.pack(fill="both", expand=True)

        self.explanation_text = tk.Text(
            self.explanation_card,
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground="#244b7a",
            selectforeground=WHITE,
            relief="flat",
            font=("Segoe UI", 11),
            wrap="word",
            padx=18,
            pady=16
        )
        self.explanation_text.pack(fill="both", expand=True, padx=1, pady=(0, 1))

        # Persistent bottom action bar.
        bottom = tk.Frame(self.audit_tab, bg=BG)
        bottom.pack(fill="x", pady=(0, 2))

        ttk.Button(
            bottom, text="Audit This Hand", style="Accent.TButton",
            command=self.audit_selected_hand
        ).pack(side="left")

        ttk.Button(
            bottom, text="Show Raw Hand", command=self._show_raw_hand
        ).pack(side="left", padx=8)

        ttk.Label(
            bottom,
            text="Tip: double-click a hand to audit it.",
            style="Muted.TLabel"
        ).pack(side="right")

        self._hand_map = {}
        self._current_hand = None

    def _build_helper_tab(self):
        top = ttk.Label(self.helper_tab, padding=(0, 12))
        top.pack(fill="x")

        form = self._card(self.helper_tab, "Pre-flop decision")
        form.pack(fill="x", pady=(0, 14))
        grid = ttk.Frame(form)
        grid.pack(fill="x", padx=16, pady=(0, 16))

        self.hole_var = tk.StringVar(value="As,Ks")
        self.pos_var = tk.StringVar(value="MP")
        self.stack_var = tk.StringVar(value="40")
        self.context_var = tk.StringVar(value="unopened")
        self.limpers_var = tk.StringVar(value="0")
        self.call_var = tk.StringVar(value="0")
        self.eff_var = tk.StringVar(value="")
        self.raiser_var = tk.StringVar(value="LP")

        fields = [
            ("Hole cards", self.hole_var, 0, 0),
            ("Position", self.pos_var, 0, 2),
            ("Stack (BB)", self.stack_var, 0, 4),
            ("Context", self.context_var, 1, 0),
            ("Limpers", self.limpers_var, 1, 2),
            ("Call amount (BB)", self.call_var, 1, 4),
            ("Effective stack (BB)", self.eff_var, 2, 0),
            ("Raiser position", self.raiser_var, 2, 2),
        ]
        for label, var, r, c in fields:
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(row=r, column=c, sticky="w", padx=(0, 6), pady=6)
            if label == "Context":
                widget = ttk.Combobox(grid, textvariable=var, values=["unopened", "limpers", "raise", "bb_defense", "short_sb"], state="readonly", width=18)
            elif label == "Raiser position":
                widget = ttk.Combobox(grid, textvariable=var, values=["EP", "MP", "LP"], state="readonly", width=18)
            else:
                widget = ttk.Entry(grid, textvariable=var, width=20)
            widget.grid(row=r, column=c + 1, sticky="w", padx=(0, 25), pady=6)

        ttk.Button(grid, text="Get Strategy Decision", style="Accent.TButton", command=self.run_helper).grid(
            row=2, column=4, sticky="w", pady=6
        )

        result = self._card(self.helper_tab, "Decision")
        result.pack(fill="both", expand=True)
        self.helper_text = tk.Text(
            result, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=("Segoe UI", 11), wrap="word", padx=18, pady=16
        )
        self.helper_text.pack(fill="both", expand=True, padx=1, pady=(0, 1))

    # ---------- session / logging ----------
    def start_session(self):
        if self.active_session is not None:
            self.stop_logger()

        try:
            self.active_session = create_session(self.session_name_var.get())
            self.last_clipboard = pyperclip.paste()
        except Exception as exc:
            messagebox.showerror("Could not start session", str(exc))
            self.active_session = None
            return

        self.stop_event.clear()
        self.logger_thread = threading.Thread(target=self._logger_loop, daemon=True)
        self.logger_thread.start()

        self.status_var.set("Active")
        self.hand_count_var.set("0")
        self.score_var.set("Not audited")
        self._log_ui(f"Started {self.active_session.name}")
        self._refresh_sessions()

    def stop_logger(self):
        if self.active_session is None:
            return
        self.stop_event.set()
        if self.logger_thread and self.logger_thread.is_alive():
            self.logger_thread.join(timeout=2)
        folder = self.active_session
        hands = len(load_hands(folder))
        update_metadata(folder, ended_at=datetime.now().isoformat(timespec="seconds"), hands_captured=hands, status="completed")
        self.status_var.set("Completed")
        self._log_ui(f"Session stopped. {hands} hands saved.")
        self._refresh_sessions()
        self.active_session = None
        self.logger_thread = None

    def _logger_loop(self):
        while not self.stop_event.is_set():
            try:
                clip = pyperclip.paste()
                if clip != self.last_clipboard:
                    self.last_clipboard = clip
                    if "Hand ID:" in clip and self.active_session is not None:
                        count = append_hand(self.active_session, clip)
                        self.ui_queue.put(("hand", count, extract_hand_id(clip), self.active_session))
            except Exception as exc:
                self.ui_queue.put(("error", str(exc)))
            self.stop_event.wait(0.75)

    # ---------- review ----------
    def _refresh_sessions(self):
        folders = session_folders()
        labels = []
        for f in folders:
            meta = read_metadata(f)
            labels.append(f"{f.name} | {meta.get('label') or 'Unnamed'} | {meta.get('hands_captured', 0)} hands | {meta.get('status', 'unknown')}")
        self._session_map = {label: folder for label, folder in zip(labels, folders)}
        self.session_combo["values"] = labels
        if labels and self.selected_session_var.get() not in labels:
            self.selected_session_var.set(labels[0])
        self._on_session_change()

    def _selected_folder(self) -> Path | None:
        return getattr(self, "_session_map", {}).get(self.selected_session_var.get())

    def _on_session_change(self):
        folder = self._selected_folder()
        self._populate_hand_tree(folder)
        if folder:
            report = folder / "audit_report.txt"
            self.score_var.set("Audit available" if report.exists() else "Not audited")
        else:
            self.score_var.set("No audit")

    def _populate_hand_tree(self, folder: Path | None):
        for row in self.hand_tree.get_children():
            self.hand_tree.delete(row)
        self._hand_map = {}
        self._result_map = {}

        if not folder:
            return

        hands = load_hands(folder)
        for idx, hand in enumerate(hands):
            hid = extract_hand_id(hand)
            label = f"{idx + 1}. {hid[:28]}"
            item = self.hand_tree.insert("", "end", values=(label, "Not audited"))
            self._hand_map[item] = hand

            # Fast status from a saved per-hand report where available.
            report = folder / f"audit_hand_{safe_label(hid)}.txt"
            if report.exists():
                txt = report.read_text(encoding="utf-8", errors="replace")
                status = "BREACH" if "BREACH" in txt else "OK"
                self.hand_tree.set(item, "status", status)

        if self.hand_tree.get_children():
            self.hand_tree.selection_set(self.hand_tree.get_children()[0])
            self._select_tree_hand()

    def _select_tree_hand(self):
        selection = self.hand_tree.selection()
        if not selection:
            return

        item = selection[0]
        self._current_hand = self._hand_map.get(item)
        if self._current_hand:
            labels = list(self.hand_combo["values"])
            index = list(self._hand_map.keys()).index(item) if item in self._hand_map else 0
            if 0 <= index < len(labels):
                self.hand_combo.current(index)

            self._render_hand_preview(self._current_hand)
            self.hand_status_label.configure(
                text="Ready to audit",
                fg=ACCENT
            )

    def _render_hand_preview(self, hand):
        hid = extract_hand_id(hand)
        self.review_title_label.configure(text=f"Hand {hid}")
        # Lightweight metadata from raw history.
        hero_match = re.search(r"Dealt to\s+([A-Za-z0-9_]+):\s*\[(.*?)\]", hand)
        hole = hero_match.group(2) if hero_match else "Unknown"
        self.review_meta_label.configure(text=f"Hole cards: {hole}    •    Select “Audit Selected Hand” for the full explanation")
        self.explanation_text.delete("1.0", "end")
        self.explanation_text.insert(
            "end",
            "This hand is selected.\n\n"
            "The detailed review will show:\n"
            "• what you actually did\n"
            "• what the strategy required\n"
            "• the specific PDF rule involved\n"
            "• why that rule applies to this situation\n"
            "• what to do differently next time\n"
        )

    def audit_current(self):
        """Run the audit against the currently active session."""
        folder = self.active_session
        if not folder:
            messagebox.showinfo("No active session", "Start a session first.")
            return

        self._run_session_audit(folder)
        self.score_var.set("Auditing...")

    def audit_selected(self):
        folder = self._selected_folder()
        if not folder:
            messagebox.showinfo("No session", "Select a saved session first.")
            return
        self._run_session_audit(folder)

    def _run_session_audit(self, folder: Path):
        hands = load_hands(folder)
        if not hands:
            self.explanation_text.delete("1.0", "end")
            self.explanation_text.insert("end", "This session has no captured hands yet.")
            return

        def worker():
            results = []
            for hand in hands:
                try:
                    results.append(self.engine.audit_hand(hand, self.player_var.get().strip() or "AbhijitJain", self.style_var.get()))
                except Exception as exc:
                    results.append({"hand_id": extract_hand_id(hand), "compliant": False, "breaches": [f"Audit error: {exc}"]})

            # Save a human-readable session summary.
            lines = ["POKER STRATEGY SESSION REVIEW", "=" * 70, ""]
            total = len(results)
            good = sum(1 for r in results if r.get("compliant"))
            for r in results:
                lines.append(f"{r.get('hand_id', 'unknown')}: {'COMPLIANT' if r.get('compliant') else 'BREACH'}")
                for b in r.get("breaches", []):
                    lines.append(f"  - {b}")
            lines += ["", f"Hands: {total}", f"Compliant: {good}", f"With breaches: {total - good}"]
            (folder / "audit_report.txt").write_text("\n".join(lines), encoding="utf-8")
            self.ui_queue.put(("session_done", results, folder))

        threading.Thread(target=worker, daemon=True).start()

    def audit_selected_hand(self):
        folder = self._selected_folder()
        hand = self._current_hand
        if not folder or not hand:
            messagebox.showinfo("No hand", "Select a hand from the session list first.")
            return

        player = self.player_var.get().strip() or "AbhijitJain"
        style = self.style_var.get()
        self.explanation_text.delete("1.0", "end")
        self.explanation_text.insert("end", "Analyzing this hand...\n")
        self.explanation_text.update_idletasks()

        def worker():
            try:
                result = self.engine.audit_hand(hand, player, style)
                self.ui_queue.put(("hand_done", result, folder, hand))
            except Exception as exc:
                self.ui_queue.put(("hand_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _render_hand_result(self, result: dict):
        hid = result.get("hand_id", "unknown")
        self.review_title_label.configure(
            text=f"Hand {hid}  •  {'COMPLIANT' if result.get('compliant') else 'BREACH'}",
            fg=GREEN if result.get("compliant") else RED,
        )
        self.review_meta_label.configure(
            text=(
                f"{result.get('hole', 'Unknown')}    •    {result.get('position', 'Unknown')}    •    "
                f"{result.get('stack', 0)} chips    •    {'Multi-way' if result.get('multiway') else 'Heads-up'}"
            )
        )

        self.explanation_text.delete("1.0", "end")
        if not result.get("breaches"):
            self.explanation_text.insert(
                "end",
                "WHAT YOU DID\n\n"
                + plain_preflop_explanation(result)
                + "\n\n"
                "RESULT\n\n"
                "This hand followed the rules that applied to it.\n",
            )
            for a in result.get("streets", []):
                observed, expected, hand_class = street_display(a)
                self.explanation_text.insert(
                    "end",
                    f"\n{a.street.title()}\n"
                    f"Hand class: {hand_class}\n"
                    f"Your action: {observed}\n"
                    f"Strategy action: {expected}\n"
                    f"Board: {', '.join(c.rank + c.suit for c in a.board)}\n"
                )
        else:
            self.explanation_text.insert("end", "WHY THIS HAND WAS FLAGGED\n\n")
            for idx, breach in enumerate(result.get("breaches", []), 1):
                info = explain_breach(breach)
                self.explanation_text.insert(
                    "end",
                    f"{idx}. {info['title']}\n\n"
                    f"What happened:\n{info['what']}\n\n"
                    f"What the strategy says:\n{info['rule']}\n\n"
                    f"What to do next time:\n{info['fix']}\n\n"
                    + ("─" * 68) + "\n\n"
                )

            self.explanation_text.insert("end", "STREET-BY-STREET\n\n")
            self.explanation_text.insert("end", plain_preflop_explanation(result) + "\n\n")
            for a in result.get("streets", []):
                observed, expected, hand_class = street_display(a)
                self.explanation_text.insert(
                    "end",
                    f"{a.street.title()}: {hand_class}. "
                    f"You {observed.lower()}; strategy expected {expected.lower()}."
                )
                if a.violations:
                    self.explanation_text.insert("end", " ← breach here.")
                self.explanation_text.insert("end", "\n")
        self.explanation_text.see("1.0")

    def _show_raw_hand(self):
        if not self._current_hand:
            return
        popup = tk.Toplevel(self)
        popup.title("Raw Hand History")
        popup.geometry("820x620")
        popup.configure(bg=BG)
        text = tk.Text(popup, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Consolas", 10), wrap="word")
        text.pack(fill="both", expand=True, padx=12, pady=12)
        text.insert("end", self._current_hand)
        text.configure(state="disabled")

    def open_selected_folder(self):
        folder = self._selected_folder()
        if folder:
            self._open_path(folder)

    def _open_path(self, path: Path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Open folder failed", str(exc))

    # ---------- helper ----------
    def run_helper(self):
        try:
            result = self.engine.recommend_preflop(
                self.hole_var.get().strip(),
                self.pos_var.get().strip(),
                float(self.stack_var.get()),
                context=self.context_var.get(),
                limpers=int(self.limpers_var.get() or 0),
                call_amount_bb=float(self.call_var.get() or 0),
                effective_stack_bb=float(self.eff_var.get()) if self.eff_var.get().strip() else None,
                raiser_position=self.raiser_var.get().strip(),
                opponent_style=self.style_var.get(),
            )
        except Exception as exc:
            result = {"action": "error", "reason": str(exc)}

        self.helper_text.delete("1.0", "end")
        action = str(result.get("action", "")).upper()
        reason = result.get("reason", "")
        self.helper_text.insert(
            "end",
            f"ACTION\n\n{action}\n\n"
            f"WHY\n\n{reason}\n\n"
            "This decision is based on the supplied strategy PDF, not a general poker solver."
        )

    def _log_ui(self, message: str):
        if hasattr(self, "live_text"):
            self.live_text.insert("end", f"{datetime.now().strftime('%H:%M:%S')}  {message}\n")
            self.live_text.see("end")

    def _drain_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "hand":
                    _, count, hand_id, folder = item
                    self.hand_count_var.set(str(count))
                    self._log_ui(f"Captured {hand_id}")
                    if self.active_session == folder:
                        self._refresh_sessions()
                elif kind == "error":
                    self._log_ui(f"Logger error: {item[1]}")
                elif kind == "session_done":
                    _, results, folder = item
                    good = sum(1 for r in results if r.get("compliant"))
                    self.score_var.set(f"{good}/{len(results)} compliant")
                    self._populate_hand_tree(folder)
                    self._log_ui(f"Session audit finished: {good}/{len(results)} compliant")
                elif kind == "hand_done":
                    _, result, folder, hand = item
                    self._render_hand_result(result)
                    hid = result.get("hand_id", extract_hand_id(hand))
                    report = folder / f"audit_hand_{safe_label(hid)}.txt"
                    # Save a plain-language report, not terminal output.
                    if result.get("breaches"):
                        blocks = ["HAND REVIEW", "=" * 60, ""]
                        for breach in result["breaches"]:
                            info = explain_breach(breach)
                            blocks += [
                                info["title"],
                                "",
                                "What happened:",
                                info["what"],
                                "",
                                "What the strategy says:",
                                info["rule"],
                                "",
                                "What to do next time:",
                                info["fix"],
                                "",
                            ]
                    else:
                        blocks = ["HAND REVIEW", "=" * 60, "", "COMPLIANT", "", plain_preflop_explanation(result)]
                    report.write_text("\n".join(blocks), encoding="utf-8")
                    self._populate_hand_tree(folder)
                elif kind == "hand_error":
                    self.explanation_text.delete("1.0", "end")
                    self.explanation_text.insert("end", f"Could not audit this hand:\n\n{item[1]}")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _on_close(self):
        if self.active_session is not None:
            self.stop_logger()
        self.destroy()


if __name__ == "__main__":
    app = PokerApp()
    app.mainloop()
