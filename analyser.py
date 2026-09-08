import argparse
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any


RANKS = {r: i for i, r in enumerate("23456789TJQKA", start=2)}
RANK_CHARS = "23456789TJQKA"
SUITS = set("shdc")


@dataclass
class Card:
    rank: str
    suit: str

    @property
    def value(self) -> int:
        return RANKS[self.rank]


@dataclass
class Action:
    player: str
    action: str
    amount: int = 0
    raw: str = ""
    amount_is_total: bool = False


@dataclass
class StreetAudit:
    street: str
    board: List[Card]
    hero_action: str
    hero_amount: int
    first_to_act: bool
    pot_before_hero: Optional[int]
    opponent_bet: Optional[int]
    opponent_bet_pct: Optional[float]
    hand_class: str
    board_texture: str
    expected: str
    violations: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Card / hand evaluation
# ---------------------------------------------------------------------------

def parse_cards(cards_str: Optional[str]) -> List[Card]:
    if not cards_str or "Unknown" in cards_str:
        return []
    cards: List[Card] = []
    for token in re.split(r"\s*,\s*", cards_str.strip()):
        token = token.strip().strip("[]")
        if len(token) < 2:
            continue
        rank, suit = token[0].upper(), token[1].lower()
        if rank in RANKS and suit in SUITS:
            cards.append(Card(rank, suit))
    return cards


def card_key(card: Card) -> str:
    return f"{card.rank}{card.suit}"


def straight_high(ranks: List[int]) -> Optional[int]:
    vals = set(ranks)
    if 14 in vals:
        vals.add(1)  # wheel
    ordered = sorted(vals)
    run = 1
    best = None
    for i in range(1, len(ordered)):
        if ordered[i] == ordered[i - 1] + 1:
            run += 1
            if run >= 5:
                best = ordered[i]
        elif ordered[i] != ordered[i - 1]:
            run = 1
    return best


def hand_rank(cards: List[Card]) -> Tuple[int, Tuple[int, ...]]:
    """Evaluate up to 7 cards. Category: 8 straight flush ... 0 high card."""
    if not cards:
        return (0, ())

    by_rank: Dict[int, int] = {}
    by_suit: Dict[str, List[int]] = {}
    for c in cards:
        by_rank[c.value] = by_rank.get(c.value, 0) + 1
        by_suit.setdefault(c.suit, []).append(c.value)

    flushes = {s: sorted(v, reverse=True) for s, v in by_suit.items() if len(v) >= 5}
    sf_highs = []
    for vals in flushes.values():
        h = straight_high(vals)
        if h is not None:
            sf_highs.append(h)
    if sf_highs:
        return (8, (max(sf_highs),))

    quads = sorted((r for r, n in by_rank.items() if n >= 4), reverse=True)
    if quads:
        q = quads[0]
        kickers = sorted((r for r in by_rank if r != q), reverse=True)
        return (7, (q, kickers[0]))

    trips = sorted((r for r, n in by_rank.items() if n >= 3), reverse=True)
    if trips:
        top_trip = trips[0]
        pair_candidates = sorted(
            (r for r, n in by_rank.items() if r != top_trip and n >= 2), reverse=True
        )
        if pair_candidates:
            return (6, (top_trip, pair_candidates[0]))

    if flushes:
        top5 = max(flushes.values())[:5]
        return (5, tuple(top5))

    sh = straight_high(list(by_rank.keys()))
    if sh is not None:
        return (4, (sh,))

    if trips:
        t = trips[0]
        kickers = sorted((r for r in by_rank if r != t), reverse=True)[:2]
        return (3, (t, *kickers))

    pairs = sorted((r for r, n in by_rank.items() if n >= 2), reverse=True)
    if len(pairs) >= 2:
        p1, p2 = pairs[:2]
        kicker = max((r for r in by_rank if r not in (p1, p2)), default=0)
        return (2, (p1, p2, kicker))
    if len(pairs) == 1:
        p = pairs[0]
        kickers = sorted((r for r in by_rank if r != p), reverse=True)[:3]
        return (1, (p, *kickers))

    return (0, tuple(sorted(by_rank.keys(), reverse=True)[:5]))


HAND_NAMES = {
    8: "Straight Flush",
    7: "Quads",
    6: "Full House",
    5: "Flush",
    4: "Straight",
    3: "Set/Trips",
    2: "Two Pair",
    1: "Pair",
    0: "High Card",
}


def has_flush_draw(cards: List[Card], hole: List[Card], board: List[Card]) -> bool:
    # Strategy treats a standard four-flush opportunity as a strong draw.
    counts: Dict[str, int] = {}
    for c in cards:
        counts[c.suit] = counts.get(c.suit, 0) + 1
    return any(n >= 4 for n in counts.values()) and hand_rank(cards)[0] < 5


def straight_draw_type(cards: List[Card]) -> Optional[str]:
    vals = sorted(set(c.value for c in cards))
    if 14 in vals:
        vals = [1] + vals
    vals = sorted(set(vals))

    # Search five-card windows. Missing exactly 1 inside the window = gutshot;
    # missing an endpoint = OESD. We only call it strong when two ends are open.
    strong = False
    gut = False
    for start in range(1, 11):
        window = set(range(start, start + 5))
        hits = len(window.intersection(vals))
        if hits != 4:
            continue
        missing = sorted(window - set(vals))
        if not missing:
            continue
        m = missing[0]
        if m == start or m == start + 4:
            strong = True
        else:
            gut = True
    if strong:
        return "OESD"
    if gut:
        return "Gutshot"
    return None


def top_pair_status(hole: List[Card], board: List[Card], rank_info: Tuple[int, Tuple[int, ...]]) -> Optional[str]:
    if not board:
        return None
    board_values = sorted({c.value for c in board}, reverse=True)
    pair_value = rank_info[1][0] if rank_info[0] == 1 else None
    if pair_value is None:
        return None
    top_board = board_values[0]
    # Pocket pair is overpair if its pair is above the board's highest card.
    if len(hole) == 2 and hole[0].value == hole[1].value and pair_value > top_board:
        return "Overpair"
    if pair_value == top_board:
        return "Top Pair"
    if len(board_values) >= 2 and pair_value == board_values[1]:
        return "Middle Pair"
    return "Bottom Pair"


def two_pair_quality(hole: List[Card], board: List[Card]) -> Optional[str]:
    rank, vals = hand_rank(hole + board)
    if rank != 2:
        return None
    pair_values = list(vals[:2])
    board_values = sorted([c.value for c in board], reverse=True)
    hole_values = [c.value for c in hole]
    if len(board_values) < 2:
        return None
    top_two = board_values[:2]
    # "Top two" means hero's two paired ranks are the board's two highest ranks.
    if all(v in top_two for v in pair_values) and pair_values == top_two:
        return "Top Two Pair"
    if all(v in board_values[:3] for v in pair_values):
        return "Mixed Two Pair"
    if min(pair_values) < board_values[0]:
        return "Bottom Two Pair"
    return "Two Pair"


def classify_hand(hole: List[Card], board: List[Card], street: Optional[str] = None) -> str:
    """Source-faithful coarse class used by the strategy, not a poker solver."""
    if len(hole) < 2 or len(board) < 3:
        return "Unknown"
    all_cards = hole + board
    category, _ = hand_rank(all_cards)
    if category >= 4:
        return "Premium"
    if category == 3:
        return "Premium"  # set/trips
    if category == 2:
        quality = two_pair_quality(hole, board)
        return "Premium" if quality else "Premium"
    if category == 1:
        pair_kind = top_pair_status(hole, board, hand_rank(all_cards))
        return "Showdown"

    # On the river there are no cards left to complete a draw. The PDF treats
    # a missed draw as Air, so never label an unfinished draw as Strong/Weak Draw
    # once all five board cards are known.
    if street == "RIVER" or len(board) >= 5:
        return "Air"

    if has_flush_draw(all_cards, hole, board):
        return "Strong Draw"
    sd = straight_draw_type(all_cards)
    if sd == "OESD":
        return "Strong Draw"
    if sd == "Gutshot":
        return "Weak Draw"
    return "Air"


def detailed_hand_class(hole: List[Card], board: List[Card]) -> str:
    if len(hole) < 2 or len(board) < 3:
        return "Unknown"
    category, vals = hand_rank(hole + board)
    if category >= 8:
        return "Premium / Straight Flush"
    if category == 7:
        return "Premium / Quads"
    if category == 6:
        return "Premium / Full House"
    if category == 5:
        return "Premium / Flush"
    if category == 4:
        return "Premium / Straight"
    if category == 3:
        return "Premium / Set+"
    if category == 2:
        q = two_pair_quality(hole, board)
        return f"Premium / {q or 'Two Pair'}"
    if category == 1:
        pair_kind = top_pair_status(hole, board, (category, vals))
        return f"Showdown / {pair_kind or 'Pair'}"
    if has_flush_draw(hole + board, hole, board):
        return "Strong Draw / Flush Draw"
    sd = straight_draw_type(hole + board)
    if sd == "OESD":
        return "Strong Draw / OESD"
    if sd == "Gutshot":
        return "Weak Draw / Gutshot"
    return "Air"


# ---------------------------------------------------------------------------
# Board texture / scary card
# ---------------------------------------------------------------------------

def straight_connectivity(board: List[Card]) -> bool:
    ranks = sorted(set(c.value for c in board))
    if 14 in ranks:
        ranks = [1] + ranks
    # A board is "wet" when it exposes a meaningful 3- or 4-card straight run.
    for start in range(1, 11):
        hit = len(set(range(start, start + 5)).intersection(ranks))
        if hit >= 3:
            return True
    return False


def board_texture(board: List[Card]) -> str:
    if len(board) < 3:
        return "Unknown"
    suit_counts: Dict[str, int] = {}
    for c in board:
        suit_counts[c.suit] = suit_counts.get(c.suit, 0) + 1
    flush_pressure = max(suit_counts.values(), default=0) >= 2
    return "Wet (Dynamic)" if flush_pressure or straight_connectivity(board) else "Dry (Static)"


def scary_card(previous: List[Card], current: List[Card]) -> bool:
    if len(current) <= len(previous):
        return False
    # Third/fourth board card of a suit, or four-card straight texture.
    suit_counts: Dict[str, int] = {}
    for c in current:
        suit_counts[c.suit] = suit_counts.get(c.suit, 0) + 1
    if max(suit_counts.values(), default=0) >= 3:
        return True

    vals = sorted(set(c.value for c in current))
    if 14 in vals:
        vals = [1] + vals
    for start in range(1, 11):
        if len(set(range(start, start + 5)).intersection(vals)) >= 4:
            return True
    return False


# ---------------------------------------------------------------------------
# Preflop strategy helpers
# ---------------------------------------------------------------------------

def normalize_hand(hole: List[Card]) -> Tuple[int, int, bool, bool]:
    a, b = sorted((c.value for c in hole), reverse=True)
    return a, b, hole[0].suit == hole[1].suit, a == b


def hand_notation(hole: List[Card]) -> str:
    if len(hole) != 2:
        return ""
    a, b, suited, pair = normalize_hand(hole)
    ra = RANK_CHARS[a - 2]
    rb = RANK_CHARS[b - 2]
    if pair:
        return ra + rb
    return ra + rb + ("s" if suited else "o")


def is_connector_or_one_gapper(hole: List[Card]) -> Tuple[bool, bool]:
    if len(hole) != 2:
        return False, False
    a, b, suited, _ = normalize_hand(hole)
    gap = a - b
    return gap == 1, gap == 2


def preflop_decision_unopened(hole: List[Card], position: str) -> Tuple[str, str]:
    a, b, suited, pair = normalize_hand(hole)
    if pair:
        if position == "EP" and a >= 7:
            return "raise", "EP: 77+"
        if position == "MP" and a >= 5:
            return "raise", "MP: 55+"
        if position in ("CO", "BTN", "LP") and a >= 2:
            return "raise", "LP: all pairs"
        return "fold", "Outside unopened pair range"

    if a == 14:  # A-x
        if suited:
            if position == "EP" and b >= 10:
                return "raise", "EP: ATs+"
            if position == "MP" and (b >= 8 or b in (4, 5)):
                return "raise", "MP: A8s+ plus A4s-A5s"
            if position in ("CO", "LP") and b >= 2:
                return "raise", "CO: all suited aces"
            if position == "BTN" and b >= 2:
                return "raise", "BTN: all suited aces"
        else:
            if position == "EP" and b >= 12:
                return "raise", "EP: AQo+"
            if position == "MP" and b >= 11:
                return "raise", "MP: AJo+"
            if position == "CO" and b >= 10:
                return "raise", "CO: ATo+"
            if position == "BTN" and b >= 2:
                return "raise", "BTN: A2o+"
        return "fold", "Ace does not meet unopened anchor"

    if a == 13:  # K-x
        if suited:
            if position == "EP" and b >= 11:
                return "raise", "EP: KJs+"
            if position == "MP" and b >= 10:
                return "raise", "MP: KTs+"
            if position == "CO" and b >= 8:
                return "raise", "CO: K8s+"
            if position == "BTN" and b >= 2:
                return "raise", "BTN: K2s+"
        else:
            if position == "MP" and b == 12:
                return "raise", "MP: KQo"
            if position == "CO" and b >= 11:
                return "raise", "CO: KJo+"
            if position == "BTN" and b >= 9:
                return "raise", "BTN: K9o+"
        return "fold", "King does not meet unopened anchor"

    if a == 12:  # Q-x
        if suited:
            if position == "EP" and b == 11:
                return "raise", "EP: QJs"
            if position == "MP" and b >= 10:
                return "raise", "MP: QTs+"
            if position == "CO" and b >= 9:
                return "raise", "CO: Q9s+"
            if position == "BTN" and b >= 5:
                return "raise", "BTN: Q5s+"
        else:
            if position == "CO" and b == 11:
                return "raise", "CO: QJo"
            if position == "BTN" and b >= 10:
                return "raise", "BTN: QTo+"
        return "fold", "Queen does not meet unopened anchor"

    if a == 11:  # J-x
        if suited:
            if position == "MP" and b == 10:
                return "raise", "MP: JTs"
            if position == "CO" and b >= 9:
                return "raise", "CO: J9s+"
            if position == "BTN" and b >= 7:
                return "raise", "BTN: J7s+"
        else:
            if position == "BTN" and b == 10:
                return "raise", "BTN: JTo"
        return "fold", "Jack does not meet unopened anchor"

    connector, one_gapper = is_connector_or_one_gapper(hole)
    if suited and connector:
        if position == "MP" and a in (9, 10):
            return "raise", "MP: 98s/T9s"
        if position == "CO" and a >= 7:
            return "raise", "CO: 76s+"
        if position == "BTN" and a >= 5:
            return "raise", "BTN: 54s+"
    if suited and one_gapper and position in ("CO", "BTN"):
        return "raise", f"{position}: suited one-gapper late-position open"
    return "fold", "Does not meet unopened anchor"


def stress_test_limpers(hole: List[Card]) -> Tuple[bool, str]:
    """Return (raise_downgraded, reason)."""
    a, b, suited, pair = normalize_hand(hole)
    note = hand_notation(hole)
    if not suited and a == 14 and 2 <= b <= 9:
        return True, "Stress Test: A2o-A9o downgraded"
    if not suited and note in {"KTo", "KJo", "K9o", "QJo", "QTo", "Q9o", "JTo"}:
        return True, f"Stress Test: {note} prohibited vs limpers"
    if pair and 2 <= a <= 8:
        return True, "Stress Test: 22-88 are not iso-raise hands"
    return False, ""


def preflop_vs_limpers(hole: List[Card], position: str, limpers: int) -> Tuple[str, str]:
    connector, one_gapper = is_connector_or_one_gapper(hole)
    if one_gapper and len(hole) == 2 and hole[0].suit == hole[1].suit:
        if position in {"CO", "BTN", "LP"}:
            if limpers >= 2:
                return "call", "Suited one-gapper: limp behind is authorized with 2+ limpers"
            return "fold", "Suited one-gapper: limp behind requires 2+ limpers"
        return "fold", "Suited one-gappers are late-position only"

    action, reason = preflop_decision_unopened(hole, position)
    downgraded, stress_reason = stress_test_limpers(hole)
    if downgraded:
        a, _, _, pair = normalize_hand(hole)
        if pair and a <= 8:
            return "call", stress_reason + "; overcall instead of bloating pot"
        return "fold", stress_reason
    if action == "raise":
        return "raise", f"Iso-raise: {reason}; size = 3x BB + 1x per limper"
    return action, reason


def preflop_facing_raise(hole: List[Card], position: str, effective_stack: int,
                         call_amount: int, opponent_style: str = "unknown") -> Tuple[str, str]:
    note = hand_notation(hole)
    a, b, suited, pair = normalize_hand(hole)
    # SB rule is position-specific and supersedes the generic rule.
    if position == "SB":
        if pair and a >= 11 or note in {"AKs", "AKo", "AQs"}:
            return "raise", "SB: 3-bet/fold, JJ+ / AK / AQs"
        return "fold", "SB: 3-bet or fold"

    # Standard raised-pot matrix in the document.
    if note in {"AA", "KK", "QQ", "AKs", "AKo"}:
        return "raise", "Premium value: 3-bet"
    # Speculative: 22-JJ, suited connectors, suited broadways, AQo.
    if pair and 2 <= a <= 11:
        mult = effective_stack / call_amount if call_amount else 0
        needed = 10 if a <= 8 else 12
        if mult >= needed:
            return "call", f"Speculative pair: Rule of 15/10-12x satisfied ({mult:.1f}x)"
        return "fold", f"Set-mining stack too shallow ({mult:.1f}x < {needed}x)"
    if note == "AQo":
        return "call", "Speculative AQo flat-call"
    connector, one_gapper = is_connector_or_one_gapper(hole)
    if suited and connector:
        return ("call", "Suited connector: Rule of 15") if effective_stack >= 15 * call_amount else ("fold", "Suited connector fails Rule of 15")
    if suited and one_gapper:
        return ("call", "Suited one-gapper: Rule of 20") if effective_stack >= 20 * call_amount else ("fold", "Suited one-gapper fails Rule of 20")
    if suited and a >= 10 and b >= 10:
        return ("call", "Suited Broadway: Rule of 15") if effective_stack >= 15 * call_amount else ("fold", "Suited Broadway fails Rule of 15")
    if note in {"AJo", "KQo"} or (a == 14 and suited and b <= 9):
        return "fold", "Marginal/dominated hand vs raise"
    return "fold", "Outside facing-raise range"


def bb_defense(hole: List[Card], raiser_position: str) -> Tuple[str, str]:
    note = hand_notation(hole)
    a, b, suited, pair = normalize_hand(hole)
    if raiser_position in {"EP", "MP"}:
        if pair:
            return ("raise", "BB vs EP: QQ+") if a >= 12 else ("call", "BB vs EP: 22-JJ")
        if suited and a == 14:
            return ("raise", "BB vs EP: AKs") if b == 13 else (("call", "BB vs EP: ATs-AQs") if b >= 10 else ("fold", "BB vs EP: weaker suited ace"))
        if not suited and a == 14:
            return ("raise", "BB vs EP: AQo-AKo") if b >= 12 else ("fold", "BB vs EP: ATo/AJo and worse")
        if suited and a == 13 and b == 12:
            return "call", "BB vs EP: KQs"
        if suited and a == 11 and b == 10:
            return "call", "BB vs EP: JTs"
        if suited and note in {"T9s", "98s", "87s"}:
            return "call", "BB vs EP: T9s/98s/87s"
        return "fold", "BB vs EP: fold outside range"

    # Late-position raise: wider defense and specified 3-bets.
    if pair:
        return ("raise", "BB vs LP: JJ+") if a >= 11 else ("call", "BB vs LP: 22-TT")
    if suited and a == 14:
        if 2 <= b <= 5 or b >= 12:
            return "raise", "BB vs LP: AQs+ / A2s-A5s 3-bet"
        if 2 <= b <= 11:
            return "call", "BB vs LP: A2s-AJs call"
    if not suited and a == 14:
        return ("raise", "BB vs LP: AKo") if b == 13 else ("call", "BB vs LP: A2o-AQo")
    if suited and a == 13:
        if note in {"KQs", "K9s", "K8s"}:
            return "raise", "BB vs LP: KQs/K9s/K8s 3-bet"
        if b >= 2:
            return "call", "BB vs LP: K2s-KTs call"
    if not suited and a == 13 and b >= 10:
        return "call", "BB vs LP: KTo+ call"
    if suited and a == 12 and b >= 2:
        return "call", "BB vs LP: Q2s+"
    if not suited and a == 12 and b >= 11:
        return "call", "BB vs LP: QJo+"
    if suited and a == 11 and b >= 2:
        return "call", "BB vs LP: J2s+"
    if not suited and a == 11 and b == 10:
        return "call", "BB vs LP: JTo"
    if suited and a - b == 1 and b >= 4:
        if note == "T9s":
            return "raise", "BB vs LP: T9s 3-bet bluff"
        return "call", "BB vs LP: 54s+"
    return "fold", "BB vs LP: outside defense range"


def short_stack_sb(hole: List[Card]) -> Tuple[str, str]:
    note = hand_notation(hole)
    a, b, suited, pair = normalize_hand(hole)
    if pair and a >= 9:
        return "raise", "<20BB SB override: 99+ 3-bet"
    if suited and a == 14 and (b >= 11 or 2 <= b <= 5):
        return "raise", "<20BB SB override: AJs+/A2s-A5s 3-bet"
    if not suited and a == 14 and b >= 12:
        return "raise", "<20BB SB override: AQo+ 3-bet"
    if suited and a == 13 and (b == 12 or b == 9):
        return "raise", "<20BB SB override: KQs/K9s"
    return "fold", "<20BB SB override: fold outside range"


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

ACTION_RE = re.compile(
    r"^\s*([A-Za-z0-9_]+):\s*"
    r"(folds|calls|raises|bets|checks|all[- ]in|shoves)(?:\s+(?:to\s+)?([\d,]+))?",
    re.I | re.M,
)


def normalize_action_name(action: str) -> str:
    a = action.lower().replace("-", " ")
    if a == "all in" or a == "shoves":
        return "allin"
    return a


def parse_actions(text: str) -> List[Action]:
    actions: List[Action] = []
    for m in ACTION_RE.finditer(text):
        player = m.group(1)
        act = normalize_action_name(m.group(2))
        amount = int(m.group(3).replace(",", "")) if m.group(3) else 0
        raw = m.group(0).strip()
        amount_is_total = bool(re.search(r"raises\s+to", raw, re.I))
        actions.append(Action(player, act, amount, raw, amount_is_total))
    return actions


def split_street_sections(hand: str) -> Dict[str, str]:
    markers = ["PRE-FLOP", "FLOP", "TURN", "RIVER", "SHOWDOWN", "HAND END"]
    sections: Dict[str, str] = {}
    for i, marker in enumerate(markers):
        nxt = markers[i + 1:] or []
        pattern = rf"\*\*\* {re.escape(marker)} \*\*\*(.*?)(?=\*\*\* (?:{'|'.join(map(re.escape, nxt))}) \*\*\*|\Z)"
        m = re.search(pattern, hand, re.S)
        if m:
            sections[marker] = m.group(1)
    return sections


def extract_hand_id(hand: str) -> str:
    m = re.search(r"Hand ID:\s*([^\s]+)", hand)
    return m.group(1) if m else "Unknown"


def extract_hole_cards(hand: str, target_player: str) -> List[Card]:
    m = re.search(rf"Dealt to {re.escape(target_player)}:\s*\[(.*?)\]", hand, re.I)
    return parse_cards(m.group(1)) if m else []


def extract_stack(hand: str, player: str) -> int:
    m = re.search(rf"{re.escape(player)}\s*\(([\d,]+)\s*chips\)", hand, re.I)
    return int(m.group(1).replace(",", "")) if m else 0


def extract_player_names(preflop_text: str, hand: str) -> List[str]:
    names: List[str] = []
    for line in preflop_text.splitlines():
        m = ACTION_RE.match(line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    # Ensure blinds exist in the order if they were not action lines.
    for blind_name in re.findall(r"([A-Za-z0-9_]+) posts (?:small|big) blind", hand, re.I):
        if blind_name not in names:
            names.append(blind_name)
    return names


def get_position(hand: str, target_player: str, preflop_text: str) -> str:
    if re.search(rf"{re.escape(target_player)} posts small blind", hand, re.I):
        return "SB"
    if re.search(rf"{re.escape(target_player)} posts big blind", hand, re.I):
        return "BB"

    players = extract_player_names(preflop_text, hand)
    if target_player not in players:
        return "Unknown"

    # Unique first-action order approximates actual table order. Blinds are the last two.
    nonblind = [p for p in players if not re.search(rf"{re.escape(p)} posts (?:small|big) blind", hand, re.I)]
    idx = nonblind.index(target_player)
    n = len(nonblind)
    if n <= 2:
        return "LP"
    if n >= 5:
        if idx == n - 2:
            return "CO"
        if idx == n - 1:
            return "BTN"
    if idx <= max(0, n - 4):
        return "EP" if idx == 0 else "MP"
    return "MP"


def count_pot_contribution(actions: List[Action], initial_blinds: int = 0) -> int:
    pot = initial_blinds
    for a in actions:
        if a.action in {"calls", "bets", "raises", "allin"}:
            pot += max(0, a.amount)
    return pot


def parse_posted_blinds(hand: str) -> int:
    total = 0
    for m in re.finditer(r"posts\s+(small|big) blind\s+([\d,]+)", hand, re.I):
        total += int(m.group(2).replace(",", ""))
    return total


def parse_big_blind(hand: str) -> int:
    m = re.search(r"posts\s+big blind\s+([\d,]+)", hand, re.I)
    return int(m.group(1).replace(",", "")) if m else 0


def street_pot_context(actions_before_street: List[Action], hand: str) -> int:
    return count_pot_contribution(actions_before_street, parse_posted_blinds(hand))


def first_action_index(actions: List[Action], player: str) -> Optional[int]:
    for i, a in enumerate(actions):
        if a.player == player:
            return i
    return None


def prior_context(actions: List[Action], hero: str) -> Dict[str, Any]:
    idx = first_action_index(actions, hero)
    prior = actions[:idx] if idx is not None else []
    raises = [a for a in prior if a.action in {"raises", "allin"}]
    callers_after_raise = 0
    if raises:
        last_raise_idx = max(i for i, a in enumerate(prior) if a.action in {"raises", "allin"})
        callers_after_raise = sum(1 for a in prior[last_raise_idx + 1:] if a.action == "calls")
    return {
        "idx": idx,
        "prior": prior,
        "facing_raise": bool(raises),
        "facing_allin": bool(any(a.action == "allin" for a in prior)),
        "callers_after_raise": callers_after_raise,
        "limpers": sum(1 for a in prior if a.action == "calls") if not raises else 0,
    }


# ---------------------------------------------------------------------------
# Pot / opponent-action reconstruction
# ---------------------------------------------------------------------------

class PotTracker:
    def __init__(self, starting_pot: int = 0):
        self.pot = starting_pot
        self.contributed: Dict[str, int] = {}
        self.pot_before: Dict[int, int] = {}

    def apply(self, action_idx: int, action: Action) -> None:
        self.pot_before[action_idx] = self.pot
        amount = max(0, action.amount)
        if action.action in {"calls", "bets", "raises", "allin"}:
            if action.amount_is_total:
                amount = max(0, amount - self.contributed.get(action.player, 0))
            self.pot += amount
            self.contributed[action.player] = self.contributed.get(action.player, 0) + amount


def opponent_aggression(street_actions: List[Action], hero: str) -> Dict[str, bool]:
    return {
        "bet": any(a.player != hero and a.action in {"bets", "raises", "allin"} for a in street_actions),
        "raise": any(a.player != hero and a.action in {"raises", "allin"} for a in street_actions),
    }


def hero_action_in_street(actions: List[Action], hero: str) -> Optional[Action]:
    for a in actions:
        if a.player == hero:
            return a
    return None


def last_opponent_bet_before_hero(actions: List[Action], hero: str) -> Optional[Action]:
    result = None
    for a in actions:
        if a.player == hero:
            break
        if a.player != hero and a.action in {"bets", "raises", "allin"}:
            result = a
    return result


# ---------------------------------------------------------------------------
# Postflop strategy engine
# ---------------------------------------------------------------------------

def flop_first_to_act(hclass: str, texture: str) -> str:
    if hclass == "Premium":
        return "bet 33%" if texture == "Dry (Static)" else "bet 75-100%"
    if hclass == "Showdown":
        return "check or bet 33%" if texture == "Dry (Static)" else "check/call"
    if hclass == "Strong Draw":
        return "bet/raise 66%"
    return "check/fold"


def flop_call_limit(hclass: str) -> Optional[Tuple[float, str]]:
    return {
        "Strong Draw": (75.0, "call up to 75% pot"),
        "Weak Draw": (25.0, "call up to 25% pot"),
        "Showdown": (50.0, "call up to 50% pot"),
    }.get(hclass)


def turn_first_to_act(hclass: str, scary: bool, exact_category: int = 0, multiway: bool = False) -> str:
    if multiway and hclass == "Strong Draw":
        return "check/call"
    if hclass == "Premium":
        if scary and exact_category in {2, 3}:
            return "check"
        return "bet 66-75%"
    if hclass == "Showdown":
        return "check"
    if hclass == "Strong Draw":
        return "check/call" if scary else "bet 50-66%"
    return "check/fold"


def turn_call_limit(hclass: str) -> Optional[Tuple[float, str]]:
    return {
        "Strong Draw": (50.0, "call up to 50% pot"),
        "Showdown": (33.0, "call up to 33% pot"),
        "Weak Draw": (0.0, "fold"),
        "Air": (0.0, "fold"),
    }.get(hclass)


def river_first_to_act(hclass: str, scary: bool, exact_category: int = 0) -> str:
    if hclass == "Premium":
        # The PDF specifically downgrades Two Pair / Trips on scary river cards,
        # but says an absolute nuts hand can still bet.
        if scary and exact_category in {2, 3}:
            return "check"
        return "bet 50-75%"
    if hclass == "Showdown":
        return "check"
    if hclass == "Air":
        return "overbet 75-100%+ only after terrifying river" if scary else "check/fold"
    return "check/fold"


def river_facing_bet(hole: List[Card], board: List[Card], bet_pct: Optional[float],
                     prior_opponent_bets: Tuple[bool, bool], scary: bool,
                     premium_quality: Optional[str] = None) -> str:
    hclass = classify_hand(hole, board, "river")
    if hclass == "Air":
        return "fold"
    if hclass == "Showdown":
        if scary:
            return "fold"
        if prior_opponent_bets[0] and prior_opponent_bets[1]:
            return "fold"
        if bet_pct is not None and bet_pct < 50:
            return "call"
        if bet_pct is not None and bet_pct >= 75:
            return "fold"
        return "call"
    if hclass == "Premium":
        if scary:
            return "call or raise only with clearly retained premium"
        return "raise or call"
    return "fold"


def compare_action(observed: str, expected: str) -> bool:
    """Loose action compliance matcher for matrix phrases."""
    observed = observed.lower()
    expected = expected.lower()
    if expected.startswith("check or"):
        return observed in {"checks", "bets"}
    if expected == "check/call":
        return observed in {"checks", "calls"}
    if expected.startswith("check/fold"):
        return observed in {"checks", "folds"}
    if expected.startswith("bet"):
        return observed in {"bets", "raises"}
    if expected.startswith("bet/raise"):
        return observed in {"bets", "raises"}
    if expected.startswith("call"):
        return observed == "calls"
    if expected == "fold":
        return observed == "folds"
    if expected.startswith("raise"):
        return observed in {"raises", "allin"}
    return True


def pct(amount: int, pot: int) -> Optional[float]:
    if amount <= 0 or pot <= 0:
        return None
    return round(amount / pot * 100, 1)


# ---------------------------------------------------------------------------
# Complete audit
# ---------------------------------------------------------------------------

def audit_preflop(hand: str, hero: str, hole: List[Card], position: str,
                   stack: int, preflop_actions: List[Action],
                   opponent_style: str = "unknown") -> Dict[str, Any]:
    ctx = prior_context(preflop_actions, hero)
    hero_action = hero_action_in_street(preflop_actions, hero)
    observed = hero_action.action if hero_action else "none"
    hero_amount = hero_action.amount if hero_action else 0

    if not hole:
        return {"observed": observed, "expected": "unknown", "reason": "No hole cards", "breaches": []}

    # Approximate effective stack from opponent stacks where possible.
    all_players = set(a.player for a in preflop_actions)
    stacks = [extract_stack(hand, p) for p in all_players if p != hero and extract_stack(hand, p) > 0]
    effective_stack = min([stack] + stacks) if stacks else stack
    call_amount = hero_amount if observed == "calls" else 0

    # Short-stack SB override.
    bb_size = parse_big_blind(hand)
    if bb_size and stack < 20 * bb_size and position == "SB":
        expected, reason = short_stack_sb(hole)
    elif ctx["facing_allin"] and bb_size and stack >= 40 * bb_size:
        note = hand_notation(hole)
        a, b, suited, pair = normalize_hand(hole)
        if note in {"AA", "KK"}:
            expected, reason = "call", "40+BB facing massive all-in: AA/KK snap call"
        elif note in {"QQ", "AKs", "AKo"}:
            if opponent_style.lower() in {"loose", "aggressive", "loose/aggressive"}:
                expected, reason = "call", "40+BB all-in: QQ/AK vs loose/aggressive"
            else:
                expected, reason = "fold", "40+BB all-in: QQ/AK vs tight/unknown"
        else:
            expected, reason = "fold", "40+BB all-in: JJ/TT/AQ/weaker fold"
    elif position == "BB" and ctx["facing_raise"]:
        # Infer raiser's position from the first raiser in the prior action.
        raiser = next((a.player for a in ctx["prior"] if a.action in {"raises", "allin"}), None)
        raiser_pos = "LP" if raiser and re.search(rf"{re.escape(raiser)}.*button", hand, re.I) else "EP"
        # If we can locate a recognizable named role in the text, use it; otherwise fall back to EP/LP by order.
        try:
            ptxt = split_street_sections(hand).get("PRE-FLOP", "")
            if raiser:
                rp = get_position(hand, raiser, ptxt)
                raiser_pos = "LP" if rp in {"CO", "BTN", "LP"} else rp
        except Exception:
            pass
        expected, reason = bb_defense(hole, raiser_pos)
    elif ctx["facing_raise"]:
        expected, reason = preflop_facing_raise(hole, position, effective_stack, max(call_amount, 1), opponent_style)
    elif ctx["limpers"] > 0:
        expected, reason = preflop_vs_limpers(hole, position, ctx["limpers"])
    else:
        expected, reason = preflop_decision_unopened(hole, position)

    # Initiating a 5-bet all-in. The PDF authorizes only AA/KK, or AK when hero is OOP.
    prior_raise_count = sum(1 for a in ctx["prior"] if a.action == "raises")
    if observed == "allin" and prior_raise_count >= 4:
        note = hand_notation(hole)
        raiser = next((a.player for a in reversed(ctx["prior"]) if a.action == "raises"), None)
        raiser_pos = get_position(hand, raiser, split_street_sections(hand).get("PRE-FLOP", "")) if raiser else "Unknown"
        pos_order = {"EP": 0, "MP": 1, "CO": 2, "BTN": 3, "SB": 4, "BB": 5, "LP": 3}
        oop = position in {"SB", "BB"} or (raiser_pos in pos_order and position in pos_order and pos_order[position] < pos_order[raiser_pos])
        if note in {"AA", "KK"}:
            expected, reason = "allin", "5-bet all-in: AA/KK always shove"
        elif note in {"AKs", "AKo"} and oop:
            expected, reason = "allin", "5-bet all-in: AK allowed OOP"
        else:
            expected, reason = "fold", "5-bet all-in: only AA/KK, or AK OOP, are authorized"

    # Squeeze protocol: premium + raise + callers before hero.
    if ctx["facing_raise"] and ctx["callers_after_raise"] > 0 and hand_notation(hole) in {"AA", "KK", "QQ", "AKs", "AKo"}:
        original_raise = next((a.amount for a in ctx["prior"] if a.action == "raises"), 0)
        squeeze_size = 3 * original_raise + ctx["callers_after_raise"] * original_raise
        if squeeze_size >= 0.30 * max(stack, 1):
            expected = "allin"
            reason = f"Dynamic squeeze: {squeeze_size} >= 30% of stack {stack}, shove"
        else:
            expected = "raise"
            reason = f"Dynamic squeeze: 3x raise + 1x per caller = {squeeze_size}"

    breaches: List[str] = []
    if expected != "unknown":
        if expected == "allin":
            ok = observed == "allin"
        elif expected == "raise":
            ok = observed in {"raises", "allin"}
        elif expected == "call":
            ok = observed == "calls"
        elif expected == "fold":
            ok = observed == "folds"
        else:
            ok = observed == expected
        if not ok:
            breaches.append(f"Preflop: expected {expected}, observed {observed}. {reason}")

    # Open sizing / iso sizing / SB sizing when raising.
    blind = parse_posted_blinds(hand) // 2 if parse_posted_blinds(hand) else 0
    if observed in {"raises", "allin"} and hero_action and blind > 0:
        if ctx["limpers"] == 0 and not ctx["facing_raise"] and not ctx["facing_allin"]:
            open_target = hero_amount / blind
            if not (2.5 <= open_target <= 3.0) and observed != "allin":
                breaches.append(f"Preflop sizing: open raise was {open_target:.1f}x BB, PDF target is 2.5-3x")
        elif ctx["limpers"] > 0 and observed == "raises":
            target = 3 * blind + ctx["limpers"] * blind
            if hero_amount != target:
                breaches.append(f"Preflop sizing: iso raise {hero_amount}, PDF formula target is {target}")

        if ctx["facing_raise"] and observed == "raises" and not ctx["facing_allin"]:
            raiser_action = next((a for a in ctx["prior"] if a.action == "raises"), None)
            if raiser_action and raiser_action.amount:
                target_mult = 4.0 if position == "SB" else 3.0
                actual_mult = hero_amount / raiser_action.amount
                # Squeeze sizing is checked separately above, so only apply this when there were no callers.
                if ctx["callers_after_raise"] == 0 and not (hand_notation(hole) in {"AA", "KK", "QQ", "AKs", "AKo"} and "Dynamic squeeze" in reason):
                    if not (target_mult - 0.5 <= actual_mult <= target_mult + 0.5):
                        breaches.append(f"Preflop 3-bet sizing: {actual_mult:.1f}x raise, PDF target is {target_mult:.0f}x")

    return {
        "observed": observed,
        "expected": expected,
        "reason": reason,
        "breaches": breaches,
        "context": ctx,
        "effective_stack": effective_stack,
    }


def audit_street(street: str, hand: str, hero: str, hole: List[Card], board: List[Card],
                 previous_board: List[Card], actions: List[Action], pot_tracker: PotTracker,
                 opponent_actions_history: Dict[str, bool], multiway: bool) -> StreetAudit:
    hero_a = hero_action_in_street(actions, hero)
    hero_action = hero_a.action if hero_a else "none"
    hero_amount = hero_a.amount if hero_a else 0
    idx = first_action_index(actions, hero)
    first_to_act = idx == 0 if idx is not None else False
    pot_before = pot_tracker.pot_before.get(idx) if idx is not None else None
    opp_bet = last_opponent_bet_before_hero(actions, hero)
    opp_pct = pct(opp_bet.amount, pot_before or 0) if opp_bet else None
    texture = board_texture(board)
    hclass = classify_hand(hole, board, "river")
    scary = scary_card(previous_board, board)
    expected = "unknown"
    violations: List[str] = []

    if street == "FLOP":
        if multiway and hclass == "Air" and hero_action in {"bets", "raises", "allin"}:
            violations.append("Flop: multi-way pure bluff with Air violates zero-bluff rule")
        if multiway and hclass == "Showdown" and hero_action in {"bets", "raises", "allin"}:
            bet_pct = pct(hero_amount, pot_before or 0) if hero_action in {"bets", "raises"} else 100.0
            if hero_action == "raises" or bet_pct is None or bet_pct > 33:
                violations.append("Flop: multi-way Showdown aggression exceeds the permitted 25-33% pot exception or raises")
        if multiway and hclass == "Strong Draw" and hero_action in {"bets", "raises", "allin"}:
            violations.append("Flop: PDF says multi-way strong draws should check/call based on price, not drive action")
        if first_to_act:
            expected = flop_first_to_act(hclass, texture)
            if multiway and hclass == "Strong Draw":
                expected = "check/call"
            if not compare_action(hero_action, expected):
                violations.append(f"Flop: first-to-act expected {expected}, observed {hero_action}")
            if hero_a and hero_action in {"bets", "raises"} and hero_amount and pot_before:
                b = pct(hero_amount, pot_before)
                if texture == "Dry (Static)" and hclass == "Premium" and not (25 <= b <= 40):
                    violations.append(f"Flop sizing: Premium dry-board bet {b}% not near 33% target")
                if texture == "Wet (Dynamic)" and hclass == "Premium" and not (70 <= b <= 110):
                    violations.append(f"Flop sizing: Premium wet-board bet {b}% not near 75-100% target")
                if texture == "Wet (Dynamic)" and hclass == "Strong Draw" and not (55 <= b <= 80):
                    violations.append(f"Flop sizing: Strong Draw bet {b}% not near 66% target")
                if texture == "Dry (Static)" and hclass == "Showdown" and b > 40:
                    violations.append(f"Flop sizing: Showdown value bet {b}% exceeds ~33% pot target")
        else:
            if opp_pct is not None:
                limit = flop_call_limit(hclass)
                if limit:
                    max_pct, text = limit
                    # The PDF has a later, wet-board single-pair note allowing calls
                    # through the 50-66% "standard bet" band, so use 66% on wet flops.
                    if texture == "Wet (Dynamic)" and hclass == "Showdown":
                        max_pct = 66.0
                    if opp_pct > max_pct:
                        violations.append(f"Flop call threshold: {hclass} faced {opp_pct}% pot, limit is {max_pct}%")
                        expected = "fold"
                    else:
                        expected = "call"
                elif hclass == "Premium":
                    expected = "raise"
                else:
                    expected = "fold"
                if expected and not compare_action(hero_action, expected):
                    violations.append(f"Flop facing bet: expected {expected}, observed {hero_action}")
                if hclass == "Premium" and opp_bet and hero_action == "raises" and hero_amount and opp_bet.amount:
                    raise_mult = hero_amount / opp_bet.amount
                    if not (2.5 <= raise_mult <= 3.0):
                        violations.append(f"Flop value-raise sizing: {raise_mult:.1f}x opponent bet, PDF target is 2.5-3x")
    elif street == "TURN":
        if first_to_act:
            exact_category = hand_rank(hole + board)[0]
            expected = turn_first_to_act(hclass, scary, exact_category, multiway)
            if not compare_action(hero_action, expected):
                violations.append(f"Turn: first-to-act expected {expected}, observed {hero_action}")
            if hero_a and hero_action in {"bets", "raises"} and hero_amount and pot_before:
                b = pct(hero_amount, pot_before)
                if hclass == "Premium" and not scary and not (60 <= b <= 85):
                    violations.append(f"Turn sizing: Premium blank-card bet {b}% not near 66-75%")
                if hclass == "Strong Draw" and not scary and not (45 <= b <= 75):
                    violations.append(f"Turn sizing: Strong Draw bet {b}% not near 50-66%")
        else:
            if opp_pct is not None:
                limit = turn_call_limit(hclass)
                if limit:
                    max_pct, _ = limit
                    if max_pct == 0.0:
                        expected = "fold"
                    else:
                        expected = "call" if opp_pct <= max_pct else "fold"
                        if opp_pct > max_pct:
                            violations.append(f"Turn call threshold: {hclass} faced {opp_pct}% pot, limit is {max_pct}%")
                else:
                    expected = "fold"
                if not compare_action(hero_action, expected):
                    violations.append(f"Turn facing bet: expected {expected}, observed {hero_action}")
                if hclass == "Premium" and opp_bet and hero_action == "raises" and hero_amount and opp_bet.amount:
                    raise_mult = hero_amount / opp_bet.amount
                    if not (2.5 <= raise_mult <= 3.0):
                        violations.append(f"Turn value-raise sizing: {raise_mult:.1f}x opponent bet, PDF target is 2.5-3x")
        if hclass == "Air" and hero_action in {"bets", "raises"}:
            violations.append("Turn: double-barrel Air is prohibited")
    elif street == "RIVER":
        prior_flop_bet = opponent_actions_history.get("opponent_bet_flop", False)
        prior_turn_bet = opponent_actions_history.get("opponent_bet_turn", False)
        prior = (prior_flop_bet, prior_turn_bet)
        if first_to_act:
            exact_category = hand_rank(hole + board)[0]
            expected = river_first_to_act(hclass, scary, exact_category)
            if not compare_action(hero_action, expected):
                violations.append(f"River: first-to-act expected {expected}, observed {hero_action}")
        else:
            exact_category = hand_rank(hole + board)[0]
            if scary and exact_category in {2, 3}:
                # Source says scary-card Two Pair/Trips are downgraded to Showdown Value.
                if opp_pct is not None and opp_pct < 50:
                    expected = "call"
                else:
                    expected = "fold"
            else:
                expected = river_facing_bet(hole, board, opp_pct, prior, scary, two_pair_quality(hole, board))
            if opp_bet and not compare_action(hero_action, expected):
                violations.append(f"River facing bet: expected {expected}, observed {hero_action}")
        # Narrative checks.
        if hclass == "Showdown" and prior_flop_bet and prior_turn_bet and opp_bet:
            if hero_action == "calls":
                violations.append("River: triple-barrel + single pair should be folded")
        if hclass == "Showdown" and (not prior_flop_bet) and (not prior_turn_bet) and opp_bet:
            # Source specifically authorizes the sudden-wake-up call when the river bet is small.
            if opp_pct is not None and opp_pct < 50 and hero_action != "calls":
                violations.append("River: sudden wake-up with small bet is a call per narrative rule")
        if hclass == "Air" and first_to_act and scary and hero_action == "bets":
            if hero_amount and pot_before:
                b = pct(hero_amount, pot_before)
                if b is not None and b < 75:
                    violations.append("River bluff sizing: terrifying-card pure bluff should be 75-100%+ overbet")
        if hclass == "Premium" and not scary and not first_to_act and opp_bet and hero_action == "calls":
            # Calling is allowed as damage control, so no breach.
            pass

        # Premium river facing a bet: safe-board Two Pair+ should raise; scary-board
        # vulnerable Two Pair/Trips are downgraded to Showdown Value.
        if opp_bet and hclass == "Premium":
            exact_category = hand_rank(hole + board)[0]
            if exact_category in {2, 3} and scary:
                if opp_pct is not None and opp_pct >= 75 and hero_action in {"calls", "raises", "allin"}:
                    violations.append("River: scary card downgrades vulnerable Two Pair/Trips to Showdown Value; heavy bet should not be raised/called automatically")
            elif exact_category in {2, 3} and not scary and hero_action == "calls":
                # The PDF allows damage-control calls in some premium situations, but explicitly
                # requires a raise on safe dry boards when acting against a bet.
                if board_texture(board) == "Dry (Static)":
                    violations.append("River: Premium Two Pair/Set facing a bet on a safe dry board should raise for value")

        # Scary-card downgrade is applied to two pair/set situations.
        if scary and hclass == "Premium" and hand_rank(hole + board)[0] in {2, 3}:
            # We don't force an action because the source says use the Showdown limits after downgrade.
            if opp_bet and opp_pct is not None and opp_pct >= 75 and hero_action == "calls":
                violations.append("River: scary-card downgrade makes vulnerable Two Pair/Trips subject to Showdown limits")

    # When hero makes a value raise and villain re-raises, use the PDF's explicit
    # Two-Pair re-raise decision tree. This applies on flop/turn/river.
    hero_idx = first_action_index(actions, hero)
    if hero_idx is not None and hero_action == "raises":
        later_re_raise = any(a.player != hero and a.action in {"raises", "allin"} for a in actions[hero_idx + 1:])
        if later_re_raise and hclass == "Premium":
            quality = two_pair_quality(hole, board)
            exact_category = hand_rank(hole + board)[0]
            if exact_category == 2 and quality == "Bottom Two Pair":
                if hero_action not in {"folds"}:
                    violations.append("Value-raise re-raise: Bottom Two Pair is a mandatory fold")
            elif exact_category == 2:
                if texture == "Dry (Static)":
                    violations.append("Value-raise re-raise: Two Pair on a dry board should fold")
                else:
                    if hero_action == "folds":
                        violations.append("Value-raise re-raise: Two Pair on a wet board is authorized to call")

    return StreetAudit(
        street=street,
        board=board,
        hero_action=hero_action,
        hero_amount=hero_amount,
        first_to_act=first_to_act,
        pot_before_hero=pot_before,
        opponent_bet=opp_bet.amount if opp_bet else None,
        opponent_bet_pct=opp_pct,
        hand_class=hclass,
        board_texture=texture,
        expected=expected,
        violations=violations,
    )


def compute_multiway(preflop_actions: List[Action]) -> bool:
    players = {a.player for a in preflop_actions}
    folded = {a.player for a in preflop_actions if a.action == "folds"}
    return len(players - folded) >= 3


def audit_hand(hand: str, hero: str, opponent_style: str = "unknown") -> Dict[str, Any]:
    sections = split_street_sections(hand)
    hole = extract_hole_cards(hand, hero)
    preflop_actions = parse_actions(sections.get("PRE-FLOP", ""))
    position = get_position(hand, hero, sections.get("PRE-FLOP", ""))
    stack = extract_stack(hand, hero)
    pre = audit_preflop(hand, hero, hole, position, stack, preflop_actions, opponent_style)

    # If Hero folded pre-flop, the strategy document has no post-flop decision
    # for Hero to audit. A later street in the hand history belongs to the
    # remaining players, so do not manufacture "none" actions or breaches.
    if pre.get("observed") == "folds":
        return {
            "hand_id": extract_hand_id(hand),
            "hole": hand_notation(hole) if hole else "Unknown",
            "position": position,
            "stack": stack,
            "multiway": False,
            "preflop": pre,
            "streets": [],
            "river_required_equity": None,
            "breaches": list(pre.get("breaches", [])),
            "compliant": len(pre.get("breaches", [])) == 0,
            "detailed_class_by_street": {},
        }

    # Boards. Logs may print either only the new card on turn/river or the cumulative board.
    flop = parse_cards(re.search(r"\*\*\* FLOP \*\*\*\s*\[(.*?)\]", hand, re.S).group(1)) if re.search(r"\*\*\* FLOP \*\*\*\s*\[(.*?)\]", hand, re.S) else []
    turn_raw = parse_cards(re.search(r"\*\*\* TURN \*\*\*\s*\[(.*?)\]", hand, re.S).group(1)) if re.search(r"\*\*\* TURN \*\*\*\s*\[(.*?)\]", hand, re.S) else []
    river_raw = parse_cards(re.search(r"\*\*\* RIVER \*\*\*\s*\[(.*?)\]", hand, re.S).group(1)) if re.search(r"\*\*\* RIVER \*\*\*\s*\[(.*?)\]", hand, re.S) else []
    turn = turn_raw if len(turn_raw) == 1 else (turn_raw[-1:] if len(turn_raw) > len(flop) else turn_raw)
    river = river_raw if len(river_raw) == 1 else (river_raw[-1:] if len(river_raw) > len(flop) + len(turn) else river_raw)

    # Actions and pot tracking. The parser assumes numeric action amounts are chips added by that action.
    tracker = PotTracker(parse_posted_blinds(hand))
    all_street_actions = []
    streets = [
        ("PRE-FLOP", preflop_actions),
        ("FLOP", parse_actions(sections.get("FLOP", ""))),
        ("TURN", parse_actions(sections.get("TURN", ""))),
        ("RIVER", parse_actions(sections.get("RIVER", ""))),
    ]
    # Carry pot forward by applying all actions in chronological order.
    for street_name, acts in streets:
        for a in acts:
            tracker.apply(len(tracker.pot_before), a)
        all_street_actions.append(acts)

    multiway = compute_multiway(preflop_actions)
    audits: List[StreetAudit] = []
    opp_history: Dict[str, bool] = {}
    prev_board: List[Card] = []
    cumulative_board = []

    for street_name, board_cards, acts in [
        ("FLOP", flop, all_street_actions[1]),
        ("TURN", flop + turn if turn else [], all_street_actions[2]),
        ("RIVER", flop + turn + river if river else [], all_street_actions[3]),
    ]:
        if not board_cards or len(board_cards) < 3:
            continue
        # Rebuild a local tracker for the street so indexes match street action indexes.
        street_tracker = PotTracker(0)
        running_pot = parse_posted_blinds(hand)
        for sname, sacts in streets:
            if sname == street_name:
                break
            for a in sacts:
                running_pot += a.amount if a.action in {"calls", "bets", "raises", "allin"} else 0
        street_tracker = PotTracker(running_pot)
        for i, a in enumerate(acts):
            street_tracker.apply(i, a)

        if street_name == "RIVER":
            opp_history["opponent_bet_flop"] = bool(all_street_actions[1] and opponent_aggression(all_street_actions[1], hero)["bet"])
            opp_history["opponent_bet_turn"] = bool(all_street_actions[2] and opponent_aggression(all_street_actions[2], hero)["bet"])
        audit = audit_street(
            street_name, hand, hero, hole, board_cards, prev_board, acts,
            street_tracker, opp_history, multiway,
        )
        audits.append(audit)
        prev_board = board_cards

    # River pot odds, when hero calls.
    river_odds = None
    river_actions = all_street_actions[3]
    rhero = hero_action_in_street(river_actions, hero)
    if rhero and rhero.action == "calls":
        idx = first_action_index(river_actions, hero)
        # Reconstruct pot before river hero call.
        pot_before = parse_posted_blinds(hand)
        for acts in all_street_actions[:3]:
            for a in acts:
                if a.action in {"calls", "bets", "raises", "allin"}:
                    pot_before += a.amount
        for a in river_actions[:idx or 0]:
            if a.action in {"calls", "bets", "raises", "allin"}:
                pot_before += a.amount
        river_odds = round(rhero.amount / (pot_before + rhero.amount) * 100, 1) if pot_before + rhero.amount else 0.0

    breaches = list(pre.get("breaches", []))
    for a in audits:
        breaches.extend(a.violations)

    return {
        "hand_id": extract_hand_id(hand),
        "hole": hand_notation(hole) if hole else "Unknown",
        "position": position,
        "stack": stack,
        "multiway": multiway,
        "preflop": pre,
        "streets": audits,
        "river_required_equity": river_odds,
        "breaches": breaches,
        "compliant": len(breaches) == 0,
        "detailed_class_by_street": {a.street: a.hand_class for a in audits},
    }


def print_hand_report(result: Dict[str, Any]) -> None:
    print(f"Hand: {result['hand_id'][:16]}... | Pos: {result['position']} | Hole: {result['hole']} | Stack: {result['stack']} | Multi-way: {result['multiway']}")
    pre = result["preflop"]
    print(f"  PRE-FLOP: observed={pre['observed']} | expected={pre['expected']} | {pre['reason']}")
    for a in result["streets"]:
        board = ",".join(card_key(c) for c in a.board)
        pot = f"{a.pot_before_hero}" if a.pot_before_hero is not None else "?"
        bet = f"{a.opponent_bet_pct:.1f}%" if a.opponent_bet_pct is not None else "?"
        print(
            f"  {a.street}: board=[{board}] | texture={a.board_texture} | class={a.hand_class} | "
            f"action={a.hero_action} {a.hero_amount or ''} | first={a.first_to_act} | pot_before={pot} | opp_bet_pct={bet}"
        )
        for v in a.violations:
            print(f"    [BREACH] {v}")
    if result["river_required_equity"] is not None:
        print(f"  RIVER POT ODDS: {result['river_required_equity']}% required equity")
    if not result["breaches"]:
        print("  STATUS: COMPLIANT")
    else:
        print(f"  STATUS: {len(result['breaches'])} BREACH(ES)")
    print()


def parse_session(file_path: str, target_player: str, opponent_style: str = "unknown") -> None:
    print(f"--- FULL PDF-BASED POKER STRATEGY AUDIT FOR: {target_player} ---\n")
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            raw_data = file.read()
    except FileNotFoundError:
        print(f"No log file found: {file_path}")
        return

    hands = re.split(r"(?=Hand ID:)", raw_data)
    total = 0
    compliant = 0
    breaches = 0

    for hand in hands:
        if not hand.strip() or "Hand ID:" not in hand:
            continue
        hole = extract_hole_cards(hand, target_player)
        if not hole:
            continue
        total += 1
        result = audit_hand(hand, target_player, opponent_style)
        print_hand_report(result)
        if result["compliant"]:
            compliant += 1
        breaches += len(result["breaches"])

    print("=" * 68)
    print("               FULL STRATEGY AUDIT SCORECARD")
    print("=" * 68)
    print(f"Hands Audited:        {total}")
    print(f"Fully Compliant:      {compliant}")
    print(f"Hands With Breaches:  {total - compliant}")
    print(f"Total Rule Breaches:  {breaches}")
    if total:
        print(f"Full-Strategy Rate:   {compliant / total * 100:.1f}%")
    print("=" * 68)


# ---------------------------------------------------------------------------
# Optional direct decision helper
# ---------------------------------------------------------------------------

def recommend_preflop(hole_text: str, position: str, stack_bb: float,
                      context: str = "unopened", limpers: int = 0,
                      call_amount_bb: float = 0.0, effective_stack_bb: Optional[float] = None,
                      raiser_position: str = "LP", opponent_style: str = "unknown") -> Dict[str, str]:
    hole = parse_cards(hole_text)
    if len(hole) != 2:
        return {"action": "error", "reason": "Expected two hole cards like As,Kd"}
    if context == "unopened":
        action, reason = preflop_decision_unopened(hole, position)
    elif context == "limpers":
        action, reason = preflop_vs_limpers(hole, position, limpers)
    elif context == "raise":
        eff = effective_stack_bb or stack_bb
        action, reason = preflop_facing_raise(
            hole, position, int(eff), max(int(call_amount_bb), 1), opponent_style
        )
    elif context == "bb_defense":
        action, reason = bb_defense(hole, raiser_position)
    elif context == "short_sb":
        action, reason = short_stack_sb(hole)
    else:
        return {"action": "error", "reason": f"Unknown context: {context}"}
    return {"action": action, "reason": reason}


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit poker hand histories against the supplied Poker Strategy PDF.")
    parser.add_argument("--log", default="current_session_log.txt")
    parser.add_argument("--player", default="AbhijitJain")
    parser.add_argument(
        "--opponent-style",
        default="unknown",
        choices=["unknown", "tight", "loose", "aggressive", "loose/aggressive"],
        help="Used only for the PDF's QQ/AK vs massive all-in branch.",
    )
    args = parser.parse_args()
    parse_session(os.path.abspath(args.log), args.player, args.opponent_style)


if __name__ == "__main__":
    main()
