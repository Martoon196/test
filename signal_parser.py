"""Turns a Telegram message into a trade signal. Pure functions, no network - see tests/test_parser.py."""
import re
from dataclasses import dataclass, field

SYMBOLS = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
           "GBPJPY", "EURJPY", "EURGBP", "BTCUSD", "ETHUSD", "US30", "NAS100", "US100", "SPX500"]
ALIASES = {
    r"\bGOLD\b": "XAUUSD", r"\bXAU\b": "XAUUSD", r"\bSILVER\b": "XAGUSD",
    r"\bBITCOIN\b": "BTCUSD", r"\bBTC\b": "BTCUSD", r"\bDOW\b": "US30", r"\bNASDAQ\b": "NAS100",
}
SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉", "01234567890123456789")

NUM = r"(?<![A-Z\d.])(\d+(?:\.\d+)?)"
SL_LABEL = r"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS)(?![A-Z])"
# "TP1 2050", "TP 2050", "TP2: 2060", "TAKE PROFIT 3 ➡️ 2070". The index is only taken when it is
# a lone 1-2 digit number, so "TP 2050" is never read as index 2 + price 050.
TP_LABEL = r"\b(?:TP|T/P|TAKE\s*PROFIT)(?:\s*\d{1,2}(?![\d.]))?"
SEP = r"[^\d\n]{0,8}?"
# Result/recap posts repeat the levels but are not new trades.
RECAP_WORDS = r"\b(HIT|HITS|HITTED|SMASHED|RUNNING|CLOSED|SECURED|BOOKED|DONE)\b"

CLOSE_RE = re.compile(r"\b(CLOSE|CLOSING|EXIT)\b(?!\s+(TO|ABOVE|BELOW|NEAR)\b)")
BREAKEVEN_RE = re.compile(r"\b(BREAK\s*EVEN|BREAKEVEN|(SL|STOP\s*LOSS)\s*(TO|AT)\s*(BE|ENTRY|BREAK\s*EVEN))\b")


@dataclass
class Signal:
    symbol: str
    direction: str                      # BUY / SELL
    sl: float
    tps: list = field(default_factory=list)
    entry: float = None                 # single entry price, None = market
    zone: tuple = None                  # (low, high) when an entry range is given
    order_hint: str = "AUTO"            # LIMIT / STOP if the message says so explicitly

    @property
    def reference_price(self):
        if self.zone:
            return sum(self.zone) / 2
        return self.entry


def normalize(text):
    text = text.upper().translate(SUPERSCRIPTS)
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)    # 2,045.50 -> 2045.50
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)            # 2045,50  -> 2045.50
    text = re.sub(r"\b([A-Z]{3})\s*/\s*([A-Z]{3})\b", r"\1\2", text)  # EUR/USD -> EURUSD
    for pattern, symbol in ALIASES.items():
        text = re.sub(pattern, symbol, text)
    return text


def find_symbol(text):
    hits = [(text.find(s), s) for s in SYMBOLS if re.search(rf"\b{s}\b", text)]
    return min(hits)[1] if hits else None


def is_close_message(text):
    return bool(CLOSE_RE.search(normalize(text)))


def is_breakeven_message(text):
    return bool(BREAKEVEN_RE.search(normalize(text)))


def parse_signal(raw_text):
    if not raw_text:
        return None
    text = normalize(raw_text)
    if re.search(RECAP_WORDS, text):
        return None

    direction_match = re.search(r"\b(BUY|SELL|LONG|SHORT)\b", text)
    symbol = find_symbol(text)
    if not direction_match or not symbol:
        return None
    direction = {"LONG": "BUY", "SHORT": "SELL"}.get(direction_match.group(1), direction_match.group(1))

    sl_match = re.search(SL_LABEL + SEP + NUM, text)
    tps = [float(m.group(1)) for m in re.finditer(TP_LABEL + SEP + NUM, text)]
    if not sl_match or not tps:
        return None
    sl = float(sl_match.group(1))

    hint_match = re.search(r"\b(?:BUY|SELL)\s*(LIMIT|STOP)\b", text)
    order_hint = hint_match.group(1) if hint_match else "AUTO"

    entry, zone = _find_entry(text, direction_match.end(), symbol)
    signal = Signal(symbol, direction, sl, tps, entry, zone, order_hint)
    return _validate(signal)


def _find_entry(text, direction_end, symbol):
    """Look for the entry between the BUY/SELL word and the first SL/TP label (or an ENTRY label)."""
    entry_label = re.search(r"\b(?:ENTRY|ENTER|PRICE|ZONE)\b", text)
    start = entry_label.end() if entry_label else direction_end
    stop = re.search(f"{SL_LABEL}|{TP_LABEL}", text[start:])
    segment = text[start:start + stop.start()] if stop else text[start:].split("\n")[0]
    segment = segment.replace(symbol, " ")

    zone_match = re.search(NUM + r"\s*(?:-|–|—|/|TO|~)\s*" + NUM, segment)
    if zone_match:
        low, high = sorted((float(zone_match.group(1)), float(zone_match.group(2))))
        if low > 0 and (high - low) / low < 0.02:      # a real zone is narrow, otherwise it's two prices
            return None, (low, high)
    single = re.search(NUM, segment)
    return (float(single.group(1)) if single else None), None


def _validate(signal):
    """Reject obvious misreads: SL on the wrong side, TPs on the wrong side, numbers wildly off."""
    buy = signal.direction == "BUY"
    ref = signal.reference_price
    if ref is not None:
        if (buy and signal.sl >= ref) or (not buy and signal.sl <= ref):
            return None
        signal.tps = [tp for tp in signal.tps if (tp > ref if buy else tp < ref)]
    else:
        # No entry given: TPs and SL must at least be on opposite sides of each other.
        signal.tps = [tp for tp in signal.tps if (tp > signal.sl if buy else tp < signal.sl)]

    # Drop numbers that are clearly not prices (e.g. "TP1 50 pips" when price is 2000).
    anchor = ref if ref is not None else signal.sl
    signal.tps = [tp for tp in signal.tps if abs(tp - anchor) / anchor < 0.2]

    # Nearest TP first, no duplicates.
    signal.tps = sorted(set(signal.tps), reverse=not buy)
    return signal if signal.tps else None
